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

> **Status: Windows preview `0.1.0-preview.2`.** Based on Herdr `0.9.1`, with
> AgentRadio `0.7.1`. This is a portable development preview, not a stable release.

## What is in this preview?

| Capability | Current behavior |
| --- | --- |
| Role-aware sidebar | Dropdown for workspace, attention, role, recent activity and name ordering; controller, worker and reviewer labels with icons and colors |
| Accounts | Codex, OpenCode, Gemini and Claude profiles; quota where supported; account and model selection; reviewed idle Codex account switching |
| Results and outputs | Per-pane Results button, readable answers and history, artifact previews and file actions |
| Teams | Quick Task, Research and Development templates with explicit roles and independent review |
| Model Lab | Fixed local and published benchmark subsets, single-model runs, comparisons, scoring and a test-case viewer |
| Workspace organization | Custom Spaces groups and rename, plus a persistent Current workspace only agent filter |
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

Click the agent list's ordering label (for example, **role ▾**) to choose an
order directly. **Needs attention first** brings blocked agents and unseen
results forward; **Recently changed** follows agent state changes; **Name
(A–Z)** helps find a known agent. The current choice is checked and saved for
the local endpoint. Use arrow keys and Enter in the menu, or Escape to cancel.
Explicit custom agent views retain their own ordering.

## Try it on Windows

Prerequisites: Windows x64, Python 3.10+ for Radio, and the agent CLI tools you
want to use. Their authentication remains managed by those tools. Account and
benchmark panels also need the optional Textual UI dependency installed by setup.

Extract a Watchtower preview ZIP into a writable folder, then run these commands
from that folder in Windows Terminal:

```powershell
.\setup-radio.cmd
.\setup-accounts.cmd
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

## Preview boundaries and next steps

The preview is Windows x64 and local-only. Upstream self-updates, remote
provisioning, detection catalog downloads and shared provider-hook modifications
are disabled. There is no Watchtower installer or automatic update feed yet.

Results currently reads Codex structured records; other providers retain their
terminal view. Same-provider account switching initially supports idle native
Codex agents in Windows PowerShell. First-use login or folder-trust screens can
still require interaction. Accounts never transfers credentials between profiles.

Next steps include wider provider Results support, more account-switch adapters,
and a documented migration and update path. Global review stays optional.

## Privacy and local data

The portable package is built from explicit runtime allowlists. It contains no
user accounts, credentials, conversations, workspaces or private benchmark data.
These stay in the user's local data directories. Custom benchmark source names,
references and hashes belong in local manifests, outside this repository.
Published benchmark subsets include their source attribution and limitations.

The [release validation](distribution/watchtower/VALIDATION.md) records the
checked source revision, test coverage, privacy audit scope and known limits.

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
