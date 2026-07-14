"""Safe, per-user uninstaller for League Skin Manager LTK.

The installed one-file executable performs confirmation and substantive cleanup
synchronously while holding both installation and application mutexes. An
authenticated relocated worker deletes only the final locked executable after
the original PyInstaller bootloader exits.
"""

from __future__ import annotations

import base64
import ctypes
import json
import os
import re
import secrets
import shutil
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable, Collection
from contextlib import suppress
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any, Protocol

import psutil

from league_skin_manager.config import APP_DISPLAY_NAME, APP_NAME
from league_skin_manager.installation import (
    INSTALL_OPERATION_MUTEX_NAME,
    AppsAndFeaturesRegistration,
    InstallationError,
    InstallLayout,
    _is_reparse_point,
    remove_start_menu_shortcut,
)
from league_skin_manager.windows_integration import SingleInstanceMutex

SERVICE_PROCESS_NAME = f"{APP_NAME}.exe"
BLOCKING_PROCESSES = (SERVICE_PROCESS_NAME,)

_RELOCATED_ENV = "LSMLTK_UNINSTALL_RELOCATED"
_RELOCATED_DIR_ENV = "LSMLTK_UNINSTALL_TEMP_DIR"
_RELOCATED_WAIT_PID_ENV = "LSMLTK_UNINSTALL_WAIT_PID"
_RELOCATED_NONCE_ENV = "LSMLTK_UNINSTALL_NONCE"
_TEMP_CLEANUP_DIR_ENV = "LSMLTK_TEMP_CLEANUP_DIR"
_TEMP_CLEANUP_ROOT_ENV = "LSMLTK_TEMP_CLEANUP_ROOT"
_TEMP_CLEANUP_PID_ENV = "LSMLTK_TEMP_CLEANUP_PID"
_TEMP_PREFIX = f"{APP_NAME}-uninstall-"
_REMNANT_MARKER_NAME = f".{APP_NAME}-owned-remnant"
_HANDSHAKE_STATUS_NAME = "worker-status.json"
_HANDSHAKE_COMMAND_NAME = "worker-command.json"
_NONCE_PATTERN = re.compile(r"[0-9a-f]{32}")
_REMNANT_PATTERN = re.compile(rf"\.{re.escape(APP_NAME)}-(install|backup)-([0-9a-f]{{32}})")
_WORKER_READY_TIMEOUT_SECONDS = 10.0
_WORKER_COMMAND_TIMEOUT_SECONDS = 60.0 * 60.0
_WORKER_COMMIT_TIMEOUT_SECONDS = 10.0
_WORKER_STOP_TIMEOUT_SECONDS = 5.0
_UNINSTALL_CANCELLED_EXIT_CODE = 1602


class RemovalState(str, Enum):
    REMOVED = "removed"
    NOT_FOUND = "not_found"
    FAILED = "failed"
    SKIPPED = "skipped"


class UninstallStatus(str, Enum):
    SUCCESS = "success"
    PARTIAL = "partial"
    ABORTED = "aborted"


@dataclass(frozen=True, slots=True)
class UninstallResult:
    status: UninstallStatus
    startup: RemovalState
    shortcut: RemovalState
    app_data: RemovalState
    registration: RemovalState
    install_files: RemovalState
    message: str
    blocking_process: str | None = None
    errors: tuple[str, ...] = ()

    @property
    def ok(self) -> bool:
        return self.status is UninstallStatus.SUCCESS


@dataclass(frozen=True, slots=True)
class CleanupWorker:
    """Authenticated handle for the final, post-exit self-delete worker."""

    temp_dir: Path
    nonce: str
    process: Any

    @property
    def status_path(self) -> Path:
        return self.temp_dir / _HANDSHAKE_STATUS_NAME

    @property
    def command_path(self) -> Path:
        return self.temp_dir / _HANDSHAKE_COMMAND_NAME


ProcessFinder = Callable[[Collection[str]], str | None]
StartupRemover = Callable[[], RemovalState]
ShortcutRemover = Callable[[], RemovalState]
TreeRemover = Callable[[Path], RemovalState]
RegistrationRemover = Callable[[], RemovalState]
InstallCleanup = Callable[[], RemovalState]
Confirmer = Callable[[str, str], bool]
Notifier = Callable[[str, str, bool], None]
BeforeCleanup = Callable[[], None]


class Mutex(Protocol):
    def acquire(self) -> bool: ...

    def release(self) -> None: ...


