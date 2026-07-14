"""UI-independent boundary for the open LTK engine and installed patcher provider."""

from __future__ import annotations

import base64
import hashlib
import json
import os
import subprocess
import sys
import uuid
from collections.abc import Callable, Iterable, Mapping, Sequence
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from threading import Lock
from time import monotonic
from typing import Any, Protocol

from .config import ENGINE_PROTOCOL_VERSION


class EngineError(RuntimeError):
    """The engine process or its protocol returned an invalid result."""


class EngineUnavailableError(EngineError):
    """The open LTK sidecar has not been built or installed."""


class ProviderUnavailableError(RuntimeError):
    """No legitimate LTK Manager installation could be found."""


@dataclass(frozen=True, slots=True)
class RegistryProduct:
    display_name: str
    install_location: str = ""
    display_icon: str = ""
    display_version: str = ""


@dataclass(frozen=True, slots=True)
class LTKInstallation:
    root: Path
    manager_executable: Path
    version: str | None
    host_executable: Path | None
    hook_dll: Path | None
    manager_sha256: str | None = None
    host_sha256: str | None = None
    hook_sha256: str | None = None

    @property
    def injection_provider_available(self) -> bool:
        return self.host_executable is not None and self.hook_dll is not None


RegistryReader = Callable[[], Iterable[RegistryProduct]]
BinaryVerifier = Callable[[Path], bool]
_MAX_VERIFIED_BINARY_BYTES = 512 * 1024 * 1024


def _registry_products() -> Iterable[RegistryProduct]:
    """Read Windows uninstall metadata without importing winreg on other platforms."""

    if sys.platform != "win32":
        return ()
    import winreg

    products: list[RegistryProduct] = []
    roots = (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE)
    views = (winreg.KEY_WOW64_64KEY, winreg.KEY_WOW64_32KEY)
    uninstall = r"Software\Microsoft\Windows\CurrentVersion\Uninstall"
    for root in roots:
        for view in views:
            try:
                key = winreg.OpenKey(root, uninstall, 0, winreg.KEY_READ | view)
            except OSError:
                continue
            with key:
                for index in range(winreg.QueryInfoKey(key)[0]):
                    try:
                        subkey_name = winreg.EnumKey(key, index)
                        subkey = winreg.OpenKey(key, subkey_name)
                    except OSError:
                        continue
                    with subkey:
                        values: dict[str, str] = {}
                        for name in (
                            "DisplayName",
                            "InstallLocation",
                            "DisplayIcon",
                            "DisplayVersion",
                        ):
                            try:
                                value, _kind = winreg.QueryValueEx(subkey, name)
                            except OSError:
                                value = ""
                            values[name] = value if isinstance(value, str) else ""
                        products.append(
                            RegistryProduct(
                                display_name=values["DisplayName"],
                                install_location=values["InstallLocation"],
                                display_icon=values["DisplayIcon"],
                                display_version=values["DisplayVersion"],
                            )
                        )
    return products


