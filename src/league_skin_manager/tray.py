"""Small, callback-driven system tray presentation layer."""

from __future__ import annotations

import importlib
import logging
from collections.abc import Callable
from threading import RLock
from typing import Protocol, cast

from .config import APP_NAME
from .controller import AppState


class TrayIcon(Protocol):
    icon: object
    title: str
    menu: object
    visible: bool

    def run(self, setup: Callable[[TrayIcon], None] | None = None) -> None: ...

    def run_detached(self, setup: Callable[[TrayIcon], None] | None = None) -> None: ...

    def stop(self) -> None: ...

    def notify(self, message: str, title: str | None = None) -> None: ...

    def update_menu(self) -> None: ...


class TrayBackend(Protocol):
    Icon: Callable[[str, object, str, object], TrayIcon]
    Menu: Callable[..., object]
    MenuItem: Callable[..., object]


ImageFactory = Callable[[AppState], object]
Action = Callable[[], object]
StartupGetter = Callable[[], bool]
StartupSetter = Callable[[bool], object]


_STATE_COLORS: dict[AppState, tuple[int, int, int]] = {
    AppState.STARTING: (30, 144, 255),
    AppState.OFFLINE_READY: (100, 149, 237),
    AppState.SYNCING: (255, 193, 7),
    AppState.READY: (46, 204, 113),
    AppState.ERROR: (231, 76, 60),
    AppState.STOPPING: (127, 140, 141),
}