def find_running_process(
    executable_names: Collection[str],
    *,
    process_iter: Callable[..., Any] = psutil.process_iter,
    current_pid: int | None = None,
    ignored_pids: Collection[int] = (),
    blocked_roots: Collection[Path] = (),
) -> str | None:
    """Return the first blocker by known name or executable path under owned roots."""

    own_pid = os.getpid() if current_pid is None else current_pid
    ignored = {own_pid, *ignored_pids}
    expected = {name.casefold() for name in executable_names}
    roots = tuple(Path(root).resolve() for root in blocked_roots)
    for process in process_iter(["pid", "name", "exe"]):
        try:
            pid = process.info.get("pid")
            name = process.info.get("name")
            if pid in ignored:
                continue
            if isinstance(name, str) and name.casefold() in expected:
                return name
            executable = process.info.get("exe")
            if isinstance(executable, str) and executable:
                resolved = Path(executable).resolve()
                if any(resolved == root or resolved.is_relative_to(root) for root in roots):
                    return name if isinstance(name, str) and name else str(resolved)
        except (psutil.Error, OSError, RuntimeError, ValueError):
            continue
    return None


def remove_user_startup_registration() -> RemovalState:
    """Remove only the current user's startup value; never touches HKLM."""

    if os.name != "nt":
        return RemovalState.NOT_FOUND

    import winreg

    key_path = r"Software\Microsoft\Windows\CurrentVersion\Run"
    try:
        with winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            key_path,
            0,
            winreg.KEY_QUERY_VALUE | winreg.KEY_SET_VALUE,
        ) as key:
            try:
                winreg.DeleteValue(key, APP_NAME)
            except FileNotFoundError:
                return RemovalState.NOT_FOUND
    except FileNotFoundError:
        return RemovalState.NOT_FOUND
    return RemovalState.REMOVED


def remove_app_data_tree(path: Path) -> RemovalState:
    if not path.exists():
        return RemovalState.NOT_FOUND
    shutil.rmtree(path)
    if path.exists():
        raise OSError(f"Application data still exists after removal: {path}")
    return RemovalState.REMOVED


def remove_install_tree(layout: InstallLayout) -> RemovalState:
    """Synchronously remove only the validated per-user program directory."""

    install_dir = layout.validated_install_dir()
    removed = remove_owned_install_remnants(layout) > 0
    if install_dir.exists():
        if not install_dir.is_dir() or _is_reparse_point(install_dir):
            raise InstallationError("Install directory is not a normal directory")
        shutil.rmtree(install_dir)
        if install_dir.exists():
            raise OSError(f"Installed files still exist after removal: {install_dir}")
        removed = True
    return RemovalState.REMOVED if removed else RemovalState.NOT_FOUND


def _remnant_marker_text(kind: str, nonce: str) -> str:
    return f"{APP_NAME}\n{kind}\n{nonce}\n"


def write_owned_install_remnant_marker(path: Path, kind: str, nonce: str) -> None:
    """Mark a setup staging directory so later cleanup can prove ownership."""

    if kind not in {"install", "backup"} or _NONCE_PATTERN.fullmatch(nonce) is None:
        raise ValueError("Invalid install-remnant identity")
    match = _REMNANT_PATTERN.fullmatch(path.name)
    if match is None or match.groups() != (kind, nonce):
        raise ValueError("Install-remnant path does not match its identity")
    if not path.is_dir() or _is_reparse_point(path):
        raise InstallationError("Install remnant is not a normal directory")
    marker = path / _REMNANT_MARKER_NAME
    marker.write_text(_remnant_marker_text(kind, nonce), encoding="utf-8")


def _is_owned_install_remnant(path: Path, parent: Path) -> bool:
    match = _REMNANT_PATTERN.fullmatch(path.name)
    if match is None or path.parent != parent:
        return False
    if not path.is_dir() or _is_reparse_point(path):
        return False
    resolved = path.resolve()
    if resolved.parent != parent.resolve() or resolved.name != path.name:
        return False
    marker = path / _REMNANT_MARKER_NAME
    if not marker.is_file() or _is_reparse_point(marker):
        return False
    try:
        if marker.stat().st_size > 256 or marker.resolve().parent != resolved:
            return False
        marker_text = marker.read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        return False
    kind, nonce = match.groups()
    return secrets.compare_digest(marker_text, _remnant_marker_text(kind, nonce))


def remove_owned_install_remnants(layout: InstallLayout) -> int:
    """Remove only direct, normal setup remnants carrying an exact ownership marker."""

    install_dir = layout.validated_install_dir()
    parent = install_dir.parent
    if not parent.exists():
        return 0
    if not parent.is_dir() or _is_reparse_point(parent):
        raise InstallationError("Per-user Programs folder is not a normal directory")
    removed = 0
    for candidate in tuple(parent.iterdir()):
        if not _is_owned_install_remnant(candidate, parent):
            continue
        shutil.rmtree(candidate)
        if candidate.exists():
            raise OSError(f"Install remnant still exists after removal: {candidate}")
        removed += 1
    return removed


