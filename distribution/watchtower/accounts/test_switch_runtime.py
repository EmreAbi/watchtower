"""Hermetic same-pane lifecycle tests. No native panes or providers are started."""
from copy import deepcopy
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from backend import AccountError
from switch_runtime import SwitchRuntime


def candidate(**changes):
    return dict(pane_id="w9:p2", terminal_id="term_synthetic", session_id="synthetic-session",
                provider="codex", workspace="w9", label="worker@004", cwd="C:/Synthetic Project", **changes)


def active():
    selected = candidate()
    session = dict(source="herdr:codex", agent="codex", kind="id", value=selected["session_id"])
    pane = dict(pane_id=selected["pane_id"], terminal_id=selected["terminal_id"], workspace_id="w9",
                label=selected["label"], cwd=selected["cwd"], agent="codex", agent_status="idle", agent_session=session)
    agent = dict(pane, interactive_ready=True, launch_pending=False, state_change_seq=5)
    process = dict(pane_id=selected["pane_id"], shell_pid=10, foreground_process_group_id=20,
                   foreground_processes=[dict(pid=20, name="codex.exe", cwd=selected["cwd"],
                                              argv=["codex.exe", "-m", "synthetic-model"])])
    return dict(pane=pane, agent=agent, process=process)


def shell():
    result = active()
    result["pane"].update(agent=None, agent_status="unknown", agent_session=None)
    result["agent"] = None
    result["process"].update(foreground_process_group_id=10,
                             foreground_processes=[dict(pid=10, name="powershell.exe", cwd="c:\\synthetic project\\")])
    return result


class Native:
    def __init__(self, before=None, after=None):
        self.before = deepcopy(before or active())
        self.after = deepcopy(after or shell())
        self.current = self.before
        self.calls = []

    def __call__(self, args):
        self.calls.append(args)
        if args[:2] == ["pane", "list"]:
            return {"panes": [deepcopy(self.current["pane"])]}
        if args[:2] == ["agent", "list"]:
            return {"agents": [deepcopy(self.current["agent"])] if self.current["agent"] else []}
        if args[:2] == ["pane", "get"]:
            return {"pane": deepcopy(self.current["pane"])}
        if args[:2] == ["agent", "get"]:
            if self.current["agent"] is None:
                raise RuntimeError("Watchtower: agent_not_found. No automatic retry.")
            return {"agent": deepcopy(self.current["agent"])}
        if args[:2] == ["pane", "process-info"]:
            return {"process_info": deepcopy(self.current["process"])}
        if args[:2] == ["agent", "quit-if-idle"]:
            if "--check" not in args:
                self.current = self.after
            return {}
        if args[:2] == ["pane", "run"]:
            return {}
        raise AssertionError(args)


