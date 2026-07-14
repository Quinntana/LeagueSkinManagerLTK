from __future__ import annotations

import os
import subprocess
from collections.abc import Callable, Mapping, Sequence
from io import StringIO
from pathlib import Path
from queue import Queue
from threading import Event, Thread
from time import monotonic, sleep
from typing import Any, TextIO, cast

import pytest

import league_skin_manager.ltk_patcher as patcher_module
from league_skin_manager.ltk_engine import LTKInstallation
from league_skin_manager.ltk_patcher import (
    HostEventKind,
    HostState,
    LTKPatcherRuntime,
    PatcherPhase,
    PatcherRuntimeError,
    ProcessFactory,
    ProviderProcess,
    parse_host_event,
)


class QueueStream:
    def __init__(self) -> None:
        self._lines: Queue[str | None] = Queue()
        self.closed = False

    def emit(self, line: str) -> None:
        self._lines.put(line)

    def readline(self, _size: int = -1) -> str:
        value = self._lines.get()
        return "" if value is None else value

    def close(self) -> None:
        if not self.closed:
            self.closed = True
            self._lines.put(None)


class RecordingInput(StringIO):
    def __init__(self, callback: Callable[[str], None]) -> None:
        super().__init__()
        self.commands: list[str] = []
        self._callback = callback

    def write(self, value: str) -> int:
        result = super().write(value)
        for line in value.splitlines():
            self.commands.append(line)
            self._callback(line)
        return result


class FakeProcess:
    def __init__(self, *, exit_on_stop: bool = True, acknowledge_commands: bool = True) -> None:
        self.pid = 4242
        self.returncode: int | None = None
        self.killed = False
        self._exited = Event()
        self._stdout = QueueStream()
        self._stderr = QueueStream()
        self.stdout = cast(TextIO, self._stdout)
        self.stderr = cast(TextIO, self._stderr)

        def command_received(line: str) -> None:
            if exit_on_stop and line == "stop":
                self.exit(0)
            elif acknowledge_commands and line.startswith("config "):
                self.emit_stdout(f"ok 1.0 accepted {line}")

        self.stdin = cast(TextIO, RecordingInput(command_received))

    @property
    def commands(self) -> list[str]:
        return cast(RecordingInput, self.stdin).commands

    def emit_stdout(self, line: str) -> None:
        self._stdout.emit(line + "\n")

    def emit_stderr(self, line: str) -> None:
        self._stderr.emit(line + "\n")

    def exit(self, returncode: int) -> None:
        if self.returncode is not None:
            return
        self.returncode = returncode
        self._exited.set()
        self._stdout.close()
        self._stderr.close()

    def poll(self) -> int | None:
        return self.returncode

    def wait(self, timeout: float | None = None) -> int:
        if not self._exited.wait(timeout):
            raise subprocess.TimeoutExpired("ltk_patcher_host.exe", timeout or 0)
        assert self.returncode is not None
        return self.returncode

    def kill(self) -> None:
        self.killed = True
        self.exit(-9)


class FakeValidator:
    def __init__(self, host: Path, order: list[str] | None = None) -> None:
        self.host = host
        self.order = order
        self.calls: list[tuple[Path, Path, tuple[str, ...]]] = []
        self.result: dict[str, Any] = {
            "available": True,
            "configuration_only": True,
            "process_started": False,
            "anti_hack_enforced": True,
            "host_executable": str(host.resolve()),
            "hook_library": str(host.with_name("ltk_patcher_dll.dll").resolve()),
            # Deliberately ignored by the runtime. Commands are fixed locally.
            "config_lines": ["config flags 4"],
        }

    def provider_smoke(
        self,
        *,
        installation_dir: Path,
        overlay_prefix: Path,
        event_lines: Sequence[str] = (),
    ) -> Mapping[str, Any]:
        if self.order is not None:
            self.order.append("smoke")
        self.calls.append((installation_dir, overlay_prefix, tuple(event_lines)))
        return self.result


def make_installation(tmp_path: Path) -> LTKInstallation:
    root = tmp_path / "LTK Manager"
    resources = root / "resources"
    resources.mkdir(parents=True)
    manager = root / "ltk-manager.exe"
    host = resources / "ltk_patcher_host.exe"
    hook = resources / "ltk_patcher_dll.dll"
    manager.write_bytes(b"MZmanager")
    host.write_bytes(b"MZhost")
    hook.write_bytes(b"MZhook")
    return LTKInstallation(root, manager, "1.12.0", host, hook)


