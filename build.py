"""Build both Windows one-file entrypoints with PyInstaller."""

from __future__ import annotations

import os
import shutil
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent
SRC_DIR = PROJECT_ROOT / "src"
PACKAGE_DIR = SRC_DIR / "league_skin_manager"
DIST_DIR = PROJECT_ROOT / "dist"
BUILD_DIR = PROJECT_ROOT / "build_main"
BUILD_DIR_UNINSTALL = PROJECT_ROOT / "build_uninstall"
BUILD_DIR_INSTALLER = PROJECT_ROOT / "build_installer"
ENGINE_EXECUTABLE = PROJECT_ROOT / "engine" / "target" / "release" / "ltk-engine.exe"
WINDOWS_VERSION_FILE = PROJECT_ROOT / "packaging" / "windows-version.txt"
WINDOWS_UNINSTALL_VERSION_FILE = PROJECT_ROOT / "packaging" / "windows-uninstall-version.txt"
WINDOWS_INSTALLER_VERSION_FILE = PROJECT_ROOT / "packaging" / "windows-installer-version.txt"

MAIN_NAME = "LeagueSkinManagerLTK"
UNINSTALL_NAME = "LeagueSkinManagerLTKUninstall"
INSTALLER_NAME = "LeagueSkinManagerLTKSetup"
_ALLOWED_OUTPUT_NAMES = frozenset({"build_main", "build_uninstall", "build_installer", "dist"})


@dataclass(frozen=True, slots=True)
class BundledData:
    source: Path
    destination: str


@dataclass(frozen=True, slots=True)
class BuildTarget:
    name: str
    entrypoint: Path
    work_dir: Path
    hidden_imports: tuple[str, ...] = ()
    data_files: tuple[BundledData, ...] = ()
    version_file: Path | None = None


LICENSE_SOURCES = (
    (PROJECT_ROOT / "LICENSE", "LICENSE-GPL-3.0.txt"),
    (PROJECT_ROOT / "engine" / "LICENSE-MIT", "LICENSE-MIT.txt"),
    (PROJECT_ROOT / "engine" / "LICENSE-APACHE", "LICENSE-APACHE-2.0.txt"),
    (PROJECT_ROOT / "engine" / "NOTICE.md", "NOTICE.txt"),
)


MAIN_TARGET = BuildTarget(
    MAIN_NAME,
    PACKAGE_DIR / "__main__.py",
    BUILD_DIR,
    hidden_imports=(
        "tkinter",
        "tkinter.ttk",
        "tkinter.filedialog",
        "pystray",
        "pystray._win32",
    ),
    data_files=(BundledData(ENGINE_EXECUTABLE, "engine"),),
    version_file=WINDOWS_VERSION_FILE,
)
UNINSTALL_TARGET = BuildTarget(
    UNINSTALL_NAME,
    PACKAGE_DIR / "uninstall.py",
    BUILD_DIR_UNINSTALL,
    version_file=WINDOWS_UNINSTALL_VERSION_FILE,
)
INSTALLER_TARGET = BuildTarget(
    INSTALLER_NAME,
    PACKAGE_DIR / "installer.py",
    BUILD_DIR_INSTALLER,
    data_files=(
        BundledData(DIST_DIR / f"{MAIN_NAME}.exe", "payload"),
        BundledData(DIST_DIR / f"{UNINSTALL_NAME}.exe", "payload"),
        *(BundledData(source, "payload/licenses") for source, _name in LICENSE_SOURCES),
    ),
    version_file=WINDOWS_INSTALLER_VERSION_FILE,
)
BUILD_TARGETS = (MAIN_TARGET, UNINSTALL_TARGET, INSTALLER_TARGET)

BuildRunner = Callable[[list[str]], None]


def _sha256_file(path: Path) -> str:
    digest = sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _verified_output_path(project_root: Path, path: Path) -> Path:
    """Accept only the three named, direct children used by this build."""

    root = project_root.resolve()
    candidate = Path(os.path.abspath(path))
    if candidate.parent != root or candidate.name not in _ALLOWED_OUTPUT_NAMES:
        raise RuntimeError(f"Refusing to clean unverified build path: {candidate}")
    if candidate.is_symlink():
        raise RuntimeError(f"Refusing to clean symbolic-link build path: {candidate}")
    if candidate.exists():
        resolved = candidate.resolve()
        if resolved.parent != root or resolved.name != candidate.name:
            raise RuntimeError(f"Refusing to clean redirected build path: {candidate}")
        if not candidate.is_dir():
            raise RuntimeError(f"Build output path is not a directory: {candidate}")
    return candidate


def clean_outputs(
    project_root: Path = PROJECT_ROOT,
    outputs: Iterable[Path] | None = None,
) -> None:
    selected = (
        tuple(outputs)
        if outputs is not None
        else (BUILD_DIR, BUILD_DIR_UNINSTALL, BUILD_DIR_INSTALLER, DIST_DIR)
    )
    for output in selected:
        verified = _verified_output_path(project_root, output)
        if verified.exists():
            print(f"Cleaning {verified}", flush=True)
            shutil.rmtree(verified)


