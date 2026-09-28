"""Add the bundled OpenCode V2 session hook to one explicitly launched child.

OpenCode's CLI config loader merges OPENCODE_CLI_CONFIG_CONTENT over cli.json;
arrays replace their predecessors. Preserve the effective plugins list and all
existing overlay fields. Never write account settings or inspect credentials.
The provider's native migration stays responsible for legacy TUI settings.

Contract: https://github.com/anomalyco/opencode/blob/v2/packages/cli/src/config/config.ts
"""
from __future__ import annotations

from copy import deepcopy
import json
import os
from pathlib import Path


HOOK_ROOT = Path(__file__).resolve().parent / "opencode-hooks"
HOOK_ID = "herdr.opencode.session-selection"
MAX_CONFIG_BYTES = 1024 * 1024


class OpenCodeContextError(ValueError):
    pass


def _jsonc(text):
    """Strip JSONC comments/trailing commas without altering quoted strings."""
    if not isinstance(text, str) or len(text.encode("utf-8")) > MAX_CONFIG_BYTES:
        raise OpenCodeContextError("OpenCode CLI settings exceed the supported size.")
    output = []
    index = 0
    quoted = escaped = False
    while index < len(text):
        value = text[index]
        if quoted:
            output.append(value)
            if escaped:
                escaped = False
            elif value == "\\":
                escaped = True
            elif value == '"':
                quoted = False
        elif value == '"':
            quoted = True
            output.append(value)
        elif text[index:index + 2] == "//":
            end = text.find("\n", index + 2)
            index = len(text) if end < 0 else end
            output.append("\n")
            continue
        elif text[index:index + 2] == "/*":
            end = text.find("*/", index + 2)
            if end < 0:
                raise OpenCodeContextError("OpenCode CLI settings contain invalid JSONC.")
            output.append(" ")
            index = end + 2
            continue
        else:
            output.append(value)
        index += 1
    # A second string-aware pass removes only commas preceding a closing brace.
    plain = "".join(output)
    output = []
    quoted = escaped = False
    for index, value in enumerate(plain):
        if quoted:
            output.append(value)
            if escaped:
                escaped = False
            elif value == "\\":
                escaped = True
            elif value == '"':
                quoted = False
        elif value == '"':
            quoted = True
            output.append(value)
        elif value == ",":
            following = index + 1
            while following < len(plain) and plain[following].isspace():
                following += 1
            if following < len(plain) and plain[following] in "}]":
                continue
            output.append(value)
        else:
            output.append(value)
    try:
        result = json.loads("".join(output), parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
        if not isinstance(result, dict):
            raise ValueError()
        return result
    except (ValueError, TypeError, RecursionError):
        raise OpenCodeContextError("OpenCode CLI settings contain invalid JSONC; repair them before launching this account.") from None


def _read(path):
    if not path.exists():
        return {}
    try:
        with path.open("rb") as stream:
            data = stream.read(MAX_CONFIG_BYTES + 1)
        if len(data) > MAX_CONFIG_BYTES:
            raise OpenCodeContextError("OpenCode CLI settings exceed the supported size.")
        return _jsonc(data.decode("utf-8-sig"))
    except (OSError, UnicodeError):
        raise OpenCodeContextError("OpenCode CLI settings could not be read; repair the selected account's settings.") from None


def _absolute(value):
    if (not isinstance(value, (str, os.PathLike)) or not str(value)
            or any(ord(c) < 32 for c in str(value)) or not Path(value).is_absolute()):
        raise OpenCodeContextError("OpenCode profile configuration paths must be absolute.")
    return Path(value).resolve()


def _plugins(config):
    items = config.get("plugins", [])
    if not isinstance(items, list):
        raise OpenCodeContextError("OpenCode CLI plugins must be a list; existing settings were preserved.")
    for item in items:
        if isinstance(item, str) and item:
            continue
        if (isinstance(item, dict) and set(item) <= {"package", "options"}
                and isinstance(item.get("package"), str) and item["package"]
                and ("options" not in item or isinstance(item["options"], dict))):
            continue
        raise OpenCodeContextError("OpenCode CLI plugin settings are invalid; existing settings were preserved.")
    return deepcopy(items)


def _matches(selector):
    return (selector == "*" or selector == HOOK_ID
            or (selector.endswith(".*") and HOOK_ID.startswith(selector[:-1])))


def prepare_context(env):
    """Mutate only this child environment, at explicit OpenCode agent launch.

    Missing hooks remain an identity error in the task workflow; this helper
    never fabricates a session, selects a session, or registers Radio bindings.
    """
    if (not isinstance(env, dict) or env.get("HERDR_ENV") != "1"
            or not env.get("HERDR_PANE_ID") or not env.get("HERDR_SOCKET_PATH")):
        raise OpenCodeContextError("OpenCode team agents must start inside their Watchtower pane.")
    if not (HOOK_ROOT / "tui.js").is_file():
        raise OpenCodeContextError("The bundled OpenCode session hook is missing. Repair the Watchtower installation.")
    if env.get("OPENCODE_CONFIG_DIR"):
        directory = _absolute(env["OPENCODE_CONFIG_DIR"])
    elif env.get("XDG_CONFIG_HOME"):
        directory = _absolute(env["XDG_CONFIG_HOME"]) / "opencode"
    else:
        directory = Path.home() / ".config" / "opencode"
    state = (_absolute(env["XDG_STATE_HOME"]) / "opencode" if env.get("XDG_STATE_HOME")
             else Path.home() / ".local" / "state" / "opencode")
    config_path = directory / "cli.json"
    if not config_path.exists() and ((directory / "tui.json").exists() or (state / "kv.json").exists()):
        raise OpenCodeContextError("Open this OpenCode account once to finish its native CLI settings migration, then retry the team launch.")
    existing = _read(config_path)
    overlay = _jsonc(env["OPENCODE_CLI_CONFIG_CONTENT"]) if env.get("OPENCODE_CLI_CONFIG_CONTENT") else {}
    entries = _plugins(overlay if "plugins" in overlay else existing)
    # Insert before user directives so normal OpenCode enable/disable ordering
    # remains authoritative. An explicit final disable must never be reversed.
    enabled = True
    for item in entries:
        target = item if isinstance(item, str) else item["package"]
        if target.startswith("-") and _matches(target[1:]):
            enabled = False
        elif _matches(target):
            enabled = True
    if not enabled:
        raise OpenCodeContextError("The OpenCode session integration is disabled in this account's CLI plugins. Enable it explicitly before using verified team workflows.")
    hook = str(HOOK_ROOT)
    if not any((item if isinstance(item, str) else item["package"]) == hook for item in entries):
        entries.insert(0, hook)
    overlay["plugins"] = entries
    encoded = json.dumps(overlay, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
    if len(encoded.encode("utf-8")) > MAX_CONFIG_BYTES:
        raise OpenCodeContextError("OpenCode CLI settings exceed the supported size.")
    env["OPENCODE_CLI_CONFIG_CONTENT"] = encoded
    return {"state": "prepared", "plugin": hook}