def wait_for(predicate: Callable[[], bool], timeout: float = 1.0) -> None:
    deadline = monotonic() + timeout
    while not predicate():
        if monotonic() >= deadline:
            raise AssertionError("condition was not reached")
        sleep(0.005)


def runtime_with(
    tmp_path: Path,
    process: FakeProcess,
    *,
    sink: Callable[[Any], None] | None = None,
    order: list[str] | None = None,
) -> tuple[LTKPatcherRuntime, FakeValidator, Path]:
    installation = make_installation(tmp_path)
    host = installation.host_executable
    assert host is not None
    validator = FakeValidator(host, order)
    overlay = tmp_path / "overlay"
    overlay.mkdir()

    def spawn(command: Sequence[str], cwd: Path) -> ProviderProcess:
        if order is not None:
            order.append("spawn")
        assert command == (str(host.resolve()),)
        assert cwd == host.resolve().parent
        return process

    runtime = LTKPatcherRuntime(
        validator,
        installation,
        process_factory=cast(ProcessFactory, spawn),
        event_sink=sink,
        shutdown_timeout_seconds=0.05,
        register_atexit=False,
    )
    return runtime, validator, overlay


@pytest.mark.parametrize(
    ("line", "kind", "state", "message"),
    (
        ("ok 1.0 config prefix set", HostEventKind.OK, None, "config prefix set"),
        (
            "status 2.0 injecting scanning for game",
            HostEventKind.STATUS,
            HostState.INJECTING,
            "scanning for game",
        ),
        ("error 3.0 unknown command", HostEventKind.ERROR, None, "unknown command"),
        (
            "dll 4.0 12 34 ERROR target: diagnostic text",
            HostEventKind.DLL,
            None,
            "target: diagnostic text",
        ),
    ),
)
def test_parse_host_event(
    line: str,
    kind: HostEventKind,
    state: HostState | None,
    message: str,
) -> None:
    event = parse_host_event(line)
    assert event is not None
    assert event.kind is kind
    assert event.state is state
    assert event.message == message
    if kind is HostEventKind.DLL:
        assert (event.pid, event.tid, event.level) == (12, 34, "ERROR")


@pytest.mark.parametrize(
    "line",
    ("", "unknown 1.0 data", "status 1.0 invalid nope", "dll 1.0 nope 2 INFO bad"),
)
def test_parse_host_event_rejects_unrecognized_or_malformed_lines(line: str) -> None:
    assert parse_host_event(line) is None


def test_start_preflights_then_sends_only_fixed_safe_commands(tmp_path: Path) -> None:
    order: list[str] = []
    process = FakeProcess()
    runtime, validator, overlay = runtime_with(tmp_path, process, order=order)
    try:
        status = runtime.start(overlay)

        assert order == ["smoke", "spawn"]
        assert validator.calls == [(validator.host.parent.parent, overlay.resolve(), ())]
        assert process.commands == [
            "config loglevel 16",
            "config flags 0",
            f"config prefix {overlay.resolve()}{os.sep}",
            "start scan",
        ]
        assert status.phase is PatcherPhase.SCANNING
        assert status.running is True
        assert status.process_id == 4242
    finally:
        runtime.close()


def test_runtime_consumes_status_error_and_dll_events_asynchronously(tmp_path: Path) -> None:
    process = FakeProcess()
    received: list[Any] = []
    runtime, _validator, overlay = runtime_with(tmp_path, process, sink=received.append)
    try:
        runtime.start(overlay)
        process.emit_stdout("status 1.0 injected DLL attached")
        wait_for(lambda: runtime.status().phase is PatcherPhase.INJECTED)
        process.emit_stdout("error 2.0 recoverable protocol warning")
        process.emit_stdout("dll 3.0 100 200 INFO redirected archive")
        wait_for(
            lambda: len([event for event in received if event.kind is not HostEventKind.OK]) == 3
        )

        status = runtime.status()
        assert status.phase is PatcherPhase.INJECTED
        assert status.last_error == "recoverable protocol warning"
        assert received[-1].kind is HostEventKind.DLL
        assert received[-1].message == "redirected archive"
    finally:
        runtime.close()


def test_failed_status_is_preserved_and_stops_host_in_background(tmp_path: Path) -> None:
    process = FakeProcess(exit_on_stop=True)
    runtime, _validator, overlay = runtime_with(tmp_path, process)
    try:
        runtime.start(overlay)
        process.emit_stdout("status 5.0 failed DLL signature rejected")
        wait_for(lambda: process.returncode == 0)

        status = runtime.status()
        assert status.phase is PatcherPhase.FAILED
        assert status.last_error == "DLL signature rejected"
        assert "stop" in process.commands
    finally:
        runtime.close()


