from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from threading import Event, Lock, Thread
from time import monotonic, sleep
from typing import Any

import pytest

from league_skin_manager.atomic import atomic_write_json
from league_skin_manager.ltk_engine import LTKInstallation
from league_skin_manager.ltk_patcher import (
    EventSink,
    HostEvent,
    HostEventKind,
    HostState,
    PatcherPhase,
    PatcherStatus,
)
from league_skin_manager.mod_library import LocalModLibrary, ModRecord
from league_skin_manager.profile import ProfileStore
from league_skin_manager.runtime import (
    EngineBoundary,
    PatcherBoundary,
    RuntimeCoordinator,
    RuntimeCoordinatorError,
    RuntimePhase,
    RuntimeStatus,
)

_PACKAGE_BYTES: dict[str, bytes] = {}


def record(digit: str, name: str = "Test Mod") -> ModRecord:
    payload = f"package:{digit}:{name}".encode()
    content_id = hashlib.sha256(payload).hexdigest()
    _PACKAGE_BYTES[content_id] = payload
    return ModRecord(
        id=content_id,
        name=name,
        author="Author",
        version="1.0",
        format="fantome",
        champions=("Ahri",),
        tags=("visual",),
        file_name=f"{content_id}.fantome",
        size=len(payload),
        content_sha256=content_id,
    )


def services(
    tmp_path: Path,
    records: tuple[ModRecord, ...],
    *,
    enabled_ids: tuple[str, ...] | None = None,
    configure_game: bool = True,
) -> tuple[LocalModLibrary, ProfileStore, Path | None]:
    root = tmp_path / "library"
    package_dir = root / "packages"
    package_dir.mkdir(parents=True)
    library = LocalModLibrary(package_dir, root / "library.json")
    atomic_write_json(
        library.manifest_file,
        {"schema_version": 1, "entries": [item.to_json() for item in records]},
    )
    for item in records:
        (package_dir / item.file_name).write_bytes(_PACKAGE_BYTES[item.id])

    profile = ProfileStore(tmp_path / "profile.json", library)
    if enabled_ids is None:
        enabled_ids = tuple(item.id for item in records)
    profile.set_enabled(enabled_ids)
    game: Path | None = None
    if configure_game:
        game = tmp_path / "League of Legends" / "Game"
        (game / "DATA" / "FINAL").mkdir(parents=True)
        game = profile.set_game_dir(game)
    return library, profile, game


def installation(tmp_path: Path, *, new_provider: bool = True) -> LTKInstallation:
    root = tmp_path / "LTK Manager"
    resources = root / "resources"
    resources.mkdir(parents=True, exist_ok=True)
    manager = root / "ltk-manager.exe"
    manager.write_bytes(b"MZmanager")
    if not new_provider:
        return LTKInstallation(root, manager, "1.11.0", None, None)
    host = resources / "ltk_patcher_host.exe"
    hook = resources / "ltk_patcher_dll.dll"
    host.write_bytes(b"MZhost")
    hook.write_bytes(b"MZhook")
    return LTKInstallation(root, manager, "1.12.0", host, hook)


def valid_hello() -> dict[str, Any]:
    return {
        "engine_name": "ltk-engine",
        "protocol_version": 1,
        "methods": ["engine.hello", "package.inspect", "overlay.build", "provider.smoke"],
        "provider": {
            "mode": "configuration_only",
            "anti_hack_enforced": True,
            "binaries_bundled": False,
            "downloads_binaries": False,
            "starts_processes": False,
        },
    }


