"""Asynchronous supervisor for an externally installed LTK patcher host.

The provider executable and DLL are intentionally not acquired or bundled by
this module.  :class:`LTKPatcherRuntime` only starts a provider that has already
been located in an official LTK Manager installation and accepted by the open
LTK sidecar's ``provider.smoke`` preflight.
"""

from __future__ import annotations

import atexit
import logging
import os
import subprocess
import sys
from collections.abc import Callable, Mapping, Sequence
from contextlib import suppress
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from queue import Empty, Queue
from threading import Lock, Thread, current_thread
from time import monotonic
from typing import Any, Protocol, TextIO, cast

from .ltk_engine import LTKEngineClient, LTKInstallation, engine_path


class PatcherRuntimeError(RuntimeError):
    """The provider could not be validated, started, or controlled safely."""


class _StartCancelled(PatcherRuntimeError):
    """Internal marker for a cooperative stop during sidecar preflight."""


class PatcherPhase(str, Enum):
    """Current lifecycle phase exposed to any UI or automation client."""

    IDLE = "idle"
    STARTING = "starting"
    SCANNING = "scanning"
    INJECTING = "injecting"
    INJECTED = "injected"
    WAITING = "waiting"
    STOPPING = "stopping"
    FAILED = "failed"


class HostEventKind(str, Enum):
    OK = "ok"
    STATUS = "status"
    ERROR = "error"
    DLL = "dll"


class HostState(str, Enum):
    INJECTING = "injecting"
    INJECTED = "injected"
    WAITING = "waiting"
    EXITED = "exited"
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class HostEvent:
    """One parsed line emitted by ``ltk_patcher_host.exe``."""

    kind: HostEventKind
    timestamp: str
    message: str
    state: HostState | None = None
    pid: int | None = None
    tid: int | None = None
    level: str | None = None


@dataclass(frozen=True, slots=True)
class PatcherStatus:
    """Immutable point-in-time runtime state."""

    phase: PatcherPhase
    message: str
    last_error: str | None
    process_id: int | None
    running: bool
    last_event: HostEvent | None


@dataclass(frozen=True, slots=True)
class _ConfigurationFailure:
    message: str


class ProviderValidator(Protocol):
    def provider_smoke(
        self,
        *,
        installation_dir: Path,
        overlay_prefix: Path,
        event_lines: Sequence[str] = (),
    ) -> Mapping[str, Any]: ...


class ProviderProcess(Protocol):
    @property
    def stdin(self) -> TextIO | None: ...

    @property
    def stdout(self) -> TextIO | None: ...

    @property
    def stderr(self) -> TextIO | None: ...

    @property
    def pid(self) -> int: ...

    def poll(self) -> int | None: ...

    def wait(self, timeout: float | None = None) -> int: ...

    def kill(self) -> None: ...


ProcessFactory = Callable[[Sequence[str], Path], ProviderProcess]
EventSink = Callable[[HostEvent], None]


def _spawn_provider(command: Sequence[str], working_directory: Path) -> ProviderProcess:
    """Start the provider with fixed, non-shell, hidden-pipe settings."""

    process = subprocess.Popen(
        list(command),
        cwd=str(working_directory),
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        bufsize=1,
        shell=False,
        creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0,
    )
    return cast(ProviderProcess, process)


def _split_first_token(value: str) -> tuple[str, str]:
    value = value.lstrip()
    for index, character in enumerate(value):
        if character in " \t":
            return value[:index], value[index + 1 :].lstrip()
    return value, ""