def test_stop_is_bounded_and_kills_an_uncooperative_host(tmp_path: Path) -> None:
    process = FakeProcess(exit_on_stop=False)
    runtime, _validator, overlay = runtime_with(tmp_path, process)
    runtime.start(overlay)

    started = monotonic()
    stopped = runtime.stop(timeout_seconds=0.03)

    assert stopped is True
    assert monotonic() - started < 0.5
    assert process.killed is True
    assert process.commands[-1] == "stop"
    assert runtime.status().phase is PatcherPhase.IDLE
    runtime.close()


def test_unexpected_host_exit_surfaces_last_protocol_error(tmp_path: Path) -> None:
    process = FakeProcess()
    runtime, _validator, overlay = runtime_with(tmp_path, process)
    try:
        runtime.start(overlay)
        process.emit_stdout("error 1.0 antivirus blocked DLL")
        process.exit(7)
        wait_for(lambda: runtime.status().phase is PatcherPhase.FAILED)

        status = runtime.status()
        assert status.running is False
        assert "antivirus blocked DLL" in (status.last_error or "")
    finally:
        runtime.close()


def test_rejected_smoke_result_never_spawns_provider(tmp_path: Path) -> None:
    installation = make_installation(tmp_path)
    assert installation.host_executable is not None
    validator = FakeValidator(installation.host_executable)
    validator.result["anti_hack_enforced"] = False
    overlay = tmp_path / "overlay"
    overlay.mkdir()
    spawned = False

    def spawn(_command: Sequence[str], _cwd: Path) -> ProviderProcess:
        nonlocal spawned
        spawned = True
        return FakeProcess()

    runtime = LTKPatcherRuntime(
        validator,
        installation,
        process_factory=cast(ProcessFactory, spawn),
        register_atexit=False,
    )
    with pytest.raises(PatcherRuntimeError, match="rejected"):
        runtime.start(overlay)

    assert spawned is False
    assert runtime.status().phase is PatcherPhase.FAILED
    runtime.close()


def test_configuration_error_prevents_scan_and_kills_host(tmp_path: Path) -> None:
    process = FakeProcess(acknowledge_commands=False)
    runtime, _validator, overlay = runtime_with(tmp_path, process)
    errors: list[Exception] = []

    def start() -> None:
        try:
            runtime.start(overlay)
        except Exception as exc:
            errors.append(exc)

    thread = Thread(target=start)
    thread.start()
    wait_for(lambda: process.commands == ["config loglevel 16"])
    process.emit_stdout("ok 1.0 loglevel accepted")
    wait_for(lambda: process.commands[-1:] == ["config flags 0"])
    process.emit_stdout("error 1.1 flags rejected")
    thread.join(1)

    assert not thread.is_alive()
    assert len(errors) == 1
    assert "flags rejected" in str(errors[0])
    assert "config prefix" not in process.commands
    assert "start scan" not in process.commands
    assert process.killed is True
    runtime.close()


def test_configuration_ack_timeout_prevents_scan(tmp_path: Path) -> None:
    installation = make_installation(tmp_path)
    assert installation.host_executable is not None
    process = FakeProcess(acknowledge_commands=False)
    validator = FakeValidator(installation.host_executable)
    overlay = tmp_path / "overlay"
    overlay.mkdir()
    runtime = LTKPatcherRuntime(
        validator,
        installation,
        process_factory=lambda _command, _cwd: process,
        configuration_timeout_seconds=0.02,
        register_atexit=False,
    )

    with pytest.raises(PatcherRuntimeError, match="did not acknowledge"):
        runtime.start(overlay)

    assert process.commands == ["config loglevel 16"]
    assert process.killed is True
    runtime.close()


def test_smoke_must_validate_the_expected_hook_library(tmp_path: Path) -> None:
    installation = make_installation(tmp_path)
    assert installation.host_executable is not None
    validator = FakeValidator(installation.host_executable)
    validator.result["hook_library"] = str(tmp_path / "different.dll")
    overlay = tmp_path / "overlay"
    overlay.mkdir()
    spawned = False

    def spawn(_command: Sequence[str], _cwd: Path) -> ProviderProcess:
        nonlocal spawned
        spawned = True
        return FakeProcess()

    runtime = LTKPatcherRuntime(
        validator,
        installation,
        process_factory=cast(ProcessFactory, spawn),
        register_atexit=False,
    )
    with pytest.raises(PatcherRuntimeError, match="different provider hook"):
        runtime.start(overlay)

    assert spawned is False
    runtime.close()


