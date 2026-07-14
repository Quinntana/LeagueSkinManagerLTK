from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from league_skin_manager.atomic import atomic_write_json
from league_skin_manager.mod_library import LocalModLibrary, ModRecord
from league_skin_manager.profile import ProfileError, ProfileStore, normalize_game_dir


def record(digit: str, name: str = "Test Mod") -> ModRecord:
    content_id = digit * 64
    return ModRecord(
        id=content_id,
        name=name,
        author="Author",
        version="1.0",
        format="fantome",
        champions=("Ahri",),
        tags=("visual",),
        file_name=f"{content_id}.fantome",
        size=10,
        content_sha256=content_id,
    )


def library(tmp_path: Path, records: tuple[ModRecord, ...]) -> LocalModLibrary:
    root = tmp_path / "library"
    service = LocalModLibrary(root / "packages", root / "library.json")
    atomic_write_json(
        service.manifest_file,
        {"schema_version": 1, "entries": [item.to_json() for item in records]},
    )
    return service


def league_root(tmp_path: Path) -> tuple[Path, Path]:
    root = tmp_path / "League of Legends"
    game = root / "Game"
    (game / "DATA" / "FINAL").mkdir(parents=True)
    return root, game.resolve()


def test_defaults_are_empty_without_creating_a_file(tmp_path: Path) -> None:
    settings_file = tmp_path / "profile.json"
    store = ProfileStore(settings_file, library(tmp_path, ()))

    assert store.enabled_records() == ()
    assert store.game_dir() is None
    assert not settings_file.exists()


def test_toggle_and_set_enabled_persist_schema_one_atomically(tmp_path: Path) -> None:
    first = record("1", "First")
    second = record("2", "Second")
    settings_file = tmp_path / "settings" / "profile.json"
    store = ProfileStore(settings_file, library(tmp_path, (first, second)))

    assert store.toggle(first.id) is True
    assert store.toggle(first.id) is False
    assert store.set_enabled((second.id, first.id)) == (first.id, second.id)
    assert store.enabled_records() == (first, second)

    raw = json.loads(settings_file.read_text(encoding="utf-8"))
    assert raw == {
        "enabled_mod_ids": [first.id, second.id],
        "game_dir": None,
        "schema_version": 1,
    }
    assert not tuple(settings_file.parent.glob("*.tmp"))


def test_unknown_valid_ids_are_reconciled_but_toggle_requires_a_library_mod(
    tmp_path: Path,
) -> None:
    available = record("a")
    missing = record("b").id
    store = ProfileStore(tmp_path / "profile.json", library(tmp_path, (available,)))

    assert store.set_enabled((missing, available.id)) == (available.id,)
    with pytest.raises(ProfileError, match="not in the local library"):
        store.toggle(missing)


def test_enabled_records_prunes_mods_removed_from_the_library(tmp_path: Path) -> None:
    first = record("1", "First")
    removed = record("2", "Removed")
    service = library(tmp_path, (first, removed))
    settings_file = tmp_path / "profile.json"
    store = ProfileStore(settings_file, service)
    store.set_enabled((first.id, removed.id))
    atomic_write_json(
        service.manifest_file,
        {"schema_version": 1, "entries": [first.to_json()]},
    )

    assert store.enabled_records() == (first,)
    assert json.loads(settings_file.read_text(encoding="utf-8"))["enabled_mod_ids"] == [first.id]


def test_game_directory_accepts_league_root_or_game_and_can_be_cleared(
    tmp_path: Path,
) -> None:
    root, game = league_root(tmp_path)
    settings_file = tmp_path / "profile.json"
    store = ProfileStore(settings_file, library(tmp_path, ()))

    assert normalize_game_dir(root) == game
    assert normalize_game_dir(game) == game
    assert store.set_game_dir(root) == game
    assert store.game_dir() == game
    assert json.loads(settings_file.read_text(encoding="utf-8"))["game_dir"] == str(game)

    store.clear_game_dir()
    assert store.game_dir() is None


@pytest.mark.parametrize("selected", ["missing", "not-league"])
def test_game_directory_rejects_missing_or_incomplete_paths(tmp_path: Path, selected: str) -> None:
    path = tmp_path / selected
    if selected == "not-league":
        path.mkdir()

    with pytest.raises(ProfileError, match="does not exist|must contain"):
        normalize_game_dir(path)


@pytest.mark.parametrize(
    "content_id",
    ["short", "A" * 64, "g" * 64, 123],
)
def test_content_ids_must_be_lowercase_sha256(tmp_path: Path, content_id: object) -> None:
    store = ProfileStore(tmp_path / "profile.json", library(tmp_path, ()))

    with pytest.raises(ProfileError, match="lowercase SHA-256"):
        store.set_enabled((content_id,))  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("raw", "message"),
    [
        ("not json", "unreadable"),
        ({"schema_version": 2, "enabled_mod_ids": [], "game_dir": None}, "schema"),
        ({"schema_version": 1, "enabled_mod_ids": "bad", "game_dir": None}, "list"),
        (
            {"schema_version": 1, "enabled_mod_ids": ["a" * 64, "a" * 64], "game_dir": None},
            "duplicate",
        ),
        ({"schema_version": 1, "enabled_mod_ids": [], "game_dir": 42}, "path or null"),
    ],
)
def test_corrupt_settings_fail_clearly(tmp_path: Path, raw: object, message: str) -> None:
    settings_file = tmp_path / "profile.json"
    if isinstance(raw, str):
        settings_file.write_text(raw, encoding="utf-8")
    else:
        settings_file.write_text(json.dumps(raw), encoding="utf-8")
    store = ProfileStore(settings_file, library(tmp_path, ()))

    with pytest.raises(ProfileError, match=message):
        store.game_dir()


def test_configured_game_directory_must_remain_valid(tmp_path: Path) -> None:
    root, game = league_root(tmp_path)
    settings_file = tmp_path / "profile.json"
    store = ProfileStore(settings_file, library(tmp_path, ()))
    store.set_game_dir(root)
    (game / "DATA" / "FINAL").rmdir()

    with pytest.raises(ProfileError, match="no longer valid"):
        store.game_dir()

    # A stale install path must not prevent the user repairing the setting.
    store.clear_game_dir()
    assert store.game_dir() is None


def test_updates_are_thread_safe(tmp_path: Path) -> None:
    available = record("c")
    store = ProfileStore(tmp_path / "profile.json", library(tmp_path, (available,)))

    with ThreadPoolExecutor(max_workers=8) as executor:
        results = tuple(executor.map(lambda _: store.toggle(available.id), range(40)))

    assert results.count(True) == results.count(False) == 20
    assert store.enabled_records() == ()
