from __future__ import annotations

from pathlib import Path
from threading import Event
from typing import Any

import pytest

from league_skin_manager.catalog import CatalogError, CatalogSnapshot
from league_skin_manager.controller import AppState
from league_skin_manager.desktop import DesktopApplication, format_package_size
from league_skin_manager.mod_library import ModRecord


@pytest.mark.parametrize(
    ("value", "expected"),
    ((0, "0 B"), (1023, "1023 B"), (1024, "1.0 KB"), (1536, "1.5 KB")),
)
def test_format_package_size(value: int, expected: str) -> None:
    assert format_package_size(value) == expected


def test_format_package_size_rejects_negative_values() -> None:
    with pytest.raises(ValueError, match="negative"):
        format_package_size(-1)


class FakeVar:
    def __init__(self, value: object = "") -> None:
        self.value = value

    def get(self) -> object:
        return self.value

    def set(self, value: object) -> None:
        self.value = value


class FakeBox:
    def __init__(self) -> None:
        self.values: tuple[str, ...] = ()

    def configure(self, **values: object) -> None:
        self.values = values["values"]  # type: ignore[assignment]


class FakeTree:
    def __init__(self) -> None:
        self.items: dict[str, tuple[object, ...]] = {}
        self.selected: tuple[str, ...] = ()

    def get_children(self) -> tuple[str, ...]:
        return tuple(self.items)

    def delete(self, *items: str) -> None:
        for item in items:
            self.items.pop(item, None)

    def insert(self, _parent: str, _position: str, *, iid: str, values: tuple[object, ...]) -> None:
        self.items[iid] = values

    def selection(self) -> tuple[str, ...]:
        return self.selected

    def selection_set(self, *items: str) -> None:
        self.selected = tuple(items)


class FakeButton:
    def __init__(self) -> None:
        self.state = "normal"

    def configure(self, **values: object) -> None:
        self.state = str(values["state"])


class FakeRoot:
    def __init__(self) -> None:
        self.actions: list[str] = []
        self.cancelled: list[str] = []

    def after(self, _milliseconds: int, _callback: object) -> str:
        self.actions.append("after")
        return "after-id"

    def after_cancel(self, identifier: str) -> None:
        self.cancelled.append(identifier)

    def deiconify(self) -> None:
        self.actions.append("show")

    def lift(self) -> None:
        self.actions.append("lift")

    def focus_force(self) -> None:
        self.actions.append("focus")

    def withdraw(self) -> None:
        self.actions.append("hide")

    def destroy(self) -> None:
        self.actions.append("destroy")


def record(name: str, author: str, champion: str, size: int, digit: str) -> ModRecord:
    digest = digit * 64
    return ModRecord(
        id=digest,
        name=name,
        author=author,
        version="2.1",
        format="fantome",
        champions=(champion,),
        tags=(),
        file_name=f"{digest}.fantome",
        size=size,
        content_sha256=digest,
    )


def make_desktop(tmp_path: Path, **overrides: Any) -> DesktopApplication:
    values: dict[str, Any] = {
        "catalog_path": tmp_path / "library" / "library.json",
        "package_dir": tmp_path / "library" / "packages",
        "data_dir": tmp_path / "data",
        "log_file": tmp_path / "data" / "logs" / "LeagueSkinManagerLTK.log",
        "on_refresh": lambda: True,
        "on_import": lambda _paths: True,
        "on_start_manager": lambda: True,
        "on_exit": lambda: True,
        "startup_enabled": lambda: False,
        "set_startup_enabled": lambda _enabled: True,
        "path_opener": lambda _path: None,
        "enabled_ids": lambda: (),
        "on_toggle_mod": lambda _content_id: False,
        "on_start_patcher": lambda: True,
        "on_stop_patcher": lambda: True,
        "game_dir": lambda: None,
        "set_game_dir": lambda path: path,
        "directory_selector": lambda: None,
        "package_selector": lambda: (),
        "catalog_loader": lambda _path: CatalogSnapshot(()),
    }
    values.update(overrides)
    return DesktopApplication(**values)


def attach_fakes(app: DesktopApplication) -> tuple[FakeRoot, FakeTree]:
    root = FakeRoot()
    tree = FakeTree()
    app._root = root
    app._tree = tree
    app._search_var = FakeVar("")
    app._champion_var = FakeVar(app.ALL_CATEGORIES)
    app._result_var = FakeVar()
    app._status_var = FakeVar()
    app._stats_var = FakeVar()
    app._detail_var = FakeVar()
    app._startup_var = FakeVar(False)
    app._game_dir_var = FakeVar("Not configured")
    app._champion_box = FakeBox()
    app._start_button = FakeButton()
    app._stop_button = FakeButton()
    app._toggle_button = FakeButton()
    app._game_dir_button = FakeButton()
    return root, tree


