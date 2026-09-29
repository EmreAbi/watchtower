"""Validate credential-free team plans and atomically publish new team metadata.

This module never starts agents, connects accounts, changes provider permissions
or writes the selected project. Runtime code supplies account metadata and later
launches verified profile IDs. A prepared team waits for an explicit user task.
"""
from __future__ import annotations

from copy import deepcopy
import ctypes
import errno
import json
import os
from pathlib import Path
import re
import shutil
import shlex
import sys
import tempfile

try:
    from .catalog import CATALOG_VERSION, get_template
except ImportError:
    from catalog import CATALOG_VERSION, get_template


VERSION = 1
_TEAM_ID = re.compile(r"[a-z][a-z0-9-]{0,47}\Z")
_PROFILE_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,47}\Z")
_WORKSPACE_ID = re.compile(r"w[1-9][0-9]*\Z")
_MODEL_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}\Z")
_OPENCODE_MODEL_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]*/[A-Za-z0-9][A-Za-z0-9_./:-]*\Z")
_RESERVED = {"con", "prn", "aux", "nul", *(f"com{i}" for i in range(1, 10)), *(f"lpt{i}" for i in range(1, 10))}
_RUNTIME_FIELDS = {"herdr", "radio_home", "radio_entrypoint", "watchtower_home", "workflow_cli", "python"}


class TeamPlanError(ValueError):
    """A short safe UI error without credentials or raw subprocess output."""

    def __init__(self, message):
        self.message = message
        super().__init__(message)


def _absolute(value, description):
    try:
        if not isinstance(value, (str, os.PathLike)) or not str(value):
            raise ValueError()
        if any(ord(c) < 32 or ord(c) == 127 for c in str(value)):
            raise ValueError()
        path = Path(value)
        if not path.is_absolute():
            raise ValueError()
        return path.resolve()
    except (OSError, ValueError, RuntimeError):
        raise TeamPlanError(f"Choose an absolute {description} path.") from None


def _workspace(value, *, required=False):
    if value is None and not required:
        return None
    if not isinstance(value, str) or not _WORKSPACE_ID.fullmatch(value):
        raise TeamPlanError("Bind the prepared team to a valid workspace ID.")
    return value


def _profiles(profiles):
    if not isinstance(profiles, (list, tuple)):
        raise TeamPlanError("Account profiles are unavailable. Refresh Accounts before creating a team.")
    result = {}
    for profile in profiles:
        if not isinstance(profile, dict):
            raise TeamPlanError("Account profile metadata is invalid.")
        profile_id = profile.get("id")
        if not isinstance(profile_id, str) or not _PROFILE_ID.fullmatch(profile_id):
            raise TeamPlanError("Account profile metadata is invalid.")
        if profile_id.lower() in result:
            raise TeamPlanError("Account profile IDs are ambiguous. Refresh Accounts.")
        # Deliberately do not copy home, environment, status, credentials or labels.
        result[profile_id.lower()] = {"id": profile_id, "provider": profile.get("provider"),
                                      "configuration_error": bool(profile.get("configuration_error"))}
    return result


def _model(value, provider):
    if value is None:
        return None
    pattern = _OPENCODE_MODEL_ID if provider == "opencode" else _MODEL_ID
    if not isinstance(value, str) or len(value) > 128 or not pattern.fullmatch(value):
        description = "provider/model" if provider == "opencode" else "a Codex model ID"
        raise TeamPlanError(f"Choose {description} without spaces or control characters, or use the account default.")
    return value


