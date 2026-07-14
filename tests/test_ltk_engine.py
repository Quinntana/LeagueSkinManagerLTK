from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from typing import Any

import pytest

import league_skin_manager.ltk_engine as engine_module
from league_skin_manager.ltk_engine import (
    EngineError,
    EngineProcessResult,
    EngineUnavailableError,
    LTKEngineClient,
    LTKInstallationLocator,
    ProviderUnavailableError,
    RegistryProduct,
    engine_path,
)


def make_install(root: Path, *, provider: bool = True) -> None:
    root.mkdir(parents=True)
    (root / "ltk-manager.exe").write_bytes(b"manager")
    if provider:
        (root / "ltk_patcher_host.exe").write_bytes(b"host")
        (root / "ltk_patcher_dll.dll").write_bytes(b"dll")


def trust_fixture(_path: Path) -> bool:
    return True


def test_locator_prefers_exact_registry_product_and_reports_provider(tmp_path: Path) -> None:
    wrong = tmp_path / "wrong"
    make_install(wrong)
    root = tmp_path / "official"
    make_install(root)
    products = (
        RegistryProduct("Not LTK Manager", str(wrong), display_version="9"),
        RegistryProduct("LTK Manager", str(root), display_version="1.12.0"),
    )
    locator = LTKInstallationLocator(
        registry_reader=lambda: products,
        candidate_roots=(),
        binary_verifier=trust_fixture,
    )

    install = locator.require()

    assert install.root == root.resolve()
    assert install.version == "1.12.0"
    assert install.manager_executable.name == "ltk-manager.exe"
    assert install.injection_provider_available is True


def test_locator_uses_display_icon_and_known_roots_without_recursive_search(
    tmp_path: Path,
) -> None:
    root = tmp_path / "LTK Manager"
    make_install(root, provider=False)
    icon = f'"{root / "ltk-manager.exe"}",0'
    from_icon = LTKInstallationLocator(
        registry_reader=lambda: (RegistryProduct("LTK Manager", display_icon=icon),),
        candidate_roots=(),
        binary_verifier=trust_fixture,
    ).require()
    from_candidate = LTKInstallationLocator(
        registry_reader=lambda: (),
        candidate_roots=(tmp_path / "missing", root),
        binary_verifier=trust_fixture,
    ).require()

    assert from_icon.root == root.resolve()
    assert from_icon.injection_provider_available is False
    assert from_candidate.manager_executable == root.resolve() / "ltk-manager.exe"


def test_locator_accepts_provider_resources_subdirectory(tmp_path: Path) -> None:
    root = tmp_path / "LTK Manager"
    make_install(root, provider=False)
    resources = root / "resources"
    resources.mkdir()
    (resources / "ltk_patcher_host.exe").write_bytes(b"host")
    (resources / "ltk_patcher_dll.dll").write_bytes(b"dll")

    installation = LTKInstallationLocator(
        registry_reader=lambda: (),
        candidate_roots=(root,),
        binary_verifier=trust_fixture,
    ).require()

    assert installation.injection_provider_available is True
    assert installation.host_executable == resources.resolve() / "ltk_patcher_host.exe"


def test_locator_deduplicates_and_fails_cleanly(tmp_path: Path) -> None:
    locator = LTKInstallationLocator(
        registry_reader=lambda: (_ for _ in ()).throw(OSError("registry denied")),
        candidate_roots=(tmp_path / "missing", tmp_path / "missing"),
        binary_verifier=trust_fixture,
    )

    assert locator.discover() is None
    with pytest.raises(ProviderUnavailableError, match="official LeagueToolkit"):
        locator.require()


def test_locator_rejects_spoofed_registry_install_and_untrusted_provider(
    tmp_path: Path,
) -> None:
    root = tmp_path / "spoofed"
    make_install(root)
    products = (RegistryProduct("LTK Manager", str(root), display_version="99"),)

    rejected = LTKInstallationLocator(
        registry_reader=lambda: products,
        candidate_roots=(),
        binary_verifier=lambda _path: False,
    )
    assert rejected.discover() is None

    manager_only = LTKInstallationLocator(
        registry_reader=lambda: products,
        candidate_roots=(),
        binary_verifier=lambda path: path.name == "ltk-manager.exe",
    ).require()
    assert manager_only.injection_provider_available is False


def test_locator_requires_provider_pair_in_one_directory(tmp_path: Path) -> None:
    root = tmp_path / "split-provider"
    make_install(root, provider=False)
    (root / "ltk_patcher_host.exe").write_bytes(b"host")
    resources = root / "resources"
    resources.mkdir()
    (resources / "ltk_patcher_dll.dll").write_bytes(b"dll")

    installation = LTKInstallationLocator(
        registry_reader=lambda: (),
        candidate_roots=(root,),
        binary_verifier=trust_fixture,
    ).require()

    assert installation.injection_provider_available is False


