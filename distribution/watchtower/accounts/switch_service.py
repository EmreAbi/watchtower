"""Explicit, same-provider account transfer; no automatic rotation or new chat.

The UI prepares a read-only selection. Only its final Switch action may stop
idle CLIs, copy their conversation and resume them in the same pane. Credentials
are never copied. Receipts are private metadata, not conversation transcripts.
"""
from __future__ import annotations

from contextlib import closing, contextmanager
from copy import deepcopy
import json
import os
from pathlib import Path
import re
import secrets
import shutil
import sqlite3
import sys
import tempfile
import threading
import time

from backend import AccountError, AccountService, _atomic_json, _same_path, provider_command
from models import valid_model
from switch_process import runner_belongs_to_shell

ROOT = Path(__file__).resolve().parent
SESSION = re.compile(r"[a-fA-F0-9-]{36}\Z")
PANE = re.compile(r"w\d+:p\d+\Z")
TOKEN = re.compile(r"[a-f0-9]{48}\Z")
UNSUPPORTED = "Session-preserving switching is not supported for this provider yet."


def session_file(home, session):
    if not SESSION.fullmatch(session or ""):
        raise AccountError("session_missing", "The recorded Codex session is unavailable.")
    root = Path(home).resolve()
    files = list((root / "sessions").glob(f"**/*{session}.jsonl"))
    if len(files) != 1 or not files[0].is_file() or not files[0].resolve().is_relative_to(root):
        raise AccountError("session_missing", "Exactly one local conversation file is required.")
    return files[0]


