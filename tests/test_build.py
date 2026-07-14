from __future__ import annotations

import runpy
from hashlib import sha256
from pathlib import Path

import pytest

from build import (
    INSTALLER_TARGET,
    MAIN_NAME,
    MAIN_TARGET,
    UNINSTALL_NAME,
    UNINSTALL_TARGET,
    BuildTarget,
    build_arguments,
    build_target,
    clean_outputs,
    stage_distribution_licenses,
    write_checksum_manifest,
)


def project_layout(tmp_path: Path) -> tuple[Path, Path, Path]:
    root = tmp_path / "repository"
    package = root / "src" / "league_skin_manager"
    package.mkdir(parents=True)
    entrypoint = package / "__main__.py"
    entrypoint.write_text("def main(): pass\n", encoding="utf-8")
    return root, package, entrypoint


def test_build_arguments_use_package_entrypoint_without_elevation(tmp_path: Path) -> None:
    root, _package, entrypoint = project_layout(tmp_path)
    target = BuildTarget("LeagueSkinManagerLTK", entrypoint, root / "build_main")

    arguments = build_arguments(target, project_root=root, dist_dir=root / "dist")

    assert "--onefile" in arguments
    assert "--noconsole" in arguments
    assert "--uac-admin" not in arguments
    assert arguments[-1] == str(entrypoint.resolve())
    assert "--collect-submodules" not in arguments
    hidden_imports = [
        arguments[index + 1]
        for index, argument in enumerate(arguments)
        if argument == "--hidden-import"
    ]
    assert hidden_imports == []
    assert str(root / "src") in arguments


def test_clean_removes_only_verified_repo_output_directories(tmp_path: Path) -> None:
    root, _package, _entrypoint = project_layout(tmp_path)
    outputs = [
        root / "build_main",
        root / "build_uninstall",
        root / "build_installer",
        root / "dist",
    ]
    for output in outputs:
        output.mkdir()
        (output / "artifact").write_text("temporary", encoding="utf-8")
    outside = tmp_path / "outside"
    outside.mkdir()
    sentinel = outside / "keep.txt"
    sentinel.write_text("keep", encoding="utf-8")

    clean_outputs(root, outputs)

    assert not any(output.exists() for output in outputs)
    assert sentinel.read_text(encoding="utf-8") == "keep"

    with pytest.raises(RuntimeError, match="unverified"):
        clean_outputs(root, [outside])
    assert sentinel.exists()


def test_build_target_requires_a_nonempty_executable(tmp_path: Path) -> None:
    root, _package, entrypoint = project_layout(tmp_path)
    target = BuildTarget("LeagueSkinManagerLTK", entrypoint, root / "build_main")
    captured: list[list[str]] = []

    def successful_runner(arguments: list[str]) -> None:
        captured.append(arguments)
        dist = Path(arguments[arguments.index("--distpath") + 1])
        name = arguments[arguments.index("--name") + 1]
        dist.mkdir(parents=True)
        (dist / f"{name}.exe").write_bytes(b"executable")

    executable = build_target(
        target,
        project_root=root,
        dist_dir=root / "dist",
        runner=successful_runner,
    )

    assert executable.read_bytes() == b"executable"
    assert len(captured) == 1

    executable.unlink()
    with pytest.raises(RuntimeError, match="did not produce"):
        build_target(
            target,
            project_root=root,
            dist_dir=root / "dist",
            runner=lambda _arguments: None,
        )


def test_build_rejects_entrypoint_outside_package(tmp_path: Path) -> None:
    root, _package, _entrypoint = project_layout(tmp_path)
    outside = root / "legacy_main.py"
    outside.write_text("pass\n", encoding="utf-8")
    target = BuildTarget("Legacy", outside, root / "build_main")

    with pytest.raises(RuntimeError, match="outside the package"):
        build_arguments(target, project_root=root, dist_dir=root / "dist")


def test_installer_target_bundles_both_application_executables() -> None:
    arguments = build_arguments(INSTALLER_TARGET)
    data_values = [
        arguments[index + 1] for index, value in enumerate(arguments) if value == "--add-data"
    ]

    assert any(f"{MAIN_NAME}.exe" in value and value.endswith(";payload") for value in data_values)
    assert any(
        f"{UNINSTALL_NAME}.exe" in value and value.endswith(";payload") for value in data_values
    )
    assert sum(value.endswith(";payload/licenses") for value in data_values) == 4


def test_each_windows_entrypoint_has_distinct_identity_metadata() -> None:
    version_files = {
        MAIN_TARGET.version_file,
        INSTALLER_TARGET.version_file,
        UNINSTALL_TARGET.version_file,
    }

    assert None not in version_files
    assert len(version_files) == 3
    assert "LeagueSkinManagerLTKSetup" in INSTALLER_TARGET.version_file.read_text(encoding="utf-8")
    assert "LeagueSkinManagerLTKUninstall" in UNINSTALL_TARGET.version_file.read_text(
        encoding="utf-8"
    )


def test_distribution_stages_human_readable_license_files(tmp_path: Path) -> None:
    root, _package, _entrypoint = project_layout(tmp_path)
    source = root / "notice.txt"
    source.write_text("notice", encoding="utf-8")

    staged = stage_distribution_licenses(
        root / "dist",
        project_root=root,
        license_sources=((source, "NOTICE.txt"),),
    )

    assert staged == (root / "dist" / "licenses" / "NOTICE.txt",)
    assert staged[0].read_text(encoding="utf-8") == "notice"


def test_distribution_writes_deterministic_sha256_manifest(tmp_path: Path) -> None:
    root, _package, _entrypoint = project_layout(tmp_path)
    dist = root / "dist"
    licenses = dist / "licenses"
    licenses.mkdir(parents=True)
    executable = dist / "LeagueSkinManagerLTK.exe"
    notice = licenses / "NOTICE.txt"
    executable.write_bytes(b"application")
    notice.write_bytes(b"notice")

    manifest = write_checksum_manifest(dist, project_root=root)

    assert manifest.read_text(encoding="utf-8").splitlines() == [
        f"{sha256(b'application').hexdigest()} *LeagueSkinManagerLTK.exe",
        f"{sha256(b'notice').hexdigest()} *licenses/NOTICE.txt",
    ]

    outside = tmp_path / "outside.exe"
    outside.write_bytes(b"outside")
    with pytest.raises(RuntimeError, match="outside the distribution"):
        write_checksum_manifest(dist, project_root=root, files=(outside,))


def test_main_target_includes_only_runtime_dynamic_imports() -> None:
    arguments = build_arguments(MAIN_TARGET)
    assert "--version-file" in arguments
    hidden_imports = [
        arguments[index + 1] for index, value in enumerate(arguments) if value == "--hidden-import"
    ]

    assert hidden_imports == [
        "tkinter",
        "tkinter.ttk",
        "tkinter.filedialog",
        "pystray",
        "pystray._win32",
    ]
    data_values = [
        arguments[index + 1] for index, value in enumerate(arguments) if value == "--add-data"
    ]
    assert any("ltk-engine.exe" in value and value.endswith(";engine") for value in data_values)


def test_setup_entrypoint_is_loadable_as_a_direct_pyinstaller_script() -> None:
    entrypoint = (
        Path(__file__).resolve().parents[1] / "src" / "league_skin_manager" / "installer.py"
    )

    namespace = runpy.run_path(str(entrypoint), run_name="packaging_smoke")

    assert callable(namespace["main"])
