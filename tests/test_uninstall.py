from __future__ import annotations

from pathlib import Path
from typing import Any

import psutil
import pytest

import league_skin_manager.uninstall as uninstall_module
from league_skin_manager.config import APP_NAME
from league_skin_manager.installation import InstallLayout, start_menu_shortcut_path
from league_skin_manager.uninstall import (
    BLOCKING_PROCESSES,
    CleanupWorker,
    RemovalState,
    Uninstaller,
    UninstallStatus,
    cleanup_relocated_copy,
    find_running_process,
    launch_relocated_uninstaller,
    main,
    prepare_install_tree_for_self_delete,
    remove_install_tree,
    remove_owned_install_remnants,
    run_uninstall_entrypoint,
    wait_for_cleanup_worker_status,
    wait_for_process_exit,
    write_owned_install_remnant_marker,
)


def app_paths(tmp_path: Path) -> tuple[Path, Path]:
    appdata = tmp_path / "LocalAppData"
    data_dir = appdata / APP_NAME
    data_dir.mkdir(parents=True)
    return appdata, data_dir


class FakeMutex:
    def __init__(
        self,
        name: str = "mutex",
        events: list[str] | None = None,
        *,
        acquired: bool = True,
    ) -> None:
        self.name = name
        self.events = events if events is not None else []
        self.acquired = acquired
        self.releases = 0

    def acquire(self) -> bool:
        self.events.append(f"{self.name}:acquire")
        return self.acquired

    def release(self) -> None:
        self.releases += 1
        self.events.append(f"{self.name}:release")


def test_running_manager_aborts_before_any_cleanup_and_releases_both_gates(
    tmp_path: Path,
) -> None:
    appdata, data_dir = app_paths(tmp_path)
    cleanup_calls: list[str] = []
    events: list[str] = []
    operation = FakeMutex("operation", events)
    app = FakeMutex("app", events)

    result = Uninstaller(
        appdata_root=appdata,
        data_dir=data_dir,
        process_finder=lambda _names: "ltk-engine.exe",
        startup_remover=lambda: cleanup_calls.append("startup") or RemovalState.REMOVED,
        tree_remover=lambda _path: cleanup_calls.append("data") or RemovalState.REMOVED,
        operation_mutex=operation,
        mutex=app,
    ).run()

    assert result.status is UninstallStatus.ABORTED
    assert result.blocking_process == "ltk-engine.exe"
    assert cleanup_calls == []
    assert events == [
        "operation:acquire",
        "app:acquire",
        "app:release",
        "operation:release",
    ]


def test_success_holds_both_gates_and_removes_registration_after_install_files(
    tmp_path: Path,
) -> None:
    appdata, data_dir = app_paths(tmp_path)
    events: list[str] = []

    result = Uninstaller(
        appdata_root=appdata,
        data_dir=data_dir,
        process_finder=lambda _names: events.append("scan") or None,
        startup_remover=lambda: events.append("startup") or RemovalState.REMOVED,
        shortcut_remover=lambda: events.append("shortcut") or RemovalState.REMOVED,
        tree_remover=lambda _path: events.append("data") or RemovalState.REMOVED,
        install_cleanup=lambda: events.append("install") or RemovalState.REMOVED,
        registration_remover=lambda: events.append("registration") or RemovalState.REMOVED,
        operation_mutex=FakeMutex("operation", events),
        mutex=FakeMutex("app", events),
    ).run()

    assert result.ok
    assert result.install_files is RemovalState.REMOVED
    assert result.registration is RemovalState.REMOVED
    assert events == [
        "operation:acquire",
        "app:acquire",
        "scan",
        "startup",
        "shortcut",
        "data",
        "install",
        "registration",
        "app:release",
        "operation:release",
    ]


def test_install_file_failure_preserves_apps_registration_for_retry(tmp_path: Path) -> None:
    appdata, data_dir = app_paths(tmp_path)
    registration_calls: list[str] = []

    def fail_install() -> RemovalState:
        raise PermissionError("file locked")

    result = Uninstaller(
        appdata_root=appdata,
        data_dir=data_dir,
        process_finder=lambda _names: None,
        startup_remover=lambda: RemovalState.REMOVED,
        tree_remover=lambda _path: RemovalState.REMOVED,
        install_cleanup=fail_install,
        registration_remover=lambda: (
            registration_calls.append("registration") or RemovalState.REMOVED
        ),
    ).run()

    assert result.status is UninstallStatus.PARTIAL
    assert result.install_files is RemovalState.FAILED
    assert result.registration is RemovalState.SKIPPED
    assert registration_calls == []


