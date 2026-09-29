"""Synthetic native-child ownership checks; never launch a provider."""
from copy import deepcopy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from backend import AccountError
import switch_launch


def fixture():
    candidate = dict(pane_id="w9:p2", terminal_id="synthetic-terminal", workspace="w9", label="worker@004",
                     cwd="C:/Synthetic", pane_cwd="C:/Synthetic", provider="codex",
                     session_id="00000000-1111-2222-3333-444444444444")
    env = dict(HERDR_PANE_ID="w9:p2", HERDR_WORKSPACE_ID="w9", CODEX_HOME="C:/Profiles/target")
    options = ["-m", "synthetic-model", "-a", "never", "-s", "danger-full-access",
               "-c", 'model_reasoning_effort="high"']
    plan = dict(kind="switch", provider="codex", profile_id="target", switch_candidate=candidate, env={"CODEX_HOME": env["CODEX_HOME"]},
                model="synthetic-model", resume_options=options)
    argv = ["C:/Tools/codex.exe", *options, "resume", candidate["session_id"]]
    pane = dict(pane_id="w9:p2", terminal_id=candidate["terminal_id"], workspace_id="w9",
                label=candidate["label"], cwd=candidate["cwd"], agent="codex")
    process = dict(pane_id="w9:p2", shell_pid=10, foreground_process_group_id=20,
                   foreground_processes=[dict(pid=20, name="codex.exe", cwd=candidate["cwd"], argv=argv)])
    return plan, argv, env, pane, process


def event(plan, **changes):
    settings = dict(model=plan["model"], reasoning_effort="high", approval_policy="never",
                    permission_profile={"type": "disabled"}, model_provider_id="openai",
                    approvals_reviewer="user", runtime_workspace_roots=[])
    settings.update(changes)
    return dict(type="event_msg", payload=dict(type="thread_settings_applied",
                                               thread_id=plan["switch_candidate"]["session_id"], thread_settings=settings))


class Native:
    def __init__(self, pane, process, *, settles=True):
        self.pane, self.process = deepcopy(pane), deepcopy(process)
        self.calls, self.settles = [], settles

    def __call__(self, args):
        self.calls.append(list(args))
        if args[:2] == ["pane", "get"]:
            return {"pane": deepcopy(self.pane)}
        if args[:2] == ["pane", "process-info"]:
            return {"process_info": deepcopy(self.process)}
        if args[:2] == ["pane", "report-agent-session"]:
            if self.settles:
                self.pane["agent_session"] = dict(source="herdr:codex", agent="codex", kind="id",
                                                  value=args[args.index("--agent-session-id") + 1])
            return {}
        raise AssertionError(args)


