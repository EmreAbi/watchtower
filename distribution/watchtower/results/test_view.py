"""Synthetic Results UI tests. No provider, live agent, file opener or network."""

from __future__ import annotations

import asyncio
import copy
import sys
import threading
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parent))

from textual.widgets import Button, DataTable, Markdown, Select, Static, TabbedContent, TextArea

from view import ResultsView, activity_text, safe_text


def snapshot():
    return {
        "agent": {"label": "sandbox-review", "provider": "Codex", "state": "done",
                  "supports_results": True, "supports_input": True, "can_send": True,
                  "blocked": False, "message": ""},
        "request": "Compare the two examples.",
        "result": "## Result\n\nThe second example is clearer.\n\n- Fewer dependencies\n- Clearer ownership",
        "activity": [{"kind": "tool", "text": "Read two source files", "timestamp": "14:10"},
                     {"kind": "commentary", "text": "Checking the examples", "timestamp": "14:11"}],
        "files": [{"id": "file-1", "name": "comparison.md", "path": "C:/demo/comparison.md", "kind": "document"},
                  {"id": "file-2", "name": "diagram.png", "path": "C:/demo/diagram.png", "kind": "image"}],
        "revision": "1",
    }


class FakeService:
    def __init__(self, data=None, preference=None):
        self.data = data or snapshot()
        self.calls = []
        self.preference = preference
        self.send_error = None
        self.block_send = False
        self.started = threading.Event()
        self.release = threading.Event()

    def snapshot(self):
        self.calls.append(("snapshot",))
        return copy.deepcopy(self.data)

    def load_preference(self):
        self.calls.append(("load_preference",))
        return self.preference

    def save_preference(self, tab):
        self.calls.append(("save_preference", tab))
        self.preference = tab

    def send(self, text, mode="quick"):
        self.calls.append(("send", text, mode))
        self.started.set()
        if self.block_send:
            self.release.wait(3)
        if self.send_error:
            raise self.send_error
        self.data["agent"]["state"] = "working"
        self.data["agent"]["can_send"] = False
        self.data["request"] = text
        self.data["result"] = ""
        self.data["revision"] = "2"
        return {"message": "Sent to existing agent."}

    def file_action(self, file_id, action):
        self.calls.append(("file_action", file_id, action))
        return {"message": "File action finished."}

    def preview(self, file_id, **options):
        self.calls.append(("preview", file_id))
        return dict(kind="text", title="Preview", body="Rendered local file", page=0, pages=1, details="Local text")

    def select_turn(self, key):
        self.calls.append(("select_turn", key))
        self.data["selected_turn"] = key
        self.data["request"] = "Previous question" if key else "Current question"
        self.data["result"] = "Previous answer" if key else "Current answer"


class FormattingTests(unittest.TestCase):
    def test_control_characters_removed_but_multiline_literal_text_retained(self):
        self.assertEqual(safe_text("[red]a[/red]\n\t b\x1b\x00"), "[red]a[/red]\n\t b")
        rendered = activity_text([{"kind": "tool", "text": "[red]literal[/red]", "timestamp": "14:20"}])
        self.assertIn("[red]literal[/red]", rendered.plain)
        self.assertIn("Tool · 14:20", rendered.plain)


class ResultsViewTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        asyncio.get_running_loop().set_debug(False)

    async def test_results_are_default_and_opening_only_reads_existing_session(self):
        service = FakeService()
        app = ResultsView(service)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            self.assertEqual(app.query_one("#tabs", TabbedContent).active, "results")
            self.assertEqual(app._result, service.data["result"])
            self.assertNotIn("Read two source files", app._result)
            self.assertIn("Compare the two examples", str(app.query_one("#request", Static).render()))
            self.assertIn("Read two source files", str(app.query_one("#activity-content", Static).render()))
            self.assertEqual(app.query_one("#output-table", DataTable).row_count, 2)
            self.assertIn("(2)", str(app.query_one("#tabs", TabbedContent).get_tab("outputs").label))
            self.assertTrue(all(call[0] in {"snapshot", "load_preference", "save_preference"} for call in service.calls))

    async def test_non_agent_pane_opens_friendly_read_only_fallback(self):
        from bridge import ResultsError
        message = "This pane has no saved agent session. Start an agent first."
        with patch("bridge.ResultsService", side_effect=ResultsError(message)) as factory:
            app = ResultsView()
        factory.assert_called_once()
        async with app.run_test(size=(70, 36)) as pilot:
            await pilot.pause()
            self.assertIn(message, str(app.query_one("#attention", Static).render()))
            self.assertFalse(app.query_one("#composer").display)
            self.assertFalse(app.query_one("#terminal", Button).disabled)
            self.assertTrue(all(button.disabled for button in app.query("#file-actions Button")))

    async def test_initialization_failure_never_displays_raw_exception(self):
        with patch("bridge.ResultsService", side_effect=RuntimeError("private-initialization-detail")):
            app = ResultsView()
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            notice = str(app.query_one("#attention", Static).render())
            self.assertIn("Results could not open", notice)
            self.assertNotIn("private-initialization-detail", notice)
            self.assertFalse(app.query_one("#terminal", Button).disabled)

    async def test_new_working_turn_clears_old_result_and_disables_send(self):
        service = FakeService()
        app = ResultsView(service)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            service.data.update(result="", request="Now check a new example.", revision="2")
            service.data["agent"].update(state="working", can_send=False)
            await app.refresh_snapshot().wait()
            self.assertEqual(app._result, "")
            self.assertFalse(app.query_one("#result", Markdown).display)
            self.assertIn("Working on your request", str(app.query_one("#result-empty", Static).render()))
            self.assertTrue(app.query_one("#composer").display)
            self.assertTrue(app.query_one("#prompt", TextArea).disabled)
            self.assertTrue(app.query_one("#send", Button).disabled)

    async def test_important_blocked_prompt_is_visible_outside_activity(self):
        data = snapshot()
        data["agent"].update(state="blocked", blocked=True, message="Permission needed: review this command in Terminal.")
        app = ResultsView(FakeService(data))
        async with app.run_test(size=(70, 36)) as pilot:
            await pilot.pause()
            self.assertTrue(app.query_one("#attention").display)
            self.assertIn("Permission needed", str(app.query_one("#attention", Static).render()))
            self.assertEqual(app.query_one("#tabs", TabbedContent).active, "results")
            self.assertTrue(app.query_one("#send", Button).disabled)
            self.assertFalse(app.query_one("#terminal", Button).disabled)

    async def test_unsupported_tool_hides_composer_and_points_to_terminal(self):
        data = snapshot()
        data["agent"].update(provider="Other CLI", supports_results=False, supports_input=False)
        data["request"], data["result"] = "", ""
        app = ResultsView(FakeService(data))
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            self.assertFalse(app.query_one("#composer").display)
            self.assertIn("not available for this tool", str(app.query_one("#result-empty", Static).render()))
            self.assertFalse(app.query_one("#terminal", Button).disabled)

    async def test_send_is_explicit_preserves_exact_multiline_text_and_existing_target(self):
        service = FakeService()
        app = ResultsView(service)
        text = "  First line\nSecond line with `code`\n"
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            editor = app.query_one("#prompt", TextArea)
            editor.load_text(text)
            editor.focus()
            await pilot.pause()
            self.assertFalse(any(call[0] == "send" for call in service.calls))
            await pilot.click("#send")
            await pilot.pause()
            await app.workers.wait_for_complete()
            self.assertEqual([call for call in service.calls if call[0] == "send"], [("send", text, "quick")])
            self.assertEqual(editor.text, "")
            self.assertTrue(app.query_one("#send", Button).disabled)

    async def test_project_mode_is_explicit_and_passed_without_changing_user_text(self):
        service = FakeService()
        app = ResultsView(service)
        async with app.run_test(size=(70, 36)) as pilot:
            await pilot.pause()
            app.query_one("#request-mode", Select).value = "project"
            app.query_one("#prompt", TextArea).load_text("  Implement this change.\n")
            await pilot.pause()
            self.assertIn("normal workflow", str(app.query_one("#compose-hint", Static).render()))
            self.assertFalse(any(call[0] == "send" for call in service.calls))
            await pilot.click("#send")
            await pilot.pause()
            self.assertIn(("send", "  Implement this change.\n", "project"), service.calls)

    async def test_enter_adds_newline_without_sending(self):
        service = FakeService()
        app = ResultsView(service)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            editor = app.query_one("#prompt", TextArea)
            editor.focus()
            await pilot.press("a", "enter", "b")
            await pilot.pause()
            self.assertEqual(editor.text, "a\nb")
            self.assertFalse(any(call[0] == "send" for call in service.calls))

    async def test_send_failure_keeps_draft_and_sanitizes_error(self):
        service = FakeService()
        service.send_error = RuntimeError("private-server-diagnostic")
        app = ResultsView(service)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            app.query_one("#prompt", TextArea).load_text("Keep my draft")
            await pilot.pause()
            await app.send_prompt().wait()
            notice = str(app.query_one("#notice", Static).render())
            self.assertNotIn("private-server-diagnostic", notice)
            self.assertIn("Delivery is unconfirmed", notice)
            self.assertEqual(app.query_one("#prompt", TextArea).text, "Keep my draft")

    async def test_busy_send_blocks_duplicate_and_popup_close(self):
        service = FakeService()
        service.block_send = True
        app = ResultsView(service)
        try:
            async with app.run_test(size=(120, 40)) as pilot:
                await pilot.pause()
                app.query_one("#prompt", TextArea).load_text("Run one task")
                await pilot.pause()
                first = app.send_prompt()
                await pilot.pause()
                self.assertTrue(service.started.is_set())
                await app.send_prompt().wait()
                with patch.object(app, "exit") as exit_app:
                    app.action_terminal()
                    exit_app.assert_not_called()
                self.assertEqual(sum(call[0] == "send" for call in service.calls), 1)
                service.release.set()
                await first.wait()
        finally:
            service.release.set()

    async def test_file_actions_use_only_selected_opaque_id(self):
        service = FakeService()
        app = ResultsView(service)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            app.query_one("#tabs", TabbedContent).active = "outputs"
            app.query_one("#output-table", DataTable).move_cursor(row=1, animate=False)
            await pilot.pause()
            for action in ("open", "save_as", "show_folder", "copy_path"):
                await app.perform_file_action(action).wait()
                self.assertIn(("file_action", "file-2", action), service.calls)
            await app.perform_file_action("delete").wait()
            self.assertNotIn(("file_action", "file-2", "delete"), service.calls)

    async def test_file_action_failure_is_visible_and_next_success_clears_error(self):
        from bridge import ResultsError
        service = FakeService()
        app = ResultsView(service)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            app.query_one("#tabs", TabbedContent).active = "outputs"
            await pilot.pause()
            with patch.object(service, "file_action", side_effect=ResultsError("The output file is unavailable.")), \
                    patch.object(app, "notify") as toast:
                await pilot.click("#file-open")
                await app.workers.wait_for_complete()
                notice = app.query_one("#notice", Static)
                self.assertTrue(notice.has_class("error"))
                self.assertIn("output file is unavailable", str(notice.render()))
                toast.assert_called_once_with("The output file is unavailable.",
                    title="Action could not finish", severity="error", timeout=6)
            await app.perform_file_action("open").wait()
            self.assertFalse(notice.has_class("error"))
            self.assertIn("File action finished", str(notice.render()))

    async def test_history_switch_is_read_only_keeps_draft_and_returns_to_latest(self):
        service = FakeService()
        service.data["turns"] = [dict(key="turn-old", request="Previous question", timestamp="2026-09-27T14:00:00Z", state="complete")]
        app = ResultsView(service)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            editor = app.query_one("#prompt", TextArea)
            editor.load_text("My unsent draft")
            app.query_one("#task-history", Select).value = "turn-old"
            await pilot.pause()
            await app.workers.wait_for_complete()
            self.assertEqual(app._result, "Previous answer")
            self.assertFalse(app.query_one("#composer").display)
            self.assertTrue(app.query_one("#send", Button).disabled)
            self.assertIn("READ ONLY", str(app.query_one("#request-label", Static).render()))
            self.assertEqual(editor.text, "My unsent draft")
            self.assertFalse(any(c[0] == "send" for c in service.calls))
            app.query_one("#task-history", Select).value = "latest"
            await pilot.pause()
            await app.workers.wait_for_complete()
            self.assertTrue(app.query_one("#composer").display)
            self.assertEqual(app._result, "Current answer")
            self.assertEqual(editor.text, "My unsent draft")
            self.assertEqual([c for c in service.calls if c[0] == "select_turn"], [("select_turn", "turn-old"), ("select_turn", None)])

    async def test_inline_preview_is_lazy_and_preview_button_stays_inside_results(self):
        from preview_view import PreviewScreen
        service = FakeService()
        app = ResultsView(service)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            self.assertFalse(any(c[0] == "preview" for c in service.calls))
            app.query_one("#tabs", TabbedContent).active = "outputs"
            await pilot.pause(0.25)  # Preview waits for the newly visible tab's layout.
            await app.workers.wait_for_complete()
            self.assertIn(("preview", "file-1"), service.calls)
            await pilot.click("#file-preview")
            await pilot.pause()
            self.assertIsInstance(app.screen, PreviewScreen)
            self.assertFalse(any(c[0] == "file_action" for c in service.calls))
            await pilot.press("escape")
            await pilot.pause()
            self.assertNotIsInstance(app.screen, PreviewScreen)
            self.assertTrue(app.is_running)

    async def test_current_agent_status_remains_visible_while_reading_history(self):
        service = FakeService()
        service.data["agent"].update(status_code="waiting_agents", status_label="Waiting for agents", status_detail="The current task is waiting for another agent's response.")
        app = ResultsView(service)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            self.assertIn("Waiting for agents", str(app.query_one("#agent-state", Static).render()))

    async def test_resize_during_preview_updates_underlying_layout(self):
        app = ResultsView(FakeService())
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            app.query_one("#tabs", TabbedContent).active = "outputs"
            await pilot.pause()
            await app.perform_file_action("preview").wait()
            await pilot.pause()
            await pilot.resize_terminal(81, 25)
            await pilot.pause()
            await pilot.press("escape")
            await pilot.pause()
            self.assertTrue(app.default_screen.has_class("compact"))
            self.assertFalse(app.query_one("#inline-preview").display)
            self.assertLessEqual(app.query_one("#file-copy_path").region.bottom, 25)

    async def test_new_latest_turn_refreshes_same_path_thumbnail(self):
        from preview_view import PreviewPanel
        service = FakeService()
        service.data["display_key"] = "one"
        app = ResultsView(service)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            app.query_one("#tabs", TabbedContent).active = "outputs"
            await pilot.pause(0.25)
            await app.workers.wait_for_complete()
            count = sum(c[0] == "preview" for c in service.calls)
            self.assertGreater(count, 0)
            service.data["display_key"] = "two"
            await app.refresh_snapshot().wait()
            await pilot.pause()
            await app.workers.wait_for_complete()
            self.assertGreater(sum(c[0] == "preview" for c in service.calls), count)
            self.assertEqual(app.query_one("#inline-preview", PreviewPanel).file_id, "file-1")

    async def test_truncated_history_notice_is_visible_without_retained_previous_turns(self):
        service = FakeService()
        service.data.update(history_truncated=True, turns=[])
        app = ResultsView(service)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            self.assertTrue(app.query_one("#history-row").display)
            self.assertIn("omitted", str(app.query_one("#history-hint", Static).render()))

    async def test_failed_history_selection_keeps_picker_and_answer_consistent(self):
        from bridge import ResultsError
        service = FakeService()
        service.data["turns"] = [dict(key="old", request="Older question")]
        app = ResultsView(service)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            answer = app._result
            with patch.object(service, "select_turn", side_effect=ResultsError("This task is no longer available.")):
                app.query_one("#task-history", Select).value = "old"
                await pilot.pause()
                await app.workers.wait_for_complete()
            self.assertEqual(app.query_one("#task-history", Select).value, "latest")
            self.assertEqual(app._result, answer)
            self.assertIn("no longer available", str(app.query_one("#notice", Static).render()))

    async def test_modal_holds_file_binding_until_closed_then_refreshes(self):
        from preview_view import PreviewScreen
        service = FakeService()
        app = ResultsView(service)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            await app.perform_file_action("preview").wait()
            await pilot.pause()
            reads = sum(c[0] == "snapshot" for c in service.calls)
            service.data["result"] = "New completed result"
            await app.refresh_snapshot().wait()
            self.assertEqual(sum(c[0] == "snapshot" for c in service.calls), reads)
            self.assertIsInstance(app.screen, PreviewScreen)
            await pilot.press("escape")
            await pilot.pause()
            await app.workers.wait_for_complete()
            self.assertEqual(app._result, "New completed result")

    async def test_result_links_show_output_guidance_without_opening_untrusted_url(self):
        service = FakeService()
        app = ResultsView(service)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            with patch.object(app, "open_url") as open_url:
                for link in ("file:///C:/demo/diagram.png", "https://example.com/report", "file:///C:/secret/other.md", "../other.md"):
                    app.query_one("#result", Markdown).post_message(
                        Markdown.LinkClicked(app.query_one("#result", Markdown), link))
                    await pilot.pause()
                    self.assertIn("Use Outputs", str(app.query_one("#notice", Static).render()))
                open_url.assert_not_called()
            self.assertFalse(any(call[0] == "file_action" for call in service.calls))

    async def test_disappearing_file_disables_actions_without_opening_anything(self):
        service = FakeService()
        app = ResultsView(service)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            await app.workers.wait_for_complete()
            service.data["files"] = []
            await app.refresh_snapshot().wait()
            await pilot.pause()
            self.assertTrue(all(button.disabled for button in app.query("#file-actions Button")))
            await app.perform_file_action("open").wait()
            self.assertFalse(any(call[0] == "file_action" for call in service.calls))

    async def test_tab_preference_restored_and_saved_without_session_mutation(self):
        service = FakeService(preference="activity")
        app = ResultsView(service)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            self.assertEqual(app.query_one("#tabs", TabbedContent).active, "activity")
            app.query_one("#tabs", TabbedContent).active = "outputs"
            await pilot.pause()
            await app.workers.wait_for_complete()
            self.assertEqual(service.preference, "outputs")
            self.assertFalse(any(call[0] in {"send", "file_action"} for call in service.calls))

    async def test_unchanged_snapshot_does_not_rebuild_markdown(self):
        app = ResultsView(FakeService())
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            with patch.object(app.query_one("#result", Markdown), "update") as update, \
                    patch.object(app, "apply_snapshot") as apply_snapshot:
                await app.refresh_snapshot().wait()
                update.assert_not_called()
                apply_snapshot.assert_not_called()

    async def test_narrow_layout_keeps_actions_and_composer_accessible(self):
        for width, height in ((70, 36), (81, 25)):
            with self.subTest(width=width, height=height):
                app = ResultsView(FakeService())
                async with app.run_test(size=(width, height)) as pilot:
                    await pilot.pause()
                    app.query_one("#prompt", TextArea).load_text("Keep this draft")
                    for widget_id in ("prompt", "send", "request-mode"):
                        region = app.query_one("#" + widget_id).region
                        self.assertGreater(region.width, 0)
                        self.assertLessEqual(region.right, width)
                        self.assertLessEqual(region.bottom, height)
                    app.query_one("#tabs", TabbedContent).active = "outputs"
                    await pilot.pause()
                    self.assertFalse(app.query_one("#composer").display)
                    for widget_id in ("terminal", "file-preview", "file-open", "file-save_as", "file-show_folder", "file-copy_path"):
                        region = app.query_one("#" + widget_id).region
                        self.assertGreater(region.width, 0, widget_id)
                        self.assertGreaterEqual(region.x, 0, widget_id)
                        self.assertLessEqual(region.right, width, widget_id)
                        self.assertLessEqual(region.bottom, height, widget_id)
                    self.assertGreaterEqual(app.query_one("#output-table", DataTable).region.height, 4)
                    app.query_one("#tabs", TabbedContent).active = "results"
                    await pilot.pause()
                    self.assertTrue(app.query_one("#composer").display)
                    self.assertEqual(app.query_one("#prompt", TextArea).text, "Keep this draft")

    async def test_terminal_closes_only_results_popup(self):
        service = FakeService()
        app = ResultsView(service)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            with patch.object(app, "exit") as exit_app:
                await pilot.click("#terminal")
                exit_app.assert_called_once()
            self.assertFalse(any(call[0] in {"send", "file_action"} for call in service.calls))


if __name__ == "__main__":
    unittest.main()