class FakeEngine:
    def __init__(
        self,
        *,
        build_gate: Event | None = None,
        returned_root: Callable[[Path], Path] | None = None,
        conflict_count: int = 0,
        linked_bin_offender_count: int = 0,
    ) -> None:
        self.build_gate = build_gate
        self.returned_root = returned_root or (lambda root: root / "built")
        self.build_entered = Event()
        self.cancel_calls = 0
        self.conflict_count = conflict_count
        self.linked_bin_offender_count = linked_bin_offender_count
        self.hello_result = valid_hello()
        self.build_error: Exception | None = None
        self.build_calls: list[tuple[Path, Path, Path, tuple[Path, ...]]] = []

    def hello(self) -> Mapping[str, Any]:
        return self.hello_result

    def build_overlay(
        self,
        *,
        game_dir: Path,
        overlay_dir: Path,
        state_dir: Path,
        enabled_packages: Sequence[Path],
        cancelled: Callable[[], bool] | None = None,
    ) -> Mapping[str, Any]:
        self.build_calls.append((game_dir, overlay_dir, state_dir, tuple(enabled_packages)))
        self.build_entered.set()
        if self.build_gate is not None:
            self.build_gate.wait(2.0)
        if self.build_error is not None:
            raise self.build_error
        root = self.returned_root(overlay_dir)
        root.mkdir(parents=True, exist_ok=True)
        return {
            "overlay_root": str(root),
            "enabled_package_count": len(enabled_packages),
            "conflict_count": self.conflict_count,
            "linked_bin_offender_count": self.linked_bin_offender_count,
        }

    def cancel_active(self) -> bool:
        self.cancel_calls += 1
        return False

    def provider_smoke(
        self,
        *,
        installation_dir: Path,
        overlay_prefix: Path,
        event_lines: Sequence[str] = (),
    ) -> Mapping[str, Any]:
        return {
            "available": True,
            "configuration_only": True,
            "process_started": False,
            "anti_hack_enforced": True,
            "installation_dir": str(installation_dir),
            "overlay_prefix": str(overlay_prefix),
            "event_lines": list(event_lines),
        }


class FakeLocator:
    def __init__(self, result: LTKInstallation | None) -> None:
        self.result = result
        self.calls = 0
        self.revalidate_result = True
        self.revalidate_calls: list[LTKInstallation] = []

    def discover(self) -> LTKInstallation | None:
        self.calls += 1
        return self.result

    def revalidate(self, installation: LTKInstallation) -> bool:
        self.revalidate_calls.append(installation)
        return self.revalidate_result


class FakePatcher:
    def __init__(self, event_sink: EventSink) -> None:
        self._event_sink = event_sink
        self._lock = Lock()
        self._status = PatcherStatus(
            PatcherPhase.IDLE,
            "Patcher is stopped",
            None,
            None,
            False,
            None,
        )
        self.start_paths: list[Path] = []
        self.stop_timeouts: list[float | None] = []
        self.close_calls = 0
        self.start_error: Exception | None = None
        self.stop_result = True

    def status(self) -> PatcherStatus:
        with self._lock:
            return self._status

    def start(self, overlay_prefix: Path) -> PatcherStatus:
        self.start_paths.append(overlay_prefix)
        if self.start_error is not None:
            raise self.start_error
        with self._lock:
            self._status = PatcherStatus(
                PatcherPhase.INJECTED,
                "Provider is running",
                None,
                4242,
                True,
                None,
            )
            return self._status

    def emit(self, message: str, phase: PatcherPhase = PatcherPhase.INJECTED) -> None:
        event = HostEvent(
            HostEventKind.STATUS,
            "1.0",
            message,
            state=HostState.INJECTED,
        )
        with self._lock:
            self._status = PatcherStatus(phase, message, None, 4242, True, event)
        self._event_sink(event)

    def stop(self, timeout_seconds: float | None = None) -> bool:
        self.stop_timeouts.append(timeout_seconds)
        if self.stop_result:
            with self._lock:
                self._status = PatcherStatus(
                    PatcherPhase.IDLE,
                    "Patcher is stopped",
                    None,
                    None,
                    False,
                    None,
                )
        return self.stop_result

    def close(self) -> None:
        self.close_calls += 1


class RecordingFactory:
    def __init__(self) -> None:
        self.calls: list[tuple[EngineBoundary, LTKInstallation]] = []
        self.patchers: list[FakePatcher] = []
        self.start_error: Exception | None = None

    def __call__(
        self,
        engine: EngineBoundary,
        installation: LTKInstallation,
        event_sink: EventSink,
    ) -> PatcherBoundary:
        self.calls.append((engine, installation))
        patcher = FakePatcher(event_sink)
        patcher.start_error = self.start_error
        self.patchers.append(patcher)
        return patcher


def wait_for(predicate: Callable[[], bool], timeout: float = 1.5) -> None:
    deadline = monotonic() + timeout
    while not predicate():
        if monotonic() >= deadline:
            raise AssertionError("condition was not reached")
        sleep(0.005)


