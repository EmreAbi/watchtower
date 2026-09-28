"""Radio context for Gemini's native SessionStart hook; never submits a task."""
from __future__ import annotations

import json
import os
from pathlib import Path
import re
import shlex
import sys
import tempfile

HOOK_NAME = "watchtower-radio"
PANE = re.compile(r"w[1-9][0-9]*:p[1-9][0-9]*\Z")
HANDLE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,47}\Z")
SESSION = re.compile(r"[A-Za-z0-9_-]{1,128}\Z")
MAX_BYTES = 1024 * 1024


def _home(env):
    value = env.get("GEMINI_CLI_HOME", "")
    if not value or not Path(value).is_absolute():
        raise ValueError("Gemini account home is unavailable.")
    return Path(value).resolve() / ".gemini"


def _read_bytes(path):
    if path.is_symlink() or path.stat().st_size > MAX_BYTES:
        raise ValueError("Gemini settings could not be safely read.")
    with path.open("rb") as stream:
        raw = stream.read(MAX_BYTES + 1)
    if len(raw) > MAX_BYTES:
        raise ValueError("Gemini settings could not be safely read.")
    return raw


def _read(path):
    value = json.loads(_read_bytes(path))
    if not isinstance(value, dict):
        raise ValueError("Gemini settings must be a JSON object.")
    return value


def _write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(value, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def prepare_hook(env):
    """Merge our hook only on an explicit new-agent request, before Radio joins."""
    home = _home(env)
    home.mkdir(parents=True, exist_ok=True)
    path = home / "settings.json"
    before = _read_bytes(path) if path.exists() else None
    settings = json.loads(before) if before is not None else {}
    if not isinstance(settings, dict):
        raise ValueError("Gemini settings must be a JSON object.")
    config = settings.get("hooksConfig", {})
    if not isinstance(config, dict) or config.get("enabled") is False:
        raise ValueError("Gemini hooks are disabled. Enable hooks before starting a Radio agent.")
    disabled = config.get("disabled", [])
    if not isinstance(disabled, list) or HOOK_NAME in disabled:
        raise ValueError("The Gemini Radio hook is disabled; existing settings were preserved.")
    hooks = settings.setdefault("hooks", {})
    if not isinstance(hooks, dict):
        raise ValueError("Gemini hook settings are invalid; existing settings were preserved.")
    groups = hooks.setdefault("SessionStart", [])
    if not isinstance(groups, list):
        raise ValueError("Gemini SessionStart settings are invalid; existing settings were preserved.")
    argv = [sys.executable, "-I", "-B", str(Path(__file__).resolve())]
    if any(any(c in part for c in "\r\n%\"!") for part in argv):
        raise ValueError("Gemini hook paths contain unsupported shell characters.")
    command = f'"{argv[0]}" -I -B "{argv[-1]}"' if os.name == "nt" else shlex.join(argv)
    replacement = dict(type="command", name=HOOK_NAME, command=command, timeout=3000)
    kept = []
    for group in groups:
        if not isinstance(group, dict) or not isinstance(group.get("hooks"), list):
            raise ValueError("Gemini SessionStart settings are invalid; existing settings were preserved.")
        remaining = [h for h in group["hooks"] if not isinstance(h, dict) or h.get("name") != HOOK_NAME]
        if remaining:
            kept.append({**group, "hooks": remaining})
    kept.append({"hooks": [replacement]})
    hooks["SessionStart"] = kept
    if before is not None and settings == json.loads(before.decode("utf-8-sig")):
        return
    if (_read_bytes(path) if path.exists() else None) != before:
        raise ValueError("Gemini settings changed. Retry the new-agent action.")
    if before is not None:
        backup = home / "watchtower-radio" / "settings.before-hook.json"
        if not backup.exists():
            _write(backup, json.loads(before.decode("utf-8-sig")))
    _write(path, settings)


def write_context(env, argv, briefing):
    pane, handle = env.get("HERDR_PANE_ID", ""), env.get("RADIO_HANDLE", "")
    scope = env.get("RADIO_JOINED_SCOPE", "")
    if not PANE.fullmatch(pane) or not HANDLE.fullmatch(handle) or not scope:
        raise ValueError("Gemini Radio identity is incomplete.")
    session = None
    for option in ("--session-id", "--resume"):
        if option in argv:
            offset = argv.index(option) + 1
            session = argv[offset] if offset < len(argv) else None
    if not isinstance(session, str) or not SESSION.fullmatch(session):
        raise ValueError("Gemini Radio session identity is unavailable.")
    if not isinstance(briefing, str) or len(briefing) > 65536:
        raise ValueError("Gemini Radio context is invalid.")
    _write(_home(env) / "watchtower-radio" / (pane.replace(":", "-") + "-" + session + ".json"),
           dict(pane=pane, handle=handle, scope=scope, session=session, briefing=briefing))


def context_for(event, env):
    pane, handle = env.get("HERDR_PANE_ID", ""), env.get("RADIO_HANDLE", "")
    if not PANE.fullmatch(pane) or not HANDLE.fullmatch(handle) or not env.get("RADIO_JOINED_SCOPE"):
        return {}
    if not isinstance(event, dict) or event.get("hook_event_name") != "SessionStart":
        return {}
    session = event.get("session_id")
    if not isinstance(session, str) or not SESSION.fullmatch(session):
        return {}
    path = _home(env) / "watchtower-radio" / (pane.replace(":", "-") + "-" + session + ".json")
    if not path.is_file():
        return {}
    context = _read(path)
    if (context.get("pane") != pane or context.get("handle") != handle
            or context.get("scope") != env["RADIO_JOINED_SCOPE"]
            or context.get("session") != event.get("session_id")):
        return {}
    briefing = context.get("briefing")
    if not isinstance(briefing, str) or len(briefing) > 65536:
        return {}
    return {"hookSpecificOutput": {"hookEventName": "SessionStart", "additionalContext": briefing}}


def main():
    try:
        raw = sys.stdin.buffer.read(MAX_BYTES + 1)
        result = context_for(json.loads(raw), os.environ) if len(raw) <= MAX_BYTES else {}
    except (OSError, ValueError, TypeError):
        result = {}
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