def make_status_icon(state: AppState, size: int = 64) -> object:
    """Create a compact colored icon without requiring a bundled image asset."""

    if size < 16:
        raise ValueError("size must be at least 16")
    from PIL import Image, ImageDraw

    image = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    margin = max(3, size // 8)
    draw.ellipse(
        (margin, margin, size - margin, size - margin),
        fill=_STATE_COLORS[state],
        outline=(255, 255, 255, 230),
        width=max(1, size // 24),
    )
    return image


class TrayApplication:
    """Render lifecycle status and translate menu clicks into safe callbacks.

    ``pystray.Icon.run`` makes the icon visible before invoking its setup
    callback.  Slow initialization therefore begins through ``on_start`` only
    after the tray is available to the user.
    """

    def __init__(
        self,
        *,
        on_start: Action,
        on_show: Action,
        on_sync: Action,
        on_start_patcher: Action,
        on_stop_patcher: Action,
        on_start_manager: Action,
        startup_enabled: StartupGetter,
        set_startup_enabled: StartupSetter,
        on_exit: Action,
        backend: TrayBackend | None = None,
        image_factory: ImageFactory = make_status_icon,
        app_name: str = APP_NAME,
        logger: logging.Logger | None = None,
    ) -> None:
        self._on_start = on_start
        self._on_show = on_show
        self._on_sync = on_sync
        self._on_start_patcher = on_start_patcher
        self._on_stop_patcher = on_stop_patcher
        self._on_start_manager = on_start_manager
        self._startup_enabled = startup_enabled
        self._set_startup_enabled = set_startup_enabled
        self._on_exit = on_exit
        self._backend = backend or self._load_backend()
        self._image_factory = image_factory
        self._app_name = app_name
        self._logger = logger or logging.getLogger(__name__)

        self._lock = RLock()
        self._state = AppState.STARTING
        self._detail = "Starting"
        self._runtime_detail = "Stopped"
        self._runtime_running = False
        self._exit_requested = False
        self._stopped = False
        self._icon = self._backend.Icon(
            self._app_name,
            self._image_factory(self._state),
            self._title(),
            self._build_menu(),
        )

    @property
    def native_icon(self) -> TrayIcon:
        return self._icon

    @property
    def state(self) -> AppState:
        with self._lock:
            return self._state

    @property
    def detail(self) -> str:
        with self._lock:
            return self._detail

    def run(self) -> None:
        """Run the platform tray loop on the calling thread."""

        self._icon.run(setup=self._setup)

    def run_detached(self) -> None:
        """Start the platform tray loop beside another GUI main loop."""

        self._icon.run_detached(setup=self._setup)

    def stop(self) -> None:
        """Stop the platform tray loop; safe to call more than once."""

        with self._lock:
            if self._stopped:
                return
            self._stopped = True
        try:
            self._icon.stop()
        except Exception:
            self._logger.exception("Unable to stop tray icon")

    def update_status(self, state: AppState, detail: str) -> None:
        """Status sink suitable for :class:`AppController`."""

        with self._lock:
            self._state = state
            self._detail = detail
            try:
                self._icon.title = self._title()
                self._icon.icon = self._image_factory(state)
                self._refresh_menu()
            except Exception:
                self._logger.exception("Unable to refresh tray status")

    def update_runtime_status(self, detail: str, running: bool) -> None:
        """Update the patcher status without replacing the library status."""

        with self._lock:
            self._runtime_detail = detail
            self._runtime_running = running
            try:
                self._icon.title = self._title()
                self._refresh_menu()
            except Exception:
                self._logger.exception("Unable to refresh tray runtime status")

    def notify(self, title: str, message: str) -> None:
        """Notification sink suitable for :class:`AppController`."""

        try:
            self._icon.notify(message, title)
        except Exception:
            self._logger.exception("Unable to display tray notification")

    def _setup(self, icon: TrayIcon) -> None:
        icon.visible = True
        self._invoke("application startup", self._on_start)

    def _build_menu(self) -> object:
        with self._lock:
            detail = self._detail
            runtime_detail = self._runtime_detail
        return self._backend.Menu(
            self._backend.MenuItem(
                "Open League Skin Manager LTK",
                self._show_clicked,
                default=True,
            ),
            self._backend.MenuItem(
                f"Library: {detail}",
                None,
                enabled=False,
            ),
            self._backend.MenuItem(
                f"Runtime: {runtime_detail}",
                None,
                enabled=False,
            ),
            self._backend.MenuItem("Refresh library", self._sync_clicked),
            self._backend.MenuItem(
                "Start enabled mods",
                self._start_patcher_clicked,
                enabled=self._start_patcher_enabled,
            ),
            self._backend.MenuItem(
                "Stop mods",
                self._stop_patcher_clicked,
                enabled=self._stop_patcher_enabled,
            ),
            self._backend.MenuItem(
                "Open LTK Manager",
                self._start_manager_clicked,
            ),
            self._backend.MenuItem(
                "Start with Windows",
                self._startup_clicked,
                checked=self._startup_checked,
            ),
            self._backend.MenuItem("Exit", self._exit_clicked),
        )

    def _refresh_menu(self) -> None:
        self._icon.menu = self._build_menu()
        try:
            self._icon.update_menu()
        except (AttributeError, NotImplementedError):
            # Some pystray backends refresh automatically when ``menu`` changes.
            return

    def _title(self) -> str:
        with self._lock:
            return f"{self._app_name} - Library: {self._detail} | Runtime: {self._runtime_detail}"

    def _sync_clicked(self, _icon: TrayIcon, _item: object) -> None:
        self._invoke("library refresh", self._on_sync)

    def _start_patcher_clicked(self, _icon: TrayIcon, _item: object) -> None:
        self._invoke("starting enabled mods", self._on_start_patcher)

    def _stop_patcher_clicked(self, _icon: TrayIcon, _item: object) -> None:
        self._invoke("stopping mods", self._on_stop_patcher)

    def _start_patcher_enabled(self, _item: object) -> bool:
        with self._lock:
            return not self._runtime_running

    def _stop_patcher_enabled(self, _item: object) -> bool:
        with self._lock:
            return self._runtime_running

    def _show_clicked(self, _icon: TrayIcon, _item: object) -> None:
        self._invoke("opening the desktop window", self._on_show)

    def _start_manager_clicked(self, _icon: TrayIcon, _item: object) -> None:
        self._invoke("manager launch", self._on_start_manager)

    def _startup_checked(self, _item: object) -> bool:
        try:
            return bool(self._startup_enabled())
        except Exception:
            self._logger.exception("Unable to read Start with Windows setting")
            return False

    def _startup_clicked(self, _icon: TrayIcon, _item: object) -> None:
        try:
            desired = not self._startup_enabled()
            result = self._set_startup_enabled(desired)
            if result is False:
                self.notify(
                    "Start with Windows",
                    "The startup setting could not be updated.",
                )
                return
            self._refresh_menu()
        except Exception as exc:
            self._logger.exception("Unable to update Start with Windows setting")
            self.notify("Start with Windows", f"Could not update setting: {exc}")

    def _exit_clicked(self, _icon: TrayIcon, _item: object) -> None:
        with self._lock:
            if self._exit_requested:
                return
            self._exit_requested = True
        try:
            try:
                result = self._on_exit()
            except Exception as exc:
                self._logger.exception("Tray callback failed during application shutdown")
                self.notify(
                    "League Skin Manager LTK",
                    f"Could not complete application shutdown: {exc}",
                )
                return
            if result is False:
                self.notify(
                    "Shutdown still in progress",
                    "Background work is still stopping. The app remains active; try Exit again.",
                )
                return
            self.stop()
        finally:
            with self._lock:
                if not self._stopped:
                    self._exit_requested = False

    def _invoke(self, description: str, callback: Action) -> object | None:
        try:
            return callback()
        except Exception as exc:
            self._logger.exception("Tray callback failed during %s", description)
            self.notify(
                "League Skin Manager LTK",
                f"Could not complete {description}: {exc}",
            )
            return None

    @staticmethod
    def _load_backend() -> TrayBackend:
        return cast(TrayBackend, importlib.import_module("pystray"))


__all__ = [
    "Action",
    "ImageFactory",
    "StartupGetter",
    "StartupSetter",
    "TrayApplication",
    "TrayBackend",
    "TrayIcon",
    "make_status_icon",
]