def coordinator(
    tmp_path: Path,
    library: LocalModLibrary,
    profile: ProfileStore,
    engine: EngineBoundary | None,
    found: LTKInstallation | None,
    *,
    factory: RecordingFactory | None = None,
    sink: Callable[[RuntimeStatus], None] | None = None,
    installation_in_use: Callable[[Path], bool] | None = None,
    revalidate_result: bool = True,
) -> tuple[RuntimeCoordinator, RecordingFactory]:
    runtime_factory = factory or RecordingFactory()
    locator = FakeLocator(found)
    locator.revalidate_result = revalidate_result
    service = RuntimeCoordinator(
        profile,
        library,
        engine,
        locator,
        overlay_dir=tmp_path / "owned" / "overlay",
        state_dir=tmp_path / "owned" / "state",
        patcher_factory=runtime_factory,
        installation_in_use=installation_in_use,
        status_sink=sink,
        shutdown_timeout_seconds=0.2,
        monitor_interval_seconds=0.01,
    )
    return service, runtime_factory


def test_start_is_nonblocking_reconciles_profile_builds_and_runs(tmp_path: Path) -> None:
    available = record("a", "Available")
    missing_id = hashlib.sha256(b"missing").hexdigest()
    library, profile, game = services(tmp_path, (available,))
    assert game is not None
    atomic_write_json(
        profile.settings_file,
        {
            "schema_version": 1,
            "enabled_mod_ids": [available.id, missing_id],
            "game_dir": str(game),
        },
    )
    gate = Event()
    engine = FakeEngine(build_gate=gate)
    factory = RecordingFactory()
    received: list[RuntimeStatus] = []
    runtime, _factory = coordinator(
        tmp_path,
        library,
        profile,
        engine,
        installation(tmp_path),
        factory=factory,
        sink=received.append,
    )
    try:
        initial = runtime.start()
        assert initial.phase is RuntimePhase.BUILDING
        assert engine.build_entered.wait(1.0)
        assert runtime.status().phase is RuntimePhase.BUILDING
        with pytest.raises(RuntimeCoordinatorError, match="already"):
            runtime.start()

        gate.set()
        wait_for(lambda: runtime.status().phase is RuntimePhase.RUNNING)
        status = runtime.status()
        assert status.enabled_mod_count == 1
        assert status.provider_version == "1.12.0"
        assert status.process_id == 4242
        assert status.overlay_root == (runtime.overlay_dir / "built").resolve()
        assert [item.phase for item in received][0] is RuntimePhase.BUILDING
        assert RuntimePhase.SCANNING in {item.phase for item in received}
        assert received[-1].phase is RuntimePhase.RUNNING
        assert engine.build_calls == [
            (
                game,
                runtime.overlay_dir,
                runtime.state_dir,
                ((library.package_dir / available.file_name).resolve(),),
            )
        ]
        assert len(factory.calls) == 1
        called_engine, called_installation = factory.calls[0]
        assert called_engine is engine
        assert called_installation.root.name == "LTK Manager"
        raw_profile = json.loads(profile.settings_file.read_text(encoding="utf-8"))
        assert raw_profile["enabled_mod_ids"] == [available.id]
    finally:
        gate.set()
        runtime.close()


@pytest.mark.parametrize(
    ("case", "message"),
    (
        ("engine", "sidecar"),
        ("mods", "Enable at least one"),
        ("game", "game directory"),
        ("provider", "verified official"),
        ("old_provider", "predates the new patcher"),
    ),
)
def test_missing_prerequisites_are_rejected_asynchronously(
    tmp_path: Path, case: str, message: str
) -> None:
    item = record("c")
    records = () if case == "mods" else (item,)
    library, profile, _game = services(
        tmp_path,
        records,
        configure_game=case != "game",
    )
    engine: EngineBoundary | None = None if case == "engine" else FakeEngine()
    if case == "provider":
        found = None
    elif case == "old_provider":
        found = installation(tmp_path, new_provider=False)
    else:
        found = installation(tmp_path)
    runtime, factory = coordinator(tmp_path, library, profile, engine, found)
    try:
        assert runtime.start().phase is RuntimePhase.BUILDING
        wait_for(lambda: runtime.status().phase is RuntimePhase.FAILED)
        assert message in (runtime.status().last_error or "")
        assert not factory.calls
    finally:
        runtime.close()


def test_incompatible_engine_handshake_and_missing_package_fail_before_build(
    tmp_path: Path,
) -> None:
    item = record("d")
    library, profile, _game = services(tmp_path, (item,))
    engine = FakeEngine()
    engine.hello_result["protocol_version"] = 99
    runtime, _factory = coordinator(tmp_path, library, profile, engine, installation(tmp_path))
    runtime.start()
    wait_for(lambda: runtime.status().phase is RuntimePhase.FAILED)
    assert "handshake" in (runtime.status().last_error or "")
    assert not engine.build_calls
    runtime.close()

    engine.hello_result = valid_hello()
    (library.package_dir / item.file_name).unlink()
    runtime, _factory = coordinator(tmp_path, library, profile, engine, installation(tmp_path))
    runtime.start()
    wait_for(lambda: runtime.status().phase is RuntimePhase.FAILED)
    assert "Enable at least one" in (runtime.status().last_error or "")
    assert not engine.build_calls
    runtime.close()