def restore_newest_owned_install_backup(layout: InstallLayout) -> Path | None:
    """Recover the newest authenticated backup when the live install is absent."""

    install_dir = layout.validated_install_dir()
    if install_dir.exists():
        return None
    parent = install_dir.parent
    if not parent.exists():
        return None
    if not parent.is_dir() or _is_reparse_point(parent):
        raise InstallationError("Per-user Programs folder is not a normal directory")
    candidates: list[tuple[int, str, Path]] = []
    for candidate in tuple(parent.iterdir()):
        match = _REMNANT_PATTERN.fullmatch(candidate.name)
        if match is None or match.group(1) != "backup":
            continue
        if not _is_owned_install_remnant(candidate, parent):
            continue
        try:
            modified = candidate.stat().st_mtime_ns
        except OSError:
            continue
        candidates.append((modified, candidate.name, candidate))
    if not candidates:
        return None
    backup = max(candidates)[2]
    os.replace(backup, install_dir)
    marker = install_dir / _REMNANT_MARKER_NAME
    try:
        marker.unlink()
    except OSError as exc:
        raise InstallationError("Recovered install ownership marker could not be removed") from exc
    return install_dir


def prepare_install_tree_for_self_delete(
    layout: InstallLayout,
    executable: Path,
) -> RemovalState:
    """Remove install contents synchronously, leaving only the running uninstaller."""

    install_dir = layout.validated_install_dir()
    running = executable.resolve()
    expected = layout.uninstaller.resolve()
    if running != expected or running.parent != install_dir:
        raise InstallationError("Only the installed uninstaller can defer its own deletion")
    if not running.is_file() or _is_reparse_point(running):
        raise InstallationError("Installed uninstaller is not a normal file")

    removed = remove_owned_install_remnants(layout) > 0
    for child in tuple(install_dir.iterdir()):
        if child.resolve() == running:
            continue
        if _is_reparse_point(child):
            raise InstallationError("Install directory contains an unsafe reparse point")
        if child.is_dir():
            shutil.rmtree(child)
        else:
            child.unlink()
        removed = True

    remaining = tuple(install_dir.iterdir())
    if remaining != (layout.uninstaller,):
        raise OSError("Installed files could not be prepared for final self-deletion")
    return RemovalState.REMOVED if removed else RemovalState.NOT_FOUND


def remove_apps_registration() -> RemovalState:
    removed = AppsAndFeaturesRegistration().unregister()
    return RemovalState.REMOVED if removed else RemovalState.NOT_FOUND


def _validated_data_dir(appdata_root: Path, data_dir: Path) -> Path:
    """Allow deletion of exactly ``<LOCALAPPDATA>/<APP_NAME>`` and no other path."""

    lexical_root = Path(os.path.abspath(appdata_root))
    root = lexical_root.resolve()
    lexical_target = Path(os.path.abspath(data_dir))
    if lexical_target.parent != lexical_root:
        raise ValueError("Application data target is outside LOCALAPPDATA")
    if lexical_target.name != APP_NAME:
        raise ValueError(f"Application data target must be named {APP_NAME}")
    if _is_reparse_point(lexical_target):
        raise ValueError("Application data target cannot be a reparse point")
    resolved_target = lexical_target.resolve()
    if resolved_target.parent != root or resolved_target.name != APP_NAME:
        raise ValueError("Resolved application data target is outside LOCALAPPDATA")
    return lexical_target


def _aborted_result(message: str, blocking_process: str | None = None) -> UninstallResult:
    return UninstallResult(
        status=UninstallStatus.ABORTED,
        startup=RemovalState.SKIPPED,
        shortcut=RemovalState.SKIPPED,
        app_data=RemovalState.SKIPPED,
        registration=RemovalState.SKIPPED,
        install_files=RemovalState.SKIPPED,
        blocking_process=blocking_process,
        message=message,
    )