class RuntimeTest(unittest.TestCase):
    def setUp(self):
        # Fixtures use synthetic PIDs: never inspect unrelated host processes.
        process_proof = patch("switch_runtime.shell_has_no_children", return_value=True)
        self.shell_idle = process_proof.start()
        self.addCleanup(process_proof.stop)

    def assert_code(self, code, callback):
        with self.assertRaises(AccountError) as caught:
            callback()
        self.assertEqual(caught.exception.code, code)

    def test_snapshot_and_inspect_preserve_metadata_without_actions(self):
        native = Native()
        runtime = SwitchRuntime(native)
        self.assertEqual(len(runtime.snapshot()["panes"]), 1)
        result = runtime.preflight(candidate())
        self.assertEqual(result["state"], "ready")
        self.assertEqual(result["readiness"], "managed")
        self.assertEqual(result["process"]["foreground_processes"][0]["argv"], ["codex.exe", "-m", "synthetic-model"])
        self.assertFalse(any(call[1] in ("prompt", "run") or (call[1] == "quit-if-idle" and "--check" not in call) for call in native.calls))

    def test_legacy_radio_readiness_requires_real_codex_and_official_session(self):
        state = active()
        del state["agent"]["interactive_ready"]
        del state["agent"]["launch_pending"]
        result = SwitchRuntime(Native(state)).preflight(candidate())
        self.assertEqual(result["readiness"], "legacy-native")
        state["agent"]["agent_session"]["source"] = "unverified"
        self.assert_code("switch_agent_unverified", lambda: SwitchRuntime(Native(state)).preflight(candidate()))

    def test_busy_or_pending_never_sends_quit(self):
        for key, value in [("agent_status", "working"), ("agent_status", "blocked"), ("agent_status", "unknown"), ("launch_pending", True)]:
            state = active()
            state["agent"][key] = value
            native = Native(state)
            self.assert_code("switch_agent_busy", lambda: SwitchRuntime(native).stop(candidate()))
            self.assertFalse(any(call[1] in ("quit-if-idle", "prompt") for call in native.calls))

    def test_changed_identity_session_cwd_or_competing_process_is_rejected(self):
        changes = [("pane", "pane_id", "w9:p3", "switch_identity_changed"),
                   ("pane", "terminal_id", "different", "switch_identity_changed"),
                   ("pane", "label", "other@004", "switch_identity_changed"),
                   ("agent", "workspace_id", "w8", "switch_identity_changed"),
                   ("pane", "cwd", "C:/Other", "switch_identity_changed")]
        for item, key, value, code in changes:
            state = active()
            state[item][key] = value
            self.assert_code(code, lambda: SwitchRuntime(Native(state)).preflight(candidate()))
        state = active()
        state["agent"]["agent_session"]["value"] = "another"
        self.assert_code("switch_agent_unverified", lambda: SwitchRuntime(Native(state)).stop(candidate()))
        for process in [dict(pid=21, name="claude.exe", cwd=candidate()["cwd"]), dict(pid=21, name="python.exe", cwd=candidate()["cwd"])]:
            state = active()
            state["process"]["foreground_processes"].append(process)
            self.assert_code("switch_agent_unverified", lambda: SwitchRuntime(Native(state)).stop(candidate()))

    def test_stop_rechecks_then_sends_quit_once_and_proves_shell(self):
        native = Native()
        result = SwitchRuntime(native).stop(candidate())
        self.assertEqual(result["state"], "stopped")
        self.assertEqual(result["shell"]["pid"], 10)
        self.assertEqual([call for call in native.calls if call[1] == "quit-if-idle" and "--check" not in call],
                         [["agent", "quit-if-idle", "w9:p2", "--terminal", "term_synthetic", "--agent-session-id", "synthetic-session", "--state-seq", "5"]])
        self.assertEqual(native.calls[:6], native.calls[6:12])
        self.assertFalse(any(call[1] == "prompt" for call in native.calls))

    def test_failed_quit_times_out_without_retry_kill_or_release(self):
        native = Native(after=active())
        runtime = SwitchRuntime(native, clock=lambda: 0, sleep=lambda _: None, stop_timeout=0.1, poll_interval=0.05)
        self.assert_code("switch_stop_timeout", lambda: runtime.stop(candidate()))
        self.assertEqual(sum(call[1] == "quit-if-idle" and "--check" not in call for call in native.calls), 1)
        self.assertFalse(any(call[1] in ("run", "close", "release-agent", "send-keys") for call in native.calls))

    def test_stop_waits_for_unrecognized_children_to_exit(self):
        native = Native()
        idle = Mock(side_effect=[False, False, True])
        runtime = SwitchRuntime(native, shell_idle=idle, sleep=lambda _: None)
        result = runtime.stop(candidate())
        self.assertEqual(result["state"], "stopped")
        self.assertEqual([call.args for call in idle.call_args_list], [(10,), (10,), (10,)])
        self.assertEqual(sum(call[1] == "quit-if-idle" and "--check" not in call for call in native.calls), 1)

    def test_unverified_process_tree_cannot_prove_stopped_or_allow_launch(self):
        for proof in (False, None, 1, OSError("PRIVATE_PROCESS_ERROR")):
            native = Native()
            idle = Mock(**({"side_effect": proof} if isinstance(proof, Exception) else {"return_value": proof}))
            runtime = SwitchRuntime(native, shell_idle=idle, clock=lambda: 0, sleep=lambda _: None,
                                    stop_timeout=.1, poll_interval=.05)
            self.assert_code("switch_stop_timeout", lambda: runtime.stop(candidate()))
            self.assertFalse(any(call[1] == "run" for call in native.calls))
        with tempfile.TemporaryDirectory() as directory:
            ticket, runner = Path(directory).resolve() / "ticket.json", Path(directory).resolve() / "switch_service.py"
            ticket.write_text("{}")
            runner.write_text("# fixture")
            native = Native(before=shell())
            runtime = SwitchRuntime(native, shell_idle=lambda _: False, runner_path=runner)
            self.assert_code("switch_shell_unverified", lambda: runtime.launch(candidate(), ticket))
            self.assertFalse(any(call[1] == "run" for call in native.calls))

    def test_shell_requires_original_root_pid_no_children_and_matching_cwd(self):
        for mutate in [lambda s: s["process"].update(shell_pid=99),
                       lambda s: s["process"].update(foreground_process_group_id=99),
                       lambda s: s["process"]["foreground_processes"][0].update(name="cmd.exe"),
                       lambda s: s["process"]["foreground_processes"][0].update(cwd="C:/Other"),
                       lambda s: s["process"].update(foreground_processes=[]),
                       lambda s: s["pane"].update(agent="codex", agent_session=active()["pane"]["agent_session"])]:
            state = shell()
            mutate(state)
            native = Native(after=state)
            runtime = SwitchRuntime(native, clock=lambda: 0, sleep=lambda _: None, stop_timeout=0.1, poll_interval=0.05)
            self.assert_code("switch_stop_timeout", lambda: runtime.stop(candidate()))

    def test_allow_shell_is_explicit_and_does_not_send_quit_again(self):
        native = Native(before=shell())
        runtime = SwitchRuntime(native)
        self.assert_code("switch_agent_busy", lambda: runtime.preflight(candidate()))
        result = runtime.stop(candidate(allow_shell=True))
        self.assertEqual(result["state"], "stopped")
        self.assertFalse(any(call[1] in ("quit-if-idle", "prompt") for call in native.calls))

    def test_missing_generation_is_unverified_and_guard_rejection_never_falls_back(self):
        state = active()
        del state["agent"]["state_change_seq"]
        self.assert_code("switch_agent_unverified", lambda: SwitchRuntime(Native(state)).stop(candidate()))
        native = Native()
        def reject_guard(args):
            if args[:2] == ["agent", "quit-if-idle"] and "--check" not in args:
                native.calls.append(args)
                raise RuntimeError("Watchtower: unknown_method. No automatic retry.")
            return native(args)
        self.assert_code("switch_stop_unconfirmed", lambda: SwitchRuntime(reject_guard).stop(candidate()))
        self.assertEqual(sum(call[1] == "quit-if-idle" and "--check" not in call for call in native.calls), 1)
        self.assertFalse(any(call[1] in ("prompt", "run", "send-keys", "close", "release-agent") for call in native.calls))

    def test_old_server_fails_read_only_check_before_any_exit_request(self):
        native = Native()
        def old_server(args):
            if args[:2] == ["agent", "quit-if-idle"]:
                native.calls.append(args)
                raise RuntimeError("Watchtower: unknown_method. No automatic retry.")
            return native(args)
        self.assert_code("switch_guard_unavailable", lambda: SwitchRuntime(old_server).stop(candidate()))
        self.assertEqual([call for call in native.calls if call[1] == "quit-if-idle"],
                         [["agent", "quit-if-idle", "w9:p2", "--terminal", "term_synthetic", "--agent-session-id", "synthetic-session", "--state-seq", "5", "--check"]])

    def test_guard_check_preserves_known_rejections_without_restart_advice(self):
        for native_code, expected in [("agent_quit_busy", "switch_agent_busy"),
                                      ("agent_quit_identity_changed", "switch_identity_changed"),
                                      ("agent_quit_session_in_use", "switch_session_shared"),
                                      ("agent_quit_unverified", "switch_agent_unverified"),
                                      ("agent_not_found", "switch_identity_changed"),
                                      ("pane_not_found", "switch_identity_changed")]:
            with self.subTest(native_code=native_code):
                native = Native()
                def rejected(args):
                    if args[:2] == ["agent", "quit-if-idle"]:
                        native.calls.append(args)
                        raise RuntimeError(f"Watchtower: {native_code}. No automatic retry.")
                    return native(args)
                with self.assertRaises(AccountError) as caught:
                    SwitchRuntime(rejected).stop(candidate())
                self.assertEqual(caught.exception.code, expected)
                self.assertNotIn("Restart", caught.exception.message)
                self.assertFalse(any(call[1] == "quit-if-idle" and "--check" not in call for call in native.calls))

    def test_guard_check_unknown_diagnostics_are_redacted_without_restart_advice(self):
        for diagnostic in ("PRIVATE=TOKEN", "Watchtower: new_failure. No automatic retry."):
            native = Native()
            def broken(args):
                if args[:2] == ["agent", "quit-if-idle"]:
                    raise RuntimeError(diagnostic)
                return native(args)
            with self.assertRaises(AccountError) as caught:
                SwitchRuntime(broken).preflight(candidate())
            self.assertEqual(caught.exception.code, "switch_runtime_unavailable")
            self.assertNotIn("Restart", caught.exception.message)
            self.assertNotIn(diagnostic, caught.exception.message)

    def test_native_invalid_request_means_guard_capability_is_unavailable(self):
        native = Native()
        def old_server(args):
            if args[:2] == ["agent", "quit-if-idle"]:
                raise RuntimeError("Watchtower: invalid_request. No automatic retry.")
            return native(args)
        self.assert_code("switch_guard_unavailable", lambda: SwitchRuntime(old_server).preflight(candidate()))

    def test_shared_session_cannot_be_stopped_or_verified(self):
        native = Native()
        def duplicate(args):
            result = native(args)
            if args[:2] == ["pane", "list"]:
                other = deepcopy(result["panes"][0])
                other.update(pane_id="w9:p3", terminal_id="other-terminal")
                result["panes"].append(other)
            return result
        runtime = SwitchRuntime(duplicate)
        self.assert_code("switch_session_shared", lambda: runtime.stop(candidate()))
        self.assert_code("switch_session_shared", lambda: runtime.verify_resumed(candidate()))
        self.assertFalse(any(call[1] in ("quit-if-idle", "prompt") for call in native.calls))

    def test_verify_resumed_allows_working_but_requires_real_identity_and_process(self):
        state = active()
        state["pane"]["agent_status"] = state["agent"]["agent_status"] = "working"
        native = Native(state)
        self.assertEqual(SwitchRuntime(native).verify_resumed(candidate())["state"], "resumed")
        state["process"]["foreground_processes"][0]["name"] = "powershell.exe"
        self.assert_code("switch_agent_unverified", lambda: SwitchRuntime(Native(state)).verify_resumed(candidate()))

    def test_pane_root_and_actual_foreground_cwd_have_separate_evidence(self):
        state = active()
        state["pane"]["cwd"] = "C:/Shell Root"
        selected = candidate(pane_cwd="C:/Shell Root")
        self.assertEqual(SwitchRuntime(Native(state)).preflight(selected)["state"], "ready")
        selected["pane_cwd"] = "C:/Changed Root"
        self.assert_code("switch_identity_changed", lambda: SwitchRuntime(Native(state)).preflight(selected))

    def test_launch_uses_literal_arguments_only_inside_verified_same_shell(self):
        with tempfile.TemporaryDirectory(prefix="switch ' $() ` ") as root:
            root = Path(root).resolve()
            ticket, runner = root / "ticket.json", root / "switch_service.py"
            ticket.write_text("{}")
            runner.write_text("# synthetic fixture")
            native = Native()
            runtime = SwitchRuntime(native, runner_path=runner)
            runtime.stop(candidate())
            self.assertEqual(runtime.launch(candidate(), ticket), {"state": "resume_requested"})
            command = native.calls[-1]
            self.assertEqual(command[:3], ["pane", "run", "w9:p2"])
            self.assertIn(" '-B' ", command[3])
            self.assertIn(" '--run-ticket' ", command[3])
            self.assertIn("switch '' $() ` ", command[3])
            self.assertNotIn("-Command", command[3])

    def test_launch_revalidates_shell_and_failed_delivery_is_not_retried(self):
        with tempfile.TemporaryDirectory() as root:
            ticket, runner = Path(root).resolve() / "ticket.json", Path(root).resolve() / "switch_service.py"
            ticket.write_text("{}")
            runner.write_text("# fixture")
            native = Native()
            runtime = SwitchRuntime(native, runner_path=runner)
            runtime.stop(candidate())
            native.current["process"].update(shell_pid=11, foreground_process_group_id=11)
            native.current["process"]["foreground_processes"][0]["pid"] = 11
            self.assert_code("switch_shell_unverified", lambda: runtime.launch(candidate(), ticket))
            self.assertFalse(any(call[1] == "run" for call in native.calls))
            native.current = shell()
            def fail_run(args):
                if args[:2] == ["pane", "run"]:
                    native.calls.append(args)
                    raise RuntimeError("PRIVATE_NATIVE_DIAGNOSTIC")
                return native(args)
            runtime.cli = fail_run
            self.assert_code("switch_launch_unconfirmed", lambda: runtime.launch(candidate(), ticket))
            self.assertEqual(sum(call[1] == "run" for call in native.calls), 1)

    def test_unsupported_provider_invalid_ticket_and_raw_errors_are_safe(self):
        value = candidate()
        value["provider"] = "claude"
        self.assert_code("switch_provider_unsupported", lambda: SwitchRuntime(Native()).preflight(value))
        self.assert_code("switch_launch_invalid", lambda: SwitchRuntime(Native()).launch(candidate(), "relative.json"))
        def broken(_):
            raise RuntimeError("TOKEN=PRIVATE")
        try:
            SwitchRuntime(broken).snapshot()
        except AccountError as error:
            self.assertNotIn("PRIVATE", str(error))
            self.assertEqual(error.code, "switch_runtime_unavailable")
        else:
            self.fail("Missing safe failure")


if __name__ == "__main__":
    unittest.main()
