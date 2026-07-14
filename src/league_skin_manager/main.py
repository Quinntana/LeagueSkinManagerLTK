"""Application composition root for the Python UI and isolated LTK engine."""

from __future__ import annotations

import logging
import sys
from argparse import ArgumentParser
from collections.abc import Callable
from pathlib import Path
from threading import Event, Lock, Thread

from .config import APP_NAME, LEAGUE_PROCESS_NAME, AppPaths, RuntimeConfig
from .controller import AppController, AppState
from .desktop import DesktopApplication
from .logging_setup import configure_logging
from .ltk_engine import LTKEngineClient, LTKInstallationLocator
from .mod_library import LocalModLibrary
from .process_monitor import LeagueProcessMonitor, WindowsProcessLookup
from .profile import ProfileStore
from .runtime import RuntimeCoordinator, RuntimePhase, RuntimeStatus
from .tray import TrayApplication
from .windows_integration import (
    InstanceActivationEvent,
    ProcessLauncher,
    SingleInstanceMutex,
    StartupRegistration,
    open_path,
    reveal_path,
    running_executable,
)
from .workflow import LibraryWorkflow


def _show_startup_error(error: BaseException, log_file: Path | None) -> None:
    """Make fatal no-console startup failures visible to the user."""

    if sys.platform != "win32":
        return
    detail = str(error).strip() or type(error).__name__
    diagnostic = (
        f"\n\nDiagnostic log:\n{log_file}"
        if log_file is not None
        else "\n\nThe diagnostic log could not be initialized."
    )
    message = (
        "League Skin Manager LTK could not start.\n\n"
        f"{detail}{diagnostic}\n\n"
        "The application was not left running in the system tray."
    )
    try:
        import ctypes

        ctypes.windll.user32.MessageBoxW(
            None,
            message,
            "League Skin Manager LTK - Startup failed",
            0x00000010 | 0x00040000,
        )
    except Exception:
        logging.getLogger("league_skin_manager").exception(
            "Could not display the startup failure dialog"
        )


def _wait_for_workers(
    controller: AppController,
    timeout_seconds: float,
    logger: logging.Logger,
) -> None:
    while not controller.shutdown(timeout_seconds):
        logger.warning("Background work is still stopping; retaining app resources and mutex")


def _wait_for_runtime(
    runtime: RuntimeCoordinator,
    timeout_seconds: float,
    logger: logging.Logger,
) -> None:
    while not runtime.close(timeout_seconds):
        logger.warning("LTK runtime is still stopping; retaining app resources and mutex")


def _listen_for_activation(
    activation_event: InstanceActivationEvent,
    stop_event: Event,
    on_activate: Callable[[], object],
    logger: logging.Logger,
) -> None:
    while not stop_event.is_set():
        try:
            activated = activation_event.wait(250)
        except Exception:
            logger.exception("Application activation listener failed")
            return
        if not activated or stop_event.is_set():
            continue
        try:
            on_activate()
        except Exception:
            logger.exception("Could not show the desktop after application activation")


