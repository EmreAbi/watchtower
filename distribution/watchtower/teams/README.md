# Workspace templates

Open **menu → teams**, choose a template, an existing project folder, a workspace
name and a Codex or OpenCode account for each role. Each role has a searchable
**Model** selector; **Provider default** inherits that account's configuration.
**Review setup** shows the account, provider and selected model before creation.
**Create workspace & agents** creates a new workspace and dedicated role tabs.
Agents receive their role instructions and await your task. Existing workspaces
are not converted. A Project terminal is retained for your own commands.

| Template | Roles | Completion |
| --- | --- | --- |
| Quick Task | Assistant | Direct answer/output; no mandatory independent review |
| Research | Researcher, Reviewer | Source-linked evidence and independent review |
| Development | Controller, Worker, Reviewer | One implementation writer, checks and independent review |

Codex and OpenCode profiles can be mixed in one team. The completion gate verifies
the configured provider and session against the live Watchtower pane and Radio ledger. The same
account may be assigned to several roles; they still have separate sessions.
Permissions inherit the selected provider configuration. Workspace
Radio frequencies isolate the teams by default; cross-workspace messages remain
possible when explicitly addressed. Roles are workflow boundaries, not OS access
controls, and selected project folders are not automatically cloned or turned
into Git worktrees. Choose separate worktrees for concurrent changes to one repo.

Metadata lives under `WATCHTOWER_HOME/state/teams/<team-id>`, outside the project.
Only profile references and runtime paths are stored, never copied credentials.
Research outputs and review reports have separate directories. Template versions
are recorded; later template edits do not rewrite existing teams.

## Task workflow

Role briefs contain exact commands and paths. The owner runs `workflow.py begin`
with a task and acceptance criteria. Development then sends a Radio assignment
to its worker. `submit` records file hashes and/or the repository snapshot and
sends the reviewer a reference. `result` accepts only the configured independent
reviewer's live identity and the current digest. `complete` accepts only the
owner, after a fresh PASS on that digest. Changing a deliverable makes the gate
stale. CHANGES_REQUIRED cannot become PASS on the same revision.

These transitions happen when agents invoke the workflow commands. There is no
background supervisor and a terminal turn ending is not proof of reviewed task
completion. Use `workflow.py status --team-dir <absolute path> --id <task>` for
the recorded task state. Quick Task may answer directly; tracking is optional.
Review validates evidence identity and freshness, not the quality of a reviewer's
reasoning, and grants no deployment or database-write permission.

Global review is optional and no global reviewer is created automatically.
An explicit escalation without a configured independent reviewer blocks
completion. Resolve reviewer routing deliberately; never silently turn a blocked
decision into PASS. The engine was adapted from Watchtower's prior local review
workflow; only generic source is bundled, with no personal teams or history.

Setup reserves each creation/launch before dispatch. A timeout preserves known
workspace and pane IDs and does not retry. Opening a restored launch tab cannot
silently create another agent. Inspect partial setup before creating another
team. Launch-request confirmation is not a provider-ready health check.
