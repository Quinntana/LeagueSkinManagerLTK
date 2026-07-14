from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from league_skin_manager.controller import AppState
from league_skin_manager.tray import TrayApplication


@dataclass
class FakeMenuItem:
    text: str
    action: Callable[..., object] | None
    options: dict[str, Any] = field(default_factory=dict)


@dataclass
class FakeMenu:
    items: tuple[FakeMenuItem, ...]


class FakeIcon:
    def __init__(
        self,
        name: str,
        image: object,
        title: str,
        menu: FakeMenu,
    ) -> None:
        self.name = name
        self.icon = image
        self.title = title
        self.menu: object = menu
        self.visible = False
        self.stop_calls = 0
        self.menu_updates = 0
        self.notifications: list[tuple[str | None, str]] = []

    def run(self, setup: Callable[[FakeIcon], None] | None = None) -> None:
        if setup is None:
            self.visible = True
        else:
            setup(self)

    def run_detached(self, setup: Callable[[FakeIcon], None] | None = None) -> None:
        self.run(setup=setup)

    def stop(self) -> None:
        self.stop_calls += 1
        self.visible = False

    def notify(self, message: str, title: str | None = None) -> None:
        self.notifications.append((title, message))

    def update_menu(self) -> None:
        self.menu_updates += 1


class FakeBackend:
    def __init__(self) -> None:
        self.icon: FakeIcon | None = None

    def Icon(
        self,
        name: str,
        image: object,
        title: str,
        menu: object,
    ) -> FakeIcon:
        assert isinstance(menu, FakeMenu)
        self.icon = FakeIcon(name, image, title, menu)
        return self.icon

    def Menu(self, *items: object) -> FakeMenu:
        assert all(isinstance(item, FakeMenuItem) for item in items)
        return FakeMenu(tuple(items))  # type: ignore[arg-type]

    def MenuItem(
        self,
        text: str,
        action: Callable[..., object] | None,
        **options: Any,
    ) -> FakeMenuItem:
        return FakeMenuItem(text, action, options)


def make_tray(
    backend: FakeBackend,
    **overrides: Any,
) -> TrayApplication:
    callbacks: dict[str, Any] = {
        "on_start": lambda: None,
        "on_show": lambda: None,
        "on_sync": lambda: None,
        "on_start_patcher": lambda: None,
        "on_stop_patcher": lambda: None,
        "on_start_manager": lambda: None,
        "startup_enabled": lambda: False,
        "set_startup_enabled": lambda _enabled: None,
        "on_exit": lambda: None,
        "backend": backend,
        "image_factory": lambda state: f"image:{state.name}",
        "app_name": "Test Skin Manager",
    }
    callbacks.update(overrides)
    return TrayApplication(**callbacks)


def menu_items(tray: TrayApplication) -> tuple[FakeMenuItem, ...]:
    menu = tray.native_icon.menu
    assert isinstance(menu, FakeMenu)
    return menu.items


def fake_icon(tray: TrayApplication) -> FakeIcon:
    icon = tray.native_icon
    assert isinstance(icon, FakeIcon)
    return icon


def click(tray: TrayApplication, item: FakeMenuItem) -> None:
    assert item.action is not None
    item.action(tray.native_icon, item)


def test_tray_is_visible_before_startup_callback_runs() -> None:
    backend = FakeBackend()
    callback_visibility: list[bool] = []

    def record_visibility() -> None:
        assert backend.icon is not None
        callback_visibility.append(backend.icon.visible)

    tray = make_tray(
        backend,
        on_start=record_visibility,
    )

    tray.run()

    assert callback_visibility == [True]
    assert fake_icon(tray).visible is True
    items = menu_items(tray)
    assert [item.text for item in items] == [
        "Open League Skin Manager LTK",
        "Library: Starting",
        "Runtime: Stopped",
        "Refresh library",
        "Start enabled mods",
        "Stop mods",
        "Open LTK Manager",
        "Start with Windows",
        "Exit",
    ]
    assert items[0].options["default"] is True
    assert items[1].options["enabled"] is False
    assert items[2].options["enabled"] is False

    start_enabled = items[4].options["enabled"]
    stop_enabled = items[5].options["enabled"]
    assert callable(start_enabled)
    assert callable(stop_enabled)
    assert start_enabled(items[4]) is True
    assert stop_enabled(items[5]) is False


def test_status_sink_updates_title_icon_and_menu() -> None:
    backend = FakeBackend()
    tray = make_tray(backend)

    tray.update_status(AppState.SYNCING, "Downloading 4 of 20")

    assert tray.state is AppState.SYNCING
    assert tray.detail == "Downloading 4 of 20"
    assert tray.native_icon.title == (
        "Test Skin Manager - Library: Downloading 4 of 20 | Runtime: Stopped"
    )
    assert tray.native_icon.icon == "image:SYNCING"
    assert menu_items(tray)[1].text == "Library: Downloading 4 of 20"
    assert fake_icon(tray).menu_updates == 1