def finish_exit(app: DesktopApplication) -> None:
    worker = app._exit_thread
    assert worker is not None
    worker.join(1)
    app._drain_events()


def test_presenter_loads_filters_sorts_selects_and_opens_package(tmp_path: Path) -> None:
    catalog = CatalogSnapshot(
        (
            record("Élémental K_DA", "Bảo", "Lux", 4096, "a"),
            record("Star Guardian Remix", "Ari", "Ahri", 2048, "b"),
        )
    )
    opened: list[Path] = []
    app = make_desktop(tmp_path, catalog_loader=lambda _path: catalog, path_opener=opened.append)
    _root, tree = attach_fakes(app)

    app._load_catalog_now()
    assert app._stats_var.get() == "0 enabled  •  2 mods  •  2 categories  •  6.0 KB on disk"
    assert app._champion_box.values == (app.ALL_CATEGORIES, "Ahri", "Lux")
    assert [values[1] for values in tree.items.values()] == [
        "Star Guardian Remix",
        "Élémental K_DA",
    ]

    app._search_var.set("element bao")
    app._apply_filter()
    assert list(tree.items.values()) == [("Off", "Élémental K_DA", "Bảo", "FANTOME", "4.0 KB")]

    app._search_var.set("")
    app._champion_var.set("Ahri")
    app._apply_filter()
    assert list(tree.items.values())[0][1] == "Star Guardian Remix"

    app._champion_var.set(app.ALL_CATEGORIES)
    app._sort_by("size")
    app._sort_by("size")
    tree.selected = (catalog.mods[0].id,)
    app._selection_changed()
    assert "Élémental K_DA" in app._detail_var.get()
    app._open_selected()
    assert opened == [tmp_path / "library" / "packages" / catalog.mods[0].file_name]


def test_presenter_marshals_events_and_invokes_import(tmp_path: Path) -> None:
    selected = (tmp_path / "one.fantome", tmp_path / "two.modpkg")
    imports: list[tuple[Path, ...]] = []
    loads: list[Path] = []
    app = make_desktop(
        tmp_path,
        package_selector=lambda: selected,
        on_import=lambda paths: imports.append(paths) or True,
        catalog_loader=lambda path: loads.append(path) or CatalogSnapshot(()),
    )
    root, _tree = attach_fakes(app)

    app._import_clicked()
    app.show()
    app.hide()
    app.refresh_catalog()
    app.update_status(AppState.READY, "Ready - 2 local mods")
    app._drain_events()

    assert imports == [selected]
    assert root.actions[:4] == ["show", "lift", "focus", "hide"]
    assert app._status_var.get() == "Ready - 2 local mods"
    assert len(loads) == 2


def test_presenter_toggles_extended_selection_and_reveals_package(tmp_path: Path) -> None:
    first = record("First", "Author A", "Ahri", 1024, "a")
    second = record("Second", "Author B", "Lux", 2048, "b")
    catalog = CatalogSnapshot((first, second))
    enabled = {first.id}
    toggled: list[str] = []
    revealed: list[Path] = []
    opened: list[Path] = []

    def toggle(content_id: str) -> bool:
        toggled.append(content_id)
        if content_id in enabled:
            enabled.remove(content_id)
            return False
        enabled.add(content_id)
        return True

    app = make_desktop(
        tmp_path,
        catalog_loader=lambda _path: catalog,
        enabled_ids=lambda: tuple(enabled),
        on_toggle_mod=toggle,
        package_revealer=revealed.append,
        path_opener=opened.append,
    )
    _root, tree = attach_fakes(app)
    app._load_catalog_now()

    assert tree.items[first.id][0] == "On"
    assert tree.items[second.id][0] == "Off"
    assert app._stats_var.get() == "1 enabled  •  2 mods  •  2 categories  •  3.0 KB on disk"

    tree.selected = (first.id, second.id)
    app._selection_changed()
    assert app._toggle_button.state == "normal"
    assert app._detail_var.get() == "2 mods selected • 1 currently enabled"
    app._toggle_selected()

    assert toggled == [first.id, second.id]
    assert tree.items[first.id][0] == "Off"
    assert tree.items[second.id][0] == "On"
    assert "Updated 2 mods" in app._status_var.get()

    tree.selected = (second.id,)
    app._open_selected()
    assert revealed == [tmp_path / "library" / "packages" / second.file_name]
    assert opened == []