def _trusted_ltk_binary(path: Path) -> bool:
    """Require a valid Windows signature from LTK's current publisher."""

    if sys.platform != "win32" or not path.is_file():
        return False
    system_root = os.environ.get("SYSTEMROOT")
    if not system_root:
        return False
    powershell = Path(system_root) / "System32" / "WindowsPowerShell" / "v1.0" / "powershell.exe"
    if not powershell.is_file():
        return False
    script = r"""
$ErrorActionPreference = 'Stop'
$signature = Get-AuthenticodeSignature -LiteralPath $env:LSMLTK_VERIFY_BINARY
if ($signature.Status -ne 'Valid' -or $null -eq $signature.SignerCertificate) { exit 2 }
$subject = $signature.SignerCertificate.Subject
if (-not [regex]::IsMatch($subject, '(^|,\s*)O=Natoken LLC(,|$)', 'IgnoreCase')) { exit 3 }
exit 0
""".strip()
    environment = os.environ.copy()
    environment["LSMLTK_VERIFY_BINARY"] = str(path.resolve())
    encoded = base64.b64encode(script.encode("utf-16-le")).decode("ascii")
    try:
        completed = subprocess.run(
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
            creationflags=subprocess.CREATE_NO_WINDOW,
            env=environment,
            timeout=15,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return completed.returncode == 0


class LTKInstallationLocator:
    """Locate, but never download or redistribute, an official LTK installation."""

    MANAGER_NAMES = ("ltk-manager.exe", "LTK Manager.exe")
    HOST_NAME = "ltk_patcher_host.exe"
    DLL_NAME = "ltk_patcher_dll.dll"

    def __init__(
        self,
        *,
        registry_reader: RegistryReader = _registry_products,
        candidate_roots: Iterable[Path] | None = None,
        binary_verifier: BinaryVerifier = _trusted_ltk_binary,
    ) -> None:
        self._registry_reader = registry_reader
        self._candidate_roots = tuple(candidate_roots or self._default_roots())
        self._binary_verifier = binary_verifier
        self._verification_cache: dict[tuple[str, int, int, str], bool] = {}
        self._verification_lock = Lock()

    def discover(self) -> LTKInstallation | None:
        candidates: list[tuple[Path, str | None]] = []
        try:
            products = self._registry_reader()
        except OSError:
            products = ()
        for product in products:
            if product.display_name.strip().casefold() != "ltk manager":
                continue
            version = product.display_version.strip() or None
            location = product.install_location.strip().strip('"')
            if location:
                candidates.append((Path(location), version))
            icon_path = _display_icon_path(product.display_icon)
            if icon_path is not None:
                candidates.append((icon_path.parent, version))
        candidates.extend((root, None) for root in self._candidate_roots)

        seen: set[str] = set()
        for root, version in candidates:
            try:
                resolved = root.expanduser().resolve()
            except OSError:
                continue
            identity = os.path.normcase(str(resolved))
            if identity in seen or not resolved.is_dir():
                continue
            seen.add(identity)
            installation = self._from_root(resolved, version)
            if installation is not None:
                return installation
        return None

    def require(self) -> LTKInstallation:
        installation = self.discover()
        if installation is None:
            raise ProviderUnavailableError(
                "LTK Manager is not installed. Install an official LeagueToolkit release first."
            )
        return installation

    def revalidate(self, installation: LTKInstallation) -> bool:
        """Recheck the exact discovery snapshot without using the signature cache."""

        manager = installation.manager_executable
        if not self._verify_snapshot(manager, installation.manager_sha256):
            return False
        host = installation.host_executable
        hook = installation.hook_dll
        if host is None and hook is None:
            return True
        return not (
            host is None
            or hook is None
            or host.parent != hook.parent
            or not self._verify_snapshot(host, installation.host_sha256)
            or not self._verify_snapshot(hook, installation.hook_sha256)
        )

    def _from_root(self, root: Path, version: str | None) -> LTKInstallation | None:
        manager = next(
            (root / name for name in self.MANAGER_NAMES if (root / name).is_file()), None
        )
        if manager is None or not self._is_verified(manager):
            return None
        manager_identity = _binary_identity(manager)
        if manager_identity is None:
            return None
        provider_roots = (root, root / "resources")
        host = next(
            (base / self.HOST_NAME for base in provider_roots if (base / self.HOST_NAME).is_file()),
            None,
        )
        hook = next(
            (base / self.DLL_NAME for base in provider_roots if (base / self.DLL_NAME).is_file()),
            None,
        )
        if (
            host is None
            or hook is None
            or host.parent != hook.parent
            or not self._is_verified(host)
            or not self._is_verified(hook)
        ):
            host = None
            hook = None
        host_identity = _binary_identity(host) if host is not None else None
        hook_identity = _binary_identity(hook) if hook is not None else None
        if host is not None and (host_identity is None or hook_identity is None):
            host = None
            hook = None
            host_identity = None
            hook_identity = None
        return LTKInstallation(
            root=root,
            manager_executable=manager,
            version=version,
            host_executable=host,
            hook_dll=hook,
            manager_sha256=manager_identity[3],
            host_sha256=host_identity[3] if host_identity is not None else None,
            hook_sha256=hook_identity[3] if hook_identity is not None else None,
        )

    def _is_verified(self, path: Path) -> bool:
        identity = _binary_identity(path)
        if identity is None:
            return False
        with self._verification_lock:
            cached = self._verification_cache.get(identity)
        if cached is not None:
            return cached
        verified = self._binary_verifier(path)
        # Do not cache or accept a signature result for bytes that changed
        # while the external verifier was inspecting the path.
        if _binary_identity(path) != identity:
            verified = False
        with self._verification_lock:
            self._verification_cache[identity] = verified
        return verified

    def _verify_snapshot(self, path: Path, expected_sha256: str | None) -> bool:
        if expected_sha256 is None:
            return False
        before = _binary_identity(path)
        if before is None or before[3] != expected_sha256:
            return False
        verified = self._binary_verifier(path)
        after = _binary_identity(path)
        return verified and after == before

    @staticmethod
    def _default_roots() -> tuple[Path, ...]:
        roots: list[Path] = []
        for variable in ("LOCALAPPDATA", "ProgramFiles", "ProgramFiles(x86)"):
            value = os.environ.get(variable)
            if not value:
                continue
            base = Path(value)
            roots.extend((base / "LTK Manager", base / "Programs" / "LTK Manager"))
        return tuple(roots)


def _binary_identity(path: Path) -> tuple[str, int, int, str] | None:
    """Fingerprint a bounded binary so same-size/timestamp swaps miss the cache."""

    try:
        resolved = path.resolve(strict=True)
        before = resolved.stat()
        if before.st_size <= 0 or before.st_size > _MAX_VERIFIED_BINARY_BYTES:
            return None
        digest = hashlib.sha256()
        with resolved.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
        after = resolved.stat()
    except (OSError, RuntimeError):
        return None
    if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
        return None
    return (
        os.path.normcase(str(resolved)),
        after.st_size,
        after.st_mtime_ns,
        digest.hexdigest(),
    )


def _display_icon_path(value: str) -> Path | None:
    candidate = value.strip()
    if not candidate:
        return None
    if candidate.startswith('"'):
        end = candidate.find('"', 1)
        candidate = candidate[1:end] if end > 1 else candidate.strip('"')
    else:
        candidate = candidate.rsplit(",", 1)[0]
    return Path(candidate) if candidate else None


def engine_path(value: str) -> Path:
    """Convert a sidecar path, including Windows verbatim form, to a normal Path."""

    if sys.platform == "win32":
        if value.casefold().startswith("\\\\?\\unc\\"):
            value = "\\\\" + value[8:]
        elif value.startswith("\\\\?\\"):
            value = value[4:]
    return Path(value)


@dataclass(frozen=True, slots=True)
class EngineProcessResult:
    returncode: int
    stdout: str
    stderr: str


class EngineRunner(Protocol):
    def __call__(
        self,
        command: Sequence[str],
        payload: str,
        timeout_seconds: float,
    ) -> EngineProcessResult: ...


class LTKEngineClient:
    """A small synchronous client for the versioned JSON-lines engine protocol."""

    MAX_OUTPUT_CHARACTERS = 4 * 1024 * 1024

    def __init__(
        self,
        executable: Path,
        *,
        timeout_seconds: float = 30.0,
        overlay_timeout_seconds: float = 15.0 * 60.0,
        runner: EngineRunner | None = None,
        id_factory: Callable[[], str] | None = None,
    ) -> None:
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        if overlay_timeout_seconds <= 0:
            raise ValueError("overlay_timeout_seconds must be positive")
        self.executable = Path(executable)
        self.timeout_seconds = timeout_seconds
        self.overlay_timeout_seconds = overlay_timeout_seconds
        self._runner = runner
        self._id_factory = id_factory or (lambda: uuid.uuid4().hex)
        self._request_lock = Lock()
        self._process_lock = Lock()
        self._active_process: subprocess.Popen[str] | None = None

    @classmethod
    def discover(
        cls,
        project_root: Path,
        *,
        timeout_seconds: float = 30.0,
        overlay_timeout_seconds: float = 15.0 * 60.0,
    ) -> LTKEngineClient | None:
        executable_name = "ltk-engine.exe" if sys.platform == "win32" else "ltk-engine"
        bundle_root = Path(getattr(sys, "_MEIPASS", Path(sys.executable).resolve().parent))
        candidates = (
            project_root / "engine" / executable_name,
            project_root / "engine" / "target" / "release" / executable_name,
            bundle_root / "engine" / executable_name,
        )
        executable = next((path for path in candidates if path.is_file()), None)
        return (
            cls(
                executable,
                timeout_seconds=timeout_seconds,
                overlay_timeout_seconds=overlay_timeout_seconds,
            )
            if executable is not None
            else None
        )

    def request(
        self,
        method: str,
        params: Mapping[str, Any] | None = None,
        *,
        timeout_seconds: float | None = None,
        cancelled: Callable[[], bool] | None = None,
    ) -> Mapping[str, Any]:
        if not method or method.strip() != method:
            raise ValueError("method must be a non-empty normalized string")
        if not self.executable.is_file():
            raise EngineUnavailableError(f"LTK engine was not found at {self.executable}")
        request_id = self._id_factory()
        request = {
            "protocol": ENGINE_PROTOCOL_VERSION,
            "id": request_id,
            "method": method,
            "params": dict(params or {}),
        }
        payload = json.dumps(request, separators=(",", ":"), ensure_ascii=False) + "\n"
        selected_timeout = self.timeout_seconds if timeout_seconds is None else timeout_seconds
        if selected_timeout <= 0:
            raise ValueError("timeout_seconds must be positive")
        with self._request_lock:
            if cancelled is not None and cancelled():
                raise EngineError("LTK engine request was cancelled")
            try:
                if self._runner is not None:
                    completed = self._runner((str(self.executable),), payload, selected_timeout)
                else:
                    completed = self._run_tracked(
                        (str(self.executable),),
                        payload,
                        selected_timeout,
                        cancelled,
                    )
            except (OSError, subprocess.SubprocessError) as exc:
                raise EngineError(f"Could not run the LTK engine: {exc}") from exc
        output = completed.stdout
        if len(output) > self.MAX_OUTPUT_CHARACTERS:
            raise EngineError("LTK engine response exceeded the safety limit")

        response: Mapping[str, Any] | None = None
        for line in output.splitlines():
            try:
                value = json.loads(line)
            except json.JSONDecodeError:
                continue
            if (
                isinstance(value, dict)
                and value.get("type") == "response"
                and value.get("id") == request_id
            ):
                response = value
        if response is None:
            detail = completed.stderr.strip()[:500]
            suffix = f": {detail}" if detail else ""
            raise EngineError(
                f"LTK engine returned no response (exit code {completed.returncode}){suffix}"
            )
        if response.get("protocol") != ENGINE_PROTOCOL_VERSION:
            raise EngineError("LTK engine protocol version does not match this app")
        if response.get("ok") is not True:
            error = response.get("error")
            message = error.get("message") if isinstance(error, dict) else None
            raise EngineError(str(message or "LTK engine rejected the request"))
        result = response.get("result")
        if not isinstance(result, dict):
            raise EngineError("LTK engine returned an invalid result")
        return result

    def cancel_active(self) -> bool:
        """Terminate the currently owned sidecar request, if one exists."""

        with self._process_lock:
            process = self._active_process
        if process is None or process.poll() is not None:
            return False
        self._terminate_process(process)
        return True

    def _run_tracked(
        self,
        command: Sequence[str],
        payload: str,
        timeout_seconds: float,
        cancelled: Callable[[], bool] | None,
    ) -> EngineProcessResult:
        process = subprocess.Popen(
            list(command),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0,
        )
        with self._process_lock:
            self._active_process = process
        deadline = monotonic() + timeout_seconds
        first_communication = True
        try:
            while True:
                if cancelled is not None and cancelled():
                    self._terminate_process(process)
                    raise EngineError("LTK engine request was cancelled")
                remaining = deadline - monotonic()
                if remaining <= 0:
                    self._terminate_process(process)
                    raise subprocess.TimeoutExpired(command, timeout_seconds)
                try:
                    stdout, stderr = process.communicate(
                        input=payload if first_communication else None,
                        timeout=min(0.1, remaining),
                    )
                    return EngineProcessResult(process.returncode or 0, stdout, stderr)
                except subprocess.TimeoutExpired:
                    first_communication = False
        finally:
            with self._process_lock:
                if self._active_process is process:
                    self._active_process = None

    @staticmethod
    def _terminate_process(process: subprocess.Popen[str]) -> None:
        if process.poll() is not None:
            return
        try:
            process.terminate()
            process.wait(timeout=0.5)
        except (OSError, subprocess.TimeoutExpired):
            with suppress(OSError):
                process.kill()
            with suppress(OSError, subprocess.TimeoutExpired):
                process.wait(timeout=0.5)

    def hello(self) -> Mapping[str, Any]:
        return self.request("engine.hello")

    def inspect_package(self, path: Path) -> Mapping[str, Any]:
        return self.request("package.inspect", {"path": str(path.resolve())})

    def build_overlay(
        self,
        *,
        game_dir: Path,
        overlay_dir: Path,
        state_dir: Path,
        enabled_packages: Sequence[Path],
        cancelled: Callable[[], bool] | None = None,
    ) -> Mapping[str, Any]:
        return self.request(
            "overlay.build",
            {
                "game_dir": str(game_dir.resolve()),
                "overlay_dir": str(overlay_dir.resolve()),
                "state_dir": str(state_dir.resolve()),
                "enabled_packages": [str(path.resolve()) for path in enabled_packages],
            },
            timeout_seconds=self.overlay_timeout_seconds,
            cancelled=cancelled,
        )

    def provider_smoke(
        self,
        *,
        installation_dir: Path,
        overlay_prefix: Path,
        event_lines: Sequence[str] = (),
    ) -> Mapping[str, Any]:
        return self.request(
            "provider.smoke",
            {
                "installation_dir": str(installation_dir.resolve()),
                "overlay_prefix": str(overlay_prefix.resolve()),
                "log_level": "info",
                "event_lines": list(event_lines),
            },
        )


__all__ = [
    "EngineError",
    "EngineProcessResult",
    "EngineUnavailableError",
    "engine_path",
    "BinaryVerifier",
    "LTKEngineClient",
    "LTKInstallation",
    "LTKInstallationLocator",
    "ProviderUnavailableError",
    "RegistryProduct",
]