def test_locator_cache_fingerprints_same_size_timestamp_swaps(tmp_path: Path) -> None:
    root = tmp_path / "official"
    make_install(root, provider=False)
    manager = root / "ltk-manager.exe"
    calls: list[bytes] = []

    def verify(path: Path) -> bool:
        calls.append(path.read_bytes())
        return True

    locator = LTKInstallationLocator(
        registry_reader=lambda: (),
        candidate_roots=(root,),
        binary_verifier=verify,
    )
    assert locator.require().manager_executable == manager.resolve()
    original_stat = manager.stat()
    manager.write_bytes(b"changed")
    os.utime(manager, ns=(original_stat.st_atime_ns, original_stat.st_mtime_ns))

    assert locator.require().manager_executable == manager.resolve()
    assert calls == [b"manager", b"changed"]


def test_locator_rejects_binary_changed_during_signature_verification(tmp_path: Path) -> None:
    root = tmp_path / "racy"
    make_install(root, provider=False)

    def mutate_while_verifying(path: Path) -> bool:
        path.write_bytes(b"changed")
        return True

    locator = LTKInstallationLocator(
        registry_reader=lambda: (),
        candidate_roots=(root,),
        binary_verifier=mutate_while_verifying,
    )

    assert locator.discover() is None


def test_locator_revalidates_exact_signed_snapshot_without_cache(tmp_path: Path) -> None:
    root = tmp_path / "official"
    make_install(root)
    verified: list[str] = []

    def verify(path: Path) -> bool:
        verified.append(path.name)
        return True

    locator = LTKInstallationLocator(
        registry_reader=lambda: (),
        candidate_roots=(root,),
        binary_verifier=verify,
    )
    installation = locator.require()
    assert installation.manager_sha256 is not None
    assert installation.host_sha256 is not None
    assert installation.hook_sha256 is not None
    initial_verifications = len(verified)

    assert locator.revalidate(installation) is True
    assert len(verified) == initial_verifications + 3

    assert installation.host_executable is not None
    installation.host_executable.write_bytes(b"evil")
    assert locator.revalidate(installation) is False


def client(
    tmp_path: Path,
    response: dict[str, Any] | None = None,
    *,
    returncode: int = 0,
    stderr: str = "",
) -> tuple[LTKEngineClient, list[tuple[tuple[str, ...], str, float]]]:
    executable = tmp_path / "ltk-engine.exe"
    executable.write_bytes(b"engine")
    calls: list[tuple[tuple[str, ...], str, float]] = []

    def run(command: Any, payload: str, timeout: float) -> EngineProcessResult:
        calls.append((tuple(command), payload, timeout))
        output = "" if response is None else json.dumps(response) + "\n"
        return EngineProcessResult(returncode, output, stderr)

    return (
        LTKEngineClient(executable, runner=run, id_factory=lambda: "request-1"),
        calls,
    )


def test_client_discovers_frozen_pyinstaller_sidecar(monkeypatch: Any, tmp_path: Path) -> None:
    bundle = tmp_path / "pyinstaller"
    executable = bundle / "engine" / "ltk-engine.exe"
    executable.parent.mkdir(parents=True)
    executable.write_bytes(b"engine")
    monkeypatch.setattr(engine_module.sys, "_MEIPASS", str(bundle), raising=False)

    discovered = LTKEngineClient.discover(tmp_path / "project")

    assert discovered is not None
    assert discovered.executable == executable


def success(result: dict[str, Any]) -> dict[str, Any]:
    return {
        "protocol": 1,
        "id": "request-1",
        "type": "response",
        "ok": True,
        "result": result,
    }


def test_client_sends_versioned_request_and_returns_matching_response(tmp_path: Path) -> None:
    sidecar, calls = client(tmp_path, success({"name": "ltk-engine"}))

    assert sidecar.hello() == {"name": "ltk-engine"}

    command, payload, timeout = calls[0]
    request = json.loads(payload)
    assert command == (str(sidecar.executable),)
    assert timeout == 30
    assert request == {
        "protocol": 1,
        "id": "request-1",
        "method": "engine.hello",
        "params": {},
    }


def test_client_inspection_resolves_path_and_ignores_progress_events(tmp_path: Path) -> None:
    package = tmp_path / "mod.fantome"
    package.write_bytes(b"package")
    response = success({"display_name": "Original Mod"})
    executable = tmp_path / "ltk-engine.exe"
    executable.write_bytes(b"engine")

    def run(_command: Any, payload: str, _timeout: float) -> EngineProcessResult:
        request = json.loads(payload)
        assert request["params"]["path"] == str(package.resolve())
        event = {"protocol": 1, "id": "request-1", "type": "event", "stage": "read"}
        return EngineProcessResult(0, f"{json.dumps(event)}\n{json.dumps(response)}\n", "")

    sidecar = LTKEngineClient(executable, runner=run, id_factory=lambda: "request-1")
    assert sidecar.inspect_package(package)["display_name"] == "Original Mod"


