"""Read-only searchable projection of the local LTK package library."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from .mod_library import SCHEMA_VERSION, ModLibraryError, ModRecord, normalize_search


class CatalogError(RuntimeError):
    """The local mod catalog could not be read safely."""


@dataclass(frozen=True, slots=True)
class CatalogSnapshot:
    mods: tuple[ModRecord, ...]

    @property
    def categories(self) -> tuple[str, ...]:
        values = {champion for mod in self.mods for champion in mod.champions}
        if any(not mod.champions for mod in self.mods):
            values.add("General")
        return tuple(sorted(values, key=str.casefold))

    @property
    def formats(self) -> tuple[str, ...]:
        return tuple(sorted({mod.format for mod in self.mods}, key=str.casefold))

    @property
    def total_bytes(self) -> int:
        return sum(mod.size for mod in self.mods)

    def filtered(self, query: str = "", category: str | None = None) -> tuple[ModRecord, ...]:
        normalized_query = normalize_search(query)
        tokens = tuple(token for token in normalized_query.split() if token)
        if normalized_query and " " not in normalized_query:
            tokens += (normalized_query.replace(" ", ""),)
        wanted_category = category.casefold() if category else None
        return tuple(
            mod
            for mod in self.mods
            if (
                wanted_category is None
                or (
                    wanted_category == "general"
                    and not mod.champions
                    or any(champion.casefold() == wanted_category for champion in mod.champions)
                )
            )
            and all(token in mod.search_text for token in tokens)
        )


def load_catalog(path: Path) -> CatalogSnapshot:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return CatalogSnapshot(())
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise CatalogError("The local mod catalog is unreadable") from exc
    try:
        if not isinstance(raw, dict) or raw.get("schema_version") != SCHEMA_VERSION:
            raise ModLibraryError("unsupported library schema")
        entries = raw.get("entries")
        if not isinstance(entries, list):
            raise ModLibraryError("missing entry list")
        mods = tuple(sorted((ModRecord.from_json(value) for value in entries), key=_sort_key))
        if len({mod.id for mod in mods}) != len(mods):
            raise ModLibraryError("duplicate package identity")
    except ModLibraryError as exc:
        raise CatalogError("The local mod catalog is unreadable") from exc
    return CatalogSnapshot(mods)


def _sort_key(mod: ModRecord) -> tuple[str, str, str]:
    return (mod.name.casefold(), mod.author.casefold(), mod.id)


__all__ = ["CatalogError", "CatalogSnapshot", "ModRecord", "load_catalog"]