def plan(team_id, label, project_dir, template_id, assignments, profiles, *, workspace_id=None, models=None):
    """Prepare a detached plan using passed-in profile metadata only.

    The only filesystem check is the selected project directory. Account homes,
    logins and provider state are never opened. Every role needs an explicit
    profile reference. Codex and OpenCode require a verified live review identity.
    Model availability is checked by the runtime against the selected account;
    this pure plan validates identifiers, never reads credentials or lists models.
    """
    if not isinstance(team_id, str) or not _TEAM_ID.fullmatch(team_id) or team_id in _RESERVED:
        raise TeamPlanError("Use a team ID of 1–48 lowercase letters, numbers or hyphens, starting with a letter.")
    if (not isinstance(label, str) or not 1 <= len(label.strip()) <= 80
            or any(ord(c) < 32 or ord(c) == 127 for c in label)):
        raise TeamPlanError("Use a workspace label of 1–80 characters without control characters.")
    project = _absolute(project_dir, "project directory")
    if not project.is_dir():
        raise TeamPlanError("The selected project directory does not exist.")
    try:
        template = get_template(template_id)
    except ValueError as exc:
        raise TeamPlanError(str(exc)) from None
    expected = {role["id"] for role in template["roles"]}
    if not isinstance(assignments, dict) or set(assignments) != expected:
        raise TeamPlanError("Choose one account profile for every template role, with no extra roles.")
    if models is not None and (not isinstance(models, dict) or set(models) - expected):
        raise TeamPlanError("Model selections must reference template roles only.")
    available = _profiles(profiles)
    roles = {}
    chosen = {}
    for role in template["roles"]:
        profile_id = assignments[role["id"]]
        if not isinstance(profile_id, str) or profile_id.lower() not in available:
            raise TeamPlanError("A selected account profile is unavailable. Refresh Accounts.")
        profile = available[profile_id.lower()]
        if profile["provider"] not in ("codex", "opencode"):
            raise TeamPlanError("Team templates currently support Codex and OpenCode profiles only.")
        if profile["configuration_error"]:
            raise TeamPlanError("A selected account profile has a configuration error. Repair it in Accounts.")
        chosen[role["id"]] = profile["id"]
        roles[role["id"]] = dict(role, profile_id=profile["id"], provider=profile["provider"])
        # Keep older prepared plans byte-for-byte compatible when no model map
        # was supplied. An explicit map records defaults as null for every role.
        if models is not None:
            roles[role["id"]]["model"] = _model(models.get(role["id"]), profile["provider"])
    reviewer = "reviewer" if "reviewer" in roles else None
    return {
        "version": VERSION, "catalog_version": CATALOG_VERSION,
        "template_id": template["id"], "template_version": template["version"],
        "team_id": team_id, "workspace_label": label.strip(), "workspace_id": _workspace(workspace_id),
        "project_path": str(project), "mode": "task_driven", "ui_telemetry": True,
        "assignments": chosen, "roles": roles,
        "agent_permissions": {"mode": "inherit"},
        "review": {"lead": template["lead"], "local_reviewer": reviewer,
                   "global_reviewer": None, "global_mode": "on_demand",
                   "required": template["independent_review"], "enforcement": "instructions_only"},
        "quality_gate": {"required": template["independent_review"],
                         "acceptance": list(template["acceptance"]), "self_approval": False},
    }


def _runtime(values):
    if values is None:
        return {}
    if not isinstance(values, dict) or set(values) - _RUNTIME_FIELDS:
        raise TeamPlanError("Runtime metadata contains unsupported fields.")
    return {key: str(_absolute(value, "runtime")) for key, value in values.items()}


def _revalidate(prepared, workspace_id):
    if not isinstance(prepared, dict):
        raise TeamPlanError("The team plan is invalid. Prepare it again.")
    try:
        roles = prepared["roles"]
        profiles = {role["profile_id"]: {"id": role["profile_id"], "provider": role["provider"]}
                    for role in roles.values()}
        models = ({role_id: role.get("model") for role_id, role in roles.items()}
                  if any("model" in role for role in roles.values()) else None)
        fresh = plan(prepared["team_id"], prepared["workspace_label"], prepared["project_path"],
                     prepared["template_id"], prepared["assignments"], list(profiles.values()),
                     workspace_id=_workspace(workspace_id, required=True), models=models)
        # Reject changed plans rather than trust hand-edited permission/scope fields.
        comparable = deepcopy(prepared)
        comparable["workspace_id"] = fresh["workspace_id"]
        if comparable != fresh:
            raise TeamPlanError("The team plan changed. Prepare it again before creating the workspace.")
        if prepared.get("workspace_id") not in (None, workspace_id):
            raise TeamPlanError("The team plan is bound to another workspace.")
        return fresh
    except (KeyError, TypeError, AttributeError):
        raise TeamPlanError("The team plan is invalid. Prepare it again.") from None