def test_client_exposes_overlay_and_configuration_only_provider_contract(tmp_path: Path) -> None:
    executable = tmp_path / "ltk-engine.exe"
    executable.write_bytes(b"engine")
    requests: list[dict[str, Any]] = []
    timeouts: list[float] = []

    def run(_command: Any, payload: str, timeout: float) -> EngineProcessResult:
        request = json.loads(payload)
        requests.append(request)
        timeouts.append(timeout)
        response = success({"accepted": True})
        return EngineProcessResult(0, json.dumps(response) + "\n", "")

    sidecar = LTKEngineClient(
        executable,
        timeout_seconds=7,
        overlay_timeout_seconds=321,
        runner=run,
        id_factory=lambda: "request-1",
    )
    package = tmp_path / "original.fantome"
    sidecar.build_overlay(
        game_dir=tmp_path / "Game",
        overlay_dir=tmp_path / "overlay",
        state_dir=tmp_path / "state",
        enabled_packages=(package,),
    )
    sidecar.provider_smoke(
        installation_dir=tmp_path / "LTK Manager",
        overlay_prefix=tmp_path / "overlay",
        event_lines=("status 1.0 waiting ready",),
    )

    assert requests[0]["method"] == "overlay.build"
    assert requests[0]["params"]["enabled_packages"] == [str(package.resolve())]
    assert requests[1]["method"] == "provider.smoke"
    assert requests[1]["params"]["log_level"] == "info"
    assert "flags" not in requests[1]["params"]
    assert timeouts == [321, 7]


def test_default_runner_terminates_a_cancelled_sidecar(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    executable = tmp_path / "ltk-engine.exe"
    executable.write_bytes(b"engine")

    class BlockingProcess:
        def __init__(self) -> None:
            self.returncode: int | None = None
            self.terminated = False

        def poll(self) -> int | None:
            return self.returncode

        def communicate(
            self, input: str | None = None, timeout: float | None = None
        ) -> tuple[str, str]:
            raise subprocess.TimeoutExpired("ltk-engine", timeout or 0)

        def terminate(self) -> None:
            self.terminated = True
            self.returncode = -15

        def kill(self) -> None:
            self.returncode = -9

        def wait(self, timeout: float | None = None) -> int:
            assert self.returncode is not None
            return self.returncode

    process = BlockingProcess()
    monkeypatch.setattr(engine_module.subprocess, "Popen", lambda *_args, **_kwargs: process)
    checks = iter((False, True))
    sidecar = LTKEngineClient(executable)

    with pytest.raises(EngineError, match="cancelled"):
        sidecar.request("engine.hello", cancelled=lambda: next(checks, True))

    assert process.terminated is True


@pytest.mark.parametrize(
    ("response", "message"),
    (
        (None, "no response"),
        (
            {"protocol": 2, "id": "request-1", "type": "response", "ok": True, "result": {}},
            "version",
        ),
        (
            {"protocol": 1, "id": "request-1", "type": "response", "ok": True, "result": []},
            "invalid result",
        ),
        (
            {
                "protocol": 1,
                "id": "request-1",
                "type": "response",
                "ok": False,
                "error": {"code": "bad_package", "message": "package rejected"},
            },
            "package rejected",
        ),
    ),
)
def test_client_rejects_invalid_or_error_responses(
    tmp_path: Path, response: dict[str, Any] | None, message: str
) -> None:
    sidecar, _calls = client(tmp_path, response, returncode=4, stderr="diagnostic")

    with pytest.raises(EngineError, match=message):
        sidecar.hello()


def test_client_validates_method_executable_output_and_runner_errors(tmp_path: Path) -> None:
    missing = LTKEngineClient(tmp_path / "missing.exe")
    with pytest.raises(EngineUnavailableError):
        missing.hello()

    sidecar, _calls = client(tmp_path, success({}))
    with pytest.raises(ValueError, match="method"):
        sidecar.request(" bad ")

    sidecar.MAX_OUTPUT_CHARACTERS = 10
    with pytest.raises(EngineError, match="safety limit"):
        sidecar.hello()

    executable = tmp_path / "failure.exe"
    executable.write_bytes(b"engine")

    def fail(_command: Any, _payload: str, _timeout: float) -> EngineProcessResult:
        raise subprocess.TimeoutExpired("ltk-engine", 1)

    failing = LTKEngineClient(executable, runner=fail)
    with pytest.raises(EngineError, match="Could not run"):
        failing.hello()


def test_display_icon_parser_handles_quoted_and_unquoted_values() -> None:
    assert engine_module._display_icon_path('"C:\\Apps\\LTK Manager\\ltk-manager.exe",0') == Path(
        r"C:\Apps\LTK Manager\ltk-manager.exe"
    )
    assert engine_module._display_icon_path(r"C:\Apps\ltk-manager.exe,0") == Path(
        r"C:\Apps\ltk-manager.exe"
    )
    assert engine_module._display_icon_path("") is None


def test_engine_path_normalizes_windows_verbatim_paths(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(engine_module.sys, "platform", "win32")
    assert engine_path(r"\\?\C:\Users\me\overlay") == Path(r"C:\Users\me\overlay")
    assert engine_path(r"\\?\UNC\server\share\overlay") == Path(r"\\server\share\overlay")
