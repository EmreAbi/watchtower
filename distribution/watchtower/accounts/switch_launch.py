"""Publish resume identity only for the native Codex child this runner owns.

This is an explicit account-switch bridge, not a replacement for provider hooks.
It never sends a prompt, changes configuration, retries launch or kills a child.
"""
from __future__ import annotations

import ntpath
import json
import os
from pathlib import Path, PureWindowsPath
import re
import subprocess
import time

try:
    from .backend import AccountError, _atomic_json
    from .integration import _cli
    from .switch_runtime import _same_path, _session
except ImportError:
    from backend import AccountError, _atomic_json
    from integration import _cli
    from switch_runtime import _same_path, _session

_UUID = re.compile(r"[a-fA-F0-9]{8}(?:-[a-fA-F0-9]{4}){3}-[a-fA-F0-9]{12}\Z")
_PANE = re.compile(r"w[1-9][0-9]*:p[1-9][0-9]*\Z")
_UNCONFIRMED_CHILDREN = []
_MAX_LINE = 1024 * 1024
_MAX_APPEND = 16 * 1024 * 1024
_GATES = ("trust this folder", "folder access", "windows sandbox setup", "set up default sandbox",
          "use non-admin sandbox", "sign in with chatgpt", "sign in to codex", "log in to codex",
          "paste your api key", "enter your api key", "press enter to continue",
          "would you like to run the following command?")


def _fail():
    raise AccountError("switch_resume_unverified",
                       "Codex resume could not be verified. Inspect this pane before retrying; a started process was not stopped.")


def _validate(plan, argv, env):
    if not isinstance(plan, dict) or not isinstance(env, dict):
        _fail()
    candidate = plan.get("switch_candidate")
    if (plan.get("kind") != "switch" or plan.get("provider") != "codex"
            or not isinstance(plan.get("profile_id"), str) or not plan["profile_id"]
            or not isinstance(plan.get("switch_receipt_path"), str)
            or not Path(plan["switch_receipt_path"]).is_absolute()
            or Path(plan["switch_receipt_path"]).is_symlink()
            or not isinstance(candidate, dict)
            or candidate.get("provider") != "codex"
            or not all(isinstance(candidate.get(key), str) for key in ("pane_id", "session_id", "workspace", "label"))
            or not _PANE.fullmatch(candidate.get("pane_id", ""))
            or not _UUID.fullmatch(candidate.get("session_id", ""))
            or not isinstance(candidate.get("terminal_id"), str) or not candidate["terminal_id"]
            or candidate.get("workspace") != candidate["pane_id"].split(":")[0]
            or not isinstance(candidate.get("label"), str)
            or not _same_path(candidate.get("cwd"), candidate.get("cwd"))
            or not isinstance(argv, list) or len(argv) < 3
            or not all(isinstance(arg, str) and "\x00" not in arg for arg in argv)
            or argv[-2:] != ["resume", candidate["session_id"]]
            or ntpath.basename(argv[0]).lower() not in ("codex", "codex.exe")
            or not (Path(argv[0]).is_absolute() or PureWindowsPath(argv[0]).is_absolute())
            or env.get("HERDR_PANE_ID") != candidate["pane_id"]
            or env.get("HERDR_WORKSPACE_ID") != candidate["workspace"]
            or not _same_path(env.get("CODEX_HOME"), plan.get("env", {}).get("CODEX_HOME"))):
        _fail()
    return candidate


def _proof(cli, candidate, child, argv):
    if child.poll() is not None:
        _fail()
    pane = cli(["pane", "get", candidate["pane_id"]]).get("pane")
    process = cli(["pane", "process-info", "--pane", candidate["pane_id"]]).get("process_info")
    if (not isinstance(pane, dict) or not isinstance(process, dict)
            or pane.get("pane_id") != candidate["pane_id"]
            or pane.get("terminal_id") != candidate["terminal_id"]
            or pane.get("workspace_id") != candidate["workspace"]
            or pane.get("label") != candidate["label"]
            or not _same_path(pane.get("cwd"), candidate.get("pane_cwd", candidate["cwd"]))
            or process.get("pane_id") != candidate["pane_id"]
            or pane.get("agent") not in (None, "codex")):
        _fail()
    rows = process.get("foreground_processes")
    session = pane.get("agent_session")
    if isinstance(session, dict) and session.get("value") not in (None, candidate["session_id"]):
        _fail()
    if not isinstance(rows, list) or len(rows) > 64:
        _fail()
    owned = [row for row in rows if isinstance(row, dict) and row.get("pid") == child.pid]
    if not owned or pane.get("agent") is None:
        return None  # Native detection can settle after Popen returns.
    if (len(rows) != 1 or len(owned) != 1
            or process.get("foreground_process_group_id") != child.pid):
        _fail()
    native = owned[0]
    actual = native.get("argv")
    if (ntpath.basename(native.get("name", "")).lower() not in ("codex", "codex.exe")
            or not _same_path(native.get("cwd"), candidate["cwd"])
            or not isinstance(actual, list) or len(actual) != len(argv)
            or not _same_path(actual[0], argv[0]) or actual[1:] != argv[1:]
            or child.poll() is not None):
        _fail()
    return pane


