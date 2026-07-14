from __future__ import annotations

import json
from pathlib import Path

import pytest

from league_skin_manager.catalog import CatalogError, load_catalog
from league_skin_manager.mod_library import ModRecord


def record(name: str, author: str, champion: str, package_format: str, digit: str) -> ModRecord:
    digest = digit * 64
    return ModRecord(
        id=digest,
        name=name,
        author=author,
        version="1.0.0",
        format=package_format,
        champions=(champion,) if champion else (),
        tags=("visual",),
        file_name=f"{digest}.{package_format}",
        size=1200,
        content_sha256=digest,
    )


def write_catalog(path: Path) -> tuple[ModRecord, ...]:
    records = (
        record("Star Guardian Remix", "Ari", "Ahri", "fantome", "a"),
        record("Élémental K_DA", "Bảo", "Lux", "modpkg", "b"),
        record("Map ambience", "Creator", "", "modpkg", "c"),
    )
    path.write_text(
        json.dumps({"schema_version": 1, "entries": [value.to_json() for value in records]}),
        encoding="utf-8",
    )
    return records


def test_catalog_loads_stats_and_searches_normalized_metadata(tmp_path: Path) -> None:
    path = tmp_path / "library.json"
    write_catalog(path)

    catalog = load_catalog(path)

    assert catalog.categories == ("Ahri", "General", "Lux")
    assert catalog.formats == ("fantome", "modpkg")
    assert catalog.total_bytes == 3600
    assert [mod.name for mod in catalog.filtered("guardian ari", "Ahri")] == ["Star Guardian Remix"]
    assert [mod.name for mod in catalog.filtered("element kda bao")] == ["Élémental K_DA"]
    assert catalog.filtered("lux", "Ahri") == ()


def test_missing_catalog_is_an_empty_first_run(tmp_path: Path) -> None:
    catalog = load_catalog(tmp_path / "missing.json")

    assert catalog.mods == ()
    assert catalog.categories == ()
    assert catalog.total_bytes == 0


@pytest.mark.parametrize(
    "payload",
    (
        "not json",
        json.dumps({"schema_version": 9, "entries": []}),
        json.dumps({"schema_version": 1, "entries": "wrong"}),
    ),
)
def test_malformed_or_untrusted_catalog_fails_closed(tmp_path: Path, payload: str) -> None:
    path = tmp_path / "library.json"
    path.write_text(payload, encoding="utf-8")

    with pytest.raises(CatalogError, match="unreadable"):
        load_catalog(path)