def test_operation_gate_blocks_before_app_gate_or_cleanup(tmp_path: Path) -> None:
    appdata, data_dir = app_paths(tmp_path)
    events: list[str] = []

    result = Uninstaller(
        appdata_root=appdata,
        data_dir=data_dir,
        process_finder=lambda _names: events.append("scan") or None,
        operation_mutex=FakeMutex("operation", events, acquired=False),
        mutex=FakeMutex("app", events),
    ).run()

    assert result.status is UninstallStatus.ABORTED
    assert events == ["operation:acquire"]


def test_app_gate_failure_releases_operation_gate(tmp_path: Path) -> None:
    appdata, data_dir = app_paths(tmp_path)
    events: list[str] = []

    result = Uninstaller(
        appdata_root=appdata,
        data_dir=data_dir,
        process_finder=lambda _names: None,
        operation_mutex=FakeMutex("operation", events),
        mutex=FakeMutex("app", events, acquired=False),
    ).run()

    assert result.status is UninstallStatus.ABORTED
    assert events == ["operation:acquire", "app:acquire", "operation:release"]


def test_uninstaller_rejects_every_target_except_appdata_app_folder(tmp_path: Path) -> None:
    appdata = tmp_path / "LocalAppData"
    appdata.mkdir(parents=True)

    with pytest.raises(ValueError, match="outside LOCALAPPDATA"):
        Uninstaller(appdata_root=appdata, data_dir=tmp_path / APP_NAME)

    with pytest.raises(ValueError, match="must be named"):
        Uninstaller(appdata_root=appdata, data_dir=appdata / "NotTheApplication")


class FakeProcess:
    def __init__(self, info: dict[str, Any] | BaseException) -> None:
        self._info = info

    @property
    def info(self) -> dict[str, Any]:
        if isinstance(self._info, BaseException):
            raise self._info
        return self._info


class FakeWorkerProcess:
    def __init__(self, poll_result: int | None = None) -> None:
        self.poll_result = poll_result
        self.waits: list[float] = []
        self.terminated = False
        self.killed = False

    def poll(self) -> int | None:
        return self.poll_result

    def wait(self, timeout: float) -> int:
        self.waits.append(timeout)
        self.poll_result = 0
        return 0

    def terminate(self) -> None:
        self.terminated = True

    def kill(self) -> None:
        self.killed = True


def test_process_detection_is_case_insensitive_and_tolerates_access_denied() -> None:
    processes = [
        FakeProcess(psutil.AccessDenied(pid=1)),
        FakeProcess({"pid": 50, "name": f"{APP_NAME.upper()}.EXE"}),
    ]

    found = find_running_process(
        BLOCKING_PROCESSES,
        process_iter=lambda _fields: iter(processes),
        current_pid=99,
    )

    assert found == f"{APP_NAME.upper()}.EXE"


def test_process_detection_blocks_helpers_inside_owned_data_root(tmp_path: Path) -> None:
    data_dir = tmp_path / APP_NAME
    helper = data_dir / "engine" / "helpers" / "ltk-engine.exe"
    processes = [FakeProcess({"pid": 50, "name": "mod-tools.exe", "exe": str(helper)})]

    found = find_running_process(
        (),
        process_iter=lambda _fields: iter(processes),
        current_pid=99,
        blocked_roots=(data_dir,),
    )

    assert found == "mod-tools.exe"


def test_process_detection_does_not_block_an_unrelated_same_named_engine() -> None:
    processes = [FakeProcess({"pid": 50, "name": "LTK-ENGINE.EXE", "exe": None})]

    found = find_running_process(
        BLOCKING_PROCESSES,
        process_iter=lambda _fields: iter(processes),
        current_pid=99,
    )

    assert found is None


def test_remove_install_tree_deletes_only_validated_program_directory(tmp_path: Path) -> None:
    layout = InstallLayout.discover(tmp_path / "LocalAppData")
    layout.install_dir.mkdir(parents=True)
    layout.executable.write_bytes(b"main")
    layout.uninstaller.write_bytes(b"uninstall")
    outside = tmp_path / "keep.txt"
    outside.write_text("keep", encoding="utf-8")

    state = remove_install_tree(layout)

    assert state is RemovalState.REMOVED
    assert not layout.install_dir.exists()
    assert outside.read_text(encoding="utf-8") == "keep"


