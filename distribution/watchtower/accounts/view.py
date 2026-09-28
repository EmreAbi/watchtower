#!/usr/bin/env python3
"""Watchtower Accounts: presentation only; credentials stay with each provider."""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
import math
import os
import re
from typing import Any, Callable
import webbrowser

from rich.text import Text
from textual import work
from textual.app import App, ComposeResult
from textual.containers import Grid, Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Button, DataTable, Footer, Input, Label, Select, Static


REFRESH_SECONDS = 30
PROVIDERS = {"codex": "Codex", "opencode": "OpenCode", "gemini": "Gemini", "claude": "Claude"}
CONNECT_GUIDANCE = {
    "gemini": "Connect opens Gemini. Choose Sign in with Google, then /quit to return to Accounts.",
    "claude": "Connect opens Claude's subscription sign-in in your browser, then returns to Accounts.",
}
STATES = {
    "unknown": "Not checked",
    "connected": "Connected",
    "not_connected": "Not connected",
    "credential_present": "Credentials present",
    "unavailable": "Unavailable",
}


def plain(value: Any, limit: int = 120) -> str:
    """Never interpret account metadata as markup or terminal control sequences."""
    return " ".join(re.sub(r"[\x00-\x1f\x7f-\x9f]", " ", str(value or "")).split())[:limit]


def percentage(value: Any) -> str:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if math.isfinite(value) and 0 <= value <= 100:
            return f"{value:g}%"
    return "—"


def moment(value: Any) -> datetime | None:
    try:
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            return datetime.fromtimestamp(value, timezone.utc)
        if isinstance(value, str) and value:
            result = datetime.fromisoformat(value.replace("Z", "+00:00"))
            return result.replace(tzinfo=timezone.utc) if result.tzinfo is None else result
    except (ValueError, OverflowError, OSError):
        pass
    return None


def age(value: Any) -> str:
    observed = moment(value)
    if observed is None:
        return "Not checked"
    seconds = max(0, int((datetime.now(timezone.utc) - observed).total_seconds()))
    if seconds < 60:
        return "Just now"
    if seconds < 3600:
        return f"{seconds // 60}m ago"
    return f"{seconds // 3600}h ago"


def quota_summary(status: dict) -> str:
    values = [percentage(window.get("remaining_percent"))
              for window in status.get("quota_windows", []) if isinstance(window, dict)]
    known = [value for value in values if value != "—"]
    return " / ".join(known) + " left" if known else "—"


def connection_label(status: dict) -> str:
    if status.get("state") == "connected" and status.get("auth_type") == "apiKey":
        return "API key configured"
    return STATES.get(status.get("state"), "Not checked")


def profile_details(profile: dict | None, default_id: str | None) -> Text:
    text = Text()
    if profile is None:
        text.append("Keep your accounts in one place\n\n", style="bold")
        text.append("Add a Codex, OpenCode, Gemini or Claude profile, then connect it.\n\n")
        text.append("Existing accounts appear automatically. Refresh checks the selected account; opening this screen does not sign in or launch an agent.", style="dim")
        return text
    status = profile.get("status") or {}
    provider = PROVIDERS.get(profile.get("provider"), plain(profile.get("provider")))
    text.append(plain(profile.get("label") or profile.get("id")) + "\n", style="bold #9ecbff")
    text.append(provider + ("  ·  Default for new agents" if profile.get("id") == default_id else "") + "\n\n", style="dim")
    text.append("CONNECTION\n", style="bold")
    text.append(connection_label(status) + "\n")
    if status.get("auth_type") == "chatgpt":
        text.append("Sign-in: ChatGPT account\n", style="dim")
    if status.get("state") == "credential_present":
        text.append("Provider credentials found; validity has not been verified.\n", style="dim")
    if status.get("email_masked") or status.get("email_domain"):
        text.append(plain(status.get("email_masked") or status.get("email_domain")) + "\n")
    if status.get("plan"):
        text.append("Plan: " + plain(status["plan"]) + "\n")
    text.append("Checked: " + age(status.get("observed_at")) + "\n", style="dim")
    guidance = CONNECT_GUIDANCE.get(profile.get("provider"))
    if guidance:
        text.append(guidance + "\n", style="dim")
    text.append("\n")
    text.append("USAGE\n", style="bold")
    windows = [item for item in status.get("quota_windows", []) if isinstance(item, dict)]
    if windows:
        for window in windows:
            text.append(plain(window.get("name") or "Quota") + ": " + percentage(window.get("remaining_percent")) + " remaining\n")
            reset = moment(window.get("resets_at"))
            text.append("Resets " + (reset.astimezone().strftime("%d %b, %H:%M %Z") if reset else "—") + "\n", style="dim")
    else:
        text.append("No quota data available.\n", style="dim")
        if profile.get("provider") in ("gemini", "claude"):
            text.append(f"{provider} usage limits are not reported in this view.\n", style="dim")
    if profile.get("provider") == "opencode":
        text.append("Zen balance: unavailable in this view.\n", style="dim")
    if status.get("error"):
        # Backend returns fixed safe error codes; never render provider diagnostics.
        if profile.get("provider") == "gemini" and status["error"] == "provider_status_not_verified":
            text.append("Gemini account validity is not checked here.\n", style="dim")
        else:
            text.append("Status could not be verified. Try Refresh or Connect.\n", style="#e5c07b")
    text.append("\nCONFIGURED AGENTS\n", style="bold")
    bindings = [item for item in profile.get("bindings", []) if isinstance(item, dict)]
    if not bindings:
        text.append("No Radio assignments.\n", style="dim")
    for binding in bindings:
        text.append(plain(binding.get("handle") or "Agent", 50) + "\n")
        text.append("  " + plain(binding.get("workspace")) + " / " + plain(binding.get("pane")) + "\n", style="dim")
    text.append("\nAssignments identify configured profiles. The account used by a running session is not verified.", style="dim")
    return text


