# Results

Click **[Results]** at the top right of an agent pane to open that agent's output,
including when another split pane has keyboard focus. The button uses the existing
top border and hides when a pane has no top border or is too narrow. The pane's
**View results and outputs** action and **results** in Watchtower's menu remain
available. Results is the default tab; Activity shows
optional tool/commentary summaries, and Outputs provides Preview, Open, Save as,
Show folder and Copy path. Escape or Terminal returns to the underlying CLI.
The first version reads existing Codex session records; other providers keep
the Terminal workflow until a structured reader is available.

The task selector shows **Latest task** and up to 20 previous tasks from this
session's bounded history. Selecting a previous task shows only its own answer,
activity and output references; the composer is hidden and its draft retained.
Return to Latest task to send. The status at the top always describes the live
agent, even while viewing an older result. Large sessions can omit earlier data;
the selector indicates this. Output references are not versioned file copies:
previews read the file currently on disk.

Outputs shows an inline thumbnail or document preview next to the selected file.
**Preview** opens a larger view inside Results; Escape closes that view first.
Images use terminal color cells, Markdown is formatted, and PDF pages have
Previous/Next controls. Below 90 columns, use Preview instead of the inline pane.
**Open** still opens the browser preview. Ctrl+R reloads an edited local output.

Agent states distinguish Ready for task, Working, Needs your input, Result ready,
Interrupted and Status unknown. Waiting for agents appears only for a current,
outstanding structured wait call. No assistant prose or Radio message backlog is
used to guess it. Conflicting terminal and transcript state is shown as unknown.

Write another request here and click Send. Quick request asks the agent to handle
standalone images, writing and questions directly. Project task preserves the
project workflow. Neither mode expands permission or database/deployment scope.
Nothing is submitted on open or refresh. Busy or blocked agents cannot receive
a new request here; switch to Terminal for permission prompts or interruption.

The view binds to the original pane, session, working directory and Radio account
home. A binding change requires reopening. It reads bounded structured session
records, never parses terminal prose into fake results and never displays hidden
reasoning. Two-second refresh runs only while the view is open and reuses the
reader cache. Previous-turn results clear when a new request begins; they remain
available through the history selector within its retention limits.

File actions accept local image/document files up to 32 MB. In-app preview uses a
bounded image/PDF renderer (20 megapixels) and a 32-entry file-metadata cache.
Text preview is limited to 100,000 characters. Markdown preview
disables raw HTML, remote media and active links. Generated preview HTML stays
under plugin state; no transcripts or credentials belong in this package.
Native file URI support dispatches to this registered handler; it does not
enable arbitrary operating-system file execution.

This plugin shares the explicitly installed Accounts runtime: Textual 8.2.8,
Pillow 12.3.0 and pypdfium2 5.13.0. Run Accounts setup to install these into the
private virtual environment; missing preview dependencies produce a visible
message. It installs no packages and starts no agent on opening. Windows is the
initial supported host.
