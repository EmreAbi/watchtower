"""Verified same-pane Codex exit/resume transport; never kills or replaces a pane.

Only the orchestrator owns account/session changes. This adapter inspects native
metadata, submits the native atomic idle-exit request once, and runs a one-use ticket only
after positively identifying the original pane's idle PowerShell process.
"""
from __future__ import annotations

import ntpath
import os
from pathlib import Path, PureWindowsPath
import re
import sys
import time

try:
    from .backend import AccountError
    from .integration import _cli
    from .switch_process import shell_has_no_children
except ImportError:
    from backend import AccountError
    from integration import _cli
    from switch_process import shell_has_no_children

_PANE = re.compile(r"w[1-9][0-9]*:p[1-9][0-9]*\Z")
_SHELLS = {"powershell", "pwsh"}
_MESSAGES = {
    "switch_runtime_unavailable": "Watchtower could not verify this pane. Nothing was retried.",
    "switch_identity_changed": "The pane or conversation changed. Refresh before switching accounts.",
    "switch_provider_unsupported": "Same-session account switching currently supports Codex only.",
    "switch_agent_busy": "This agent is busy or waiting for input. Finish its current task before switching.",
    "switch_agent_unverified": "The current Codex process and conversation could not be verified.",
    "switch_session_shared": "This conversation is open in another pane. Close that duplicate before switching accounts.",
    "switch_stop_unconfirmed": "The guarded exit could not be confirmed. Inspect this pane before retrying; update Watchtower if this server lacks guarded switching.",
    "switch_guard_unavailable": "Watchtower could not verify guarded switching. Restart Watchtower with the updated server, then refresh Accounts.",
    "switch_shell_unverified": "The original pane is not a verified idle PowerShell terminal.",
    "switch_stop_timeout": "Codex did not return to an idle PowerShell terminal. It was not force-stopped.",
    "switch_launch_invalid": "The prepared switch command is unavailable. Refresh before trying again.",
    "switch_launch_unconfirmed": "The resume command may have been submitted. Inspect the pane before retrying.",
}
_GUARD_ERRORS = {
    "agent_quit_busy": "switch_agent_busy",
    "agent_quit_identity_changed": "switch_identity_changed",
    "agent_quit_session_in_use": "switch_session_shared",
    "agent_quit_unverified": "switch_agent_unverified",
    "agent_not_found": "switch_identity_changed",
    "pane_not_found": "switch_identity_changed",
    # The existing native server uses invalid_request for an unknown Method.
    "invalid_request": "switch_guard_unavailable",
    "unknown_method": "switch_guard_unavailable",
    "unsupported_method": "switch_guard_unavailable",
}


def _fail(code):
    raise AccountError(code, _MESSAGES[code])


def _path_key(value):
    if not isinstance(value, str) or not value or len(value) > 32760 or any(ord(c) < 32 or ord(c) == 127 for c in value):
        return None
    if PureWindowsPath(value).is_absolute():
        return ntpath.normcase(ntpath.normpath(value))
    if Path(value).is_absolute():
        return os.path.normcase(os.path.normpath(value))
    return None


def _same_path(left, right):
    first, second = _path_key(left), _path_key(right)
    return first is not None and second is not None and first == second


def _process_name(process):
    value = process.get("name")
    if not isinstance(value, str):
        return ""
    return ntpath.basename(value).lower().removesuffix(".exe")


def _session(row, expected):
    session = row.get("agent_session")
    return (isinstance(session, dict) and session.get("source") == "herdr:codex"
            and session.get("agent") == "codex" and session.get("kind") == "id"
            and session.get("value") == expected)


def _literal(value):
    # PowerShell also treats smart quotes as delimiters; fail closed rather than
    # normalize a real path to a different name. ASCII apostrophes are escaped.
    if not isinstance(value, str) or any(ord(c) < 32 or ord(c) == 127 or c in "\u2018\u2019\u201a\u201b" for c in value):
        _fail("switch_launch_invalid")
    return "'" + value.replace("'", "''") + "'"