def _publish_new(staging, destination):
    """Atomic rename that never replaces even an empty destination directory."""
    if os.name == "nt":
        os.rename(staging, destination)  # Windows rename refuses any existing destination.
        return
    library = ctypes.CDLL(None, use_errno=True)
    source, target = os.fsencode(staging), os.fsencode(destination)
    if sys.platform.startswith("linux") and hasattr(library, "renameat2"):
        rename = library.renameat2
        rename.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]
        rename.restype = ctypes.c_int
        result = rename(-100, source, -100, target, 1)  # AT_FDCWD, RENAME_NOREPLACE.
    elif sys.platform == "darwin" and hasattr(library, "renamex_np"):
        rename = library.renamex_np
        rename.argtypes = [ctypes.c_char_p, ctypes.c_char_p, ctypes.c_uint]
        rename.restype = ctypes.c_int
        result = rename(source, target, 4)  # RENAME_EXCL.
    else:
        raise TeamPlanError("Atomic team creation is unavailable on this platform.")
    if result:
        code = ctypes.get_errno()
        raise OSError(code, os.strerror(code))


def _brief(config):
    lead = config["review"]["lead"]
    review = ("An independent local review is required for substantive task completion. "
              "The reviewer checks the exact submitted work and records findings; no author may approve their own work."
              if config["review"]["required"] else
              "Routine standalone tasks do not require independent review, work-item ceremony or delegation. "
              "Follow the project's existing rules if a later user request involves substantive project work.")
    roles = "\n".join(f"- `{key}` — {role['name']}: {role['scope']}" for key, role in config["roles"].items())
    workflow = ("The configured workflow CLI validates identity, submitted digests and completion gates when invoked. "
                "Agents must run its commands; creating a team does not automatically review every action."
                if config.get("workflow_cli") else
                "No workflow CLI is bound yet. Review policy is instructions only; do not claim a recorded or "
                "automatically enforced review gate until runtime integration is configured.")
    return f"""# {config['workspace_label']}

Template: {get_template(config['template_id'])['name']} v{config['template_version']}.
Workspace: `{config['workspace_id']}`. Project reference: `{config['project_path']}`.
The project and team metadata are separate. Configured `paths` identify owned work,
outputs, work items, reviews and reports. Do not write setup files into the project.

## Roles

{roles}

## Startup and communication

Read config.json and your role brief. Acknowledge readiness once and wait for an
explicit user task; template setup is not an instruction to research, edit, test,
start a service or reopen completed work. `{lead}` is the user's entry point.
Use Radio only from your own verified pane/account binding. Communication is
scoped to this workspace; no other workspace is subscribed automatically.
Request a reply only when one is needed. Replies arrive by push; do not poll an
inbox or create timers. Share long content through a file reference.

## Task and quality policy

{review}
{workflow}
Global review is optional, on demand. This template creates no global reviewer
and does not start a review-monitoring automation. Keep checks and reports
proportional to the actual task. Never turn a setup acknowledgement into PASS.

## Permissions and file ownership

Inherit each provider's existing permission and approval settings. This template
does not request Full Access, change sandbox policy, alter credentials or expand
the user's task authorization. Existing project instructions still apply.
Role and directory separation are workflow boundaries, not OS security isolation.
Keep temporary work and deliverables in the configured team work/output roots.
Research roles and development controller/reviewer do not edit project files.
The development worker writes only the explicitly assigned project change.
Do not run DB writes, migrations, publish, deploy or restart services merely to
set up a team. Coordinate any task-authorized shared test/build/host activity.
"""


def _shell_quote(value):
    return "'" + str(value).replace("'", "''") + "'" if os.name == "nt" else shlex.quote(str(value))


