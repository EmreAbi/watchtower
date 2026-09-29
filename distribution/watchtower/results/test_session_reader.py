"""Synthetic rollouts only: never read developer credentials or real transcripts."""

import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from session_reader import CodexSessionReader, _artifact_path


SESSION = "019fa455-1847-7e41-b28f-3cf4a0ab989b"
OTHER = "01a0d734-d773-7921-a72a-3c516e58d942"
STAMP = "2026-09-27T14:00:00Z"


def record(typ, **payload):
    return dict(type=typ, timestamp=STAMP, payload=payload)


def event(kind, **payload):
    return record("event_msg", type=kind, **payload)


def response(phase, text, role="assistant", **extra):
    return record("response_item", type="message", role=role, phase=phase,
                  content=[dict(type="output_text", text=text)], **extra)


def item(item_type, turn_id="turn-1", **values):
    return event("item_completed", turn_id=turn_id, item=dict(type=item_type, **values))


class ReaderTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        self.home = self.root / "account-one"
        self.cwd = self.root / "project"
        self.cwd.mkdir()
        self.path = self.home / "sessions/2026/09/27" / ("rollout-2026-09-27T14-00-00-" + SESSION + ".jsonl")
        self.path.parent.mkdir(parents=True)
        self.reader = CodexSessionReader()
        self.write()

    def write(self, *records, session_id=SESSION, cwd=None):
        meta = record("session_meta", id=session_id, cwd=str(cwd or self.cwd), cli_version="0.157.1",
                      base_instructions="private-instructions-never-returned")
        self.path.write_text("".join(json.dumps(value) + "\n" for value in (meta, *records)), encoding="utf-8")

    def append(self, *records):
        with self.path.open("a", encoding="utf-8") as stream:
            for value in records:
                stream.write(json.dumps(value) + "\n")

    def read(self, reader=None, **kwargs):
        return (reader or self.reader).read(SESSION, self.home, self.cwd, **kwargs)

    def test_final_commentary_and_tool_output_are_distinct(self):
        self.write(event("task_started", turn_id="turn-1"),
                   item("UserMessage", id="u1", content=[dict(type="text", text="Make a diagram")]),
                   response("commentary", "Preparing the diagram", id="a1"),
                   record("response_item", type="function_call", name="exec_command", call_id="call1", arguments="secret-command"),
                   record("response_item", type="function_call_output", call_id="call1", output="raw-tool-secret-log"),
                   record("response_item", type="reasoning", summary=[dict(text="hidden-reasoning")]),
                   item("Reasoning", raw_content="hidden-more-reasoning"),
                   response("analysis", "hidden-channel-reasoning", channel="analysis"),
                   response("final_answer", "Here is the diagram.", id="a2"),
                   event("task_complete", turn_id="turn-1", last_agent_message="Here is the diagram."))
        result = self.read()
        self.assertEqual(result["state"], "complete")
        self.assertEqual(result["task"]["request"], "Make a diagram")
        self.assertEqual([message["kind"] for message in result["messages"]], ["commentary", "final"])
        self.assertEqual(len(result["activity"]), 1)
        for forbidden in ("secret-command", "raw-tool-secret-log", "hidden-", "private-instructions"):
            self.assertNotIn(forbidden, json.dumps(result))

    def test_current_item_completed_and_response_duplicates_collapse(self):
        self.write(event("task_started", turn_id="turn-1"),
                   item("AgentMessage", id="a1", phase="commentary", content=[dict(type="Text", text="Working")]),
                   response("commentary", "Working", id="a1"),
                   item("AgentMessage", id="a2", phase="final_answer", content=[dict(type="Text", text="Finished")]),
                   response("final_answer", "Finished", id="a2"),
                   event("task_complete", last_agent_message="Finished", turn_id="turn-1"))
        result = self.read()
        self.assertEqual([m["text"] for m in result["messages"]], ["Working", "Finished"])

    def test_legacy_unknown_phase_is_final_only_after_explicit_completion(self):
        self.write(event("task_started", turn_id="turn-1"), event("user_message", message="Question"),
                   response(None, "Answer"))
        self.assertEqual(self.read()["messages"][0]["kind"], "commentary")
        self.append(event("task_complete", last_agent_message="Answer", turn_id="turn-1"))
        result = self.read()
        self.assertEqual([m["kind"] for m in result["messages"]], ["final"])

    def test_new_turn_clears_old_final_and_artifacts(self):
        self.write(event("task_started", turn_id="old"), response("final_answer", "[Old](C:/output/old.png)"),
                   event("task_complete", turn_id="old"))
        self.assertEqual(self.read()["state"], "complete")
        self.append(event("task_started", turn_id="new"), event("user_message", message="New request"))
        result = self.read()
        self.assertEqual(result["state"], "running")
        self.assertEqual(result["task"]["id"], "new")
        self.assertEqual(result["task"]["request"], "New request")
        self.assertEqual(result["messages"], [])
        self.assertEqual(result["artifacts"], [])

    def test_interrupted_turn_is_not_presented_as_complete(self):
        self.write(event("task_started", turn_id="turn-1"), response("commentary", "Started"), event("turn_aborted"))
        self.assertEqual(self.read()["state"], "interrupted")
        self.assertFalse(any(m["kind"] == "final" for m in self.read()["messages"]))

    def test_imagegen_and_multiple_markdown_artifacts(self):
        self.write(event("task_started", turn_id="turn-1"),
                   record("response_item", type="function_call", name="image_gen.imagegen", call_id="image1"),
                   record("response_item", type="function_call_output", call_id="image1", output=json.dumps({
                       "content": [{"type": "resource_link", "uri": "file:///C:/outputs/first.png", "name": "First"},
                                   {"type": "resource", "resource": {"uri": "file:///C:/outputs/second.jpg", "type": "resource"}},
                                   {"type": "text", "text": "![Third](C:/outputs/third.webp)"}]})),
                   response("final_answer", "![First](C:/outputs/first.png)\n[Report](<C:/output files/report.md>)\n"
                            "[Wrapped](<file:///C:/output%20files/\nrender.png>)"),
                   item("ImageGeneration", id="img2", status="completed", result="huge-base64-should-not-be-a-path",
                        saved_path="C:/outputs/native.png"))
        artifacts = self.read()["artifacts"]
        paths = {a["path"] for a in artifacts}
        self.assertEqual(paths, {"C:/outputs/first.png", "C:/outputs/second.jpg", "C:/outputs/third.webp",
                                "C:/output files/report.md", "C:/output files/render.png", "C:/outputs/native.png"})
        self.assertEqual(sum(a["kind"] == "image" for a in artifacts), 5)
        self.assertEqual(sum(a["path"] == "C:/outputs/first.png" for a in artifacts), 1)

    def test_relative_final_links_resolve_against_verified_cwd(self):
        self.write(event("task_started", turn_id="turn-1"), response("final_answer", "[Result](./output/result.md:12)"))
        self.assertEqual(self.read()["artifacts"][0]["path"], str((self.cwd / "output/result.md").resolve()))

    def test_markdown_slash_drive_links_preserve_absolute_path_and_deduplicate(self):
        expected = "C:/output files/Gün doğuşu.png"
        self.write(event("task_started", turn_id="turn-1"),
                   response("final_answer", "[Gün doğuşu](</C:/output files/Gün doğuşu.png>)\n"
                            "[Same file](<C:/output files/Gün doğuşu.png>)\n"
                            "[URI](file:///C:/output%20files/G%C3%BCn%20do%C4%9Fu%C5%9Fu.png)"))
        self.assertEqual([artifact["path"] for artifact in self.read()["artifacts"]], [expected])

    def test_slash_drive_normalization_does_not_allow_unsafe_local_references(self):
        for value in ("//server/share/output.png", "\\\\server\\share\\output.png",
                      "file://server/share/output.png", "C:relative.png",
                      "/C:/outputs/bad\x00.png", "/C:/outputs/bad\n.png"):
            with self.subTest(value=value):
                self.assertIsNone(_artifact_path(value, str(self.cwd)))

    def test_native_imagegen_extensions_keep_two_outputs_and_original_request(self):
        # Codex 0.157.1 persists image_gen.generation extensions with sizeable
        # base64 results. Keep the paths and request; never return image payloads.
        self.write(event("task_started", turn_id="image-task"),
                   item("UserMessage", turn_id="image-task", content=[dict(type="text", text="Generate sunset images")]),
                   item("Extension", id="first-image", kind="image_gen.generation", status="completed",
                        turn_id="image-task", result="privatebase64" * 210000, savedPath="C:/outputs/first.png"),
                   item("Extension", id="second-image", kind="image_gen.generation", status="completed",
                        turn_id="image-task", result="privatebase64" * 210000, savedPath="C:/outputs/second.png"),
                   record("response_item", type="custom_tool_call_output", call_id="i1", output=[
                       dict(type="input_image", image_url="data:image/png;base64," + "privatebase64" * 210000)]),
                   response("final_answer", "Done."), event("task_complete", turn_id="image-task"))
        result = self.read()
        self.assertEqual(result["task"]["request"], "Generate sunset images")
        self.assertEqual({a["path"] for a in result["artifacts"]}, {"C:/outputs/first.png", "C:/outputs/second.png"})
        self.assertFalse(result["truncated"])
        self.assertNotIn("privatebase64", json.dumps(result))

    def test_remote_images_are_references_only_no_embedded_payloads_or_unsafe_schemes(self):
        self.write(event("task_started", turn_id="turn-1"), item("ImageGeneration", id="i1", status="completed",
                   result="https://example.com/image/one?signature=abc"),
                   response("final_answer", "![Bad](javascript:run())\n![Bad](data:image/png;base64,private)\n"
                            "![Bad](file://server/share/a.png)\n![Good](https://example.com/a.png)"))
        paths = {item["path"] for item in self.read()["artifacts"]}
        self.assertEqual(paths, {"https://example.com/image/one?signature=abc", "https://example.com/a.png"})

    def test_generic_tool_logs_do_not_become_results_or_file_artifacts(self):
        self.write(event("task_started", turn_id="turn-1"),
                   record("response_item", type="function_call_output", call_id="unknown", output="C:/private/auth.json\nfinished"),
                   item("CommandExecution", id="c1", exit_code=1, stdout="private-output", command="private-command"))
        result = self.read()
        self.assertEqual(result["messages"], [])
        self.assertEqual(result["artifacts"], [])
        self.assertEqual(result["activity"][0]["text"], "Command failed")
        self.assertNotIn("private-", json.dumps(result))

    def test_identity_cwd_and_private_profile_boundaries(self):
        self.write(event("task_started", turn_id="turn-1"), response("final_answer", "private result"), session_id=OTHER)
        result = self.read()
        self.assertEqual(result["error"], "session_identity_mismatch")
        self.assertNotIn("private result", json.dumps(result))
        self.write(event("task_started", turn_id="turn-1"))
        self.assertEqual(self.reader.read(SESSION, self.home, self.root)["error"], "session_cwd_mismatch")
        another = self.root / "account-two"
        another.mkdir()
        self.assertEqual(self.reader.read(SESSION, another, self.cwd)["error"], "session_not_found")

    def test_same_id_in_two_rollouts_is_ambiguous_and_never_guessed(self):
        target = self.home / "archived_sessions" / self.path.name
        target.parent.mkdir()
        target.write_bytes(self.path.read_bytes())
        self.assertEqual(self.read()["error"], "ambiguous_session")

    def test_only_matching_transcript_is_opened_and_cache_is_incremental(self):
        other = self.path.parent / ("rollout-time-" + OTHER + ".jsonl")
        other.write_text("never-open")
        self.write(event("task_started", turn_id="turn-1"), response("commentary", "First"))
        original = Path.open
        opened = []

        def spy(path, *args, **kwargs):
            opened.append(path)
            self.assertNotEqual(path, other)
            return original(path, *args, **kwargs)

        with patch.object(Path, "open", spy):
            first = self.read()
            cached = self.read()
        self.assertEqual(opened, [self.path])
        self.assertEqual(first, cached)
        self.append(response("final_answer", "Second"), event("task_complete", turn_id="turn-1"))
        with patch.object(self.reader, "_find", side_effect=AssertionError("must reuse located session")):
            result = self.read()
        self.assertEqual([m["text"] for m in result["messages"]], ["First", "Second"])
        self.assertNotEqual(first["revision"], result["revision"])

    def test_partial_json_line_waits_for_completion_without_duplication(self):
        self.write(event("task_started", turn_id="turn-1"))
        line = json.dumps(response("final_answer", "Complete only once")) + "\n"
        with self.path.open("a", encoding="utf-8") as stream:
            stream.write(line[:31])
        partial = self.read()
        self.assertTrue(partial["truncated"])
        self.assertEqual(partial["messages"], [])
        with self.path.open("a", encoding="utf-8") as stream:
            stream.write(line[31:])
        completed = self.read()
        self.assertEqual(len(completed["messages"]), 1)
        self.assertEqual(completed["messages"][0]["text"], "Complete only once")

    def test_large_record_and_tail_read_are_bounded(self):
        reader = CodexSessionReader(max_read_bytes=4096, max_record_bytes=1024)
        self.write(event("task_started", turn_id="old"),
                   record("response_item", type="reasoning", encrypted_content="x" * 20000),
                   event("task_started", turn_id="new"), event("user_message", message="Current request"),
                   response("final_answer", "Current answer"), event("task_complete", turn_id="new"))
        result = self.read(reader)
        self.assertTrue(result["truncated"])
        self.assertEqual(result["state"], "complete")
        self.assertEqual(result["task"]["request"], "Current request")
        self.assertEqual([m["text"] for m in result["messages"]], ["Current answer"])

    def test_appended_oversized_record_is_discarded_across_reads(self):
        reader = CodexSessionReader(max_read_bytes=2048, max_record_bytes=1024)
        self.write(event("task_started", turn_id="turn-1"))
        self.read(reader)
        self.append(record("response_item", type="reasoning", encrypted_content="secret" * 1500),
                    response("final_answer", "Final after oversized record"))
        for _ in range(6):
            result = self.read(reader)
        self.assertEqual([m["text"] for m in result["messages"]], ["Final after oversized record"])
        self.assertNotIn("secret", json.dumps(result))

    def test_unknown_schema_is_unavailable_instead_of_fabricated_answer(self):
        self.write(record("future_output", final="not-supported-answer"))
        result = self.read()
        self.assertEqual(result["state"], "unavailable")
        self.assertEqual(result["error"], "history_schema_unknown")
        self.assertEqual(result["messages"], [])

    def test_invalid_session_and_unsupported_provider_never_search(self):
        with patch.object(self.reader, "_find", side_effect=AssertionError("must not search")):
            self.assertEqual(self.reader.read("../secret", self.home)["error"], "invalid_session_id")
            result = self.reader.read(SESSION, self.home, provider="claude")
            self.assertFalse(result["provider_supported"])
            self.assertEqual(result["state"], "unsupported")

    def test_lookup_has_entry_bound(self):
        self.assertEqual(self.read(CodexSessionReader(max_entries=1))["error"], "session_lookup_limit")

    def test_replaced_or_truncated_rollout_cannot_keep_stale_final(self):
        self.write(event("task_started", turn_id="old"), response("final_answer", "Old final"), event("task_complete"))
        self.read()
        self.write(event("task_started", turn_id="new"))
        result = self.read()
        self.assertEqual(result["state"], "running")
        self.assertEqual(result["messages"], [])

    def test_terminal_control_codes_are_removed_from_display(self):
        self.write(event("task_started", turn_id="one"), response("final_answer", "Safe\x1b[2J\x00 text"))
        self.assertEqual(self.read()["messages"][0]["text"], "Safe text")

    def test_history_is_detached_ordered_and_current_does_not_repeat_old_results(self):
        self.write(event("task_started", turn_id="old"), event("user_message", message="First request"),
                   response("final_answer", "[Old result](C:/deleted/already.png)"),
                   event("task_complete", turn_id="old"))
        first = self.read()
        self.append(event("task_started", turn_id="next"), event("user_message", message="Second request"))
        result = self.read()
        self.assertEqual(result["history"][0]["key"], first["key"])
        self.assertEqual(result["history"][0]["state"], "complete")
        self.assertEqual(result["history"][0]["artifacts"][0]["path"], "C:/deleted/already.png")
        self.assertEqual(set(result["history"][0]), {"key", "task", "state", "messages", "activity", "artifacts"})
        for field in ("messages", "artifacts", "activity"):
            self.assertEqual(result[field], [])
        result["history"][0]["messages"].clear()
        result["history"][0]["task"]["request"] = "Mutated by caller"
        untouched = self.read()
        self.assertTrue(untouched["history"][0]["messages"])
        self.assertEqual(untouched["history"][0]["task"]["request"], "First request")

    def test_duplicate_start_and_completion_do_not_duplicate_history(self):
        self.write(event("task_started", turn_id="one"), event("task_started", turn_id="one"),
                   response("final_answer", "Once"), event("task_complete", turn_id="one"),
                   event("task_complete", turn_id="one"), event("task_started", turn_id="one"),
                   event("task_started", turn_id="two"))
        result = self.read()
        self.assertEqual(len(result["history"]), 1)
        self.assertEqual(len(result["history"][0]["messages"]), 1)
        self.assertEqual(result["history"][0]["state"], "complete")

    def test_missing_turn_ids_same_timestamps_still_have_stable_distinct_keys(self):
        self.write(event("user_message", message="Repeat me"), response("final_answer", "First"),
                   event("task_complete"))
        first = self.read()
        self.append(event("user_message", message="Repeat me"))
        second = self.read()
        self.assertNotEqual(first["key"], second["key"])
        self.assertEqual(second["history"][0]["key"], first["key"])
        self.append(response("final_answer", "Second"), event("task_complete"))
        completed = self.read()
        self.assertEqual(completed["key"], second["key"])
        self.assertEqual(completed, self.read())
        self.assertEqual(self.read(CodexSessionReader())["key"], completed["key"])

    def test_followup_during_open_turn_archives_interrupted_without_old_wait(self):
        self.write(event("task_started", turn_id="one"), event("user_message", message="First"),
                   response("commentary", "Initial work"),
                   record("response_item", type="function_call", name="collaboration.wait_agent", call_id="c1"))
        self.assertEqual(self.read()["waiting"]["kind"], "agents")
        self.append(event("user_message", message="Changed request"),
                    response(None, "Changed request", role="user"),
                    event("task_complete", turn_id="one", last_agent_message="Old final after follow-up"))
        result = self.read()
        self.assertEqual(len(result["history"]), 1)
        self.assertEqual(result["history"][0]["state"], "interrupted")
        self.assertEqual(result["task"]["request"], "Changed request")
        self.assertEqual(result["messages"], [])
        self.assertIsNone(result["waiting"])
        self.assertEqual(result["state"], "running")
        self.append(event("turn_aborted"), event("user_message", message="After interruption"))
        result = self.read()
        self.assertEqual([turn["state"] for turn in result["history"]], ["interrupted", "interrupted"])

    def test_history_count_bound_and_opt_out(self):
        events = []
        for i in range(25):
            events += [event("task_started", turn_id=str(i)), event("user_message", message="Request " + str(i)),
                       response("final_answer", "Answer " + str(i)), event("task_complete", turn_id=str(i))]
        self.write(*events)
        default = self.read()
        self.assertEqual(len(default["history"]), 20)
        self.assertEqual([turn["task"]["id"] for turn in default["history"]], list(map(str, range(4, 24))))
        self.assertTrue(default["truncated"])
        bounded = self.read(CodexSessionReader(max_turns=3))
        self.assertEqual([turn["task"]["id"] for turn in bounded["history"]], ["21", "22", "23"])
        self.assertEqual(self.read(CodexSessionReader(max_turns=0))["history"], [])

    def test_total_history_text_bound_even_before_turn_count_limit(self):
        events = []
        for i in range(6):
            events.append(event("task_started", turn_id=str(i)))
            events.extend(response("commentary", str(j) + "x" * 65520) for j in range(8))
            events.append(event("task_complete", turn_id=str(i)))
        self.write(*events)
        result = self.read()
        retained = sum(len(m["text"]) for turn in result["history"] for m in turn["messages"])
        self.assertLessEqual(retained, 2 * 1024 * 1024)
        self.assertLess(len(result["history"]), 5)
        self.assertTrue(result["truncated"])
        self.assertEqual(len(result["messages"]), 8)

    def test_only_exact_outstanding_tools_prove_waiting(self):
        self.write(event("task_started", turn_id="one"),
                   response("commentary", "Waiting for your input and other agents"),
                   record("response_item", type="function_call", name="custom.request_user_input", call_id="wrong"),
                   record("response_item", type="function_call", name="exec_command", call_id="shell",
                          arguments="collaboration.wait_agent"))
        self.assertIsNone(self.read()["waiting"])
        self.append(record("response_item", type="function_call", name="collaboration.wait_agent", call_id="agent"))
        self.assertEqual(self.read()["waiting"], dict(kind="agents", tool="collaboration.wait_agent"))
        self.append(record("response_item", type="custom_tool_call", name="functions.request_user_input", call_id="user",
                           input="private question argument"))
        result = self.read()
        self.assertEqual(result["waiting"], dict(kind="user", tool="functions.request_user_input"))
        self.assertNotIn("private question argument", json.dumps(result))
        self.append(record("response_item", type="custom_tool_call_output", call_id="user", output="private answer"))
        self.assertEqual(self.read()["waiting"]["kind"], "agents")
        self.append(record("response_item", type="function_call_output", call_id="agent", output="done"))
        self.assertIsNone(self.read()["waiting"])
        self.append(record("response_item", type="function_call", name="collaboration.wait_agent", call_id="agent"))
        self.assertIsNone(self.read()["waiting"], "A mirrored old start must not resurrect a finished call")

    def test_waiting_clears_on_abort_completion_and_new_turn(self):
        for ending in (event("turn_aborted", turn_id="one"), event("task_complete", turn_id="one"),
                       event("task_started", turn_id="two")):
            with self.subTest(ending=ending):
                self.write(event("task_started", turn_id="one"),
                           record("response_item", type="function_call", name="functions.request_user_input", call_id="user"))
                self.assertEqual(self.read()["waiting"]["kind"], "user")
                self.append(ending)
                self.assertIsNone(self.read()["waiting"])

    def test_typed_tool_status_and_late_prior_turn_events_are_scoped(self):
        self.write(event("task_started", turn_id="one"),
                   event("item_started", turn_id="one", item=dict(type="DynamicToolCall", id="c1",
                                                                  tool="collaboration.wait_agents", status="inProgress")))
        self.assertEqual(self.read()["waiting"]["kind"], "agents")
        self.append(event("item_completed", turn_id="one", item=dict(type="DynamicToolCall", id="c1",
                                                                     tool="collaboration.wait_agents", status="completed")))
        self.assertIsNone(self.read()["waiting"])
        self.append(event("task_started", turn_id="two"),
                    event("item_started", turn_id="one", item=dict(type="DynamicToolCall", id="old",
                                                                   tool="request_user_input", status="inProgress")),
                    event("task_complete", turn_id="one", last_agent_message="Stale private final"))
        result = self.read()
        self.assertIsNone(result["waiting"])
        self.assertEqual(result["state"], "running")
        self.assertEqual(result["messages"], [])
        self.assertNotIn("Stale private final", json.dumps(result))

    def test_waiting_after_long_tool_sequence_is_not_lost(self):
        self.write(event("task_started", turn_id="one"))
        for i in range(300):
            self.append(record("response_item", type="function_call", name="exec_command", call_id=str(i)),
                        record("response_item", type="function_call_output", call_id=str(i), output="done"))
        self.append(record("response_item", type="function_call", name="request_user_input", call_id="user"))
        self.assertEqual(self.read()["waiting"]["kind"], "user")

    def test_new_anonymous_start_clears_answer_even_before_user_line_arrives(self):
        self.write(event("task_started"), event("user_message", message="Earlier"), response("final_answer", "Old result"))
        previous = self.read()
        self.append(event("task_started"))
        result = self.read()
        self.assertNotEqual(result["key"], previous["key"])
        self.assertEqual(result["messages"], [])
        self.assertEqual(result["history"][0]["messages"][0]["text"], "Old result")

    def test_typed_user_followup_establishes_turn_when_start_event_is_absent(self):
        self.write(event("task_started", turn_id="old"), event("user_message", message="Earlier request"),
                   item("AgentMessage", turn_id="old", phase="final_answer", text="Earlier result"),
                   event("task_complete", turn_id="old"))
        earlier = self.read()
        self.append(item("UserMessage", turn_id="new", content=[dict(type="text", text="New request")]))
        started = self.read()
        self.assertEqual(started["task"]["id"], "new")
        self.assertEqual(started["task"]["request"], "New request")
        self.assertEqual(started["messages"], [])
        self.assertEqual(started["history"][0]["key"], earlier["key"])
        self.append(item("AgentMessage", turn_id="new", phase="final_answer", text="New result"),
                    event("task_complete", turn_id="new"),
                    item("UserMessage", turn_id="old", content=[dict(type="text", text="Late old request")]))
        final = self.read()
        self.assertEqual(final["task"]["request"], "New request")
        self.assertEqual([message["text"] for message in final["messages"]], ["New result"])
        self.assertEqual(final["state"], "complete")

    def test_same_turn_user_answer_keeps_request_wait_identity_and_completion(self):
        self.write(event("task_started", turn_id="one"),
                   item("UserMessage", turn_id="one", content=[dict(type="text", text="Choose a colour")]),
                   record("response_item", type="function_call", name="request_user_input", call_id="question"))
        first = self.read()
        self.append(item("UserMessage", turn_id="one", content=[dict(type="text", text="Use blue")]))
        answered = self.read()
        self.assertEqual(answered["key"], first["key"])
        self.assertEqual(answered["task"]["request"], "Choose a colour")
        self.assertEqual(answered["history"], [])
        # Only the matching tool output proves the question was answered.
        self.assertEqual(answered["waiting"]["kind"], "user")
        self.append(record("response_item", type="function_call_output", call_id="question", output="blue"),
                    item("AgentMessage", turn_id="one", phase="final_answer", text="Blue selected"),
                    event("task_complete", turn_id="one"))
        final = self.read()
        self.assertEqual(final["state"], "complete")
        self.assertEqual(final["key"], first["key"])
        self.assertEqual(final["history"], [])
        self.assertEqual([message["text"] for message in final["messages"]], ["Blue selected"])
        self.assertIsNone(final["waiting"])

    def test_model_input_context_and_user_mirrors_cannot_split_typed_turns(self):
        self.write(event("task_started", turn_id="first"),
                   response(None, "private injected model context", role="user"),
                   response(None, "Actual first request", role="user"),
                   item("UserMessage", turn_id="first", content=[dict(type="text", text="Actual first request")]),
                   response(None, "Same-turn steering text", role="user"),
                   item("UserMessage", turn_id="first", content=[dict(type="text", text="Same-turn steering text")]),
                   item("AgentMessage", turn_id="first", phase="final_answer", text="First result"),
                   event("task_complete", turn_id="first"))
        first = self.read()
        self.assertEqual(first["state"], "complete")
        self.assertEqual(first["task"]["id"], "first")
        self.assertEqual(first["task"]["request"], "Actual first request")
        self.assertEqual(first["history"], [])
        self.assertNotIn("private injected", json.dumps(first))
        self.assertNotIn("Same-turn steering", json.dumps(first))
        self.append(event("task_started", turn_id="second"),
                    response(None, "Actual second request", role="user"),
                    item("UserMessage", turn_id="second", content=[dict(type="text", text="Actual second request")]),
                    item("AgentMessage", turn_id="second", phase="final_answer", text="Second result"),
                    response("final_answer", "Second result"), event("task_complete", turn_id="second"))
        second = self.read()
        self.assertEqual(second["state"], "complete")
        self.assertEqual(second["task"]["id"], "second")
        self.assertEqual(second["task"]["request"], "Actual second request")
        self.assertEqual([m["text"] for m in second["messages"]], ["Second result"])
        self.assertEqual(len(second["history"]), 1)
        self.assertEqual(second["history"][0]["key"], first["key"])
        self.assertEqual(second["history"][0]["state"], "complete")

    def test_partial_tail_implicit_current_has_stable_key_and_no_fabricated_history(self):
        reader = CodexSessionReader(max_read_bytes=2048)
        self.write(event("task_started", turn_id="one"), event("user_message", message="Outside tail"),
                   record("response_item", type="reasoning", encrypted_content="private" * 1000),
                   response("commentary", "Retained activity"))
        partial = self.read(reader)
        self.assertEqual(partial["history"], [])
        self.assertTrue(partial["key"])
        self.append(response("final_answer", "Current final"), event("task_complete", turn_id="one"))
        completed = self.read(reader)
        self.assertEqual(completed["key"], partial["key"])
        self.assertEqual(completed["task"]["id"], "one")
        self.assertNotIn("private", json.dumps(completed))


if __name__ == "__main__":
    unittest.main()
