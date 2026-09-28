#!/usr/bin/env python3
"""Workspace chooser; catalog/model reads never start or connect agents."""
from __future__ import annotations

import asyncio
import re

from rich.text import Text
from textual import work
from textual.app import App, ComposeResult
from textual.containers import Grid, Horizontal, Vertical, VerticalScroll
from textual.widgets import Button, Footer, Input, Label, Select, Static


def plain(value, limit=500):
    return re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f]", "", str(value or ""))[:limit]


def safe_error(error, fallback):
    try:
        from service import TeamError
    except ImportError:
        return fallback
    return plain(getattr(error, "message", None) or str(error)) if isinstance(error, TeamError) else fallback


def plan_text(plan):
    """Only display the prepared plan's public fields, never opaque token data."""
    result = Text()
    result.append(plain(plan.get("label"), 120) + "\n", "bold #9ecbff")
    result.append("Template: " + plain(plan.get("template"), 120) + "\n")
    result.append("Project: " + plain(plan.get("project"), 2000) + "\n\n")
    for role in plan.get("roles", []):
        result.append(plain(role.get("label") or role.get("role"), 100) + " → ", "bold")
        result.append(plain(role.get("profile_label") or role.get("profile_id"), 120)
                      + " · " + plain(role.get("provider"), 30) + "\n")
        result.append("  Model: " + plain(role.get("model") or "Provider default", 240) + "\n")
        if role.get("cwd") and role["cwd"] != plan.get("project"):
            result.append("  " + plain(role["cwd"], 2000) + "\n", "dim")
    result.append("\nCompletion policy\n", "bold")
    result.append(plain(plan.get("completion_policy"), 2000) + "\n\n")
    result.append("Creates a new workspace and starts these agents with their role instructions.\n"
                  "The agents wait for your first task. Existing workspaces stay unchanged.", "#b3c5db")
    return result


def outcome_text(outcome):
    result = Text()
    state = outcome.get("state")
    title = {"created": "Workspace created", "partial": "Setup incomplete", "failed": "Setup did not complete"}.get(state, "Check setup status")
    result.append(title + "\n\n", "bold #9ecbff" if state == "created" else "bold #ffb595")
    result.append(plain(outcome.get("message"), 2000) + "\n\n")
    if outcome.get("workspace_id"):
        result.append("Workspace: " + plain(outcome["workspace_id"], 100) + "\n", "bold")
    for pane in outcome.get("panes", []):
        result.append(plain(pane.get("role"), 100) + ": " + plain(pane.get("pane_id") or "not started", 100))
        if pane.get("state"):
            result.append(" · " + plain(pane["state"], 100))
        result.append("\n")
    if state != "created":
        result.append("\nInspect the workspace and panes listed above before creating another team. Nothing is retried automatically.", "#b3c5db")
    return result


