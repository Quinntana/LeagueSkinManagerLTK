"""Narrow Windows integration boundary for instance/startup/process handling."""

from __future__ import annotations

import ctypes
import logging
import os
import subprocess
import sys
from collections.abc import Collection
from contextlib import suppress
from ctypes import wintypes
from pathlib import Path
from typing import Any

import psutil

from .config import APP_NAME, MANAGER_PROCESS_NAMES

ERROR_ALREADY_EXISTS = 183
LEGACY_MUTEX_NAME = f"{APP_NAME}_Mutex_v1"
MUTEX_NAME = f"Local\\{APP_NAME}_Mutex_v2"
MUTEX_NAMES = (LEGACY_MUTEX_NAME, MUTEX_NAME)
ACTIVATION_EVENT_NAME = f"Local\\{APP_NAME}_Activate_v1"
WAIT_OBJECT_0 = 0x00000000
WAIT_TIMEOUT = 0x00000102


class SingleInstanceMutex:
    def __init__(self, name: str | None = None, kernel32: Any | None = None) -> None:
        self.names = (name,) if name is not None else MUTEX_NAMES
        self.kernel32 = kernel32
        self.handle: int | None = None
        self._handles: list[int] = []
        if self.kernel32 is None and os.name == "nt":
            self.kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

    def acquire(self) -> bool:
        if self.kernel32 is None:
            return True
        if self._handles:
            return True
        create_mutex = self.kernel32.CreateMutexW
        create_mutex.argtypes = [wintypes.LPVOID, wintypes.BOOL, wintypes.LPCWSTR]
        create_mutex.restype = wintypes.HANDLE
        for name in self.names:
            ctypes.set_last_error(0)
            handle = create_mutex(None, False, name)
            if not handle:
                self.release()
                return False
            native_handle = int(handle)
            self._handles.append(native_handle)
            self.handle = self._handles[0]
            if ctypes.get_last_error() == ERROR_ALREADY_EXISTS:
                self.release()
                return False
        return True

    def release(self) -> None:
        handles = tuple(reversed(self._handles))
        self._handles.clear()
        if not handles or self.kernel32 is None:
            self.handle = None
            return
        self.handle = None
        for handle in handles:
            with suppress(Exception):
                self.kernel32.CloseHandle(wintypes.HANDLE(handle))

    def __enter__(self) -> SingleInstanceMutex:
        if not self.acquire():
            raise RuntimeError(f"{APP_NAME} is already running")
        return self

    def __exit__(self, *_args: object) -> None:
        self.release()


class InstanceActivationEvent:
    """Auto-reset event used to ask the owning instance to show its window."""

    def __init__(self, name: str = ACTIVATION_EVENT_NAME, kernel32: Any | None = None) -> None:
        self.name = name
        self.kernel32 = kernel32
        self.handle: int | None = None
        if self.kernel32 is None and os.name == "nt":
            self.kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

    def create(self) -> bool:
        """Create or open the named event and retain its native handle."""

        if self.handle is not None:
            return True
        if self.kernel32 is None:
            return False
        create_event = self.kernel32.CreateEventW
        create_event.argtypes = [
            wintypes.LPVOID,
            wintypes.BOOL,
            wintypes.BOOL,
            wintypes.LPCWSTR,
        ]
        create_event.restype = wintypes.HANDLE
        handle = create_event(None, False, False, self.name)
        if not handle:
            error = ctypes.get_last_error()
            raise OSError(error, "Could not create the application activation event")
        self.handle = int(handle)
        return True

    def signal(self) -> bool:
        """Wake the primary instance, retaining a signal until it begins waiting."""

        if not self.create() or self.kernel32 is None or self.handle is None:
            return False
        set_event = self.kernel32.SetEvent
        set_event.argtypes = [wintypes.HANDLE]
        set_event.restype = wintypes.BOOL
        if not set_event(wintypes.HANDLE(self.handle)):
            error = ctypes.get_last_error()
            raise OSError(error, "Could not signal the application activation event")
        return True

    def wait(self, timeout_milliseconds: int) -> bool:
        """Wait for activation, returning ``False`` when the timeout expires."""

        if timeout_milliseconds < 0:
            raise ValueError("timeout_milliseconds cannot be negative")
        if not self.create() or self.kernel32 is None or self.handle is None:
            return False
        wait_for_single = self.kernel32.WaitForSingleObject
        wait_for_single.argtypes = [wintypes.HANDLE, wintypes.DWORD]
        wait_for_single.restype = wintypes.DWORD
        result = int(
            wait_for_single(
                wintypes.HANDLE(self.handle),
                wintypes.DWORD(timeout_milliseconds),
            )
        )
        if result == WAIT_OBJECT_0:
            return True
        if result == WAIT_TIMEOUT:
            return False
        error = ctypes.get_last_error()
        raise OSError(error, "Could not wait for the application activation event")

    def close(self) -> None:
        """Release the native event handle; safe to call more than once."""

        handle = self.handle
        self.handle = None
        if handle is None or self.kernel32 is None:
            return
        with suppress(Exception):
            self.kernel32.CloseHandle(wintypes.HANDLE(handle))

    def __enter__(self) -> InstanceActivationEvent:
        self.create()
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()


