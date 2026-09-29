"""Explicit Accounts actions through the existing Watchtower plugin API.

Only a dedicated new plugin tab runs an agent. Nothing types commands into an
existing pane. One-use launch tickets prevent a restored plugin tab from
silently creating another agent/session.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import re
import secrets
import subprocess
import sys
import time
import webbrowser

ROOT = Path(__file__).resolve().parent
PLUGIN = "watchtower-accounts"
TICKET_ENV = "WATCHTOWER_ACCOUNTS_LAUNCH"
_CLI_JSON_LIMIT = 4 * 1024 * 1024
_CLI_SILENT_OK = {("pane", "run"), ("pane", "report-agent-session")}


def _host_env():
    env = dict(os.environ)
    binary = Path(env.get("HERDR_BIN_PATH", ""))
    socket = env.get("HERDR_SOCKET_PATH", "")
    if (not binary.is_absolute() or not binary.is_file()
            or binary.stem.lower() not in ("watchtower", "herdr")
            or not socket or not Path(socket).is_absolute()):
        raise ValueError("Open Accounts from the Watchtower menu.")
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    return str(binary), env


def _cli(args):
    binary, env = _host_env()
    flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
    try:
        result = subprocess.run([binary, *args], env=env, capture_output=True,
                                text=True, encoding="utf-8", timeout=15,
                                creationflags=flags)
    except (OSError, ValueError, subprocess.TimeoutExpired) as exc:
        raise RuntimeError("Watchtower did not return a valid response; no automatic retry.") from exc
    # Native CLI success envelopes use stdout, but API failures are emitted as
    # JSON on stderr. Parse only complete, bounded envelopes; never mine JSON
    # fragments from diagnostics or reflect their contents into Accounts.
    envelopes = []
    for output in (result.stdout, getattr(result, "stderr", "")):
        if not isinstance(output, str) or len(output) > _CLI_JSON_LIMIT:
            raise RuntimeError("Watchtower returned an invalid response; no automatic retry.")
        try:
            envelopes.append(json.loads(output) if output.strip() else None)
        except (ValueError, RecursionError):
            envelopes.append(None)
    response = envelopes[0]
    errors = [item for item in envelopes if isinstance(item, dict) and "error" in item]
    if errors:
        codes = []
        for item in errors:
            error = item["error"]
            code = error.get("code") if isinstance(error, dict) else None
            codes.append(code if isinstance(code, str) and re.fullmatch(r"[a-z_]{1,80}", code) else "request_failed")
        # Conflicting channels cannot prove a particular failure classification.
        code = codes[0] if all(code == codes[0] for code in codes) else "request_failed"
        raise RuntimeError(f"Watchtower: {code}. No automatic retry.")
    # These native commands use send_ok_request: success intentionally prints
    # nothing. Keep this explicit; an empty read response proves no state.
    if (tuple(args[:2]) in _CLI_SILENT_OK and result.returncode == 0
            and not result.stdout.strip() and not getattr(result, "stderr", "").strip()):
        return {"type": "ok"}
    if not isinstance(response, dict):
        raise RuntimeError("Watchtower returned an invalid response; no automatic retry.")
    if result.returncode:
        raise RuntimeError("Watchtower: request_failed. No automatic retry.")
    payload = response.get("result")
    if not isinstance(payload, dict):
        raise RuntimeError("Watchtower returned an invalid result; no automatic retry.")
    return payload


def list_workspaces():
    workspaces = _cli(["workspace", "list"]).get("workspaces", [])
    panes = _cli(["pane", "list"]).get("panes", [])
    result = []
    for workspace in workspaces:
        wid = workspace.get("workspace_id", "")
        candidates = [pane for pane in panes if pane.get("workspace_id") == wid]
        candidates.sort(key=lambda p: (not p.get("focused", False),
                                        p.get("tab_id") != workspace.get("active_tab_id")))
        cwd = next((p.get("cwd") for p in candidates if p.get("cwd")), None)
        if re.fullmatch(r"w[0-9]+", wid):
            result.append({"id": wid, "name": workspace.get("label") or wid,
                           "cwd": cwd, "focused": workspace.get("focused", False)})
    return result


def _ticket_dir(service):
    return Path(service.home) / "state" / "accounts" / "launches"


def launch_profile(service, profile, handle, workspace, model=None):
    """Validate first, then open a fresh argv-based tab once (never retry)."""
    selected = next((w for w in list_workspaces() if w["id"] == workspace), None)
    if not selected or not selected["cwd"] or not Path(selected["cwd"]).is_dir():
        raise ValueError("Choose an existing workspace with an available working directory.")
    service.ensure_registered(profile)  # explicit Start may link an existing provider home
    service.launch_plan(profile, handle, workspace=workspace, model=model)  # preflight before tab creation
    _, env = _host_env()
    ticket = {"profile": profile, "handle": handle, "workspace": workspace,
              "cwd": selected["cwd"], "socket": env["HERDR_SOCKET_PATH"],
              "created": time.time(), "model": model}
    token = secrets.token_hex(24)
    directory = _ticket_dir(service)
    directory.mkdir(parents=True, exist_ok=True)
    with (directory / f"{token}.json").open("x", encoding="utf-8") as stream:
        json.dump(ticket, stream)
    result = _cli(["plugin", "pane", "open", "--plugin", PLUGIN,
                   "--entrypoint", "launch-agent", "--placement", "tab",
                   "--workspace", workspace, "--cwd", selected["cwd"],
                   "--env", f"{TICKET_ENV}={token}", "--focus"])
    pane = result.get("plugin_pane", {}).get("pane", {})
    if not pane.get("pane_id") or pane.get("workspace_id") != workspace:
        raise RuntimeError("Agent tab delivery is unconfirmed; inspect Watchtower before retrying.")
    return {"pane_id": pane["pane_id"], "workspace_id": workspace,
            "profile_id": profile, "status": "launch_requested"}


def plan_environment(plan, base=None):
    env = dict(os.environ if base is None else base)
    for key in (*plan.get("unset_env", []), *plan.get("env", {})):
        # Windows environment keys are case insensitive.
        for existing in list(env):
            if existing.upper() == key.upper():
                env.pop(existing, None)
    env.update(plan.get("env", {}))
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    return env


def run_connect(service, profile):
    plan = service.connect_plan(profile)
    # Interactive: terminal ownership is suspended by the Textual caller.
    # Do not capture credentials, auth URLs, or native login prompts.
    if plan.get("provider") == "gemini":
        print("Gemini: choose Login with Google. Use /auth to change the account, then /quit to return to Accounts.", flush=True)
    elif plan.get("provider") == "claude":
        print("Claude: complete browser sign-in; Accounts returns when the login command finishes.", flush=True)
    return subprocess.run(plan["argv"], env=plan_environment(plan),
                          cwd=plan.get("cwd") or None).returncode


def _valid_login_link(url):
    from browser_login import BrowserLoginError, validate_auth_url
    try:
        return validate_auth_url(url)
    except BrowserLoginError:
        return None


def open_browser_login(url):
    """Open only the validated provider URL after the user's button click."""
    link = _valid_login_link(url)
    if link is None:
        return False
    try:
        return bool(webbrowser.open(link, new=2))
    except Exception:
        return False


