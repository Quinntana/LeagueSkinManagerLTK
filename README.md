# League Skin Manager LTK

A native Windows library and runtime UI for authorized League of Legends mods,
with an isolated open-source LeagueToolkit engine. This repository preserves the
history of
[LeagueSkinManagerVN](https://github.com/Quinntana/LeagueSkinManagerVN) while
removing its CSLOL updater, remote paid-skin mirror, and direct CSLOL folder
integration.

> **Upstream availability:** the application can build an overlay and supervise
> the new official LTK patcher from a separate, verified LTK Manager
> installation. However, the new `ltk_patcher_host.exe` and
> `ltk_patcher_dll.dll` are not in stable LTK Manager v1.11.0; they currently
> exist only on upstream `main`. A typical stable installation therefore cannot
> activate mods yet. This project never bundles or downloads those binaries.

## What works

- Search and filter imported `.fantome` and `.modpkg` packages by name,
  author, champion, tag, version, or format.
- Import packages into a content-addressed local library with SHA-256 duplicate
  detection, bounded metadata parsing, atomic copies, and an atomic index.
- Persist enabled mods and the selected League `Game` directory in a default
  profile, and start or stop that profile from the desktop or tray.
- Inspect both package formats and build deterministic, ordered overlays through
  a Rust newline-delimited JSON sidecar built on `ltk_overlay 0.5.2`,
  `ltk_modpkg 0.6.0`, and `ltk_fantome 0.6.1`.
- Locate a separately installed official LTK Manager and require valid
  Authenticode signatures from its current publisher on the manager, new host,
  and DLL before treating the provider as available.
- Supervise the external host asynchronously, surface typed lifecycle status,
  and stop it within a bounded deadline. The host receives fixed
  `config flags 0`; callers cannot disable the anti-hack behavior.
- Keep the Python/Tk UI replaceable: package and overlay operations use a
  versioned engine protocol, while provider lifecycle code is in a separate
  UI-independent runtime boundary.
- Run as a single-instance desktop and system-tray app, start in the background,
  monitor League efficiently, and expose a per-user Windows setup/uninstaller.
- Show a Windows error dialog with the diagnostic log path if no-console startup
  or tray initialization fails, instead of disappearing silently.
- Register a precise Apps & Features entry whose `UninstallString` points to
  the installed uninstaller. Setup also creates a Start Menu shortcut, embeds
  Windows version metadata, and installs the application/engine license notices.
- Recover authenticated setup backups after an interrupted upgrade and return
  cancellation or substantive cleanup failures directly to Apps & Features;
  only deletion of the running uninstaller itself is deferred until process exit.
- Remove this app's packages, overlays, profiles, state, cache, logs, shortcut,
  startup entry, registration, and program files while leaving an independently
  installed official LTK Manager alone.

Only original or otherwise authorized custom mods belong in this library. The
old default mirror was removed because LTK's launcher policy prohibits paid Riot
skin replication and unfair-advantage mods.

## Architecture

```text
Python/Tk presentation + default profile
        |                         |
        | versioned NDJSON        | lifecycle/status
        v                         v
Rust ltk-engine sidecar     Python runtime supervisor
  - package inspection       - verified provider discovery
  - ltk_overlay build        - fixed flags 0 configuration
  - provider.smoke           - start/stop external host
        |                         |
        +------------+------------+
                     v
        Separately installed official LTK provider
```

The Rust sidecar contains no Tauri, React, Tk, injection DLL, download logic, or
anti-hack opt-out. Its `provider.smoke` method is configuration-only and never
starts a process. After profile validation and overlay construction, the Python
runtime coordinator may start the already-installed, Authenticode-verified
provider and monitor its line protocol. See [engine/README.md](engine/README.md)
and [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md).

## Important upstream status

- [LTK Manager](https://github.com/LeagueToolkit/ltk-manager) is the official
  successor to CSLOL Manager.
- The latest stable release audited here is
  [v1.11.0](https://github.com/LeagueToolkit/ltk-manager/releases/tag/v1.11.0),
  published July 7, 2026. It still ships `cslol-host.exe` and
  `cslol-hook-dll.dll`, which this application does not use.
- The renamed new patcher was merged on July 11, 2026 in
  [PR #292](https://github.com/LeagueToolkit/ltk-manager/pull/292) and has not
  reached a stable release. Until it does, normal stable users can manage the
  local library and profile but cannot start mod activation.
- The manager UI/backend and reusable LeagueToolkit crates are source-available
  under MIT/Apache-2.0. The injected patcher binaries are binary-only in the
  public sources inspected and are subject to additional or unclear
  redistribution terms. They are intentionally absent here.

Read [docs/LTK-LICENSING.md](docs/LTK-LICENSING.md) for the exact source,
runtime, and distribution boundaries.

## Local data

Mutable data is stored beneath `%LOCALAPPDATA%\LeagueSkinManagerLTK`:

- `library/packages/`: content-addressed user packages.
- `library/library.json`: atomic searchable index.
- `profiles/default.json`: enabled package identities and the League game path.
- `engine-state/`: incremental LTK overlay state.
- `overlay/`: patched overlay copies; original game WADs are not the output.
- `cache/` and `logs/`: disposable runtime data and diagnostics.

The app does not write into an official LTK Manager installation. The per-user
program files are separate at
`%LOCALAPPDATA%\Programs\LeagueSkinManagerLTK`.

## Development

Python 3.10-3.14:

```powershell
python -m pip install poetry==2.2.1
poetry install
poetry run pytest
poetry run ruff format --check .
poetry run ruff check .
poetry run mypy src
```

Rust stable:

```powershell
cargo fmt --manifest-path engine/Cargo.toml --all -- --check
cargo clippy --manifest-path engine/Cargo.toml --all-targets --all-features -- -D warnings
cargo test --manifest-path engine/Cargo.toml --all-features
cargo build --manifest-path engine/Cargo.toml --release
```

Build the Rust release sidecar before running `python build.py`; when present,
the Windows executable embeds it under its private one-file runtime directory.
The build creates:

- `dist\LeagueSkinManagerLTK.exe`
- `dist\LeagueSkinManagerLTKUninstall.exe`
- `dist\LeagueSkinManagerLTKSetup.exe`
- `dist\licenses\` with the GPL application and independent engine notices
- `dist\SHA256SUMS.txt` covering every executable and license file

The setup installs per-user under
`%LOCALAPPDATA%\Programs\LeagueSkinManagerLTK`, creates a Start Menu shortcut,
and registers the exact uninstaller without requiring elevation. Do not test the
uninstaller while this app is running.

## Safe testing boundary

Automated tests use synthetic packages/game directories and fake provider
processes. They never start League, inject a DLL, or modify real game files. A
real-game acceptance test must be an explicit opt-in using an original creative
mod in Practice Tool/custom play, with before/after game-file hashes and the
current official signed provider.

## Professional roadmap

The highest-value remaining work is tracked in
[docs/ROADMAP.md](docs/ROADMAP.md): signed releases and automatic updates,
release-hash pinning, thumbnail and README previews, conflict and compatibility
diagnostics, structured support bundles, accessibility and localization, a
Windows Sandbox installer/uninstaller matrix, and an opt-in Practice Tool
acceptance harness.

## Licenses

The inherited Python application remains GPL-3.0; see [LICENSE](LICENSE).
`engine/` is independently available under MIT OR Apache-2.0 and retains
LeagueToolkit attribution in [engine/NOTICE.md](engine/NOTICE.md). Setup and
portable build output include these project and engine license files.

Current artifacts are unsigned developer builds, not production releases. A
public binary release still requires complete transitive dependency notices/an
SBOM plus code signing and timestamping; these gates are tracked in the roadmap.

This project is not affiliated with or endorsed by Riot Games. Custom-mod tools
can break after a League patch and may carry account, license, or
Terms-of-Service risk.
