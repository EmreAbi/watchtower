# Watchtower 0.1.0-preview.1 for Windows

Watchtower is an experimental Herdr-derived terminal workspace for coding
agents. This first preview includes the Herdr 0.9.1 terminal runtime and an
optional **role** ordering mode alongside grouped and priority ordering. Roles
use the existing `team_role` pane metadata; a new installation starts without
preconfigured teams. The private Radio 0.7.1 adapter and a 30-second Agent
Context pane are included as a local plugin.

Extract the entire ZIP into a writable folder. On first use run
`setup-radio.cmd` to register the private Radio plugin, then
`open-watchtower.cmd` from Windows Terminal or another terminal. Keep the
`conpty` directory alongside the executable. You can also invoke
`watchtower.exe` directly. The identical `herdr.exe` is a compatibility entry point for agent
plugins; it runs Watchtower too. No global PATH change, administrator install,
or Visual C++ Redistributable is required.

The application maintains separate Watchtower configuration and runtime state.
The ZIP contains no accounts, credentials, live sessions or personal settings.
The included binary manages that separation; the launchers use only paths
relative to their own location. Agent CLI tools and their authentication remain
external prerequisites. If Radio is linked while Watchtower is already running,
restart only the Watchtower server to activate its startup hook.

## Preview boundaries

- Full Account Stats, team templates and automatic review flows
  from the original local setup are not included in this package yet.
- Radio requires Python 3.10+; its source revision, file hashes and MIT license
  are recorded under `radio/`. The optional Radio view needs Textual, which is
  installed only on explicit request. See `radio/README.md` for linking and use.
- This is an unsigned preview archive, not an installer. There is no automatic
  update feed for Watchtower yet; replace the extracted application files with
  a later verified preview when one is available.
- The repository retains upstream implementation names for compatibility; the
  portable executables and runtime identity belong to Watchtower.

## Roles and icons

The sidebar defaults to role ordering and displays role, state and job text.
After an agent has joined, assign optional display metadata to its pane:

```powershell
./watchtower.exe pane report-metadata w1:p1 --source watchtower --token 'team_role=👤 CONTROL' --token 'team_target=Coordination'
```

Use `⚙ WORKER` or `🔎 REVIEW` for the other built-in ordering categories.
Other labels remain usable and sort after these categories. Click the agents
ordering label to cycle grouped, priority and role ordering. These are display
roles, not permissions or automatic task assignments.

## Separate runtime

Default Windows roots are `%APPDATA%/watchtower` for configuration/sessions
and `%LOCALAPPDATA%/watchtower` for runtime state. `WATCHTOWER_HOME` can select
an absolute alternative root (with `config` and `state` children). Each server
socket gets a separate hashed Radio ledger directory, including named sessions.

Incoming official Herdr/Radio routing is discarded. Inside Watchtower, legacy
`HERDR_*` variables remain available for compatible plugins. Explicit incoming
overrides use `WATCHTOWER_CONFIG_PATH`, `WATCHTOWER_SOCKET_PATH`,
`WATCHTOWER_CLIENT_SOCKET_PATH` and `WATCHTOWER_SESSION`.

The preview disables upstream self-updates, remote provisioning, remote detection
catalog updates and changes to shared provider hooks. Existing agent logins and
compatible installed hooks can still be used. Stop only this runtime with
`watchtower.exe server stop`; use the same `--session` when targeting a named one.

`BUILD-MANIFEST.json` records product and upstream versions, the source commit,
whether the source tree had uncommitted changes, and packaged file hashes.
The adjacent `.sha256` file verifies the complete archive. Packaging validates
the exact private Radio file allowlist before copying it; no installed user
plugin or Radio state is copied.

Source: https://github.com/EmreAbi/watchtower
Upstream: https://github.com/herdrdev/herdr

See [VALIDATION.md](VALIDATION.md) for checks performed and known limitations.

## Building the preview

Requirements: Windows x64, the pinned Rust toolchain in `rust-toolchain.toml`,
MSVC build tools and Windows SDK, Zig 0.16.0, Python 3, Git and the .NET SDK
(for NuGet signature verification). Put the tools on the invoking process PATH
or supply `ZIG`; the build script does not install tools or change user PATH.

From the repository root:

```powershell
pwsh -NoProfile -File scripts/watchtower_build_windows.ps1
```

The build uses `cargo build --release --locked --features watchtower --target
x86_64-pc-windows-msvc`. Artifacts default to `target/watchtower-artifacts`.
Use `-BinaryPath <built-herdr.exe>` to package an already built Watchtower binary.
Use `-ConPtyPackage <cached.nupkg>` to reuse the pinned Microsoft package;
otherwise it is downloaded into the artifact cache and verified.

Run `just check` before release preparation. The manual **Watchtower Windows
preview** GitHub workflow builds and uploads an artifact only; it does not
publish a GitHub Release, create a tag, deploy a site or change any update feed.