def session_options(path, session):
    """Fold persisted settings in order; a last turn may predate a model change."""
    if path.stat().st_size > 256 * 1024 * 1024:
        raise AccountError("session_too_large", "This conversation needs a manual transfer.")
    verified, context = False, None
    with path.open("rb") as stream:
        while True:
            line = stream.readline(1024 * 1024 + 1)
            if not line:
                break
            if len(line) > 1024 * 1024:
                while line and not line.endswith(b"\n"):
                    line = stream.readline(1024 * 1024 + 1)
                continue
            if not any(marker in line for marker in
                       (b'"session_meta"', b'"turn_context"', b'"thread_settings_applied"')):
                continue
            try:
                record = json.loads(line)
            except ValueError:
                continue
            if not isinstance(record, dict) or not isinstance(record.get("payload"), dict):
                continue
            data = record["payload"]
            if record.get("type") == "session_meta" and not verified:
                if data.get("id") != session:
                    raise AccountError("session_mismatch", "The conversation identity changed.")
                verified = True
            elif verified and record.get("type") == "turn_context":
                context = {key: data.get(key) for key in ("model", "effort", "approval_policy", "sandbox_policy")}
            elif (verified and record.get("type") == "event_msg"
                  and data.get("type") == "thread_settings_applied" and data.get("thread_id") == session):
                settings = data.get("thread_settings")
                if not isinstance(settings, dict):
                    context = {"model": None}
                    continue
                # This is a full snapshot, not a sparse override. Never fall
                # back to stale turn permissions when a newer profile is unknown.
                profile = settings.get("permission_profile")
                if profile == {"type": "disabled"}:
                    policy = {"type": "danger-full-access"}
                elif profile is None and isinstance(settings.get("sandbox_policy"), dict):
                    policy = settings["sandbox_policy"]  # Legacy snapshot.
                else:
                    context = {"model": None}
                    continue
                context = {"model": settings.get("model"), "effort": settings.get("reasoning_effort"),
                           "approval_policy": settings.get("approval_policy"), "sandbox_policy": policy,
                           "runtime_workspace_roots": settings.get("runtime_workspace_roots", []),
                           "model_provider_id": settings.get("model_provider_id"),
                           "approvals_reviewer": settings.get("approvals_reviewer")}
    if not verified or not context or not context["model"] or not valid_model("codex", context["model"]):
        raise AccountError("settings_unavailable", "The current model and session settings could not be verified.")
    approval = context["approval_policy"]
    policy = context["sandbox_policy"]
    sandbox = policy.get("type") if isinstance(policy, dict) else None
    if approval not in ("never", "on-request", "on-failure", "untrusted") or sandbox not in (
            "read-only", "workspace-write", "danger-full-access"):
        raise AccountError("settings_unavailable", "This session uses settings that cannot be transferred automatically.")
    options = ["-m", context["model"], "-a", approval, "-s", sandbox]
    provider = context.get("model_provider_id")
    if provider is not None:
        if provider != "openai":
            raise AccountError("settings_unavailable", "A custom model provider needs a manual transfer.")
        options += ["-c", 'model_provider="openai"']
    reviewer = context.get("approvals_reviewer")
    if reviewer is not None:
        if reviewer != "user":
            raise AccountError("settings_unavailable", "This approval reviewer needs a manual transfer.")
        options += ["-c", 'approvals_reviewer="user"']
    workspace_roots = context.get("runtime_workspace_roots", [])
    if (not isinstance(workspace_roots, list) or len(workspace_roots) > 128
            or not all(isinstance(root, str) and Path(root).is_absolute()
                       and not any(ord(char) < 32 or ord(char) == 127 for char in root)
                       for root in workspace_roots)):
        raise AccountError("settings_unavailable", "The session workspace roots could not be verified.")
    for root in dict.fromkeys(workspace_roots):
        options += ["--add-dir", root]
    effort = context["effort"]
    if effort is not None:
        if effort not in ("none", "minimal", "low", "medium", "high", "xhigh", "max", "ultra"):
            raise AccountError("settings_unavailable")
        options += ["-c", "model_reasoning_effort=" + json.dumps(effort)]
    if sandbox == "workspace-write":
        # Explicitly retain each sandbox property, rather than inheriting a
        # broader target profile's filesystem/network policy.
        for key in ("network_access", "exclude_tmpdir_env_var", "exclude_slash_tmp"):
            value = policy.get(key, False)
            if not isinstance(value, bool):
                raise AccountError("settings_unavailable")
            options += ["-c", f"sandbox_workspace_write.{key}=" + str(value).lower()]
        roots = policy.get("writable_roots", [])
        if not isinstance(roots, list) or not all(isinstance(p, str) and Path(p).is_absolute() for p in roots):
            raise AccountError("settings_unavailable")
        options += ["-c", "sandbox_workspace_write.writable_roots=" + json.dumps(roots)]
    return context["model"], options


def copy_destination(source_home, target_home, session):
    """Read-only conflict check, repeated after the source exits."""
    source = session_file(source_home, session)
    root = Path(target_home).resolve()
    destination = root / source.relative_to(Path(source_home).resolve())
    if not destination.resolve().is_relative_to(root):
        raise AccountError("unsafe_session_path")
    known = list((root / "sessions").glob(f"**/*{session}.jsonl"))
    if any(path.resolve() != destination.resolve() for path in known):
        raise AccountError("session_conflict", "The target has another file for this conversation.")
    if destination.exists():
        # Round trips are safe only if the older target is an exact prefix.
        with source.open("rb") as current, destination.open("rb") as old:
            while chunk := old.read(1024 * 1024):
                if current.read(len(chunk)) != chunk:
                    raise AccountError("session_conflict", "The target contains different conversation history; neither copy was overwritten.")
    return source, destination


def copy_session(source_home, target_home, session):
    """Copy a flushed rollout atomically; refuse divergent target history."""
    source, destination = copy_destination(source_home, target_home, session)
    destination.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=".account-switch-", dir=destination.parent)
    try:
        with os.fdopen(descriptor, "wb") as output, source.open("rb") as input_stream:
            shutil.copyfileobj(input_stream, output)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, destination)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
    # Resume is by exact UUID. The optional name index is not needed and is not
    # rewritten while unrelated target sessions may be appending to it.
    return destination


