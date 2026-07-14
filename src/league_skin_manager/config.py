"""Side-effect-free application configuration and path discovery."""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from pathlib import Path

APP_NAME = "LeagueSkinManagerLTK"
APP_DISPLAY_NAME = "League Skin Manager LTK"
APP_VERSION = "0.1.0"
APP_PUBLISHER = "Quinntana"
APP_INFO_URL = "https://github.com/Quinntana/LeagueSkinManagerLTK"
UNINSTALL_APP_NAME = "LeagueSkinManagerLTKUninstall"
SETUP_APP_NAME = "LeagueSkinManagerLTKSetup"
MANAGER_PROCESS_NAME = "ltk-manager.exe"
MANAGER_PROCESS_NAMES = (MANAGER_PROCESS_NAME, "LTK Manager.exe", "ltk_patcher_host.exe")
LEAGUE_PROCESS_NAME = "LeagueClient.exe"

LTK_RELEASES_URL = "https://github.com/LeagueToolkit/ltk-manager/releases/latest"
ENGINE_PROTOCOL_VERSION = 1


@dataclass(frozen=True, slots=True)
class AppPaths:
    project_root: Path
    data_dir: Path
    library_dir: Path
    package_dir: Path
    profile_dir: Path
    engine_state_dir: Path
    overlay_dir: Path
    cache_dir: Path
    log_dir: Path
    library_manifest_file: Path
    default_profile_file: Path

    @classmethod
    def discover(
        cls,
        *,
        appdata: str | Path | None = None,
        local_appdata: str | Path | None = None,
        project_root: str | Path | None = None,
    ) -> AppPaths:
        if project_root is None:
            if getattr(sys, "frozen", False):
                root = Path(sys.executable).resolve().parent
            else:
                root = Path(__file__).resolve().parents[2]
        else:
            root = Path(project_root).resolve()

        # Packages, overlays, cache, and logs are machine-local and can be very
        # large. Never place them in the roaming APPDATA profile. ``appdata`` is
        # retained only as a compatibility fallback for callers on older setups.
        local_value = (
            local_appdata
            if local_appdata is not None
            else os.environ.get("LOCALAPPDATA") or appdata
        )
        data_dir = Path(local_value).resolve() / APP_NAME if local_value else root / "data"
        library_dir = data_dir / "library"
        cache_dir = data_dir / "cache"
        return cls(
            project_root=root,
            data_dir=data_dir,
            library_dir=library_dir,
            package_dir=library_dir / "packages",
            profile_dir=data_dir / "profiles",
            engine_state_dir=data_dir / "engine-state",
            overlay_dir=data_dir / "overlay",
            cache_dir=cache_dir,
            log_dir=data_dir / "logs",
            library_manifest_file=library_dir / "library.json",
            default_profile_file=data_dir / "profiles" / "default.json",
        )

    def ensure(self) -> None:
        for directory in (
            self.data_dir,
            self.library_dir,
            self.package_dir,
            self.profile_dir,
            self.engine_state_dir,
            self.overlay_dir,
            self.cache_dir,
            self.log_dir,
        ):
            directory.mkdir(parents=True, exist_ok=True)


@dataclass(frozen=True, slots=True)
class RuntimeConfig:
    process_poll_seconds: float = 5.0
    engine_timeout_seconds: float = 30.0
    overlay_timeout_seconds: float = 15.0 * 60.0
    shutdown_timeout_seconds: float = 15.0

    def __post_init__(self) -> None:
        if self.process_poll_seconds <= 0:
            raise ValueError("process_poll_seconds must be positive")
        if self.engine_timeout_seconds <= 0:
            raise ValueError("engine_timeout_seconds must be positive")
        if self.overlay_timeout_seconds <= 0:
            raise ValueError("overlay_timeout_seconds must be positive")
        if self.shutdown_timeout_seconds <= 0:
            raise ValueError("shutdown_timeout_seconds must be positive")