class ResumeTest(unittest.TestCase):
    def setUp(self):
        self.addCleanup(switch_launch._UNCONFIRMED_CHILDREN.clear)

    def run_case(self, native=None, child=None, *, append_event=True, event_change=None, visible=None, **kwargs):
        plan, argv, env, pane, process = fixture()
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        env["CODEX_HOME"] = plan["env"]["CODEX_HOME"] = directory.name
        path = Path(directory.name).resolve() / "sessions" / ("rollout-" + plan["switch_candidate"]["session_id"] + ".jsonl")
        path.parent.mkdir()
        # Matching old metadata is deliberately present; it is not launch proof.
        path.write_text(json.dumps(event(plan)) + "\n", encoding="utf-8")
        plan["switch_session_path"] = str(path)
        receipt = Path(directory.name).resolve() / "switch.receipt.json"
        receipt.write_text(json.dumps({"state": "launching", "profile_id": "target",
                                       "session_id": plan["switch_candidate"]["session_id"]}))
        plan["switch_receipt_path"] = str(receipt)
        self.receipt = receipt
        native = native or Native(pane, process)
        child = child or Mock(pid=20, poll=Mock(return_value=None), wait=Mock(return_value=0))
        def launch(*_, **__):
            if append_event:
                appended = event(plan)
                if event_change:
                    event_change(appended)
                with path.open("a", encoding="utf-8") as stream:
                    stream.write(json.dumps(appended) + "\n")
            return child
        popen = Mock(side_effect=launch)
        result = switch_launch.run_codex_resume(plan, argv, env, popen=popen, cli=native,
                                                clock=lambda: 0, sleep=lambda _: None, now=lambda: 123,
                                                visible=visible or (lambda _: "› Ask Codex to do anything\nsynthetic-model · C:/Synthetic\n? for shortcuts\n"),
                                                timeout=.1, interval=.05, **kwargs)
        return result, native, child, popen

    def test_owned_native_resume_reports_once_then_waits_normally(self):
        result, native, child, popen = self.run_case()
        self.assertEqual(result, 0)
        plan, argv, env, _, _ = fixture()
        popen.assert_called_once()
        self.assertEqual(popen.call_args.args, (argv,))
        self.assertEqual(popen.call_args.kwargs["env"]["HERDR_PANE_ID"], env["HERDR_PANE_ID"])
        child.wait.assert_called_once_with()
        reports = [call for call in native.calls if call[1] == "report-agent-session"]
        self.assertEqual(reports, [["pane", "report-agent-session", "w9:p2", "--source", "herdr:codex",
                                  "--agent", "codex", "--seq", "123000", "--agent-session-id",
                                  plan["switch_candidate"]["session_id"], "--session-start-source", "resume"]])
        self.assertGreater(len(native.calls), native.calls.index(reports[0]) + 2)
        self.assertEqual(json.loads(self.receipt.read_text()), {
            "state": "ready", "profile_id": "target",
            "session_id": plan["switch_candidate"]["session_id"], "native_pid": 20,
        })
        child.terminate.assert_not_called()
        child.kill.assert_not_called()

    def test_wrong_native_generation_or_resume_argv_is_never_reported(self):
        for mutate in [lambda p, r: r["foreground_processes"][0].update(pid=21),
                       lambda p, r: r.update(foreground_process_group_id=21),
                       lambda p, r: r["foreground_processes"][0].update(name="python.exe"),
                       lambda p, r: r["foreground_processes"][0].update(argv=["C:/Tools/codex.exe", "resume", "other"]),
                       lambda p, r: r["foreground_processes"][0].update(cwd="C:/Other"),
                       lambda p, r: p.update(terminal_id="other"),
                       lambda p, r: p.update(workspace_id="w8"),
                       lambda p, r: p.update(label="other@004"),
                       lambda p, r: p.update(cwd="C:/Other"),
                       lambda p, r: p.update(agent="claude"),
                       lambda p, r: p.update(agent_session={"value": "other-session"})]:
            _, _, _, pane, process = fixture()
            mutate(pane, process)
            native = Native(pane, process)
            with self.assertRaises(AccountError) as caught:
                self.run_case(native)
            self.assertEqual(caught.exception.code, "switch_resume_unverified")
            self.assertFalse(any(call[1] == "report-agent-session" for call in native.calls))

    def test_delayed_provider_detection_is_polled_without_relaunch(self):
        _, _, _, pane, process = fixture()
        native = Native(pane, process)
        count = 0
        def delayed(args):
            nonlocal count
            result = native(args)
            if args[:2] == ["pane", "get"]:
                count += 1
                if count == 1:
                    result["pane"]["agent"] = None
            return result
        result, _, _, popen = self.run_case(delayed)
        self.assertEqual(result, 0)
        self.assertEqual(popen.call_count, 1)

    def test_no_report_acceptance_times_out_without_stopping_or_retrying(self):
        _, _, _, pane, process = fixture()
        native = Native(pane, process, settles=False)
        child = Mock(pid=20, poll=Mock(return_value=None))
        with self.assertRaises(AccountError):
            self.run_case(native, child)
        self.assertEqual(sum(call[1] == "report-agent-session" for call in native.calls), 1)
        child.terminate.assert_not_called()
        child.kill.assert_not_called()
        child.wait.assert_not_called()
        self.assertIn(child, switch_launch._UNCONFIRMED_CHILDREN)
        self.assertEqual(json.loads(self.receipt.read_text())["state"], "launching")

    def test_folder_trust_gate_or_only_old_event_never_reports_ready(self):
        _, _, _, pane, process = fixture()
        native = Native(pane, process)
        child = Mock(pid=20, poll=Mock(return_value=None))
        with self.assertRaises(AccountError):
            self.run_case(native, child, append_event=False)
        self.assertFalse(any(call[1] == "report-agent-session" for call in native.calls))
        child.kill.assert_not_called()
        child.terminate.assert_not_called()

    def test_wrong_fresh_thread_or_settings_never_reports(self):
        changes = [lambda row: row["payload"].update(thread_id="99999999-1111-2222-3333-444444444444"),
                   lambda row: row["payload"]["thread_settings"].update(model="another-model"),
                   lambda row: row["payload"]["thread_settings"].update(reasoning_effort="low"),
                   lambda row: row["payload"]["thread_settings"].update(approval_policy="on-request"),
                   lambda row: row["payload"]["thread_settings"].update(permission_profile={"type": "other"})]
        for change in changes:
            _, _, _, pane, process = fixture()
            native = Native(pane, process)
            with self.assertRaises(AccountError):
                self.run_case(native, event_change=change)
            self.assertFalse(any(call[1] == "report-agent-session" for call in native.calls))

    def test_fresh_settings_during_sandbox_or_login_gate_do_not_report(self):
        for gate in ("Folder access\nTrust this folder?", "Windows sandbox setup\nSet up default sandbox (admin)",
                     "Sign in with ChatGPT", "Initializing…", "› Pick an option\n1. Continue"):
            _, _, _, pane, process = fixture()
            native = Native(pane, process)
            child = Mock(pid=20, poll=Mock(return_value=None))
            with self.assertRaises(AccountError):
                self.run_case(native, child, visible=lambda _: gate)
            self.assertFalse(any(call[1] == "report-agent-session" for call in native.calls))
            child.terminate.assert_not_called()

    def test_onboarding_gate_overrides_visible_old_composer(self):
        text = "› Ask Codex to do anything\n? for shortcuts\nWindows sandbox setup\nUse non-admin sandbox"
        self.assertFalse(switch_launch._visible_ready(text))
        self.assertTrue(switch_launch._visible_ready("│ » Ask Codex to do anything\n│ synthetic model\n│ ? for shortcuts"))
        self.assertTrue(switch_launch._visible_ready("›\nsynthetic-model\n? for shortcuts"))
        self.assertFalse(switch_launch._visible_ready("old › Ask Codex to do anything\ntext ? for shortcuts"))

    def test_old_composer_followed_by_shell_launch_text_is_not_ready(self):
        text = "› Ask Codex to do anything\nsynthetic-model\n? for shortcuts\nPS C:\\Synthetic> python launch.py"
        self.assertFalse(switch_launch._visible_ready(text))

    def test_early_session_hook_cannot_write_ready_receipt_during_gate(self):
        plan, _, _, pane, process = fixture()
        pane["agent_session"] = dict(source="herdr:codex", agent="codex", kind="id",
                                     value=plan["switch_candidate"]["session_id"])
        native = Native(pane, process)
        with self.assertRaises(AccountError):
            self.run_case(native, visible=lambda _: "Windows sandbox setup")
        self.assertEqual(json.loads(self.receipt.read_text())["state"], "launching")
        self.assertFalse(any(call[1] == "report-agent-session" for call in native.calls))

    def test_ready_receipt_failure_is_not_success_and_does_not_stop_child(self):
        child = Mock(pid=20, poll=Mock(return_value=None))
        with patch.object(switch_launch, "_atomic_json", side_effect=OSError("private path")):
            with self.assertRaises(AccountError):
                self.run_case(child=child)
        child.wait.assert_not_called()
        child.kill.assert_not_called()
        self.assertEqual(json.loads(self.receipt.read_text())["state"], "launching")

    def test_sandbox_gate_can_clear_without_relaunch_or_reporting_early(self):
        visible = Mock(side_effect=["Windows sandbox setup", "› Ask Codex to do anything\n? for shortcuts",
                                    "› Ask Codex to do anything\n? for shortcuts"])
        result, native, _, popen = self.run_case(visible=visible)
        self.assertEqual(result, 0)
        popen.assert_called_once()
        self.assertEqual(sum(call[1] == "report-agent-session" for call in native.calls), 1)

    def fresh_reader(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        plan, _, _, _, _ = fixture()
        plan["env"]["CODEX_HOME"] = directory.name
        path = Path(directory.name).resolve() / "sessions" / ("rollout-" + plan["switch_candidate"]["session_id"] + ".jsonl")
        path.parent.mkdir()
        path.write_text(json.dumps(event(plan)) + "\n", encoding="utf-8")
        plan["switch_session_path"] = str(path)
        reader = switch_launch._FreshResume(plan, plan["switch_candidate"])
        self.addCleanup(reader.close)
        return reader, plan, path

    def test_append_reader_waits_for_complete_metadata_line(self):
        reader, plan, path = self.fresh_reader()
        self.assertFalse(reader.ready(plan))
        with path.open("ab") as stream:
            stream.write(json.dumps(event(plan)).encode("utf-8"))
        self.assertFalse(reader.ready(plan))
        with path.open("ab") as stream:
            stream.write(b"\n")
        self.assertTrue(reader.ready(plan))
        self.assertFalse(reader.ready(plan))

    def test_append_reader_rejects_oversized_line_or_total_growth(self):
        for bounded_total in (False, True):
            reader, plan, path = self.fresh_reader()
            with path.open("ab") as stream:
                stream.write(b"x" * 129)
            with patch.object(switch_launch, "_MAX_APPEND" if bounded_total else "_MAX_LINE", 128):
                with self.assertRaises(AccountError):
                    reader.ready(plan)

    def test_append_reader_rejects_rollout_truncation(self):
        reader, plan, path = self.fresh_reader()
        path.write_bytes(b"\n")
        with self.assertRaises(AccountError):
            reader.ready(plan)

    def test_ended_child_does_not_donate_its_pid_to_another_generation(self):
        _, _, _, pane, process = fixture()
        native = Native(pane, process)
        child = Mock(pid=20, poll=Mock(side_effect=[None, 0]))
        with self.assertRaises(AccountError):
            self.run_case(native, child)
        self.assertFalse(any(call[1] == "report-agent-session" for call in native.calls))

    def test_failed_report_keeps_child_and_redacts_transport_details(self):
        _, _, _, pane, process = fixture()
        native = Native(pane, process)
        child = Mock(pid=20, poll=Mock(return_value=None))
        def fail(args):
            if args[:2] == ["pane", "report-agent-session"]:
                raise RuntimeError("PRIVATE_TOKEN_DIAGNOSTIC")
            return native(args)
        with self.assertRaises(AccountError) as caught:
            self.run_case(fail, child)
        self.assertNotIn("PRIVATE", str(caught.exception))
        child.kill.assert_not_called()
        self.assertIn(child, switch_launch._UNCONFIRMED_CHILDREN)

    def test_invalid_switch_or_non_native_executable_never_launches(self):
        for change in ("kind", "provider", "session", "context", "home", "wrapper"):
            plan, argv, env, _, _ = fixture()
            if change in ("kind", "provider"):
                plan[change] = "other"
            elif change == "session":
                argv[-1] = "other"
            elif change == "context":
                env["HERDR_PANE_ID"] = "w9:p3"
            elif change == "home":
                env["CODEX_HOME"] = "C:/Profiles/other"
            else:
                argv[0] = "C:/Tools/codex.cmd"
            popen = Mock()
            with self.assertRaises(AccountError):
                switch_launch.run_codex_resume(plan, argv, env, popen=popen)
            popen.assert_not_called()


if __name__ == "__main__":
    unittest.main()