class SwitchRuntime:
    def __init__(self, cli=None, *, clock=None, sleep=None, stop_timeout=20.0,
                 poll_interval=0.25, python=None, runner_path=None, shell_idle=None):
        self.cli = cli or _cli
        self.clock = clock or time.monotonic
        self.sleep = sleep or time.sleep
        self.stop_timeout = min(30.0, max(0.1, float(stop_timeout)))
        self.poll_interval = min(1.0, max(0.05, float(poll_interval)))
        self.python = str(python or sys.executable)
        self.runner_path = Path(runner_path) if runner_path else Path(__file__).with_name("switch_service.py")
        self.shell_idle = shell_has_no_children if shell_idle is None else shell_idle
        self._shells = {}

    def _call(self, args, *, missing_agent=False):
        try:
            result = self.cli(args)
        except Exception as error:
            # integration._cli deliberately exposes only a bounded API error code.
            # Absence is allowed solely for agent.get; all other failures remain errors.
            if missing_agent and str(error) == "Watchtower: agent_not_found. No automatic retry.":
                return None
            if args[:2] == ["agent", "quit-if-idle"]:
                # Preserve only recognized native codes, never raw diagnostics.
                matched = re.fullmatch(r"Watchtower: ([a-z_]{1,80})\. No automatic retry\.", str(error))
                if matched and matched[1] in _GUARD_ERRORS:
                    _fail(_GUARD_ERRORS[matched[1]])
            _fail("switch_runtime_unavailable")
        if not isinstance(result, dict) or "error" in result:
            _fail("switch_runtime_unavailable")
        return result

    def snapshot(self):
        panes = self._call(["pane", "list"]).get("panes")
        agents = self._call(["agent", "list"]).get("agents")
        if any(not isinstance(rows, list) or len(rows) > 10000
               or any(not isinstance(row, dict) for row in rows) for rows in (panes, agents)):
            _fail("switch_runtime_unavailable")
        return {"panes": panes, "agents": agents}

    def inspect(self, pane_id):
        if not isinstance(pane_id, str) or not _PANE.fullmatch(pane_id):
            _fail("switch_identity_changed")
        pane = self._call(["pane", "get", pane_id]).get("pane")
        if not isinstance(pane, dict) or pane.get("pane_id") != pane_id:
            _fail("switch_identity_changed")
        response = self._call(["agent", "get", pane_id], missing_agent=True)
        agent = response.get("agent") if response is not None else None
        if agent is not None and not isinstance(agent, dict):
            _fail("switch_runtime_unavailable")
        process = self._call(["pane", "process-info", "--pane", pane_id]).get("process_info")
        if not isinstance(process, dict) or process.get("pane_id") != pane_id:
            _fail("switch_identity_changed")
        return {"pane": pane, "agent": agent, "process": process}

    @staticmethod
    def _identity(candidate, inspected):
        if not isinstance(candidate, dict) or candidate.get("provider") != "codex":
            _fail("switch_provider_unsupported")
        pane = inspected["pane"]
        if (not isinstance(candidate.get("terminal_id"), str) or not candidate["terminal_id"]
                or not isinstance(candidate.get("session_id"), str) or not candidate["session_id"]
                or pane.get("pane_id") != candidate.get("pane_id")
                or pane.get("terminal_id") != candidate["terminal_id"]
                or pane.get("workspace_id") != candidate.get("workspace")
                or pane.get("label") != candidate.get("label")
                or not _same_path(pane.get("cwd"), candidate.get("pane_cwd", candidate.get("cwd")))):
            _fail("switch_identity_changed")
        agent = inspected["agent"]
        if agent is not None and (agent.get("pane_id") != candidate["pane_id"]
                or agent.get("terminal_id") != candidate["terminal_id"]
                or agent.get("workspace_id") != candidate["workspace"]):
            _fail("switch_identity_changed")

    @staticmethod
    def _processes(inspected):
        process = inspected["process"]
        rows = process.get("foreground_processes")
        shell_pid = process.get("shell_pid")
        if (not isinstance(rows, list) or not rows or len(rows) > 64
                or any(not isinstance(row, dict) or type(row.get("pid")) is not int or row["pid"] <= 0 for row in rows)
                or type(shell_pid) is not int or shell_pid <= 0):
            return None
        return rows

    def _shell(self, candidate, inspected):
        self._identity(candidate, inspected)
        pane, agent, process = inspected["pane"], inspected["agent"], inspected["process"]
        if pane.get("agent") or (agent is not None and agent.get("agent")):
            return None
        if any(row and (row.get("agent_status") in ("working", "blocked") or row.get("launch_pending"))
               for row in (pane, agent)):
            return None
        processes = self._processes(inspected)
        if not processes or len(processes) != 1:
            return None
        shell = processes[0]
        if (shell["pid"] != process["shell_pid"]
                or process.get("foreground_process_group_id") != shell["pid"]
                or _process_name(shell) not in _SHELLS
                or not _same_path(shell.get("cwd"), candidate["cwd"])):
            return None
        # Windows' foreground API may report the root shell while an unrecognized
        # Python/Radio child is still alive. Require an actual process-tree proof
        # before either copying a finished rollout or typing the resume command.
        try:
            if self.shell_idle(shell["pid"]) is not True:
                return None
        except (OSError, ValueError, RuntimeError):
            return None
        return {"pid": shell["pid"], "name": _process_name(shell), "cwd": shell["cwd"]}

    def _unique_session(self, candidate):
        snapshot = self.snapshot()
        for row in (*snapshot["panes"], *snapshot["agents"]):
            if (row.get("pane_id") != candidate["pane_id"] and row.get("agent") == "codex"
                    and _session(row, candidate["session_id"])):
                _fail("switch_session_shared")

    def _native_codex(self, candidate, inspected):
        pane, agent = inspected["pane"], inspected["agent"]
        if (agent is None or pane.get("agent") != "codex" or agent.get("agent") != "codex"
                or not _session(pane, candidate["session_id"]) or not _session(agent, candidate["session_id"])):
            _fail("switch_agent_unverified")

        processes = self._processes(inspected)
        if (not processes or len(processes) != 1 or _process_name(processes[0]) != "codex"
                or inspected["process"].get("foreground_process_group_id") != processes[0]["pid"]
                or not _same_path(processes[0].get("cwd"), candidate["cwd"])):
            _fail("switch_agent_unverified")

    @staticmethod
    def _guard_command(candidate, inspected):
        return ["agent", "quit-if-idle", candidate["pane_id"],
                "--terminal", candidate["terminal_id"], "--agent-session-id", candidate["session_id"],
                "--state-seq", str(inspected["agent"]["state_change_seq"])]

    def verify_resumed(self, candidate):
        """Confirm identity and a real native process without requiring an idle turn."""
        inspected = self.inspect(candidate.get("pane_id") if isinstance(candidate, dict) else None)
        self._identity(candidate, inspected)
        self._native_codex(candidate, inspected)
        self._unique_session(candidate)
        return {**inspected, "state": "resumed"}

    def preflight(self, candidate):
        inspected = self.inspect(candidate.get("pane_id") if isinstance(candidate, dict) else None)
        self._identity(candidate, inspected)
        self._unique_session(candidate)
        shell = self._shell(candidate, inspected)
        if shell is not None and candidate.get("allow_shell") is True:
            return {**inspected, "state": "shell", "readiness": "shell", "shell": shell}
        pane, agent = inspected["pane"], inspected["agent"]
        if any(row and (row.get("agent_status") not in ("idle", "done") or row.get("launch_pending"))
               for row in (pane, agent)):
            _fail("switch_agent_busy")
        self._native_codex(candidate, inspected)
        if type(agent.get("state_change_seq")) is not int or agent["state_change_seq"] < 0:
            _fail("switch_agent_unverified")
        self._call([*self._guard_command(candidate, inspected), "--check"])
        # Radio-launched legacy agents are not agent.start-managed, so native
        # interactive_ready is omitted. Official session + actual native process
        # + idle evidence is the positive compatibility path, not a false-ready flag.
        readiness = "managed" if agent.get("interactive_ready") is True else "legacy-native"
        return {**inspected, "state": "ready", "readiness": readiness}

    def stop(self, candidate):
        first = self.preflight(candidate)
        if first["state"] == "shell":
            self._shells[candidate["pane_id"]] = first["shell"]
            return {**first, "state": "stopped"}
        current = self.preflight(candidate)
        if (current["state"] != "ready"
                or current["process"]["foreground_processes"][0]["pid"] != first["process"]["foreground_processes"][0]["pid"]
                or current["agent"].get("state_change_seq") != first["agent"].get("state_change_seq")):
            _fail("switch_identity_changed")
        # The server checks identity and idle state atomically before sending its
        # native exit key. An older server rejects this method; no prompt/kill
        # fallback is allowed. A lost response may still mean exit was requested.
        try:
            self._call(self._guard_command(candidate, current))
        except AccountError:
            _fail("switch_stop_unconfirmed")
        deadline = self.clock() + self.stop_timeout
        max_polls = int(self.stop_timeout / self.poll_interval) + 2
        for _ in range(max_polls):
            inspected = self.inspect(candidate["pane_id"])
            self._identity(candidate, inspected)
            shell = self._shell(candidate, inspected)
            if shell is not None:
                self._shells[candidate["pane_id"]] = shell
                return {**inspected, "state": "stopped", "readiness": "shell", "shell": shell}
            pane, agent = inspected["pane"], inspected["agent"]
            if (pane.get("agent") not in (None, "codex")
                    or (agent and agent.get("agent") not in (None, "codex"))
                    or (pane.get("agent") == "codex" and not _session(pane, candidate["session_id"]))
                    or (agent and agent.get("agent") == "codex" and not _session(agent, candidate["session_id"]))):
                _fail("switch_identity_changed")
            if any(row and row.get("agent_status") in ("working", "blocked") for row in (pane, agent)):
                _fail("switch_agent_busy")
            if self.clock() >= deadline:
                break
            self.sleep(min(self.poll_interval, max(0.0, deadline - self.clock())))
        _fail("switch_stop_timeout")

    def launch(self, candidate, ticket_path):
        ticket = Path(ticket_path)
        if (not ticket.is_absolute() or not ticket.is_file() or ticket.is_symlink()
                or not self.runner_path.is_absolute() or not self.runner_path.is_file()
                or self.runner_path.is_symlink() or not _path_key(self.python)):
            _fail("switch_launch_invalid")
        argv = [self.python, "-B", str(self.runner_path), "--run-ticket", str(ticket)]
        command = "& " + " ".join(_literal(part) for part in argv)
        if len(command) > 30000:
            _fail("switch_launch_invalid")
        inspected = self.inspect(candidate.get("pane_id"))
        shell = self._shell(candidate, inspected)
        if shell is None or (candidate["pane_id"] in self._shells and shell != self._shells[candidate["pane_id"]]):
            _fail("switch_shell_unverified")
        try:
            self._call(["pane", "run", candidate["pane_id"], command])
        except AccountError:
            _fail("switch_launch_unconfirmed")
        return {"state": "resume_requested"}