def _settings_match(settings, plan):
    """Verify explicitly requested runtime settings, not TUI detector readiness."""
    if not isinstance(settings, dict) or settings.get("model") != plan.get("model"):
        return False
    options = plan.get("resume_options")
    if not isinstance(options, list) or len(options) % 2:
        return False
    flags, config = {}, {}
    for flag, value in zip(options[::2], options[1::2]):
        if not isinstance(flag, str) or not isinstance(value, str):
            return False
        if flag == "-c":
            key, equal, encoded = value.partition("=")
            if not equal:
                return False
            try:
                config[key] = json.loads(encoded)
            except ValueError:
                return False
        elif flag != "--add-dir":
            flags[flag] = value
    if flags.get("-m") != plan.get("model") or settings.get("approval_policy") != flags.get("-a"):
        return False
    policy = settings.get("sandbox_policy")
    profile = settings.get("permission_profile")
    if profile == {"type": "disabled"}:
        policy = {"type": "danger-full-access"}
    elif profile is not None:
        return False  # Unsupported permission representations are not equivalent.
    if not isinstance(policy, dict) or policy.get("type") != flags.get("-s"):
        return False
    for key, setting in (("model_reasoning_effort", "reasoning_effort"),
                         ("model_provider", "model_provider_id"), ("approvals_reviewer", "approvals_reviewer")):
        if key in config and settings.get(setting) != config[key]:
            return False
    if flags.get("-s") == "workspace-write":
        for key in ("network_access", "exclude_tmpdir_env_var", "exclude_slash_tmp"):
            if policy.get(key, False) != config.get("sandbox_workspace_write." + key, False):
                return False
        actual, expected = policy.get("writable_roots", []), config.get("sandbox_workspace_write.writable_roots", [])
        if (not isinstance(actual, list) or not isinstance(expected, list) or len(actual) != len(expected)
                or not all(any(_same_path(value, root) for root in actual) for value in expected)):
            return False
    return True


def _visible_ready(text):
    if not isinstance(text, str) or len(text) > 256 * 1024:
        return False
    # Only the current bottom viewport matters. Old transcript mentions do not
    # establish readiness; positive input+shortcut chrome must both be present.
    lines = [line.rstrip() for line in text.splitlines() if line.strip()][-24:]
    bottom = "\n".join(lines).casefold()
    if any(gate in bottom for gate in _GATES):
        return False
    prompt = [index for index, line in enumerate(lines)
              if re.match(r"^\s*[│┃]?\s*[›»❯](?:\s|$)", line)]
    shortcut = [index for index, line in enumerate(lines)
                if re.match(r"^\s*[│┃]?\s*\?\s+for shortcuts\b", line, re.IGNORECASE)]
    return bool(prompt and shortcut and shortcut[-1] == len(lines) - 1
                and 0 < shortcut[-1] - prompt[-1] <= 6)