@contextmanager
def transfer_lock(home):
    """Cross-window OS lock; released even if the Accounts process exits."""
    directory = Path(home) / "state/accounts"
    directory.mkdir(parents=True, exist_ok=True)
    with (directory / "switch.lock").open("a+b") as stream:
        if stream.tell() == 0:
            stream.write(b"0")
            stream.flush()
        stream.seek(0)
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            raise AccountError("switch_busy", "Another account switch is in progress.") from None
        try:
            yield
        finally:
            stream.seek(0)
            if os.name == "nt":
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)


class SwitchService:
    def __init__(self, service, runtime=None, *, clock=time.monotonic, sleep=time.sleep):
        if runtime is None:
            from switch_runtime import SwitchRuntime
            runtime = SwitchRuntime()
        self.service, self.runtime = service, runtime
        self.clock, self.sleep = clock, sleep
        self.prepared = {}
        self.operation = threading.Lock()

    def _profiles(self, source_id, target_id=None):
        source = self.service.get(source_id)
        self.service._plan(source, "switch", [])  # reject custom/changed registration
        if target_id is None:
            return source, None
        target = self.service.get(target_id)
        if source["provider"] != target["provider"]:
            raise AccountError("provider_mismatch", "Choose another account from the same provider.")
        if _same_path(source["home"], target["home"]):
            raise AccountError("same_account", "Choose a different account directory.")
        self.service._plan(target, "switch", [])
        return source, target

    def switch_candidates(self, source_id):
        source, _ = self._profiles(source_id)
        profiles = self.service.list()["profiles"]
        targets = [{key: p[key] for key in ("id", "label", "provider")} for p in profiles
                   if p["provider"] == source["provider"] and not _same_path(p["home"], source["home"])
                   and not p.get("configuration_error") and not p.get("custom_environment")]
        _, bindings = self.service._ledger()
        snapshot = self.runtime.snapshot()
        panes = {p["pane_id"]: p for p in snapshot.get("panes", [])}
        agents = []
        for binding in source["bindings"]:
            pane_id = binding["pane"]
            pane = panes.get(pane_id, {})
            matches = [row for row in bindings if row["session_ref"] == "herdr:" + pane_id]
            item = dict(pane_id=pane_id, handle=binding["handle"], workspace=binding["workspace"],
                        provider=source["provider"], session_id=binding["session_id"],
                        terminal_id=pane.get("terminal_id"), label=pane.get("label"),
                        pane_cwd=pane.get("cwd"),
                        cwd=pane.get("foreground_cwd") or pane.get("cwd"),
                        status=pane.get("agent_status", "unknown"), eligible=False, reason="")
            try:
                if source["provider"] != "codex":
                    raise AccountError("unsupported_provider", UNSUPPORTED)
                if len(matches) != 1 or not pane or not PANE.fullmatch(pane_id):
                    raise AccountError("binding_unverified", "The live pane and Radio binding could not be matched.")
                row = matches[0]
                scope = row["workspace"]
                label = row["name"] + ("@" + scope[5:] if scope.startswith("freq.") else "")
                if pane.get("label") != label or pane.get("workspace_id") != binding["workspace"]:
                    raise AccountError("binding_unverified", "The pane identity changed. Refresh before switching.")
                item.update(scope=scope, account=row["account"])
                self._unique_session(item, snapshot)
                self.runtime.preflight(item)
                path = session_file(source["home"], item["session_id"])
                item["model"], item["resume_options"] = session_options(path, item["session_id"])
                item["eligible"] = True
            except AccountError as exc:
                item["reason"] = exc.message
            except (OSError, ValueError, RuntimeError, KeyError, TypeError):
                item["reason"] = "Agent details are unavailable. Refresh before switching."
            agents.append(item)
        return dict(source={key: source[key] for key in ("id", "label", "provider")}, targets=targets,
                    agents=agents, notice="Select idle Codex agents. Their panes, Radio roles, model and conversation are retained. Busy agents are skipped; no automatic rotation.")

    def prepare_switch(self, source_id, target_id, pane_ids):
        source, target = self._profiles(source_id, target_id)
        if source["provider"] != "codex":
            raise AccountError("unsupported_provider", UNSUPPORTED)
        if not isinstance(pane_ids, list) or not pane_ids or len(pane_ids) != len(set(pane_ids)):
            raise AccountError("selection_required", "Select one or more eligible agents.")
        available = self.switch_candidates(source_id)
        selected = [row for row in available["agents"] if row["pane_id"] in pane_ids]
        if len(selected) != len(pane_ids) or any(not row["eligible"] for row in selected):
            raise AccountError("selection_changed", "An agent is busy or its identity changed. Refresh the selection.")
        token = secrets.token_hex(24)
        self.prepared.clear()
        self.prepared[token] = dict(source=source, target=target, agents=deepcopy(selected), created=self.clock())
        return dict(token=token, source=available["source"], target={key: target[key] for key in ("id", "label", "provider")},
                    agents=selected, notice="The selected idle agents will briefly exit and resume in their current panes. Account defaults and other agents stay unchanged.")

    def _binding(self, candidate):
        _, rows = self.service._ledger()
        matches = [row for row in rows if row["workspace"] == candidate["scope"] and row["name"] == candidate["handle"]]
        if len(matches) != 1:
            raise AccountError("binding_changed", "The Radio binding changed; no automatic retry.")
        row = matches[0]
        if (row["session_ref"] != "herdr:" + candidate["pane_id"] or row["agent"] != candidate["provider"]
                or row["agent_session"] != candidate["session_id"] or row["account"] != candidate["account"]):
            raise AccountError("binding_changed", "The Radio session or account changed; no automatic retry.")
        return row

    def _unique_session(self, candidate, snapshot=None):
        snapshot = snapshot if snapshot is not None else self.runtime.snapshot()
        if any(p.get("pane_id") != candidate["pane_id"] and p.get("agent") and
               (p.get("agent_session") or {}).get("value") == candidate["session_id"]
               for p in [*snapshot.get("panes", []), *snapshot.get("agents", [])]):
            raise AccountError("session_in_use", "This conversation is also open in another pane. Close that copy before switching.")

    def _assign(self, candidate, account):
        with closing(sqlite3.connect(self.service.radio_home / "radio.db", timeout=3)) as connection:
            cursor = connection.execute(
                "UPDATE handles SET account=? WHERE workspace=? AND name=? AND session_ref=? "
                "AND agent=? AND agent_session=? AND account IS ?",
                (account, candidate["scope"], candidate["handle"], "herdr:" + candidate["pane_id"],
                 candidate["provider"], candidate["session_id"], candidate["account"]))
            if cursor.rowcount != 1:
                connection.rollback()
                raise AccountError("binding_changed", "The Radio binding changed; the account was not reassigned.")
            connection.commit()

    def _ticket(self, candidate, target):
        from integration import _host_env
        _, env = _host_env()
        directory = self.service.home / "state/accounts/switches"
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / (secrets.token_hex(24) + ".json")
        ticket = dict(version=1, home=str(self.service.home), radio_home=str(self.service.radio_home),
                      socket=env["HERDR_SOCKET_PATH"], created=time.time(), candidate=candidate,
                      target_id=target["id"], target_home=target["home"], target_account=target["radio_account"])
        _atomic_json(path, ticket)
        return path

    def _wait_resume(self, candidate, path, target_id):
        deadline = self.clock() + 30
        while self.clock() < deadline:
            receipt = path.with_suffix(".receipt.json")
            if receipt.is_file():
                data = json.loads(receipt.read_text(encoding="utf-8"))
                if data.get("state") == "failed":
                    raise AccountError("resume_failed", "The target CLI could not resume. The conversation copy is preserved; inspect this pane.")
                if (data.get("state") == "ready" and data.get("profile_id") == target_id
                        and data.get("session_id") == candidate["session_id"]
                        and type(data.get("native_pid")) is int and data["native_pid"] > 0):
                    try:
                        inspected = self.runtime.verify_resumed(candidate)
                        process = inspected["process"]
                        rows = process.get("foreground_processes", [])
                        if (len(rows) == 1 and rows[0].get("pid") == data["native_pid"]
                                and process.get("foreground_process_group_id") == data["native_pid"]):
                            return
                    except AccountError:
                        pass  # Provider hooks/detection can settle after launch.
            self.sleep(.4)
        raise AccountError("resume_unconfirmed", "Resume was requested but is not yet confirmed. Inspect this pane before retrying.")

    def switch_accounts(self, token, progress_callback=None):
        if not self.operation.acquire(blocking=False):
            raise AccountError("switch_busy")
        results = []
        try:
            prepared = self.prepared.pop(token, None)
            if not prepared or not 0 <= self.clock() - prepared["created"] <= 300:
                raise AccountError("selection_expired", "The selection expired or was already used. Review it again.")
            source, target = self._profiles(prepared["source"]["id"], prepared["target"]["id"])
            if not _same_path(source["home"], prepared["source"]["home"]) or not _same_path(target["home"], prepared["target"]["home"]):
                raise AccountError("profile_changed")
            # Verify target login before interrupting even an idle source CLI.
            checked = self.service.refresh(target["id"])
            if (checked.get("status") or {}).get("state") != "connected":
                raise AccountError("target_not_connected", "Connect or refresh the target account before switching.")
            provider_command("codex")
            target = self.service.ensure_registered(target["id"])
            failed = False
            with transfer_lock(self.service.home):
                for candidate in prepared["agents"]:
                    row = dict(pane_id=candidate["pane_id"], handle=candidate["handle"], state="skipped", message="Not attempted after an earlier failure.")
                    if not failed:
                        stopped = False
                        assigned = False
                        try:
                            if progress_callback:
                                progress_callback({**row, "state": "switching", "message": "Checking and switching this idle agent…"})
                            self._binding(candidate)
                            self._unique_session(candidate)
                            self.runtime.preflight(candidate)
                            copy_destination(source["home"], target["home"], candidate["session_id"])
                            # Once exit is requested, even a response timeout may
                            # mean the CLI stopped. Never describe it as untouched.
                            stopped = True
                            self.runtime.stop(candidate)
                            self._binding(candidate)
                            self._unique_session(candidate)
                            file = session_file(source["home"], candidate["session_id"])
                            candidate["model"], candidate["resume_options"] = session_options(file, candidate["session_id"])
                            copy_session(source["home"], target["home"], candidate["session_id"])
                            self._assign(candidate, target["radio_account"])
                            assigned = True
                            path = self._ticket(candidate, target)
                            self.runtime.launch(candidate, path)
                            self._wait_resume(candidate, path, target["id"])
                            row.update(state="switched", message="Target profile started; the same conversation resumed.")
                        except (AccountError, OSError, ValueError, RuntimeError, sqlite3.Error) as exc:
                            message = exc.message if isinstance(exc, AccountError) else "The switch could not be verified. Inspect this pane before retrying."
                            if assigned:
                                message += " The target account is assigned and the conversation copy is preserved."
                            elif stopped:
                                message += " The source account is still assigned; the CLI may have exited."
                            row.update(state="needs_attention" if stopped else "skipped", message=message)
                            failed = True
                    results.append(row)
                    if progress_callback:
                        progress_callback(deepcopy(row))
            count = sum(row["state"] == "switched" for row in results)
            return dict(state="completed" if count == len(results) else "partial", results=results,
                        message=f"{count}/{len(results)} agents switched. No working agent was intentionally interrupted.")
        finally:
            self.operation.release()