def _workflow_instructions(config, role_id):
    required = config["review"]["required"]
    if not config.get("workflow_cli") or not config.get("python"):
        return ("\n## Review policy\n\nIndependent review is required, but the task workflow is not connected. "
                "Report this concrete setup limitation; do not invent PASS or claim a completion gate ran.\n"
                if required else "")
    shell = "powershell" if os.name == "nt" else "sh"
    command = ("& " if os.name == "nt" else "") + _shell_quote(config["python"]) + " -B " + _shell_quote(config["workflow_cli"])
    team_arg = " --team-dir " + _shell_quote(config["team_root"]) + " --id TASK_ID"
    request = _shell_quote(str(Path(config["team_root"]) / "work-items" / "TASK_ID-request.json"))
    submission = _shell_quote(str(Path(config["team_root"]) / "work-items" / "TASK_ID-submission.json"))
    report = _shell_quote(str(Path(config["team_root"]) / "reports" / "TASK_ID-review.md"))
    if not required:
        return f"""
## Optional task tracking

Complete routine standalone requests directly. Do not create records or review
work merely to satisfy this template. If the user explicitly wants tracking,
the owner can use the workflow CLI; Quick Task completion has no review digest.
Existing project rules and task permission boundaries still apply.

For optional tracking, write an absolute JSON request file with `task` and
`acceptance_criteria` (an array of concrete checks). Replace `TASK_ID` consistently
with one stable ID, begin before work, and complete when the outcome is verified:

```{shell}
{command} begin{team_arg} --request {request}
{command} complete{team_arg} --summary 'Short factual completion summary'
```
"""
    lead = config["review"]["lead"]
    shared = f"""
## Tracked task workflow

Work starts only from an explicit user task. `TASK_ID` below is a stable task ID,
not a new ID on every retry. The owner begins a record before substantive work.
Request/submission arguments are absolute JSON **file paths**, not inline JSON.
Put input JSON files directly under `work-items/`; the workflow owns the generated
`work-items/TASK_ID/task.json` and review records. Do not edit those records by hand.
Keep final deliverables under `outputs/TASK_ID/` and independent findings under
`reports/TASK_ID/`. Use new filenames for revised evidence; preserve earlier outputs.
Never assume a message, a report filename, or a setup ACK proves PASS.

For a deliberate status check, run the read-only command below. Replies and work
arrive through Radio push; do not poll status or inbox while awaiting another agent.

```{shell}
{command} status{team_arg}
```
"""
    if role_id == lead:
        shared += f"""
### Owner actions

Write the begin request file with `task` (the agreed user request) and
`acceptance_criteria` (an array of concrete checks), then begin it:

```{shell}
{command} begin{team_arg} --request {request}
```

For Development, begin hands the assigned work to the worker through Radio.
Coordinate fixes after CHANGES_REQUIRED; investigate BLOCKED or stale evidence
without marking it PASS. Complete only after the exact current digest has an
independent PASS and the requested outcome has been verified:

```{shell}
{command} complete{team_arg} --digest CURRENT_DIGEST --summary 'Short factual completion summary'
```
"""
    if role_id in ("worker", "researcher"):
        shared += f"""
### Submit stable work

Write a submission JSON file containing `task`, `summary`, `acceptance_criteria`,
`artifacts` (an array of objects with absolute `path`), `repos` (an array of objects
with absolute `path`, or an empty array for a research report), `shared_impact`
(an array), and `reviewer_contributed: false`. Copy task and criteria unchanged
from the owner's record. List the actual output and validation evidence.

```{shell}
{command} submit{team_arg} --request {submission}
```

Submit requests the configured independent review through Radio. Preserve that
submitted version during review; a later implementation change needs a fresh
submission/digest. Do not call result as if you were the independent reviewer.
"""
    if role_id == "reviewer":
        shared += f"""
### Independent review

Wait for an actual submitted task and digest. Read the exact submitted artifacts,
acceptance criteria and relevant evidence. Do not edit implementation or submitted
artifacts. Write a concise review report, then record the observed decision:

```{shell}
{command} result{team_arg} --digest SUBMITTED_DIGEST --decision PASS --report {report}
```

Choose PASS only for a supported independent conclusion; otherwise use
CHANGES_REQUIRED or BLOCKED with concrete findings. Identity and digest are
validated by the command. A global escalation route is not configured by this
template: report the specific block to the owner instead of silently passing it.
"""
    return shared