def copy_login_link(url):
    """Copy the complete link through stdin; never put OAuth data in argv."""
    link = _valid_login_link(url)
    if link is None:
        return False
    if sys.platform == "darwin":
        try:
            result = subprocess.run(
                ["/usr/bin/pbcopy"], input=link, text=True, encoding="utf-8",
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                timeout=5, check=False,
            )
            return result.returncode == 0
        except (OSError, subprocess.SubprocessError):
            return False
    if os.name != "nt":
        return False
    powershell = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32/WindowsPowerShell/v1.0/powershell.exe"
    command = (
        "$ErrorActionPreference='Stop'; "
        "[Console]::InputEncoding=[System.Text.Encoding]::UTF8; "
        "Set-Clipboard -Value ([Console]::In.ReadToEnd()) -ErrorAction Stop"
    )
    try:
        result = subprocess.run(
            [str(powershell), "-NoProfile", "-NonInteractive", "-STA", "-Command", command],
            input=link, text=True, encoding="utf-8", stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL, creationflags=subprocess.CREATE_NO_WINDOW,
            timeout=5, check=False,
        )
        return result.returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def claim_ticket(service, token, env=None):
    env = os.environ if env is None else env
    if not re.fullmatch(r"[a-f0-9]{48}", token or ""):
        raise ValueError("Launch request missing. Open Accounts and choose New agent.")
    directory = _ticket_dir(service)
    ticket = json.loads((directory / f"{token}.json").read_text(encoding="utf-8"))
    if not 0 <= time.time() - ticket["created"] <= 600:
        raise ValueError("Launch request expired. Open Accounts and choose New agent.")
    if (env.get("HERDR_PLUGIN_ID") != PLUGIN
            or env.get("HERDR_PLUGIN_ENTRYPOINT_ID") != "launch-agent"
            or ticket["workspace"] != env.get("HERDR_WORKSPACE_ID")
            or os.path.normcase(ticket["socket"]) != os.path.normcase(env.get("HERDR_SOCKET_PATH", ""))
            or not re.fullmatch(re.escape(ticket["workspace"]) + r":p[0-9]+",
                                env.get("HERDR_PANE_ID", ""))):
        raise ValueError("Launch request does not match this pane/workspace.")
    # O_EXCL makes claiming atomic, even if the UI is restored twice.
    try:
        with (directory / f"{token}.claimed").open("x", encoding="utf-8") as stream:
            json.dump({"pane_id": env["HERDR_PANE_ID"], "claimed": time.time()}, stream)
    except FileExistsError as exc:
        raise ValueError("This launch was already used. Existing sessions are not restarted automatically.") from exc
    return ticket


