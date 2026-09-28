"""Pure, conservative presentation of a verified pane's current agent state.

The caller owns pane/session identity verification. This module reads no files,
changes no agent authority, and never interprets assistant prose as a wait state.
Only structured current-turn evidence can distinguish waiting on other agents.
"""


_LABELS = {
    "ready": "Ready for task",
    "waiting_user": "Needs your input",
    "waiting_agents": "Waiting for agents",
    "working": "Working",
    "done": "Result ready",
    "interrupted": "Interrupted",
    "unknown": "Status unknown",
}


def _status(code, detail, can_send=False):
    return dict(code=code, label=_LABELS[code], detail=detail, can_send=bool(can_send))


def describe_status(pane, data, pending=False):
    """Combine current runtime state and its matching current-turn snapshot.

    ``waiting`` is the reader's structured, outstanding-tool summary; its kind is
    ``user`` or ``agents``. Completed/interrupted history cannot override a busy
    runtime, nor can a stale unfinished turn make an idle pane look ready. The
    caller must use the latest snapshot for status even when viewing old results.
    """
    if not isinstance(pane, dict) or not isinstance(data, dict):
        return _status("unknown", "Current agent state could not be verified. Open Terminal to check.")

    native = pane.get("agent_status", "unknown")
    # A live permission/input dialog must remain visible even if history lags.
    if native == "blocked":
        return _status("waiting_user", "Open Terminal to answer the agent's question or approval prompt.")
    if pending:
        return _status("working", "Your request was sent; waiting for the agent to start it.")

    supported = data.get("provider_supported") is True
    readable = supported and not data.get("error")
    state = data.get("state")
    waiting = data.get("waiting")
    wait_kind = waiting.get("kind") if isinstance(waiting, dict) else None

    if native == "working":
        if readable and state == "running" and wait_kind == "user":
            return _status("waiting_user", "The agent has an unanswered input request. Open Terminal to answer it.")
        if readable and state == "running" and wait_kind == "agents":
            return _status("waiting_agents", "The current task is waiting for another agent's response.")
        return _status("working", "The agent is working on its current task.")

    if native not in ("idle", "done"):
        return _status("unknown", "Current agent state could not be verified. Open Terminal to check.")
    if not supported:
        return _status("unknown", "Detailed task status is not available for this provider. Use Terminal.")
    if not readable or state in ("unavailable", "unsupported"):
        return _status("unknown", "The current session could not be read. Open Terminal to check.")
    if state == "running":
        return _status("unknown", "Terminal appears ready, but the recorded task is still open. Check Terminal before continuing.")
    if state == "interrupted":
        return _status("interrupted", "The last task was interrupted. Open Terminal to inspect or continue it.")
    if state == "complete":
        return _status("done", "The last task has finished. You can view its result or send a new request.", True)
    if state == "idle":
        return _status("ready", "The agent is ready for a new task.", True)
    return _status("unknown", "The session's task state is not recognized. Open Terminal to check.")