def test_owned_install_remnants_require_exact_name_marker_and_location(
    tmp_path: Path,
) -> None:
    layout = InstallLayout.discover(tmp_path / "LocalAppData")
    parent = layout.install_dir.parent
    parent.mkdir(parents=True)
    owned_nonce = "1" * 32
    owned = parent / f".{APP_NAME}-install-{owned_nonce}"
    owned.mkdir()
    write_owned_install_remnant_marker(owned, "install", owned_nonce)
    (owned / "payload.bin").write_bytes(b"owned")

    unowned_nonce = "2" * 32
    unowned = parent / f".{APP_NAME}-backup-{unowned_nonce}"
    unowned.mkdir()
    unowned_sentinel = unowned / "keep.txt"
    unowned_sentinel.write_text("not marked", encoding="utf-8")
    lookalike = parent / f".{APP_NAME}-install-not-a-nonce"
    lookalike.mkdir()
    outside = tmp_path / f".{APP_NAME}-install-{'3' * 32}"
    outside.mkdir()

    assert remove_owned_install_remnants(layout) == 1

    assert not owned.exists()
    assert unowned_sentinel.read_text(encoding="utf-8") == "not marked"
    assert lookalike.is_dir()
    assert outside.is_dir()


def test_prepare_self_delete_leaves_only_exact_running_uninstaller_and_sentinels(
    tmp_path: Path,
) -> None:
    layout = InstallLayout.discover(tmp_path / "LocalAppData")
    layout.install_dir.mkdir(parents=True)
    layout.executable.write_bytes(b"main")
    layout.uninstaller.write_bytes(b"uninstall")
    licenses = layout.install_dir / "licenses"
    licenses.mkdir()
    (licenses / "NOTICE.txt").write_text("notice", encoding="utf-8")
    owned_nonce = "4" * 32
    owned = layout.install_dir.parent / f".{APP_NAME}-backup-{owned_nonce}"
    owned.mkdir()
    write_owned_install_remnant_marker(owned, "backup", owned_nonce)
    unowned_nonce = "5" * 32
    unowned = layout.install_dir.parent / f".{APP_NAME}-backup-{unowned_nonce}"
    unowned.mkdir()
    sentinel = unowned / "keep.txt"
    sentinel.write_text("keep", encoding="utf-8")

    state = prepare_install_tree_for_self_delete(layout, layout.uninstaller)

    assert state is RemovalState.REMOVED
    assert tuple(layout.install_dir.iterdir()) == (layout.uninstaller,)
    assert not owned.exists()
    assert sentinel.read_text(encoding="utf-8") == "keep"


def test_relocation_copies_installed_uninstaller_and_passes_bootloader_pid(
    monkeypatch: Any,
    tmp_path: Path,
) -> None:
    local = tmp_path / "LocalAppData"
    layout = InstallLayout.discover(local)
    layout.install_dir.mkdir(parents=True)
    layout.uninstaller.write_bytes(b"uninstaller")
    layout.executable.write_bytes(b"main")
    temp_root = tmp_path / "Temp"
    calls: list[tuple[list[str], dict[str, object]]] = []
    process = FakeWorkerProcess()
    nonce = "a" * 32
    monkeypatch.setattr(uninstall_module.os, "name", "nt")

    worker = launch_relocated_uninstaller(
        layout,
        executable=layout.uninstaller,
        parent_pid=321,
        temp_root=temp_root,
        nonce=nonce,
        popen=lambda args, **kwargs: calls.append((args, kwargs)) or process,
    )

    relocated = worker.temp_dir / layout.uninstaller.name
    assert relocated.read_bytes() == b"uninstaller"
    assert calls[0][0] == [str(relocated)]
    assert worker.process is process
    assert worker.nonce == nonce
    environment = calls[0][1]["env"]
    assert isinstance(environment, dict)
    assert environment["LSMLTK_UNINSTALL_RELOCATED"] == "cleanup"
    assert environment["LSMLTK_UNINSTALL_WAIT_PID"] == "321"
    assert environment["LSMLTK_UNINSTALL_TEMP_DIR"] == str(worker.temp_dir)
    assert environment["LSMLTK_UNINSTALL_NONCE"] == nonce
    assert environment["PYINSTALLER_RESET_ENVIRONMENT"] == "1"