def _native_provider_argv(plan, args):
    """Build native arguments without changing the provider's permissions."""
    from backend import provider_command
    provider, rest = args[0], list(args[1:])
    if provider == "codex":
        rest = ["-c", "check_for_update_on_startup=false", *rest]
        if plan.get("kind") == "switch":
            # Built and revalidated from the recorded turn context by the
            # one-use account-switch runner. Retain permissions as well as model.
            rest = [*plan["resume_options"], *rest]
        elif plan.get("model") is not None:
            from models import valid_model
            if not valid_model("codex", plan["model"]):
                raise ValueError("Invalid Codex model selection.")
            rest = ["-m", plan["model"], *rest]
    if provider == "opencode":
        rest = [*plan.get("provider_extra_args", []), *rest]
    return [*provider_command(provider), *rest]


def run_radio_plan(plan):
    """Keep vendored Radio unchanged, but use native argv for provider launch."""
    adapter = ROOT.parent / "radio" / "watchtower_radio.py"
    adapter_ns = {"__file__": str(adapter), "__name__": "watchtower_accounts_radio"}
    exec(compile(adapter.read_bytes(), str(adapter), "exec"), adapter_ns)
    adapter_ns["verified_files"]()
    radio = ROOT.parent / "radio" / "vendor" / "AgentRadio" / "bin" / "radio"
    argv = plan["argv"]
    position = next((i for i, arg in enumerate(argv)
                     if Path(arg).is_absolute() and Path(arg).resolve() == radio.resolve()), None)
    if position is None or argv[position + 1:position + 2] != ["join"]:
        raise ValueError("Unexpected Radio launch plan.")
    env = plan_environment(plan)
    os.environ.clear()
    os.environ.update(env)
    namespace = {"__file__": str(radio), "__name__": "watchtower_accounts_join"}
    exec(compile(radio.read_bytes(), str(radio), "exec"), namespace)
    if plan.get("provider") == "gemini":
        from gemini_context import prepare_hook
        prepare_hook(env)

    def launch_native(args, child_env):
        provider = args[0]
        native_argv = _native_provider_argv(plan, args)
        if provider == "codex" and plan.get("kind") == "switch":
            from switch_launch import run_codex_resume
            return run_codex_resume(plan, native_argv, child_env)
        if provider == "opencode":
            from opencode_context import prepare_context
            prepare_context(child_env)
        if provider == "gemini":
            from gemini_context import write_context
            write_context(child_env, args, namespace["briefing_text"](
                child_env.get("RADIO_HANDLE", ""), child_env.get("RADIO_JOINED_SCOPE", "")))
        return subprocess.run(native_argv, env=child_env).returncode

    namespace["launch_agent"] = launch_native
    namespace["utf8_streams"]()
    previous_argv = sys.argv
    try:
        sys.argv = [str(radio), *argv[position + 1:]]
        return namespace["main"]()
    finally:
        sys.argv = previous_argv


def main():
    from backend import AccountError, AccountService
    try:
        service = AccountService()
        ticket = claim_ticket(service, os.environ.get(TICKET_ENV, ""))
        plan = service.launch_plan(ticket["profile"], ticket["handle"], workspace=ticket["workspace"], model=ticket.get("model"))
        os.chdir(ticket["cwd"])
        return run_radio_plan(plan)
    except (AccountError, OSError, ValueError, RuntimeError) as exc:
        print(f"Accounts: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
