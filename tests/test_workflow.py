from __future__ import annotations

import logging
from pathlib import Path
from threading import Event
from typing import cast

from league_skin_manager.controller import AppState
from league_skin_manager.ltk_engine import LTKInstallation
from league_skin_manager.mod_library import ImportResult, LocalModLibrary, ModRecord
from league_skin_manager.workflow import LibraryWorkflow


def mod() -> ModRecord:
    digest = "a" * 64
    return ModRecord(
        id=digest,
        name="Original Ahri Remix",
        author="Creator",
        version="1",
        format="fantome",
        champions=("Ahri",),
        tags=(),
        file_name=f"{digest}.fantome",
        size=12,
        content_sha256=digest,
    )


class FakeLibrary:
    def __init__(self) -> None:
        self.imported: list[tuple[Path, ...]] = []
        self.refreshes = 0

    def import_packages(
        self, paths: tuple[Path, ...], *, cancelled: object | None = None
    ) -> ImportResult:
        del cancelled
        self.imported.append(paths)
        return ImportResult((mod(),), 1)

    def refresh(self) -> tuple[ModRecord, ...]:
        self.refreshes += 1
        return (mod(),)


def provider(tmp_path: Path, *, patcher: bool = True) -> LTKInstallation:
    root = tmp_path / "LTK Manager"
    return LTKInstallation(
        root=root,
        manager_executable=root / "ltk-manager.exe",
        version="1.12.0",
        host_executable=root / "ltk_patcher_host.exe" if patcher else None,
        hook_dll=root / "ltk_patcher_dll.dll" if patcher else None,
    )


def workflow(
    library: FakeLibrary,
    *,
    provider_value: LTKInstallation | None,
    engine: bool,
) -> LibraryWorkflow:
    return LibraryWorkflow(
        library=cast(LocalModLibrary, library),
        provider_lookup=lambda: provider_value,
        engine_health=(lambda: {"name": "ltk-engine"}) if engine else None,
        logger=logging.getLogger("test.workflow"),
    )


def test_refresh_reports_full_ready_state(tmp_path: Path) -> None:
    library = FakeLibrary()
    action = workflow(library, provider_value=provider(tmp_path), engine=True)

    outcome = action(Event())

    assert outcome.state is AppState.READY
    assert outcome.detail == "Ready - 1 local mod"
    assert library.refreshes == 1


def test_import_queue_is_single_slot_and_reports_duplicates(tmp_path: Path) -> None:
    library = FakeLibrary()
    action = workflow(library, provider_value=provider(tmp_path), engine=True)
    paths = (tmp_path / "one.fantome",)

    assert action.queue_import(paths) is True
    assert action.import_pending is True
    assert action.queue_import((tmp_path / "two.fantome",)) is False
    outcome = action(Event())

    assert library.imported == [paths]
    assert action.import_pending is False
    assert outcome.detail == "Ready - 1 local mod; imported 1, skipped 1 duplicate"


def test_cancel_and_empty_queue_are_safe(tmp_path: Path) -> None:
    action = workflow(FakeLibrary(), provider_value=None, engine=False)

    assert action.queue_import(()) is False
    assert action.queue_import((tmp_path / "one.fantome",)) is True
    action.cancel_pending_import()

    assert action.import_pending is False


def test_missing_components_are_explicit_offline_status(tmp_path: Path) -> None:
    action = workflow(FakeLibrary(), provider_value=None, engine=False)

    outcome = action(Event())

    assert outcome.state is AppState.OFFLINE_READY
    assert "build ltk-engine" in outcome.detail
    assert "install official LTK Manager" in outcome.detail


def test_stable_provider_is_not_mislabeled_as_new_patcher(tmp_path: Path) -> None:
    action = workflow(FakeLibrary(), provider_value=provider(tmp_path, patcher=False), engine=True)

    outcome = action(Event())

    assert outcome.state is AppState.OFFLINE_READY
    assert "installed LTK release has no new patcher provider" in outcome.detail


def test_cancelled_and_failed_health_checks_fail_safe(tmp_path: Path) -> None:
    library = FakeLibrary()
    action = LibraryWorkflow(
        library=cast(LocalModLibrary, library),
        provider_lookup=lambda: (_ for _ in ()).throw(OSError("registry")),
        engine_health=lambda: (_ for _ in ()).throw(RuntimeError("engine")),
        logger=logging.getLogger("test.workflow.failure"),
    )
    cancelled = Event()
    cancelled.set()

    assert action(cancelled).detail == "Stopping library refresh"
    outcome = action(Event())
    assert outcome.state is AppState.OFFLINE_READY
    assert "install official LTK Manager" in outcome.detail
