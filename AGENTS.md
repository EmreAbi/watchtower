# Watchtower development guidance

Work in this independent fork, EmreAbi/watchtower. Keep Herdr and third-party
license attribution. Read CONTRIBUTING.md for Watchtower contribution rules;
there is no approved-contributor list. Never publish changes to upstream unless
the user explicitly asks and that upstream permits the contribution.

Build Watchtower with `--features watchtower`; keep the default engine build
compatible. Preserve the user's live installation and account state. Never
commit credentials, transcripts, personal teams or private benchmark content.
Use fresh absolute WATCHTOWER_HOME roots for smoke tests and stop only processes
owned by that test. On Windows use PowerShell with profile loading disabled.

## Universal Project Rules

### Principles

- **State is separated from runtime.** `AppState` is pure data, testable without PTYs or async. `PaneState` is separate from `PaneRuntime`. Workspace logic doesn't need real terminals.
- **Render is pure.** `compute_view()` handles geometry and mutations. `render()` takes `&AppState` and only draws. Never mutate state during render.
- **No god objects.** If a module is doing too many things, split it. `app/` is already split into state, actions, and input. Keep it that way.
- **Platform code is isolated.** OS-specific behavior lives in the matching `src/platform/<os>.rs` file, with only shared traits, types, wrappers, and testable contracts in `src/platform/mod.rs`. Core modules don't have `#[cfg(target_os)]`.
- **Detection is decoupled.** The detector reads a screen snapshot, never touches the parser or viewport state.
- **Screen detection is evidence-based.** When changing `src/detect/manifests/`, first capture the relevant bottom-buffer state with `herdr agent read <pane> --source detection --format text` and, when styling or alternate screen behavior matters, `--format ansi`. Decide which visible controls are invariant, which are alternatives, and encode them as explicit AND/OR gates. Do not match whole-pane incidental text, and do not use the user-visible viewport for agent status because users can scroll it.
- **UI patterns should be reused.** Herdr is a mouse-first TUI. New dialogs, onboarding, settings, and post-update flows should follow the existing UI/UX language and interaction patterns instead of inventing one-off screens. Prefer reusing existing modal/screen structure, affordances, and close actions so the app feels consistent.

### Multiplicative performance paths

Treat work reachable from view computation, rendering, background-pane resizing,
PTY parsing, detection, and client frame fanout as multiplicative. Before adding
work, identify its frequency and cardinality: per byte, event, or render × panes,
tabs, or workspaces × attached clients.

Inside pane-scaled render and layout loops:

- Use narrow terminal-state accessors. Do not collect aggregate input state,
  format terminal snapshots, inspect process trees, perform filesystem I/O, or
  allocate when one scalar fact is enough.
- Keep terminal-core lock duration minimal.
- Preserve hidden-source and retained-render early exits. Hidden panes still
  parse output, but their output must not trigger presentation work merely to
  keep terminal or detection state current.
- When a change adds or widens work in one of these loops, profile fixed geometry
  with 1 and at least 15 populated panes and report the scaling delta. Use
  `just bench-render-scale` to exercise both background-workspace and active-pane
  cardinality when applicable.

Prefer deterministic operation or architecture tests to wall-clock CI limits.
Performance benchmarks are supporting evidence, not substitutes for behavioral
coverage. Before a stable release, `just bench-release-smoke` must compare the
candidate with the current stable binary under hidden and visible output. When
the result moves materially or when validating performance work, repeat it with
`HERDR_PERF_SAMPLE_SECONDS=60` and investigate the affected scenario.

### Runtime/client boundary guardrail

Herdr is migrating toward a server-owned runtime protocol with the TUI as one client. New work should not deepen the current server/TUI coupling.

Before adding state, API fields, events, commands, or socket messages, classify the feature:

- Shared runtime/session fact: belongs in server state and should be exposed through the JSON API/event path when practical.
- TUI presentation state: belongs only in the TUI/client layer.

Do not add new shared behavior that only works through the private TUI client socket. Use neutral server/API names, not UI-surface names like sidebar, row, card, or widget.

Examples:

- Pane/agent metadata, process state, terminal state, events: server/runtime.
- Sidebar layout, token placement, colors, selection, modals, mouse/viewport state: TUI/client.
- Workspace/tab/pane remain shared session organization for now, but avoid making them mandatory identity for unrelated runtime features.

### Stable client endpoint contract

The client-owned TUI endpoint generation is independent from the private same-install protocol. Generation 1 is the compatibility floor for Local, SSH, and Cloud connections and must remain available unless retired for a security reason.

- Named core codecs are immutable. Do not add, remove, reorder, or reinterpret fields or enum variants reachable from a published codec. Introduce a new codec name and keep the old codec as a fallback instead.
- Keep baseline JSON handshake and snapshot fields required. New JSON fields must be optional or have field-specific defaults; new enum values need an `Unknown` fallback where older clients can safely ignore them.
- Add server features through advertised API methods and optional snapshot data when possible. A missing optional feature must disable only that action, not reject the connection.
- Do not change the meaning or load-bearing parameter shape of an advertised endpoint method. If an old server could ignore a new field and incorrectly report success, add a new method name or a separately advertised capability and omit that field without it.
- Missing methods, rejections, timeouts, and unavailable servers are client-local outcomes. They must not disconnect other compatible servers, and typing in a pane must not dismiss their notices.
- Frozen endpoint fixtures, bincode digests, wire-tag tests, and `tests/fixtures/endpoint-method-shapes-v1.json` are compatibility contracts. Never update a generation-1 expectation merely to bless a wire change; create and negotiate a new codec or method.
- Stable and preview update manifests advertise `endpoint_generation`. Keep release tooling aligned so an older updater knows when a new server generation really requires replacement.
- Existing-value digests cannot detect an appended enum variant. Review every enum reachable from a frozen codec as append-closed even when tests remain green.

## Validation and packaging

Run cargo fmt --check, clippy with warnings denied, relevant Rust tests with the
Watchtower feature and affected Python runtime/packaging tests. Use synthetic
provider fixtures; do not run paid inference during CI. Native platform changes
need native tests. Report failures and unavailable checks; never change protocol
fixtures simply to bless a regression. Broader test limitations belong in the
preview validation report. Preserve vendored source provenance and patches.

Watchtower owns its version in distribution/watchtower/product.json and
src/distro.rs. Use scripts/watchtower_build_windows.ps1 on Windows and
scripts/watchtower_build_macos.py on Apple Silicon. Package from a clean source
commit for publication. Use Watchtower workflows, never upstream installers,
release scripts or update manifests. Release assets must contain only validated
allowlisted runtime files, attribution and hashes, never the live installation.

User-facing Watchtower docs live in README.md, CONTRIBUTING.md and
distribution/watchtower/. Preserve historical upstream docs as reference.
