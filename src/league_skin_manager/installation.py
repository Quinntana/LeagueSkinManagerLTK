"""Per-user install layout and Windows Apps & Features registration."""

from __future__ import annotations

import base64
import os
import stat
import subprocess
from contextlib import suppress
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any

from .config import (
    APP_DISPLAY_NAME,
    APP_INFO_URL,
    APP_NAME,
    APP_PUBLISHER,
    APP_VERSION,
    UNINSTALL_APP_NAME,
)

UNINSTALL_REGISTRY_PARENT = r"Software\Microsoft\Windows\CurrentVersion\Uninstall"
UNINSTALL_REGISTRY_KEY = rf"{UNINSTALL_REGISTRY_PARENT}\{APP_NAME}"
INSTALL_OPERATION_MUTEX_NAME = rf"Local\{APP_NAME}_InstallOperation_v1"
START_MENU_SHORTCUT_NAME = f"{APP_DISPLAY_NAME}.lnk"


class InstallationError(RuntimeError):
    """The per-user install layout or registration is unsafe or unavailable."""


def _is_reparse_point(path: Path) -> bool:
    """Return whether an existing path redirects filesystem traversal."""

    if path.is_symlink():
        return True
    try:
        attributes = path.lstat().st_file_attributes
    except (AttributeError, FileNotFoundError, OSError):
        return False
    return bool(attributes & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0))


@dataclass(frozen=True, slots=True)
class InstallLayout:
    local_appdata: Path
    install_dir: Path
    executable: Path
    uninstaller: Path

    @classmethod
    def discover(cls, local_appdata: str | Path | None = None) -> InstallLayout:
        raw = local_appdata if local_appdata is not None else os.environ.get("LOCALAPPDATA")
        if not raw:
            raise InstallationError("LOCALAPPDATA is unavailable")
        root = Path(raw).resolve()
        install_dir = root / "Programs" / APP_NAME
        return cls(
            local_appdata=root,
            install_dir=install_dir,
            executable=install_dir / f"{APP_NAME}.exe",
            uninstaller=install_dir / f"{UNINSTALL_APP_NAME}.exe",
        )

    def validated_install_dir(self) -> Path:
        root = self.local_appdata.resolve()
        programs = root / "Programs"
        lexical = Path(os.path.abspath(self.install_dir))
        if lexical.parent != programs or lexical.name != APP_NAME:
            raise InstallationError("Install directory is outside the per-user Programs folder")
        if _is_reparse_point(programs):
            raise InstallationError("Per-user Programs folder cannot be a reparse point")
        resolved_programs = programs.resolve()
        if resolved_programs.parent != root or resolved_programs.name != "Programs":
            raise InstallationError("Per-user Programs folder is redirected outside LOCALAPPDATA")
        if _is_reparse_point(lexical):
            raise InstallationError("Install directory cannot be a reparse point")
        if lexical.exists() and not lexical.is_dir():
            raise InstallationError("Install directory is not a normal directory")
        resolved = lexical.resolve()
        if resolved.parent != resolved_programs or resolved.name != APP_NAME:
            raise InstallationError("Resolved install directory is outside the expected location")
        expected_executable = lexical / f"{APP_NAME}.exe"
        expected_uninstaller = lexical / f"{UNINSTALL_APP_NAME}.exe"
        if Path(os.path.abspath(self.executable)) != expected_executable:
            raise InstallationError("Application executable is outside the install directory")
        if Path(os.path.abspath(self.uninstaller)) != expected_uninstaller:
            raise InstallationError("Uninstaller executable is outside the install directory")
        return lexical


def quote_command(executable: Path, *arguments: str) -> str:
    if any('"' in argument for argument in arguments):
        raise ValueError("command arguments cannot contain quotes")
    suffix = "" if not arguments else " " + " ".join(arguments)
    return f'"{executable.resolve()}"{suffix}'


