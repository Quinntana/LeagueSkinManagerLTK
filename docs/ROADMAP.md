# Professional roadmap

## Delivered foundation

- [x] Search and filter a local `.fantome`/`.modpkg` library by package and
  gameplay metadata.
- [x] Content-addressed imports, duplicate detection, atomic index/profile
  writes, integrity rehashing, transactional batches, and bounded parsing.
- [x] Persist enabled mods and a validated League `Game` directory in the
  default profile.
- [x] Build enabled packages in deterministic profile order with the isolated
  Rust sidecar.
- [x] Start/stop controls and lifecycle status in both the desktop and tray.
- [x] Discover the separately installed new LTK provider and require valid
  Authenticode signatures from its current publisher before use.
- [x] Enforce provider flags 0, keep provider acquisition out of the app, and
  implement bounded asynchronous shutdown.
- [x] Per-user Apps & Features registration, exact uninstaller, Start Menu
  shortcut, Windows version metadata, and bundled license notices.
- [x] Keep large mutable packages, overlay state, cache, and logs under
  `%LOCALAPPDATA%\LeagueSkinManagerLTK`.

## Release safety

- Generate and bundle complete Python/Rust transitive dependency notices and a
  CycloneDX or SPDX SBOM. The current developer build includes project/engine
  notices and a SHA-256 manifest, but is not a production compliance bundle.
- Sign and timestamp the setup, app, uninstaller, and sidecar; publish the
  post-signing SHA-256 manifest and SBOM for every release.
- Pin provider host/DLL hashes to a reviewed official release manifest and
  validate its trusted timestamp in addition to the current publisher check.
- Add a signed, rollback-capable updater with explicit stable/preview channels,
  a staged health check, and recovery to the last known-good version.
- Test install, update, repair, upgrade-from-old-branding, and uninstall in
  Windows Sandbox for clean and dirty states, including explicit
  keep/remove-data choices and interrupted operations.
- Add crash recovery for an unexpectedly orphaned external host and incomplete
  overlay builds without deleting user packages.

## Mod workflow

- Add named profiles, duplicate/rename/export, drag-and-drop priority, and an
  explicit load-order preview. The current release has one persisted default
  profile and deterministic ordering, but no priority editor.
- Add thumbnail, README, license, author-link, layer, WAD-footprint, and
  disk-usage previews without extracting untrusted archives into shared paths.
- Add pre-build conflict, linked-bin, game-patch, and prohibited-content
  diagnostics with actionable explanations and per-mod attribution.
- Add backup/export/import of profile and library metadata without duplicating
  content-addressed package blobs.
- Detect Riot/League installations for one-click game-path selection while
  retaining manual validation and never silently selecting a test/PBE tree.

## Runtime diagnostics and performance

- Distinguish missing/unreleased provider, signature or hash mismatch,
  antivirus delay, stale game cache, incompatible mod, host crash, and injection
  lifecycle failures in user-facing recovery guidance.
- Add structured rotating logs, a user-reviewed redacted diagnostics bundle,
  and opt-in crash reporting. Never include package contents or account data by
  default.
- Record local build timings and cache hit rates, then add measurable resource
  budgets, cancellation checkpoints, and timeouts around long overlay builds.
- Reuse unchanged overlay inputs safely through content hashes and invalidate
  only the affected state after a game patch or profile change.
- Add a health page for engine version/protocol, provider version/signature,
  storage use, last build, and last clean shutdown.

## Product quality

- Add a professionally designed application/setup icon and preserve the current
  distinct app, setup, and uninstaller version identities.
- Vietnamese and English localization, scalable typography, keyboard-only
  navigation, screen-reader labels, high-contrast mode, and reduced motion.
- First-run onboarding that explains original/authorized-mod policy, local data
  ownership, and why the separate official LTK dependency may be unavailable on
  the latest stable release.
- Non-blocking notifications for imports/builds, clear empty/error states, and
  undo for reversible profile/library actions.
- Opt-in Practice Tool acceptance harness with before/after game-file hashes;
  never automate ranked or live-match testing.