class AddProfile(ModalScreen[dict | None]):
    BINDINGS = [("escape", "cancel", "Cancel")]

    def compose(self) -> ComposeResult:
        with VerticalScroll(classes="dialog"):
            yield Label("Add account", classes="dialog-title")
            yield Static("Each new profile gets its own provider settings and sign-in.", classes="hint")
            yield Label("Tool")
            yield Select([(label, name) for name, label in PROVIDERS.items()], value="codex", allow_blank=False, id="provider")
            yield Static("", id="provider-guide", classes="hint", markup=False)
            yield Label("Profile ID")
            yield Input(placeholder="e.g. work or personal", id="profile-name")
            yield Label("Display name")
            yield Input(placeholder="e.g. Codex · Work", max_length=64, id="profile-label")
            yield Static("", id="form-error", classes="error")
            with Horizontal(classes="dialog-buttons"):
                yield Button("Cancel", id="cancel")
                yield Button("Add account", variant="primary", id="save")

    def on_mount(self) -> None:
        self.query_one("#profile-name", Input).focus()

    def on_select_changed(self, event: Select.Changed) -> None:
        if event.select.id == "provider":
            self.query_one("#provider-guide", Static).update(CONNECT_GUIDANCE.get(str(event.value), ""))

    def action_cancel(self) -> None:
        self.dismiss(None)

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "cancel":
            self.dismiss(None)
        elif event.button.id == "save":
            name = self.query_one("#profile-name", Input).value.strip()
            if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,47}", name):
                self.query_one("#form-error", Static).update("Use 1–48 letters, numbers, hyphens or underscores.")
                return
            self.dismiss({"name": name, "provider": str(self.query_one("#provider", Select).value),
                          "label": self.query_one("#profile-label", Input).value.strip() or name})


class NewAgent(ModalScreen[dict | None]):
    BINDINGS = [("escape", "cancel", "Cancel")]

    def __init__(self, profiles: list[dict], selected: str | None, workspaces: list[dict], *, model_loader: Callable | None = None):
        super().__init__()
        self.profiles, self.selected, self.workspaces = profiles, selected, workspaces
        self.model_loader = model_loader
        self._model_request = 0
        self._model_profile = None
        self._model_ids: set[str] = set()

    def compose(self) -> ComposeResult:
        focused = next((item["id"] for item in self.workspaces if item.get("focused") or item.get("active")), None)
        focused = focused or next((item["id"] for item in self.workspaces if item["id"] == os.environ.get("HERDR_WORKSPACE_ID")), None)
        with Vertical(classes="dialog agent-dialog"):
            yield Label("New agent", classes="dialog-title")
            with VerticalScroll(id="agent-fields"):
                yield Static("Opens a fresh Radio agent in a new tab. Existing agents keep their current accounts.", classes="hint")
                yield Label("Account")
                yield Select([(f"{plain(item.get('label') or item['id'])} · {PROVIDERS.get(item['provider'], item['provider'])}", item["id"])
                              for item in self.profiles], value=self.selected or self.profiles[0]["id"], allow_blank=False, id="launch-profile")
                yield Label("Model · type to search")
                yield Select([("Provider default", "")], value="", allow_blank=False, id="launch-model", type_to_search=True)
                yield Static("", id="model-notice", classes="hint", markup=False)
                yield Label("Radio handle")
                yield Input(placeholder="e.g. experiment-review", id="agent-handle")
                yield Label("Workspace")
                yield Select([(f"{plain(item.get('name') or item['id'])} ({item['id']})", item["id"])
                              for item in self.workspaces], value=focused or self.workspaces[0]["id"], allow_blank=False, id="launch-workspace")
            yield Static("", id="form-error", classes="error")
            with Horizontal(classes="dialog-buttons"):
                yield Button("Cancel", id="cancel")
                yield Button("Start agent", variant="primary", id="launch")

    def on_mount(self) -> None:
        self.query_one("#launch-profile", Select).focus()
        self.load_profile_models()

    def on_select_changed(self, event: Select.Changed) -> None:
        if event.select.id == "launch-profile" and self.is_mounted:
            self.load_profile_models()

    def load_profile_models(self) -> None:
        profile = str(self.query_one("#launch-profile", Select).value)
        if profile == self._model_profile:
            return
        self._model_profile = profile
        self._model_request += 1
        self._model_ids.clear()
        select = self.query_one("#launch-model", Select)
        select.set_options([("Provider default", "")])
        select.value = ""
        self.query_one("#model-notice", Static).update("Loading models… Provider default is available.")
        self.fetch_models(profile, self._model_request)

    @work(group="model-catalog")
    async def fetch_models(self, profile: str, request: int) -> None:
        try:
            catalog = await asyncio.to_thread(self.model_loader, profile) if self.model_loader else {}
            rows = catalog.get("models", [])
            models = {row["id"]: plain(row.get("label") or row["id"], 160)
                      for row in rows if isinstance(row, dict) and isinstance(row.get("id"), str) and row["id"]}
            notice = plain(catalog.get("notice"), 280) or (f"{len(models)} models. Type a name to search." if models else "Use the provider's default model.")
        except Exception:
            models = {}
            notice = "Could not load models. Provider default is still available."
        if not self.is_mounted or request != self._model_request or profile != str(self.query_one("#launch-profile", Select).value):
            return
        self._model_ids = set(models)
        select = self.query_one("#launch-model", Select)
        select.set_options([("Provider default", ""), *((label, identity) for identity, label in models.items())])
        select.value = ""
        self.query_one("#model-notice", Static).update(Text(notice))

    def action_cancel(self) -> None:
        self.dismiss(None)

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "cancel":
            self.dismiss(None)
        elif event.button.id == "launch":
            handle = self.query_one("#agent-handle", Input).value.strip()
            if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,47}", handle):
                self.query_one("#form-error", Static).update("Use 1–48 letters, numbers, hyphens or underscores.")
                return
            profile = str(self.query_one("#launch-profile", Select).value)
            model = self.query_one("#launch-model", Select).value
            # A queued account change must never carry the previous account's model.
            if profile != self._model_profile or model not in self._model_ids:
                model = None
            self.dismiss({"profile": profile, "handle": handle, "model": model,
                          "workspace": str(self.query_one("#launch-workspace", Select).value)})


