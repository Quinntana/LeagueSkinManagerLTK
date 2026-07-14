# Architecture

## Component boundaries

The presentation layer is Python/Tk. It owns search, filtering, import dialogs,
the persisted default profile, game-directory selection, Windows startup, the
tray, setup, and uninstall. Package enablement is stored by SHA-256 content ID,
so a renamed source file cannot silently change profile identity.

`RuntimeCoordinator` is UI-independent Python orchestration. It snapshots and
reconciles the enabled profile, rehashes cached packages, validates the selected
League `Game` directory, negotiates the engine protocol, discovers the external
provider, builds the overlay, and exposes immutable lifecycle status to the
desktop and tray. The desktop disables profile/path controls while the provider
is active, and the composition root rejects concurrent imports or enable-state
changes.

`ltk-engine` is a Rust child process with a stable NDJSON protocol over
stdin/stdout. It owns package inspection and overlay construction through pinned
LeagueToolkit crates. Requests carry a protocol version and correlation ID;
progress events and the final response use the same ID. The sidecar does not
launch the injection host.

`LTKPatcherRuntime` is the third boundary. It supervises a provider already
installed by the user as part of official LTK Manager, consumes its stdout and
stderr asynchronously, maps host lines into typed status, and implements a
bounded stop/kill fallback. The repository contains no provider binary or
provider downloader.

```text
Desktop/tray
    |
    v
Default profile -> RuntimeCoordinator -> ltk-engine -> separate overlay
                         |
                         v
            verified external LTK installation
                         |
                         v
                  LTKPatcherRuntime
                  (start/status/stop)
```

## Supported engine methods

- `engine.hello`: negotiate protocol and capabilities.
- `package.inspect`: bounded metadata inspection for `.modpkg` and
  `.fantome`.
- `overlay.build`: ordered package overlay build with progress events.
- `provider.smoke`: validate PE paths, create fixed safe configuration lines,
  and parse supplied event samples. It is configuration-only and never starts
  a process.

The protocol is documented in [../engine/README.md](../engine/README.md).

## Runtime start sequence

1. Rehash the content-addressed library and reconcile missing profile entries.
2. Require at least one enabled mod and a valid League `Game` directory.
3. Negotiate protocol v1 with `ltk-engine`.
4. Locate an official LTK Manager installation. Windows Authenticode status must
   be valid and the signer subject must contain `O=Natoken LLC` for the manager,
   `ltk_patcher_host.exe`, and `ltk_patcher_dll.dll`.
5. Build an ordered overlay under this application's local data root. The
   returned path is resolved again and must remain beneath the owned overlay
   directory.
6. Run the sidecar's configuration-only `provider.smoke` preflight and require
   it to return the exact located host path.
7. Start that external host without a shell and send exactly the fixed
   configuration used by this implementation: log level 16, flags 0, the owned
   overlay prefix, then `start scan`.
8. Stream scanning/injection/waiting/failure status to both UIs. Stop sends a
   bounded shutdown request and kills the child only if it fails to exit by the
   deadline.

There is no caller-controlled flags field and no anti-hack opt-out. Authenticode
verification is cached only for an unchanged path, size, and modification time.
A future release should additionally pin a reviewed release hash and validate a
trusted timestamp.

## Package library and profile

Imports are copied atomically to
`library/packages/<sha256>.<format>`. The filename is the content identity, so
duplicates are deterministic and user-controlled filenames never become paths.
The library index and default profile are written with atomic replacement.
Archives are not extracted by the Python layer.

The previous GitHub skin mirror, CSLOL executable updater, and CSLOL
`installed/`/profile mutation code were removed. This makes ownership and
uninstall boundaries unambiguous. The application operates only on packages the
user explicitly imports.

## Output and installation safety

Mutable data lives under `%LOCALAPPDATA%\LeagueSkinManagerLTK`; it is never
placed in the roaming profile. The engine requires a real game directory but
writes only to separate state and overlay roots. It rejects output paths that
overlap the game tree or each other. Automated tests use synthetic paths and
packages.

The per-user setup installs under
`%LOCALAPPDATA%\Programs\LeagueSkinManagerLTK`, creates its exact Start Menu
shortcut and Apps & Features record, and includes application/engine license
notices plus Windows version metadata. The uninstaller removes only paths and
registry values owned by this application; it does not remove or modify LTK
Manager. Setup remnants require an exact nonce-bearing name and ownership
marker; a valid backup is recovered if an upgrade was interrupted before the
new program directory was committed. The installed uninstaller reports cancel
and cleanup failures synchronously, then uses an authenticated temporary worker
only for the unavoidable deletion of its locked executable after process exit.

## Upstream provider availability

This lifecycle is implemented, but it requires the renamed
`ltk_patcher_host.exe`/`ltk_patcher_dll.dll` provider. Stable LTK Manager
[v1.11.0](https://github.com/LeagueToolkit/ltk-manager/releases/tag/v1.11.0)
still contains the old CSLOL-named pair. The new pair landed in upstream
[PR #292](https://github.com/LeagueToolkit/ltk-manager/pull/292) after that
release and is currently unreleased. Consequently, the normal stable install is
correctly reported as unavailable for activation; search, import, profiles, and
package management continue to work.
