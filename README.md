# Watchtower

**A terminal workspace for coordinating coding-agent teams.**

Watchtower brings agent roles, project context and communication into one place.
The goal is to make it clear who is coordinating, who is implementing, who is
reviewing, and which project each agent belongs to—without turning the sidebar
into a wall of account statistics.

Watchtower is an independent fork of [Herdr](https://github.com/herdrdev/herdr),
with [AgentRadio](https://github.com/detailles/AgentRadio) for communication.
Herdr provides the terminal runtime; this project develops its own defaults,
team experience and release path.

> **Status: Windows preview `0.1.0-preview.1`.** Based on Herdr `0.9.1`, with
> AgentRadio `0.7.1`. This is a portable development preview, not a stable release.

## What is in this preview?

| Capability | Current behavior |
| --- | --- |
| Role-aware sidebar | Grouped, priority and role ordering; controller, worker and reviewer labels with icons and colors |
| Job descriptions | Optional per-pane role and job metadata, with agent state beside it |
| Visible pane identity | Pane borders remain visible even with one pane |
| Private Radio | Bundled, pinned Radio runtime with separate state for each Watchtower server/session |
| Agent Context | Optional pane with approximate session context information, refreshed every 30 seconds |
| Separate installation | Watchtower configuration, sockets and runtime data are isolated from an existing Herdr installation |
| Portable Windows package | App-local ConPTY, launchers, dependency notices and a file-hash manifest |

Roles describe the work; they do not grant permissions or automatically assign
tasks. A fresh installation starts without preconfigured teams or provider
accounts. Agent Context is session context information, not remaining account
quota.

## Try it on Windows

Prerequisites: Windows x64, Python 3.10+ for Radio, and the agent CLI tools you
want to use. Their authentication remains managed by those tools.

Extract a Watchtower preview ZIP into a writable folder, then run these commands
from that folder in Windows Terminal:

```powershell
.\setup-radio.cmd
.\open-watchtower.cmd
```

Keep the whole archive together, including the `conpty` and `radio` directories.
Setup registers the private plugin; it does not replace an existing Herdr/Radio
installation, change the system PATH or import credentials. The included
`herdr.exe` is a compatibility entry point for plugins and runs Watchtower too.

Inside a Watchtower pane, join an agent explicitly:

```powershell
radio join lead --provider codex --new
```

Assign optional role and job metadata using the actual pane ID:

```powershell
.\watchtower.exe pane report-metadata w1:p1 --source watchtower --token 'team_role=👤 CONTROL' --token 'team_target=Coordination'
```

Use `⚙ WORKER` or `🔎 REVIEW` for the other built-in role categories. Labels are
metadata, so custom roles remain possible. See the
[preview guide](distribution/watchtower/README.md) for runtime paths, named
sessions, configuration and shutdown, and the
[Radio guide](distribution/watchtower/radio/README.md) for communication and
context panes.

## Build from source

This checkout retains the upstream default build. Select the Watchtower product
explicitly with the `watchtower` feature:

```powershell
cargo build --release --locked --features watchtower
pwsh -NoProfile -File scripts/watchtower_build_windows.ps1
```

The packaging script builds the Windows target and verifies its pinned Microsoft
ConPTY package. Build prerequisites and optional arguments are in the
[build guide](distribution/watchtower/README.md#building-the-preview).
The manual **Watchtower Windows preview** workflow produces a downloadable
artifact; it does not publish a release automatically.

See the [validation report](distribution/watchtower/VALIDATION.md) for native
isolation checks, test results and the known Windows ACL test limitation.

## What comes next?

The earlier local Herdr setup is a source of requirements, not a claim that every
customization is already in this package. Follow-up work includes:

- Reusable project/team templates with controller, worker and reviewer roles.
- A dedicated Account Stats workspace, keeping quota details out of the sidebar.
- Clear workspace communication boundaries and optional advanced channels.
- Task-based review handoffs, with global review requested only when needed.
- An explicit migration path for existing local team setups.

The preview is local-only. Upstream self-updates, remote provisioning, detection
catalog downloads and shared provider-hook modifications are disabled while
Watchtower establishes its own distribution paths. There is no Watchtower
installer or automatic update feed yet.

## Origins and licensing

Watchtower is independently maintained and is not an official Herdr release.
The terminal engine comes from Herdr; communication comes from AgentRadio.
Our changes focus on team presentation, product defaults, runtime isolation and
packaging. Upstream history and copyright notices are preserved.

The project retains the [Apache 2.0 license](LICENSE). AgentRadio retains its
[MIT license](distribution/watchtower/radio/vendor/AgentRadio/LICENSE), and the
portable package includes dependency notices. Exact upstream revisions are in
[product.json](distribution/watchtower/product.json) and
[Radio provenance](distribution/watchtower/radio/provenance.json); each ZIP
records its source revision and file hashes in `BUILD-MANIFEST.json`.