def run_ticket(path):
    """Runs only inside the chosen pane, after its original CLI has exited."""
    from integration import run_radio_plan
    path = Path(path)
    if not path.is_absolute() or not TOKEN.fullmatch(path.stem) or path.suffix != ".json":
        raise AccountError("invalid_ticket")
    if path.stat().st_size > 128 * 1024:
        raise AccountError("invalid_ticket")
    ticket = json.loads(path.read_text(encoding="utf-8"))
    candidate = ticket["candidate"]
    expected = Path(ticket["home"]).resolve() / "state/accounts/switches" / path.name
    if (path.resolve() != expected or ticket.get("version") != 1
            or not 0 <= time.time() - ticket["created"] <= 300
            or os.environ.get("HERDR_PANE_ID") != candidate["pane_id"]
            or os.environ.get("HERDR_WORKSPACE_ID") != candidate["workspace"]
            or not _same_path(os.environ.get("HERDR_SOCKET_PATH", ""), ticket["socket"])):
        raise AccountError("ticket_mismatch")
    # O_EXCL stops duplicate delivery or terminal restoration from replaying it.
    with path.with_suffix(".claimed").open("x", encoding="utf-8") as stream:
        stream.write("claimed\n")
    receipt = path.with_suffix(".receipt.json")
    try:
        service = AccountService(ticket["home"], ticket["radio_home"])
        target = service.get(ticket["target_id"])
        if not _same_path(target["home"], ticket["target_home"]) or target["radio_account"] != ticket["target_account"] or target["provider"] != "codex":
            raise AccountError("profile_changed")
        current = dict(candidate, account=target["radio_account"])
        SwitchService(service)._binding(current)
        # Re-read the copied turn settings; a ticket cannot inject provider args.
        copied_session = session_file(target["home"], candidate["session_id"])
        model, options = session_options(copied_session, candidate["session_id"])
        if options != candidate["resume_options"] or model != candidate["model"]:
            raise AccountError("settings_changed")
        from integration import _cli
        pane = _cli(["pane", "get", candidate["pane_id"]]).get("pane", {})
        if (pane.get("terminal_id") != candidate["terminal_id"] or pane.get("label") != candidate["label"]
                or pane.get("agent") or pane.get("workspace_id") != candidate["workspace"]
                or not _same_path(pane.get("cwd", ""), candidate.get("pane_cwd") or candidate["cwd"])):
            raise AccountError("pane_changed")
        processes = _cli(["pane", "process-info", "--pane", candidate["pane_id"]]).get("process_info", {})
        if (processes.get("pane_id") != candidate["pane_id"]
                or not runner_belongs_to_shell(processes.get("shell_pid"))):
            raise AccountError("runner_unverified")
        SwitchService(service)._unique_session(candidate)
        args = [*service.radio_command, "join", candidate["handle"], "--provider", "codex", "--resume", "--account", target["radio_account"], "--pane", candidate["pane_id"]]
        if candidate["scope"].startswith("freq."):
            args += ["--frequency", candidate["scope"][5:]]
        else:
            args += ["--workspace-frequency"]
        plan = service._plan(target, "switch", args)
        plan["model"] = model
        plan["resume_options"] = options
        plan["switch_candidate"] = deepcopy(candidate)
        plan["switch_session_path"] = str(copied_session)
        plan["switch_receipt_path"] = str(receipt)
        plan["env"].update(WATCHTOWER_HOME=str(service.home), RADIO_HOME=str(service.radio_home))
        os.chdir(candidate["cwd"])
        _atomic_json(receipt, {"state": "launching", "profile_id": target["id"], "session_id": candidate["session_id"]})
        result = run_radio_plan(plan)
        _atomic_json(receipt, {"state": "exited" if result == 0 else "failed", "exit_code": result})
        return result
    except BaseException:
        _atomic_json(receipt, {"state": "failed"})
        raise


if __name__ == "__main__":
    try:
        if len(sys.argv) != 3 or sys.argv[1] != "--run-ticket":
            raise AccountError("invalid_ticket")
        raise SystemExit(run_ticket(sys.argv[2]))
    except (AccountError, OSError, ValueError, KeyError, TypeError, RuntimeError):
        print("Accounts: account resume could not be completed. Open Accounts and inspect the switch result.", file=sys.stderr)
        raise SystemExit(1)
