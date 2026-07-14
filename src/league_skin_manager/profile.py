"""Thread-safe persistence for the application's default mod profile."""

from __future__ import annotations

import json
import re
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from threading import RLock

from .atomic import atomic_write_json
from .mod_library import LocalModLibrary, ModRecord

SCHEMA_VERSION = 1
_CONTENT_ID = re.compile(r"[0-9a-f]{64}")


class ProfileError(RuntimeError):
    """The default profile could not be read, validated, or saved."""


@dataclass(frozen=True, slots=True)
class _ProfileSettings:
    enabled_mod_ids: tuple[str, ...] = ()
    game_dir: Path | None = None


class ProfileStore:
    """Persist one default profile and reconcile it with a local mod library."""

    def __init__(self, settings_file: Path, library: LocalModLibrary) -> None:
        self.settings_file = Path(settings_file).resolve()
        self.library = library
        self._lock = RLock()

    def toggle(self, content_id: str) -> bool:
        """Toggle a library mod and return ``True`` when it is now enabled."""

        validated = _validate_content_id(content_id)
        with self._lock:
            records = self.library.records()
            known_ids = {record.id for record in records}
            if validated not in known_ids:
                raise ProfileError("Cannot enable a mod that is not in the local library")

            settings = self._load(validate_game_dir=False)
            enabled = set(settings.enabled_mod_ids) & known_ids
            if validated in enabled:
                enabled.remove(validated)
                is_enabled = False
            else:
                enabled.add(validated)
                is_enabled = True
            self._save(
                _ProfileSettings(
                    enabled_mod_ids=_ordered_ids(records, enabled),
                    game_dir=settings.game_dir,
                )
            )
            return is_enabled

    def set_enabled(self, content_ids: Iterable[str]) -> tuple[str, ...]:
        """Replace the enabled set, dropping valid IDs absent from the library."""

        requested = {_validate_content_id(content_id) for content_id in content_ids}
        with self._lock:
            records = self.library.records()
            enabled_ids = _ordered_ids(records, requested)
            settings = self._load(validate_game_dir=False)
            self._save(_ProfileSettings(enabled_mod_ids=enabled_ids, game_dir=settings.game_dir))
            return enabled_ids

    def enabled_records(self) -> tuple[ModRecord, ...]:
        """Return enabled records and atomically remove IDs no longer in the library."""

        with self._lock:
            records = self.library.records()
            settings = self._load(validate_game_dir=False)
            enabled = set(settings.enabled_mod_ids)
            result = tuple(record for record in records if record.id in enabled)
            reconciled = tuple(record.id for record in result)
            if reconciled != settings.enabled_mod_ids:
                self._save(
                    _ProfileSettings(
                        enabled_mod_ids=reconciled,
                        game_dir=settings.game_dir,
                    )
                )
            return result

    def game_dir(self) -> Path | None:
        """Return the configured, currently valid League ``Game`` directory."""

        with self._lock:
            return self._load().game_dir

    def set_game_dir(self, selected: str | Path) -> Path:
        """Validate a League root or ``Game`` directory, persist it, and return ``Game``."""

        normalized = normalize_game_dir(selected)
        with self._lock:
            settings = self._load(validate_game_dir=False)
            self._save(
                _ProfileSettings(
                    enabled_mod_ids=settings.enabled_mod_ids,
                    game_dir=normalized,
                )
            )
        return normalized

    def clear_game_dir(self) -> None:
        """Remove the configured League directory while preserving enabled mods."""

        with self._lock:
            settings = self._load(validate_game_dir=False)
            self._save(_ProfileSettings(enabled_mod_ids=settings.enabled_mod_ids, game_dir=None))

    def _load(self, *, validate_game_dir: bool = True) -> _ProfileSettings:
        try:
            raw = json.loads(self.settings_file.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return _ProfileSettings()
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ProfileError("The default profile settings are unreadable") from exc

        if not isinstance(raw, dict):
            raise ProfileError("The default profile settings must be a JSON object")
        schema_version = raw.get("schema_version")
        if type(schema_version) is not int or schema_version != SCHEMA_VERSION:
            raise ProfileError("The default profile settings schema is unsupported")

        raw_ids = raw.get("enabled_mod_ids")
        if not isinstance(raw_ids, list):
            raise ProfileError("The default profile enabled_mod_ids must be a list")
        try:
            enabled_mod_ids = tuple(_validate_content_id(value) for value in raw_ids)
        except ProfileError as exc:
            raise ProfileError("The default profile contains an invalid mod content ID") from exc
        if len(set(enabled_mod_ids)) != len(enabled_mod_ids):
            raise ProfileError("The default profile contains duplicate mod content IDs")

        raw_game_dir = raw.get("game_dir")
        if raw_game_dir is None:
            game_dir = None
        elif isinstance(raw_game_dir, str) and raw_game_dir:
            if validate_game_dir:
                try:
                    game_dir = normalize_game_dir(raw_game_dir)
                except ProfileError as exc:
                    raise ProfileError(
                        "The configured League game directory is no longer valid"
                    ) from exc
            else:
                try:
                    game_dir = Path(raw_game_dir).expanduser()
                except (OSError, RuntimeError, ValueError) as exc:
                    raise ProfileError("The default profile game_dir is invalid") from exc
                if not game_dir.is_absolute():
                    raise ProfileError("The default profile game_dir must be absolute")
        else:
            raise ProfileError("The default profile game_dir must be a path or null")
        return _ProfileSettings(enabled_mod_ids=enabled_mod_ids, game_dir=game_dir)

    def _save(self, settings: _ProfileSettings) -> None:
        try:
            atomic_write_json(
                self.settings_file,
                {
                    "schema_version": SCHEMA_VERSION,
                    "enabled_mod_ids": list(settings.enabled_mod_ids),
                    "game_dir": str(settings.game_dir) if settings.game_dir is not None else None,
                },
            )
        except OSError as exc:
            raise ProfileError("Could not save the default profile settings") from exc


def normalize_game_dir(selected: str | Path) -> Path:
    """Normalize a selected League root or ``Game`` directory to the latter."""

    try:
        selected_path = Path(selected).expanduser().resolve(strict=True)
    except (OSError, RuntimeError, TypeError, ValueError) as exc:
        raise ProfileError("The selected League directory does not exist") from exc
    if not selected_path.is_dir():
        raise ProfileError("The selected League path is not a directory")

    game_dir = (
        selected_path if _has_final_data(selected_path) else _child_dir(selected_path, "Game")
    )
    if game_dir is None or not _has_final_data(game_dir):
        raise ProfileError("The selected League directory must contain Game/DATA/FINAL")
    try:
        return game_dir.resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise ProfileError("The selected League Game directory is inaccessible") from exc


def _has_final_data(game_dir: Path) -> bool:
    data_dir = _child_dir(game_dir, "DATA")
    return data_dir is not None and _child_dir(data_dir, "FINAL") is not None


def _child_dir(parent: Path, name: str) -> Path | None:
    direct = parent / name
    if direct.is_dir():
        return direct
    try:
        return next(
            (
                child
                for child in parent.iterdir()
                if child.name.casefold() == name.casefold() and child.is_dir()
            ),
            None,
        )
    except OSError:
        return None


def _validate_content_id(value: object) -> str:
    if not isinstance(value, str) or _CONTENT_ID.fullmatch(value) is None:
        raise ProfileError("Mod content IDs must be lowercase SHA-256 values")
    return value


def _ordered_ids(records: tuple[ModRecord, ...], enabled: set[str]) -> tuple[str, ...]:
    return tuple(record.id for record in records if record.id in enabled)


DefaultProfileStore = ProfileStore

__all__ = [
    "DefaultProfileStore",
    "ProfileError",
    "ProfileStore",
    "SCHEMA_VERSION",
    "normalize_game_dir",
]