def test_presenter_controls_game_directory_and_runtime_status(tmp_path: Path) -> None:
    mod = record("Runtime Mod", "Author", "Ahri", 1024, "c")
    selected_root = tmp_path / "League"
    normalized_game = selected_root / "Game"
    starts: list[str] = []
    stops: list[str] = []
    selections: list[Path] = []

    def save_game_dir(path: Path) -> Path:
        selections.append(path)
        return path / "Game"

    app = make_desktop(
        tmp_path,
        catalog_loader=lambda _path: CatalogSnapshot((mod,)),
        enabled_ids=lambda: (mod.id,),
        on_start_patcher=lambda: starts.append("start") or True,
        on_stop_patcher=lambda: stops.append("stop") or True,
        directory_selector=lambda: selected_root,
        set_game_dir=save_game_dir,
    )
    root, _tree = attach_fakes(app)
    app._load_catalog_now()
    assert app._start_button.state == "disabled"

    app._choose_game_dir()
    assert selections == [selected_root]
    assert app._game_dir_var.get() == str(normalized_game)
    assert app._start_button.state == "normal"

    app._start_patcher_clicked()
    assert starts == ["start"]
    assert app._start_button.state == "disabled"
    assert app._stop_button.state == "disabled"

    app.update_runtime_status("Patcher is running", True)
    app._drain_events()
    assert app._status_var.get() == "Patcher is running"
    assert app._stop_button.state == "normal"
    assert app._game_dir_button.state == "disabled"

    app._stop_patcher_clicked()
    assert stops == ["stop"]
    assert app._stop_button.state == "disabled"
    app.update_runtime_status("Runtime is stopped", False)
    app._drain_events()
    assert app._start_button.state == "normal"
    assert app._game_dir_button.state == "normal"
    assert root.actions.count("after") == 2


def test_presenter_runtime_and_profile_callback_failures_are_visible(tmp_path: Path) -> None:
    mod = record("Broken", "Author", "Ahri", 1024, "d")

    def fail_toggle(_content_id: str) -> bool:
        raise RuntimeError("profile locked")

    app = make_desktop(
        tmp_path,
        catalog_loader=lambda _path: CatalogSnapshot((mod,)),
        enabled_ids=lambda: (mod.id,),
        on_toggle_mod=fail_toggle,
        on_start_patcher=lambda: False,
        on_stop_patcher=lambda: False,
        directory_selector=lambda: tmp_path,
        set_game_dir=lambda _path: (_ for _ in ()).throw(RuntimeError("invalid install")),
    )
    _root, tree = attach_fakes(app)
    app._load_catalog_now()
    tree.selected = (mod.id,)

    app._toggle_selected()
    assert app._status_var.get() == "Could not toggle Broken: profile locked"
    app._choose_game_dir()
    assert app._status_var.get() == "Could not set League Game directory: invalid install"

    app._game_dir_value = tmp_path / "Game"
    app._start_patcher_clicked()
    assert app._status_var.get() == "Could not start patcher: the start request was rejected"
    app._runtime_running = True
    app._stop_patcher_clicked()
    assert app._status_var.get() == "Could not stop patcher: the stop request was rejected"


def test_presenter_actions_report_failures_and_preserve_startup_state(tmp_path: Path) -> None:
    def fail_open(_path: Path) -> None:
        raise OSError("shell unavailable")

    app = make_desktop(
        tmp_path,
        on_refresh=lambda: False,
        package_selector=lambda: (tmp_path / "bad.fantome",),
        on_import=lambda _paths: False,
        on_start_manager=lambda: False,
        set_startup_enabled=lambda _enabled: False,
        path_opener=fail_open,
    )
    root, tree = attach_fakes(app)

    app._refresh_clicked()
    assert app._status_var.get() == "Library refresh was not started"
    app._import_clicked()
    assert app._status_var.get() == "Mod import was not started"
    app._manager_clicked()
    assert app._status_var.get() == "LTK Manager could not be started"
    app._startup_var.set(True)
    app._startup_clicked()
    assert app._startup_var.get() is False
    app._open_path(tmp_path)
    assert "Could not open" in app._status_var.get()
    tree.selected = ()
    app._open_selected()
    assert app._status_var.get() == "Select a mod first"

    app._filter_after_id = "old-filter"
    app._filter_changed()
    assert root.cancelled == ["old-filter"]


def test_desktop_exit_remains_responsive_and_retries(tmp_path: Path) -> None:
    started = Event()
    release = Event()
    outcomes = iter((False, True))

    def stop_application() -> bool:
        started.set()
        release.wait(1)
        return next(outcomes)

    app = make_desktop(tmp_path, on_exit=stop_application)
    root, _tree = attach_fakes(app)
    app._exit_clicked()
    assert started.wait(1)
    assert "destroy" not in root.actions
    release.set()
    finish_exit(app)
    assert "still stopping" in app._status_var.get()

    app._exit_clicked()
    finish_exit(app)
    assert root.actions[-1] == "destroy"


def test_catalog_error_keeps_existing_rows(tmp_path: Path) -> None:
    app = make_desktop(
        tmp_path,
        catalog_loader=lambda _path: (_ for _ in ()).throw(CatalogError("broken catalog")),
    )
    _root, tree = attach_fakes(app)
    tree.items["keep"] = ("Keep", "Author", "MODPKG", "1 KB")

    app._load_catalog_now()

    assert app._status_var.get() == "broken catalog"
    assert "keep" in tree.items