class Uninstaller:
    """Orchestrate process-gated, per-user cleanup with an explicit result."""

    def __init__(
        self,
        *,
        appdata_root: Path,
        data_dir: Path,
        process_finder: ProcessFinder = find_running_process,
        startup_remover: StartupRemover = remove_user_startup_registration,
        shortcut_remover: ShortcutRemover = lambda: RemovalState.NOT_FOUND,
        tree_remover: TreeRemover = remove_app_data_tree,
        registration_remover: RegistrationRemover = lambda: RemovalState.NOT_FOUND,
        install_cleanup: InstallCleanup = lambda: RemovalState.NOT_FOUND,
        operation_mutex: Mutex | None = None,
        mutex: Mutex | None = None,
    ) -> None:
        self.data_dir = _validated_data_dir(appdata_root, data_dir)
        self.process_finder = process_finder
        self.startup_remover = startup_remover
        self.shortcut_remover = shortcut_remover
        self.tree_remover = tree_remover
        self.registration_remover = registration_remover
        self.install_cleanup = install_cleanup
        self.operation_mutex = operation_mutex
        self.mutex = mutex

    def run(self) -> UninstallResult:
        operation_acquired = False
        app_acquired = False
        try:
            if self.operation_mutex is not None:
                operation_acquired = self.operation_mutex.acquire()
                if not operation_acquired:
                    return _aborted_result(
                        "Another League Skin Manager setup or uninstall is already active."
                    )
            if self.mutex is not None:
                app_acquired = self.mutex.acquire()
                if not app_acquired:
                    return _aborted_result(
                        f"Close {SERVICE_PROCESS_NAME} before uninstalling.",
                        SERVICE_PROCESS_NAME,
                    )

            blocking_process = self.process_finder(BLOCKING_PROCESSES)
            if blocking_process is not None:
                return _aborted_result(
                    f"Close {blocking_process} before uninstalling.", blocking_process
                )

            errors: list[str] = []
            try:
                startup_state = self.startup_remover()
                if startup_state is RemovalState.FAILED:
                    errors.append("startup registration: removal failed")
            except (OSError, RuntimeError) as exc:
                startup_state = RemovalState.FAILED
                errors.append(f"startup registration: {exc}")

            try:
                shortcut_state = self.shortcut_remover()
                if shortcut_state is RemovalState.FAILED:
                    errors.append("Start Menu shortcut: removal failed")
            except (OSError, RuntimeError, InstallationError) as exc:
                shortcut_state = RemovalState.FAILED
                errors.append(f"Start Menu shortcut: {exc}")

            try:
                app_data_state = self.tree_remover(self.data_dir)
                if app_data_state is RemovalState.FAILED:
                    errors.append("application data: removal failed")
            except OSError as exc:
                app_data_state = RemovalState.FAILED
                errors.append(f"application data: {exc}")

            if errors:
                return UninstallResult(
                    status=UninstallStatus.PARTIAL,
                    startup=startup_state,
                    shortcut=shortcut_state,
                    app_data=app_data_state,
                    registration=RemovalState.SKIPPED,
                    install_files=RemovalState.SKIPPED,
                    errors=tuple(errors),
                    message="Uninstall incomplete: " + "; ".join(errors),
                )

            try:
                install_state = self.install_cleanup()
                if install_state is RemovalState.FAILED:
                    errors.append("installed files: cleanup failed")
            except (OSError, RuntimeError) as exc:
                install_state = RemovalState.FAILED
                errors.append(f"installed files: {exc}")
            if errors:
                return UninstallResult(
                    status=UninstallStatus.PARTIAL,
                    startup=startup_state,
                    shortcut=shortcut_state,
                    app_data=app_data_state,
                    registration=RemovalState.SKIPPED,
                    install_files=install_state,
                    errors=tuple(errors),
                    message="Uninstall incomplete: " + "; ".join(errors),
                )

            try:
                registration_state = self.registration_remover()
                if registration_state is RemovalState.FAILED:
                    errors.append("Apps & Features registration: removal failed")
            except (OSError, RuntimeError) as exc:
                registration_state = RemovalState.FAILED
                errors.append(f"Apps & Features registration: {exc}")
            if errors:
                return UninstallResult(
                    status=UninstallStatus.PARTIAL,
                    startup=startup_state,
                    shortcut=shortcut_state,
                    app_data=app_data_state,
                    registration=registration_state,
                    install_files=install_state,
                    errors=tuple(errors),
                    message="Uninstall incomplete: " + "; ".join(errors),
                )

            startup_text = (
                "startup registration removed"
                if startup_state is RemovalState.REMOVED
                else "startup registration was already absent"
            )
            data_text = (
                "application data removed"
                if app_data_state is RemovalState.REMOVED
                else "application data was already absent"
            )
            shortcut_text = (
                "Start Menu shortcut removed"
                if shortcut_state is RemovalState.REMOVED
                else "Start Menu shortcut was already absent"
            )
            install_text = (
                "installed files removed"
                if install_state is RemovalState.REMOVED
                else "installed files were already absent"
            )
            registration_text = (
                "Apps & Features registration removed"
                if registration_state is RemovalState.REMOVED
                else "Apps & Features registration was already absent"
            )
            return UninstallResult(
                status=UninstallStatus.SUCCESS,
                startup=startup_state,
                shortcut=shortcut_state,
                app_data=app_data_state,
                registration=registration_state,
                install_files=install_state,
                message=(
                    f"Uninstall complete: {startup_text}; {shortcut_text}; {data_text}; "
                    f"{install_text}; "
                    f"{registration_text}."
                ),
            )
        finally:
            if app_acquired and self.mutex is not None:
                self.mutex.release()
            if operation_acquired and self.operation_mutex is not None:
                self.operation_mutex.release()


def confirm_uninstall(title: str, message: str) -> bool:
    if os.name != "nt":
        return False
    yes = 6
    yes_no = 0x00000004
    warning = 0x00000030
    result = ctypes.windll.user32.MessageBoxW(None, message, title, yes_no | warning)
    return int(result) == yes


def show_result(title: str, message: str, error: bool) -> None:
    if os.name != "nt":
        print(f"{title}: {message}")
        return
    ok = 0x00000000
    icon = 0x00000010 if error else 0x00000040
    ctypes.windll.user32.MessageBoxW(None, message, title, ok | icon)