def run(*, sync_on_start: bool = True, show_window: bool = True) -> int:
    logger = logging.getLogger("league_skin_manager")
    if sys.platform != "win32":
        logger.error("%s is Windows-only", APP_NAME)
        return 1

    activation_candidate = InstanceActivationEvent()
    activation_event: InstanceActivationEvent | None = activation_candidate
    try:
        activation_candidate.create()
    except Exception:
        logger.exception("Could not create the application activation event")
        activation_candidate.close()
        activation_event = None

    mutex = SingleInstanceMutex()
    if not mutex.acquire():
        if activation_event is not None:
            try:
                activation_event.signal()
                logger.info("Asked the active application instance to show its desktop")
            except Exception:
                logger.exception("Could not activate the existing application instance")
            finally:
                activation_event.close()
        return 2

    controller: AppController | None = None
    desktop: DesktopApplication | None = None
    tray: TrayApplication | None = None
    runtime_service: RuntimeCoordinator | None = None
    activation_stop = Event()
    activation_thread: Thread | None = None
    diagnostic_log: Path | None = None
    runtime = RuntimeConfig()
    try:
        paths = AppPaths.discover()
        paths.ensure()
        diagnostic_log = paths.log_dir / f"{APP_NAME}.log"
        logger = configure_logging(paths.log_dir)
        launcher = ProcessLauncher(logger.getChild("launcher"))
        locator = LTKInstallationLocator()
        engine = LTKEngineClient.discover(
            paths.project_root,
            timeout_seconds=runtime.engine_timeout_seconds,
            overlay_timeout_seconds=runtime.overlay_timeout_seconds,
        )
        library = LocalModLibrary(
            paths.package_dir,
            paths.library_manifest_file,
            engine=engine,
        )
        profile = ProfileStore(paths.default_profile_file, library)
        workflow = LibraryWorkflow(
            library=library,
            provider_lookup=locator.discover,
            engine_health=engine.hello if engine is not None else None,
            logger=logger.getChild("library"),
        )
        monitor = LeagueProcessMonitor(
            WindowsProcessLookup(),
            LEAGUE_PROCESS_NAME,
            runtime.process_poll_seconds,
            logger.getChild("process_monitor"),
        )
        startup = StartupRegistration()
        executable = running_executable()
        operation_lock = Lock()
        manager_activity_lock = Lock()
        shutdown_lock = Lock()
        shutdown_started = Event()

        def ensure_not_shutting_down() -> None:
            if shutdown_started.is_set():
                raise RuntimeError("The application is shutting down")

        def launch_manager() -> bool:
            with manager_activity_lock:
                ensure_not_shutting_down()
                if runtime_service is not None and runtime_service.status().active:
                    raise RuntimeError("Stop the active mod runtime before opening LTK Manager")
                installation = locator.require()
                if launcher.is_running_under(installation.root):
                    return True
                return launcher.launch(installation.manager_executable)

        def startup_enabled() -> bool:
            return startup.is_enabled(executable)

        def set_startup_enabled(enabled: bool) -> bool:
            startup.set_enabled(executable, enabled)
            return True

        def active_controller() -> AppController:
            if controller is None:
                raise RuntimeError("Application controller is not initialized")
            return controller

        def active_runtime() -> RuntimeCoordinator:
            if runtime_service is None:
                raise RuntimeError("LTK runtime is not initialized")
            return runtime_service

        def import_packages(paths_to_import: tuple[Path, ...]) -> bool:
            with operation_lock:
                ensure_not_shutting_down()
                if active_runtime().status().active:
                    raise RuntimeError("Stop enabled mods before importing packages")
                active = active_controller()
                return active.request_sync(
                    prepare=lambda: workflow.queue_import(paths_to_import),
                    rollback=workflow.cancel_pending_import,
                )

        def refresh_library() -> bool:
            with operation_lock:
                ensure_not_shutting_down()
                if active_runtime().status().active:
                    raise RuntimeError("Stop enabled mods before refreshing the library")
                return active_controller().request_sync()

        def shutdown_services() -> bool:
            with shutdown_lock:
                with operation_lock:
                    shutdown_started.set()
                # Let a launch already committed by the controller finish, or
                # make a queued launch observe the shutdown marker.
                with manager_activity_lock:
                    pass
                controller_stopped = active_controller().shutdown(runtime.shutdown_timeout_seconds)
                runtime_stopped = active_runtime().close(runtime.shutdown_timeout_seconds)
            return runtime_stopped and controller_stopped

        def exit_from_tray() -> bool:
            result = shutdown_services()
            if result and desktop is not None:
                desktop.stop()
            return result

        def exit_from_desktop() -> bool:
            result = shutdown_services()
            if result and tray is not None:
                tray.stop()
            return result

        def enabled_mod_ids() -> tuple[str, ...]:
            return tuple(record.id for record in profile.enabled_records())

        def toggle_mod(content_id: str) -> bool:
            with operation_lock:
                ensure_not_shutting_down()
                if active_runtime().status().active:
                    raise RuntimeError("Stop enabled mods before changing the profile")
                if active_controller().sync_in_progress:
                    raise RuntimeError("Wait for the library operation to finish")
                return profile.toggle(content_id)

        def set_game_dir(selected: Path) -> Path:
            with operation_lock:
                ensure_not_shutting_down()
                if active_runtime().status().active:
                    raise RuntimeError("Stop enabled mods before changing the game directory")
                if active_controller().sync_in_progress:
                    raise RuntimeError("Wait for the library operation to finish")
                return profile.set_game_dir(selected)

        def start_patcher() -> bool:
            with operation_lock, manager_activity_lock:
                ensure_not_shutting_down()
                if active_controller().sync_in_progress:
                    raise RuntimeError("Wait for the library operation to finish")
                active_runtime().start()
            return True

        def stop_patcher() -> bool:
            with operation_lock:
                return active_runtime().stop(runtime.shutdown_timeout_seconds)

        desktop = DesktopApplication(
            catalog_path=paths.library_manifest_file,
            package_dir=paths.package_dir,
            data_dir=paths.data_dir,
            log_file=paths.log_dir / f"{APP_NAME}.log",
            on_refresh=refresh_library,
            on_import=import_packages,
            on_start_manager=lambda: active_controller().start_manager(),
            enabled_ids=enabled_mod_ids,
            on_toggle_mod=toggle_mod,
            on_start_patcher=start_patcher,
            on_stop_patcher=stop_patcher,
            game_dir=profile.game_dir,
            set_game_dir=set_game_dir,
            on_exit=exit_from_desktop,
            startup_enabled=startup_enabled,
            set_startup_enabled=set_startup_enabled,
            path_opener=open_path,
            package_revealer=reveal_path,
            logger=logger.getChild("desktop"),
        )
        tray = TrayApplication(
            on_start=lambda: active_controller().start(),
            on_show=desktop.show,
            on_sync=refresh_library,
            on_start_patcher=start_patcher,
            on_stop_patcher=stop_patcher,
            on_start_manager=lambda: active_controller().start_manager(),
            startup_enabled=startup_enabled,
            set_startup_enabled=set_startup_enabled,
            on_exit=exit_from_tray,
            logger=logger.getChild("tray"),
        )

        last_notified_runtime_error: str | None = None

        def update_runtime_status(status: RuntimeStatus) -> None:
            nonlocal last_notified_runtime_error
            is_running = status.active
            if tray is not None:
                tray.update_runtime_status(status.message, is_running)
            if desktop is not None:
                desktop.update_runtime_status(status.message, is_running)
            if status.phase is RuntimePhase.FAILED:
                failure = status.last_error or status.message
                if tray is not None and failure != last_notified_runtime_error:
                    tray.notify("LTK runtime failed", failure)
                    last_notified_runtime_error = failure
            else:
                last_notified_runtime_error = None

        runtime_service = RuntimeCoordinator(
            profile,
            library,
            engine,
            locator,
            overlay_dir=paths.overlay_dir,
            state_dir=paths.engine_state_dir,
            installation_in_use=launcher.is_running_under,
            status_sink=update_runtime_status,
            shutdown_timeout_seconds=runtime.shutdown_timeout_seconds,
            logger=logger.getChild("runtime"),
        )

        def update_status(state: AppState, detail: str) -> None:
            if tray is not None:
                tray.update_status(state, detail)
            if desktop is not None:
                desktop.update_status(state, detail)

        controller = AppController(
            sync=workflow,
            launcher=launch_manager,
            monitor=monitor,
            status_sink=update_status,
            notify_sink=tray.notify,
            sync_on_start=sync_on_start,
            shutdown_timeout_seconds=runtime.shutdown_timeout_seconds,
            logger=logger.getChild("controller"),
        )

        logger.info("Application starting from %s", Path(sys.executable))
        if activation_event is not None:
            activation_thread = Thread(
                target=_listen_for_activation,
                args=(
                    activation_event,
                    activation_stop,
                    desktop.show,
                    logger.getChild("activation"),
                ),
                name="instance-activation-listener",
                daemon=False,
            )
            activation_thread.start()
        tray.run_detached()
        desktop.run(show_on_start=show_window)
        return 0
    except KeyboardInterrupt:
        logger.info("Application interrupted")
        return 0
    except Exception as exc:
        logger.exception("Application startup, desktop, or system tray failed")
        _show_startup_error(exc, diagnostic_log)
        return 1
    finally:
        activation_stop.set()
        if activation_thread is not None and activation_thread.is_alive():
            activation_thread.join(1.0)
            if activation_thread.is_alive():
                logger.warning("Application activation listener did not stop promptly")
        if activation_event is not None:
            activation_event.close()
        if controller is not None:
            _wait_for_workers(controller, runtime.shutdown_timeout_seconds, logger)
        if runtime_service is not None:
            _wait_for_runtime(runtime_service, runtime.shutdown_timeout_seconds, logger)
        if tray is not None:
            tray.stop()
        if desktop is not None:
            desktop.stop()
        mutex.release()


def main() -> None:
    parser = ArgumentParser(prog=APP_NAME)
    parser.add_argument(
        "--no-refresh",
        "--no-sync",
        dest="no_refresh",
        action="store_true",
        help="Start from the local library without running startup reconciliation.",
    )
    parser.add_argument(
        "--background",
        action="store_true",
        help="Start with the desktop window hidden and remain available in the tray.",
    )
    arguments = parser.parse_args()
    raise SystemExit(
        run(sync_on_start=not arguments.no_refresh, show_window=not arguments.background)
    )