class BrowserLoginModal(ModalScreen[dict]):
    """Own one explicit Codex login, including startup and cancellation races."""

    BINDINGS = [("escape", "cancel", "Cancel")]

    def __init__(self, login, label: str, *, browser_opener: Callable, clipboard_writer: Callable):
        super().__init__()
        self.login, self.profile_label = login, label
        self.browser_opener, self.clipboard_writer = browser_opener, clipboard_writer
        self._login_url: str | None = None
        self._start_task: asyncio.Task | None = None
        self._cleanup_task: asyncio.Task | None = None
        self._login_closing = False
        self._polling = False
        self._external_action_pending = False
        self._result: dict | None = None

    def compose(self) -> ComposeResult:
        with VerticalScroll(classes="dialog login-dialog"):
            yield Label("Connect Codex", classes="dialog-title")
            yield Static(Text(plain(self.profile_label)), id="login-profile")
            yield Static("Sign in in your browser. The local callback is handled automatically.", classes="hint")
            yield Static("Preparing sign-in…", id="login-status", markup=False)
            yield Static("When ready, choose Open browser or copy the complete link into your browser.", id="login-help", classes="hint", markup=False)
            with Horizontal(classes="dialog-buttons login-buttons"):
                yield Button("Open browser", variant="primary", id="login-open", disabled=True)
                yield Button("Copy link", id="login-copy", disabled=True)
                yield Button("Cancel", id="login-cancel")

    def on_mount(self) -> None:
        self.start_login()
        self.set_interval(1.0, self.poll_login)

    def status(self, message: str) -> None:
        if self.is_mounted and self.query("#login-status"):
            self.query_one("#login-status", Static).update(Text(message))

    def controls(self) -> None:
        if not self.is_mounted or not self.query("#login-open"):
            return
        disabled = self._login_closing or self._result is not None or not self._login_url or self._external_action_pending
        self.query_one("#login-open", Button).disabled = bool(disabled)
        self.query_one("#login-copy", Button).disabled = bool(disabled)
        self.query_one("#login-cancel", Button).disabled = self._login_closing

    def finish(self, result: dict) -> None:
        self._result = result
        self._login_url = None
        self.status(plain(result.get("message")) or ("Sign-in complete." if result.get("state") == "success" else "Sign-in could not be completed."))
        if self.is_mounted:
            self.query_one("#login-cancel", Button).label = "Close"
            self.query_one("#login-help", Static).update("Close to return to Accounts." if result.get("state") == "success" else "Close and select Connect to try again.")
            self.controls()

    @work(group="login-start")
    async def start_login(self) -> None:
        # Shield the start task from screen worker cancellation. Cleanup waits
        # for this bounded task, then closes again to catch a late process.
        self._start_task = asyncio.create_task(asyncio.to_thread(self.login.start))
        try:
            result = await asyncio.shield(self._start_task)
            if self._login_closing:
                return
            url = result.get("url") if isinstance(result, dict) else None
            if not isinstance(url, str) or not url:
                raise ValueError("Missing login link")
            self._login_url = url
            self.status("Ready. Open your browser to sign in.")
            self.controls()
        except asyncio.CancelledError:
            raise
        except Exception as error:
            if not self._login_closing:
                from browser_login import BrowserLoginError
                message = error.message if isinstance(error, BrowserLoginError) else "Sign-in could not start. Confirm Codex is installed and try again."
                self.finish({"state": "error", "message": message})

    @work(group="login-poll")
    async def poll_login(self) -> None:
        if self._login_closing or self._polling or self._result is not None or not self._login_url:
            return
        self._polling = True
        try:
            result = await asyncio.to_thread(self.login.poll)
            if self._login_closing or result is None:
                return
            if not isinstance(result, dict) or result.get("state") not in ("success", "error", "cancelled"):
                raise ValueError("Unexpected login status")
            self.finish(result)
        except Exception:
            if not self._login_closing:
                self.finish({"state": "error", "message": "Sign-in status could not be checked. Close and try again."})
        finally:
            self._polling = False

    def on_button_pressed(self, event: Button.Pressed) -> None:
        event.stop()
        if event.button.id == "login-cancel":
            self.action_cancel()
        elif event.button.id in ("login-open", "login-copy"):
            self.external_action(event.button.id == "login-copy")

    @work(group="login-external")
    async def external_action(self, copy: bool) -> None:
        if self._login_closing or self._result is not None or not self._login_url or self._external_action_pending:
            return
        self._external_action_pending = True
        self.controls()
        url = self._login_url
        try:
            success = await asyncio.to_thread(self.clipboard_writer if copy else self.browser_opener, url)
            if not self._login_closing and self._result is None:
                if success:
                    self.status("Complete link copied. Paste it into your browser to sign in." if copy else "Browser opened. Waiting for sign-in…")
                else:
                    self.status("The link could not be copied. Try Open browser." if copy else "Browser could not open. Use Copy link and paste it into your browser.")
        except Exception:
            if not self._login_closing and self._result is None:
                self.status("The link could not be copied. Try Open browser." if copy else "Browser could not open. Use Copy link instead.")
        finally:
            self._external_action_pending = False
            self.controls()

    def action_cancel(self) -> None:
        if self._login_closing:
            return
        self._login_closing = True
        self._login_url = None
        self.controls()
        if self._result is None:
            self.status("Canceling sign-in…")
        self.cancel_and_dismiss()

    async def _finish_cleanup(self) -> None:
        if not self._result or self._result.get("state") != "success":
            try:
                await asyncio.to_thread(self.login.cancel)
            except Exception:
                pass
        if self._start_task is not None:
            try:
                await asyncio.shield(self._start_task)
            except (Exception, asyncio.CancelledError):
                pass
        try:
            await asyncio.to_thread(self.login.close)
        except Exception:
            pass

    async def cleanup(self) -> None:
        self._login_closing = True
        self._login_url = None
        if self._cleanup_task is None:
            self._cleanup_task = asyncio.create_task(self._finish_cleanup())
        await asyncio.shield(self._cleanup_task)

    @work(group="login-cancel")
    async def cancel_and_dismiss(self) -> None:
        await self.cleanup()
        if self.is_mounted:
            self.dismiss(self._result or {"state": "cancelled", "message": "Sign-in cancelled."})

    async def on_unmount(self) -> None:
        # Also handles the host closing its popup during provider startup.
        await self.cleanup()


