"""Thread-safe application lifecycle coordination.

The controller deliberately knows nothing about pystray, the registry, downloads, or
process creation.  Those boundaries are injected so lifecycle behavior remains
deterministic and testable.
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from enum import Enum, auto
from threading import Event, Lock, RLock, Thread
from typing import Protocol


class AppState(Enum):
    """Observable lifecycle states for the tray and diagnostics."""

    STARTING = auto()
    OFFLINE_READY = auto()
    SYNCING = auto()
    READY = auto()
    ERROR = auto()
    STOPPING = auto()


@dataclass(frozen=True, slots=True)
class SyncOutcome:
    """Successful sync completion, including an expected offline result."""

    state: AppState
    detail: str

    def __post_init__(self) -> None:
        if self.state not in (AppState.READY, AppState.OFFLINE_READY):
            raise ValueError("sync outcome must be READY or OFFLINE_READY")
        if not self.detail.strip():
            raise ValueError("sync outcome detail must not be empty")


class ProcessMonitor(Protocol):
    """Cooperative League process monitor boundary."""

    def run(
        self,
        stop_event: Event,
        changed: Callable[[int | None], None],
    ) -> None: ...


SyncCallable = Callable[[Event], SyncOutcome | None]
SyncPreparation = Callable[[], bool]
SyncRollback = Callable[[], None]
ManagerLauncher = Callable[[], bool | None]
StatusSink = Callable[[AppState, str], None]
NotifySink = Callable[[str, str], None]


_DEFAULT_STATUS: dict[AppState, str] = {
    AppState.STARTING: "Starting",
    AppState.OFFLINE_READY: "Ready offline",
    AppState.SYNCING: "Refreshing library",
    AppState.READY: "Ready",
    AppState.ERROR: "An error occurred",
    AppState.STOPPING: "Stopping",
}


class AppController:
    """Own the app state machine and its two cooperative background activities."""

    def __init__(
        self,
        *,
        sync: SyncCallable,
        launcher: ManagerLauncher,
        monitor: ProcessMonitor,
        status_sink: StatusSink | None = None,
        notify_sink: NotifySink | None = None,
        sync_on_start: bool = True,
        auto_launch_on_league: bool = False,
        shutdown_timeout_seconds: float = 10.0,
        logger: logging.Logger | None = None,
    ) -> None:
        if shutdown_timeout_seconds <= 0:
            raise ValueError("shutdown_timeout_seconds must be positive")

        self._sync = sync
        self._launcher = launcher
        self._monitor = monitor
        self._status_sink = status_sink
        self._notify_sink = notify_sink
        self._sync_on_start = sync_on_start
        self._auto_launch_on_league = auto_launch_on_league
        self._shutdown_timeout_seconds = shutdown_timeout_seconds
        self._logger = logger or logging.getLogger(__name__)

        self._lock = RLock()
        self._manager_launch_lock = Lock()
        self._stop_event = Event()
        self._state = AppState.STARTING
        self._status_detail = _DEFAULT_STATUS[self._state]
        self._started = False
        self._sync_running = False
        self._startup_sync_pending = False
        self._sync_thread: Thread | None = None
        self._sync_threads: set[Thread] = set()
        self._monitor_thread: Thread | None = None
        self._current_league_pid: int | None = None
        self._pending_league_pid: int | None = None
        self._launched_for_league_pid: int | None = None
        self._manual_manager_launch_pending = False
        self._monitor_failure_detail: str | None = None

    @property
    def state(self) -> AppState:
        with self._lock:
            return self._state

    @property
    def status_detail(self) -> str:
        with self._lock:
            return self._status_detail

    @property
    def sync_in_progress(self) -> bool:
        with self._lock:
            return self._sync_running

    @property
    def launched_for_league_pid(self) -> int | None:
        with self._lock:
            return self._launched_for_league_pid

    def start(self) -> bool:
        """Start monitoring and optionally queue the initial sync.

        The tray should invoke this from its setup callback, which pystray runs
        after the icon has become visible.  All potentially slow work is then
        performed on background threads.
        """

        with self._lock:
            if self._started or self._stop_event.is_set():
                return False
            self._started = True
            self._startup_sync_pending = self._sync_on_start
            self._state = AppState.OFFLINE_READY
            self._status_detail = _DEFAULT_STATUS[self._state]
            monitor_thread = Thread(
                target=self._run_monitor,
                name="league-process-monitor",
                # Never abandon monitor-owned cleanup during interpreter exit.
                # ``shutdown`` supplies the bounded cooperative join.
                daemon=False,
            )
            self._monitor_thread = monitor_thread

            state = self._state
            detail = self._status_detail

        # Publish before starting work so a very fast worker cannot report a
        # later state and then be overwritten by this initial transition.
        self._publish_status(state, detail)

        with self._lock:
            if self._stop_event.is_set():
                return False
            try:
                monitor_thread.start()
            except Exception as exc:
                self._startup_sync_pending = False
                self._state = AppState.ERROR
                self._status_detail = f"Process monitor failed: {exc}"
                self._logger.exception("Unable to start League process monitor")
                state = self._state
                detail = self._status_detail

        if state is AppState.ERROR:
            self._publish_status(state, detail)
            self._notify("League Skin Manager LTK", detail)
            return False
        if self._sync_on_start:
            self.request_sync()
        return True

    def request_sync(
        self,
        *,
        prepare: SyncPreparation | None = None,
        rollback: SyncRollback | None = None,
    ) -> bool:
        """Atomically prepare and start the sole library worker.

        ``prepare`` runs only after duplicate/stopping checks and before the
        worker can start. This closes the import queue vs. Refresh race without
        coupling the controller to a particular operation type.
        """

        rejection: str | None = None
        sync_thread: Thread | None = None
        prepared = False
        state = self.state
        detail = self.status_detail
        # Serialize the decision with manager launch. Whichever operation wins
        # this lock completes its transition before the other observes state.
        with self._manager_launch_lock, self._lock:
            if not self._started:
                rejection = "The application is still starting."
            elif self._stop_event.is_set() or self._state is AppState.STOPPING:
                rejection = "The application is stopping."
            elif self._sync_running:
                rejection = "A library operation is already in progress."
            else:
                try:
                    prepared = prepare is None or prepare()
                except Exception as exc:
                    self._logger.exception("Could not prepare library operation")
                    rejection = f"Could not queue the library operation: {exc}"
                if rejection is None and not prepared:
                    rejection = "The library operation could not be queued."
                if rejection is None:
                    self._sync_running = True
                    self._startup_sync_pending = False
                    self._state = AppState.SYNCING
                    self._status_detail = _DEFAULT_STATUS[self._state]
                    sync_thread = Thread(
                        target=self._run_sync,
                        name="library-worker",
                        # A daemon could be killed halfway through an atomic install.
                        daemon=False,
                    )
                    self._sync_thread = sync_thread
                    self._sync_threads.add(sync_thread)

                    state = self._state
                    detail = self._status_detail

        if rejection is not None:
            self._notify("Library operation not started", rejection)
            return False

        if sync_thread is None:
            raise RuntimeError("sync worker was not created")
        self._publish_status(state, detail)

        with self._lock:
            if self._stop_event.is_set() or self._state is AppState.STOPPING:
                self._sync_running = False
                self._sync_threads.discard(sync_thread)
                if prepared and rollback is not None:
                    rollback()
                return False
            try:
                sync_thread.start()
            except Exception as exc:
                self._sync_running = False
                self._sync_threads.discard(sync_thread)
                if prepared and rollback is not None:
                    rollback()
                self._state = AppState.ERROR
                self._status_detail = f"Unable to start sync: {exc}"
                self._logger.exception("Unable to start sync worker")
                state = self._state
                detail = self._status_detail

        if state is AppState.ERROR:
            self._publish_status(state, detail)
            self._notify("Library refresh failed", detail)
            return False
        return True

    def start_manager(self) -> bool:
        """Launch LTK Manager, or safely queue it behind an active library operation."""

        with self._lock:
            if self._stop_event.is_set():
                return False
            if self._sync_running or self._startup_sync_pending:
                self._manual_manager_launch_pending = True
                queued = True
            else:
                queued = False
        if queued:
            self._notify(
                "LTK Manager",
                "Manager launch queued until the library operation finishes.",
            )
            return True
        return self._launch_manager_now()

    def _launch_manager_now(self) -> bool:
        """Serialize launch checks and close the final sync/launch race."""

        with self._manager_launch_lock:
            with self._lock:
                if self._stop_event.is_set():
                    return False
                if self._sync_running or self._startup_sync_pending:
                    self._manual_manager_launch_pending = True
                    return True
            try:
                result = self._launcher()
            except Exception as exc:
                self._logger.exception("LTK Manager launch failed")
                self._notify("LTK Manager", f"Could not start manager: {exc}")
                return False
            if result is False:
                self._notify("LTK Manager", "Could not start manager.")
                return False
            return True

    def shutdown(self, timeout_seconds: float | None = None) -> bool:
        """Request cooperative stop and join workers within one total deadline."""

        timeout = self._shutdown_timeout_seconds if timeout_seconds is None else timeout_seconds
        if timeout <= 0:
            raise ValueError("timeout_seconds must be positive")

        should_publish = False
        with self._lock:
            if self._state is not AppState.STOPPING:
                self._state = AppState.STOPPING
                self._status_detail = _DEFAULT_STATUS[self._state]
                should_publish = True
            self._stop_event.set()
            workers = tuple(self._sync_threads)
            if self._monitor_thread is not None:
                workers += (self._monitor_thread,)
            state = self._state
            detail = self._status_detail

        if should_publish:
            self._publish_status(state, detail)

        deadline = time.monotonic() + timeout
        current = threading.current_thread()
        for worker in workers:
            if worker is current or not worker.is_alive():
                continue
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            worker.join(remaining)

        alive = [worker.name for worker in workers if worker is not current and worker.is_alive()]
        if alive:
            names = ", ".join(alive)
            self._logger.warning("Shutdown timed out waiting for: %s", names)
            self._notify(
                "League Skin Manager LTK",
                f"Shutdown timed out waiting for: {names}",
            )
            return False
        return True

    def _run_sync(self) -> None:
        try:
            try:
                outcome = self._sync(self._stop_event)
                if outcome is None:
                    outcome = SyncOutcome(
                        AppState.READY,
                        _DEFAULT_STATUS[AppState.READY],
                    )
                elif not isinstance(outcome, SyncOutcome):
                    raise TypeError("sync must return SyncOutcome or None")
            except Exception as exc:
                self._logger.exception("Library operation failed")
                self._complete_sync(AppState.ERROR, f"Library operation failed: {exc}")
                if not self._stop_event.is_set():
                    self._notify("Library operation failed", str(exc))
            else:
                self._complete_sync(outcome.state, outcome.detail)
        finally:
            with self._lock:
                self._sync_threads.discard(threading.current_thread())

    def _complete_sync(self, state: AppState, detail: str) -> None:
        launch_pid: int | None = None
        launch_manual = False
        with self._lock:
            self._sync_running = False
            if self._stop_event.is_set() or self._state is AppState.STOPPING:
                return
            sync_succeeded = state in (AppState.READY, AppState.OFFLINE_READY)
            if self._monitor_failure_detail is not None:
                publish_state = AppState.ERROR
                publish_detail = self._monitor_failure_detail
            else:
                publish_state = state
                publish_detail = detail
            self._state = publish_state
            self._status_detail = publish_detail
            if sync_succeeded:
                pending = self._pending_league_pid
                self._pending_league_pid = None
                if (
                    pending is not None
                    and pending == self._current_league_pid
                    and pending != self._launched_for_league_pid
                ):
                    self._launched_for_league_pid = pending
                    launch_pid = pending
            launch_manual = self._manual_manager_launch_pending
            self._manual_manager_launch_pending = False
        self._publish_status(publish_state, publish_detail)
        if launch_pid is not None:
            self._launch_reserved_pid(launch_pid, deferred=True)
        elif launch_manual:
            self._launch_manager_now()

    def _run_monitor(self) -> None:
        try:
            self._monitor.run(self._stop_event, self._league_pid_changed)
        except Exception as exc:
            self._logger.exception("League process monitor stopped unexpectedly")
            self._report_monitor_failure(str(exc))
        else:
            if not self._stop_event.is_set():
                self._logger.error("League process monitor returned unexpectedly")
                self._report_monitor_failure("Monitor stopped unexpectedly")

    def _report_monitor_failure(self, message: str) -> None:
        with self._lock:
            if self._stop_event.is_set():
                return
            self._state = AppState.ERROR
            self._status_detail = f"Process monitor failed: {message}"
            self._monitor_failure_detail = self._status_detail
            state = self._state
            detail = self._status_detail
        self._publish_status(state, detail)
        self._notify("League process monitor", message)

    def _launch_reserved_pid(self, pid: int, *, deferred: bool) -> None:
        with self._lock:
            if (
                self._stop_event.is_set()
                or self._current_league_pid != pid
                or self._launched_for_league_pid != pid
            ):
                return
        if not self._launch_manager_now():
            launch_kind = "Deferred" if deferred else "Automatic"
            self._logger.warning(
                "%s LTK Manager launch failed for League PID %s",
                launch_kind,
                pid,
            )

    def _league_pid_changed(self, pid: int | None) -> None:
        with self._lock:
            if self._stop_event.is_set():
                return
            self._current_league_pid = pid
            if not self._auto_launch_on_league:
                self._launched_for_league_pid = None
                self._pending_league_pid = None
                return
            if pid is None:
                self._launched_for_league_pid = None
                self._pending_league_pid = None
                return
            if pid == self._launched_for_league_pid:
                self._pending_league_pid = None
                return
            if self._sync_running or self._startup_sync_pending:
                # Do not launch a manager that may read files while sync is
                # replacing them. Keep only the latest still-running League PID.
                self._pending_league_pid = pid
                return
            # Reserve the PID before launching so concurrent duplicate callbacks
            # cannot start multiple manager processes.
            self._launched_for_league_pid = pid
            self._pending_league_pid = None

        self._launch_reserved_pid(pid, deferred=False)

    def _publish_status(self, state: AppState, detail: str) -> None:
        if self._status_sink is None:
            return
        try:
            self._status_sink(state, detail)
        except Exception:
            self._logger.exception("Status sink failed")

    def _notify(self, title: str, message: str) -> None:
        if self._notify_sink is None:
            return
        try:
            self._notify_sink(title, message)
        except Exception:
            self._logger.exception("Notification sink failed")


__all__ = [
    "AppController",
    "AppState",
    "ManagerLauncher",
    "NotifySink",
    "ProcessMonitor",
    "StatusSink",
    "SyncCallable",
    "SyncOutcome",
    "SyncPreparation",
    "SyncRollback",
]
