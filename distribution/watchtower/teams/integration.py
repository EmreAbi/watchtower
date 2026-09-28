"""Create a new prepared workspace and launch each owned role at most once.

Only explicit Create reaches this module. Every mutating request is journalled
before dispatch; an uncertain response is never retried. New plugin tabs consume
one-use launch tickets, so restoring a tab cannot start another agent. Existing
workspaces, panes, project files and provider permissions are left alone.
"""
from __future__ import annotations

from copy import deepcopy
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import secrets
import subprocess
import sys
import tempfile
import time

try:
    from . import scaffold
except ImportError:
    import scaffold


ROOT = Path(__file__).resolve().parent
PLUGIN = "watchtower-teams"
ENTRYPOINT = "launch-agent"
TICKET_ENV = "WATCHTOWER_TEAM_TICKET"
_TEAM = r"[a-z][a-z0-9-]{0,47}"
_TOKEN = re.compile(r"(" + _TEAM + r")\.([a-f0-9]{48})\Z")
_WORKSPACE = re.compile(r"w[1-9][0-9]*\Z")
_PANE = re.compile(r"w[1-9][0-9]*:p[0-9]+\Z")
_ROLE_LABELS = {"controller": "CONTROL", "worker": "WORKER", "reviewer": "REVIEW"}


class TeamLaunchError(ValueError):
    def __init__(self, message):
        self.message = message
        super().__init__(message)


def _accounts():
    """Alias the bundled Accounts module; never import this file as Accounts."""
    name = "watchtower_teams_accounts_integration"
    if name not in sys.modules:
        account_root = ROOT.parent / "accounts"
        if str(account_root) not in sys.path:
            sys.path.insert(0, str(account_root))
        spec = importlib.util.spec_from_file_location(name, account_root / "integration.py")
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module
        spec.loader.exec_module(module)
    return sys.modules[name]


def _absolute(value, name):
    if (not isinstance(value, (str, os.PathLike)) or not str(value)
            or any(ord(char) < 32 for char in str(value))):
        raise TeamLaunchError(f"An absolute {name} path is required.")
    path = Path(value)
    if not path.is_absolute():
        raise TeamLaunchError(f"An absolute {name} path is required.")
    return path.resolve()


def _host(environ):
    env = dict(os.environ if environ is None else environ)
    binary = _absolute(env.get("HERDR_BIN_PATH", ""), "Watchtower executable")
    _absolute(env.get("HERDR_SOCKET_PATH", ""), "Watchtower socket")
    if not binary.is_file() or binary.stem.lower() not in ("watchtower", "herdr"):
        raise TeamLaunchError("Open Teams from Watchtower's menu.")
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    return binary, env


