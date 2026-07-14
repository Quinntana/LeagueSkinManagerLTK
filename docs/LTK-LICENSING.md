# LTK source, runtime, and distribution audit

This is an engineering summary, not legal advice. The upstream state described
here was audited on July 14, 2026.

## Open-source components

LTK Manager's React frontend, Tauri command layer, Rust orchestration, and host
client are published under MIT OR Apache-2.0. The reusable
[`league-mod`](https://github.com/LeagueToolkit/league-mod) crates used by this
project publish the same SPDX licensing in their crate manifests:
`ltk_overlay`, `ltk_modpkg`, `ltk_fantome`, and related packages.

Those crates provide a clean package/overlay seam. LTK Manager itself is a
binary application whose domain objects depend on Tauri state; it is not a
supported headless library or external IPC service. The upstream request for a
headless interface remains tracked in
[issue #104](https://github.com/LeagueToolkit/ltk-manager/issues/104).

This project therefore keeps its own UI independent from upstream's UI:

- the inherited Python application is distributed under GPL-3.0;
- the isolated Rust `engine/` crate is independently offered under
  MIT OR Apache-2.0 and retains LeagueToolkit attribution;
- the engine consumes the published LeagueToolkit crates rather than copying
  LTK Manager's Tauri application code.

Build and installer output include human-readable copies of the applicable GPL,
MIT, Apache-2.0, and attribution notices.

## Injection components

The public LTK Manager repository includes compiled patcher host/DLL resources,
but no matching source project was found in the official organization during
this audit. Stable
[v1.11.0](https://github.com/LeagueToolkit/ltk-manager/releases/tag/v1.11.0)
contains the CSLOL-named pair. The renamed `ltk_patcher_host.exe` and
`ltk_patcher_dll.dll` pair exists only on `main` after
[PR #292](https://github.com/LeagueToolkit/ltk-manager/pull/292) and has not yet
been released.

LTK Manager's
[CSLOL DLL License Addendum](https://github.com/LeagueToolkit/ltk-manager/blob/main/LICENSE-CSLOL.md)
says, among other conditions, that a third-party distributor must not
redistribute the licensor-signed DLL, must use its own publicly trusted and
timestamped signature, must publish certificate identity and hashes, must
enforce prohibited-content rules in the launcher, and must not modify or
reverse-engineer the DLL. The renamed new binaries are not clearly addressed by
a separate notice, so the precise applicability of that addendum should not be
assumed away.

## Boundary used here

This repository:

- does not contain, copy, download, update, or redistribute either provider
  pair;
- discovers only a provider in a separate LTK Manager installation chosen and
  installed by the user;
- requires Windows to report a valid Authenticode signature whose certificate
  subject identifies the current publisher, `O=Natoken LLC`, for the manager,
  new host, and new DLL;
- keeps provider flags fixed at zero and exposes no anti-hack opt-out;
- uses the Rust sidecar only for configuration validation; `provider.smoke`
  never launches a process;
- lets the separate Python runtime coordinator start, monitor, and stop the
  already-installed new host after overlay construction and preflight;
- never writes into or uninstalls the official LTK Manager installation; and
- removes the old paid-skin mirror and accepts only user-authorized local
  imports.

Authenticode publisher validation is a provenance control, not a grant of
redistribution rights and not a complete release allowlist. The current code
does not pin a reviewed host/DLL hash or validate a specific trusted timestamp.
Those are remaining release-hardening items.

Because the new provider is unreleased and its separate redistribution/runtime
terms are not explicit, obtain written clarification from LeagueToolkit and
qualified legal review before publicly distributing a launcher build that can
start it. If future releases make the provider available, consume it only from
the user's official installation; do not add it to this repository or installer
without an explicit license and signing plan.

## Primary references

- <https://github.com/LeagueToolkit/ltk-manager>
- <https://github.com/LeagueToolkit/ltk-manager/blob/main/LICENSE-CSLOL.md>
- <https://github.com/LeagueToolkit/ltk-manager/blob/main/DESIGN.md>
- <https://github.com/LeagueToolkit/ltk-manager/pull/292>
- <https://github.com/LeagueToolkit/ltk-manager/issues/104>
- <https://github.com/LeagueToolkit/league-mod>