class CodexTools(ModalScreen[None]):
    """Manage the shared CLI without changing any account or running agent."""

    BINDINGS = [("escape", "close", "Close")]
    STATE_LABELS = {
        "unchecked": "Not checked", "current": "Up to date",
        "update_available": "Update available", "unsupported": "Managed externally",
        "unavailable": "Unavailable", "busy": "Another update is running",
        "updated": "Updated", "error": "Could not complete the action",
    }

    def __init__(self, service):
        super().__init__()
        self.service = service
        self.tool_status: dict = {}
        self.tool_action: str | None = None

    def compose(self) -> ComposeResult:
        with VerticalScroll(classes="dialog tools-dialog"):
            yield Label("Codex tools", classes="dialog-title")
            yield Static("Codex is shared by all accounts. Updates apply to newly started agents.", classes="hint")
            yield Static("Installed: —\nLatest: —", id="tool-versions", markup=False)
            yield Static("Reading installed version…", id="tool-status", markup=False)
            with Horizontal(classes="dialog-buttons tools-buttons"):
                yield Button("Check updates", id="tool-check")
                yield Button("Update Codex", variant="primary", id="tool-update", disabled=True)
                yield Button("Close", id="tool-close")

    def on_mount(self) -> None:
        self.start_action("status")

    def set_status(self, message: str) -> None:
        if self.is_mounted and self.query("#tool-status"):
            self.query_one("#tool-status", Static).update(Text(message))

    def update_controls(self) -> None:
        if not self.is_mounted or not self.query("#tool-check"):
            return
        self.query_one("#tool-check", Button).disabled = self.tool_action is not None
        self.query_one("#tool-update", Button).disabled = self.tool_action is not None or self.tool_status.get("can_update") is not True
        self.query_one("#tool-close", Button).disabled = self.tool_action == "update"

    def apply_status(self, status: dict) -> None:
        if not self.is_mounted:
            return
        self.tool_status = status
        installed = plain(status.get("installed_version")) or "Not available"
        latest = plain(status.get("latest_version")) or "Not checked"
        self.query_one("#tool-versions", Static).update(Text(f"Installed: {installed}\nLatest: {latest}"))
        label = self.STATE_LABELS.get(status.get("state"), "Status unavailable")
        message = plain(status.get("message"), 280)
        self.set_status(label + ("\n" + message if message else ""))

    def start_action(self, action: str) -> None:
        if self.tool_action is not None:
            return
        if action == "update" and self.tool_status.get("can_update") is not True:
            return
        self.tool_action = action
        self.update_controls()
        self.set_status({"status": "Reading installed version…", "check": "Checking for Codex updates…",
                         "update": "Updating Codex… Keep this window open until the update finishes."}[action])
        self.run_action(action)

    @work(group="tool-action")
    async def run_action(self, action: str) -> None:
        try:
            if action == "status":
                result = await asyncio.to_thread(self.service.status, check_latest=False)
            elif action == "check":
                result = await asyncio.to_thread(self.service.check)
            else:
                result = await asyncio.to_thread(self.service.update)
            if not isinstance(result, dict):
                raise ValueError("Unexpected tool status")
            self.apply_status(result)
        except Exception:
            # Only the service's allowlisted result messages reach the screen.
            self.set_status("Codex tools could not complete this action. Try Check updates again.")
        finally:
            self.tool_action = None
            self.update_controls()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        event.stop()
        if event.button.id == "tool-close":
            self.action_close()
        elif event.button.id == "tool-check":
            self.start_action("check")
        elif event.button.id == "tool-update":
            self.start_action("update")

    def action_close(self) -> None:
        if self.tool_action == "update":
            self.set_status("Updating Codex… Wait for the result before closing this window.")
            return
        self.dismiss(None)