def apps_entry_values(
    layout: InstallLayout,
    *,
    estimated_size_kib: int,
    install_date: date | None = None,
) -> dict[str, tuple[object, str]]:
    """Return deterministic registry values and their logical Windows types."""

    if estimated_size_kib < 1:
        raise ValueError("estimated_size_kib must be positive")
    install_root = layout.validated_install_dir()
    installed_on = install_date or date.today()
    return {
        "DisplayName": (APP_DISPLAY_NAME, "str"),
        "DisplayVersion": (APP_VERSION, "str"),
        "Publisher": (APP_PUBLISHER, "str"),
        "InstallLocation": (str(install_root), "str"),
        "DisplayIcon": (f'"{layout.executable.resolve()}",0', "str"),
        "UninstallString": (quote_command(layout.uninstaller), "str"),
        "NoModify": (1, "dword"),
        "NoRepair": (1, "dword"),
        "EstimatedSize": (estimated_size_kib, "dword"),
        "InstallDate": (installed_on.strftime("%Y%m%d"), "str"),
        "URLInfoAbout": (APP_INFO_URL, "str"),
    }


class AppsAndFeaturesRegistration:
    """Write only the current user's uninstall entry; never touches HKLM."""

    def __init__(self, registry: Any | None = None) -> None:
        if registry is None and os.name == "nt":
            import winreg

            registry = winreg
        self._registry = registry

    def register(self, layout: InstallLayout, *, estimated_size_kib: int) -> None:
        registry = self._registry
        if registry is None:
            raise InstallationError("Windows registry is unavailable")
        values = apps_entry_values(layout, estimated_size_kib=estimated_size_kib)
        access = registry.KEY_QUERY_VALUE | registry.KEY_SET_VALUE
        snapshot = self._snapshot_values(registry)
        try:
            with registry.CreateKeyEx(
                registry.HKEY_CURRENT_USER,
                UNINSTALL_REGISTRY_KEY,
                0,
                access,
            ) as key:
                for name, (value, kind) in values.items():
                    registry_kind = registry.REG_DWORD if kind == "dword" else registry.REG_SZ
                    registry.SetValueEx(key, name, 0, registry_kind, value)
        except Exception as exc:
            try:
                self._restore_values(registry, snapshot)
            except Exception as rollback_exc:
                raise InstallationError(
                    f"Apps & Features registration failed and rollback failed: {rollback_exc}"
                ) from exc
            raise

    @staticmethod
    def _enum_values(registry: Any, key: Any) -> dict[str, tuple[object, object]]:
        values: dict[str, tuple[object, object]] = {}
        index = 0
        while True:
            try:
                name, value, kind = registry.EnumValue(key, index)
            except OSError as exc:
                error_code = getattr(exc, "winerror", None) or exc.errno
                if error_code != 259:
                    raise
                break
            values[name] = (value, kind)
            index += 1
        return values

    def _snapshot_values(self, registry: Any) -> dict[str, tuple[object, object]] | None:
        try:
            with registry.OpenKey(
                registry.HKEY_CURRENT_USER,
                UNINSTALL_REGISTRY_KEY,
                0,
                registry.KEY_QUERY_VALUE,
            ) as key:
                return self._enum_values(registry, key)
        except FileNotFoundError:
            return None

    def _restore_values(
        self,
        registry: Any,
        snapshot: dict[str, tuple[object, object]] | None,
    ) -> None:
        if snapshot is None:
            with suppress(FileNotFoundError):
                registry.DeleteKey(registry.HKEY_CURRENT_USER, UNINSTALL_REGISTRY_KEY)
            return
        access = registry.KEY_QUERY_VALUE | registry.KEY_SET_VALUE
        with registry.CreateKeyEx(
            registry.HKEY_CURRENT_USER,
            UNINSTALL_REGISTRY_KEY,
            0,
            access,
        ) as key:
            for name in self._enum_values(registry, key):
                registry.DeleteValue(key, name)
            for name, (value, kind) in snapshot.items():
                registry.SetValueEx(key, name, 0, kind, value)

    def unregister(self) -> bool:
        registry = self._registry
        if registry is None:
            return False
        try:
            registry.DeleteKey(registry.HKEY_CURRENT_USER, UNINSTALL_REGISTRY_KEY)
        except FileNotFoundError:
            return False
        return True