class TeamSetup(App):
    TITLE = "Watchtower · New workspace"
    BINDINGS = [("escape", "close", "Close")]
    CSS = """
    Screen { background: #11151c; color: #d7deea; }
    #heading { height: 2; padding: 0 2; color: #9ecbff; text-style: bold; }
    #body { height: 1fr; padding: 0 2; }
    #setup-form { height: auto; }
    #identity-row { height: 4; grid-size: 2; grid-columns: 1fr; grid-gutter: 0 2; }
    .field { height: 4; }
    .field Label { height: 1; color: #b3c5db; }
    .field Input, .field Select { margin: 0; height: 3; }
    #role-accounts { height: 10; grid-size: 3; grid-columns: 1fr; grid-gutter: 0 1; }
    .role-field { height: 10; }
    .model-notice { height: 2; color: #8995a7; }
    .model-notice.error { color: #ff929d; }
    #template-summary { height: auto; min-height: 2; color: #d7deea; padding-top: 1; }
    #completion-policy { height: auto; color: #8995a7; }
    #provider-hint { height: auto; color: #8995a7; }
    #prepared-plan, #outcome { height: auto; padding: 1 0; }
    #notice { height: 2; padding: 0 2; color: #b3c5db; }
    #notice.error { color: #ff929d; }
    #actions { height: 3; padding: 0 2; align-horizontal: right; }
    #actions Button { margin-left: 1; }
    #review-setup { width: 18; min-width: 16; }
    #create-workspace { width: 29; min-width: 28; }
    #back, #close { width: 10; min-width: 8; }
    Footer { background: #1b2432; }
    """

    def __init__(self, service=None):
        super().__init__()
        self.initial_error = ""
        if service is None:
            try:
                from service import TeamService
                service = TeamService()
            except Exception as error:
                self.initial_error = safe_error(error, "Workspace setup is unavailable. Close and reopen it to try again.")
        self.service = service
        self.templates = []
        self.profiles = []
        self.default_profile = None
        self.current_template = None
        self.assignments = {}
        self.models = {}
        self._model_profiles = {}
        self._model_generations = [0, 0, 0]
        self._models_loading = set()
        self.prepared = None
        self.outcome = None
        self.stage = "form"
        self.busy = True
        self.create_started = False
        self._updating = False

    def compose(self) -> ComposeResult:
        yield Static("New workspace · Team templates", id="heading", markup=False)
        with VerticalScroll(id="body"):
            with Vertical(id="setup-form"):
                with Grid(id="identity-row"):
                    with Vertical(classes="field"):
                        yield Label("Template")
                        yield Select([], prompt="Choose a template", id="template", disabled=True)
                    with Vertical(classes="field"):
                        yield Label("Workspace name")
                        yield Input(placeholder="Name for the new workspace", id="workspace-label", disabled=True)
                with Vertical(classes="field"):
                    yield Label("Existing project folder · full path")
                    yield Input(placeholder="C:/Projects/my-project", id="project-dir", disabled=True)
                with Grid(id="role-accounts"):
                    for index in range(3):
                        with Vertical(classes="field role-field", id=f"role-field-{index}"):
                            yield Label("Account", id=f"role-label-{index}")
                            yield Select([], prompt="Choose an account", id=f"role-account-{index}", disabled=True)
                            yield Label("Model")
                            yield Select([("Provider default", None)], value=None, allow_blank=False,
                                         type_to_search=True, id=f"role-model-{index}", disabled=True)
                            yield Static("", id=f"model-notice-{index}", classes="model-notice", markup=False)
                yield Static("Choose a template to see its team.", id="template-summary", markup=False)
                yield Static("", id="completion-policy", markup=False)
                yield Static("Codex and OpenCode accounts. Type in a model dropdown to search; manage sign-in in Accounts.", id="provider-hint", markup=False)
            yield Static("", id="prepared-plan", markup=False)
            yield Static("", id="outcome", markup=False)
        yield Static("Reading templates and saved accounts…", id="notice", markup=False)
        with Horizontal(id="actions"):
            yield Button("Back", id="back")
            yield Button("Review setup", id="review-setup", variant="primary", disabled=True)
            yield Button("Create workspace & agents", id="create-workspace", variant="primary", disabled=True)
            yield Button("Close", id="close")
        yield Footer()

    def on_mount(self):
        self.update_controls()
        if self.initial_error:
            self.busy = False
            self.notice(self.initial_error, error=True)
            self.update_controls()
        else:
            self.load_catalog()

    def notice(self, text, error=False):
        item = self.query_one("#notice", Static)
        item.set_class(error, "error")
        item.update(Text(plain(text, 800)))

    def selected_template(self):
        return next((item for item in self.templates if item["id"] == self.current_template), None)

    def form_values(self):
        template = self.selected_template() or {}
        roles = template.get("roles", [])
        accounts = {}
        for index, role in enumerate(roles):
            value = self.query_one(f"#role-account-{index}", Select).value
            if value is not Select.NULL:
                accounts[role["id"]] = str(value)
        return (self.current_template, self.query_one("#workspace-label", Input).value,
                self.query_one("#project-dir", Input).value, accounts,
                {role["id"]: self.models.get(role["id"]) for role in roles})

    def update_controls(self):
        if not self.query("#setup-form"):
            return
        self.query_one("#setup-form").display = self.stage == "form"
        self.query_one("#prepared-plan").display = self.stage == "review"
        self.query_one("#outcome").display = self.stage == "finished"
        self.query_one("#review-setup").display = self.stage == "form"
        self.query_one("#create-workspace").display = self.stage == "review"
        self.query_one("#back").display = self.stage == "review"
        valid = bool(self.selected_template() and self.profiles)
        for control in self.query("#setup-form Input, #setup-form Select"):
            control.disabled = self.busy or not valid
        for index in self._models_loading:
            self.query_one(f"#role-model-{index}", Select).disabled = True
        self.query_one("#review-setup", Button).disabled = self.busy or not valid or bool(self._models_loading)
        self.query_one("#create-workspace", Button).disabled = self.busy or not self.prepared or self.create_started
        self.query_one("#back", Button).disabled = self.busy or self.create_started
        self.query_one("#close", Button).disabled = self.busy and self.create_started

    @work(group="catalog")
    async def load_catalog(self):
        try:
            templates, accounts = await asyncio.gather(
                asyncio.to_thread(self.service.templates), asyncio.to_thread(self.service.profiles))
            if not isinstance(templates, list) or not isinstance(accounts, dict):
                raise ValueError("Invalid catalog")
            self.templates = [item for item in templates if isinstance(item, dict) and item.get("id") and 1 <= len(item.get("roles", [])) <= 3]
            self.profiles = [item for item in accounts.get("profiles", []) if isinstance(item, dict) and item.get("id") and item.get("provider") in {"codex", "opencode"}]
            self.default_profile = accounts.get("default_profile")
            picker = self.query_one("#template", Select)
            self._updating = True
            try:
                with picker.prevent(Select.Changed):
                    picker.set_options([(Text(plain(item.get("name") or item["id"], 80)), item["id"]) for item in self.templates])
                    picker.value = self.templates[0]["id"] if self.templates else Select.NULL
                self.current_template = self.templates[0]["id"] if self.templates else None
                self.configure_template()
            finally:
                self._updating = False
            if not self.templates:
                self.notice("No templates are available. Close and reopen setup after repairing the installation.", error=True)
            elif not self.profiles:
                self.notice("No Codex or OpenCode accounts found. Add or connect an account in Accounts, then reopen this screen.", error=True)
            else:
                self.notice("Choose the new workspace details, then review the setup.")
        except Exception as error:
            self.notice(safe_error(error, "Templates or saved accounts could not be loaded. Nothing was created."), error=True)
        finally:
            self.busy = False
            self.update_controls()

    def configure_template(self):
        template = self.selected_template() or {}
        roles = template.get("roles", [])
        ids = [profile["id"] for profile in self.profiles]
        default = self.default_profile if self.default_profile in ids else (ids[0] if ids else None)
        options = [(Text(plain(profile.get("label") or profile["id"], 60) + " · "
                         + {"codex": "Codex", "opencode": "OpenCode"}[profile["provider"]]), profile["id"])
                   for profile in self.profiles]
        self.query_one("#role-accounts").styles.grid_size_columns = max(1, len(roles))
        for index in range(3):
            self._model_generations[index] += 1
            self._models_loading.discard(index)
            self.query_one(f"#role-field-{index}").display = index < len(roles)
            if index >= len(roles):
                continue
            role = roles[index]
            self.query_one(f"#role-label-{index}", Label).update(Text(plain(role.get("label") or role["id"], 80)))
            picker = self.query_one(f"#role-account-{index}", Select)
            wanted = self.assignments.get(role["id"], default)
            with picker.prevent(Select.Changed):
                picker.set_options(options)
                picker.value = wanted if wanted in ids else Select.NULL
            if picker.value is not Select.NULL:
                self.assignments[role["id"]] = str(picker.value)
            self.configure_models(index, role["id"], None if picker.value is Select.NULL else str(picker.value), preserve=True)
        self.query_one("#template-summary", Static).update(Text(plain(template.get("summary"), 1000)))
        self.query_one("#completion-policy", Static).update(Text("Completion: " + plain(template.get("completion_policy"), 1200)))

    def model_notice(self, index, message, error=False):
        item = self.query_one(f"#model-notice-{index}", Static)
        item.set_class(error, "error")
        item.update(Text(plain(message, 500)))
        item.tooltip = plain(message, 500)

    def configure_models(self, index, role_id, profile_id, preserve=False):
        self._model_generations[index] += 1
        generation = self._model_generations[index]
        wanted = self.models.get(role_id) if preserve and self._model_profiles.get(role_id) == profile_id else None
        self.models[role_id] = None
        self._model_profiles[role_id] = profile_id
        picker = self.query_one(f"#role-model-{index}", Select)
        with picker.prevent(Select.Changed):
            picker.set_options([("Provider default", None)])
            picker.value = None
        picker.tooltip = "Provider default"
        self._models_loading.discard(index)
        if profile_id:
            self._models_loading.add(index)
            self.model_notice(index, "Loading models…")
            self.load_models(index, role_id, profile_id, generation, wanted)
        else:
            self.model_notice(index, "Choose an account first.")

    def model_request_current(self, index, role_id, profile_id, generation):
        roles = (self.selected_template() or {}).get("roles", [])
        return (self.is_mounted and self.stage == "form" and self._model_generations[index] == generation
                and index < len(roles) and roles[index]["id"] == role_id
                and self.assignments.get(role_id) == profile_id)

    @work(group="models")
    async def load_models(self, index, role_id, profile_id, generation, wanted):
        error = False
        try:
            result = await asyncio.to_thread(self.service.models, profile_id)
            if not isinstance(result, dict) or not isinstance(result.get("models"), list):
                raise ValueError("Invalid model catalog")
            entries = {}
            for item in result["models"]:
                if isinstance(item, dict) and isinstance(item.get("id"), str) and item["id"]:
                    entries.setdefault(item["id"], plain(item.get("label") or item["id"], 160))
            message = plain(result.get("notice"), 500)
            if not message:
                message = ("Default: " + plain(result.get("default_model"), 240) if result.get("default_model")
                           else "Type to search models." if entries else "No models listed. Provider default is available.")
        except Exception as exc:
            entries = {}
            error = True
            message = safe_error(exc, "Models unavailable. Use Provider default or check Accounts.")
        if not self.model_request_current(index, role_id, profile_id, generation):
            return
        self._models_loading.discard(index)
        picker = self.query_one(f"#role-model-{index}", Select)
        options = [("Provider default", None)]
        for model_id, label in entries.items():
            options.append((Text(label if label == model_id else label + " · " + plain(model_id, 240)), model_id))
        selected = wanted if wanted in entries else None
        with picker.prevent(Select.Changed):
            picker.set_options(options)
            picker.value = selected
        picker.tooltip = selected or "Provider default"
        self.models[role_id] = selected
        self.model_notice(index, message, error=error)
        self.update_controls()

    def on_select_changed(self, event: Select.Changed):
        if self._updating or self.busy or self.stage != "form":
            return
        if event.select.id == "template" and event.value is not Select.NULL:
            self.current_template = str(event.value)
            self.configure_template()
            self.prepared = None
        elif event.select.id and event.select.id.startswith("role-account-"):
            index = int(event.select.id.rsplit("-", 1)[1])
            roles = (self.selected_template() or {}).get("roles", [])
            if index < len(roles) and event.value is not Select.NULL:
                role_id = roles[index]["id"]
                self.assignments[role_id] = str(event.value)
                self.configure_models(index, role_id, str(event.value))
            elif index < len(roles):
                role_id = roles[index]["id"]
                self.assignments.pop(role_id, None)
                self.configure_models(index, role_id, None)
            self.prepared = None
        elif event.select.id and event.select.id.startswith("role-model-"):
            index = int(event.select.id.rsplit("-", 1)[1])
            roles = (self.selected_template() or {}).get("roles", [])
            if (index < len(roles) and index not in self._models_loading
                    and event.value is not Select.NULL and event.value == event.select.value):
                self.models[roles[index]["id"]] = None if event.value is None else str(event.value)
                event.select.tooltip = event.value or "Provider default"
            self.prepared = None
        self.update_controls()

    def on_button_pressed(self, event: Button.Pressed):
        action = {"review-setup": self.prepare_setup, "create-workspace": self.create_workspace,
                  "back": self.back, "close": self.action_close}.get(event.button.id)
        if action:
            event.stop()
            action()

    @work(group="setup-action")
    async def prepare_setup(self):
        if self.busy or self.stage != "form" or self.query_one("#review-setup", Button).disabled:
            return
        values = self.form_values()
        self.busy = True
        self.prepared = None
        self.update_controls()
        self.notice("Validating the workspace, folder, accounts and models…")
        try:
            plan = await asyncio.to_thread(self.service.prepare, *values)
            if not isinstance(plan, dict) or not plan.get("token"):
                raise ValueError("Invalid prepared plan")
            self.prepared = plan
            self.query_one("#prepared-plan", Static).update(plan_text(plan))
            self.stage = "review"
            self.notice("Review this setup. Create starts the new workspace and its agents.")
            self.query_one("#body", VerticalScroll).scroll_home(animate=False)
        except Exception as error:
            self.notice(safe_error(error, "Setup could not be validated. Check the details; nothing was created."), error=True)
        finally:
            self.busy = False
            self.update_controls()

    def back(self):
        if not self.busy and not self.create_started:
            self.prepared = None
            self.stage = "form"
            self.notice("Edit the details, then review the updated setup.")
            self.update_controls()

    @work(group="setup-action")
    async def create_workspace(self):
        if self.busy or self.stage != "review" or not self.prepared or self.create_started:
            return
        token = self.prepared["token"]
        self.create_started = True
        self.busy = True
        self.update_controls()
        self.notice("Creating workspace and starting the selected agents…")
        try:
            outcome = await asyncio.to_thread(self.service.create, token)
            if not isinstance(outcome, dict) or outcome.get("state") not in {"created", "partial", "failed"}:
                raise ValueError("Invalid creation result")
            self.outcome = outcome
        except Exception as error:
            self.outcome = {"state": "failed", "message": safe_error(error,
                "Creation status is unconfirmed. Check Watchtower before creating another workspace."), "panes": []}
        finally:
            self.stage = "finished"
            self.busy = False
            self.query_one("#outcome", Static).update(outcome_text(self.outcome))
            self.notice("Open the new workspace and give its controller your first task." if self.outcome.get("state") == "created"
                        else "Setup stopped. Inspect the recorded workspace and panes before trying again.",
                        error=self.outcome.get("state") != "created")
            self.query_one("#body", VerticalScroll).scroll_home(animate=False)
            self.update_controls()

    def action_close(self):
        if self.busy and self.create_started:
            self.notice("Workspace setup is in progress. Wait for its result before closing.")
            return
        self.exit()


def main():
    TeamSetup().run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
