from __future__ import annotations

import hashlib
import json
import zipfile
from pathlib import Path
from typing import Any

import pytest

from league_skin_manager.mod_library import (
    LocalModLibrary,
    ModLibraryError,
    ModRecord,
    inspect_fantome,
)


def write_fantome(path: Path, metadata: object | None = None) -> None:
    value = (
        metadata
        if metadata is not None
        else {
            "Name": "Original Ahri Remix",
            "Author": "Creator",
            "Version": "1.2.3",
            "Description": "Original custom effects",
            "Champions": ["Ahri"],
            "Tags": ["visual", "original"],
        }
    )
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("META/info.json", json.dumps(value))
        archive.writestr("WAD/Ahri.wad.client", b"synthetic-test-wad")


def library(tmp_path: Path, **kwargs: Any) -> LocalModLibrary:
    root = tmp_path / "library"
    return LocalModLibrary(root / "packages", root / "library.json", **kwargs)


def test_import_is_content_addressed_searchable_and_persistent(tmp_path: Path) -> None:
    package = tmp_path / "Pretty Name.fantome"
    write_fantome(package)
    expected_hash = hashlib.sha256(package.read_bytes()).hexdigest()
    service = library(tmp_path)

    result = service.import_packages((package,))

    assert result.duplicate_count == 0
    assert len(result.imported) == 1
    record = result.imported[0]
    assert record.id == expected_hash
    assert record.name == "Original Ahri Remix"
    assert record.author == "Creator"
    assert record.category == "Ahri"
    assert "original ahri" in record.search_text
    assert record.file_name == f"{expected_hash}.fantome"
    assert (service.package_dir / record.file_name).read_bytes() == package.read_bytes()
    assert service.records() == (record,)

    raw = json.loads(service.manifest_file.read_text(encoding="utf-8"))
    assert raw["schema_version"] == 1
    assert raw["entries"][0]["content_sha256"] == expected_hash


def test_duplicate_import_does_not_copy_or_duplicate_index(tmp_path: Path) -> None:
    package = tmp_path / "same.fantome"
    write_fantome(package)
    service = library(tmp_path)
    first = service.import_packages((package,)).imported[0]

    duplicate = service.import_packages((package,))

    assert duplicate.imported == ()
    assert duplicate.duplicate_count == 1
    assert service.records() == (first,)
    assert len(tuple(service.package_dir.iterdir())) == 1


def test_reimport_repairs_a_corrupt_content_addressed_copy(tmp_path: Path) -> None:
    package = tmp_path / "same.fantome"
    write_fantome(package)
    expected = package.read_bytes()
    service = library(tmp_path)
    record = service.import_packages((package,)).imported[0]
    destination = service.package_dir / record.file_name
    destination.write_bytes(b"x" * len(expected))

    result = service.import_packages((package,))

    assert result.duplicate_count == 1
    assert destination.read_bytes() == expected
    assert service.records() == (record,)


class FakeEngine:
    def __init__(self, result: dict[str, Any] | None = None) -> None:
        self.result = result or {
            "display_name": "Layered Map Mod",
            "authors": [{"name": "Builder"}, "Coauthor"],
            "version": "4.0",
            "champions": [],
            "tags": ["map"],
        }
        self.paths: list[Path] = []

    def inspect_package(self, path: Path) -> dict[str, Any]:
        self.paths.append(path)
        return self.result


def test_modpkg_metadata_comes_from_open_engine_sidecar(tmp_path: Path) -> None:
    package = tmp_path / "map.modpkg"
    package.write_bytes(b"synthetic modpkg fixture")
    engine = FakeEngine()
    service = library(tmp_path, engine=engine)

    record = service.import_packages((package,)).imported[0]

    assert engine.paths == [(service.package_dir / record.file_name).resolve()]
    assert record.name == "Layered Map Mod"
    assert record.author == "Builder, Coauthor"
    assert record.version == "4.0"
    assert record.category == "General"
    assert record.format == "modpkg"


def test_fantome_uses_open_engine_when_available(tmp_path: Path) -> None:
    package = tmp_path / "map.fantome"
    write_fantome(package)
    engine = FakeEngine()
    service = library(tmp_path, engine=engine)

    record = service.import_packages((package,)).imported[0]

    assert engine.paths == [(service.package_dir / record.file_name).resolve()]
    assert record.name == "Layered Map Mod"


def test_modpkg_without_sidecar_and_engine_rejection_fail_closed(tmp_path: Path) -> None:
    package = tmp_path / "map.modpkg"
    package.write_bytes(b"modpkg")

    with pytest.raises(ModLibraryError, match="requires the open ltk-engine"):
        library(tmp_path).import_packages((package,))

    class FailingEngine:
        def inspect_package(self, _path: Path) -> dict[str, Any]:
            raise RuntimeError("bad header")

    with pytest.raises(ModLibraryError, match="LTK rejected"):
        library(tmp_path / "failure", engine=FailingEngine()).import_packages((package,))


@pytest.mark.parametrize("suffix", (".zip", ".exe", ""))
def test_import_rejects_unsupported_files(tmp_path: Path, suffix: str) -> None:
    package = tmp_path / f"package{suffix}"
    package.write_bytes(b"value")

    with pytest.raises(ModLibraryError, match="Only .fantome and .modpkg"):
        library(tmp_path).import_packages((package,))


