"""Account switching fixtures use disposable ledgers and fake CLIs only."""
from copy import deepcopy
from contextlib import closing
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import time
import unittest
from unittest.mock import Mock, patch

from backend import AccountError, AccountService
from switch_service import SwitchService, copy_session, run_ticket, session_file, session_options

SESSION = "01234567-89ab-cdef-0123-456789abcdef"


def rollout(home, session=SESSION, **context):
    path = Path(home) / "sessions/2026/09/28" / ("rollout-" + session + ".jsonl")
    path.parent.mkdir(parents=True, exist_ok=True)
    data = {"model": "gpt-6-luna", "effort": "high", "approval_policy": "on-request", "sandbox_policy": {"type": "read-only"}}
    data.update(context)
    path.write_text(json.dumps({"type": "session_meta", "payload": {"id": session}}) + "\n" +
                    json.dumps({"type": "turn_context", "payload": data}) + "\n", encoding="utf-8")
    return path


def settings_event(path, session=SESSION, **changes):
    settings = {"model": "gpt-6-luna", "reasoning_effort": "xhigh", "approval_policy": "never",
                "permission_profile": {"type": "disabled"}, "model_provider_id": "openai",
                "approvals_reviewer": "user", "runtime_workspace_roots": []}
    settings.update(changes)
    with path.open("a", encoding="utf-8") as stream:
        stream.write(json.dumps({"type": "event_msg", "payload": {
            "type": "thread_settings_applied", "thread_id": session, "thread_settings": settings}}) + "\n")


class SwitchTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.service = AccountService(self.root / "watchtower", self.root / "radio", reader=Mock())
        self.service._default_home = lambda p: self.root / "unused" / p
        self.source = self.service.add("source", "codex")
        self.target = self.service.add("target", "codex")
        self.path = rollout(self.source["home"])
        self.sql("INSERT INTO handles(workspace,pane_workspace,name,session_ref,agent,agent_session,account,created_at,last_seen) VALUES(?,?,?,?,?,?,?,?,?)",
                 ("freq.004", "w4", "lead", "herdr:w4:p1", "codex", SESSION, "source", "now", "now"))
        self.pane = dict(pane_id="w4:p1", terminal_id="t123", workspace_id="w4", label="lead@004", cwd=str(self.root),
                         agent="codex", agent_status="idle", agent_session={"value": SESSION})
        self.runtime = Mock()
        self.runtime.snapshot.return_value = {"panes": [self.pane], "agents": []}
        self.runtime.preflight.return_value = {}
        self.runtime.stop.return_value = {"state": "stopped"}
        self.switch = SwitchService(self.service, self.runtime)
        self.switch._wait_resume = Mock()
        self.service.refresh = Mock(return_value={"status": {"state": "connected"}})
        self.ticket = self.root / "ticket.json"
        self.switch._ticket = Mock(return_value=self.ticket)

    def sql(self, statement, args=()):
        with closing(sqlite3.connect(self.service.radio_home / "radio.db")) as connection:
            connection.execute(statement, args)
            connection.commit()

    def prepare(self):
        return self.switch.prepare_switch("source", "target", ["w4:p1"])["token"]

    def apply(self, token=None, **kwargs):
        token = token or self.prepare()
        with patch("switch_service.provider_command", return_value=["codex.exe"]):
            return self.switch.switch_accounts(token, **kwargs)

    def test_prepare_is_read_only_and_filters_cross_provider(self):
        self.service.add("zen", "opencode")
        before = (self.service.radio_home / "radio.db").read_bytes()
        rows = self.switch.switch_candidates("source")
        self.assertEqual([p["id"] for p in rows["targets"]], ["target"])
        self.assertTrue(rows["agents"][0]["eligible"])
        self.prepare()
        self.assertEqual(before, (self.service.radio_home / "radio.db").read_bytes())
        self.runtime.stop.assert_not_called()
        self.assertFalse((Path(self.target["home"]) / "sessions").exists())

    def test_cross_provider_rejected_before_runtime_action(self):
        self.service.add("zen", "opencode")
        with self.assertRaisesRegex(AccountError, "same provider"):
            self.switch.prepare_switch("source", "zen", ["w4:p1"])
        self.runtime.snapshot.assert_not_called()

    def test_success_preserves_session_source_and_metadata_with_progress(self):
        before = self.path.read_bytes()
        callback = Mock()
        result = self.apply(progress_callback=callback)
        self.assertEqual(result["state"], "completed")
        self.assertEqual(result["results"][0]["state"], "switched")
        self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual(session_file(self.target["home"], SESSION).read_bytes(), before)
        self.assertEqual(self.service.get("target")["bindings"][0]["session_id"], SESSION)
        self.assertEqual(callback.call_count, 2)
        self.assertEqual(self.runtime.launch.call_args.args[0]["model"], "gpt-6-luna")

    def test_replayed_selection_cannot_stop_again(self):
        token = self.prepare()
        self.apply(token)
        with self.assertRaises(AccountError):
            self.apply(token)
        self.assertEqual(self.runtime.stop.call_count, 1)

    def test_busy_change_between_review_and_apply_is_skipped(self):
        token = self.prepare()
        self.runtime.preflight.side_effect = AccountError("busy", "Agent is working.")
        result = self.apply(token)
        self.assertEqual(result["results"][0]["state"], "skipped")
        self.runtime.stop.assert_not_called()
        self.assertEqual(len(self.service.get("source")["bindings"]), 1)

    def test_bad_target_login_cannot_stop_agent(self):
        token = self.prepare()
        self.service.refresh.return_value = {"status": {"state": "not_connected"}}
        with self.assertRaisesRegex(AccountError, "target account"):
            self.apply(token)
        self.runtime.stop.assert_not_called()

    def test_identity_change_cannot_reassign(self):
        token = self.prepare()
        self.sql("UPDATE handles SET agent_session='changed'")
        result = self.apply(token)
        self.assertEqual(result["results"][0]["state"], "skipped")
        self.runtime.stop.assert_not_called()

    def test_conflicting_history_does_not_stop_or_overwrite(self):
        target = rollout(self.target["home"], model="different-model")
        before = target.read_bytes()
        result = self.apply()
        self.assertEqual(result["results"][0]["state"], "skipped")
        self.assertEqual(target.read_bytes(), before)
        self.runtime.stop.assert_not_called()

    def test_resume_failure_is_not_success_and_does_not_retry(self):
        self.runtime.launch.side_effect = RuntimeError("private diagnostics")
        result = self.apply()
        self.assertEqual(result["results"][0]["state"], "needs_attention")
        self.assertNotIn("private diagnostics", json.dumps(result))
        self.assertEqual(self.runtime.launch.call_count, 1)
        self.assertEqual(self.path.read_bytes(), session_file(self.target["home"], SESSION).read_bytes())

    def test_lost_exit_response_is_not_reported_as_untouched(self):
        self.runtime.stop.side_effect = AccountError("timeout", "Exit delivery is unconfirmed.")
        result = self.apply()
        self.assertEqual(result["results"][0]["state"], "needs_attention")
        self.assertIn("may have exited", result["results"][0]["message"])
        self.runtime.launch.assert_not_called()

    def test_conversation_open_in_another_pane_is_disabled(self):
        other = {**self.pane, "pane_id": "w4:p99", "terminal_id": "t456"}
        self.runtime.snapshot.return_value = {"panes": [self.pane, other], "agents": []}
        rows = self.switch.switch_candidates("source")
        self.assertFalse(rows["agents"][0]["eligible"])
        self.assertIn("another pane", rows["agents"][0]["reason"])

    def test_verification_requires_expected_receipt_and_native_identity(self):
        candidate = self.switch.switch_candidates("source")["agents"][0]
        receipt = self.ticket.with_suffix(".receipt.json")
        receipt.write_text(json.dumps({"state": "ready", "profile_id": "wrong", "session_id": SESSION, "native_pid": 20}))
        self.switch.clock = Mock(side_effect=[0, 1, 40])
        self.switch.sleep = Mock()
        with self.assertRaises(AccountError):
            SwitchService._wait_resume(self.switch, candidate, self.ticket, "target")
        self.runtime.verify_resumed.assert_not_called()
        receipt.write_text(json.dumps({"state": "ready", "profile_id": "target", "session_id": SESSION, "native_pid": 20}))
        self.switch.clock = Mock(side_effect=[0, 1, 40])
        self.runtime.verify_resumed.side_effect = AccountError("foreign_process")
        with self.assertRaises(AccountError):
            SwitchService._wait_resume(self.switch, candidate, self.ticket, "target")
        self.switch.clock = Mock(side_effect=[0, 1])
        self.runtime.verify_resumed.side_effect = None
        self.runtime.verify_resumed.return_value = {"process": {
            "foreground_process_group_id": 20, "foreground_processes": [{"pid": 20}]}}
        SwitchService._wait_resume(self.switch, candidate, self.ticket, "target")

    def test_launching_receipt_with_official_native_identity_cannot_complete(self):
        candidate = self.switch.switch_candidates("source")["agents"][0]
        receipt = self.ticket.with_suffix(".receipt.json")
        receipt.write_text(json.dumps({"state": "launching", "profile_id": "target", "session_id": SESSION}))
        self.runtime.verify_resumed.return_value = {"process": {
            "foreground_process_group_id": 20, "foreground_processes": [{"pid": 20}]}}
        self.switch.clock = Mock(side_effect=[0, 1, 40])
        self.switch.sleep = Mock()
        with self.assertRaises(AccountError):
            SwitchService._wait_resume(self.switch, candidate, self.ticket, "target")
        self.runtime.verify_resumed.assert_not_called()

    def test_ready_receipt_requires_matching_live_native_pid(self):
        candidate = self.switch.switch_candidates("source")["agents"][0]
        receipt = self.ticket.with_suffix(".receipt.json")
        self.switch.sleep = Mock()
        self.runtime.verify_resumed.return_value = {"process": {
            "foreground_process_group_id": 20, "foreground_processes": [{"pid": 20}]}}
        for pid in (None, True, 0, 21):
            receipt.write_text(json.dumps({"state": "ready", "profile_id": "target", "session_id": SESSION,
                                           "native_pid": pid}))
            self.switch.clock = Mock(side_effect=[0, 1, 40])
            with self.assertRaises(AccountError):
                SwitchService._wait_resume(self.switch, candidate, self.ticket, "target")

    def test_native_resume_retains_model_effort_and_permissions(self):
        from integration import _native_provider_argv
        model, options = session_options(self.path, SESSION)
        with patch("backend.provider_command", return_value=["codex.exe"]):
            argv = _native_provider_argv({"kind": "switch", "model": model, "resume_options": options},
                                         ["codex", "resume", SESSION])
        self.assertIn("read-only", argv)
        self.assertIn("on-request", argv)
        self.assertIn('model_reasoning_effort="high"', argv)
        self.assertEqual(argv[-2:], ["resume", SESSION])

    def test_newest_thread_settings_preserve_current_model_permissions_and_roots(self):
        self.path = rollout(self.source["home"], model="gpt-6-astra", effort="ultra")
        roots = [str(self.root), str(self.root / "shared"), str(self.root / "shared")]
        settings_event(self.path, runtime_workspace_roots=roots)
        model, options = session_options(self.path, SESSION)
        self.assertEqual(model, "gpt-6-luna")
        self.assertIn('model_reasoning_effort="xhigh"', options)
        self.assertEqual(options[options.index("-a") + 1], "never")
        self.assertEqual(options[options.index("-s") + 1], "danger-full-access")
        self.assertIn('model_provider="openai"', options)
        self.assertIn('approvals_reviewer="user"', options)
        self.assertEqual([options[index + 1] for index, item in enumerate(options) if item == "--add-dir"], roots[:2])
        self.assertNotIn("ultra", " ".join(options))

    def test_settings_for_other_thread_do_not_replace_bound_settings(self):
        settings_event(self.path, session="fedcba98-7654-3210-fedc-ba9876543210")
        _, options = session_options(self.path, SESSION)
        self.assertIn('model_reasoning_effort="high"', options)
        self.assertEqual(options[options.index("-a") + 1], "on-request")

    def test_only_latest_unknown_profile_blocks_switch_and_later_known_profile_recovers(self):
        settings_event(self.path, permission_profile={"type": "managed", "file_system": {}})
        with self.assertRaises(AccountError):
            session_options(self.path, SESSION)
        settings_event(self.path)
        _, options = session_options(self.path, SESSION)
        self.assertEqual(options[options.index("-s") + 1], "danger-full-access")
        settings_event(self.path, permission_profile={"type": "custom"})
        with self.assertRaises(AccountError):
            session_options(self.path, SESSION)

    def test_later_legacy_turn_context_supersedes_settings_snapshot(self):
        settings_event(self.path)
        context = {"model": "gpt-6-astra", "effort": "high", "approval_policy": "on-request",
                   "sandbox_policy": {"type": "read-only"}}
        with self.path.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps({"type": "turn_context", "payload": context}) + "\n")
        model, options = session_options(self.path, SESSION)
        self.assertEqual(model, "gpt-6-astra")
        self.assertEqual(options[options.index("-s") + 1], "read-only")

    def test_legacy_settings_snapshot_preserves_workspace_policy(self):
        settings_event(self.path, permission_profile=None,
                       sandbox_policy={"type": "workspace-write", "network_access": False,
                                       "exclude_tmpdir_env_var": True, "exclude_slash_tmp": True,
                                       "writable_roots": [str(self.root / "allowed")]})
        _, options = session_options(self.path, SESSION)
        self.assertEqual(options[options.index("-s") + 1], "workspace-write")
        self.assertIn("sandbox_workspace_write.network_access=false", options)
        self.assertIn("sandbox_workspace_write.exclude_tmpdir_env_var=true", options)
        self.assertIn("sandbox_workspace_write.exclude_slash_tmp=true", options)
        self.assertIn("sandbox_workspace_write.writable_roots=" + json.dumps([str(self.root / "allowed")]), options)

    def test_invalid_current_workspace_roots_or_custom_provider_fail_closed(self):
        for changes in ({"runtime_workspace_roots": ["relative"]},
                        {"runtime_workspace_roots": [str(self.root) + "\ncommand"]},
                        {"model_provider_id": "custom"}, {"approvals_reviewer": "unknown"}):
            with self.subTest(changes=changes):
                self.path = rollout(self.source["home"])
                settings_event(self.path, **changes)
                with self.assertRaises(AccountError):
                    session_options(self.path, SESSION)

    def test_duplicate_pane_and_unsupported_provider_not_eligible(self):
        self.sql("INSERT INTO handles(workspace,pane_workspace,name,session_ref,agent,agent_session,account,created_at,last_seen) VALUES(?,?,?,?,?,?,?,?,?)",
                 ("freq.005", "w4", "other", "herdr:w4:p1", "codex", SESSION, "source", "now", "now"))
        self.assertTrue(all(not row["eligible"] for row in self.switch.switch_candidates("source")["agents"]))

    def test_settings_and_round_trip_copy(self):
        _, options = session_options(self.path, SESSION)
        self.assertIn("read-only", options)
        self.assertIn("on-request", options)
        self.assertIn('model_reasoning_effort="high"', options)
        first = copy_session(self.source["home"], self.target["home"], SESSION)
        with first.open("a", encoding="utf-8") as stream:
            stream.write('{"type":"event_msg","payload":{"type":"task_complete"}}\n')
        restored = copy_session(self.target["home"], self.source["home"], SESSION)
        self.assertEqual(first.read_bytes(), restored.read_bytes())

    def test_unknown_permissions_fail_closed(self):
        rollout(self.source["home"], sandbox_policy={"type": "external-sandbox"})
        with self.assertRaises(AccountError):
            session_options(self.path, SESSION)

    def test_same_home_or_no_selection_rejected(self):
        with self.assertRaises(AccountError):
            self.switch.prepare_switch("source", "source", ["w4:p1"])
        with self.assertRaises(AccountError):
            self.switch.prepare_switch("source", "target", [])

    def test_unmatched_ticket_never_claimed_or_launched(self):
        directory = self.service.home / "state/accounts/switches"
        directory.mkdir(parents=True)
        path = directory / ("a" * 48 + ".json")
        data = {"version": 1, "home": str(self.service.home), "created": time.time(), "socket": str(self.root / "host.sock"),
                "candidate": {"pane_id": "w4:p1", "workspace": "w4"}}
        path.write_text(json.dumps(data))
        with patch.dict(os.environ, {"HERDR_PANE_ID": "w9:p99", "HERDR_WORKSPACE_ID": "w9"}), \
                patch("integration.run_radio_plan") as runner, self.assertRaises(AccountError):
            run_ticket(path)
        runner.assert_not_called()
        self.assertFalse(path.with_suffix(".claimed").exists())

    def runner_ticket(self):
        candidate = self.switch.switch_candidates("source")["agents"][0]
        target = self.service.ensure_registered("target")
        copy_session(self.source["home"], target["home"], SESSION)
        self.switch._assign(candidate, target["radio_account"])
        socket = str(self.root / "host.sock")
        with patch("integration._host_env", return_value=(None, {"HERDR_SOCKET_PATH": socket})):
            path = SwitchService._ticket(self.switch, candidate, target)
        return path, socket

    def test_ticket_resumes_exact_target_once_in_verified_pane(self):
        path, socket = self.runner_ticket()
        shell = {**self.pane, "agent": None}
        def cli(argv):
            if argv[:2] == ["pane", "get"]:
                return {"pane": shell}
            # Windows foreground metadata reports the pane shell while an
            # unrecognized Python runner is active; it is not a process tree.
            return {"process_info": {"pane_id": "w4:p1", "shell_pid": 123,
                                     "foreground_processes": [{"pid": 123, "name": "powershell.exe"}]}}
        with patch.dict(os.environ, {"HERDR_PANE_ID": "w4:p1", "HERDR_WORKSPACE_ID": "w4", "HERDR_SOCKET_PATH": socket}), \
                patch("integration._cli", side_effect=cli), \
                patch("switch_service.runner_belongs_to_shell", return_value=True) as ownership, \
                patch("switch_runtime.SwitchRuntime.snapshot", return_value={"panes": [shell], "agents": []}), \
                patch("switch_service.os.chdir") as chdir, \
                patch("integration.run_radio_plan", return_value=0) as runner:
            self.assertEqual(run_ticket(path), 0)
            ownership.assert_called_once_with(123)
            plan = runner.call_args.args[0]
            self.assertEqual(plan["profile_id"], "target")
            self.assertEqual(plan["env"]["CODEX_HOME"], self.target["home"])
            self.assertEqual(plan["model"], "gpt-6-luna")
            self.assertEqual(plan["switch_receipt_path"], str(path.with_suffix(".receipt.json")))
            self.assertIn("--resume", plan["argv"])
            self.assertEqual(plan["argv"][-2:], ["--frequency", "004"])
            chdir.assert_called_once_with(str(self.root))
            with self.assertRaises(FileExistsError):
                run_ticket(path)
            runner.assert_called_once()
        self.assertEqual(json.loads(path.with_suffix(".receipt.json").read_text())["state"], "exited")

    def test_ticket_outside_actual_pane_shell_tree_cannot_launch(self):
        path, socket = self.runner_ticket()
        with patch.dict(os.environ, {"HERDR_PANE_ID": "w4:p1", "HERDR_WORKSPACE_ID": "w4", "HERDR_SOCKET_PATH": socket}), \
                patch("integration._cli", side_effect=[{"pane": {**self.pane, "agent": None}},
                      {"process_info": {"pane_id": "w4:p1", "shell_pid": 123,
                                        "foreground_processes": [{"pid": 123, "name": "powershell.exe"}]}}]), \
                patch("switch_service.runner_belongs_to_shell", return_value=False) as ownership, \
                patch("integration.run_radio_plan") as runner, self.assertRaises(AccountError):
            run_ticket(path)
        ownership.assert_called_once_with(123)
        runner.assert_not_called()
        self.assertEqual(json.loads(path.with_suffix(".receipt.json").read_text())["state"], "failed")


if __name__ == "__main__":
    unittest.main()
