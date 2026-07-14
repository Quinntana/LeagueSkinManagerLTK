from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import FrozenInstanceError
from threading import Event, Lock, Thread, current_thread

import pytest

from league_skin_manager.controller import AppController, AppState, SyncOutcome


def wait_until(predicate: Callable[[], bool], timeout: float = 1.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.005)
    raise AssertionError("condition was not reached before timeout")


def assert_state(controller: AppController, expected: AppState) -> None:
    assert controller.state is expected


class FakeMonitor:
    def __init__(self) -> None:
        self.started = Event()
        self.stopped = Event()
        self.daemon: bool | None = None
        self._changed: Callable[[int | None], None] | None = None

    def run(
        self,
        stop_event: Event,
        changed: Callable[[int | None], None],
    ) -> None:
        self._changed = changed
        self.daemon = current_thread().daemon
        self.started.set()
        stop_event.wait()
        self.stopped.set()

    def emit(self, pid: int | None) -> None:
        if self._changed is None:
            raise AssertionError("monitor has not started")
        self._changed(pid)


def test_start_is_idempotent_and_exposes_offline_ready_before_work() -> None:
    monitor = FakeMonitor()
    statuses: list[tuple[AppState, str]] = []
    controller = AppController(
        sync=lambda _stop: None,
        launcher=lambda: True,
        monitor=monitor,
        status_sink=lambda state, detail: statuses.append((state, detail)),
        sync_on_start=False,
    )

    assert_state(controller, AppState.STARTING)
    assert controller.start() is True
    assert monitor.started.wait(1)
    assert controller.start() is False
    assert_state(controller, AppState.OFFLINE_READY)
    assert monitor.daemon is False
    assert statuses == [(AppState.OFFLINE_READY, "Ready offline")]

    assert controller.shutdown() is True
    assert monitor.stopped.wait(1)
    assert statuses[-1] == (AppState.STOPPING, "Stopping")


def test_repeated_sync_clicks_never_create_parallel_workers() -> None:
    monitor = FakeMonitor()
    entered = Event()
    release = Event()
    calls = 0
    active = 0
    maximum_active = 0
    sync_daemon: bool | None = None
    counts_lock = Lock()
    statuses: list[AppState] = []
    notifications: list[tuple[str, str]] = []

    def sync(stop_event: Event) -> None:
        nonlocal calls, active, maximum_active, sync_daemon
        sync_daemon = current_thread().daemon
        with counts_lock:
            calls += 1
            active += 1
            maximum_active = max(maximum_active, active)
        entered.set()
        while not release.is_set() and not stop_event.is_set():
            stop_event.wait(0.01)
        with counts_lock:
            active -= 1

    controller = AppController(
        sync=sync,
        launcher=lambda: True,
        monitor=monitor,
        status_sink=lambda state, _detail: statuses.append(state),
        notify_sink=lambda title, message: notifications.append((title, message)),
        sync_on_start=False,
    )
    controller.start()

    assert controller.request_sync() is True
    assert entered.wait(1)
    assert controller.request_sync() is False
    assert calls == 1
    assert maximum_active == 1
    assert sync_daemon is False
    assert notifications[-1] == (
        "Library operation not started",
        "A library operation is already in progress.",
    )

    release.set()
    wait_until(lambda: controller.state is AppState.READY)
    assert statuses == [
        AppState.OFFLINE_READY,
        AppState.SYNCING,
        AppState.READY,
    ]
    assert controller.shutdown() is True


def test_prepared_library_operation_is_atomic_against_refresh() -> None:
    monitor = FakeMonitor()
    prepare_entered = Event()
    release_prepare = Event()
    sync_entered = Event()
    release_sync = Event()
    prepared: list[str] = []
    results: dict[str, bool] = {}

    def prepare_import() -> bool:
        prepare_entered.set()
        assert release_prepare.wait(1)
        prepared.append("import")
        return True

    def sync(stop_event: Event) -> None:
        assert prepared == ["import"]
        sync_entered.set()
        while not release_sync.is_set() and not stop_event.is_set():
            stop_event.wait(0.01)

    controller = AppController(
        sync=sync,
        launcher=lambda: True,
        monitor=monitor,
        sync_on_start=False,
    )
    controller.start()

    importer = Thread(
        target=lambda: results.setdefault("import", controller.request_sync(prepare=prepare_import))
    )
    refresh = Thread(target=lambda: results.setdefault("refresh", controller.request_sync()))
    importer.start()
    assert prepare_entered.wait(1)
    refresh.start()
    release_prepare.set()
    importer.join(1)
    refresh.join(1)

    assert results == {"import": True, "refresh": False}
    assert sync_entered.wait(1)
    release_sync.set()
    wait_until(lambda: not controller.sync_in_progress)
    assert controller.shutdown() is True