def installed_size_kib(paths: tuple[Path, ...]) -> int:
    total = 0
    for path in paths:
        try:
            total += path.stat().st_size
        except OSError as exc:
            raise InstallationError(f"Could not measure install payload: {path}") from exc
    return max(1, (total + 1023) // 1024)


def start_menu_shortcut_path(appdata: str | Path | None = None) -> Path:
    raw = appdata if appdata is not None else os.environ.get("APPDATA")
    if not raw:
        raise InstallationError("APPDATA is unavailable for the Start Menu shortcut")
    root = Path(raw).resolve()
    programs = root / "Microsoft" / "Windows" / "Start Menu" / "Programs"
    if programs.exists() and (_is_reparse_point(programs) or not programs.is_dir()):
        raise InstallationError("The per-user Start Menu Programs folder is unsafe")
    return programs / START_MENU_SHORTCUT_NAME


def create_start_menu_shortcut(
    layout: InstallLayout,
    *,
    appdata: str | Path | None = None,
    runner: Any = subprocess.run,
) -> Path:
    """Create one exact per-user Shell Link through Windows' supported COM API."""

    layout.validated_install_dir()
    if not layout.executable.is_file():
        raise InstallationError("Cannot create a shortcut before the application is installed")
    shortcut = start_menu_shortcut_path(appdata)
    shortcut.parent.mkdir(parents=True, exist_ok=True)
    if shortcut.exists() and (_is_reparse_point(shortcut) or not shortcut.is_file()):
        raise InstallationError("The existing Start Menu shortcut path is unsafe")
    system_root = os.environ.get("SYSTEMROOT")
    if not system_root:
        raise InstallationError("SYSTEMROOT is unavailable for shortcut creation")
    powershell = Path(system_root) / "System32" / "WindowsPowerShell" / "v1.0" / "powershell.exe"
    if not powershell.is_file():
        raise InstallationError("Windows PowerShell is unavailable for shortcut creation")
    script = r"""
$ErrorActionPreference = 'Stop'
$shell = New-Object -ComObject WScript.Shell
$shortcut = $shell.CreateShortcut($env:LSMLTK_SHORTCUT_PATH)
$shortcut.TargetPath = $env:LSMLTK_SHORTCUT_TARGET
$shortcut.WorkingDirectory = $env:LSMLTK_SHORTCUT_WORKDIR
$shortcut.Description = $env:LSMLTK_SHORTCUT_DESCRIPTION
$shortcut.Save()
""".strip()
    environment = os.environ.copy()
    environment["LSMLTK_SHORTCUT_PATH"] = str(shortcut)
    environment["LSMLTK_SHORTCUT_TARGET"] = str(layout.executable.resolve())
    environment["LSMLTK_SHORTCUT_WORKDIR"] = str(layout.install_dir.resolve())
    environment["LSMLTK_SHORTCUT_DESCRIPTION"] = APP_DISPLAY_NAME
    encoded = base64.b64encode(script.encode("utf-16-le")).decode("ascii")
    try:
        completed = runner(
            [
                str(powershell),
                "-NoLogo",
                "-NoProfile",
                "-NonInteractive",
                "-WindowStyle",
                "Hidden",
                "-EncodedCommand",
                encoded,
            ],
            capture_output=True,
            check=False,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
            env=environment,
            timeout=20,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise InstallationError(f"Could not create the Start Menu shortcut: {exc}") from exc
    if completed.returncode != 0 or not shortcut.is_file() or shortcut.stat().st_size <= 0:
        detail = str(getattr(completed, "stderr", "")).strip()[:300]
        suffix = f": {detail}" if detail else ""
        raise InstallationError(f"Could not create the Start Menu shortcut{suffix}")
    return shortcut


def remove_start_menu_shortcut(appdata: str | Path | None = None) -> bool:
    """Remove only this app's exact per-user shortcut."""

    shortcut = start_menu_shortcut_path(appdata)
    if not shortcut.exists():
        return False
    if _is_reparse_point(shortcut) or not shortcut.is_file():
        raise InstallationError("The Start Menu shortcut path is unsafe")
    shortcut.unlink()
    return True


__all__ = [
    "AppsAndFeaturesRegistration",
    "INSTALL_OPERATION_MUTEX_NAME",
    "InstallLayout",
    "InstallationError",
    "UNINSTALL_REGISTRY_KEY",
    "UNINSTALL_REGISTRY_PARENT",
    "apps_entry_values",
    "create_start_menu_shortcut",
    "installed_size_kib",
    "quote_command",
    "remove_start_menu_shortcut",
    "start_menu_shortcut_path",
]