def test_wait_for_original_bootloader_parent() -> None:
    calls: list[float] = []

    class Process:
        def wait(self, timeout: float) -> None:
            calls.append(timeout)

    wait_for_process_exit(321, timeout_seconds=4.5, process_factory=lambda _pid: Process())

    assert calls == [4.5]


def test_frozen_installed_entrypoint_propagates_cancel_without_starting_worker(
    monkeypatch: Any,
    tmp_path: Path,
) -> None:
    layout = InstallLayout.discover(tmp_path / "LocalAppData")
    layout.install_dir.mkdir(parents=True)
    layout.executable.write_bytes(b"main")
    layout.uninstaller.write_bytes(b"uninstall")
    notifications: list[tuple[str, str, bool]] = []
    monkeypatch.delenv("LSMLTK_UNINSTALL_RELOCATED", raising=False)
    monkeypatch.delenv("LSMLTK_UNINSTALL_TEMP_DIR", raising=False)
    monkeypatch.delenv("LSMLTK_UNINSTALL_WAIT_PID", raising=False)
    monkeypatch.setattr(uninstall_module.InstallLayout, "discover", lambda: layout)
    monkeypatch.setattr(uninstall_module.sys, "executable", str(layout.uninstaller))
    monkeypatch.setattr(uninstall_module.sys, "frozen", True, raising=False)
    monkeypatch.setattr(
        uninstall_module,
        "launch_relocated_uninstaller",
        lambda *_args, **_kwargs: pytest.fail("cancel must not launch a worker"),
    )

    def cancelled_main(**kwargs: Any) -> int:
        kwargs["notifier"]("Uninstall cancelled", "Nothing was removed.", False)
        return int(kwargs["cancelled_exit_code"])

    monkeypatch.setattr(uninstall_module, "main", cancelled_main)
    monkeypatch.setattr(
        uninstall_module,
        "show_result",
        lambda title, message, error: notifications.append((title, message, error)),
    )

    result = run_uninstall_entrypoint()

    assert result == 1602
    assert notifications == [("Uninstall cancelled", "Nothing was removed.", False)]


def test_frozen_installed_entrypoint_commits_only_after_synchronous_success(
    monkeypatch: Any,
    tmp_path: Path,
) -> None:
    layout = InstallLayout.discover(tmp_path / "LocalAppData")
    process = FakeWorkerProcess()
    worker = CleanupWorker(tmp_path, "b" * 32, process)
    events: list[str] = []
    monkeypatch.delenv("LSMLTK_UNINSTALL_RELOCATED", raising=False)
    monkeypatch.setattr(uninstall_module.InstallLayout, "discover", lambda: layout)
    monkeypatch.setattr(uninstall_module.sys, "executable", str(layout.uninstaller))
    monkeypatch.setattr(uninstall_module.sys, "frozen", True, raising=False)
    monkeypatch.setattr(
        uninstall_module,
        "launch_relocated_uninstaller",
        lambda *_args, **_kwargs: events.append("launch") or worker,
    )
    monkeypatch.setattr(
        uninstall_module,
        "wait_for_cleanup_worker_status",
        lambda *_args, **_kwargs: events.append("ready"),
    )
    monkeypatch.setattr(
        uninstall_module,
        "commit_cleanup_worker",
        lambda _worker: events.append("commit"),
    )

    def successful_main(**kwargs: Any) -> int:
        events.append("main")
        kwargs["before_cleanup"]()
        events.append("cleanup")
        kwargs["notifier"]("Uninstall complete", "done", False)
        return 0

    monkeypatch.setattr(uninstall_module, "main", successful_main)
    monkeypatch.setattr(
        uninstall_module,
        "show_result",
        lambda *_args: events.append("notify"),
    )

    result = run_uninstall_entrypoint()

    assert result == 0
    assert events == ["main", "launch", "ready", "cleanup", "commit", "notify"]


