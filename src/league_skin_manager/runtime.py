"""UI-independent orchestration for overlay builds and the external LTK patcher.

The coordinator deliberately owns no widgets and starts no provider directly.
It snapshots the default profile, asks the open sidecar to build an overlay, and
then delegates the provider lifecycle to :class:`LTKPatcherRuntime`.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from enum import Enum
from pathlib import Path
from threading import Event, Lock, RLock, Thread, current_thread
from time import monotonic
from typing import Any, Protocol

from .config import ENGINE_PROTOCOL_VERSION
from .ltk_engine import LTKInstallation, LTKInstallationLocator, engine_path
from .ltk_patcher import (
    EventSink,
    HostEvent,
    LTKPatcherRuntime,
    PatcherPhase,
    PatcherStatus,
)
from .mod_library import LocalModLibrary, ModRecord
from .profile import ProfileStore


class RuntimeCoordinatorError(RuntimeError):
    """The application runtime could not be started or controlled safely."""


class RuntimePhase(str, Enum):
    """Small, UI-stable lifecycle exposed by :class:`RuntimeCoordinator`."""

    IDLE = "idle"
    BUILDING = "building"
    SCANNING = "scanning"
    RUNNING = "running"
    STOPPING = "stopping"
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class RuntimeStatus:
    """Immutable point-in-time state safe to retain across callback threads."""

    phase: RuntimePhase
    message: str
    last_error: str | None = None
    enabled_mod_count: int = 0
    overlay_root: Path | None = None
    provider_version: str | None = None
    process_id: int | None = None
    conflict_count: int = 0
    linked_bin_offender_count: int = 0
    owns_resources: bool = False

    @property
    def active(self) -> bool:
        return self.owns_resources or self.phase in {
            RuntimePhase.BUILDING,
            RuntimePhase.SCANNING,
            RuntimePhase.RUNNING,
            RuntimePhase.STOPPING,
        }


class EngineBoundary(Protocol):
    """Subset of :class:`LTKEngineClient` needed by the coordinator."""

    def hello(self) -> Mapping[str, Any]: ...

    def build_overlay(
        self,
        *,
        game_dir: Path,
        overlay_dir: Path,
        state_dir: Path,
        enabled_packages: Sequence[Path],
        cancelled: Callable[[], bool] | None = None,
    ) -> Mapping[str, Any]: ...

    def cancel_active(self) -> bool: ...

    def provider_smoke(
        self,
        *,
        installation_dir: Path,
        overlay_prefix: Path,
        event_lines: Sequence[str] = (),
    ) -> Mapping[str, Any]: ...


class InstallationLocatorBoundary(Protocol):
    def discover(self) -> LTKInstallation | None: ...

    def revalidate(self, installation: LTKInstallation) -> bool: ...


class PatcherBoundary(Protocol):
    def status(self) -> PatcherStatus: ...

    def start(self, overlay_prefix: Path) -> PatcherStatus: ...

    def stop(self, timeout_seconds: float | None = None) -> bool: ...

    def close(self) -> None: ...


class PatcherFactory(Protocol):
    def __call__(
        self,
        engine: EngineBoundary,
        installation: LTKInstallation,
        event_sink: EventSink,
    ) -> PatcherBoundary: ...


StatusSink = Callable[[RuntimeStatus], None]
InstallationInUse = Callable[[Path], bool]


def _default_patcher_factory(
    engine: EngineBoundary,
    installation: LTKInstallation,
    event_sink: EventSink,
) -> PatcherBoundary:
    return LTKPatcherRuntime(engine, installation, event_sink=event_sink)


class _Cancelled(RuntimeCoordinatorError):
    pass


class _Keep:
    pass


_KEEP = _Keep()


class RuntimeCoordinator:
    """Coordinate profile, package library, open engine, and external provider.

    ``start`` only reserves the lifecycle and launches a daemon worker. Slow
    profile validation, Authenticode-backed provider discovery, overlay building,
    and provider preflight therefore never block the caller. Status callbacks are
    serialized and are always invoked without holding the state lock.
    """

    def __init__(
        self,
        profile: ProfileStore,
        library: LocalModLibrary,
        engine: EngineBoundary | None,
        locator: LTKInstallationLocator | InstallationLocatorBoundary,
        *,
        overlay_dir: Path,
        state_dir: Path,
        patcher_factory: PatcherFactory = _default_patcher_factory,
        installation_in_use: InstallationInUse | None = None,
        status_sink: StatusSink | None = None,
        shutdown_timeout_seconds: float = 5.0,
        monitor_interval_seconds: float = 0.1,
        logger: logging.Logger | None = None,
    ) -> None:
        if shutdown_timeout_seconds <= 0:
            raise ValueError("shutdown_timeout_seconds must be positive")
        if monitor_interval_seconds <= 0:
            raise ValueError("monitor_interval_seconds must be positive")
        self._profile = profile
        self._library = library
        self._engine = engine
        self._locator = locator
        self.overlay_dir = Path(overlay_dir).resolve()
        self.state_dir = Path(state_dir).resolve()
        if _paths_overlap(self.overlay_dir, self.state_dir):
            raise ValueError("overlay_dir and state_dir must not overlap")
        self._patcher_factory = patcher_factory
        self._installation_in_use = installation_in_use
        self._status_sink = status_sink
        self._shutdown_timeout_seconds = shutdown_timeout_seconds
        self._monitor_interval_seconds = monitor_interval_seconds
        self._logger = logger or logging.getLogger(__name__)

        self._lock = RLock()
        self._callback_lock = RLock()
        self._stop_lock = Lock()
        self._generation = 0
        self._cancel = Event()
        self._worker: Thread | None = None
        self._patcher: PatcherBoundary | None = None
        self._closed = False
        self._status = RuntimeStatus(RuntimePhase.IDLE, "Runtime is stopped")

    def __enter__(self) -> RuntimeCoordinator:
        return self

    def __exit__(self, _type: object, _value: object, _traceback: object) -> None:
        self.close()

    def status(self) -> RuntimeStatus:
        """Return the latest immutable, thread-safe lifecycle snapshot."""

        with self._lock:
            return self._status

    def start(self) -> RuntimeStatus:
        """Begin validation and overlay construction without blocking the caller."""

        with self._lock:
            if self._closed:
                raise RuntimeCoordinatorError("Runtime coordinator is closed")
            if self._has_active_work_locked():
                raise RuntimeCoordinatorError("Runtime is already starting, running, or stopping")
            self._generation += 1
            generation = self._generation
            cancel = Event()
            self._cancel = cancel
            initial = RuntimeStatus(
                RuntimePhase.BUILDING,
                "Validating the enabled mod profile",
                owns_resources=True,
            )
            self._status = initial
            worker = Thread(
                target=self._run,
                args=(generation, cancel),
                name="ltk-runtime-coordinator",
                daemon=True,
            )
            self._worker = worker

        # Holding the callback lock preserves callback ordering if the worker
        # completes immediately, while the state lock remains fully re-entrant.
        try:
            with self._callback_lock:
                worker.start()
                self._emit(initial)
        except RuntimeError as exc:
            with self._lock:
                if self._worker is worker:
                    self._worker = None
                self._cancel.set()
            self._fail(generation, f"Could not start runtime worker: {exc}")
            self._release_ownership(generation)
            raise RuntimeCoordinatorError("Could not start runtime worker") from exc
        return initial

    def stop(self, timeout_seconds: float | None = None) -> bool:
        """Stop background work and any provider within one bounded deadline."""

        timeout = self._shutdown_timeout_seconds if timeout_seconds is None else timeout_seconds
        if timeout <= 0:
            raise ValueError("timeout_seconds must be positive")
        deadline = monotonic() + timeout
        with self._stop_lock:
            with self._lock:
                worker = self._worker
                patcher = self._patcher
                active = self._has_active_work_locked()
                if not active:
                    if self._status.phase is RuntimePhase.FAILED:
                        idle = RuntimeStatus(RuntimePhase.IDLE, "Runtime is stopped")
                        self._status = idle
                    else:
                        idle = None
                else:
                    self._cancel.set()
                    stopping = RuntimeStatus(
                        RuntimePhase.STOPPING,
                        "Stopping the LTK runtime",
                        enabled_mod_count=self._status.enabled_mod_count,
                        overlay_root=self._status.overlay_root,
                        provider_version=self._status.provider_version,
                        process_id=self._status.process_id,
                        conflict_count=self._status.conflict_count,
                        linked_bin_offender_count=self._status.linked_bin_offender_count,
                        owns_resources=True,
                    )
                    self._status = stopping
                    idle = None
            if not active:
                if idle is not None:
                    self._emit(idle)
                return True

            self._emit(stopping)
            try:
                if self._engine is not None:
                    self._engine.cancel_active()
            except Exception:
                self._logger.exception("Could not cancel the active LTK engine request")
            patcher_stopped = True
            if patcher is not None:
                patcher_stopped = self._stop_patcher(patcher, deadline)

            if worker is not None and worker is not current_thread():
                remaining = deadline - monotonic()
                if remaining > 0 and worker.is_alive():
                    worker.join(remaining)
            worker_stopped = worker is None or worker is current_thread() or not worker.is_alive()

            # The worker may have constructed the patcher just after our first
            # snapshot. Give that instance the remaining part of the same budget.
            with self._lock:
                late_patcher = self._patcher
            if late_patcher is not None and late_patcher is not patcher:
                patcher_stopped = self._stop_patcher(late_patcher, deadline) and patcher_stopped

            with self._lock:
                if patcher_stopped and (self._patcher is patcher or self._patcher is late_patcher):
                    self._patcher = None

            stopped = patcher_stopped and worker_stopped
            with self._lock:
                if stopped:
                    final = RuntimeStatus(RuntimePhase.IDLE, "Runtime is stopped")
                else:
                    message = "LTK runtime did not stop within the shutdown deadline"
                    final = RuntimeStatus(
                        RuntimePhase.FAILED,
                        message,
                        last_error=message,
                        enabled_mod_count=self._status.enabled_mod_count,
                        overlay_root=self._status.overlay_root,
                        provider_version=self._status.provider_version,
                        process_id=self._status.process_id,
                        conflict_count=self._status.conflict_count,
                        linked_bin_offender_count=self._status.linked_bin_offender_count,
                        owns_resources=True,
                    )
                self._status = final
            self._emit(final)
            return stopped

    def close(self, timeout_seconds: float | None = None) -> bool:
        """Permanently close the coordinator and stop any external host."""

        with self._lock:
            self._closed = True
        return self.stop(timeout_seconds)

    def _run(self, generation: int, cancel: Event) -> None:
        patcher: PatcherBoundary | None = None
        patcher_stopped = False
        try:
            engine = self._engine
            if engine is None:
                raise RuntimeCoordinatorError(
                    "The open ltk-engine sidecar is not built or installed"
                )
            enabled = self._reconciled_enabled_records()
            if not enabled:
                raise RuntimeCoordinatorError("Enable at least one local mod before starting")
            self._check_cancelled(cancel)
            game_dir = self._profile.game_dir()
            if game_dir is None:
                raise RuntimeCoordinatorError("Select a League of Legends game directory first")
            package_paths = self._package_paths(enabled)
            self._transition(
                generation,
                RuntimePhase.BUILDING,
                "Checking the open LTK engine",
                enabled_mod_count=len(enabled),
                clear_last_error=True,
            )
            self._validate_engine(engine.hello())
            self._check_cancelled(cancel)

            installation = self._locator.discover()
            if installation is None:
                raise RuntimeCoordinatorError(
                    "No verified official LTK Manager installation was found"
                )
            if not installation.injection_provider_available:
                raise RuntimeCoordinatorError(
                    "The installed LTK Manager release predates the new patcher provider"
                )
            if self._installation_in_use is not None and self._installation_in_use(
                installation.root
            ):
                raise RuntimeCoordinatorError(
                    "Close LTK Manager or its patcher host before starting enabled mods"
                )
            host = installation.host_executable
            hook = installation.hook_dll
            if host is None or hook is None or not host.is_file() or not hook.is_file():
                raise RuntimeCoordinatorError(
                    "The verified LTK patcher host or DLL is no longer available"
                )
            self._check_cancelled(cancel)

            self.overlay_dir.parent.mkdir(parents=True, exist_ok=True)
            self.state_dir.parent.mkdir(parents=True, exist_ok=True)
            self._transition(
                generation,
                RuntimePhase.BUILDING,
                f"Building an overlay for {len(enabled)} enabled mod(s)",
                enabled_mod_count=len(enabled),
                provider_version=installation.version,
                clear_last_error=True,
            )
            result = engine.build_overlay(
                game_dir=game_dir,
                overlay_dir=self.overlay_dir,
                state_dir=self.state_dir,
                enabled_packages=package_paths,
                cancelled=cancel.is_set,
            )
            overlay_root, conflict_count, linked_bin_offender_count = (
                self._validated_overlay_result(result, len(enabled))
            )
            if linked_bin_offender_count:
                self._transition(
                    generation,
                    RuntimePhase.BUILDING,
                    "Overlay diagnostics blocked activation",
                    conflict_count=conflict_count,
                    linked_bin_offender_count=linked_bin_offender_count,
                )
                raise RuntimeCoordinatorError(
                    "Overlay diagnostics found "
                    f"{linked_bin_offender_count} linked-BIN offender(s); activation was blocked"
                )
            self._check_cancelled(cancel)

            self._transition(
                generation,
                RuntimePhase.SCANNING,
                "Starting the verified LTK patcher provider",
                enabled_mod_count=len(enabled),
                overlay_root=overlay_root,
                provider_version=installation.version,
                conflict_count=conflict_count,
                linked_bin_offender_count=linked_bin_offender_count,
                clear_last_error=True,
            )
            if not self._locator.revalidate(installation):
                raise RuntimeCoordinatorError(
                    "The official LTK provider changed or failed signature revalidation"
                )
            patcher = self._patcher_factory(
                engine,
                installation,
                lambda event: self._on_patcher_event(generation, event),
            )
            with self._lock:
                if generation != self._generation:
                    raise _Cancelled("Runtime start was superseded")
                self._patcher = patcher
            self._check_cancelled(cancel)
            patcher_status = patcher.start(overlay_root)
            self._sync_patcher_status(generation, patcher_status)
            self._check_cancelled(cancel)

            while not cancel.wait(self._monitor_interval_seconds):
                patcher_status = patcher.status()
                self._sync_patcher_status(generation, patcher_status)
                if patcher_status.phase is PatcherPhase.FAILED:
                    raise RuntimeCoordinatorError(
                        patcher_status.last_error or "LTK patcher provider failed"
                    )
                if patcher_status.phase is PatcherPhase.IDLE and not patcher_status.running:
                    self._transition(
                        generation,
                        RuntimePhase.IDLE,
                        "Runtime is stopped",
                        process_id=None,
                        clear_last_error=True,
                    )
                    return
        except _Cancelled:
            pass
        except Exception as exc:
            if not cancel.is_set():
                message = str(exc).strip() or type(exc).__name__
                self._fail(generation, message)
        finally:
            if patcher is not None:
                try:
                    patcher_stopped = patcher.stop(self._shutdown_timeout_seconds)
                    if patcher_stopped:
                        patcher.close()
                except Exception:
                    self._logger.exception("Could not clean up the LTK patcher runtime")
            with self._lock:
                if self._patcher is patcher and (patcher is None or patcher_stopped):
                    self._patcher = None
                if self._worker is current_thread():
                    self._worker = None
            self._release_ownership(generation)

    def _reconciled_enabled_records(self) -> tuple[ModRecord, ...]:
        # Refresh hashes every content-addressed package before trusting profile
        # IDs.  In particular, a same-size cache modification must not benefit
        # from the manifest's size-based metadata fast path.
        self._library.refresh()
        enabled = self._profile.enabled_records()
        current = {record.id: record for record in self._library.records()}
        reconciled_ids = tuple(record.id for record in enabled if record.id in current)
        if reconciled_ids != tuple(record.id for record in enabled):
            self._profile.set_enabled(reconciled_ids)
        return tuple(current[content_id] for content_id in reconciled_ids)

    def _package_paths(self, enabled: tuple[ModRecord, ...]) -> tuple[Path, ...]:
        package_root = self._library.package_dir.resolve()
        paths: list[Path] = []
        for record in enabled:
            candidate = package_root / record.file_name
            if candidate.is_symlink() or not candidate.is_file():
                raise RuntimeCoordinatorError(
                    f"Enabled package is missing from the local library: {record.name}"
                )
            try:
                resolved = candidate.resolve(strict=True)
                resolved.relative_to(package_root)
            except (OSError, RuntimeError, ValueError) as exc:
                raise RuntimeCoordinatorError(
                    f"Enabled package has an unsafe library path: {record.name}"
                ) from exc
            paths.append(resolved)
        return tuple(paths)

    @staticmethod
    def _validate_engine(hello: Mapping[str, Any]) -> None:
        methods = hello.get("methods")
        provider = hello.get("provider")
        required_methods = {"engine.hello", "overlay.build", "provider.smoke"}
        if (
            hello.get("engine_name") != "ltk-engine"
            or hello.get("protocol_version") != ENGINE_PROTOCOL_VERSION
            or not isinstance(methods, list)
            or not required_methods.issubset(
                {method for method in methods if isinstance(method, str)}
            )
            or not isinstance(provider, dict)
            or provider.get("mode") != "configuration_only"
            or provider.get("anti_hack_enforced") is not True
            or provider.get("binaries_bundled") is not False
            or provider.get("downloads_binaries") is not False
            or provider.get("starts_processes") is not False
        ):
            raise RuntimeCoordinatorError(
                "The ltk-engine handshake is incompatible with this application"
            )

    def _validated_overlay_result(
        self, result: Mapping[str, Any], enabled_mod_count: int
    ) -> tuple[Path, int, int]:
        raw_root = result.get("overlay_root")
        returned_count = result.get("enabled_package_count")
        conflict_count = result.get("conflict_count")
        linked_bin_offender_count = result.get("linked_bin_offender_count")
        if not isinstance(raw_root, str) or not raw_root or "\0" in raw_root:
            raise RuntimeCoordinatorError("LTK engine returned an invalid overlay root")
        if type(returned_count) is not int or returned_count != enabled_mod_count:
            raise RuntimeCoordinatorError("LTK engine returned an invalid enabled package count")
        if type(conflict_count) is not int or conflict_count < 0:
            raise RuntimeCoordinatorError("LTK engine returned an invalid conflict count")
        if type(linked_bin_offender_count) is not int or linked_bin_offender_count < 0:
            raise RuntimeCoordinatorError(
                "LTK engine returned an invalid linked-BIN offender count"
            )
        try:
            owned_root = self.overlay_dir.resolve(strict=True)
            overlay_root = engine_path(raw_root).resolve(strict=True)
            overlay_root.relative_to(owned_root)
        except (OSError, RuntimeError, ValueError) as exc:
            raise RuntimeCoordinatorError(
                "LTK engine returned an overlay outside the application-owned directory"
            ) from exc
        if not overlay_root.is_dir():
            raise RuntimeCoordinatorError("LTK engine overlay root is not a directory")
        return overlay_root, conflict_count, linked_bin_offender_count

    def _on_patcher_event(self, generation: int, _event: HostEvent) -> None:
        with self._lock:
            if generation != self._generation or self._cancel.is_set():
                return
            patcher = self._patcher
        if patcher is not None:
            self._sync_patcher_status(generation, patcher.status())

    def _sync_patcher_status(self, generation: int, status: PatcherStatus) -> None:
        with self._lock:
            if (
                generation != self._generation
                or self._cancel.is_set()
                or self._status.phase is RuntimePhase.FAILED
            ):
                return
        phase = _coordinator_phase(status.phase)
        message = status.message
        with self._lock:
            conflict_count = self._status.conflict_count
        if phase is RuntimePhase.RUNNING and conflict_count:
            message = f"{message} ({conflict_count} package conflict(s) resolved by load order)"
        self._transition(
            generation,
            phase,
            message,
            last_error=status.last_error,
            process_id=status.process_id,
        )

    def _stop_patcher(self, patcher: PatcherBoundary, deadline: float) -> bool:
        remaining = deadline - monotonic()
        if remaining <= 0:
            return False
        try:
            stopped = patcher.stop(remaining)
            if stopped:
                patcher.close()
            return stopped
        except Exception:
            self._logger.exception("Could not stop the LTK patcher runtime")
            return False

    def _has_active_work_locked(self) -> bool:
        return (
            (self._worker is not None and self._worker.is_alive())
            or self._patcher is not None
            or self._status.active
        )

    @staticmethod
    def _check_cancelled(cancel: Event) -> None:
        if cancel.is_set():
            raise _Cancelled("Runtime start was cancelled")

    def _fail(self, generation: int, message: str) -> None:
        self._transition(
            generation,
            RuntimePhase.FAILED,
            message,
            last_error=message,
            process_id=None,
        )

    def _transition(
        self,
        generation: int,
        phase: RuntimePhase,
        message: str,
        *,
        last_error: str | None | _Keep = _KEEP,
        enabled_mod_count: int | _Keep = _KEEP,
        overlay_root: Path | None | _Keep = _KEEP,
        provider_version: str | None | _Keep = _KEEP,
        process_id: int | None | _Keep = _KEEP,
        conflict_count: int | _Keep = _KEEP,
        linked_bin_offender_count: int | _Keep = _KEEP,
        owns_resources: bool | _Keep = _KEEP,
        clear_last_error: bool = False,
    ) -> RuntimeStatus | None:
        with self._lock:
            if generation != self._generation:
                return None
            current = self._status
            error_value = (
                None
                if clear_last_error
                else current.last_error
                if isinstance(last_error, _Keep)
                else last_error
            )
            updated = RuntimeStatus(
                phase=phase,
                message=message,
                last_error=error_value,
                enabled_mod_count=(
                    current.enabled_mod_count
                    if isinstance(enabled_mod_count, _Keep)
                    else enabled_mod_count
                ),
                overlay_root=(
                    current.overlay_root if isinstance(overlay_root, _Keep) else overlay_root
                ),
                provider_version=(
                    current.provider_version
                    if isinstance(provider_version, _Keep)
                    else provider_version
                ),
                process_id=(current.process_id if isinstance(process_id, _Keep) else process_id),
                conflict_count=(
                    current.conflict_count if isinstance(conflict_count, _Keep) else conflict_count
                ),
                linked_bin_offender_count=(
                    current.linked_bin_offender_count
                    if isinstance(linked_bin_offender_count, _Keep)
                    else linked_bin_offender_count
                ),
                owns_resources=(
                    current.owns_resources if isinstance(owns_resources, _Keep) else owns_resources
                ),
            )
            if updated == current:
                return updated
            self._status = updated
        self._emit(updated)
        return updated

    def _release_ownership(self, generation: int) -> None:
        """Publish when a failed/idle worker no longer owns a host or build task."""

        with self._lock:
            if generation != self._generation:
                return
            owns_resources = (
                self._worker is not None and self._worker.is_alive()
            ) or self._patcher is not None
            if owns_resources or not self._status.owns_resources:
                return
            updated = replace(self._status, owns_resources=False)
            self._status = updated
        self._emit(updated)

    def _emit(self, status: RuntimeStatus) -> None:
        sink = self._status_sink
        if sink is None:
            return
        with self._callback_lock:
            try:
                sink(status)
            except Exception:
                self._logger.exception("LTK runtime status callback failed")


def _coordinator_phase(phase: PatcherPhase) -> RuntimePhase:
    if phase in {PatcherPhase.STARTING, PatcherPhase.SCANNING}:
        return RuntimePhase.SCANNING
    if phase in {PatcherPhase.INJECTING, PatcherPhase.INJECTED, PatcherPhase.WAITING}:
        return RuntimePhase.RUNNING
    if phase is PatcherPhase.STOPPING:
        return RuntimePhase.STOPPING
    if phase is PatcherPhase.FAILED:
        return RuntimePhase.FAILED
    return RuntimePhase.IDLE


def _paths_overlap(first: Path, second: Path) -> bool:
    try:
        first.relative_to(second)
        return True
    except ValueError:
        pass
    try:
        second.relative_to(first)
        return True
    except ValueError:
        return False


LTKRuntimeCoordinator = RuntimeCoordinator

__all__ = [
    "EngineBoundary",
    "InstallationLocatorBoundary",
    "InstallationInUse",
    "LTKRuntimeCoordinator",
    "PatcherBoundary",
    "PatcherFactory",
    "RuntimeCoordinator",
    "RuntimeCoordinatorError",
    "RuntimePhase",
    "RuntimeStatus",
    "StatusSink",
]