def test_running_official_manager_blocks_overlay_and_provider_start(tmp_path: Path) -> None:
    item = record("manager-running")
    library, profile, _game = services(tmp_path, (item,))
    checked: list[Path] = []

    def in_use(root: Path) -> bool:
        checked.append(root)
        return True

    engine = FakeEngine()
    runtime, factory = coordinator(
        tmp_path,
        library,
        profile,
        engine,
        installation(tmp_path),
        installation_in_use=in_use,
    )
    try:
        runtime.start()
        wait_for(lambda: runtime.status().phase is RuntimePhase.FAILED)
        assert "Close LTK Manager" in (runtime.status().last_error or "")
        assert checked == [installation(tmp_path).root]
        assert not engine.build_calls
        assert not factory.calls
    finally:
        runtime.close()


def test_same_size_package_tamper_is_rehashed_and_never_reaches_engine(
    tmp_path: Path,
) -> None:
    item = record("tamper")
    library, profile, _game = services(tmp_path, (item,))
    package = library.package_dir / item.file_name
    original = package.read_bytes()
    package.write_bytes(bytes(byte ^ 0xFF for byte in original))
    assert package.stat().st_size == item.size
    engine = FakeEngine()
    runtime, factory = coordinator(tmp_path, library, profile, engine, installation(tmp_path))
    try:
        runtime.start()
        wait_for(lambda: runtime.status().phase is RuntimePhase.FAILED)
        assert "Enable at least one" in (runtime.status().last_error or "")
        assert not engine.build_calls
        assert not factory.calls
        assert library.records() == ()
        assert profile.enabled_records() == ()
    finally:
        runtime.close()


def test_overlay_result_must_remain_under_owned_directory(tmp_path: Path) -> None:
    item = record("e")
    library, profile, _game = services(tmp_path, (item,))
    engine = FakeEngine(returned_root=lambda _owned: tmp_path / "outside")
    runtime, factory = coordinator(tmp_path, library, profile, engine, installation(tmp_path))
    try:
        runtime.start()
        wait_for(lambda: runtime.status().phase is RuntimePhase.FAILED)
        assert "outside" in (runtime.status().last_error or "")
        assert not factory.calls
    finally:
        runtime.close()


def test_linked_bin_diagnostics_block_provider_activation(tmp_path: Path) -> None:
    item = record("linked-bin")
    library, profile, _game = services(tmp_path, (item,))
    engine = FakeEngine(linked_bin_offender_count=2)
    runtime, factory = coordinator(tmp_path, library, profile, engine, installation(tmp_path))
    try:
        runtime.start()
        wait_for(lambda: runtime.status().phase is RuntimePhase.FAILED)
        assert "2 linked-BIN offender" in (runtime.status().last_error or "")
        assert runtime.status().linked_bin_offender_count == 2
        assert not factory.calls
    finally:
        runtime.close()


def test_provider_snapshot_is_revalidated_after_overlay_build(tmp_path: Path) -> None:
    item = record("provider-swap")
    library, profile, _game = services(tmp_path, (item,))
    engine = FakeEngine()
    runtime, factory = coordinator(
        tmp_path,
        library,
        profile,
        engine,
        installation(tmp_path),
        revalidate_result=False,
    )
    try:
        runtime.start()
        wait_for(lambda: runtime.status().phase is RuntimePhase.FAILED)
        assert "signature revalidation" in (runtime.status().last_error or "")
        assert engine.build_calls
        assert not factory.calls
    finally:
        runtime.close()


def test_resolved_conflicts_are_preserved_in_runtime_status(tmp_path: Path) -> None:
    item = record("conflicts")
    library, profile, _game = services(tmp_path, (item,))
    runtime, _factory = coordinator(
        tmp_path,
        library,
        profile,
        FakeEngine(conflict_count=3),
        installation(tmp_path),
    )
    try:
        runtime.start()
        wait_for(lambda: runtime.status().phase is RuntimePhase.RUNNING)
        assert runtime.status().conflict_count == 3
        assert "3 package conflict" in runtime.status().message
    finally:
        runtime.close()