def _role_brief(config, role_id):
    role = config["roles"][role_id]
    return f"""# {role['name']} / {role_id}

Read `{Path(config['team_root']) / 'BRIEF.md'}` and
`{Path(config['team_root']) / 'config.json'}` before accepting a task.
Role: `{role['kind']}`. Scope: {role['scope']}
Working directory: `{role['cwd']}`.
Project reference: `{config['project_path']}`.
Account profile reference: `{role['profile_id']}`. The launcher resolves the
profile; do not inspect, copy or replace its credentials or authentication files.

Start with one concise ready message, then wait. Do not start project work,
research, a review, a host or a test merely because this pane was created.
Only act on an explicit task within this role's scope. Keep outputs in the
configured owned directories and preserve unrelated work.
""" + _workflow_instructions(config, role_id)


def scaffold(prepared, team_root, *, workspace_id, runtime=None):
    """Create a new complete metadata tree, or leave the destination untouched."""
    config = _revalidate(prepared, workspace_id)
    raw_target = Path(team_root) if isinstance(team_root, (str, os.PathLike)) else None
    if raw_target is not None and (raw_target.exists() or raw_target.is_symlink()):
        raise TeamPlanError("The team directory already exists. Existing teams are never overwritten.")
    target = _absolute(team_root, "team directory")
    project = Path(config["project_path"])
    if target == project or target.is_relative_to(project):
        raise TeamPlanError("Store team metadata outside the selected project directory.")
    if target.exists() or target.is_symlink():
        raise TeamPlanError("The team directory already exists. Existing teams are never overwritten.")
    if not target.parent.is_dir():
        raise TeamPlanError("Create the team state parent directory before preparing a team.")
    config.update(_runtime(runtime))
    config["team_root"] = str(target)
    config["frequency_display"] = workspace_id[1:].zfill(3)
    config["paths"] = {"metadata": str(target), "work": str(target / "work"),
                       "outputs": str(target / "outputs"), "work_items": str(target / "work-items"),
                       "reviews": str(target / "reviews"), "reports": str(target / "reports")}
    if "workflow_cli" in config and config["review"]["required"]:
        config["review"]["enforcement"] = "workflow_cli"
    for role_id, role in config["roles"].items():
        destination = role["cwd_target"]
        role["cwd"] = config["project_path"] if destination == "project" else config["paths"][destination]
        role["entrypoint"] = str(target / "roles" / (role_id + ".md"))
        role["ui_target"] = config["workspace_label"] + " / " + role["tab"]
    staging = None
    try:
        staging = Path(tempfile.mkdtemp(prefix=".team-stage-", dir=target.parent))
        for child in ("roles", "work", "outputs", "work-items", "reviews", "reports"):
            (staging / child).mkdir()
        (staging / "config.json").write_text(json.dumps(config, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        (staging / "BRIEF.md").write_text(_brief(config), encoding="utf-8")
        for role_id in config["roles"]:
            (staging / "roles" / (role_id + ".md")).write_text(_role_brief(config, role_id), encoding="utf-8")
        _publish_new(staging, target)
        staging = None
    except FileExistsError:
        raise TeamPlanError("The team directory already exists. Existing teams are never overwritten.") from None
    except OSError as exc:
        if exc.errno in (errno.EEXIST, errno.ENOTEMPTY):
            raise TeamPlanError("The team directory already exists. Existing teams are never overwritten.") from None
        raise TeamPlanError("The team files could not be prepared. No existing team was changed.") from None
    finally:
        if staging is not None and staging.parent == target.parent and staging.name.startswith(".team-stage-"):
            shutil.rmtree(staging)
    return deepcopy(config)
