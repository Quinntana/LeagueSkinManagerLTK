# ltk-engine

`ltk-engine` is the UI-independent core used by LeagueSkinManagerLTK. It is a
small newline-delimited JSON (NDJSON) sidecar around the official open
LeagueToolkit crates:

- `ltk_overlay` 0.5.2
- `ltk_modpkg` 0.6.0
- `ltk_fantome` 0.6.1

It inspects mod packages and builds filesystem overlays. It has no Tauri,
React, Tk, or other UI dependency, so any desktop UI can own the process and
communicate over standard input/output.

## Security and distribution boundary

This crate does **not** contain, download, execute, or redistribute an injection
host or DLL. `provider.smoke` only validates the expected files in a
caller-supplied, installed LTK Manager directory, generates safe configuration
lines, and parses sample host event lines. It never starts a process.

The provider API has no caller-controlled hook flags. It always generates
`config flags 0`; the anti-hack opt-out flag is intentionally neither defined
nor exposed.

LeagueSkinManagerLTK's Python runtime is intentionally outside this crate. It
locates the new provider in a separate official LTK Manager installation,
requires valid Windows Authenticode signatures from the current publisher,
calls this crate's configuration-only smoke check, then starts and supervises
that exact external host. The Python supervisor also sends fixed flags 0; it
does not copy the host or DLL into this repository, application, or installer.
Any launcher must independently satisfy the
[CSLOL DLL License Addendum](https://github.com/LeagueToolkit/ltk-manager/blob/main/LICENSE-CSLOL.md)
and any terms applicable to the renamed provider, including signing,
distribution, and prohibited-content obligations.

Stable LTK Manager
[v1.11.0](https://github.com/LeagueToolkit/ltk-manager/releases/tag/v1.11.0)
does not contain `ltk_patcher_host.exe`/`ltk_patcher_dll.dll`. The pair landed
afterward in [PR #292](https://github.com/LeagueToolkit/ltk-manager/pull/292)
and is currently unreleased, so a normal stable installation cannot activate a
profile through the Python runtime yet.

Building an overlay only writes patched WAD copies to `overlay_dir`. It does
not modify the League installation and does not make the game load the overlay.
The engine rejects overlay/state paths that overlap the game directory.

## Protocol

One request and one response/event occupy one UTF-8 JSON line. stdout is
reserved for protocol frames; fatal process diagnostics go to stderr. Request
lines are capped at 1 MiB. A malformed request returns an error and the engine
continues with the next line.

Request:

```json
{"protocol":1,"id":"request-42","method":"engine.hello","params":{}}
```

Success response:

```json
{"protocol":1,"id":"request-42","type":"response","ok":true,"result":{}}
```

Error response:

```json
{"protocol":1,"id":"request-42","type":"response","ok":false,"error":{"code":"invalid_params","message":"..."}}
```

Progress event (emitted before the matching final response):

```json
{"protocol":1,"id":"build-1","type":"event","event":"overlay.progress","data":{"stage":"indexing","current_file":null,"current":0,"total":0}}
```

Request IDs must be non-empty strings of at most 256 bytes without control
characters. Method parameter objects are strict: unknown fields are rejected so
typos and attempts to pass unsupported provider flags cannot be silently ignored.
On Windows, response paths use conventional drive or UNC spelling rather than
the internal `\\?\` canonical prefix, so owning applications can compare them
with their normal profile paths. Canonical paths remain unchanged internally
for containment checks.

### `engine.hello`

Parameters: `{}`

Returns protocol, engine, crate, method, package-format, load-order, and provider
capabilities. Call this once after starting the process and reject an unsupported
protocol before sending work.

### `package.inspect`

Parameters:

```json
{"path":"C:\\Mods\\my-mod.modpkg"}
```

Supports `.modpkg` and legacy `.fantome` archives. The result uses stable
snake_case fields including `display_name`, `authors`, `version`, `champions`,
and `tags`, plus layer, WAD, and size summaries. Fantome metadata reads are
bounded to 1 MiB. Package files larger than 2 GiB are rejected, and Fantome
archives are limited to 100,000 entries before their contents are processed.

### `overlay.build`

Parameters:

```json
{
  "game_dir":"C:\\Riot Games\\League of Legends\\Game",
  "overlay_dir":"C:\\Users\\me\\AppData\\Local\\LeagueSkinManagerLTK\\overlay",
  "state_dir":"C:\\Users\\me\\AppData\\Local\\LeagueSkinManagerLTK\\engine-state",
  "enabled_packages":["C:\\Mods\\highest-priority.modpkg","C:\\Mods\\fallback.fantome"]
}
```

`enabled_packages` is ordered and limited to 1,024 paths. Index 0 has the
highest priority, matching `ltk_overlay`. Duplicate paths, oversized packages,
oversized Fantome entry tables, and unsupported extensions are rejected. The
state directory contains LTK's incremental `overlay.json` and game-index cache.
The two output directories must be non-overlapping siblings (or otherwise
disjoint), and each leaf's parent directory must already exist. The engine
validates both paths before creating either leaf.

### `provider.smoke`

Parameters:

```json
{
  "installation_dir":"C:\\Users\\me\\AppData\\Local\\LTK Manager",
  "overlay_prefix":"C:\\Users\\me\\AppData\\Local\\LeagueSkinManagerLTK\\overlay",
  "log_level":"info",
  "event_lines":["status 0.1000000 injecting scanning for game"]
}
```

The smoke check looks for `ltk_patcher_host.exe` and `ltk_patcher_dll.dll` at
the installation root or its `resources` directory, requires the pair to
resolve from the same directory, checks that both are PE files, returns fixed
safe configuration lines, and parses the optional event samples. It does not
verify Authenticode and therefore must not be treated as a proof of provenance.
LeagueSkinManagerLTK performs its Authenticode publisher check in the separate
Python installation locator before invoking this method; other callers must
provide an equivalent trust boundary. Allowed log levels are `error`, `info`,
and `debug`.

## Development

The crate uses Rust edition 2024 and the stable toolchain.
Windows MSVC release builds use the static CRT so the sidecar can be embedded
without shipping a separate Visual C++ runtime DLL.

```text
cargo fmt --manifest-path engine/Cargo.toml --all -- --check
cargo clippy --manifest-path engine/Cargo.toml --all-targets --all-features -- -D warnings
cargo test --manifest-path engine/Cargo.toml --all-features
```

Tests build synthetic Fantome and Modpkg archives with the official writers,
exercise the NDJSON contract and error recovery, parse host protocol samples,
and run a no-mod overlay build against a synthetic empty game layout. They do
not launch League or any injection process.

## Licensing and attribution

This engine is available under `MIT OR Apache-2.0`; see `LICENSE-MIT` and
`LICENSE-APACHE`.

The LeagueToolkit crates and the host line-protocol behavior used as the
interoperability reference are authored by LeagueToolkit and licensed under
MIT or Apache-2.0. See the upstream projects:

- <https://github.com/LeagueToolkit/league-mod>
- <https://github.com/LeagueToolkit/ltk-manager>

League of Legends and Riot Games are trademarks of Riot Games, Inc. This
project is not endorsed by Riot Games.