def test_runtime_status_is_retained_across_library_updates() -> None:
    backend = FakeBackend()
    tray = make_tray(backend)

    tray.update_runtime_status("Patching League", running=True)

    items = menu_items(tray)
    assert items[1].text == "Library: Starting"
    assert items[2].text == "Runtime: Patching League"
    assert tray.native_icon.title == (
        "Test Skin Manager - Library: Starting | Runtime: Patching League"
    )
    start_enabled = items[4].options["enabled"]
    stop_enabled = items[5].options["enabled"]
    assert callable(start_enabled)
    assert callable(stop_enabled)
    assert start_enabled(items[4]) is False
    assert stop_enabled(items[5]) is True

    tray.update_status(AppState.READY, "3 mods enabled")

    items = menu_items(tray)
    assert items[1].text == "Library: 3 mods enabled"
    assert items[2].text == "Runtime: Patching League"
    assert tray.native_icon.title == (
        "Test Skin Manager - Library: 3 mods enabled | Runtime: Patching League"
    )
    assert fake_icon(tray).menu_updates == 2


def test_menu_actions_toggle_startup_and_exit_only_once() -> None:
    backend = FakeBackend()
    calls: list[str] = []
    startup = {"enabled": False}

    def set_startup(enabled: bool) -> bool:
        startup["enabled"] = enabled
        calls.append(f"startup:{enabled}")
        return True

    tray = make_tray(
        backend,
        on_show=lambda: calls.append("show"),
        on_sync=lambda: calls.append("sync"),
        on_start_patcher=lambda: calls.append("start-patcher"),
        on_stop_patcher=lambda: calls.append("stop-patcher"),
        on_start_manager=lambda: calls.append("manager"),
        startup_enabled=lambda: startup["enabled"],
        set_startup_enabled=set_startup,
        on_exit=lambda: calls.append("exit"),
    )
    items = menu_items(tray)

    click(tray, items[0])
    click(tray, items[3])
    click(tray, items[4])
    click(tray, items[5])
    click(tray, items[6])
    click(tray, items[7])
    refreshed_startup = menu_items(tray)[7]
    checked = refreshed_startup.options["checked"]
    assert callable(checked)
    assert checked(refreshed_startup) is True
    click(tray, items[8])
    click(tray, items[8])

    assert calls == [
        "show",
        "sync",
        "start-patcher",
        "stop-patcher",
        "manager",
        "startup:True",
        "exit",
    ]
    assert fake_icon(tray).stop_calls == 1


def test_callback_errors_are_not_raised_from_tray_handlers() -> None:
    backend = FakeBackend()

    def fail() -> None:
        raise RuntimeError("sync exploded")

    tray = make_tray(backend, on_sync=fail)

    click(tray, menu_items(tray)[3])

    assert fake_icon(tray).notifications == [
        (
            "League Skin Manager LTK",
            "Could not complete library refresh: sync exploded",
        )
    ]


def test_patcher_callback_errors_use_existing_notifications() -> None:
    backend = FakeBackend()

    def fail() -> None:
        raise RuntimeError("provider unavailable")

    tray = make_tray(backend, on_start_patcher=fail)

    click(tray, menu_items(tray)[4])

    assert fake_icon(tray).notifications == [
        (
            "League Skin Manager LTK",
            "Could not complete starting enabled mods: provider unavailable",
        )
    ]


def test_failed_startup_toggle_keeps_existing_state_and_notifies() -> None:
    backend = FakeBackend()
    tray = make_tray(
        backend,
        startup_enabled=lambda: False,
        set_startup_enabled=lambda _enabled: False,
    )

    click(tray, menu_items(tray)[7])

    assert fake_icon(tray).notifications == [
        ("Start with Windows", "The startup setting could not be updated.")
    ]


def test_timed_out_shutdown_keeps_tray_active_and_allows_retry() -> None:
    backend = FakeBackend()
    outcomes = iter((False, True))
    exit_calls = 0

    def exit_app() -> bool:
        nonlocal exit_calls
        exit_calls += 1
        return next(outcomes)

    tray = make_tray(backend, on_exit=exit_app)
    exit_item = menu_items(tray)[8]

    click(tray, exit_item)
    assert exit_calls == 1
    assert fake_icon(tray).stop_calls == 0
    assert fake_icon(tray).notifications[-1] == (
        "Shutdown still in progress",
        "Background work is still stopping. The app remains active; try Exit again.",
    )

    click(tray, exit_item)
    assert exit_calls == 2
    assert fake_icon(tray).stop_calls == 1


def test_shutdown_callback_error_does_not_stop_tray() -> None:
    backend = FakeBackend()

    def fail_exit() -> bool:
        raise RuntimeError("worker state unavailable")

    tray = make_tray(backend, on_exit=fail_exit)

    click(tray, menu_items(tray)[8])

    assert fake_icon(tray).stop_calls == 0
    assert fake_icon(tray).notifications[-1] == (
        "League Skin Manager LTK",
        "Could not complete application shutdown: worker state unavailable",
    )