def _native_visible(pane_id):
    # pane.read intentionally emits text rather than an RPC result envelope.
    # Keep its contents out of errors, receipts and logs.
    try:
        try:
            from .integration import _host_env
        except ImportError:
            from integration import _host_env
        binary, env = _host_env()
        result = subprocess.run([binary, "pane", "read", pane_id, "--source", "visible", "--lines", "40"],
                                env=env, capture_output=True, text=True, encoding="utf-8", timeout=10,
                                creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
        if result.returncode or not isinstance(result.stdout, str) or len(result.stdout) > 256 * 1024:
            _fail()
        return result.stdout
    except Exception:
        _fail()


class _FreshResume:
    """Read only complete metadata records appended after this launch's baseline."""
    def __init__(self, plan, candidate):
        self.stream = None
        try:
            path = Path(plan["switch_session_path"])
            home = Path(plan["env"]["CODEX_HOME"]).resolve()
            if (not path.is_absolute() or path.is_symlink() or not path.is_file()
                    or not path.resolve().is_relative_to(home / "sessions")
                    or not path.name.endswith(candidate["session_id"] + ".jsonl")):
                _fail()
            self.path, self.session = path, candidate["session_id"]
            self.stream = path.open("rb")
            stat = os.fstat(self.stream.fileno())
            self.identity, self.baseline = (stat.st_dev, stat.st_ino), stat.st_size
            observed = path.stat()
            if (observed.st_dev, observed.st_ino) != self.identity:
                _fail()
            if self.baseline:
                self.stream.seek(self.baseline - 1)
                if self.stream.read(1) != b"\n":
                    _fail()
            self.stream.seek(self.baseline)
            self.pending, self.read = b"", 0
        except Exception:
            self.close()
            _fail()

    def close(self):
        if self.stream is not None:
            self.stream.close()
            self.stream = None

    def ready(self, plan):
        stat = self.path.stat()
        if ((stat.st_dev, stat.st_ino) != self.identity or stat.st_size < self.baseline + self.read
                or stat.st_size - self.baseline > _MAX_APPEND):
            _fail()
        found = False
        while self.read < stat.st_size - self.baseline:
            chunk = self.stream.read(min(_MAX_LINE, stat.st_size - self.baseline - self.read))
            if not chunk:
                break
            self.read += len(chunk)
            lines = (self.pending + chunk).split(b"\n")
            self.pending = lines.pop()
            if len(self.pending) > _MAX_LINE:
                _fail()
            for line in lines:
                if len(line) > _MAX_LINE:
                    _fail()
                if b'"thread_settings_applied"' not in line:
                    continue
                try:
                    record = json.loads(line)
                except (ValueError, RecursionError):
                    _fail()
                data = record.get("payload") if isinstance(record, dict) else None
                if (record.get("type") != "event_msg" or not isinstance(data, dict)
                        or data.get("type") != "thread_settings_applied" or data.get("thread_id") != self.session):
                    continue
                if not _settings_match(data.get("thread_settings"), plan):
                    _fail()
                found = True
        return found


def run_codex_resume(plan, argv, env, *, popen=None, cli=None, clock=None,
                     sleep=None, now=None, timeout=15.0, interval=.2, visible=None):
    """Launch once, prove owned generation, report once, then wait normally."""
    candidate = _validate(plan, argv, env)
    fresh = _FreshResume(plan, candidate)
    popen, cli = popen or subprocess.Popen, cli or _cli
    visible = visible or _native_visible
    clock, sleep, now = clock or time.monotonic, sleep or time.sleep, now or time.time
    timeout, interval = min(20.0, max(.1, float(timeout))), min(1.0, max(.05, float(interval)))
    child = None
    try:
        # Inherit the current pane's terminal. No shell, detached window, output
        # capture or process replacement: the native process remains interactive.
        child = popen(argv, env=env)
        if type(child.pid) is not int or child.pid <= 0:
            _fail()
        deadline = clock() + timeout
        reported, resumed = False, False
        for _ in range(int(timeout / interval) + 2):
            pane = _proof(cli, candidate, child, argv)
            resumed = fresh.ready(plan) or resumed
            if pane is not None and resumed and _visible_ready(visible(candidate["pane_id"])):
                if reported and _session(pane, candidate["session_id"]):
                    # SessionStart hooks may publish identity before onboarding
                    # completes. Only this owned bridge can acknowledge readiness.
                    _atomic_json(Path(plan["switch_receipt_path"]), {
                        "state": "ready", "profile_id": plan["profile_id"],
                        "session_id": candidate["session_id"], "native_pid": child.pid,
                    })
                    fresh.close()
                    return child.wait()
                if not reported:
                    cli(["pane", "report-agent-session", candidate["pane_id"],
                         "--source", "herdr:codex", "--agent", "codex",
                         "--seq", str(int(now() * 1000)),
                         "--agent-session-id", candidate["session_id"],
                         "--session-start-source", "resume"])
                    reported = True
                    # Re-read the same live generation before accepting metadata.
                    continue
            if clock() >= deadline:
                break
            sleep(min(interval, max(0.0, deadline - clock())))
        _fail()
    except Exception:
        if child is not None:
            # Keep the owned handle until this runner exits. Never terminate a
            # potentially healthy interactive CLI after an uncertain API reply.
            _UNCONFIRMED_CHILDREN.append(child)
        _fail()
    finally:
        fresh.close()