def test_sync_failure_is_retryable_and_reported() -> None:
    monitor = FakeMonitor()
    attempts = 0
    notifications: list[tuple[str, str]] = []

    def sync(_stop_event: Event) -> None:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise RuntimeError("network unavailable")

    controller = AppController(
        sync=sync,
        launcher=lambda: True,
        monitor=monitor,
        notify_sink=lambda title, message: notifications.append((title, message)),
        sync_on_start=True,
    )
    controller.start()
    wait_until(lambda: controller.state is AppState.ERROR)

    assert "network unavailable" in controller.status_detail
    assert ("Library operation failed", "network unavailable") in notifications
    assert controller.request_sync() is True
    wait_until(lambda: controller.state is AppState.READY)
    assert attempts == 2
    assert controller.shutdown() is True


def test_sync_outcome_can_report_expected_offline_readiness() -> None:
    monitor = FakeMonitor()
    statuses: list[tuple[AppState, str]] = []
    outcome = SyncOutcome(AppState.OFFLINE_READY, "Using cached skin catalog")
    controller = AppController(
        sync=lambda _stop: outcome,
        launcher=lambda: True,
        monitor=monitor,
        status_sink=lambda state, detail: statuses.append((state, detail)),
        sync_on_start=True,
    )

    controller.start()
    wait_until(lambda: controller.status_detail == "Using cached skin catalog")

    assert controller.state is AppState.OFFLINE_READY
    assert statuses[-1] == (
        AppState.OFFLINE_READY,
        "Using cached skin catalog",
    )
    with pytest.raises(FrozenInstanceError):
        outcome.detail = "changed"  # type: ignore[misc]
    with pytest.raises(ValueError, match="READY or OFFLINE_READY"):
        SyncOutcome(AppState.ERROR, "bad")
    with pytest.raises(ValueError, match="must not be empty"):
        SyncOutcome(AppState.READY, "  ")
    assert controller.shutdown() is True


def test_process_callback_launches_once_per_pid_and_resets_when_gone() -> None:
    monitor = FakeMonitor()
    launched: list[int] = []
    current_pid = 0

    def launcher() -> bool:
        launched.append(current_pid)
        return True

    controller = AppController(
        sync=lambda _stop: None,
        launcher=launcher,
        monitor=monitor,
        sync_on_start=False,
        auto_launch_on_league=True,
    )
    controller.start()
    assert monitor.started.wait(1)

    current_pid = 101
    monitor.emit(101)
    monitor.emit(101)
    current_pid = 202
    monitor.emit(202)
    monitor.emit(202)
    assert launched == [101, 202]
    assert controller.launched_for_league_pid == 202

    monitor.emit(None)
    assert controller.launched_for_league_pid is None
    monitor.emit(202)
    assert launched == [101, 202, 202]
    assert controller.shutdown() is True


def test_launcher_failure_is_contained_and_not_retried_for_same_pid() -> None:
    monitor = FakeMonitor()
    attempts = 0
    notifications: list[tuple[str, str]] = []

    def launcher() -> bool:
        nonlocal attempts
        attempts += 1
        raise OSError("manager missing")

    controller = AppController(
        sync=lambda _stop: None,
        launcher=launcher,
        monitor=monitor,
        notify_sink=lambda title, message: notifications.append((title, message)),
        sync_on_start=False,
        auto_launch_on_league=True,
    )
    controller.start()
    assert monitor.started.wait(1)

    monitor.emit(77)
    monitor.emit(77)
    assert attempts == 1
    assert notifications == [("LTK Manager", "Could not start manager: manager missing")]
    assert controller.shutdown() is True


def test_league_launch_waits_for_sync_and_keeps_latest_live_pid() -> None:
    monitor = FakeMonitor()
    sync_started = Event()
    release = Event()
    launched: list[int] = []
    pid_at_launch = 0

    def sync(stop_event: Event) -> None:
        sync_started.set()
        while not release.is_set() and not stop_event.is_set():
            stop_event.wait(0.01)

    def launcher() -> bool:
        launched.append(pid_at_launch)
        return True

    controller = AppController(
        sync=sync,
        launcher=launcher,
        monitor=monitor,
        sync_on_start=True,
        auto_launch_on_league=True,
    )
    controller.start()
    assert monitor.started.wait(1)
    assert sync_started.wait(1)

    monitor.emit(301)
    pid_at_launch = 302
    monitor.emit(302)
    assert launched == []
    assert controller.launched_for_league_pid is None

    release.set()
    wait_until(lambda: controller.state is AppState.READY)
    wait_until(lambda: launched == [302])
    assert controller.launched_for_league_pid == 302
    monitor.emit(302)
    assert launched == [302]
    assert controller.shutdown() is True