def test_frozen_installed_entrypoint_cancels_worker_and_propagates_cleanup_failure(
    monkeypatch: Any,
    tmp_path: Path,
) -> None:
    layout = InstallLayout.discover(tmp_path / "LocalAppData")
    process = FakeWorkerProcess()
    worker = CleanupWorker(tmp_path, "6" * 32, process)
    events: list[str] = []
    monkeypatch.delenv("LSMLTK_UNINSTALL_RELOCATED", raising=False)
    monkeypatch.setattr(uninstall_module.InstallLayout, "discover", lambda: layout)
    monkeypatch.setattr(uninstall_module.sys, "executable", str(layout.uninstaller))
    monkeypatch.setattr(uninstall_module.sys, "frozen", True, raising=False)
    monkeypatch.setattr(
        uninstall_module,
        "launch_relocated_uninstaller",
        lambda *_args, **_kwargs: worker,
    )
    monkeypatch.setattr(
        uninstall_module,
        "wait_for_cleanup_worker_status",
        lambda *_args, **_kwargs: events.append("ready"),
    )
    monkeypatch.setattr(
        uninstall_module,
        "cancel_cleanup_worker",
        lambda _worker: events.append("cancel"),
    )
    monkeypatch.setattr(
        uninstall_module,
        "commit_cleanup_worker",
        lambda _worker: pytest.fail("failed cleanup must not commit"),
    )

    def failed_main(**kwargs: Any) -> int:
        kwargs["before_cleanup"]()
        events.append("failed")
        kwargs["notifier"]("Uninstall incomplete", "locked", True)
        return 1

    monkeypatch.setattr(uninstall_module, "main", failed_main)
    monkeypatch.setattr(
        uninstall_module,
        "show_result",
        lambda *_args: events.append("notify"),
    )

    assert run_uninstall_entrypoint() == 1
    assert events == ["ready", "failed", "cancel", "notify"]


def test_commit_ack_timeout_is_reported_and_buffered_success_is_suppressed(
    monkeypatch: Any,
    tmp_path: Path,
) -> None:
    layout = InstallLayout.discover(tmp_path / "LocalAppData")
    worker = CleanupWorker(tmp_path, "7" * 32, FakeWorkerProcess())
    notifications: list[tuple[str, str, bool]] = []
    cancellations: list[CleanupWorker] = []
    monkeypatch.delenv("LSMLTK_UNINSTALL_RELOCATED", raising=False)
    monkeypatch.setattr(uninstall_module.InstallLayout, "discover", lambda: layout)
    monkeypatch.setattr(uninstall_module.sys, "executable", str(layout.uninstaller))
    monkeypatch.setattr(uninstall_module.sys, "frozen", True, raising=False)
    monkeypatch.setattr(uninstall_module, "launch_relocated_uninstaller", lambda *_a, **_kw: worker)
    monkeypatch.setattr(uninstall_module, "wait_for_cleanup_worker_status", lambda *_a, **_kw: None)
    monkeypatch.setattr(
        uninstall_module,
        "commit_cleanup_worker",
        lambda _worker: (_ for _ in ()).throw(RuntimeError("commit acknowledgement timeout")),
    )
    monkeypatch.setattr(
        uninstall_module,
        "cancel_cleanup_worker",
        lambda value: cancellations.append(value),
    )

    def successful_main(**kwargs: Any) -> int:
        kwargs["before_cleanup"]()
        kwargs["notifier"]("Uninstall complete", "done", False)
        return 0

    monkeypatch.setattr(uninstall_module, "main", successful_main)
    monkeypatch.setattr(
        uninstall_module,
        "show_result",
        lambda title, message, error: notifications.append((title, message, error)),
    )

    assert run_uninstall_entrypoint() == 1
    assert cancellations == [worker]
    assert notifications == [("Uninstall incomplete", "commit acknowledgement timeout", True)]


