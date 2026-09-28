#!/usr/bin/env python3
"""Result-first view of one existing agent. All runtime and file work is delegated."""

from __future__ import annotations

import asyncio
import re
from typing import Any

from rich.text import Text
from textual import work
from textual.app import App, ComposeResult
from textual.containers import Grid, Horizontal, Vertical, VerticalScroll
from textual.widgets import Button, DataTable, Footer, Markdown, Select, Static, TabbedContent, TabPane, TextArea

from preview_view import PreviewPanel, PreviewScreen


POLL_SECONDS = 2
TAB_IDS = {"results", "activity", "outputs"}
STATE_LABELS = {
    "idle": "Ready for task", "working": "Working", "blocked": "Needs your input",
    "done": "Result ready", "unknown": "Status unknown",
}
ACTIVITY_LABELS = {"commentary": "Update", "tool": "Tool", "error": "Error",
                   "warning": "Notice", "status": "Status"}


def safe_text(value: Any, limit: int = 100_000) -> str:
    """Retain paragraphs without allowing terminal control characters or markup."""
    value = str(value or "")
    return re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f]", "", value)[:limit]


def label(value: Any, limit: int = 100) -> str:
    return " ".join(safe_text(value, limit * 2).split())[:limit]


def activity_text(items: list[dict]) -> Text:
    rendered = Text()
    for item in items[-100:]:
        if not isinstance(item, dict):
            continue
        kind = ACTIVITY_LABELS.get(item.get("kind"), "Update")
        stamp = label(item.get("timestamp"), 40)
        rendered.append(kind + (" · " + stamp if stamp else "") + "\n", style="bold #9ecbff")
        rendered.append(safe_text(item.get("text"), 8_000) + "\n\n")
    if not rendered.plain:
        rendered.append("No activity summaries for this turn.", style="dim")
    return rendered


class UnavailableResultsService:
    """A read-only explanation when the focused pane cannot provide Results."""

    def __init__(self, message: str):
        self.message = message

    def snapshot(self) -> dict:
        return {"agent": {"label": "Results", "state": "unknown", "supports_input": False,
                          "supports_results": False, "can_send": False, "message": self.message},
                "request": "", "result": "", "activity": [], "files": []}