def parse_host_event(line: str) -> HostEvent | None:
    """Parse the documented LTK host line protocol without executing input."""

    line = line.rstrip("\r\n")
    if not line:
        return None
    keyword, rest = _split_first_token(line)
    timestamp, rest = _split_first_token(rest)
    if not timestamp:
        return None
    if keyword == HostEventKind.OK.value:
        return HostEvent(HostEventKind.OK, timestamp, rest)
    if keyword == HostEventKind.ERROR.value:
        return HostEvent(HostEventKind.ERROR, timestamp, rest)
    if keyword == HostEventKind.STATUS.value:
        state_value, message = _split_first_token(rest)
        try:
            state = HostState(state_value)
        except ValueError:
            return None
        return HostEvent(HostEventKind.STATUS, timestamp, message, state=state)
    if keyword == HostEventKind.DLL.value:
        pid_value, rest = _split_first_token(rest)
        tid_value, rest = _split_first_token(rest)
        level, message = _split_first_token(rest)
        try:
            pid = int(pid_value)
            tid = int(tid_value)
        except ValueError:
            return None
        if pid < 0 or tid < 0 or not level:
            return None
        return HostEvent(
            HostEventKind.DLL,
            timestamp,
            message,
            pid=pid,
            tid=tid,
            level=level,
        )
    return None