def _cli(args, environ=None):
    binary, env = _host(environ)
    try:
        result = subprocess.run([str(binary), *args], env=env, capture_output=True,
                                text=True, encoding="utf-8", timeout=15,
                                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        # This native CLI command uses send_ok_request: it prints nothing on
        # success and returns nonzero on a rejected API response. Creation/get
        # requests still require their actual structured response below.
        if args[:2] == ["pane", "report-metadata"] and result.returncode == 0 and not result.stdout.strip():
            return {"type": "ok"}
        response = json.loads(result.stdout)
    except (OSError, ValueError, subprocess.SubprocessError):
        raise TeamLaunchError("Watchtower did not confirm the request. Nothing will be retried automatically.") from None
    if (not isinstance(response, dict) or result.returncode or "error" in response
            or not isinstance(response.get("result"), dict)):
        raise TeamLaunchError("Watchtower rejected or could not confirm the request. Inspect the new workspace before retrying.")
    return response["result"]


def _read(path):
    if path.stat().st_size > 512 * 1024:
        raise TeamLaunchError("Team launch metadata is invalid; existing work was preserved.")
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise TeamLaunchError("Team launch metadata is invalid; existing work was preserved.")
    return value


def _hash(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _atomic(path, value):
    descriptor, temporary = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(value, stream, ensure_ascii=False, indent=2)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def _exclusive(path, value):
    with path.open("x", encoding="utf-8") as stream:
        json.dump(value, stream, ensure_ascii=False)
        stream.flush()
        os.fsync(stream.fileno())


def _operations(service):
    return _absolute(service.home, "Watchtower home") / "state" / "teams-operations"


def _profile_identity(profile):
    if profile.get("provider") not in ("codex", "opencode") or profile.get("configuration_error") or profile.get("custom_environment"):
        raise TeamLaunchError("Choose a Codex or OpenCode profile with verified settings in Accounts.")
    return dict(id=profile["id"], provider=profile["provider"], home=str(_absolute(profile["home"], "account home")))


def create_team(service, prepared, *, team_root=None, cli=None, environ=None):
    """Explicit creation; ``team_root`` is the exact new metadata directory.

    Return a durable outcome, including partial/unconfirmed work. Calling again
    with the same team ID only returns its recorded outcome, never repeats an
    account registration, workspace creation or role launch.
    """
    if not isinstance(prepared, dict) or not re.fullmatch(_TEAM, str(prepared.get("team_id", ""))):
        raise TeamLaunchError("Prepare a valid team before creating its workspace.")
    team_id = prepared["team_id"]
    operation = _operations(service) / team_id
    outcome_path = operation / "outcome.json"
    if operation.exists():
        if outcome_path.is_file():
            return _read(outcome_path)
        return dict(operation_id=team_id, state="unconfirmed", workspace_id=None,
                    team_dir=None, roles=[], notice="This creation was already attempted. Inspect Watchtower; it will not be repeated.")
    if prepared.get("workspace_id") is not None:
        raise TeamLaunchError("Templates create a new workspace; an existing workspace cannot be adopted.")
    # Recompute policy from the current catalog/account references before writes.
    models = ({name: role.get("model") for name, role in prepared.get("roles", {}).items()}
              if any("model" in role for role in prepared.get("roles", {}).values()) else None)
    checked = scaffold.plan(team_id, prepared.get("workspace_label"), prepared.get("project_path"),
                            prepared.get("template_id"), prepared.get("assignments"), service.list()["profiles"], models=models)
    if checked != prepared:
        raise TeamLaunchError("The team plan changed. Prepare it again before creating the workspace.")
    binary, env = _host(environ)
    radio_home = _absolute(service.radio_home, "Radio state")
    if _absolute(env.get("RADIO_HOME", ""), "Radio state") != radio_home:
        raise TeamLaunchError("The selected Accounts service belongs to another Radio context.")
    if env.get("WATCHTOWER_HOME") and _absolute(env["WATCHTOWER_HOME"], "Watchtower home") != _absolute(service.home, "Watchtower home"):
        raise TeamLaunchError("The selected Accounts service belongs to another Watchtower home.")
    destination = _absolute(team_root, "team directory") if team_root else _absolute(service.home, "Watchtower home") / "state/teams" / team_id
    project = Path(checked["project_path"])
    if destination == project or destination.is_relative_to(project):
        raise TeamLaunchError("Store team metadata outside the selected project.")
    if destination.exists() or destination.is_symlink():
        raise TeamLaunchError("The team directory already exists. Existing teams are never overwritten.")
    accounts = {profile_id: _profile_identity(service.get(profile_id))
                for profile_id in dict.fromkeys(checked["assignments"].values())}
    catalogs = {}
    for role in checked["roles"].values():
        if role.get("model") is None:
            continue
        profile_id = role["profile_id"]
        if profile_id not in catalogs:
            catalogs[profile_id] = {item["id"] for item in service.models(profile_id).get("models", [])}
        if role["model"] not in catalogs[profile_id]:
            raise TeamLaunchError("A selected model is unavailable in the account catalog. Prepare the setup again.")
    call = cli or (lambda args: _cli(args, env))
    operation.parent.mkdir(parents=True, exist_ok=True)
    try:
        operation.mkdir()  # atomic ownership for double clicks/concurrent creators
    except FileExistsError:
        raise TeamLaunchError("This team creation is already in progress. It will not be repeated.") from None
    outcome = dict(operation_id=team_id, state="preparing", workspace_id=None,
                   team_dir=str(destination), roles=[], notice="Preparing the selected accounts.")
    _exclusive(operation / "plan.json", dict(plan=checked, digest=_hash(checked)))
    _atomic(outcome_path, outcome)
    try:
        # Registration only links explicitly selected existing profiles.
        for profile_id, identity in accounts.items():
            if _profile_identity(service.ensure_registered(profile_id)) != identity:
                raise TeamLaunchError("An account profile changed during setup.")
        outcome.update(state="workspace_requested", notice="Workspace creation was requested; awaiting confirmation.")
        _atomic(outcome_path, outcome)  # reserve before the non-idempotent request
        created = call(["workspace", "create", "--cwd", str(project), "--label", checked["workspace_label"], "--focus"])
        workspace = created.get("workspace", {}).get("workspace_id", "")
        root_pane = created.get("root_pane", {})
        tab = created.get("tab", {})
        if (not _WORKSPACE.fullmatch(workspace) or root_pane.get("workspace_id") != workspace
                or not _PANE.fullmatch(root_pane.get("pane_id", ""))
                or not str(root_pane["pane_id"]).startswith(workspace + ":")
                or not isinstance(tab.get("tab_id"), str) or not tab["tab_id"]):
            raise TeamLaunchError("Workspace creation could not be confirmed.")
        outcome.update(workspace_id=workspace, state="preparing_roles", project_pane=root_pane["pane_id"])
        _atomic(outcome_path, outcome)
        destination.parent.mkdir(parents=True, exist_ok=True)
        config = scaffold.scaffold(checked, destination, workspace_id=workspace,
            runtime=dict(herdr=str(binary), radio_home=str(radio_home),
                         radio_entrypoint=str(ROOT.parent / "radio/vendor/AgentRadio/bin/radio"),
                         watchtower_home=str(_absolute(service.home, "Watchtower home")),
                         workflow_cli=str(ROOT / "workflow.py"), python=str(Path(sys.executable).resolve())))
        # The initially created shell stays available; do not type into it or
        # replace it with an agent. Every role gets a dedicated plugin tab.
        call(["tab", "rename", tab["tab_id"], "Project terminal"])
        (operation / "tickets").mkdir()
        for role_id, role in config["roles"].items():
            model_args = {"model": role["model"]} if role.get("model") is not None else {}
            service.launch_plan(role["profile_id"], role_id, workspace=workspace, **model_args)
            token = team_id + "." + secrets.token_hex(24)
            ticket = dict(version=1, token=token, created=time.time(), workspace=workspace,
                          socket=env["HERDR_SOCKET_PATH"], radio_home=str(radio_home),
                          config_path=str(destination / "config.json"), config_digest=_hash(config),
                          role=role_id, profile=accounts[role["profile_id"]], cwd=role["cwd"])
            _exclusive(operation / "tickets" / (token.split(".")[1] + ".json"), ticket)
            item = dict(id=role_id, profile_id=role["profile_id"], model=role.get("model"),
                        state="launch_requested", pane_id=None, tab_id=None)
            outcome["roles"].append(item)
            _atomic(outcome_path, outcome)  # reserve before opening a new tab
            launched = call(["plugin", "pane", "open", "--plugin", PLUGIN,
                             "--entrypoint", ENTRYPOINT, "--placement", "tab", "--workspace", workspace,
                             "--cwd", role["cwd"], "--env", TICKET_ENV + "=" + token,
                             "--focus"])
            pane = launched.get("plugin_pane", {}).get("pane", {})
            pane_id = pane.get("pane_id", "")
            if (pane.get("workspace_id") != workspace or not _PANE.fullmatch(pane_id)
                    or not pane_id.startswith(workspace + ":") or not pane.get("tab_id")
                    or pane_id == root_pane["pane_id"]
                    or any(other.get("pane_id") == pane_id for other in outcome["roles"][:-1])):
                item["state"] = "unconfirmed"
                raise TeamLaunchError("Role tab delivery could not be confirmed.")
            item.update(pane_id=pane_id, tab_id=pane["tab_id"], state="launch_requested")
            _atomic(outcome_path, outcome)
            call(["tab", "rename", pane["tab_id"], role["tab"]])
        outcome.update(state="launch_requested",
                       notice="Team tabs are prepared. Agents launch with their role instructions; no project task was sent.")
    except Exception:
        # A transport timeout may follow a successful mutation. Keep every owned
        # artifact and its reservation for inspection; never retry or tear down.
        outcome.update(state="partial" if outcome["workspace_id"] else "unconfirmed",
                       notice="Setup could not confirm every step. Inspect this workspace and its recorded roles; no creation or launch will be retried automatically.")
    try:
        _atomic(outcome_path, outcome)
    except OSError:
        # Mutations may already be confirmed. Preserve their identities in the
        # caller response even when a final disk write cannot be completed.
        outcome.update(state="partial" if outcome["workspace_id"] else "unconfirmed",
                       notice="Setup was attempted, but its final journal update failed. The known workspace and roles are shown; nothing will be retried automatically.")
    return deepcopy(outcome)


def claim_ticket(service, token, environ=None):
    """Consume one ticket atomically inside its new plugin pane, never on restore."""
    env = dict(os.environ if environ is None else environ)
    match = _TOKEN.fullmatch(token or "")
    if not match:
        raise TeamLaunchError("Team launch request is missing. Create a team from Watchtower.")
    directory = _operations(service) / match[1] / "tickets"
    ticket = _read(directory / (match[2] + ".json"))
    workspace = ticket.get("workspace", "")
    if (ticket.get("version") != 1 or ticket.get("token") != token
            or not isinstance(ticket.get("created"), (int, float))
            or not 0 <= time.time() - ticket["created"] <= 600):
        raise TeamLaunchError("This team launch request has expired or is invalid. It was not repeated.")
    if (env.get("HERDR_PLUGIN_ID") != PLUGIN or env.get("HERDR_PLUGIN_ENTRYPOINT_ID") != ENTRYPOINT
            or not _WORKSPACE.fullmatch(workspace) or env.get("HERDR_WORKSPACE_ID") != workspace
            or not re.fullmatch(re.escape(workspace) + r":p[0-9]+", env.get("HERDR_PANE_ID", ""))
            or os.path.normcase(ticket.get("socket", "")) != os.path.normcase(env.get("HERDR_SOCKET_PATH", ""))
            or _absolute(ticket["radio_home"], "Radio state") != _absolute(service.radio_home, "Radio state")):
        raise TeamLaunchError("This launch request does not belong to this plugin pane/workspace.")
    path = _absolute(ticket["config_path"], "team config")
    config = _read(path)
    role = config.get("roles", {}).get(ticket.get("role"))
    if (config.get("workspace_id") != workspace or config.get("team_id") != match[1]
            or config.get("team_root") != str(path.parent) or _hash(config) != ticket.get("config_digest")
            or not isinstance(role, dict) or role.get("cwd") != ticket.get("cwd")
            or role.get("profile_id") != ticket.get("profile", {}).get("id")
            or role.get("provider") not in ("codex", "opencode")
            or role.get("provider") != ticket.get("profile", {}).get("provider")):
        raise TeamLaunchError("The prepared team changed. No agent was launched.")
    if _profile_identity(service.get(role["profile_id"])) != ticket["profile"]:
        raise TeamLaunchError("The selected account profile changed. No agent was launched.")
    try:
        _exclusive(directory / (match[2] + ".claimed"), dict(pane_id=env["HERDR_PANE_ID"], claimed=time.time()))
    except FileExistsError:
        raise TeamLaunchError("This role launch was already used. Restoring its tab never starts another agent.") from None
    return ticket, config, role


def _role_instructions(config, role):
    root = _absolute(config["team_root"], "team directory")
    role_path = _absolute(role["entrypoint"], "role brief")
    if role_path != root / "roles" / (role["id"] + ".md"):
        raise TeamLaunchError("The role brief is outside this prepared team.")
    texts = []
    for path in (root / "BRIEF.md", role_path):
        if path.stat().st_size > 24 * 1024:
            raise TeamLaunchError("The role briefing is too large. No agent was launched.")
        texts.append(path.read_text(encoding="utf-8"))
    # Native Radio joins this paragraph to its own instructions. No startup task
    # or synthetic user prompt is sent, and no provider config is overwritten.
    briefing = "\n\n".join(texts)
    if len(briefing) > 12000:
        # Keep native Windows argv bounded. The complete documents remain local
        # references; this compact bootstrap is still actual provider context.
        briefing = (f"You are {role['name']} ({role['id']}) in this prepared team. {role['scope']} "
                    f"Read the complete team rules at {root / 'BRIEF.md'} and your role instructions at {role_path} "
                    "before accepting a task. Follow their workflow commands and independent-review rules. "
                    "Global review is optional. Inherit existing provider permissions; never change credentials or approval settings. "
                    "This launch is preparation only: do not start research, edits, tests or services.")
    return briefing + "\n\nTeam configuration: " + str(root / "config.json") + ". Wait for an explicit user task."


def run_ticket(service, token=None, *, cli=None, environ=None, runner=None):
    env = dict(os.environ if environ is None else environ)
    _host(env)
    token = token if token is not None else env.get(TICKET_ENV, "")
    ticket, config, role = claim_ticket(service, token, env)
    call = cli or (lambda args: _cli(args, env))
    pane_id = env["HERDR_PANE_ID"]
    pane = call(["pane", "get", pane_id]).get("pane", {})
    if (pane.get("pane_id") != pane_id or pane.get("workspace_id") != ticket["workspace"]
            or pane.get("agent") or not pane.get("cwd")
            or _absolute(pane["cwd"], "pane directory") != _absolute(role["cwd"], "role directory")):
        raise TeamLaunchError("The role's new pane could not be verified. No existing agent was replaced.")
    model_args = {"model": role["model"]} if role.get("model") is not None else {}
    plan = service.launch_plan(role["profile_id"], role["id"], workspace=ticket["workspace"], **model_args)
    if plan.get("provider") != role["provider"]:
        raise TeamLaunchError("The selected provider changed. No agent was launched.")
    plan = deepcopy(plan)
    plan["argv"] = [*plan["argv"], "--role", _role_instructions(config, role)]
    label = _ROLE_LABELS.get(role["kind"])
    if label is None:
        raise TeamLaunchError("The role metadata is invalid.")
    call(["pane", "report-metadata", pane_id, "--source", "watchtower-teams", "--agent", role["provider"],
          "--token", "team_role=" + role["icon"] + "  " + label,
          "--token", "team_target=" + role["ui_target"],
          "--token", "team_frequency=" + config["frequency_display"]])
    current = Path.cwd()
    try:
        os.chdir(role["cwd"])
        return (runner or _accounts().run_radio_plan)(plan)
    finally:
        os.chdir(current)


def main():
    _accounts()  # Adds the verified bundled Accounts backend to the import path.
    from backend import AccountError, AccountService
    try:
        return run_ticket(AccountService())
    except (AccountError, OSError, ValueError, RuntimeError, KeyError, TypeError):
        print("Teams: this role could not be launched or its launch was already used. Inspect the team setup; no automatic retry.", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
