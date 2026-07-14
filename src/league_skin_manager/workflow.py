"""Background reconciliation workflow for the local LTK mod library."""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterable, Mapping
from pathlib import Path
from threading import Event, Lock
from typing import Any

from .controller import AppState, SyncOutcome
from .ltk_engine import LTKInstallation
from .mod_library import ImportResult, LocalModLibrary

ProviderLookup = Callable[[], LTKInstallation | None]
EngineHealthCheck = Callable[[], Mapping[str, Any]]


class LibraryWorkflow:
    """Serialize import requests through the controller's existing worker boundary."""

    def __init__(
        self,
        *,
        library: LocalModLibrary,
        provider_lookup: ProviderLookup,
        engine_health: EngineHealthCheck | None,
        logger: logging.Logger,
    ) -> None:
        self.library = library
        self.provider_lookup = provider_lookup
        self.engine_health = engine_health
        self.logger = logger
        self._pending_lock = Lock()
        self._pending_import: tuple[Path, ...] = ()

    @property
    def import_pending(self) -> bool:
        with self._pending_lock:
            return bool(self._pending_import)

    def queue_import(self, paths: Iterable[Path]) -> bool:
        values = tuple(Path(path) for path in paths)
        if not values:
            return False
        with self._pending_lock:
            if self._pending_import:
                return False
            self._pending_import = values
        return True

    def cancel_pending_import(self) -> None:
        with self._pending_lock:
            self._pending_import = ()

    def __call__(self, cancel_event: Event) -> SyncOutcome:
        if cancel_event.is_set():
            return SyncOutcome(AppState.OFFLINE_READY, "Stopping library refresh")
        with self._pending_lock:
            pending = self._pending_import
            self._pending_import = ()

        imported: ImportResult | None = None
        if pending:
            imported = self.library.import_packages(pending, cancelled=cancel_event.is_set)
        if cancel_event.is_set():
            return SyncOutcome(AppState.OFFLINE_READY, "Stopping library refresh")
        records = self.library.refresh()

        provider = self._provider()
        engine_ready = self._engine_ready()
        detail = f"Ready - {len(records):,} local mod{'s' if len(records) != 1 else ''}"
        if imported is not None:
            detail += f"; imported {len(imported.imported)}"
            if imported.duplicate_count:
                detail += f", skipped {imported.duplicate_count} duplicate"
        if not engine_ready:
            detail += "; build ltk-engine for .modpkg and overlays"
        if provider is None:
            detail += "; install official LTK Manager to run mods"
        elif not provider.injection_provider_available:
            detail += "; installed LTK release has no new patcher provider"

        state = (
            AppState.READY
            if engine_ready and provider is not None and provider.injection_provider_available
            else AppState.OFFLINE_READY
        )
        return SyncOutcome(state, detail)

    def _provider(self) -> LTKInstallation | None:
        try:
            return self.provider_lookup()
        except Exception:
            self.logger.exception("LTK Manager discovery failed")
            return None

    def _engine_ready(self) -> bool:
        if self.engine_health is None:
            return False
        try:
            result = self.engine_health()
        except Exception:
            self.logger.exception("LTK engine health check failed")
            return False
        return bool(result)


__all__ = ["EngineHealthCheck", "LibraryWorkflow", "ProviderLookup"]
