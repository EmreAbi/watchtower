# Watchtower Accounts

Open **menu → accounts** to manage Codex, OpenCode, Gemini and Claude profiles without creating
a workspace. The existing popup close control or Escape returns to your work.
This screen uses the public plugin API; no server/protocol upgrade is needed.

Run `setup-accounts.cmd` once in the portable package to install the pinned
Textual UI and the shared Results preview dependencies (Pillow and pypdfium2)
in Watchtower's private state and link the plugin. Python 3.10+ and
the provider CLIs are prerequisites. Setup downloads UI dependencies only;
opening Accounts does not install anything. An existing private Radio Textual
environment can also be used.

## Using accounts

- **Add account** creates an isolated provider home. Give each Codex account a
  different profile ID, for example `personal` and `work`.
- **Connect** shows Codex browser sign-in with **Open browser**, **Copy link**
  and **Cancel** buttons. Finish signing in in the browser; the popup reports
  completion. The localhost callback is handled automatically and is not a
  page to open manually. Copy link copies the entire URL, including wrapped
  query parameters. OpenCode keeps its native Zen connection flow. Gemini opens
  its native Google sign-in: use `/auth` to change an existing login, then `/quit`
  to return to Accounts. Claude opens its native browser login and returns when
  that command finishes. Credentials
  remain in the provider's home, never in Watchtower profile metadata.
- **Refresh** reads the selected Codex account and quota through its local app
  server. The table redraws cached data every 30 seconds. The actual check time
  is displayed; a missing quota is unknown, not zero.
- **Billing** opens the official OpenCode account page. Zen balance is not
  available in this first version; an auth file is not proof of a valid login.
- **Set default** chooses the initial profile in this screen. It does not
  rewrite personal team launchers or change existing agents.
- **New agent** chooses a profile, model, Radio handle and existing workspace, and
  starts a fresh session in a dedicated new tab. It sends no task prompt.
  Existing handles cannot be taken over. A launch request is single-use and
  expires after ten minutes; reopening a restored tab cannot repeat it.
  Codex and OpenCode have searchable model selectors. **Provider default** keeps
  the provider's own choice; changing accounts clears the previous model choice.
  Codex uses that profile's local model cache. OpenCode reads its enabled models
  from a short-lived local server for the selected profile and waits for plugin
  model discovery before displaying the list. The owned server is then stopped;
  discovery sends no model request. A catalog entry is not a balance or entitlement
  check. Gemini and Claude keep their native model selection for now.
- **Tools** manages the shared Codex CLI installation, independently of the
  selected account. **Check updates** compares installed and available versions;
  **Update Codex** runs one explicit update for the supported native Windows
  installation. Concurrent update attempts are refused. Existing agents keep
  running; newly opened agents use the updated installation. Codex agents
  launched through Accounts disable their individual startup update prompts.

Gemini requires CLI 0.61.0 or newer for the verified home isolation. Its profile
directory is the actual `.gemini` configuration directory; Accounts supplies
the parent through `GEMINI_CLI_HOME`. Claude uses `CLAUDE_CONFIG_DIR` directly.
Existing provider homes appear automatically without copying their credentials.
Gemini Refresh reports credential presence only. Claude Refresh reads its native
CLI's masked account/plan metadata; neither provider exposes quota in this view.

An explicit Gemini New agent request adds a scoped SessionStart hook to that
profile's settings, preserving other settings/hooks and retaining a backup.
The hook supplies the verified Radio handle/frequency/session context without
submitting a task. Disabled hooks are preserved and prevent setup; workspace or
administrator settings can also disable hooks. The user must enable hooks in
those settings to use Gemini with Radio. Normal Gemini launches outside Radio
receive no added context. Login and account listing do not install this hook.

OpenCode agent launches add the bundled session-selection hook through a child-only
CLI configuration overlay. It preserves existing plugin settings and Radio's
briefing configuration; profile files remain unchanged. The hook reports the
selected root session to Watchtower so Radio and team review can verify identity.
Explicitly disabled hooks and pending legacy CLI configuration migration block
this setup with an explanation instead of silently bypassing those settings.

Profiles discovered from existing homes can be used without copying credentials.
An explicit new-agent action registers that home in the private Radio ledger.
Radio assignments describe configured profiles, not verified authentication of
an already-running process. Reconnecting a shared home may affect other agents
using it; add a separate profile when connecting a different account.

## Scope

Provider settings and new account credentials are isolated. Automatic account
rotation, per-role/workspace defaults and billing transactions are outside
this version. Changing a
default applies to future starts from Accounts only. Provider-native model
selection remains available inside the newly opened agent.

## Switch account

Select the source account, click **Switch account…**, choose another account
for the same tool, and select agents (or **All eligible**). **Review switch**
shows the exact selection; only **Switch selected** starts the operation.
Opening or closing the dialog does not modify profiles, sessions or agents.

Session-preserving transfer currently supports native Codex agents in Windows
PowerShell panes. OpenCode, Gemini and Claude are listed with an explicit
unsupported reason; choosing another provider or starting a fresh conversation
is never a fallback. Working, blocked, missing or ambiguous agents are excluded.
The target login is checked before any source agent exits. Model, reasoning
effort, supported approval/sandbox policy, pane identity, workspace and Radio
role are retained. Target-profile settings and installed integrations otherwise
remain those of that account; credentials are never copied.

The server's guarded `agent.quit-if-idle` method checks the selected terminal,
conversation and idle-state sequence before sending Codex's native exit key.
Older servers require a restart with the updated executable; there is no
unguarded prompt/kill fallback. The transfer waits for the original pane to
return to a verified shell, copies the conversation and resumes its exact UUID.
It does not close panes or restart the Watchtower server. Do not type into a
selected pane during the short switch. If another task starts before the exit
check, the operation is rejected.

Windows shell readiness is verified against the actual process tree, including
Radio/Python wrappers. The switch runner confirms the native Codex process it
started before publishing the resumed conversation identity; a target profile
does not need an extra global session hook for this transfer. Recent Codex
settings-change records take precedence over older turn metadata, so changing a
model or permissions while idle is retained when switching accounts.
Success also requires new resume metadata and a visible Codex input prompt.
First-use folder trust, login or Windows setup screens require attention in the
agent pane and are not counted as a completed switch.

An existing target conversation must be an unchanged prefix of the source.
Divergent copies and conversations open in another pane are rejected. Source
history is retained. Transfers are serial; an uncertain outcome stops the batch
and is displayed as **needs_attention**, never success. Inspect that pane before
retrying: the result states whether the source or target account is assigned.
The switch changes the Radio binding for subsequent Radio restores; manually
rerunning an older custom/team launcher can explicitly choose its original
profile again. The account default and saved team definitions are not rewritten.
Native `resume_agents_on_restore` does not carry provider-home overrides; use
Radio restore for account-aware restarts (the normal Watchtower configuration
keeps native automatic agent restore disabled).

All runtime state lives outside this source/package directory. Bundled Radio
0.7.1 stays byte-for-byte unchanged. The Accounts runner uses native provider
argv on Windows to avoid PowerShell policy and command-shim quoting problems.

## macOS preview

Accounts can connect provider profiles and launch new agents on macOS. Codex
browser sign-in includes Open browser and Copy link (`pbcopy`); profile homes
stay separate and no credentials are transferred between them. Providers must
already be installed and available on PATH.

Switch account for existing agents is disabled on macOS in this preview because
its process-ownership and same-session resume checks require Windows PowerShell.
Use New agent with the desired account instead. The central Codex updater is
also Windows-only; macOS installations remain managed by their original installer.