class LTKPatcherRuntime:
    """UI-independent lifecycle manager for the official external provider.

    ``start`` performs the sidecar preflight and returns after the process is
    configured.  Output is consumed on daemon threads.  ``stop`` has one
    bounded deadline and force-kills a host that ignores ``stop`` and EOF.
    """

    MAX_HOST_LINE_CHARACTERS = 64 * 1024

    def __init__(
        self,
        engine: LTKEngineClient | ProviderValidator,
        installation: LTKInstallation,
        *,
        process_factory: ProcessFactory = _spawn_provider,
        event_sink: EventSink | None = None,
        shutdown_timeout_seconds: float = 5.0,
        configuration_timeout_seconds: float = 5.0,
        register_atexit: bool = True,
        logger: logging.Logger | None = None,
    ) -> None:
        if shutdown_timeout_seconds <= 0:
            raise ValueError("shutdown_timeout_seconds must be positive")
        if configuration_timeout_seconds <= 0:
            raise ValueError("configuration_timeout_seconds must be positive")
        self._engine = engine
        self._installation = installation
        self._process_factory = process_factory
        self._event_sink = event_sink
        self._shutdown_timeout_seconds = shutdown_timeout_seconds
        self._configuration_timeout_seconds = configuration_timeout_seconds
        self._logger = logger or logging.getLogger(__name__)
        self._lock = Lock()
        self._shutdown_lock = Lock()
        self._process: ProviderProcess | None = None
        self._stdout_thread: Thread | None = None
        self._stderr_thread: Thread | None = None
        self._configuration_events: Queue[HostEvent | _ConfigurationFailure] | None = None
        self._generation = 0
        self._stop_requested_generation: int | None = None
        self._phase = PatcherPhase.IDLE
        self._message = "Patcher is stopped"
        self._last_error: str | None = None
        self._last_event: HostEvent | None = None
        self._closed = False
        self._atexit_callback: Callable[[], None] | None = None
        if register_atexit:
            self._atexit_callback = self._shutdown_at_exit
            atexit.register(self._atexit_callback)

    def __enter__(self) -> LTKPatcherRuntime:
        return self

    def __exit__(self, _type: object, _value: object, _traceback: object) -> None:
        self.close()

    def status(self) -> PatcherStatus:
        """Return a thread-safe lifecycle snapshot."""

        with self._lock:
            return self._status_locked()

    def start(self, overlay_prefix: Path) -> PatcherStatus:
        """Validate and start scanning with the fixed safe host configuration."""

        with self._lock:
            self._ensure_startable_locked()
        overlay_prefix = Path(overlay_prefix).resolve()
        self._validate_start_paths(overlay_prefix)
        with self._lock:
            self._ensure_startable_locked()
            self._generation += 1
            generation = self._generation
            self._stop_requested_generation = None
            self._phase = PatcherPhase.STARTING
            self._message = "Validating installed LTK patcher provider"
            self._last_error = None
            self._last_event = None

        try:
            smoke = self._engine.provider_smoke(
                installation_dir=self._installation.root,
                overlay_prefix=overlay_prefix,
            )
            host = self._validated_host(smoke)
        except Exception as exc:
            message = f"LTK patcher preflight failed: {exc}"
            with self._lock:
                if self._generation == generation:
                    self._phase = PatcherPhase.FAILED
                    self._message = message
                    self._last_error = message
            if isinstance(exc, PatcherRuntimeError):
                raise
            raise PatcherRuntimeError(message) from exc

        prefix = str(overlay_prefix)
        if not prefix.endswith(("/", "\\")):
            prefix += os.sep
        configuration_commands = (
            "config loglevel 16",
            "config flags 0",
            f"config prefix {prefix}",
        )

        process: ProviderProcess | None = None
        stdout_thread: Thread | None = None
        stderr_thread: Thread | None = None
        try:
            with self._lock:
                if self._generation != generation or self._stop_requested_generation == generation:
                    self._phase = PatcherPhase.IDLE
                    self._message = "Patcher start was cancelled"
                    raise _StartCancelled("Patcher start was cancelled")
            process = self._process_factory((str(host),), host.parent)
            if process.stdin is None or process.stdout is None or process.stderr is None:
                raise PatcherRuntimeError("LTK patcher host did not expose all protocol pipes")
            configuration_events: Queue[HostEvent | _ConfigurationFailure] = Queue()
            stdout_thread = Thread(
                target=self._read_stdout,
                args=(process, generation),
                name="ltk-patcher-stdout",
                daemon=True,
            )
            stderr_thread = Thread(
                target=self._read_stderr,
                args=(process, generation),
                name="ltk-patcher-stderr",
                daemon=True,
            )
            with self._lock:
                if self._generation != generation or self._stop_requested_generation == generation:
                    self._phase = PatcherPhase.IDLE
                    self._message = "Patcher start was cancelled"
                    raise _StartCancelled("Patcher start was cancelled")
                self._process = process
                self._configuration_events = configuration_events
                self._message = "Configuring installed LTK patcher provider"
                self._stdout_thread = stdout_thread
                self._stderr_thread = stderr_thread
            stdout_thread.start()
            stderr_thread.start()

            configuration_deadline = monotonic() + self._configuration_timeout_seconds
            for command in configuration_commands:
                self._send_command(process, command)
                self._await_configuration_ack(
                    process,
                    generation,
                    configuration_events,
                    command,
                    configuration_deadline,
                )
            # Injection scanning is impossible until every safety-critical
            # configuration line above has received an ordered `ok` response.
            self._send_command(process, "start scan")
            with self._lock:
                if self._configuration_events is configuration_events:
                    self._configuration_events = None
                if self._generation != generation or self._stop_requested_generation == generation:
                    raise _StartCancelled("Patcher start was cancelled")
                if self._phase is PatcherPhase.STARTING:
                    self._phase = PatcherPhase.SCANNING
                    self._message = "Scanning for League of Legends"
                return self._status_locked()
        except Exception as exc:
            if process is not None:
                with self._shutdown_lock:
                    deadline = monotonic() + self._shutdown_timeout_seconds
                    self._kill_process(process)
                    self._wait_until(process, deadline)
                    self._close_output_pipes(process)
                    self._join_reader(stdout_thread, deadline)
                    self._join_reader(stderr_thread, deadline)
            message = f"Could not start LTK patcher host: {exc}"
            with self._lock:
                if self._generation == generation:
                    if self._configuration_events is not None:
                        self._configuration_events = None
                    if self._process is process:
                        self._process = None
                    if isinstance(exc, _StartCancelled):
                        self._phase = PatcherPhase.IDLE
                        self._message = "Patcher start was cancelled"
                        self._last_error = None
                    else:
                        self._phase = PatcherPhase.FAILED
                        self._message = message
                        self._last_error = message
            if isinstance(exc, PatcherRuntimeError):
                raise
            raise PatcherRuntimeError(message) from exc

    def stop(self, timeout_seconds: float | None = None) -> bool:
        """Stop the current host within one deadline, killing it if necessary."""

        timeout = self._shutdown_timeout_seconds if timeout_seconds is None else timeout_seconds
        if timeout <= 0:
            raise ValueError("timeout_seconds must be positive")
        deadline = monotonic() + timeout
        with self._shutdown_lock:
            with self._lock:
                process = self._process
                generation = self._generation
                self._stop_requested_generation = generation
                if process is None:
                    if self._phase is not PatcherPhase.FAILED:
                        self._phase = PatcherPhase.IDLE
                        self._message = "Patcher is stopped"
                    return True
                self._phase = PatcherPhase.STOPPING
                self._message = "Stopping LTK patcher provider"
                stdout_thread = self._stdout_thread
                stderr_thread = self._stderr_thread

            self._signal_stop(process)
            stopped = self._wait_until(process, deadline)
            if not stopped:
                self._kill_process(process)
                stopped = self._wait_until(process, deadline)
            self._close_output_pipes(process)
            self._join_reader(stdout_thread, deadline)
            self._join_reader(stderr_thread, deadline)

            with self._lock:
                if self._process is process:
                    self._process = None
                    self._stdout_thread = None
                    self._stderr_thread = None
                self._configuration_events = None
                self._phase = PatcherPhase.IDLE if stopped else PatcherPhase.FAILED
                self._message = (
                    "Patcher is stopped"
                    if stopped
                    else "LTK patcher provider did not stop within the shutdown deadline"
                )
                if not stopped:
                    self._last_error = self._message
            return stopped

    def close(self) -> None:
        """Permanently close the runtime and stop its child process."""

        with self._lock:
            if self._closed:
                return
            self._closed = True
        try:
            self.stop()
        finally:
            callback = self._atexit_callback
            if callback is not None:
                atexit.unregister(callback)
                self._atexit_callback = None

    def _validate_start_paths(self, overlay_prefix: Path) -> None:
        host = self._installation.host_executable
        hook = self._installation.hook_dll
        if host is None or hook is None or not host.is_file() or not hook.is_file():
            raise PatcherRuntimeError(
                "The installed LTK Manager release does not contain the new patcher provider"
            )
        if not overlay_prefix.is_dir():
            raise PatcherRuntimeError(f"Overlay prefix is not a directory: {overlay_prefix}")
        if any(character in str(overlay_prefix) for character in ("\r", "\n", "\0")):
            raise PatcherRuntimeError("Overlay prefix is unsafe for the host line protocol")

    def _ensure_startable_locked(self) -> None:
        if self._closed:
            raise PatcherRuntimeError("Patcher runtime is closed")
        if self._process is not None or self._phase not in {
            PatcherPhase.IDLE,
            PatcherPhase.FAILED,
        }:
            raise PatcherRuntimeError("Patcher is already running or stopping")

    def _validated_host(self, smoke: Mapping[str, Any]) -> Path:
        required = {
            "available": True,
            "configuration_only": True,
            "process_started": False,
            "anti_hack_enforced": True,
        }
        if any(smoke.get(name) is not expected for name, expected in required.items()):
            raise PatcherRuntimeError("LTK engine rejected the installed patcher provider")
        value = smoke.get("host_executable")
        if not isinstance(value, str) or not value:
            raise PatcherRuntimeError("LTK engine returned no validated provider executable")
        returned_host = engine_path(value).resolve()
        expected_host = self._installation.host_executable
        hook_value = smoke.get("hook_library")
        expected_hook = self._installation.hook_dll
        assert expected_host is not None
        assert expected_hook is not None
        if returned_host != expected_host.resolve():
            raise PatcherRuntimeError("LTK engine validated a different provider executable")
        if not isinstance(hook_value, str) or not hook_value:
            raise PatcherRuntimeError("LTK engine returned no validated provider hook library")
        if engine_path(hook_value).resolve() != expected_hook.resolve():
            raise PatcherRuntimeError("LTK engine validated a different provider hook library")
        if returned_host.parent != expected_hook.resolve().parent:
            raise PatcherRuntimeError("LTK provider host and hook library must share a directory")
        return returned_host

    @staticmethod
    def _send_command(process: ProviderProcess, command: str) -> None:
        stream = process.stdin
        if stream is None or stream.closed:
            raise PatcherRuntimeError("LTK patcher host stdin closed unexpectedly")
        stream.write(command + "\n")
        stream.flush()

    def _await_configuration_ack(
        self,
        process: ProviderProcess,
        generation: int,
        events: Queue[HostEvent | _ConfigurationFailure],
        command: str,
        deadline: float,
    ) -> None:
        """Require one ordered host acknowledgement before sending the next command."""

        while True:
            with self._lock:
                if (
                    self._process is not process
                    or self._generation != generation
                    or self._stop_requested_generation == generation
                ):
                    raise _StartCancelled("Patcher start was cancelled")
            remaining = deadline - monotonic()
            if remaining <= 0:
                raise PatcherRuntimeError(
                    f"LTK patcher did not acknowledge '{command}' within the configuration deadline"
                )
            try:
                signal = events.get(timeout=min(0.1, remaining))
            except Empty:
                continue
            if isinstance(signal, _ConfigurationFailure):
                raise PatcherRuntimeError(signal.message)
            if signal.kind is HostEventKind.ERROR:
                detail = signal.message or "unspecified host error"
                raise PatcherRuntimeError(f"LTK patcher rejected '{command}': {detail}")
            if signal.kind is HostEventKind.OK:
                return
            if signal.kind is HostEventKind.STATUS:
                raise PatcherRuntimeError(
                    f"LTK patcher changed state before acknowledging '{command}'"
                )

    def _read_stdout(self, process: ProviderProcess, generation: int) -> None:
        stream = process.stdout
        assert stream is not None
        try:
            while True:
                line = stream.readline(self.MAX_HOST_LINE_CHARACTERS + 1)
                if not line:
                    break
                if len(line) > self.MAX_HOST_LINE_CHARACTERS:
                    self._record_protocol_error(
                        process,
                        generation,
                        "LTK patcher host emitted an oversized protocol line",
                    )
                    continue
                event = parse_host_event(line)
                if event is not None:
                    self._record_event(process, generation, event)
        except (OSError, ValueError) as exc:
            self._record_protocol_error(
                process,
                generation,
                f"Could not read LTK patcher host output: {exc}",
            )
        finally:
            self._record_stdout_closed(process, generation)

    def _read_stderr(self, process: ProviderProcess, generation: int) -> None:
        stream = process.stderr
        assert stream is not None
        try:
            while True:
                line = stream.readline(self.MAX_HOST_LINE_CHARACTERS + 1)
                if not line:
                    break
                self._logger.warning("[ltk-patcher stderr] %s", line.rstrip())
        except (OSError, ValueError) as exc:
            with self._lock:
                expected_stop = self._stop_requested_generation == generation
            if not expected_stop:
                self._logger.warning("Could not read LTK patcher stderr: %s", exc)

    def _record_event(
        self,
        process: ProviderProcess,
        generation: int,
        event: HostEvent,
    ) -> None:
        failed = False
        configuration_events: Queue[HostEvent | _ConfigurationFailure] | None = None
        with self._lock:
            if self._process is not process or self._generation != generation:
                return
            configuration_events = self._configuration_events
            self._last_event = event
            if event.kind is HostEventKind.ERROR:
                self._last_error = event.message
            elif event.kind is HostEventKind.STATUS and event.state is not None:
                phase = {
                    HostState.INJECTING: PatcherPhase.INJECTING,
                    HostState.INJECTED: PatcherPhase.INJECTED,
                    HostState.WAITING: PatcherPhase.WAITING,
                    HostState.EXITED: PatcherPhase.SCANNING,
                    HostState.FAILED: PatcherPhase.FAILED,
                }[event.state]
                if self._phase is not PatcherPhase.STOPPING:
                    self._phase = phase
                    self._message = event.message or phase.value.capitalize()
                if event.state is HostState.FAILED:
                    self._last_error = event.message or "LTK patcher injection failed"
                    failed = True
        if configuration_events is not None:
            configuration_events.put(event)
        self._emit_event(event)
        if failed:
            Thread(
                target=self._stop_after_failure,
                args=(process, generation),
                name="ltk-patcher-failure-stop",
                daemon=True,
            ).start()

    def _record_protocol_error(
        self,
        process: ProviderProcess,
        generation: int,
        message: str,
    ) -> None:
        configuration_events: Queue[HostEvent | _ConfigurationFailure] | None = None
        with self._lock:
            if self._process is process and self._generation == generation:
                self._last_error = message
                configuration_events = self._configuration_events
        if configuration_events is not None:
            configuration_events.put(_ConfigurationFailure(message))
        self._logger.warning(message)

    def _record_stdout_closed(self, process: ProviderProcess, generation: int) -> None:
        configuration_events: Queue[HostEvent | _ConfigurationFailure] | None = None
        with self._lock:
            if self._process is not process or self._generation != generation:
                return
            configuration_events = self._configuration_events
            expected = self._stop_requested_generation == generation
            return_code = process.poll()
            if return_code is not None:
                self._process = None
                self._stdout_thread = None
                self._stderr_thread = None
            if not expected and self._phase is not PatcherPhase.FAILED:
                detail = self._last_error
                suffix = f" (host reported: {detail})" if detail else ""
                self._phase = PatcherPhase.FAILED
                self._message = f"LTK patcher host exited unexpectedly{suffix}"
                self._last_error = self._message
        if configuration_events is not None:
            configuration_events.put(
                _ConfigurationFailure("LTK patcher host exited during configuration")
            )

    def _stop_after_failure(self, process: ProviderProcess, generation: int) -> None:
        with self._shutdown_lock:
            with self._lock:
                if self._process is not process or self._generation != generation:
                    return
                self._stop_requested_generation = generation
            deadline = monotonic() + self._shutdown_timeout_seconds
            self._signal_stop(process)
            if not self._wait_until(process, deadline):
                self._kill_process(process)
                self._wait_until(process, deadline)
            self._close_output_pipes(process)
            with self._lock:
                if self._process is process:
                    self._process = None

    def _emit_event(self, event: HostEvent) -> None:
        if self._event_sink is None:
            return
        try:
            self._event_sink(event)
        except Exception:
            self._logger.exception("LTK patcher event sink failed")

    @staticmethod
    def _signal_stop(process: ProviderProcess) -> None:
        stream = process.stdin
        if stream is None:
            return
        try:
            if not stream.closed:
                stream.write("stop\n")
                stream.flush()
                stream.close()
        except (OSError, ValueError):
            with suppress(OSError, ValueError):
                stream.close()

    @staticmethod
    def _wait_until(process: ProviderProcess, deadline: float) -> bool:
        if process.poll() is not None:
            return True
        remaining = deadline - monotonic()
        if remaining <= 0:
            return False
        try:
            process.wait(timeout=remaining)
        except (subprocess.TimeoutExpired, OSError):
            return process.poll() is not None
        return True

    @staticmethod
    def _kill_process(process: ProviderProcess) -> None:
        if process.poll() is not None:
            return
        with suppress(OSError):
            process.kill()

    @staticmethod
    def _close_output_pipes(process: ProviderProcess) -> None:
        for stream in (process.stdout, process.stderr):
            if stream is None:
                continue
            with suppress(OSError, ValueError):
                stream.close()

    @staticmethod
    def _join_reader(thread: Thread | None, deadline: float) -> None:
        if thread is None or thread is current_thread():
            return
        remaining = deadline - monotonic()
        if remaining > 0:
            thread.join(remaining)

    def _status_locked(self) -> PatcherStatus:
        process = self._process
        running = process is not None and process.poll() is None
        return PatcherStatus(
            phase=self._phase,
            message=self._message,
            last_error=self._last_error,
            process_id=process.pid if process is not None and running else None,
            running=running,
            last_event=self._last_event,
        )

    def _shutdown_at_exit(self) -> None:
        try:
            self.stop()
        except Exception:
            self._logger.exception("Could not stop LTK patcher during application shutdown")


__all__ = [
    "HostEvent",
    "HostEventKind",
    "HostState",
    "LTKPatcherRuntime",
    "PatcherPhase",
    "PatcherRuntimeError",
    "PatcherStatus",
    "ProcessFactory",
    "ProviderProcess",
    "ProviderValidator",
    "parse_host_event",
]