def test_worker_status_wait_is_bounded_and_rejects_forged_nonce(
    monkeypatch: Any,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(uninstall_module.tempfile, "gettempdir", lambda: str(tmp_path))
    nonce = "8" * 32
    temp_dir = tmp_path / f"{APP_NAME}-uninstall-{nonce}"
    temp_dir.mkdir()
    worker = CleanupWorker(temp_dir, nonce, FakeWorkerProcess())
    ticks = iter((0.0, 1.0))
    with pytest.raises(RuntimeError, match="Timed out"):
        wait_for_cleanup_worker_status(
            worker,
            "ready",
            timeout_seconds=0.5,
            monotonic=lambda: next(ticks),
            sleeper=lambda _seconds: None,
        )

    worker.status_path.write_text(
        '{"nonce":"99999999999999999999999999999999","state":"ready"}',
        encoding="utf-8",
    )
    with pytest.raises(RuntimeError, match="authentication"):
        wait_for_cleanup_worker_status(worker, "ready", timeout_seconds=0.5)


def test_cleanup_relocated_copy_removes_unlocked_temp_directory(
    monkeypatch: Any,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(uninstall_module.tempfile, "gettempdir", lambda: str(tmp_path))
    nonce = "c" * 32
    temp_dir = tmp_path / f"{APP_NAME}-uninstall-{nonce}"
    temp_dir.mkdir()
    (temp_dir / "copy.exe").write_bytes(b"copy")

    cleanup_relocated_copy(temp_dir, nonce=nonce)

    assert not temp_dir.exists()


def test_cleanup_worker_self_cleanup_waits_for_its_bootloader_parent(
    monkeypatch: Any,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(uninstall_module.tempfile, "gettempdir", lambda: str(tmp_path))
    nonce = "d" * 32
    temp_dir = tmp_path / f"{APP_NAME}-uninstall-{nonce}"
    temp_dir.mkdir()
    layout = InstallLayout.discover(tmp_path / "LocalAppData")
    running = temp_dir / layout.uninstaller.name
    running.write_bytes(b"worker")
    calls: list[tuple[Path, str | None, int | None]] = []
    monkeypatch.setattr(
        uninstall_module,
        "_wait_for_cleanup_command",
        lambda *_args, **_kwargs: "cancel",
    )
    monkeypatch.setattr(uninstall_module.os, "getppid", lambda: 4321)
    monkeypatch.setattr(
        uninstall_module,
        "cleanup_relocated_copy",
        lambda path, *, nonce=None, parent_pid=None: calls.append((path, nonce, parent_pid)),
    )

    result = uninstall_module._run_cleanup_worker(
        layout,
        running=running.resolve(),
        temp_dir=temp_dir,
        nonce=nonce,
        wait_pid=9876,
    )

    assert result == 0
    assert calls == [(temp_dir, nonce, 4321)]


def test_main_cancel_returns_normally_without_acquiring_gates(tmp_path: Path) -> None:
    appdata, data_dir = app_paths(tmp_path)
    events: list[str] = []
    notifications: list[tuple[str, str, bool]] = []

    result = main(
        appdata=appdata,
        local_appdata=tmp_path / "LocalAppData",
        confirmer=lambda _title, _message: False,
        notifier=lambda title, message, error: notifications.append((title, message, error)),
        operation_mutex=FakeMutex("operation", events),
        mutex=FakeMutex("app", events),
    )

    assert result == 0
    assert events == []
    assert data_dir.exists()
    assert notifications == [("Uninstall cancelled", "Nothing was removed.", False)]


def test_complete_cleanup_leaves_separately_installed_ltk_manager_untouched(
    tmp_path: Path,
) -> None:
    appdata, data_dir = app_paths(tmp_path)
    (data_dir / "library").mkdir()
    (data_dir / "library" / "mod.fantome").write_bytes(b"owned mod")

    local_appdata = tmp_path / "LocalAppData"
    layout = InstallLayout.discover(local_appdata)
    layout.install_dir.mkdir(parents=True)
    layout.executable.write_bytes(b"main")
    layout.uninstaller.write_bytes(b"uninstall")

    official_ltk = local_appdata / "LTK Manager"
    official_ltk.mkdir(parents=True)
    sentinel = official_ltk / "ltk-manager.exe"
    sentinel.write_bytes(b"external official app")
    roaming = tmp_path / "AppData" / "Roaming"
    shortcut = start_menu_shortcut_path(roaming)
    shortcut.parent.mkdir(parents=True)
    shortcut.write_bytes(b"owned shell link")

    notifications: list[tuple[str, str, bool]] = []
    result = main(
        appdata=roaming,
        local_appdata=local_appdata,
        confirmer=lambda _title, _message: True,
        notifier=lambda title, message, error: notifications.append((title, message, error)),
        process_finder=lambda _names: None,
        startup_remover=lambda: RemovalState.NOT_FOUND,
        registration_remover=lambda: RemovalState.REMOVED,
        operation_mutex=FakeMutex(),
        mutex=FakeMutex(),
    )

    assert result == 0
    assert not data_dir.exists()
    assert not layout.install_dir.exists()
    assert not shortcut.exists()
    assert sentinel.read_bytes() == b"external official app"
    assert notifications[0][0] == "Uninstall complete"
    assert notifications[0][2] is False