def test_pending_league_launch_is_discarded_when_client_exits_during_sync() -> None:
    monitor = FakeMonitor()
    sync_started = Event()
    release = Event()
    launches = 0

    def sync(stop_event: Event) -> None:
        sync_started.set()
        while not release.is_set() and not stop_event.is_set():
            stop_event.wait(0.01)

    def launcher() -> bool:
        nonlocal launches
        launches += 1
        return True

    controller = AppController(
        sync=sync,
        launcher=launcher,
        monitor=monitor,
        sync_on_start=True,
        auto_launch_on_league=True,
    )
    controller.start()
    assert sync_started.wait(1)
    monitor.emit(404)
    monitor.emit(None)
    release.set()
    wait_until(lambda: controller.state is AppState.READY)

    assert launches == 0
    assert controller.launched_for_league_pid is None
    assert controller.shutdown() is True


def test_manual_manager_launch_is_queued_until_sync_finishes() -> None:
    monitor = FakeMonitor()
    sync_started = Event()
    release = Event()
    launches = 0
    notifications: list[tuple[str, str]] = []

    def sync(stop_event: Event) -> None:
        sync_started.set()
        while not release.is_set() and not stop_event.is_set():
            stop_event.wait(0.01)

    def launcher() -> bool:
        nonlocal launches
        launches += 1
        return True

    controller = AppController(
        sync=sync,
        launcher=launcher,
        monitor=monitor,
        notify_sink=lambda title, message: notifications.append((title, message)),
        sync_on_start=True,
    )
    controller.start()
    assert sync_started.wait(1)

    assert controller.start_manager() is True
    assert launches == 0
    assert notifications[-1] == (
        "LTK Manager",
        "Manager launch queued until the library operation finishes.",
    )

    release.set()
    wait_until(lambda: controller.state is AppState.READY)
    wait_until(lambda: launches == 1)
    assert controller.shutdown() is True


def test_monitor_failure_remains_visible_after_concurrent_sync_succeeds() -> None:
    sync_started = Event()
    release_sync = Event()
    fail_monitor = Event()

    class FailingMonitor:
        def run(
            self,
            stop_event: Event,
            _changed: Callable[[int | None], None],
        ) -> None:
            while not fail_monitor.is_set() and not stop_event.is_set():
                stop_event.wait(0.01)
            if not stop_event.is_set():
                raise RuntimeError("monitor unavailable")

    def sync(stop_event: Event) -> None:
        sync_started.set()
        while not release_sync.is_set() and not stop_event.is_set():
            stop_event.wait(0.01)

    controller = AppController(
        sync=sync,
        launcher=lambda: True,
        monitor=FailingMonitor(),
        sync_on_start=True,
    )
    controller.start()
    assert sync_started.wait(1)
    fail_monitor.set()
    wait_until(lambda: controller.state is AppState.ERROR)

    release_sync.set()
    wait_until(lambda: not controller.sync_in_progress)
    assert controller.state is AppState.ERROR
    assert controller.status_detail == "Process monitor failed: monitor unavailable"
    assert controller.shutdown() is True


def test_shutdown_cooperatively_stops_and_joins_both_workers() -> None:
    monitor = FakeMonitor()
    sync_started = Event()
    sync_stopped = Event()

    def sync(stop_event: Event) -> None:
        sync_started.set()
        stop_event.wait()
        sync_stopped.set()

    controller = AppController(
        sync=sync,
        launcher=lambda: True,
        monitor=monitor,
        sync_on_start=True,
    )
    controller.start()
    assert monitor.started.wait(1)
    assert sync_started.wait(1)

    assert controller.shutdown(timeout_seconds=0.5) is True
    assert sync_stopped.wait(1)
    assert monitor.stopped.wait(1)
    assert controller.state is AppState.STOPPING
    assert controller.sync_in_progress is False


def test_shutdown_has_one_bounded_deadline_for_uncooperative_work() -> None:
    monitor = FakeMonitor()
    sync_started = Event()
    release = Event()
    notifications: list[tuple[str, str]] = []

    def sync(_stop_event: Event) -> None:
        sync_started.set()
        release.wait()

    controller = AppController(
        sync=sync,
        launcher=lambda: True,
        monitor=monitor,
        notify_sink=lambda title, message: notifications.append((title, message)),
        sync_on_start=True,
    )
    controller.start()
    assert sync_started.wait(1)

    started = time.monotonic()
    assert controller.shutdown(timeout_seconds=0.03) is False
    elapsed = time.monotonic() - started
    assert elapsed < 0.2
    assert "library-worker" in notifications[-1][1]

    release.set()
    wait_until(lambda: not controller.sync_in_progress)