def main(
    *,
    appdata: str | Path | None = None,
    local_appdata: str | Path | None = None,
    confirmer: Confirmer = confirm_uninstall,
    notifier: Notifier = show_result,
    process_finder: ProcessFinder = find_running_process,
    startup_remover: StartupRemover = remove_user_startup_registration,
    shortcut_remover: ShortcutRemover | None = None,
    tree_remover: TreeRemover = remove_app_data_tree,
    registration_remover: RegistrationRemover = remove_apps_registration,
    install_cleanup: InstallCleanup | None = None,
    before_cleanup: BeforeCleanup | None = None,
    cancelled_exit_code: int = 0,
    ignored_process_ids: Collection[int] = (),
    operation_mutex: Mutex | None = None,
    mutex: Mutex | None = None,
) -> int:
    """Run the interactive entrypoint without elevation or callback exits."""

    raw_local_appdata = (
        local_appdata if local_appdata is not None else os.environ.get("LOCALAPPDATA")
    )
    if not raw_local_appdata:
        notifier("Uninstall failed", "LOCALAPPDATA is unavailable; nothing was removed.", True)
        return 1
    raw_appdata = appdata if appdata is not None else os.environ.get("APPDATA")

    if not confirmer(
        f"Uninstall {APP_NAME}",
        f"Remove {APP_DISPLAY_NAME}, imported mod packages, engine state, overlays, cache, "
        "logs, and all other application data? The separately installed official LTK Manager "
        "will not be removed.",
    ):
        notifier("Uninstall cancelled", "Nothing was removed.", False)
        return cancelled_exit_code

    appdata_root = Path(raw_local_appdata)
    try:
        if before_cleanup is not None:
            before_cleanup()
        layout = InstallLayout.discover(raw_local_appdata)
        data_dir = appdata_root / APP_NAME
        selected_finder = process_finder
        if process_finder is find_running_process:

            def find_owned_process(names: Collection[str]) -> str | None:
                return find_running_process(
                    names,
                    ignored_pids=ignored_process_ids,
                    blocked_roots=(data_dir, layout.install_dir),
                )

            selected_finder = find_owned_process
        selected_install_cleanup = (
            install_cleanup if install_cleanup is not None else lambda: remove_install_tree(layout)
        )
        selected_shortcut_remover = shortcut_remover
        if selected_shortcut_remover is None:

            def remove_shortcut() -> RemovalState:
                if raw_appdata is None:
                    return RemovalState.NOT_FOUND
                removed = remove_start_menu_shortcut(raw_appdata)
                return RemovalState.REMOVED if removed else RemovalState.NOT_FOUND

            selected_shortcut_remover = remove_shortcut
        selected_operation_mutex = (
            operation_mutex
            if operation_mutex is not None
            else SingleInstanceMutex(name=INSTALL_OPERATION_MUTEX_NAME)
        )
        selected_app_mutex = mutex if mutex is not None else SingleInstanceMutex()
        result = Uninstaller(
            appdata_root=appdata_root,
            data_dir=data_dir,
            process_finder=selected_finder,
            startup_remover=startup_remover,
            shortcut_remover=selected_shortcut_remover,
            tree_remover=tree_remover,
            registration_remover=registration_remover,
            install_cleanup=selected_install_cleanup,
            operation_mutex=selected_operation_mutex,
            mutex=selected_app_mutex,
        ).run()
    except (OSError, RuntimeError, ValueError, InstallationError) as exc:
        notifier("Uninstall failed", str(exc), True)
        return 1

    title = {
        UninstallStatus.SUCCESS: "Uninstall complete",
        UninstallStatus.PARTIAL: "Uninstall incomplete",
        UninstallStatus.ABORTED: "Uninstall aborted",
    }[result.status]
    notifier(title, result.message, result.status is not UninstallStatus.SUCCESS)
    return 0 if result.ok else 1


def _detached_creation_flags() -> int:
    if os.name != "nt":
        return 0
    return (
        subprocess.CREATE_NO_WINDOW
        | subprocess.DETACHED_PROCESS
        | subprocess.CREATE_NEW_PROCESS_GROUP
    )


def launch_relocated_uninstaller(
    layout: InstallLayout,
    *,
    executable: Path | None = None,
    parent_pid: int | None = None,
    temp_root: Path | None = None,
    nonce: str | None = None,
    popen: Callable[..., Any] = subprocess.Popen,
) -> CleanupWorker:
    """Launch an authenticated worker that can delete the final locked executable."""

    install_dir = layout.validated_install_dir()
    source = (executable or Path(sys.executable)).resolve()
    if source != layout.uninstaller.resolve() or source.parent != install_dir:
        raise InstallationError("Only the installed uninstaller can be relocated")
    nonce_value = nonce or secrets.token_hex(16)
    if _NONCE_PATTERN.fullmatch(nonce_value) is None:
        raise ValueError("Invalid cleanup-worker nonce")
    root = (temp_root or Path(tempfile.gettempdir())).resolve()
    root.mkdir(parents=True, exist_ok=True)
    if not root.is_dir() or _is_reparse_point(root):
        raise InstallationError("Temporary directory is not a normal directory")
    temp_dir = root / f"{_TEMP_PREFIX}{nonce_value}"
    temp_dir.mkdir()
    relocated = temp_dir / layout.uninstaller.name
    try:
        shutil.copy2(source, relocated)
        environment = os.environ.copy()
        environment[_RELOCATED_ENV] = "cleanup"
        environment[_RELOCATED_DIR_ENV] = str(temp_dir)
        environment[_RELOCATED_NONCE_ENV] = nonce_value
        environment["PYINSTALLER_RESET_ENVIRONMENT"] = "1"
        environment[_RELOCATED_WAIT_PID_ENV] = str(
            os.getppid() if parent_pid is None else parent_pid
        )
        process = popen(
            [str(relocated)],
            cwd=str(temp_dir),
            close_fds=True,
            creationflags=_detached_creation_flags(),
            env=environment,
        )
    except Exception:
        shutil.rmtree(temp_dir, ignore_errors=True)
        raise
    return CleanupWorker(temp_dir=temp_dir, nonce=nonce_value, process=process)