def test_import_rejects_empty_missing_oversized_and_malformed_packages(tmp_path: Path) -> None:
    service = library(tmp_path, max_package_bytes=20)
    empty = tmp_path / "empty.fantome"
    empty.write_bytes(b"")
    large = tmp_path / "large.fantome"
    large.write_bytes(b"x" * 21)
    malformed = tmp_path / "malformed.fantome"
    malformed.write_bytes(b"not zip")

    with pytest.raises(ModLibraryError, match="Select at least"):
        service.import_packages(())
    with pytest.raises(ModLibraryError, match="does not exist"):
        service.import_packages((tmp_path / "missing.fantome",))
    with pytest.raises(ModLibraryError, match="empty or exceeds"):
        service.import_packages((empty,))
    with pytest.raises(ModLibraryError, match="empty or exceeds"):
        service.import_packages((large,))
    with pytest.raises(ModLibraryError, match="Invalid Fantome"):
        library(tmp_path / "malformed").import_packages((malformed,))


def test_fantome_metadata_validation_is_bounded(tmp_path: Path) -> None:
    missing = tmp_path / "missing-info.fantome"
    with zipfile.ZipFile(missing, "w") as archive:
        archive.writestr("README.md", "none")
    invalid = tmp_path / "invalid-json.fantome"
    with zipfile.ZipFile(invalid, "w") as archive:
        archive.writestr("META/info.json", b"not json")
    object_required = tmp_path / "list.fantome"
    write_fantome(object_required, ["not", "object"])
    schema_invalid = tmp_path / "schema-invalid.fantome"
    write_fantome(schema_invalid, {})

    with pytest.raises(ModLibraryError, match="missing META"):
        inspect_fantome(missing)
    with pytest.raises(ModLibraryError, match="UTF-8 JSON"):
        inspect_fantome(invalid)
    with pytest.raises(ModLibraryError, match="must be an object"):
        inspect_fantome(object_required)
    with pytest.raises(ModLibraryError, match="field Name must be text"):
        inspect_fantome(schema_invalid)


def test_refresh_reconciles_removed_and_content_addressed_files(tmp_path: Path) -> None:
    source = tmp_path / "source.fantome"
    write_fantome(source)
    service = library(tmp_path)
    record = service.import_packages((source,)).imported[0]

    assert service.refresh() == (record,)
    (service.package_dir / record.file_name).unlink()
    assert service.refresh() == ()

    write_fantome(
        source,
        {
            "Name": "New Mod",
            "Author": "New",
            "Version": "1",
            "Description": "New",
        },
    )
    digest = hashlib.sha256(source.read_bytes()).hexdigest()
    destination = service.package_dir / f"{digest}.fantome"
    destination.write_bytes(source.read_bytes())
    refreshed = service.refresh()
    assert [value.name for value in refreshed] == ["New Mod"]


def test_refresh_ignores_wrongly_named_or_corrupt_packages(tmp_path: Path) -> None:
    service = library(tmp_path)
    service.package_dir.mkdir(parents=True)
    wrong = service.package_dir / f"{'a' * 64}.fantome"
    write_fantome(wrong)
    corrupt = service.package_dir / f"{'b' * 64}.fantome"
    corrupt.write_bytes(b"invalid")

    assert service.refresh() == ()


def test_refresh_rejects_same_size_tampering_of_a_cached_package(tmp_path: Path) -> None:
    source = tmp_path / "source.fantome"
    write_fantome(source)
    service = library(tmp_path)
    record = service.import_packages((source,)).imported[0]
    destination = service.package_dir / record.file_name
    destination.write_bytes(b"x" * record.size)

    assert service.refresh() == ()


def test_failed_batch_rolls_back_new_package_files(tmp_path: Path) -> None:
    valid = tmp_path / "valid.fantome"
    invalid = tmp_path / "invalid.fantome"
    write_fantome(valid)
    invalid.write_bytes(b"not a zip")
    service = library(tmp_path)

    with pytest.raises(ModLibraryError, match="Invalid Fantome"):
        service.import_packages((valid, invalid))

    assert tuple(service.package_dir.iterdir()) == ()
    assert service.records() == ()


def test_cancelled_import_removes_temporary_and_new_files(tmp_path: Path) -> None:
    package = tmp_path / "cancel.fantome"
    write_fantome(package)
    service = library(tmp_path)
    calls = 0

    def cancelled() -> bool:
        nonlocal calls
        calls += 1
        return calls >= 2

    with pytest.raises(ModLibraryError, match="cancelled"):
        service.import_packages((package,), cancelled=cancelled)

    assert tuple(service.package_dir.iterdir()) == ()


def test_manifest_and_record_validation_reject_tampering(tmp_path: Path) -> None:
    service = library(tmp_path)
    service.manifest_file.parent.mkdir(parents=True)
    service.manifest_file.write_text("not-json", encoding="utf-8")
    with pytest.raises(ModLibraryError, match="unreadable"):
        service.records()

    digest = "a" * 64
    raw = {
        "id": digest,
        "name": "Name",
        "author": "Author",
        "version": "1",
        "format": "fantome",
        "champions": [],
        "tags": [],
        "file_name": "../escape.fantome",
        "size": 1,
        "content_sha256": digest,
    }
    with pytest.raises(ModLibraryError, match="unsafe package filename"):
        ModRecord.from_json(raw)

    raw["file_name"] = f"{digest}.fantome"
    raw["content_sha256"] = "bad"
    with pytest.raises(ModLibraryError, match="invalid content identity"):
        ModRecord.from_json(raw)


def test_constructor_requires_one_library_root(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="share the library root"):
        LocalModLibrary(tmp_path / "one" / "packages", tmp_path / "two" / "library.json")
    with pytest.raises(ValueError, match="positive"):
        library(tmp_path, max_package_bytes=0)