class ResultsView(App):
    TITLE = "Watchtower · Results"
    BINDINGS = [("escape", "terminal", "Terminal"), ("ctrl+r", "refresh", "Refresh")]
    CSS = """
    Screen { background: #11151c; color: #d7deea; }
    #topbar { height: 3; padding: 0 1; }
    #agent-title { width: 1fr; height: 3; padding: 1 1; text-style: bold; color: #9ecbff; }
    #terminal { min-width: 10; width: 12; }
    #agent-state { height: auto; max-height: 2; padding: 0 2; color: #a8c59b; }
    #attention { height: auto; max-height: 6; margin: 0 2; padding: 1; border-left: thick #e5c07b; color: #f0d59d; }
    #history-row { height: 3; margin: 0 2; }
    #task-history { width: 1fr; }
    #history-hint { width: 26; padding: 1 1; color: #8995a7; }
    Screen.compact #history-hint { display: none; }
    #tabs { height: 1fr; margin: 0 1; }
    #tabs ContentSwitcher { height: 1fr; }
    TabPane { padding: 0; }
    #results-scroll, #activity-scroll { height: 1fr; padding: 1 2; background: #171d27; }
    .section-label { color: #8995a7; text-style: bold; height: 1; margin-bottom: 1; }
    #request { height: auto; max-height: 7; padding-left: 1; margin-bottom: 1; border-left: solid #344055; }
    #result { height: auto; margin: 0; padding: 0; }
    #result-empty { height: auto; color: #a6b6cb; padding: 1 0; }
    #activity-content { height: auto; }
    #output-body { height: 1fr; }
    #output-table { width: 42%; height: 1fr; margin-top: 1; background: #171d27; border: round #344055; }
    #inline-preview { width: 58%; height: 1fr; margin-top: 1; }
    Screen.compact #inline-preview { display: none; }
    Screen.compact #output-table { width: 100%; }
    DataTable > .datatable--header { background: #232c3b; color: #bcc7d8; }
    DataTable > .datatable--cursor { background: #304664; color: #ffffff; }
    #file-details { height: 3; padding: 0 1; color: #8995a7; }
    #file-actions { height: 3; grid-size: 5; grid-columns: 1fr; grid-rows: 3; grid-gutter: 0 1; }
    #file-actions Button { min-width: 0; width: 100%; }
    Screen.compact #file-actions { height: 6; grid-size: 3; }
    #composer { height: 8; margin: 0 2; }
    #prompt { height: 4; border: round #344055; background: #171d27; }
    #compose-actions { height: 3; }
    #request-mode { width: 22; margin-right: 1; }
    #compose-hint { width: 1fr; height: 3; color: #8995a7; }
    #send { width: 10; min-width: 8; }
    #notice { height: auto; max-height: 3; padding: 0 2; color: #b3c5db; }
    #notice.error { color: #ff929d; text-style: bold; }
    Footer { background: #1b2432; }
    """

    def __init__(self, service=None):
        super().__init__()
        if service is None:
            safe_error_type = ()
            try:
                from bridge import ResultsError, ResultsService
                safe_error_type = ResultsError
                service = ResultsService()
            except Exception as error:
                message = getattr(error, "message", "") if isinstance(error, safe_error_type) else ""
                service = UnavailableResultsService(message or "Results could not open for this pane. Return to Terminal and try again.")
        self.service = service
        self.snapshot: dict = {}
        self.busy = False
        self.reading = False
        self._preferences_loaded = False
        self._restoring_preference = False
        self._selected_file: str | None = None
        self._file_rows: list = []
        self._request = None
        self._result = None
        self._activity = None
        self._agent = None
        self._updating_files = False
        self._history_options = None
        self._displayed_turn = None

    def compose(self) -> ComposeResult:
        with Horizontal(id="topbar"):
            yield Static("Results", id="agent-title", markup=False)
            yield Button("Terminal", id="terminal")
        yield Static("Reading existing session…", id="agent-state", markup=False)
        yield Static("", id="attention", markup=False)
        with Horizontal(id="history-row"):
            yield Select([("Latest task", "latest")], value="latest", allow_blank=False, id="task-history")
            yield Static("Recent tasks in this session", id="history-hint", markup=False)
        with TabbedContent(initial="results", id="tabs"):
            with TabPane("Results", id="results"):
                with VerticalScroll(id="results-scroll"):
                    yield Static("LATEST REQUEST", id="request-label", classes="section-label")
                    yield Static("No request found yet.", id="request", markup=False)
                    yield Markdown("", id="result", open_links=False)
                    yield Static("Waiting for session data…", id="result-empty", markup=False)
            with TabPane("Activity", id="activity"):
                with VerticalScroll(id="activity-scroll"):
                    yield Static("No activity summaries for this turn.", id="activity-content", markup=False)
            with TabPane("Outputs", id="outputs"):
                with Horizontal(id="output-body"):
                    yield DataTable(id="output-table", cursor_type="row", show_row_labels=False, zebra_stripes=True)
                    yield PreviewPanel(getattr(self.service, "preview", None), id="inline-preview")
                yield Static("Files referenced by this agent appear here.", id="file-details", markup=False)
                with Grid(id="file-actions"):
                    yield Button("Preview", id="file-preview", disabled=True)
                    yield Button("Open", id="file-open", disabled=True)
                    yield Button("Save as…", id="file-save_as", disabled=True)
                    yield Button("Show folder", id="file-show_folder", disabled=True)
                    yield Button("Copy path", id="file-copy_path", disabled=True)
        with Vertical(id="composer"):
            yield TextArea("", id="prompt", show_line_numbers=False)
            with Horizontal(id="compose-actions"):
                yield Select([("Quick request", "quick"), ("Project task", "project")],
                             value="quick", allow_blank=False, id="request-mode")
                yield Static("Quick: answers, text or images.\nEnter adds a new line.", id="compose-hint", markup=False)
                yield Button("Send", variant="primary", id="send", disabled=True)
        yield Static("", id="notice", markup=False)
        yield Footer()

    def on_mount(self) -> None:
        self.query_one("#output-table", DataTable).add_columns("File", "Type")
        self.query_one("#composer").display = False
        self.query_one("#attention").display = False
        self.refresh_snapshot()
        self.set_interval(POLL_SECONDS, self.refresh_snapshot)

    def on_resize(self, event) -> None:
        self.default_screen.set_class(event.size.width < 90, "compact")
        self.call_after_refresh(self.update_inline_preview)

    def notify_status(self, message: str, *, error: bool = False) -> None:
        if self.query("#notice"):
            notice = self.query_one("#notice", Static)
            notice.set_class(error, "error")
            notice.update(Text(safe_text(message, 500)))
        if error:
            self.notify(safe_text(message, 500), title="Action could not finish", severity="error", timeout=6)

    def safe_error(self, error: Exception, fallback: str) -> None:
        try:
            from bridge import ResultsError
        except ImportError:
            ResultsError = ()
        safe = getattr(error, "message", "") if isinstance(error, ResultsError) else ""
        self.notify_status(safe or fallback, error=True)

    @work(group="results-read")
    async def refresh_snapshot(self) -> None:
        if self.reading or self.busy or not self.is_running or isinstance(self.screen, PreviewScreen):
            return
        self.reading = True
        try:
            if not self._preferences_loaded:
                self._preferences_loaded = True
                loader = getattr(self.service, "load_preference", None)
                if callable(loader):
                    try:
                        preference = await asyncio.to_thread(loader)
                    except Exception:
                        preference = None
                    tab = preference.get("tab") if isinstance(preference, dict) else preference
                    if tab in TAB_IDS:
                        self._restoring_preference = True
                        self.query_one("#tabs", TabbedContent).active = tab
                        self.call_later(self._finish_preference_restore)
            snapshot = await asyncio.to_thread(self.service.snapshot)
            if not self.busy:
                visible_fields = ("agent", "request", "result", "activity", "files", "turns", "display_key", "display_state", "selected_turn", "history_notice", "history_truncated")
                if any(snapshot.get(key) != self.snapshot.get(key) for key in visible_fields):
                    await self.apply_snapshot(snapshot)
                else:
                    self.snapshot = snapshot
        except Exception as error:
            self.safe_error(error, "Session could not be read. Open Terminal to check this agent.")
        finally:
            self.reading = False

    def _finish_preference_restore(self) -> None:
        self._restoring_preference = False

    async def apply_snapshot(self, snapshot: dict) -> None:
        self.snapshot = snapshot
        selected = snapshot.get("selected_turn")
        display_key = (selected, snapshot.get("display_key"))
        if display_key != self._displayed_turn:
            self._displayed_turn = display_key
            self._selected_file = None
            self._file_rows = []
            self.query_one("#inline-preview", PreviewPanel).set_file(None)
        options = [("Latest task · limited history" if snapshot.get("history_truncated") else "Latest task", "latest")]
        for turn in snapshot.get("turns", []):
            stamp = label(turn.get("timestamp"), 20).replace("T", " ")
            request = label(turn.get("request") or "Untitled task", 90)
            options.append(((stamp + " · " if stamp else "") + request, str(turn["key"])))
        history = self.query_one("#task-history", Select)
        # Prevent programmatic Select events from being mistaken for user navigation.
        with history.prevent(Select.Changed):
            if options != self._history_options:
                history.set_options(options)
                self._history_options = options
            history.value = selected or "latest"
        self.query_one("#history-row").display = len(options) > 1 or bool(snapshot.get("history_truncated"))
        hint = "Recent history · earlier data omitted" if snapshot.get("history_truncated") else "Recent tasks in this session"
        self.query_one("#history-hint", Static).update(hint)
        self.query_one("#request-label", Static).update("PREVIOUS REQUEST · READ ONLY" if selected else "LATEST REQUEST")
        if snapshot.get("history_notice"):
            self.notify_status(snapshot["history_notice"])
        agent = snapshot.get("agent") or {}
        if agent != self._agent:
            self._agent = dict(agent)
            title = label(agent.get("label") or "Agent")
            provider = label(agent.get("provider"), 30)
            self.query_one("#agent-title", Static).update(Text(title + (" · " + provider if provider else ""), no_wrap=True, overflow="ellipsis"))
            state = agent.get("state", "unknown")
            state_text = agent.get("status_label") or STATE_LABELS.get(state, "Status unknown")
            if agent.get("status_detail"):
                state_text += " · " + safe_text(agent["status_detail"], 500)
            elif state == "working":
                state_text += " · The final response will appear here."
            self.query_one("#agent-state", Static).update(state_text)
            message = safe_text(agent.get("message"), 1_000)
            if (state == "blocked" or agent.get("blocked")) and not message:
                message = "This agent needs attention. Open Terminal to respond to its prompt."
            self.query_one("#attention", Static).update(Text(message))
            self.query_one("#attention").display = bool(message)
        request = safe_text(snapshot.get("request"))
        if request != self._request:
            self._request = request
            self.query_one("#request", Static).update(Text(request or "No request found yet."))
        result = safe_text(snapshot.get("result"))
        if result != self._result:
            self._result = result
            await self.query_one("#result", Markdown).update(result)
        self.query_one("#result").display = bool(result)
        empty = "No final response yet."
        if selected:
            empty = "This task was interrupted before a final response." if snapshot.get("display_state") == "interrupted" else "No final response was recorded for this task."
        elif agent.get("state") == "working":
            empty = "Working on your request… Open Activity for progress summaries."
        elif not agent.get("supports_results", True):
            empty = "Structured results are not available for this tool yet. Open Terminal to view its response."
        self.query_one("#result-empty", Static).update(empty)
        self.query_one("#result-empty").display = not result
        activity = snapshot.get("activity") or []
        if activity != self._activity:
            self._activity = list(activity)
            self.query_one("#activity-content", Static).update(activity_text(activity))
        self.update_files(snapshot.get("files") or [])
        self.update_controls()

    def update_files(self, files: list[dict]) -> None:
        rows = [(str(item["id"]), label(item.get("name") or item.get("path") or "File", 160), label(item.get("kind"), 20))
                for item in files if isinstance(item, dict) and item.get("id") is not None]
        if rows != self._file_rows:
            table = self.query_one("#output-table", DataTable)
            self._updating_files = True
            table.clear()
            ids = [row[0] for row in rows]
            self._selected_file = self._selected_file if self._selected_file in ids else (ids[0] if ids else None)
            for key, name, kind in rows:
                table.add_row(Text(name), Text(kind), key=key)
            if self._selected_file:
                table.move_cursor(row=ids.index(self._selected_file), animate=False)
            self._updating_files = False
            self._file_rows = rows
            self.query_one("#tabs", TabbedContent).get_tab("outputs").label = f"Outputs ({len(rows)})" if rows else "Outputs"
        self.show_file_details()

    def selected_file(self) -> dict | None:
        return next((item for item in self.snapshot.get("files", []) if str(item.get("id")) == self._selected_file), None)

    def show_file_details(self) -> None:
        if not self.query("#file-details"):
            return
        item = self.selected_file()
        details = safe_text(item.get("path") or item.get("name"), 1_000) if item else "No output files found for this session."
        self.query_one("#file-details", Static).update(Text(details))
        self.update_inline_preview()

    def update_inline_preview(self, *, force: bool = False) -> None:
        if not self.query("#inline-preview"):
            return
        panel = self.query_one("#inline-preview", PreviewPanel)
        active = self.query_one("#tabs", TabbedContent).active == "outputs"
        if active and not self.default_screen.has_class("compact"):
            panel.set_file(self._selected_file, force=force)
        elif not self._selected_file:
            panel.set_file(None)

    def on_data_table_row_highlighted(self, event: DataTable.RowHighlighted) -> None:
        if event.data_table.id == "output-table" and not self._updating_files and event.row_key.value:
            self._selected_file = str(event.row_key.value)
            self.show_file_details()
            self.update_controls()

    def update_controls(self) -> None:
        if not self.query("#send"):
            return
        agent = self.snapshot.get("agent") or {}
        historical = self.snapshot.get("selected_turn") is not None
        self.query_one("#composer").display = bool(agent.get("supports_input")) and not historical and self.query_one("#tabs", TabbedContent).active == "results"
        ready = bool(agent.get("supports_input") and agent.get("can_send"))
        ready &= agent.get("state") not in ("working", "blocked") and not agent.get("blocked") and not historical
        self.query_one("#send", Button).disabled = self.busy or not ready or not self.query_one("#prompt", TextArea).text.strip()
        self.query_one("#prompt", TextArea).disabled = self.busy or not ready
        self.query_one("#request-mode", Select).disabled = self.busy
        self.query_one("#terminal", Button).disabled = self.busy
        self.query_one("#task-history", Select).disabled = self.busy
        for button in self.query("#file-actions Button"):
            button.disabled = self.busy or self.selected_file() is None

    def on_text_area_changed(self, event: TextArea.Changed) -> None:
        if event.text_area.id == "prompt":
            self.update_controls()

    def on_select_changed(self, event: Select.Changed) -> None:
        if event.select.id == "request-mode":
            hint = "Project task: normal workflow." if event.value == "project" else "Quick: answers, text or images."
            self.query_one("#compose-hint", Static).update(hint + "\nEnter adds a new line.")
        elif event.select.id == "task-history" and event.value is not Select.BLANK:
            key = None if event.value == "latest" else str(event.value)
            if key != self.snapshot.get("selected_turn"):
                self.change_task(key)

    @work(group="results-action")
    async def change_task(self, key: str | None) -> None:
        if self.busy:
            return
        self.busy = True
        self.update_controls()
        try:
            await asyncio.to_thread(self.service.select_turn, key)
            await self.apply_snapshot(await asyncio.to_thread(self.service.snapshot))
        except Exception as error:
            history = self.query_one("#task-history", Select)
            with history.prevent(Select.Changed):
                history.value = self.snapshot.get("selected_turn") or "latest"
            self.safe_error(error, "This task could not be loaded. Refresh Results and try again.")
        finally:
            self.busy = False
            self.update_controls()

    def on_markdown_link_clicked(self, event: Markdown.LinkClicked) -> None:
        event.stop()
        self.notify_status("Use Outputs to preview listed files. Open Terminal for other links.")

    def on_tabbed_content_tab_activated(self, event: TabbedContent.TabActivated) -> None:
        self.update_controls()
        self.update_inline_preview()
        if event.tabbed_content.id == "tabs" and self._preferences_loaded and not self._restoring_preference:
            tab = event.pane.id
            if tab in TAB_IDS:
                self.save_tab(tab)

    @work(exclusive=True, group="results-preference")
    async def save_tab(self, tab: str) -> None:
        save = getattr(self.service, "save_preference", None)
        if callable(save):
            try:
                await asyncio.to_thread(save, tab)
            except Exception:
                pass  # A display preference must never block the session.

    def on_button_pressed(self, event: Button.Pressed) -> None:
        name = event.button.id or ""
        if name == "terminal":
            self.action_terminal()
        elif name == "send":
            self.send_prompt()
        elif name.startswith("file-"):
            self.perform_file_action(name.removeprefix("file-"))

    def action_refresh(self) -> None:
        self.refresh_snapshot()
        self.update_inline_preview(force=True)

    def action_terminal(self) -> None:
        if self.busy:
            self.notify_status("Wait for the current action to finish before returning to Terminal.")
            return
        self.exit()

    @work(group="results-action")
    async def send_prompt(self) -> None:
        if self.busy or self.query_one("#send", Button).disabled:
            return
        text = self.query_one("#prompt", TextArea).text
        mode = str(self.query_one("#request-mode", Select).value)
        if mode not in {"quick", "project"}:
            return
        self.busy = True
        self.update_controls()
        try:
            result = await asyncio.to_thread(self.service.send, text, mode=mode)
            self.query_one("#prompt", TextArea).clear()
            self.notify_status(result.get("message") if isinstance(result, dict) and result.get("message") else "Sent to this agent.")
        except Exception as error:
            self.safe_error(error, "Delivery is unconfirmed. Check Terminal before sending again.")
        finally:
            self.busy = False
            self.update_controls()
            self.refresh_snapshot()

    @work(group="results-action")
    async def perform_file_action(self, action: str) -> None:
        if self.busy or self._selected_file is None or action not in {"preview", "open", "save_as", "show_folder", "copy_path"}:
            return
        file_id = self._selected_file
        if action == "preview":
            self.push_screen(PreviewScreen(getattr(self.service, "preview", None), file_id),
                             lambda _: self.refresh_snapshot())
            return
        self.busy = True
        self.update_controls()
        self.notify_status("Opening output…" if action in {"preview", "open", "show_folder"} else "Processing output…")
        try:
            result = await asyncio.to_thread(self.service.file_action, file_id, action)
            message = result.get("message") if isinstance(result, dict) else result
            self.notify_status(message or "File action finished.")
        except Exception as error:
            self.safe_error(error, "File action could not finish. The file may have moved or be unavailable.")
        finally:
            self.busy = False
            self.update_controls()


def main() -> int:
    ResultsView().run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