def build_arguments(
    target: BuildTarget,
    *,
    project_root: Path = PROJECT_ROOT,
    dist_dir: Path = DIST_DIR,
) -> list[str]:
    root = project_root.resolve()
    source_dir = root / "src"
    package_dir = source_dir / "league_skin_manager"
    entrypoint = target.entrypoint.resolve()
    try:
        entrypoint.relative_to(package_dir)
    except ValueError as exc:
        raise RuntimeError(f"Build entrypoint is outside the package: {entrypoint}") from exc
    if not entrypoint.is_file():
        raise RuntimeError(f"Build entrypoint does not exist: {entrypoint}")

    work_dir = _verified_output_path(root, target.work_dir)
    verified_dist = _verified_output_path(root, dist_dir)
    arguments = [
        "--onefile",
        "--noconfirm",
        "--clean",
        "--noconsole",
        "--name",
        target.name,
        "--distpath",
        str(verified_dist),
        "--workpath",
        str(work_dir),
        "--specpath",
        str(work_dir),
        "--paths",
        str(source_dir),
    ]
    for hidden_import in target.hidden_imports:
        arguments.extend(("--hidden-import", hidden_import))
    if target.version_file is not None:
        version_file = target.version_file.resolve()
        if not version_file.is_file():
            raise RuntimeError(f"Windows version metadata is missing: {version_file}")
        arguments.extend(("--version-file", str(version_file)))
    for data_file in target.data_files:
        arguments.extend(
            (
                "--add-data",
                f"{data_file.source.resolve()}{os.pathsep}{data_file.destination}",
            )
        )
    arguments.append(str(entrypoint))
    return arguments


def _run_pyinstaller(arguments: list[str]) -> None:
    import PyInstaller.__main__  # type: ignore[import-untyped]

    PyInstaller.__main__.run(arguments)


def build_target(
    target: BuildTarget,
    *,
    project_root: Path = PROJECT_ROOT,
    dist_dir: Path = DIST_DIR,
    runner: BuildRunner = _run_pyinstaller,
) -> Path:
    arguments = build_arguments(target, project_root=project_root, dist_dir=dist_dir)
    print(f"Building {target.name} from {target.entrypoint}", flush=True)
    runner(arguments)

    executable = _verified_output_path(project_root, dist_dir) / f"{target.name}.exe"
    if not executable.is_file() or executable.stat().st_size <= 0:
        raise RuntimeError(f"PyInstaller did not produce a valid executable: {executable}")
    print(f"Built {executable} ({executable.stat().st_size:,} bytes)", flush=True)
    return executable


def stage_distribution_licenses(
    dist_dir: Path = DIST_DIR,
    *,
    project_root: Path = PROJECT_ROOT,
    license_sources: tuple[tuple[Path, str], ...] = LICENSE_SOURCES,
) -> tuple[Path, ...]:
    """Place human-readable license notices beside portable build artifacts."""

    verified_dist = _verified_output_path(project_root, dist_dir)
    destination_dir = verified_dist / "licenses"
    destination_dir.mkdir(parents=True, exist_ok=True)
    staged: list[Path] = []
    for source, destination_name in license_sources:
        if not source.is_file():
            raise RuntimeError(f"Required license file is missing: {source}")
        destination = destination_dir / destination_name
        shutil.copy2(source, destination)
        staged.append(destination)
    return tuple(staged)


def write_checksum_manifest(
    dist_dir: Path = DIST_DIR,
    *,
    project_root: Path = PROJECT_ROOT,
    files: Iterable[Path] | None = None,
) -> Path:
    """Write deterministic SHA-256 entries for every distributable file."""

    verified_dist = _verified_output_path(project_root, dist_dir)
    manifest = verified_dist / "SHA256SUMS.txt"
    selected = (
        tuple(files)
        if files is not None
        else tuple(
            sorted(
                (path for path in verified_dist.rglob("*") if path.is_file() and path != manifest),
                key=lambda path: path.relative_to(verified_dist).as_posix(),
            )
        )
    )
    entries: list[str] = []
    for path in selected:
        candidate = Path(os.path.abspath(path))
        if candidate.is_symlink() or not candidate.is_file():
            raise RuntimeError(f"Checksum input is not a regular file: {candidate}")
        try:
            relative = candidate.resolve(strict=True).relative_to(verified_dist)
        except (OSError, ValueError) as exc:
            raise RuntimeError(f"Checksum input is outside the distribution: {candidate}") from exc
        digest = _sha256_file(candidate)
        entries.append(f"{digest} *{relative.as_posix()}")
    temporary = manifest.with_suffix(".tmp")
    temporary.write_text("\n".join(entries) + "\n", encoding="utf-8", newline="\n")
    os.replace(temporary, manifest)
    return manifest


def main() -> None:
    # Validate both package entrypoints before removing any prior build output.
    for target in BUILD_TARGETS:
        build_arguments(target)
    clean_outputs()
    built = [build_target(target) for target in (MAIN_TARGET, UNINSTALL_TARGET)]
    built.append(build_target(INSTALLER_TARGET))
    licenses = stage_distribution_licenses()
    checksum_file = write_checksum_manifest(files=(*built, *licenses))
    print("Builds complete:", flush=True)
    for executable in built:
        print(f"  {executable}", flush=True)
    for license_file in licenses:
        print(f"  {license_file}", flush=True)
    print(f"  {checksum_file}", flush=True)


if __name__ == "__main__":
    main()


__all__ = [
    "BUILD_TARGETS",
    "INSTALLER_TARGET",
    "BundledData",
    "BuildTarget",
    "build_arguments",
    "build_target",
    "clean_outputs",
    "main",
    "stage_distribution_licenses",
    "write_checksum_manifest",
]