class SwitchAccount(ModalScreen[dict | None]):
    """Review a same-provider account move before any agent is restarted."""
    BINDINGS = [("escape", "close", "Close")]

    def __init__(self, service, source: dict):
        super().__init__()
        self.service, self.source = service, dict(source)
        self.targets: list[dict] = []
        self.agents: list[dict] = []
        self.selected: set[str] = set()
        self.stage = "loading"
        self.plan = None
        self.result = None
        self._generation = 0
        self._apply_task: asyncio.Task | None = None

    def compose(self) -> ComposeResult:
        with Vertical(classes="dialog switch-dialog"):
            yield Static("Switch account", classes="dialog-title")
            yield Static("From: " + plain(self.source.get("label") or self.source["id"]) + " · "
                         + PROVIDERS.get(self.source.get("provider"), plain(self.source.get("provider"))),
                         id="switch-source", markup=False)
            with Vertical(id="switch-picker"):
                yield Label("To account · same tool")
                yield Select([], id="switch-target", prompt="Choose a target account", disabled=True)
                yield Static("Choose agents to move. Busy or unverifiable agents stay on their current account.",
                             id="switch-guide", markup=False)
                yield DataTable(id="switch-agents", cursor_type="row", zebra_stripes=True, show_row_labels=False)
                yield Static("", id="switch-agent-reason", markup=False)
                with Horizontal(id="switch-selection-actions"):
                    yield Static("0 selected", id="switch-count", markup=False)
            with VerticalScroll(id="switch-summary-scroll"):
                yield Static("", id="switch-summary", markup=False)
            yield Static("Reading configured agents…", id="switch-status", markup=False)
            with Horizontal(classes="dialog-buttons switch-buttons"):
                yield Button("All eligible", id="switch-all")
                yield Button("Clear", id="switch-clear")
                yield Button("Back", id="switch-back")
                yield Button("Review switch", variant="primary", id="switch-review")
                yield Button("Switch selected", variant="primary", id="switch-apply")
                yield Button("Close", id="switch-close")

    def on_mount(self) -> None:
        table=self.query_one("#switch-agents",DataTable)
        for label,width in (("",3),("Agent",18),("Workspace",12),("State / reason",25)):
            table.add_column(label,width=width)
        self.controls()
        self.load_candidates()

    def controls(self) -> None:
        if not self.is_mounted:
            return
        choosing=self.stage in {"loading","select","preparing"}
        self.query_one("#switch-picker").display=choosing
        self.query_one("#switch-summary-scroll").display=not choosing
        for identifier in ("switch-target","switch-agents","switch-all","switch-clear"):
            self.query_one("#"+identifier).disabled=self.stage!="select"
        self.query_one("#switch-all",Button).disabled=self.stage!="select" or not any(a.get("eligible") is True for a in self.agents)
        self.query_one("#switch-clear",Button).disabled=self.stage!="select" or not self.selected
        self.query_one("#switch-review").display=choosing
        self.query_one("#switch-all").display=choosing
        self.query_one("#switch-clear").display=choosing
        self.query_one("#switch-review",Button).disabled=(self.stage!="select" or not self.selected
            or self.query_one("#switch-target",Select).value is Select.NULL)
        self.query_one("#switch-apply").display=self.stage=="review"
        self.query_one("#switch-apply",Button).disabled=self.stage!="review" or not self.plan
        self.query_one("#switch-back").display=self.stage=="review"
        self.query_one("#switch-close",Button).disabled=self.stage=="applying"
        eligible=sum(a.get("eligible") is True for a in self.agents)
        self.query_one("#switch-count",Static).update(f"{len(self.selected)} / {eligible} eligible selected")

    def status(self, message, error=False) -> None:
        if self.is_mounted:
            widget=self.query_one("#switch-status",Static)
            widget.set_class(error,"switch-error")
            widget.update(Text(plain(message,1000)))

    @staticmethod
    def safe_message(error, fallback):
        try:
            from backend import AccountError
            if isinstance(error,AccountError):
                return plain(getattr(error,"message","") or str(error),1000)
        except ImportError:
            pass
        return fallback

    @work(group="switch-candidates")
    async def load_candidates(self) -> None:
        self._generation+=1
        generation=self._generation
        try:
            data=await asyncio.to_thread(self.service.switch_candidates,self.source["id"])
            if not self.is_mounted or generation!=self._generation:
                return
            self.targets=[p for p in data.get("targets",[]) if isinstance(p,dict) and p.get("id")
                          and p["id"]!=self.source["id"] and p.get("provider")==self.source.get("provider")]
            self.agents=[a for a in data.get("agents",[]) if isinstance(a,dict) and isinstance(a.get("pane_id"),str)]
            picker=self.query_one("#switch-target",Select)
            picker.set_options([(Text(plain(p.get("label") or p["id"])),p["id"]) for p in self.targets])
            picker.value=Select.NULL
            self.stage="select"
            self.render_agents()
            self.status(data.get("notice") or ("Select a target account and agents, then review the switch." if self.targets
                else "Add another account for this tool before switching agents."))
        except Exception as error:
            if self.is_mounted and generation==self._generation:
                self.stage="select"
                self.status(self.safe_message(error,"Agent eligibility could not be verified. Close and reopen this dialog; no agents were changed."),True)
        finally:
            self.controls()

    def render_agents(self) -> None:
        table=self.query_one("#switch-agents",DataTable)
        old_key=table.coordinate_to_cell_key(table.cursor_coordinate).row_key.value if table.row_count else None
        table.clear()
        for agent in self.agents:
            eligible=agent.get("eligible") is True
            style="" if eligible else "dim"
            reason=plain(agent.get("status") if eligible else agent.get("reason") or "Eligibility not verified",140)
            table.add_row(Text("[x]" if agent["pane_id"] in self.selected else "[ ]" if eligible else " —",style=style),
                Text(plain(agent.get("handle") or agent["pane_id"]),style=style),Text(plain(agent.get("workspace")),style=style),
                Text(reason,style=style),key=agent["pane_id"])
        ids=[a["pane_id"] for a in self.agents]
        if old_key in ids:
            table.move_cursor(row=ids.index(old_key),animate=False)
        self.controls()

    def on_data_table_row_highlighted(self,event: DataTable.RowHighlighted) -> None:
        if event.data_table.id!="switch-agents":
            return
        event.stop()
        agent=next((a for a in self.agents if a["pane_id"]==event.row_key.value),None)
        if agent:
            self.query_one("#switch-agent-reason",Static).update(Text(plain(agent.get("handle") or agent["pane_id"])
                + ": " + plain(agent.get("reason") or ("Eligible. Click or press Enter to toggle." if agent.get("eligible") is True else "Eligibility not verified."),300)))

    def on_data_table_row_selected(self,event: DataTable.RowSelected) -> None:
        if event.data_table.id!="switch-agents":
            return
        event.stop()
        agent=next((a for a in self.agents if a["pane_id"]==event.row_key.value),None)
        if self.stage!="select" or not agent:
            return
        if agent.get("eligible") is not True:
            self.status(agent.get("reason") or "This agent is not eligible to switch.",True)
            return
        pane=agent["pane_id"]
        self.selected.symmetric_difference_update({pane})
        self.plan=None
        self.render_agents()

    def on_select_changed(self,event: Select.Changed) -> None:
        if event.select.id=="switch-target":
            event.stop()
            self.plan=None
            self.controls()

    @work(group="switch-prepare")
    async def prepare_switch(self) -> None:
        target=self.query_one("#switch-target",Select).value
        if self.stage!="select" or target not in {p["id"] for p in self.targets} or not self.selected:
            return
        self.stage="preparing";self.controls()
        try:
            selected=[a["pane_id"] for a in self.agents if a["pane_id"] in self.selected and a.get("eligible") is True]
            plan=await asyncio.to_thread(self.service.prepare_switch,self.source["id"],target,selected)
            if not self.is_mounted:
                return
            if not isinstance(plan,dict) or not plan.get("token"):
                raise ValueError("Invalid switch plan")
            self.plan=plan
            destination=next(p for p in self.targets if p["id"]==target)
            text=Text()
            text.append("Review account switch\n\n",style="bold")
            text.append(plain(self.source.get("label") or self.source["id"])+" → "+plain(destination.get("label") or target)+"\n\n")
            agents=plan.get("agents") or [a for a in self.agents if a["pane_id"] in selected]
            for agent in agents:
                text.append("• "+plain(agent.get("handle") or agent.get("pane_id"))+" · "+plain(agent.get("workspace"))+"\n")
            text.append("\n"+plain(plan.get("notice") or "The selected agents will restart under the target account. Active work is rechecked before each switch.",1800))
            self.query_one("#switch-summary",Static).update(text)
            self.stage="review"
            self.status("Review the exact agents above. Switch selected applies this change once.")
        except Exception as error:
            if self.is_mounted:
                self.stage="select"
                self.status(self.safe_message(error,"The switch could not be prepared. No agents were changed."),True)
        finally:
            self.controls()

    def receive_progress(self,progress) -> None:
        if self.is_mounted and self.stage=="applying" and isinstance(progress,dict):
            identity=plain(progress.get("handle") or progress.get("pane_id"),80)
            message=plain(progress.get("message") or progress.get("state") or "Switching…",500)
            self.status((identity+": " if identity else "")+message)

    @work(group="switch-apply")
    async def apply_switch(self) -> None:
        if self.stage!="review" or not self.plan:
            return
        token=self.plan["token"];self.plan=None
        self.stage="applying";self.controls()
        self.status("Switching selected agents… Keep this window open until the result is recorded.")
        def progress(data):
            if not self.is_mounted:
                return
            try:
                self.app.call_from_thread(self.receive_progress,data)
            except RuntimeError:
                pass
        self._apply_task=asyncio.create_task(asyncio.to_thread(self.service.switch_accounts,token,progress))
        try:
            result=await asyncio.shield(self._apply_task)
            if not self.is_mounted:
                return
            if not isinstance(result,dict):
                raise ValueError("Invalid switch result")
            self.result=result
            text=Text()
            text.append("Switch results\n\n",style="bold")
            for item in result.get("results",[]):
                state=plain(item.get("state"))
                text.append(plain(item.get("handle") or item.get("pane_id"))+" · "+state+"\n")
                text.append(plain(item.get("message"),1000)+"\n\n")
            self.query_one("#switch-summary",Static).update(text)
            self.status(result.get("message") or "Switch finished. Inspect each agent result above.")
        except asyncio.CancelledError:
            await asyncio.shield(self._apply_task)
            raise
        except Exception as error:
            if self.is_mounted:
                self.result={"state":"unconfirmed"}
                self.query_one("#switch-summary",Static).update("Switch outcome is unconfirmed. Inspect the selected agents before attempting another switch.")
                self.status(self.safe_message(error,"Switch outcome is unconfirmed. No automatic retry was started."),True)
        finally:
            if self.is_mounted:
                self.stage="finished";self.controls()

    def on_button_pressed(self,event: Button.Pressed) -> None:
        if not (event.button.id or "").startswith("switch-"):
            return
        event.stop()
        action=event.button.id
        if action=="switch-close":
            self.action_close()
        elif action=="switch-all" and self.stage=="select":
            self.selected={a["pane_id"] for a in self.agents if a.get("eligible") is True};self.render_agents()
        elif action=="switch-clear" and self.stage=="select":
            self.selected.clear();self.render_agents()
        elif action=="switch-review":
            self.prepare_switch()
        elif action=="switch-apply":
            self.apply_switch()
        elif action=="switch-back" and self.stage=="review":
            self.plan=None;self.stage="select";self.controls()
            self.status("Adjust the selection, then review again.")

    def action_close(self) -> None:
        if self.stage=="applying":
            self.status("A switch is in progress. Wait for its result before closing.")
            return
        self._generation+=1
        self.dismiss(self.result)

    async def on_unmount(self) -> None:
        if self._apply_task is not None and not self._apply_task.done():
            try:
                await asyncio.shield(self._apply_task)
            except Exception:
                pass


