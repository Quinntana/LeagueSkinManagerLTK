"""Content-addressed local library for user-authorized LTK mod packages."""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
import unicodedata
import zipfile
from collections.abc import Callable, Iterable, Mapping
from dataclasses import asdict, dataclass
from pathlib import Path
from threading import RLock
from typing import Any, Protocol

from .atomic import atomic_write_json

SCHEMA_VERSION = 1
SUPPORTED_EXTENSIONS = frozenset({".fantome", ".modpkg"})
MAX_PACKAGE_BYTES = 2 * 1024 * 1024 * 1024
MAX_METADATA_BYTES = 1024 * 1024
MAX_ARCHIVE_ENTRIES = 100_000


class ModLibraryError(RuntimeError):
    """A package or library manifest was invalid."""


class PackageInspector(Protocol):
    def inspect_package(self, path: Path) -> Mapping[str, Any]: ...


@dataclass(frozen=True, slots=True)
class ModRecord:
    id: str
    name: str
    author: str
    version: str
    format: str
    champions: tuple[str, ...]
    tags: tuple[str, ...]
    file_name: str
    size: int
    content_sha256: str

    @property
    def category(self) -> str:
        return self.champions[0] if self.champions else "General"

    @property
    def search_text(self) -> str:
        values = " ".join(
            (self.name, self.author, self.version, self.format, *self.champions, *self.tags)
        )
        normalized = normalize_search(values)
        return f"{normalized} {normalized.replace(' ', '')}"

    def to_json(self) -> dict[str, Any]:
        value = asdict(self)
        value["champions"] = list(self.champions)
        value["tags"] = list(self.tags)
        return value

    @classmethod
    def from_json(cls, raw: object) -> ModRecord:
        if not isinstance(raw, dict):
            raise ModLibraryError("library entry must be an object")
        try:
            record = cls(
                id=_clean_text(raw["id"], "id", 64),
                name=_clean_text(raw["name"], "name", 200),
                author=_clean_text(raw["author"], "author", 200),
                version=_clean_text(raw["version"], "version", 80),
                format=_clean_text(raw["format"], "format", 16),
                champions=_clean_list(raw.get("champions", []), "champions"),
                tags=_clean_list(raw.get("tags", []), "tags"),
                file_name=_clean_text(raw["file_name"], "file_name", 100),
                size=int(raw["size"]),
                content_sha256=_clean_text(raw["content_sha256"], "content_sha256", 64),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise ModLibraryError("library entry has invalid fields") from exc
        if record.id != record.content_sha256 or not _is_sha256(record.id):
            raise ModLibraryError("library entry has an invalid content identity")
        expected_name = f"{record.content_sha256}.{record.format}"
        if record.file_name.casefold() != expected_name.casefold():
            raise ModLibraryError("library entry has an unsafe package filename")
        if record.format not in {"fantome", "modpkg"} or record.size <= 0:
            raise ModLibraryError("library entry has an invalid package format or size")
        return record


@dataclass(frozen=True, slots=True)
class ImportResult:
    imported: tuple[ModRecord, ...]
    duplicate_count: int


def normalize_search(value: str) -> str:
    decomposed = unicodedata.normalize("NFKD", value.casefold())
    characters = (
        character if character.isalnum() else " "
        for character in decomposed
        if not unicodedata.combining(character)
    )
    return " ".join("".join(characters).split())


class LocalModLibrary:
    """Own package copies and a small atomic index; never extracts game content."""

    def __init__(
        self,
        package_dir: Path,
        manifest_file: Path,
        *,
        engine: PackageInspector | None = None,
        max_package_bytes: int = MAX_PACKAGE_BYTES,
    ) -> None:
        if max_package_bytes <= 0:
            raise ValueError("max_package_bytes must be positive")
        self.package_dir = Path(package_dir).resolve()
        self.manifest_file = Path(manifest_file).resolve()
        if self.manifest_file.parent != self.package_dir.parent:
            raise ValueError("manifest_file and package_dir must share the library root")
        self.engine = engine
        self.max_package_bytes = max_package_bytes
        self._lock = RLock()

    def import_packages(
        self,
        paths: Iterable[Path],
        *,
        cancelled: Callable[[], bool] | None = None,
    ) -> ImportResult:
        sources = tuple(Path(path) for path in paths)
        if not sources:
            raise ModLibraryError("Select at least one .fantome or .modpkg package")
        with self._lock:
            self.package_dir.mkdir(parents=True, exist_ok=True)
            existing = {record.id: record for record in self._load_manifest()}
            imported: list[ModRecord] = []
            duplicates = 0
            created_files: list[Path] = []
            try:
                for source in sources:
                    if cancelled is not None and cancelled():
                        raise ModLibraryError("Package import was cancelled")
                    # Validate before copying, then inspect the immutable content-addressed
                    # copy so a source-file swap cannot create stale metadata.
                    self._validate_source(source)
                    destination, digest, size, created = self._copy_content_addressed(
                        source,
                        cancelled=cancelled,
                    )
                    if created:
                        created_files.append(destination)
                    if digest in existing:
                        duplicates += 1
                        continue
                    metadata = self._inspect(destination)
                    record = _record_from_metadata(metadata, destination, digest, size)
                    existing[record.id] = record
                    imported.append(record)
                records = tuple(sorted(existing.values(), key=_record_sort_key))
                self._write_manifest(records)
            except Exception:
                # The index is committed only after every package succeeds. Remove
                # only files that did not exist before this batch; repaired cache
                # entries and all pre-existing content are preserved.
                for created_file in created_files:
                    created_file.unlink(missing_ok=True)
                raise
            return ImportResult(tuple(imported), duplicates)

    def refresh(self) -> tuple[ModRecord, ...]:
        """Reconcile the index with content-addressed package files already on disk."""

        with self._lock:
            self.package_dir.mkdir(parents=True, exist_ok=True)
            existing = {record.file_name.casefold(): record for record in self._load_manifest()}
            records: list[ModRecord] = []
            for package in sorted(
                self.package_dir.iterdir(), key=lambda path: path.name.casefold()
            ):
                if not package.is_file() or package.suffix.casefold() not in SUPPORTED_EXTENSIONS:
                    continue
                try:
                    digest, size = _hash_file(package, self.max_package_bytes)
                    expected = f"{digest}{package.suffix.casefold()}"
                    if package.name.casefold() != expected.casefold():
                        continue
                    cached = existing.get(package.name.casefold())
                    if cached is not None and size == cached.size:
                        records.append(cached)
                        continue
                    metadata = self._inspect(package)
                    records.append(_record_from_metadata(metadata, package, digest, size))
                except (OSError, ModLibraryError):
                    continue
            result = tuple(sorted(records, key=_record_sort_key))
            self._write_manifest(result)
            return result

    def records(self) -> tuple[ModRecord, ...]:
        with self._lock:
            return self._load_manifest()

    def _inspect(self, source: Path) -> Mapping[str, Any]:
        resolved = self._validate_source(source)
        if self.engine is not None:
            try:
                return self.engine.inspect_package(resolved)
            except Exception as exc:
                raise ModLibraryError(f"LTK rejected {resolved.name}: {exc}") from exc
        if resolved.suffix.casefold() == ".modpkg":
            raise ModLibraryError(
                ".modpkg inspection requires the open ltk-engine sidecar; build or install it first"
            )
        return inspect_fantome(resolved)

    def _validate_source(self, source: Path) -> Path:
        try:
            resolved = source.expanduser().resolve(strict=True)
        except (OSError, RuntimeError) as exc:
            raise ModLibraryError(f"Package does not exist: {source}") from exc
        if source.is_symlink() or not resolved.is_file():
            raise ModLibraryError("Package must be a regular file, not a link or directory")
        suffix = resolved.suffix.casefold()
        if suffix not in SUPPORTED_EXTENSIONS:
            raise ModLibraryError("Only .fantome and .modpkg packages are supported")
        size = resolved.stat().st_size
        if size <= 0 or size > self.max_package_bytes:
            raise ModLibraryError("Package is empty or exceeds the configured size limit")
        return resolved

    def _copy_content_addressed(
        self,
        source: Path,
        *,
        cancelled: Callable[[], bool] | None = None,
    ) -> tuple[Path, str, int, bool]:
        temporary_name: str | None = None
        digest = hashlib.sha256()
        size = 0
        try:
            with (
                source.open("rb") as input_file,
                tempfile.NamedTemporaryFile(
                    mode="wb",
                    dir=self.package_dir,
                    prefix=".import-",
                    suffix=".tmp",
                    delete=False,
                ) as output_file,
            ):
                temporary_name = output_file.name
                while chunk := input_file.read(1024 * 1024):
                    if cancelled is not None and cancelled():
                        raise ModLibraryError("Package import was cancelled")
                    size += len(chunk)
                    if size > self.max_package_bytes:
                        raise ModLibraryError("Package exceeds the configured size limit")
                    digest.update(chunk)
                    output_file.write(chunk)
                output_file.flush()
                os.fsync(output_file.fileno())
            content_sha256 = digest.hexdigest()
            destination = self.package_dir / f"{content_sha256}{source.suffix.casefold()}"
            if destination.exists():
                existing_digest, existing_size = _hash_file(destination, self.max_package_bytes)
                if existing_digest == content_sha256 and existing_size == size:
                    Path(temporary_name).unlink(missing_ok=True)
                    temporary_name = None
                    return destination, content_sha256, size, False
                # Repair a corrupt digest-named cache entry with the already
                # verified temporary copy. It was pre-existing, so callers must
                # not delete it as part of a later batch rollback.
                os.replace(temporary_name, destination)
                temporary_name = None
                return destination, content_sha256, size, False
            os.replace(temporary_name, destination)
            temporary_name = None
            return destination, content_sha256, size, True
        except OSError as exc:
            raise ModLibraryError(f"Could not copy {source.name}: {exc}") from exc
        finally:
            if temporary_name is not None:
                Path(temporary_name).unlink(missing_ok=True)

    def _load_manifest(self) -> tuple[ModRecord, ...]:
        try:
            raw = json.loads(self.manifest_file.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return ()
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ModLibraryError("The local mod library index is unreadable") from exc
        if not isinstance(raw, dict) or raw.get("schema_version") != SCHEMA_VERSION:
            raise ModLibraryError("The local mod library schema is unsupported")
        entries = raw.get("entries")
        if not isinstance(entries, list):
            raise ModLibraryError("The local mod library has no valid entry list")
        records = tuple(ModRecord.from_json(entry) for entry in entries)
        if len({record.id for record in records}) != len(records):
            raise ModLibraryError("The local mod library contains duplicate identities")
        return tuple(sorted(records, key=_record_sort_key))

    def _write_manifest(self, records: tuple[ModRecord, ...]) -> None:
        self.manifest_file.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_json(
            self.manifest_file,
            {
                "schema_version": SCHEMA_VERSION,
                "entries": [record.to_json() for record in records],
            },
        )


def inspect_fantome(path: Path) -> Mapping[str, Any]:
    """Read only bounded metadata from a legacy package; do not extract archive entries."""

    try:
        with zipfile.ZipFile(path) as archive:
            if len(archive.infolist()) > MAX_ARCHIVE_ENTRIES:
                raise ModLibraryError("Fantome package contains too many entries")
            member = next(
                (name for name in archive.namelist() if name.casefold() == "meta/info.json"),
                None,
            )
            if member is None:
                raise ModLibraryError("Fantome package is missing META/info.json")
            info = archive.getinfo(member)
            if info.file_size <= 0 or info.file_size > MAX_METADATA_BYTES:
                raise ModLibraryError("Fantome metadata is empty or too large")
            if info.compress_size > 0 and info.file_size / info.compress_size > 200:
                raise ModLibraryError("Fantome metadata has an unsafe compression ratio")
            payload = archive.read(info)
    except (OSError, zipfile.BadZipFile, RuntimeError) as exc:
        raise ModLibraryError(f"Invalid Fantome package: {exc}") from exc
    try:
        raw = json.loads(payload.decode("utf-8-sig"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ModLibraryError("Fantome metadata is not valid UTF-8 JSON") from exc
    if not isinstance(raw, dict):
        raise ModLibraryError("Fantome metadata must be an object")
    _validate_fantome_metadata(raw)
    return raw


def _validate_fantome_metadata(raw: Mapping[str, Any]) -> None:
    """Mirror the required public ``ltk_fantome::FantomeInfo`` shape."""

    for field in ("Name", "Author", "Version", "Description"):
        value = raw.get(field)
        if not isinstance(value, str):
            raise ModLibraryError(f"Fantome metadata field {field} must be text")
    for field in ("Tags", "Champions", "Maps"):
        value = raw.get(field, [])
        if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
            raise ModLibraryError(f"Fantome metadata field {field} must be a text list")
    layers = raw.get("Layers", {})
    if not isinstance(layers, dict):
        raise ModLibraryError("Fantome metadata field Layers must be an object")
    for layer in layers.values():
        if not isinstance(layer, dict):
            raise ModLibraryError("Fantome layer metadata must be an object")
        if not isinstance(layer.get("Name"), str) or not isinstance(layer.get("Priority"), int):
            raise ModLibraryError("Fantome layer Name/Priority fields are invalid")


def _record_from_metadata(
    metadata: Mapping[str, Any], destination: Path, digest: str, size: int
) -> ModRecord:
    name = _first_text(metadata, ("display_name", "displayName", "name"), destination.stem, 200)
    authors = _metadata_value(metadata, "authors")
    if isinstance(authors, list):
        author = ", ".join(_author_name(item) for item in authors if _author_name(item))
    else:
        author = _first_text(metadata, ("author",), "Unknown author", 200)
    author = _clean_text(author or "Unknown author", "author", 200)
    version = _first_text(metadata, ("version",), "Unknown", 80)
    champions = _clean_list(_metadata_value(metadata, "champions", []), "champions")
    tags = _clean_list(_metadata_value(metadata, "tags", []), "tags")
    package_format = destination.suffix.casefold().removeprefix(".")
    return ModRecord(
        id=digest,
        name=name,
        author=author,
        version=version,
        format=package_format,
        champions=champions,
        tags=tags,
        file_name=destination.name,
        size=size,
        content_sha256=digest,
    )


def _author_name(value: object) -> str:
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, dict):
        name = _metadata_value(value, "name")
        return name.strip() if isinstance(name, str) else ""
    return ""


def _first_text(
    values: Mapping[str, Any], keys: tuple[str, ...], fallback: str, maximum: int
) -> str:
    for key in keys:
        value = _metadata_value(values, key)
        if isinstance(value, str) and value.strip():
            return _clean_text(value, key, maximum)
    return _clean_text(fallback, keys[0], maximum)


def _metadata_value(values: Mapping[str, Any], key: str, default: object | None = None) -> object:
    wanted = key.casefold()
    for candidate, value in values.items():
        if isinstance(candidate, str) and candidate.casefold() == wanted:
            return value
    return default


def _clean_text(value: object, field: str, maximum: int) -> str:
    if not isinstance(value, str):
        raise ModLibraryError(f"{field} must be text")
    cleaned = " ".join(value.replace("\x00", "").split())
    if not cleaned or len(cleaned) > maximum:
        raise ModLibraryError(f"{field} is empty or too long")
    return cleaned


def _clean_list(value: object, field: str) -> tuple[str, ...]:
    if not isinstance(value, list) or len(value) > 100:
        raise ModLibraryError(f"{field} must be a short list")
    cleaned = tuple(_clean_text(item, field, 100) for item in value)
    return tuple(dict.fromkeys(cleaned))


def _is_sha256(value: str) -> bool:
    return len(value) == 64 and all(character in "0123456789abcdef" for character in value)


def _hash_file(path: Path, maximum: int) -> tuple[str, int]:
    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            size += len(chunk)
            if size > maximum:
                raise ModLibraryError("Package exceeds the configured size limit")
            digest.update(chunk)
    if size <= 0:
        raise ModLibraryError("Package is empty")
    return digest.hexdigest(), size


def _record_sort_key(record: ModRecord) -> tuple[str, str, str]:
    return (record.name.casefold(), record.author.casefold(), record.id)


__all__ = [
    "ImportResult",
    "LocalModLibrary",
    "MAX_PACKAGE_BYTES",
    "ModLibraryError",
    "ModRecord",
    "SUPPORTED_EXTENSIONS",
    "inspect_fantome",
    "normalize_search",
]
