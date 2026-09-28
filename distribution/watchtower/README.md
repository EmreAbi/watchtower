# Watchtower 0.1.0-preview.2 for Windows

Watchtower is an experimental Herdr-derived terminal workspace for coding
agents. This preview includes the Herdr 0.9.1 terminal runtime and an
agent ordering dropdown with workspace, attention, role, recent and name modes. Roles
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

- **menu → accounts** opens account profiles, Codex quota and an explicit new-agent
  account picker. Run `setup-accounts.cmd` once; see `accounts/README.md`.
  **Switch account…** moves selected idle Codex agents to another Codex profile
  in their existing panes, preserving the conversation and model. Review the
  selection before applying; busy agents and unsupported providers are excluded.
  OpenCode Zen balance and existing personal team launcher migration are not
  included.
- **menu → teams** creates Quick Task (one assistant), Research (researcher +
  reviewer), or Development (controller + worker + reviewer) workspaces. Choose
  the folder, Codex/OpenCode accounts and per-role models, review the setup,
  then explicitly create it.
  Reviewed templates include a task/digest completion gate; global review stays
  optional. See `teams/README.md` for workflow and isolation limits.
- Radio requires Python 3.10+; its source revision, file hashes and MIT license
  are recorded under `radio/`. The optional Radio view needs Textual, which is
  installed only on explicit request. See `radio/README.md` for linking and use.
- This is an unsigned preview archive, not an installer. There is no automatic
  update feed for Watchtower yet; replace the extracted application files with
  a later verified preview when one is available.
- The repository retains upstream implementation names for compatibility; the
  portable executables and runtime identity belong to Watchtower.

## Spaces groups

The **Flat list / Groups** dropdown in the Spaces header controls visual
organization. Choose **New group...** to create a group. Right-click a workspace
and choose **Move to group...** to assign it or return it to **Ungrouped**.
Related worktrees move together. Right-click a group header for **Rename group...**,
**Move up**, **Move down**, or **Remove group**; removing a group keeps its
workspaces open.

Click a group header to collapse or expand it. The count and aggregate agent
status remain visible, and the focused workspace remains accessible. Names,
membership, group order and collapse state are saved in local client preferences,
separately for each machine. Groups do not change agents, Radio or workspace
identity. Flat list preserves the previous workspace/worktree layout. Workspace
drag reordering is available in Flat list; grouped placement uses the menu.

## Results and outputs

Click **[Results]** at the top right of an agent pane. Each split pane opens its own
results. You can also use **results** in the menu, or right-click the pane and choose
**View results and outputs** in its actions. Results shows the latest final answer;
Activity is optional; Outputs offers Preview, Open, Save as, Show folder and Copy
path. The task selector reopens recent answers and outputs from the same session.
Outputs includes image, Markdown, text and paged PDF previews inside Results;
Preview enlarges them without opening another application. Escape closes the
enlarged preview first, then returns to the terminal. Supported local `file://` links can also open
through the registered preview action, including terminal soft wraps.

The first structured reader supports Codex. Other providers retain the terminal
view. Quick request handles standalone text, questions and images directly;
Project task retains the existing project/review workflow. Requests are sent
only on an explicit Send click to the verified original agent. Opening this view
never starts another agent or consumes model usage.

Run `setup-accounts.cmd` once to install the private shared Textual runtime and
link Accounts, Results and Teams. The view refreshes only while open. See
[results/README.md](results/README.md) for provider and file-preview limits.

## Roles and icons

The sidebar defaults to role ordering and displays role, state and job text on
separate lines. States include Ready for task and Needs your input. Results
adds structured agent-wait details when supported by the current session.
After an agent has joined, assign optional display metadata to its pane:

```powershell
./watchtower.exe pane report-metadata w1:p1 --source watchtower --token 'team_role=👤 CONTROL' --token 'team_target=Coordination'
```

Use `⚙ WORKER` or `🔎 REVIEW` for the other built-in ordering categories.
Other labels remain usable and sort after these categories. Click the agents
ordering dropdown to choose a mode. Toggle **Current workspace only** in the same
menu to show agents from the selected workspace while retaining the chosen sort.
The list follows workspace selection, including keyboard navigation, and the
filter is remembered when the interface is reopened. Toggle it off to see all
workspaces again. These are display
roles, not permissions or automatic task assignments.

## Model Lab

Use **menu → model lab** to benchmark explicit Codex or OpenCode models on seven
frozen packs: four original packs (Structured Decisions, Coding, Code Review,
Debugging) and published GSM8K, BIG-Bench Hard and BIG-Bench Extra Hard subsets.
There are 200 bundled cases: original packs have 20 Full cases and published
subsets have 40; all have the same five Quick cases on every run. Choose accounts
and models, review the request count, then Start. **Single model test** is the
default; select **Compare two models** to add a second target. Separate matching
runs can be compared later without rerunning either model. Opening the page does not run
inference. History retains per-case evidence, and strict comparison rejects
different pack, grader, adapter or execution conditions.

The **Tests** tab lets you inspect prompts, response formats, references and
scoring before running a model, even without an account. Optional local custom
packs support typed-choice questions; their cases and source metadata stay in
the user's private library. Reference quality is declared in each imported pack,
and unscored source records remain visible for inspection.

**View results** starts with model scorecards: score out of 100, fully passed
cases, answer coverage, execution errors and response time. The Cases tab shows
the task, response and grading explanation; technical evidence is optional.
Incomplete runs and targets without responses are marked **Not scored**.
Speed and reported cost remain separate from the quality score.
Published packs show binary answer accuracy, source/version and attribution.
They use an adapted JSON answer protocol; their scores are not official
full-benchmark leaderboard results. See [benchmark sources and roadmap](benchmarks/BENCHMARKS.md).

The initial Coding/Debugging packs assess small pure Python functions through a
bounded interpreter. They do not execute arbitrary generated code or assess
whole-repository changes. Codex CLI and OpenCode stateless results have different
protocols and remain separate for strict ranking. See
[Model Lab](benchmarks/README.md) for scoring, limits and available usage data.

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