class StartupRegistration:
    KEY_PATH = r"Software\Microsoft\Windows\CurrentVersion\Run"

    def __init__(
        self,
        app_name: str = APP_NAME,
        arguments: tuple[str, ...] = ("--background",),
    ) -> None:
        self.app_name = app_name
        self.arguments = arguments

    @staticmethod
    def _command(executable: Path, arguments: tuple[str, ...] = ("--background",)) -> str:
        suffix = "" if not arguments else " " + " ".join(arguments)
        return f'"{executable.resolve()}"{suffix}'

    def is_enabled(self, executable: Path) -> bool:
        if os.name != "nt":
            return False
        import winreg

        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, self.KEY_PATH) as key:
                value, _kind = winreg.QueryValueEx(key, self.app_name)
            return str(value) == self._command(executable, self.arguments)
        except (FileNotFoundError, OSError):
            return False

    def set_enabled(self, executable: Path, enabled: bool) -> None:
        if os.name != "nt":
            return
        import winreg

        access = winreg.KEY_QUERY_VALUE | winreg.KEY_SET_VALUE
        with winreg.CreateKeyEx(
            winreg.HKEY_CURRENT_USER,
            self.KEY_PATH,
            0,
            access,
        ) as key:
            if enabled:
                winreg.SetValueEx(
                    key,
                    self.app_name,
                    0,
                    winreg.REG_SZ,
                    self._command(executable, self.arguments),
                )
            else:
                with suppress(FileNotFoundError):
                    winreg.DeleteValue(key, self.app_name)


class ProcessLauncher:
    def __init__(self, logger: logging.Logger) -> None:
        self.logger = logger

    @staticmethod
    def is_running(executable_name: str) -> bool:
        expected = executable_name.casefold()
        for process in psutil.process_iter(["name"]):
            try:
                name = process.info.get("name")
                if isinstance(name, str) and name.casefold() == expected:
                    return True
            except (psutil.Error, OSError):
                continue
        return False

    @staticmethod
    def is_running_under(
        directory: Path,
        known_executable_names: Collection[str] = MANAGER_PROCESS_NAMES,
    ) -> bool:
        """Return whether any process executable is inside an owned directory."""

        root = directory.resolve()
        expected_names = {name.casefold() for name in known_executable_names}
        for process in psutil.process_iter(["name", "exe"]):
            try:
                name = process.info.get("name")
                executable = process.info.get("exe")
            except (psutil.Error, OSError):
                continue
            known_name = isinstance(name, str) and name.casefold() in expected_names
            if not isinstance(executable, str) or not executable:
                if known_name:
                    return True
                continue
            try:
                resolved = Path(executable).resolve()
            except (OSError, ValueError):
                if known_name:
                    return True
                continue
            if resolved == root or resolved.is_relative_to(root):
                return True
        return False

    def launch(self, executable: Path) -> bool:
        if not executable.is_file():
            self.logger.warning("Executable is unavailable: %s", executable)
            return False
        try:
            creation_flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
            subprocess.Popen(
                [str(executable)],
                cwd=str(executable.parent),
                close_fds=True,
                creationflags=creation_flags,
            )
            self.logger.info("Launched %s", executable)
            return True
        except OSError:
            self.logger.exception("Could not launch %s", executable)
            return False


def running_executable() -> Path:
    return Path(sys.executable).resolve()


def open_path(path: Path) -> None:
    """Open a local file or directory with the user's Windows shell."""

    if os.name != "nt":
        raise OSError("Opening paths is supported only on Windows")
    os.startfile(str(path.resolve()))


def reveal_path(path: Path) -> None:
    """Open a directory, or select a file in Explorer without executing it."""

    if os.name != "nt":
        raise OSError("Revealing paths is supported only on Windows")
    resolved = path.resolve(strict=True)
    if resolved.is_dir():
        os.startfile(str(resolved))
        return
    system_root = os.environ.get("SYSTEMROOT")
    if not system_root:
        raise OSError("SYSTEMROOT is unavailable")
    explorer = Path(system_root) / "explorer.exe"
    if not explorer.is_file():
        raise OSError("Windows Explorer is unavailable")
    subprocess.Popen(
        [str(explorer), "/select,", str(resolved)],
        close_fds=True,
    )
