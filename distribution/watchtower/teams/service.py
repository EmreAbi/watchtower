"""Passive template/account discovery and explicit one-use setup actions."""
from __future__ import annotations

from copy import deepcopy
from pathlib import Path
import secrets
import sys

try:
    from .catalog import list_templates
    from .integration import TeamLaunchError
    from .scaffold import plan, TeamPlanError
except ImportError:
    from catalog import list_templates
    from integration import TeamLaunchError
    from scaffold import plan, TeamPlanError


class TeamError(ValueError):
    def __init__(self, message):
        self.message = message
        super().__init__(message)


class TeamService:
    def __init__(self, accounts=None, creator=None):
        if accounts is None:
            sys.path.append(str(Path(__file__).resolve().parent.parent / "accounts"))
            from backend import AccountService
            accounts = AccountService()
        self.accounts = accounts
        self.creator = creator
        self._plans = {}

    def templates(self):
        return [{"id": item["id"], "name": item["name"], "summary": item["description"],
                 "completion_policy": ("Exact output + independent review required. Global review is optional."
                    if item["independent_review"] else "Direct delivery; independent review is not required."),
                 "roles": [{"id": role["id"], "label": role["icon"] + " " + role["name"],
                            "description": role["scope"]} for role in item["roles"]]}
                for item in list_templates()]

    def profiles(self):
        try:
            state = self.accounts.list()
        except Exception:
            raise TeamError("Account profiles are unavailable. Check Accounts and reopen Teams.") from None
        return {"profiles": [{"id": p["id"], "label": p.get("label") or p["id"], "provider": p["provider"]}
                             for p in state["profiles"] if p["provider"] in ("codex", "opencode") and not p.get("configuration_error")],
                "default_profile": state.get("defaults", {}).get("default_profile")}

    def models(self, profile_id):
        try:
            result = self.accounts.models(profile_id)
            return {"models": [{"id": m["id"], "label": m.get("label") or m["id"]}
                               for m in result.get("models", [])],
                    "default_model": result.get("default_model"), "notice": result.get("notice", "")}
        except Exception:
            return {"models": [], "default_model": None,
                    "notice": "Models could not be loaded for this account. Provider default remains available."}

    def prepare(self, template_id, label, project_dir, assignments, models=None):
        try:
            state = self.accounts.list()
            team_id = "team-" + secrets.token_hex(8)
            prepared = plan(team_id, label, project_dir, template_id, assignments, state["profiles"], models=models)
            available = {}
            for role in prepared["roles"].values():
                if role.get("model") is None:
                    continue
                profile = role["profile_id"]
                if profile not in available:
                    available[profile] = {m["id"] for m in self.models(profile)["models"]}
                if role["model"] not in available[profile]:
                    raise TeamPlanError("A selected model is no longer in this account's catalog. Select a current model or Provider default.")
        except TeamPlanError as exc:
            raise TeamError(exc.message) from None
        except Exception:
            raise TeamError("Setup could not be prepared. Check Accounts and the project directory.") from None
        token = secrets.token_hex(24)
        template = next(t for t in self.templates() if t["id"] == template_id)
        profiles = {p["id"]: p for p in state["profiles"]}
        self._plans[token] = prepared
        return {"token": token, "template": template["name"], "label": prepared["workspace_label"],
                "project": prepared["project_path"], "summary": template["summary"],
                "completion_policy": template["completion_policy"],
                "roles": [{"role": role["id"], "label": role["icon"] + " " + role["name"],
                           "profile_id": role["profile_id"], "profile_label": profiles[role["profile_id"]].get("label") or role["profile_id"],
                           "provider": role["provider"], "model": role.get("model")}
                          for role in prepared["roles"].values()]}

    def create(self, token):
        prepared = self._plans.pop(token, None)
        if prepared is None:
            raise TeamError("This setup was already used or expired. Inspect any created workspace before preparing another.")
        try:
            if self.creator is None:
                if __package__:
                    from .integration import create_team
                else:
                    from integration import create_team
                creator = create_team
            else:
                creator = self.creator
            outcome = creator(self.accounts, deepcopy(prepared))
        except (TeamLaunchError, TeamPlanError) as exc:
            # These messages are safe for display. A prior/racing attempt may
            # still exist, so consume the token without promising a clean retry.
            return {"state": "partial", "workspace_id": None, "panes": [],
                    "message": exc.message}
        except Exception:
            # Never imply that a timed-out mutation did not happen.
            return {"state": "partial", "workspace_id": None, "panes": [],
                    "message": "Setup delivery is unconfirmed. Inspect workspaces and the setup record before trying again."}
        state = {"launch_requested": "created", "unconfirmed": "partial"}.get(outcome["state"], outcome["state"])
        return {"state": state, "workspace_id": outcome.get("workspace_id"),
                "panes": [{"role": role.get("id"), "pane_id": role.get("pane_id"),
                           "tab_id": role.get("tab_id"), "state": role.get("state")}
                          for role in outcome.get("roles", [])],
                "message": outcome.get("notice", "Setup requested; agents wait for your task.")}
