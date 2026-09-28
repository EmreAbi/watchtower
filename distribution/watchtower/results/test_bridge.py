"""Explicit result actions against fake pane APIs and isolated SQLite bindings."""

from copy import deepcopy
from contextlib import closing
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import Mock, patch

import bridge
from bridge import QUICK_PREFIX, ResultsError, ResultsService
from session_reader import CodexSessionReader


SESSION = "019fa455-1847-7e41-b28f-3cf4a0ab989b"


class ResultsBridgeTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.home = self.root / "profile"
        self.home.mkdir()
        self.output = self.root / "result.md"
        self.output.write_text("# Result")
        self.env = {"WATCHTOWER_RESULTS_PANE": "w9:p1", "HERDR_PLUGIN_STATE_DIR": str(self.root / "results"),
                    "CODEX_HOME": str(self.home)}
        self.pane = dict(pane_id="w9:p1", agent="codex", agent_status="idle", label="worker",
                         cwd=str(self.root), workspace_id="w9", agent_session=dict(value=SESSION, agent="codex"))
        self.calls = []
        self.reader = Mock()
        self.data = dict(provider_supported=True, state="complete", error=None, revision="before",
                         task=dict(id="old-task", request="Old request", status="complete"),
                         messages=[dict(kind="final", text="Old result"), dict(kind="commentary", text="old activity")],
                         activity=[dict(kind="tool", text="Used imagegen")],
                         artifacts=[dict(id="file1", name="result.md", path=str(self.output), kind="document")])
        self.reader.read.side_effect = lambda *args, **kwargs: deepcopy(self.data)
        self.service = ResultsService(self.env, self.api, self.reader)

    def api(self, args):
        self.calls.append(args)
        if args[:2] == ["pane", "get"]:
            return {"pane": deepcopy(self.pane)}
        if args[:2] == ["agent", "prompt"]:
            return {"type": "ok"}
        raise AssertionError(args)

    def prompts(self):
        return [args for args in self.calls if args[:2] == ["agent", "prompt"]]

    def add_history(self):
        self.previous_output = self.root / "previous.md"
        self.previous_output.write_text("# Previous result", encoding="utf-8")
        self.data["history"] = [dict(
            key="opaque-history-key", state="complete",
            task=dict(id="prior-task", request=QUICK_PREFIX + "Previous request",
                      status="complete", timestamp="2026-09-27T13:00:00Z"),
            messages=[dict(kind="final", text="Previous result"),
                      dict(kind="commentary", text="previous commentary")],
            activity=[dict(kind="tool", text="Previous tool")],
            artifacts=[dict(id="previous-file", name="previous.md", path=str(self.previous_output), kind="document")],
        )]

    def test_open_and_refresh_only_read_selected_session_never_send(self):
        first = self.service.snapshot()
        second = self.service.snapshot()
        self.assertEqual(self.prompts(), [])
        self.reader.read.assert_called_with(SESSION, self.home, cwd=str(self.root), provider="codex")
        self.assertEqual(first["result"], "Old result")
        self.assertNotIn("old activity", first["result"])
        self.assertEqual(first["revision"], second["revision"])
        self.assertTrue(first["agent"]["supports_results"])

    def test_quick_and_project_requests_route_once_to_original_pane(self):
        self.service.send("Make an image", "quick")
        self.assertEqual(self.prompts(), [["agent", "prompt", "w9:p1", QUICK_PREFIX + "Make an image"]])
        self.data["task"]["id"] = "task-after-first-send"
        self.data["revision"] = "after-first-send"
        self.service.snapshot()
        self.service.send("Implement feature", "project")
        self.assertEqual(self.prompts()[-1], ["agent", "prompt", "w9:p1", "Implement feature"])
        self.assertEqual(len(self.prompts()), 2)

    def test_busy_blocked_or_unknown_agents_receive_no_prompt(self):
        for state in ("working", "blocked", "waiting", "unknown"):
            with self.subTest(state=state):
                self.pane["agent_status"] = state
                with self.assertRaises(ResultsError):
                    self.service.send("Do work")
        self.assertEqual(self.prompts(), [])

    def test_rebound_closed_or_changed_provider_pane_is_rejected(self):
        for key, replacement in (("agent_session", dict(value="different", agent="codex")),
                                  ("workspace_id", "w10"), ("cwd", str(self.root / "other")),
                                  ("pane_id", "w9:p2")):
            original = self.pane[key]
            self.pane[key] = replacement
            with self.subTest(key=key):
                with self.assertRaises(ResultsError):
                    self.service.send("Do work")
                with self.assertRaises(ResultsError):
                    self.service.snapshot()
            self.pane[key] = original
        self.assertEqual(self.prompts(), [])

    def test_unsupported_provider_results_and_input_are_honest(self):
        self.pane["agent"] = "claude"
        self.pane["agent_session"]["agent"] = "claude"
        self.data.update(provider_supported=False, state="unsupported", error="unsupported_provider", messages=[], artifacts=[])
        service = ResultsService(self.env, self.api, self.reader)
        snapshot = service.snapshot()
        self.assertFalse(snapshot["agent"]["supports_results"])
        self.assertFalse(snapshot["agent"]["can_send"])
        with self.assertRaises(ResultsError):
            service.send("Do work")
        self.assertEqual(self.prompts(), [])

    def test_send_rejection_does_not_retry_or_claim_success(self):
        self.service.snapshot()
        original = self.service.api

        def rejecting(args):
            if args[:2] == ["agent", "prompt"]:
                self.calls.append(args)
                raise ResultsError("rejected")
            return original(args)

        self.service.api = rejecting
        with self.assertRaises(ResultsError):
            self.service.send("Try once")
        self.assertEqual(len(self.prompts()), 1)
        self.assertEqual(self.service.snapshot()["result"], "Old result")

    def test_accepted_send_hides_stale_result_and_files_until_new_turn_arrives(self):
        self.service.snapshot()
        self.service.send("New image")
        waiting = self.service.snapshot()
        self.assertEqual(waiting["result"], "")
        self.assertEqual(waiting["files"], [])
        self.assertFalse(waiting["agent"]["can_send"])
        with self.assertRaises(ResultsError):
            self.service.file_action("file1", "preview")
        self.data.update(revision="started", state="running", messages=[], artifacts=[])
        self.data["task"].update(id="new-task", request=QUICK_PREFIX + "New image", status="running")
        started = self.service.snapshot()
        self.assertNotIn("Old result", started["result"])
        self.data.update(revision="finished", state="complete", messages=[dict(kind="final", text="New result")])
        self.data["task"]["status"] = "complete"
        finished = self.service.snapshot()
        self.assertEqual(finished["result"], "New result")

    def test_pending_request_rejects_second_send_and_immediately_revokes_old_files(self):
        self.service.snapshot()
        self.service.send("First request")
        with self.assertRaises(ResultsError) as error:
            self.service.send("Second request")
        self.assertIn("awaiting confirmation", error.exception.message)
        self.assertEqual(len(self.prompts()), 1)
        # No snapshot refresh is needed to remove the previous file action.
        with patch("bridge.output_action") as action:
            with self.assertRaises(ResultsError):
                self.service.file_action("file1", "preview")
        action.assert_not_called()

    def test_pending_request_does_not_show_old_commentary_as_new_activity(self):
        self.service.snapshot()
        self.service.send("Fresh request")
        waiting = self.service.snapshot()
        self.assertEqual(waiting["request"], "Fresh request")
        self.assertEqual(waiting["activity"], [])
        self.assertNotIn("old activity", json.dumps(waiting))

    def test_structured_running_state_overrides_stale_idle_detector(self):
        self.data["state"] = "running"
        snapshot = self.service.snapshot()
        self.assertEqual(snapshot["agent"]["state"], "working")
        self.assertFalse(snapshot["agent"]["can_send"])
        with self.assertRaises(ResultsError):
            self.service.send("Do not overlap")
        self.assertEqual(self.prompts(), [])

    def test_reader_error_or_unsupported_identity_prevents_send(self):
        for changes in ({"error": "session_identity_mismatch"}, {"provider_supported": False},
                        {"error": "history_schema_unknown", "state": "unavailable"}):
            original = deepcopy(self.data)
            self.data.update(changes)
            with self.subTest(changes=changes), self.assertRaises(ResultsError):
                self.service.send("Do not send")
            self.data = original
        self.assertEqual(self.prompts(), [])

    def test_readiness_is_rechecked_after_history_read_before_prompt(self):
        def read_then_busy(*args, **kwargs):
            self.pane["agent_status"] = "working"
            return deepcopy(self.data)

        self.reader.read.side_effect = read_then_busy
        with self.assertRaises(ResultsError) as error:
            self.service.send("Race with another user input")
        self.assertIn("no longer ready", error.exception.message)
        self.assertEqual(self.prompts(), [])

    def test_session_identity_is_rechecked_after_history_read_before_prompt(self):
        def read_then_rebind(*args, **kwargs):
            self.pane["agent_session"]["value"] = "another-session"
            return deepcopy(self.data)

        self.reader.read.side_effect = read_then_rebind
        with self.assertRaises(ResultsError):
            self.service.send("Do not send to replacement")
        self.assertEqual(self.prompts(), [])

    def test_progress_commentary_merges_chronologically_without_reasoning_or_tool_logs(self):
        self.data["activity"] = [dict(kind="tool", text="Used imagegen", timestamp="2026-09-27T14:00:01Z")]
        self.data["messages"] = [
            dict(kind="commentary", text="Preparing the image", timestamp="2026-09-27T14:00:00Z"),
            dict(kind="final", text="Image ready", timestamp="2026-09-27T14:00:02Z"),
            dict(kind="reasoning", text="hidden-reasoning", timestamp="2026-09-27T14:00:00Z"),
            dict(kind="tool_output", text="private-tool-stdout", timestamp="2026-09-27T14:00:01Z"),
        ]
        snapshot = self.service.snapshot()
        self.assertEqual(snapshot["result"], "Image ready")
        self.assertEqual([item["text"] for item in snapshot["activity"]], ["Preparing the image", "Used imagegen"])
        self.assertNotIn("hidden-reasoning", json.dumps(snapshot))
        self.assertNotIn("private-tool-stdout", json.dumps(snapshot))

    def test_error_interruption_and_blocked_prompts_have_visible_notices(self):
        self.data["activity"] = [dict(kind="error", text="The agent reported an error; see its terminal for details.")]
        self.assertIn("reported an error", self.service.snapshot()["agent"]["message"])
        self.data["state"] = "interrupted"
        self.assertIn("interrupted", self.service.snapshot()["agent"]["message"])
        self.pane["agent_status"] = "blocked"
        blocked = self.service.snapshot()["agent"]
        self.assertTrue(blocked["blocked"])
        self.assertFalse(blocked["can_send"])
        self.assertIn("needs input", blocked["message"])

    def test_quick_mode_internal_guidance_is_not_repeated_in_request_display(self):
        self.data["task"]["request"] = QUICK_PREFIX + "Make an image"
        self.assertEqual(self.service.snapshot()["request"], "Make an image")

    def test_invalid_inputs_never_send(self):
        for text, mode in (("", "quick"), ("  ", "quick"), ("x\0y", "quick"), ("x" * 16001, "project"),
                           ("request", "unknown")):
            with self.assertRaises(ResultsError):
                self.service.send(text, mode)
        self.assertEqual(self.prompts(), [])

    def test_file_action_requires_current_snapshot_id_and_verified_pane(self):
        with patch("bridge.output_action", return_value="opened") as action:
            with self.assertRaises(ResultsError):
                self.service.file_action("file1", "preview")
            self.service.snapshot()
            self.assertEqual(self.service.file_action("file1", "preview"), "opened")
            action.assert_called_once_with(self.output, "preview", self.root / "results/previews")
            self.pane["agent_session"]["value"] = "changed"
            with self.assertRaises(ResultsError):
                self.service.file_action("file1", "preview")
        self.assertEqual(self.prompts(), [])

    def test_history_selection_scopes_request_result_activity_and_files(self):
        self.add_history()
        latest = self.service.snapshot()
        self.assertEqual(latest["selected_turn"], None)
        self.assertEqual(latest["turns"], [dict(key="opaque-history-key", request="Previous request",
                                             timestamp="2026-09-27T13:00:00Z", state="complete")])
        self.service.select_turn("opaque-history-key")
        selected = self.service.snapshot()
        self.assertEqual(selected["selected_turn"], "opaque-history-key")
        self.assertEqual(selected["request"], "Previous request")
        self.assertEqual(selected["result"], "Previous result")
        self.assertEqual([item["text"] for item in selected["activity"]], ["Previous tool", "previous commentary"])
        self.assertEqual([item["id"] for item in selected["files"]], ["previous-file"])
        self.assertFalse(selected["agent"]["can_send"])
        self.assertNotIn("Old result", json.dumps(selected))
        self.assertNotIn(QUICK_PREFIX, json.dumps(selected))
        self.assertEqual(self.prompts(), [])

    def test_history_picker_lists_newest_first_without_exposing_transcript_data(self):
        self.add_history()
        newer = deepcopy(self.data["history"][0])
        newer.update(key="newer", messages=[dict(kind="final", text="Not a picker label")])
        newer["task"].update(request="Newer request", timestamp="2026-09-27T14:00:00Z")
        self.data["history"].append(newer)
        turns = self.service.snapshot()["turns"]
        self.assertEqual([turn["key"] for turn in turns], ["newer", "opaque-history-key"])
        self.assertNotIn("Not a picker label", json.dumps(turns))
        self.assertNotIn(str(self.previous_output), json.dumps(turns))

    def test_legacy_current_task_snapshot_has_empty_history_and_normal_status(self):
        latest = self.service.snapshot()
        self.assertEqual(latest["turns"], [])
        self.assertIsNone(latest["selected_turn"])
        self.assertFalse(latest["history_truncated"])
        self.assertEqual(latest["agent"]["status_code"], "done")
        self.assertEqual(latest["agent"]["status_label"], "Result ready")
        self.assertTrue(latest["agent"]["can_send"])

    def test_evicted_selected_history_falls_back_to_latest_with_notice_and_files(self):
        self.add_history()
        self.service.snapshot()
        self.service.select_turn("opaque-history-key")
        self.service.snapshot()
        self.data.update(history=[], truncated=True)
        result = self.service.snapshot()
        self.assertIsNone(result["selected_turn"])
        self.assertIsNone(self.service.selected_turn)
        self.assertEqual(result["result"], "Old result")
        self.assertEqual([item["id"] for item in result["files"]], ["file1"])
        self.assertIn("outside the retained history", result["history_notice"])
        self.assertTrue(result["history_truncated"])
        self.assertTrue(result["agent"]["can_send"])
        with patch("bridge.output_action") as action:
            with self.assertRaises(ResultsError):
                self.service.file_action("previous-file", "open")
        action.assert_not_called()

    def test_turn_selection_immediately_revokes_previous_files_before_next_snapshot(self):
        self.add_history()
        self.service.snapshot()
        self.service.select_turn("opaque-history-key")
        with patch("bridge.output_action") as action, patch("preview.preview") as preview:
            for file_id in ("file1", "previous-file"):
                with self.assertRaises(ResultsError):
                    self.service.file_action(file_id, "open")
                with self.assertRaises(ResultsError):
                    self.service.preview(file_id)
        action.assert_not_called()
        preview.assert_not_called()
        self.service.snapshot()
        self.service.select_turn(None)
        self.assertEqual(self.service.files, {})
        latest = self.service.snapshot()
        self.assertEqual(latest["result"], "Old result")
        self.assertTrue(latest["agent"]["can_send"])

    def test_history_file_actions_only_use_displayed_turn_outputs(self):
        self.add_history()
        self.service.snapshot()
        self.service.select_turn("opaque-history-key")
        self.service.snapshot()
        with patch("bridge.output_action", return_value="opened") as action:
            self.assertEqual(self.service.file_action("previous-file", "open"), "opened")
            action.assert_called_once_with(self.previous_output, "open", self.root / "results/previews")
            with self.assertRaises(ResultsError):
                self.service.file_action("file1", "open")
        self.assertEqual(self.prompts(), [])

    def test_history_view_cannot_send_even_when_current_agent_is_ready(self):
        self.add_history()
        self.service.snapshot()
        self.service.select_turn("opaque-history-key")
        with self.assertRaisesRegex(ResultsError, "Return to Latest task"):
            self.service.send("Do not send from old task")
        self.assertEqual(self.prompts(), [])
        self.service.select_turn(None)
        self.service.send("Send only from Latest", "project")
        self.assertEqual(self.prompts(), [["agent", "prompt", "w9:p1", "Send only from Latest"]])

    def test_history_selection_requires_retained_key_and_valid_session(self):
        self.add_history()
        with self.assertRaises(ResultsError):
            self.service.select_turn("opaque-history-key")
        self.service.snapshot()
        for invalid in ("prior-task", "../previous.md", str(self.previous_output)):
            with self.assertRaises(ResultsError):
                self.service.select_turn(invalid)
        self.pane["agent_session"]["value"] = "replaced-session"
        with self.assertRaises(ResultsError):
            self.service.select_turn("opaque-history-key")
        self.assertIsNone(self.service.selected_turn)

    def test_preview_delegates_only_selected_opaque_file_id_and_geometry(self):
        self.service.snapshot()
        content = dict(kind="text", body="# Result", pages=1, page=0)
        with patch("preview.preview", return_value=content) as preview:
            self.assertEqual(self.service.preview("file1", columns=55, rows=13, page=2), content)
            preview.assert_called_once_with(str(self.output), columns=55, rows=13, page=2)
            preview.reset_mock()
            for invalid in (str(self.output), "file:///C:/arbitrary.png", "../result.md", "unknown"):
                with self.assertRaises(ResultsError):
                    self.service.preview(invalid)
            preview.assert_not_called()

    def test_preview_revalidates_session_and_account_before_rendering(self):
        self.service.snapshot()
        with patch("preview.preview") as preview:
            self.pane["agent_session"]["value"] = "replaced-session"
            with self.assertRaises(ResultsError):
                self.service.preview("file1")
            self.pane["agent_session"]["value"] = SESSION
            self.service.env["CODEX_HOME"] = str(self.root / "another-account")
            with self.assertRaises(ResultsError):
                self.service.preview("file1")
        preview.assert_not_called()

    def test_preview_failure_becomes_user_notice_and_does_not_send(self):
        self.service.snapshot()
        with patch("preview.preview", side_effect=ValueError("Image preview is unavailable.")):
            with self.assertRaisesRegex(ResultsError, "Image preview is unavailable"):
                self.service.preview("file1")
        self.assertEqual(self.prompts(), [])

    def test_current_status_remains_live_while_viewing_completed_history(self):
        self.add_history()
        self.service.snapshot()
        self.service.select_turn("opaque-history-key")
        self.pane["agent_status"] = "working"
        self.data.update(state="running", waiting=dict(kind="agents", tool="collaboration.wait_agent"))
        selected = self.service.snapshot()
        self.assertEqual(selected["result"], "Previous result")
        self.assertEqual(selected["agent"]["status_code"], "waiting_agents")
        self.assertEqual(selected["agent"]["status_label"], "Waiting for agents")
        self.assertIn("another agent", selected["agent"]["status_detail"])
        self.assertFalse(selected["agent"]["can_send"])
        self.pane["agent_status"] = "blocked"
        self.assertEqual(self.service.snapshot()["agent"]["status_code"], "waiting_user")

    def test_status_fields_respect_cautious_idle_running_conflict(self):
        self.data.update(state="running", waiting=dict(kind="agents", tool="collaboration.wait_agent"))
        current = self.service.snapshot()["agent"]
        self.assertEqual(current["status_code"], "unknown")
        self.assertEqual(current["status_label"], "Status unknown")
        self.assertFalse(current["can_send"])

    def test_interrupted_and_unknown_record_states_never_accept_send(self):
        for state in ("interrupted", "unexpected", "unavailable"):
            self.data["state"] = state
            with self.subTest(state=state):
                self.assertFalse(self.service.snapshot()["agent"]["can_send"])
                with self.assertRaises(ResultsError):
                    self.service.send("Do not bypass disabled Send")
        self.assertEqual(self.prompts(), [])

    @unittest.skipUnless(os.name == "nt", "Windows Markdown output action pipeline")
    def test_markdown_slash_drive_output_reaches_real_file_actions(self):
        output = self.root / "Gün doğuşu resmi.png"
        output.write_bytes(b"synthetic-image-bytes")
        transcript = self.home / "sessions" / ("rollout-" + SESSION + ".jsonl")
        transcript.parent.mkdir()
        final = "[Gün doğuşu görselini indir](</" + output.as_posix() + ">)"
        events = [
            dict(type="session_meta", payload=dict(id=SESSION, cwd=str(self.root))),
            dict(type="event_msg", payload=dict(type="task_started", turn_id="image-task")),
            dict(type="response_item", payload=dict(type="message", role="assistant", phase="final_answer",
                 content=[dict(type="output_text", text=final)])),
            dict(type="event_msg", payload=dict(type="task_complete", turn_id="image-task")),
        ]
        transcript.write_text("".join(json.dumps(event) + "\n" for event in events), encoding="utf-8")
        service = ResultsService(self.env, self.api, CodexSessionReader())
        snapshot = service.snapshot()
        self.assertEqual(len(snapshot["files"]), 1)
        artifact = snapshot["files"][0]
        self.assertEqual(Path(artifact["path"]), output)
        # Exercise the actual reader -> service -> validation -> action path.
        # Only the external Windows applications are replaced with mocks.
        with patch("files.webbrowser.open", return_value=True) as browser, patch("files.subprocess.Popen") as process:
            for name in ("preview", "open"):
                self.assertEqual(service.file_action(artifact["id"], name), "Opened in browser.")
            previews = list((service.state / "previews").glob("*.html"))
            self.assertEqual(len(previews), 1)
            self.assertIn("data:image/png;base64,", previews[0].read_text(encoding="utf-8"))
            self.assertEqual(browser.call_count, 2)
            browser.assert_called_with(previews[0].as_uri(), new=2)
            self.assertEqual(service.file_action(artifact["id"], "show_folder"), "Opened containing folder.")
            process.assert_called_once_with([str(Path(os.environ["SystemRoot"]) / "explorer.exe"), "/select,", str(output)])
        self.assertEqual(self.prompts(), [])

    def test_preference_corruption_is_ignored_and_saved_preferences_are_allowlisted(self):
        self.service.state.mkdir()
        path = self.service.state / "view.json"
        for text in ("{invalid", "null", "[]", '{"tab":"unsafe"}'):
            path.write_text(text)
            self.assertEqual(self.service.load_preference(), "results")
        self.service.save_preference("activity")
        self.assertEqual(self.service.load_preference(), "activity")
        self.service.save_preference("../unsafe")
        self.assertEqual(self.service.load_preference(), "activity")

    def test_named_radio_home_is_verified_and_changed_binding_rejected(self):
        radio = self.root / "radio"
        radio.mkdir()
        db = radio / "radio.db"
        with closing(sqlite3.connect(db)) as connection:
            connection.execute("CREATE TABLE handles (session_ref TEXT,agent_session TEXT,agent TEXT,account TEXT)")
            connection.execute("CREATE TABLE accounts (name TEXT,provider TEXT,home TEXT,env TEXT)")
            connection.execute("INSERT INTO accounts VALUES (?,?,?,?)", ("work", "codex", str(self.home), "{}"))
            connection.execute("INSERT INTO handles VALUES (?,?,?,?)", ("herdr:w9:p1", SESSION, "codex", "work"))
            connection.commit()
        service = ResultsService({**self.env, "RADIO_HOME": str(radio)}, self.api, self.reader)
        self.assertEqual(service.home, self.home)
        with closing(sqlite3.connect(db)) as connection:
            connection.execute("UPDATE accounts SET home=?", (str(self.root / "other-account"),))
            connection.commit()
        with self.assertRaises(ResultsError):
            service.send("Do work")
        self.assertEqual(self.prompts(), [])

    def test_invalid_cli_context_cannot_invoke_arbitrary_executable(self):
        with patch("bridge.subprocess.run") as run:
            with self.assertRaises(ResultsError):
                bridge.cli(["agent", "prompt"], {"HERDR_BIN_PATH": str(self.output), "HERDR_SOCKET_PATH": str(self.root / "socket")})
        run.assert_not_called()


if __name__ == "__main__":
    unittest.main()