def wait_for_process_exit(
    pid: int,
    *,
    timeout_seconds: float = 30.0,
    process_factory: Callable[[int], Any] = psutil.Process,
) -> None:
    """Wait for the original one-file bootloader parent before deleting its EXE."""

    if pid <= 0 or pid == os.getpid():
        raise ValueError("Invalid parent process identifier")
    try:
        process = process_factory(pid)
        process.wait(timeout=timeout_seconds)
    except psutil.NoSuchProcess:
        return
    except psutil.TimeoutExpired as exc:
        raise RuntimeError("Timed out waiting for the installed uninstaller to exit") from exc


def _validated_temp_copy_dir(path: Path, nonce: str | None = None) -> Path:
    root = Path(tempfile.gettempdir()).resolve()
    lexical = Path(os.path.abspath(path))
    suffix = lexical.name.removeprefix(_TEMP_PREFIX)
    nonce_value = suffix if nonce is None else nonce
    if _NONCE_PATTERN.fullmatch(nonce_value) is None:
        raise ValueError("Temporary uninstaller nonce is invalid")
    if nonce is not None and not secrets.compare_digest(suffix, nonce):
        raise ValueError("Temporary uninstaller nonce does not match its directory")
    if lexical.parent != root or lexical.name != f"{_TEMP_PREFIX}{nonce_value}":
        raise ValueError("Temporary uninstaller directory is outside TEMP")
    if _is_reparse_point(lexical):
        raise ValueError("Temporary uninstaller directory cannot be a reparse point")
    resolved = lexical.resolve()
    if resolved.parent != root or resolved.name != lexical.name:
        raise ValueError("Resolved temporary uninstaller directory is outside TEMP")
    return lexical


def _validated_handshake_path(temp_dir: Path, nonce: str, name: str) -> Path:
    target = _validated_temp_copy_dir(temp_dir, nonce)
    if name not in {_HANDSHAKE_STATUS_NAME, _HANDSHAKE_COMMAND_NAME}:
        raise ValueError("Invalid cleanup-worker handshake file")
    return target / name


def _write_handshake(path: Path, nonce: str, state: str) -> None:
    if state not in {"ready", "commit", "committed", "cancel", "cancelled", "failed"}:
        raise ValueError("Invalid cleanup-worker handshake state")
    payload = json.dumps(
        {"nonce": nonce, "state": state},
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    )
    temporary = path.parent / f".{path.name}.{os.getpid()}.tmp"
    with temporary.open("x", encoding="utf-8", newline="\n") as handle:
        handle.write(payload)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)


def _read_handshake(path: Path, nonce: str) -> str | None:
    if not path.exists():
        return None
    if not path.is_file() or _is_reparse_point(path):
        raise RuntimeError("Cleanup-worker handshake is not a normal file")
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise RuntimeError("Cleanup-worker handshake could not be read") from exc
    if len(raw) > 512:
        raise RuntimeError("Cleanup-worker handshake is too large")
    try:
        value = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RuntimeError("Cleanup-worker handshake is invalid") from exc
    if not isinstance(value, dict) or set(value) != {"nonce", "state"}:
        raise RuntimeError("Cleanup-worker handshake has an invalid schema")
    actual_nonce = value.get("nonce")
    state = value.get("state")
    if (
        not isinstance(actual_nonce, str)
        or not secrets.compare_digest(actual_nonce, nonce)
        or not isinstance(state, str)
        or state not in {"ready", "commit", "committed", "cancel", "cancelled", "failed"}
    ):
        raise RuntimeError("Cleanup-worker handshake failed authentication")
    return state


def wait_for_cleanup_worker_status(
    worker: CleanupWorker,
    expected: str,
    *,
    timeout_seconds: float,
    monotonic: Callable[[], float] = time.monotonic,
    sleeper: Callable[[float], None] = time.sleep,
) -> None:
    """Wait boundedly for an authenticated state from the exact spawned worker."""

    if timeout_seconds <= 0:
        raise ValueError("Cleanup-worker timeout must be positive")
    status_path = _validated_handshake_path(worker.temp_dir, worker.nonce, _HANDSHAKE_STATUS_NAME)
    deadline = monotonic() + timeout_seconds
    while True:
        state = _read_handshake(status_path, worker.nonce)
        if state == expected:
            return
        if state == "failed":
            raise RuntimeError("Cleanup worker reported a failure")
        return_code = worker.process.poll()
        if return_code is not None:
            raise RuntimeError(
                f"Cleanup worker exited before {expected!r} (exit code {return_code})"
            )
        remaining = deadline - monotonic()
        if remaining <= 0:
            raise RuntimeError(f"Timed out waiting for cleanup worker to become {expected!r}")
        sleeper(min(0.05, remaining))


