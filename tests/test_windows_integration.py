from __future__ import annotations

import ctypes
import logging
from pathlib import Path
from typing import Any

import pytest

from league_skin_manager.windows_integration import (
    ACTIVATION_EVENT_NAME,
    ERROR_ALREADY_EXISTS,
    LEGACY_MUTEX_NAME,
    MUTEX_NAME,
    WAIT_OBJECT_0,
    WAIT_TIMEOUT,
    InstanceActivationEvent,
    ProcessLauncher,
    SingleInstanceMutex,
    StartupRegistration,
    open_path,
    reveal_path,
)


class Function:
    def __init__(self, result: int) -> None:
        self.result = result
        self.calls: list[tuple[Any, ...]] = []
        self.argtypes: object = None
        self.restype: object = None

    def __call__(self, *args: Any) -> int:
        self.calls.append(args)
        return self.result


class Kernel32:
    def __init__(self, handle: int = 123) -> None:
        self.CreateMutexW = Function(handle)
        self.CloseHandle = Function(1)


class ActivationKernel32:
    def __init__(self) -> None:
        self.CreateEventW = Function(456)
        self.SetEvent = Function(1)
        self.WaitForSingleObject = Function(WAIT_OBJECT_0)
        self.CloseHandle = Function(1)


def test_mutex_acquires_and_releases_native_handle(monkeypatch: Any) -> None:
    kernel32 = Kernel32()
    monkeypatch.setattr(ctypes, "set_last_error", lambda _value: None)
    monkeypatch.setattr(ctypes, "get_last_error", lambda: 0)
    mutex = SingleInstanceMutex(kernel32=kernel32)
    assert mutex.acquire()
    assert mutex.handle == 123
    mutex.release()
    assert mutex.handle is None
    assert [call[2] for call in kernel32.CreateMutexW.calls] == [
        LEGACY_MUTEX_NAME,
        MUTEX_NAME,
    ]
    assert len(kernel32.CloseHandle.calls) == 2


def test_mutex_rejects_existing_instance_and_closes_duplicate(monkeypatch: Any) -> None:
    kernel32 = Kernel32()
    monkeypatch.setattr(ctypes, "set_last_error", lambda _value: None)
    monkeypatch.setattr(ctypes, "get_last_error", lambda: ERROR_ALREADY_EXISTS)
    mutex = SingleInstanceMutex(kernel32=kernel32)
    assert not mutex.acquire()
    assert mutex.handle is None
    assert len(kernel32.CloseHandle.calls) == 1


def test_mutex_rolls_back_legacy_handle_when_v2_instance_exists(monkeypatch: Any) -> None:
    kernel32 = Kernel32()
    errors = iter((0, ERROR_ALREADY_EXISTS))
    monkeypatch.setattr(ctypes, "set_last_error", lambda _value: None)
    monkeypatch.setattr(ctypes, "get_last_error", lambda: next(errors))

    mutex = SingleInstanceMutex(kernel32=kernel32)

    assert not mutex.acquire()
    assert mutex.handle is None
    assert len(kernel32.CreateMutexW.calls) == 2
    assert len(kernel32.CloseHandle.calls) == 2


def test_mutex_without_native_backend_is_a_safe_noop() -> None:
    mutex = SingleInstanceMutex(kernel32=None)
    mutex.kernel32 = None
    assert mutex.acquire()
    mutex.release()


def test_activation_event_retains_signals_waits_and_closes() -> None:
    kernel32 = ActivationKernel32()
    activation = InstanceActivationEvent(kernel32=kernel32)

    assert activation.create()
    assert activation.signal()
    assert activation.wait(250)
    kernel32.WaitForSingleObject.result = WAIT_TIMEOUT
    assert not activation.wait(0)
    activation.close()
    activation.close()

    assert len(kernel32.CreateEventW.calls) == 1
    assert kernel32.CreateEventW.calls[0][3] == ACTIVATION_EVENT_NAME
    assert len(kernel32.SetEvent.calls) == 1
    assert len(kernel32.WaitForSingleObject.calls) == 2
    assert len(kernel32.CloseHandle.calls) == 1


def test_activation_event_without_native_backend_is_a_safe_noop() -> None:
    activation = InstanceActivationEvent(kernel32=None)
    activation.kernel32 = None

    assert not activation.create()
    assert not activation.signal()
    assert not activation.wait(0)
    activation.close()


def test_activation_event_surfaces_native_failures(monkeypatch: Any) -> None:
    monkeypatch.setattr(ctypes, "get_last_error", lambda: 5)

    create_failure = ActivationKernel32()
    create_failure.CreateEventW.result = 0
    with pytest.raises(OSError, match="create"):
        InstanceActivationEvent(kernel32=create_failure).create()

    signal_failure = ActivationKernel32()
    signal_failure.SetEvent.result = 0
    activation = InstanceActivationEvent(kernel32=signal_failure)
    with pytest.raises(OSError, match="signal"):
        activation.signal()
    activation.close()

    wait_failure = ActivationKernel32()
    wait_failure.WaitForSingleObject.result = 0xFFFFFFFF
    activation = InstanceActivationEvent(kernel32=wait_failure)
    with pytest.raises(ValueError, match="negative"):
        activation.wait(-1)
    with pytest.raises(OSError, match="wait"):
        activation.wait(1)
    activation.close()