def test_runtime_rejects_missing_provider_and_duplicate_start(tmp_path: Path) -> None:
    process = FakeProcess()
    runtime, _validator, overlay = runtime_with(tmp_path, process)
    try:
        runtime.start(overlay)
        with pytest.raises(PatcherRuntimeError, match="already running"):
            runtime.start(overlay)
    finally:
        runtime.close()

    installation = make_installation(tmp_path / "other")
    assert installation.hook_dll is not None
    installation.hook_dll.unlink()
    assert installation.host_executable is not None
    validator = FakeValidator(installation.host_executable)
    missing = LTKPatcherRuntime(validator, installation, register_atexit=False)
    with pytest.raises(PatcherRuntimeError, match="does not contain"):
        missing.start(tmp_path)
    missing.close()


def test_context_manager_stops_provider_and_closes_runtime(tmp_path: Path) -> None:
    process = FakeProcess()
    runtime, _validator, overlay = runtime_with(tmp_path, process)
    with runtime:
        runtime.start(overlay)

    assert process.returncode == 0
    assert runtime.status().phase is PatcherPhase.IDLE
    with pytest.raises(PatcherRuntimeError, match="closed"):
        runtime.start(overlay)


def test_stop_cancels_a_start_waiting_for_preflight(tmp_path: Path) -> None:
    installation = make_installation(tmp_path)
    assert installation.host_executable is not None
    entered = Event()
    release = Event()
    errors: list[Exception] = []
    spawned = False

    class BlockingValidator(FakeValidator):
        def provider_smoke(
            self,
            *,
            installation_dir: Path,
            overlay_prefix: Path,
            event_lines: Sequence[str] = (),
        ) -> Mapping[str, Any]:
            entered.set()
            release.wait(1)
            return super().provider_smoke(
                installation_dir=installation_dir,
                overlay_prefix=overlay_prefix,
                event_lines=event_lines,
            )

    validator = BlockingValidator(installation.host_executable)

    def spawn(_command: Sequence[str], _cwd: Path) -> ProviderProcess:
        nonlocal spawned
        spawned = True
        return FakeProcess()

    runtime = LTKPatcherRuntime(
        validator,
        installation,
        process_factory=spawn,
        register_atexit=False,
    )

    def start() -> None:
        try:
            runtime.start(tmp_path)
        except Exception as exc:
            errors.append(exc)

    thread = Thread(target=start)
    thread.start()
    assert entered.wait(1)
    assert runtime.stop() is True
    release.set()
    thread.join(1)

    assert not thread.is_alive()
    assert len(errors) == 1
    assert isinstance(errors[0], PatcherRuntimeError)
    assert "cancelled" in str(errors[0])
    assert spawned is False
    assert runtime.status().phase is PatcherPhase.IDLE
    runtime.close()


def test_default_process_factory_uses_no_shell_and_hidden_pipes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    process = FakeProcess()
    captured: dict[str, Any] = {}

    def popen(command: list[str], **kwargs: Any) -> FakeProcess:
        captured["command"] = command
        captured.update(kwargs)
        return process

    monkeypatch.setattr(patcher_module.subprocess, "Popen", popen)  # type: ignore[attr-defined]
    returned = patcher_module._spawn_provider(("provider.exe",), tmp_path)

    assert returned.pid == process.pid
    assert captured["command"] == ["provider.exe"]
    assert captured["cwd"] == str(tmp_path)
    assert captured["shell"] is False
    assert captured["stdin"] is subprocess.PIPE
    assert captured["stdout"] is subprocess.PIPE
    assert captured["stderr"] is subprocess.PIPE


def test_atexit_registration_is_removed_by_close(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    installation = make_installation(tmp_path)
    assert installation.host_executable is not None
    validator = FakeValidator(installation.host_executable)
    registered: list[Callable[[], None]] = []
    unregistered: list[Callable[[], None]] = []
    atexit_module = cast(Any, patcher_module).atexit
    monkeypatch.setattr(atexit_module, "register", registered.append)
    monkeypatch.setattr(atexit_module, "unregister", unregistered.append)

    runtime = LTKPatcherRuntime(validator, installation)
    runtime.close()

    assert len(registered) == 1
    assert unregistered == registered