def commit_cleanup_worker(worker: CleanupWorker) -> None:
    command_path = _validated_handshake_path(worker.temp_dir, worker.nonce, _HANDSHAKE_COMMAND_NAME)
    _write_handshake(command_path, worker.nonce, "commit")
    wait_for_cleanup_worker_status(
        worker,
        "committed",
        timeout_seconds=_WORKER_COMMIT_TIMEOUT_SECONDS,
    )


def cancel_cleanup_worker(worker: CleanupWorker) -> None:
    """Cancel or terminate a prepared worker without allowing install deletion."""

    if worker.process.poll() is None:
        command_path = _validated_handshake_path(
            worker.temp_dir, worker.nonce, _HANDSHAKE_COMMAND_NAME
        )
        with suppress(OSError, RuntimeError, ValueError):
            _write_handshake(command_path, worker.nonce, "cancel")
        try:
            worker.process.wait(timeout=_WORKER_STOP_TIMEOUT_SECONDS)
        except subprocess.TimeoutExpired:
            worker.process.terminate()
            try:
                worker.process.wait(timeout=_WORKER_STOP_TIMEOUT_SECONDS)
            except subprocess.TimeoutExpired:
                worker.process.kill()
                worker.process.wait(timeout=_WORKER_STOP_TIMEOUT_SECONDS)
    with suppress(OSError, ValueError):
        shutil.rmtree(_validated_temp_copy_dir(worker.temp_dir, worker.nonce))


def _wait_for_cleanup_command(
    temp_dir: Path,
    nonce: str,
    *,
    timeout_seconds: float = _WORKER_COMMAND_TIMEOUT_SECONDS,
    monotonic: Callable[[], float] = time.monotonic,
    sleeper: Callable[[float], None] = time.sleep,
) -> str:
    command_path = _validated_handshake_path(temp_dir, nonce, _HANDSHAKE_COMMAND_NAME)
    deadline = monotonic() + timeout_seconds
    while True:
        state = _read_handshake(command_path, nonce)
        if state in {"commit", "cancel"}:
            return state
        remaining = deadline - monotonic()
        if remaining <= 0:
            raise RuntimeError("Timed out waiting for cleanup-worker command")
        sleeper(min(0.1, remaining))


def cleanup_relocated_copy(
    temp_dir: Path,
    *,
    nonce: str | None = None,
    parent_pid: int | None = None,
    popen: Callable[..., Any] = subprocess.Popen,
) -> None:
    """Best-effort removal of the relocated one-file executable after it exits."""

    target = _validated_temp_copy_dir(temp_dir, nonce)
    try:
        shutil.rmtree(target)
        return
    except OSError:
        pass
    system_root = os.environ.get("SYSTEMROOT")
    if os.name != "nt" or not system_root:
        return
    powershell = Path(system_root) / "System32" / "WindowsPowerShell" / "v1.0" / "powershell.exe"
    if not powershell.is_file():
        return
    script = f"""
$ErrorActionPreference = 'Stop'
$target = [IO.Path]::GetFullPath($env:{_TEMP_CLEANUP_DIR_ENV})
$root = [IO.Path]::GetFullPath($env:{_TEMP_CLEANUP_ROOT_ENV})
if (-not [StringComparer]::OrdinalIgnoreCase.Equals(
    [IO.Path]::GetDirectoryName($target).TrimEnd('\\'), $root.TrimEnd('\\')
)) {{ exit 2 }}
if (-not [IO.Path]::GetFileName($target).StartsWith('{_TEMP_PREFIX}')) {{ exit 2 }}
Wait-Process -Id ([int]$env:{_TEMP_CLEANUP_PID_ENV}) -ErrorAction SilentlyContinue
for ($attempt = 0; $attempt -lt 20; $attempt++) {{
    try {{ Remove-Item -LiteralPath $target -Recurse -Force -ErrorAction Stop }} catch {{}}
    if (-not (Test-Path -LiteralPath $target)) {{ exit 0 }}
    Start-Sleep -Milliseconds 250
}}
exit 1
""".strip()
    environment = os.environ.copy()
    environment[_TEMP_CLEANUP_DIR_ENV] = str(target)
    environment[_TEMP_CLEANUP_ROOT_ENV] = str(target.parent)
    environment[_TEMP_CLEANUP_PID_ENV] = str(os.getppid() if parent_pid is None else parent_pid)
    encoded = base64.b64encode(script.encode("utf-16-le")).decode("ascii")
    try:
        popen(
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
            close_fds=True,
            creationflags=_detached_creation_flags(),
            env=environment,
        )
    except OSError:
        return