def test_activation_event_context_manager_closes_handle() -> None:
    kernel32 = ActivationKernel32()

    with InstanceActivationEvent(kernel32=kernel32) as activation:
        assert activation.handle == 456

    assert activation.handle is None
    assert len(kernel32.CloseHandle.calls) == 1


def test_startup_command_quotes_absolute_executable(tmp_path: Path) -> None:
    executable = tmp_path / "folder with spaces" / "app.exe"
    assert StartupRegistration._command(executable) == f'"{executable.resolve()}" --background'
    assert StartupRegistration._command(executable, ()) == f'"{executable.resolve()}"'


def test_open_path_uses_windows_shell(monkeypatch: Any, tmp_path: Path) -> None:
    opened: list[str] = []
    monkeypatch.setattr("league_skin_manager.windows_integration.os.name", "nt")
    monkeypatch.setattr(
        "league_skin_manager.windows_integration.os.startfile",
        lambda value: opened.append(value),
    )

    open_path(tmp_path)

    assert opened == [str(tmp_path.resolve())]


def test_reveal_file_selects_it_in_explorer_without_executing(
    monkeypatch: Any, tmp_path: Path
) -> None:
    selected = tmp_path / "mod.fantome"
    selected.write_bytes(b"mod")
    system_root = tmp_path / "Windows"
    explorer = system_root / "explorer.exe"
    system_root.mkdir()
    explorer.write_bytes(b"explorer")
    calls: list[tuple[list[str], dict[str, object]]] = []
    monkeypatch.setattr("league_skin_manager.windows_integration.os.name", "nt")
    monkeypatch.setenv("SYSTEMROOT", str(system_root))
    monkeypatch.setattr(
        "league_skin_manager.windows_integration.subprocess.Popen",
        lambda args, **kwargs: calls.append((args, kwargs)),
    )

    reveal_path(selected)

    assert calls == [([str(explorer), "/select,", str(selected.resolve())], {"close_fds": True})]


class Process:
    def __init__(self, name: str | None = None, error: Exception | None = None) -> None:
        self.info = {"name": name}
        self.error = error


def test_process_running_lookup_is_case_insensitive(monkeypatch: Any) -> None:
    monkeypatch.setattr(
        "league_skin_manager.windows_integration.psutil.process_iter",
        lambda _fields: [Process(None), Process("LTK-MANAGER.EXE")],
    )
    assert ProcessLauncher.is_running("ltk-manager.exe")
    assert not ProcessLauncher.is_running("different.exe")


def test_launcher_rejects_missing_file_and_starts_existing(
    monkeypatch: Any, tmp_path: Path
) -> None:
    launcher = ProcessLauncher(logging.getLogger("test"))
    assert not launcher.launch(tmp_path / "missing.exe")

    executable = tmp_path / "manager.exe"
    executable.write_bytes(b"exe")
    calls: list[tuple[list[str], dict[str, object]]] = []
    monkeypatch.setattr(
        "league_skin_manager.windows_integration.subprocess.Popen",
        lambda args, **kwargs: calls.append((args, kwargs)),
    )
    assert launcher.launch(executable)
    assert calls[0][0] == [str(executable)]
    assert calls[0][1]["cwd"] == str(tmp_path)


def test_running_under_matches_exact_owned_process_paths(monkeypatch: Any, tmp_path: Path) -> None:
    class OwnedProcess:
        def __init__(self, executable: Path | None) -> None:
            self.info = {"exe": str(executable) if executable is not None else None}

    manager_dir = tmp_path / "LTK Manager"
    owned = manager_dir / "resources" / "ltk_patcher_host.exe"
    unrelated = tmp_path / "elsewhere" / "mod-tools.exe"
    monkeypatch.setattr(
        "league_skin_manager.windows_integration.psutil.process_iter",
        lambda _fields: [OwnedProcess(unrelated), OwnedProcess(owned), OwnedProcess(None)],
    )

    assert ProcessLauncher.is_running_under(manager_dir)
    assert not ProcessLauncher.is_running_under(tmp_path / "different")


def test_running_under_blocks_known_owned_names_when_executable_is_inaccessible(
    monkeypatch: Any,
    tmp_path: Path,
) -> None:
    class Process:
        def __init__(self, name: str | None, executable: str | None) -> None:
            self.info = {"name": name, "exe": executable}

    processes = [
        Process("unrelated.exe", None),
        Process("LTK_PATCHER_HOST.EXE", None),
    ]
    monkeypatch.setattr(
        "league_skin_manager.windows_integration.psutil.process_iter",
        lambda _fields: processes,
    )

    assert ProcessLauncher.is_running_under(tmp_path / "manager")


def test_running_under_does_not_block_same_known_name_at_accessible_unowned_path(
    monkeypatch: Any,
    tmp_path: Path,
) -> None:
    class Process:
        info = {"name": "mod-tools.exe", "exe": str(tmp_path / "other" / "mod-tools.exe")}

    monkeypatch.setattr(
        "league_skin_manager.windows_integration.psutil.process_iter",
        lambda _fields: [Process()],
    )

    assert not ProcessLauncher.is_running_under(tmp_path / "manager")