class AccountCenter(App):
    TITLE = "Watchtower · Accounts"
    BINDINGS = [("escape", "quit", "Close"), ("a", "add_profile", "Add account"),
                ("r", "refresh_status", "Refresh"), ("n", "new_agent", "New agent")]
    CSS = """
    Screen { background: #11151c; color: #d7deea; }
    #topbar { height: 3; }
    #heading { width: 1fr; height: 3; padding: 1 2 0 2; text-style: bold; color: #9ecbff; }
    #tools { width: 11; min-width: 10; margin-right: 2; }
    #switch-account { width: 20; min-width: 18; margin-right: 1; }
    #subtitle { height: 2; padding: 0 2; color: #8995a7; }
    #content { height: 1fr; padding: 0 2; }
    #profiles { width: 55%; height: 1fr; background: #171d27; border: round #344055; }
    DataTable > .datatable--header { background: #232c3b; color: #bcc7d8; }
    DataTable > .datatable--cursor { background: #304664; color: #ffffff; }
    #detail-scroll { width: 45%; height: 1fr; padding: 1 2; border: round #344055; margin-left: 1; }
    #details { height: auto; }
    #buttons { height: 3; margin: 1 2 0 2; grid-size: 6; grid-columns: 1fr; grid-rows: 3; grid-gutter: 0 1; }
    #buttons Button { min-width: 0; width: 100%; }
    Screen.compact #content { layout: vertical; }
    Screen.compact #profiles { width: 100%; height: 10; }
    Screen.compact #detail-scroll { width: 100%; height: 1fr; margin-left: 0; }
    Screen.compact #buttons { grid-size: 3; height: 6; }
    #notice { height: 2; padding: 0 2; color: #9aaac0; }
    Footer { background: #1b2432; }
    AddProfile, NewAgent, BrowserLoginModal, CodexTools, SwitchAccount { align: center middle; background: #000000 60%; }
    .dialog { width: 64; height: auto; max-height: 95%; padding: 1 2; background: #171d27; border: round #7da6da; }
    .agent-dialog { height: 38; }
    #agent-fields { height: 1fr; }
    .dialog-title { text-style: bold; color: #9ecbff; margin-bottom: 1; }
    .dialog Label { margin-top: 1; }
    .hint { height: auto; color: #8995a7; margin-bottom: 1; }
    .dialog Input, .dialog Select { margin: 0; }
    .error { height: 2; color: #f2a49b; }
    .dialog-buttons { height: 3; align-horizontal: right; }
    .dialog-buttons Button { margin-left: 1; }
    #login-profile { color: #c0cfe1; margin-bottom: 1; height: auto; }
    #login-status { min-height: 3; height: auto; margin: 1 0; color: #d7e8fc; }
    .login-buttons Button { min-width: 10; }
    #tool-versions { height: auto; margin: 1 0; color: #d7e8fc; }
    #tool-status { min-height: 4; height: auto; margin-bottom: 1; }
    .tools-buttons Button { min-width: 9; }
    .switch-dialog { width: 94%; height: 94%; max-height: 100%; padding: 0 2; }
    .switch-dialog .dialog-title { height: 1; margin: 0; }
    #switch-source { height: 1; color: #c0cfe1; }
    #switch-picker { height: 1fr; }
    #switch-picker Label { height: 1; margin: 0; }
    #switch-target { height: 3; }
    #switch-guide { height: 2; color: #8995a7; }
    #switch-agents { height: 1fr; min-height: 3; }
    #switch-agent-reason { height: 2; color: #aebdd1; }
    #switch-selection-actions { height: 1; }
    #switch-count { width: 1fr; }
    #switch-summary-scroll { height: 1fr; }
    #switch-summary { height: auto; }
    #switch-status { height: 2; color: #aebdd1; }
    #switch-status.switch-error { color: #f2a49b; }
    .switch-buttons Button { min-width: 8; }
    """

    def __init__(self, service=None, *, workspace_loader: Callable | None = None,
                 launcher: Callable | None = None, connector: Callable | None = None,
                 login_factory: Callable | None = None, browser_opener: Callable | None = None,
                 clipboard_writer: Callable | None = None, tool_service_factory: Callable | None = None,
                 switch_service_factory: Callable | None = None):
        super().__init__()
        if service is None:
            from backend import AccountService
            service = AccountService()
        if workspace_loader is None or launcher is None or connector is None:
            from integration import list_workspaces, launch_profile, run_connect
            workspace_loader = workspace_loader or list_workspaces
            launcher = launcher or launch_profile
            connector = connector or run_connect
        self.service = service
        self.workspace_loader, self.launcher, self.connector = workspace_loader, launcher, connector
        self.login_factory = login_factory
        self.browser_opener, self.clipboard_writer = browser_opener, clipboard_writer
        self.tool_service_factory = tool_service_factory
        self.switch_service_factory = switch_service_factory
        self.snapshot: dict = {"profiles": [], "defaults": {}}
        self.selected_id: str | None = None
        self.busy = False
        self._updating_table = False
        self._table_rows: list = []

    def compose(self) -> ComposeResult:
        with Horizontal(id="topbar"):
            yield Static("◈  Accounts", id="heading")
            yield Button("Switch account…", id="switch-account")
            yield Button("Tools", id="tools")
        yield Static("Choose who your next agent signs in as. Accounts are shared across workspaces.", id="subtitle")
        with Horizontal(id="content"):
            yield DataTable(id="profiles", cursor_type="row", zebra_stripes=True, show_row_labels=False)
            with VerticalScroll(id="detail-scroll"):
                yield Static(id="details", markup=False)
        with Grid(id="buttons"):
            yield Button("Add account", id="add", variant="primary")
            yield Button("Connect", id="connect")
            yield Button("Refresh", id="refresh")
            yield Button("Set default", id="default")
            yield Button("New agent", id="new-agent", variant="primary")
            yield Button("Zen billing", id="billing")
        yield Static("Cached view refreshes every 30s. Refresh checks only the selected account.", id="notice", markup=False)
        yield Footer()

    def on_mount(self) -> None:
        table = self.query_one("#profiles", DataTable)
        table.add_columns("Account", "Tool", "Connection", "Quota")
        table.focus()
        self.reload_snapshot()
        self.set_interval(REFRESH_SECONDS, self.reload_snapshot)

    def on_resize(self, event) -> None:
        root = self.screen_stack[0]
        compact = event.size.width < 100
        if root.has_class("compact") != compact:
            root.set_class(compact, "compact")

    def action_quit(self) -> None:
        if self.busy:
            self.notify_status("Finish the current action before closing Accounts.")
            return
        self.exit()

    def selected_profile(self) -> dict | None:
        return next((item for item in self.snapshot.get("profiles", []) if item.get("id") == self.selected_id), None)

    def notify_status(self, message: str) -> None:
        if self.query("#notice"):
            self.query_one("#notice", Static).update(Text(message))

    def safe_error(self, error: Exception, fallback: str) -> None:
        # Exceptions may contain provider output, command env or credentials.
        # The service's own errors carry a fixed safe message, not stderr.
        try:
            from backend import AccountError
        except ImportError:
            AccountError = ()
        message = getattr(error, "message", "") if isinstance(error, AccountError) else ""
        self.notify_status(plain(message) if message else fallback)

    def set_busy(self, value: bool) -> None:
        self.busy = value
        self.update_buttons()

    def update_buttons(self) -> None:
        if self.query("#tools"):
            self.query_one("#tools", Button).disabled = self.busy
        if not self.query("#default"):
            return
        selected = self.selected_profile()
        if self.query("#switch-account"):
            self.query_one("#switch-account",Button).disabled=self.busy or selected is None
        for button in self.query("#buttons Button"):
            button.disabled = self.busy or (button.id != "add" and selected is None)
        if selected and not self.busy:
            self.query_one("#default", Button).disabled = selected["id"] == self.snapshot.get("defaults", {}).get("default_profile")
        self.query_one("#billing", Button).disabled = self.busy or not selected or selected.get("provider") != "opencode"

    def apply_snapshot(self, snapshot: dict, *, select: str | None = None) -> None:
        self.snapshot = snapshot
        profiles = snapshot.get("profiles", [])
        ids = [profile["id"] for profile in profiles]
        wanted = select or self.selected_id or snapshot.get("defaults", {}).get("default_profile")
        self.selected_id = wanted if wanted in ids else (ids[0] if ids else None)
        default = snapshot.get("defaults", {}).get("default_profile")
        rows = [(profile["id"], ("★ " if profile["id"] == default else "") + plain(profile.get("label") or profile["id"], 40),
                 PROVIDERS.get(profile["provider"], plain(profile["provider"])),
                 connection_label(profile.get("status") or {}),
                 quota_summary(profile.get("status") or {})) for profile in profiles]
        table = self.query_one("#profiles", DataTable)
        if rows != self._table_rows:
            self._updating_table = True
            table.clear()
            for key, *cells in rows:
                table.add_row(*(Text(cell) for cell in cells), key=key)
            self._table_rows = rows
            self._updating_table = False
        if self.selected_id in ids:
            table.move_cursor(row=ids.index(self.selected_id), animate=False)
        self.show_details()

    def show_details(self) -> None:
        # A queued table highlight may arrive after a dialog opened or teardown.
        if not self.query("#details"):
            return
        self.query_one("#details", Static).update(profile_details(self.selected_profile(), self.snapshot.get("defaults", {}).get("default_profile")))
        self.update_buttons()

    def on_data_table_row_highlighted(self, event: DataTable.RowHighlighted) -> None:
        if not self._updating_table and event.row_key.value:
            if any(item.get("id") == event.row_key.value for item in self.snapshot.get("profiles", [])):
                self.selected_id = event.row_key.value
                self.show_details()

    @work(exclusive=True, group="snapshot")
    async def reload_snapshot(self) -> None:
        if self.busy or len(self.screen_stack) > 1:
            return
        try:
            snapshot = await asyncio.to_thread(self.service.list)
            if not self.busy and len(self.screen_stack) == 1:
                self.apply_snapshot(snapshot)
        except Exception as error:
            self.safe_error(error, "Accounts are unavailable. Try Refresh.")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        actions = {"add": self.action_add_profile, "connect": self.action_connect,
                   "refresh": self.action_refresh_status, "default": self.action_set_default,
                   "new-agent": self.action_new_agent, "billing": self.action_billing,
                   "tools": self.action_tools, "switch-account": self.action_switch_account}
        action = actions.get(event.button.id or "")
        if action:
            action()

    @work(group="action")
    async def action_switch_account(self) -> None:
        source=self.selected_profile()
        if self.busy or not source:
            return
        self.set_busy(True)
        try:
            factory=self.switch_service_factory
            if factory is None:
                from switch_service import SwitchService
                factory=SwitchService
            result=await self.push_screen_wait(SwitchAccount(factory(self.service),source))
            if result is not None:
                self.apply_snapshot(await asyncio.to_thread(self.service.list),select=source["id"])
                self.notify_status("Account assignments refreshed. Check each agent's reported switch outcome.")
        except Exception:
            self.notify_status("Account switching is unavailable. Inspect the selected agents before retrying.")
        finally:
            self.set_busy(False)

    @work(group="action")
    async def action_tools(self) -> None:
        if self.busy:
            return
        self.set_busy(True)
        try:
            factory = self.tool_service_factory
            if factory is None:
                from tool_updates import ToolUpdateService
                factory = ToolUpdateService
            await self.push_screen_wait(CodexTools(factory()))
        except Exception:
            self.notify_status("Codex tools are unavailable. Close and reopen Accounts to try again.")
        finally:
            self.set_busy(False)

    def action_billing(self) -> None:
        profile = self.selected_profile()
        if not self.busy and profile and profile.get("provider") == "opencode":
            try:
                opened = webbrowser.open("https://opencode.ai/auth")
                self.notify_status("OpenCode account and billing opened in your browser." if opened else "Open https://opencode.ai/auth to manage Zen billing.")
            except Exception:
                self.notify_status("Open https://opencode.ai/auth to manage Zen billing.")

    @work(group="action")
    async def action_add_profile(self) -> None:
        if self.busy:
            return
        self.set_busy(True)
        try:
            result = await self.push_screen_wait(AddProfile())
            if result is None:
                return
            created = await asyncio.to_thread(self.service.add, **result)
            self.apply_snapshot(await asyncio.to_thread(self.service.list), select=created.get("id") or result["name"])
            self.notify_status("Account added. Select Connect to sign in.")
        except Exception as error:
            self.safe_error(error, "Account could not be added. Check the profile ID and try again.")
        finally:
            self.set_busy(False)

    @work(group="action")
    async def action_refresh_status(self) -> None:
        if self.busy or not self.selected_id:
            return
        self.set_busy(True)
        self.notify_status("Checking selected account…")
        try:
            await asyncio.to_thread(self.service.refresh, self.selected_id)
            self.apply_snapshot(await asyncio.to_thread(self.service.list))
            self.notify_status("Status updated. Unknown quota values remain unavailable.")
        except Exception as error:
            self.safe_error(error, "Status could not be checked. Try Connect if sign-in has expired.")
        finally:
            self.set_busy(False)

    @work(group="action")
    async def action_set_default(self) -> None:
        if self.busy or not self.selected_id:
            return
        self.set_busy(True)
        try:
            await asyncio.to_thread(self.service.set_default, self.selected_id)
            self.apply_snapshot(await asyncio.to_thread(self.service.list))
            self.notify_status("Default saved for new agents opened from Accounts. Running agents are unchanged.")
        except Exception as error:
            self.safe_error(error, "Default account could not be saved.")
        finally:
            self.set_busy(False)

    def execute_connect(self, profile: str) -> int:
        with self.suspend():
            return self.connector(self.service, profile)

    @work(group="action")
    async def action_connect(self) -> None:
        if self.busy or not self.selected_id:
            return
        self.set_busy(True)
        profile = self.selected_id
        try:
            if self.selected_profile().get("provider") == "codex":
                factory = self.login_factory
                if factory is None:
                    from browser_login import BrowserLogin
                    factory = BrowserLogin
                opener, copier = self.browser_opener, self.clipboard_writer
                if opener is None or copier is None:
                    from integration import open_browser_login, copy_login_link
                    opener, copier = opener or open_browser_login, copier or copy_login_link
                login = factory(self.service, profile)
                result = await self.push_screen_wait(BrowserLoginModal(
                    login, self.selected_profile().get("label") or profile,
                    browser_opener=opener, clipboard_writer=copier))
                succeeded = isinstance(result, dict) and result.get("state") == "success"
            else:
                # Other tools retain their own native sign-in flows.
                succeeded = self.execute_connect(profile) == 0
            if succeeded:
                await asyncio.to_thread(self.service.refresh, profile)
                self.apply_snapshot(await asyncio.to_thread(self.service.list), select=profile)
                self.notify_status("Sign-in command finished. Check connection status above.")
            else:
                self.notify_status("Sign-in did not complete. Select Connect to try again.")
        except Exception as error:
            self.safe_error(error, "Provider sign-in could not start. Confirm its CLI is installed.")
        finally:
            self.set_busy(False)

    @work(group="action")
    async def action_new_agent(self) -> None:
        if self.busy or not self.selected_id:
            return
        self.set_busy(True)
        try:
            workspaces = await asyncio.to_thread(self.workspace_loader)
            if not workspaces:
                self.notify_status("Create a workspace before starting an agent.")
                return
            result = await self.push_screen_wait(NewAgent(self.snapshot.get("profiles", []), self.selected_id, workspaces,
                                                        model_loader=getattr(self.service, "models", None)))
            if result is None:
                return
            self.notify_status("Opening agent in a new tab…")
            model_args = {"model": result["model"]} if result.get("model") else {}
            await asyncio.to_thread(self.launcher, self.service, result["profile"], result["handle"], result["workspace"], **model_args)
            self.exit()
        except Exception as error:
            self.safe_error(error, "Agent delivery is unconfirmed. Inspect Watchtower before retrying.")
        finally:
            self.set_busy(False)


def main() -> int:
    AccountCenter().run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