def _run_cleanup_worker(
    layout: InstallLayout,
    *,
    running: Path,
    temp_dir: Path,
    nonce: str,
    wait_pid: int,
) -> int:
    """Acknowledge a command, then perform only the unavoidable post-exit delete."""

    target = _validated_temp_copy_dir(temp_dir, nonce)
    expected = (target / layout.uninstaller.name).resolve()
    if running != expected or not running.is_file() or _is_reparse_point(running):
        raise InstallationError("Cleanup worker is not the authenticated relocated executable")
    status_path = _validated_handshake_path(target, nonce, _HANDSHAKE_STATUS_NAME)
    try:
        _write_handshake(status_path, nonce, "ready")
        command = _wait_for_cleanup_command(target, nonce)
        if command == "cancel":
            _write_handshake(status_path, nonce, "cancelled")
            return 0
        _write_handshake(status_path, nonce, "committed")
        wait_for_process_exit(wait_pid)
        remove_install_tree(layout)
        if layout.install_dir.exists():
            raise OSError("Final installed uninstaller could not be removed")
        return 0
    except (InstallationError, OSError, RuntimeError, ValueError):
        with suppress(OSError, RuntimeError, ValueError):
            _write_handshake(status_path, nonce, "failed")
        return 1
    finally:
        with suppress(OSError, ValueError):
            # The one-file bootloader parent owns the relocated EXE mapping;
            # wait for that process, not only this Python child, before deletion.
            cleanup_relocated_copy(target, nonce=nonce, parent_pid=os.getppid())


def run_uninstall_entrypoint() -> int:
    """Run cleanup synchronously, deferring only deletion of the locked executable."""

    try:
        layout = InstallLayout.discover()
        running = Path(sys.executable).resolve()
        if os.environ.get(_RELOCATED_ENV) == "cleanup":
            temp_dir_raw = os.environ.get(_RELOCATED_DIR_ENV)
            nonce = os.environ.get(_RELOCATED_NONCE_ENV)
            wait_pid_raw = os.environ.get(_RELOCATED_WAIT_PID_ENV)
            if not temp_dir_raw or not nonce or not wait_pid_raw:
                raise ValueError("Cleanup-worker environment is incomplete")
            return _run_cleanup_worker(
                layout,
                running=running,
                temp_dir=Path(temp_dir_raw),
                nonce=nonce,
                wait_pid=int(wait_pid_raw),
            )

        if getattr(sys, "frozen", False) and running == layout.uninstaller.resolve():
            worker: CleanupWorker | None = None
            notifications: list[tuple[str, str, bool]] = []
            bootloader_pid = os.getppid()

            def prepare_worker() -> None:
                nonlocal worker
                worker = launch_relocated_uninstaller(
                    layout,
                    executable=running,
                    parent_pid=bootloader_pid,
                )
                wait_for_cleanup_worker_status(
                    worker,
                    "ready",
                    timeout_seconds=_WORKER_READY_TIMEOUT_SECONDS,
                )

            result = main(
                notifier=lambda title, message, error: notifications.append(
                    (title, message, error)
                ),
                install_cleanup=lambda: prepare_install_tree_for_self_delete(layout, running),
                before_cleanup=prepare_worker,
                cancelled_exit_code=_UNINSTALL_CANCELLED_EXIT_CODE,
                ignored_process_ids=(bootloader_pid,),
            )
            if result == 0:
                if worker is None:
                    raise RuntimeError("Cleanup worker was not prepared")
                try:
                    commit_cleanup_worker(worker)
                except (OSError, RuntimeError, ValueError) as exc:
                    cancel_cleanup_worker(worker)
                    show_result("Uninstall incomplete", str(exc), True)
                    return 1
            elif worker is not None:
                cancel_cleanup_worker(worker)
            for title, message, error in notifications:
                show_result(title, message, error)
            return result
        return main()
    except (InstallationError, OSError, RuntimeError, ValueError) as exc:
        show_result("Uninstall failed", str(exc), True)
        return 1


if __name__ == "__main__":
    raise SystemExit(run_uninstall_entrypoint())


__all__ = [
    "BLOCKING_PROCESSES",
    "CleanupWorker",
    "RemovalState",
    "UninstallResult",
    "UninstallStatus",
    "Uninstaller",
    "cancel_cleanup_worker",
    "cleanup_relocated_copy",
    "commit_cleanup_worker",
    "find_running_process",
    "launch_relocated_uninstaller",
    "main",
    "prepare_install_tree_for_self_delete",
    "remove_app_data_tree",
    "remove_apps_registration",
    "remove_install_tree",
    "remove_owned_install_remnants",
    "restore_newest_owned_install_backup",
    "remove_user_startup_registration",
    "run_uninstall_entrypoint",
    "wait_for_process_exit",
    "wait_for_cleanup_worker_status",
    "write_owned_install_remnant_marker",
]
