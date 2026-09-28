"""Current task states must not be inferred from prose or previous results."""

import unittest

from status import describe_status


class AgentStatusTest(unittest.TestCase):
    def pane(self, state="idle"):
        return {"agent_status": state, "agent": "codex"}

    def data(self, state="idle", **values):
        return dict(provider_supported=True, error=None, state=state, **values)

    def test_ready_and_done_allow_new_requests(self):
        for state, expected in (("idle", "ready"), ("complete", "done")):
            for native in ("idle", "done"):
                with self.subTest(native=native, state=state):
                    result = describe_status(self.pane(native), self.data(state))
                    self.assertEqual(result["code"], expected)
                    self.assertTrue(result["can_send"])

    def test_live_working_overrides_old_complete_or_interrupted_history(self):
        for state in ("idle", "complete", "interrupted"):
            result = describe_status(self.pane("working"), self.data(state))
            self.assertEqual(result["code"], "working")
            self.assertFalse(result["can_send"])

    def test_native_input_prompt_wins_over_stale_history_and_pending(self):
        result = describe_status(self.pane("blocked"), self.data("complete"), pending=True)
        self.assertEqual(result["code"], "waiting_user")
        self.assertEqual(result["label"], "Needs your input")
        self.assertFalse(result["can_send"])

    def test_pending_submission_is_not_previous_result(self):
        result = describe_status(self.pane(), self.data("complete"), pending=True)
        self.assertEqual(result["code"], "working")
        self.assertFalse(result["can_send"])

    def test_outstanding_known_tool_distinguishes_waits(self):
        for kind, expected in (("user", "waiting_user"), ("agents", "waiting_agents")):
            result = describe_status(self.pane("working"), self.data("running", waiting={"kind": kind, "tool": "known_tool"}))
            self.assertEqual(result["code"], expected)
            self.assertFalse(result["can_send"])

    def test_completed_wait_cannot_override_current_task(self):
        waiting = {"kind": "agents", "tool": "wait_agent"}
        for state, expected in (("idle", "ready"), ("complete", "done"), ("interrupted", "interrupted")):
            result = describe_status(self.pane(), self.data(state, waiting=waiting))
            self.assertEqual(result["code"], expected)

    def test_idle_open_turn_is_unconfirmed_never_ready_or_waiting_forever(self):
        for waiting in (None, {"kind": "user"}, {"kind": "agents"}):
            result = describe_status(self.pane(), self.data("running", waiting=waiting))
            self.assertEqual(result["code"], "unknown")
            self.assertIn("recorded task is still open", result["detail"])
            self.assertFalse(result["can_send"])

    def test_prose_radio_messages_and_arbitrary_tokens_are_not_wait_evidence(self):
        data = self.data("running", messages=[{"kind": "commentary", "text": "Waiting for agents"}],
                         activity=[{"text": "Used collaboration.wait_agent"}])
        pane = dict(self.pane("working"), tokens={"waiting": "agents"},
                    state_labels={"working": "Waiting for approval"}, radio_pending=10)
        self.assertEqual(describe_status(pane, data)["code"], "working")
        self.assertEqual(describe_status(self.pane(), self.data("idle", messages=data["messages"]))["code"], "ready")

    def test_unknown_wait_shape_does_not_imply_approval(self):
        for waiting in (None, "user", {}, {"kind": "approval"}, {"kind": ["user"]}):
            result = describe_status(self.pane("working"), self.data("running", waiting=waiting))
            self.assertEqual(result["code"], "working")

    def test_interrupted_blocks_new_request_until_terminal_checked(self):
        result = describe_status(self.pane(), self.data("interrupted"))
        self.assertEqual(result["code"], "interrupted")
        self.assertFalse(result["can_send"])

    def test_unreadable_or_unsupported_session_does_not_claim_ready(self):
        for data in ({"state": "idle"}, self.data("unexpected"),
                     dict(self.data(), error="history_schema_unknown"),
                     dict(self.data(), provider_supported=False)):
            self.assertEqual(describe_status(self.pane(), data)["code"], "unknown")
            self.assertFalse(describe_status(self.pane(), data)["can_send"])

    def test_native_working_and_blocked_are_known_without_transcript(self):
        for native, expected in (("working", "working"), ("blocked", "waiting_user")):
            self.assertEqual(describe_status(self.pane(native), {})["code"], expected)

    def test_unrecognized_runtime_never_uses_old_completed_task(self):
        for native in (None, "unknown", "future_state", "waiting"):
            result = describe_status(self.pane(native), self.data("complete"))
            self.assertEqual(result["code"], "unknown")
            self.assertFalse(result["can_send"])

    def test_unknown_data_and_raw_errors_are_safe_and_not_echoed(self):
        for pane, data in ((None, {}), ({}, None), ([], [])):
            self.assertEqual(describe_status(pane, data)["code"], "unknown")
        data = dict(self.data(), error="secret raw path\x1b[31m")
        self.assertNotIn("secret", str(describe_status(self.pane(), data)))

    def test_status_sequence_follows_current_evidence_without_sticking(self):
        sequence = [
            ("idle", "idle", None, "ready"),
            ("working", "running", None, "working"),
            ("working", "running", {"kind": "agents"}, "waiting_agents"),
            ("working", "running", None, "working"),
            ("blocked", "running", None, "waiting_user"),
            ("working", "running", None, "working"),
            ("done", "complete", None, "done"),
        ]
        for native, state, waiting, code in sequence:
            self.assertEqual(describe_status(self.pane(native), self.data(state, waiting=waiting))["code"], code)


if __name__ == "__main__":
    unittest.main()