def test_stop_and_close_are_bounded_and_stop_the_external_runtime(tmp_path: Path) -> None:
    item = record("f")
    library, profile, _game = services(tmp_path, (item,))
    runtime, factory = coordinator(tmp_path, library, profile, FakeEngine(), installation(tmp_path))
    runtime.start()
    wait_for(lambda: runtime.status().phase is RuntimePhase.RUNNING)
    patcher = factory.patchers[0]

    assert runtime.stop(0.5) is True
    assert runtime.status() == RuntimeStatus(RuntimePhase.IDLE, "Runtime is stopped")
    assert patcher.stop_timeouts
    assert patcher.close_calls >= 1
    assert runtime.close(0.5) is True
    with pytest.raises(RuntimeCoordinatorError, match="closed"):
        runtime.start()


def test_stop_during_blocked_build_returns_at_deadline_and_never_starts_host(
    tmp_path: Path,
) -> None:
    item = record("1")
    library, profile, _game = services(tmp_path, (item,))
    gate = Event()
    engine = FakeEngine(build_gate=gate)
    runtime, factory = coordinator(tmp_path, library, profile, engine, installation(tmp_path))
    runtime.start()
    assert engine.build_entered.wait(1.0)

    started = monotonic()
    assert runtime.stop(0.02) is False
    assert monotonic() - started < 0.2
    assert engine.cancel_calls == 1
    assert runtime.status().phase is RuntimePhase.FAILED
    assert runtime.status().owns_resources is True
    assert runtime.status().active is True
    gate.set()
    wait_for(lambda: not runtime.status().active)
    assert runtime.status().phase is RuntimePhase.FAILED
    assert runtime.status().owns_resources is False
    sleep(0.03)
    assert not factory.calls
    runtime.close()


def test_patcher_start_failure_is_reported_and_cleaned_up(tmp_path: Path) -> None:
    item = record("2")
    library, profile, _game = services(tmp_path, (item,))
    factory = RecordingFactory()
    factory.start_error = RuntimeError("provider start exploded")
    runtime, _factory = coordinator(
        tmp_path,
        library,
        profile,
        FakeEngine(),
        installation(tmp_path),
        factory=factory,
    )
    try:
        runtime.start()
        wait_for(lambda: runtime.status().phase is RuntimePhase.FAILED)
        assert "provider start exploded" in (runtime.status().last_error or "")
        wait_for(lambda: bool(factory.patchers[0].stop_timeouts))
    finally:
        runtime.close()


def test_status_callbacks_are_serialized_across_provider_threads(tmp_path: Path) -> None:
    item = record("3")
    library, profile, _game = services(tmp_path, (item,))
    callback_lock = Lock()
    active_callbacks = 0
    maximum_callbacks = 0

    def sink(_status: RuntimeStatus) -> None:
        nonlocal active_callbacks, maximum_callbacks
        with callback_lock:
            active_callbacks += 1
            maximum_callbacks = max(maximum_callbacks, active_callbacks)
        sleep(0.002)
        with callback_lock:
            active_callbacks -= 1

    runtime, factory = coordinator(
        tmp_path,
        library,
        profile,
        FakeEngine(),
        installation(tmp_path),
        sink=sink,
    )
    try:
        runtime.start()
        wait_for(lambda: runtime.status().phase is RuntimePhase.RUNNING)
        patcher = factory.patchers[0]
        threads = [Thread(target=patcher.emit, args=(f"event-{index}",)) for index in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        assert maximum_callbacks == 1
    finally:
        runtime.close()


def test_constructor_and_timeouts_validate_safety_bounds(tmp_path: Path) -> None:
    item = record("4")
    library, profile, _game = services(tmp_path, (item,))
    found = installation(tmp_path)
    with pytest.raises(ValueError, match="shutdown_timeout"):
        RuntimeCoordinator(
            profile,
            library,
            FakeEngine(),
            FakeLocator(found),
            overlay_dir=tmp_path / "overlay",
            state_dir=tmp_path / "state",
            shutdown_timeout_seconds=0,
        )
    with pytest.raises(ValueError, match="must not overlap"):
        RuntimeCoordinator(
            profile,
            library,
            FakeEngine(),
            FakeLocator(found),
            overlay_dir=tmp_path / "owned",
            state_dir=tmp_path / "owned" / "state",
        )
    runtime, _factory = coordinator(tmp_path, library, profile, FakeEngine(), found)
    with pytest.raises(ValueError, match="positive"):
        runtime.stop(0)
    runtime.close()
