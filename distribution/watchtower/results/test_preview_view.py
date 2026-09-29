"""Synthetic in-app preview interactions; no live files, browser or provider."""
from __future__ import annotations

import asyncio
import threading
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from rich.text import Text
from textual.app import App, ComposeResult
from textual.widgets import Button, Markdown, Static, TabbedContent, TabPane

from bridge import ResultsError
from preview import PreviewError
from preview_view import PreviewPanel, PreviewScreen

try:
    from PIL import Image
except ImportError:
    Image = None


def result(file_id, kind="image", page=0, pages=1):
    body = Text("▀▀▀\n▀▀▀", style="#ff8844 on #334477") if kind in {"image", "pdf"} else "# Example\n\n[link](https://invalid.example)"
    return {"kind": kind, "title": f"Output {file_id}", "body": body,
            "page": page, "pages": pages, "details": "Local preview"}


class Loader:
    def __init__(self, kind="image", pages=1):
        self.kind, self.pages = kind, pages
        self.calls = []
        self.thread_ids = []
        self.error = None
        self.block_id = None
        self.started, self.release = threading.Event(), threading.Event()

    def __call__(self, file_id, **options):
        self.calls.append((file_id, options))
        self.thread_ids.append(threading.get_ident())
        if file_id == self.block_id:
            self.started.set()
            self.release.wait(3)
        if self.error:
            raise self.error
        return result(file_id, self.kind, options.get("page", 0), self.pages)


class PreviewApp(App):
    BINDINGS = [("escape", "quit", "Quit")]

    def __init__(self, loader):
        super().__init__()
        self.loader = loader

    def compose(self) -> ComposeResult:
        yield PreviewPanel(self.loader, id="embedded-preview")


class PreviewViewTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        asyncio.get_running_loop().set_debug(False)

    async def wait_preview(self, app, pilot):
        await pilot.pause()
        await app.workers.wait_for_complete()
        await pilot.pause()

    async def test_loader_runs_off_ui_thread_and_receives_only_file_id_and_geometry(self):
        loader = Loader()
        app = PreviewApp(loader)
        async with app.run_test(size=(90, 30)) as pilot:
            panel = app.query_one(PreviewPanel)
            self.assertFalse(loader.calls)
            panel.set_file("opaque-42")
            await self.wait_preview(app, pilot)
            self.assertEqual(panel.result["title"], "Output opaque-42")
            file_id, options = loader.calls[-1]
            self.assertEqual(file_id, "opaque-42")
            self.assertEqual(set(options), {"columns", "rows", "page"})
            self.assertLessEqual(options["columns"], 90)
            self.assertLessEqual(options["rows"], 30)
            self.assertTrue(all(identity != threading.get_ident() for identity in loader.thread_ids))
            self.assertTrue(panel.query_one(".preview-image").display)
            self.assertFalse(panel.query_one(".preview-navigation").display)
            before = len(loader.calls)
            panel.set_file("opaque-42")
            await pilot.pause()
            self.assertEqual(len(loader.calls), before)
            panel.set_file("opaque-42", force=True)
            await self.wait_preview(app, pilot)
            self.assertEqual(len(loader.calls), before + 1)

    async def test_selection_before_mount_loads_once_ready(self):
        loader = Loader()
        app = PreviewApp(loader)
        original = app.compose

        def compose():
            for panel in original():
                panel.set_file("before-mount")
                yield panel

        app.compose = compose
        async with app.run_test(size=(90, 30)) as pilot:
            await self.wait_preview(app, pilot)
            self.assertEqual(app.query_one(PreviewPanel).result["title"], "Output before-mount")

    async def test_pdf_previous_next_uses_same_file_and_respects_bounds(self):
        loader = Loader("pdf", 3)
        app = PreviewApp(loader)
        async with app.run_test(size=(90, 30)) as pilot:
            panel = app.query_one(PreviewPanel)
            panel.set_file("pdf-id")
            await self.wait_preview(app, pilot)
            # Textual deliberately ignores mouse clicks during its button flash;
            # navigation assertions should not race that animation timer.
            for button in panel.query(".preview-navigation Button"):
                button.active_effect_duration = 0
            self.assertTrue(panel.query_one("#preview-previous", Button).disabled)
            self.assertFalse(panel.query_one("#preview-next", Button).disabled)
            await pilot.click("#preview-next")
            await self.wait_preview(app, pilot)
            self.assertEqual(panel.page, 1)
            self.assertIn("2 / 3", str(panel.query_one(".preview-page", Static).render()))
            await pilot.click("#preview-next")
            await self.wait_preview(app, pilot)
            self.assertEqual(panel.page, 2)
            self.assertTrue(panel.query_one("#preview-next", Button).disabled)
            await pilot.click("#preview-previous")
            await self.wait_preview(app, pilot)
            self.assertEqual(panel.page, 1)
            self.assertTrue(all(file_id == "pdf-id" for file_id, _ in loader.calls))
            panel.set_file("other-pdf")
            await self.wait_preview(app, pilot)
            self.assertEqual(panel.page, 0)

    async def test_late_old_selection_cannot_replace_new_preview(self):
        loader = Loader()
        loader.block_id = "old"
        app = PreviewApp(loader)
        try:
            async with app.run_test(size=(90, 30)) as pilot:
                panel = app.query_one(PreviewPanel)
                panel.set_file("old")
                self.assertTrue(await asyncio.to_thread(loader.started.wait, 1))
                panel.set_file("new")
                await self.wait_preview(app, pilot)
                self.assertEqual(panel.result["title"], "Output new")
                loader.release.set()
                await pilot.pause()
                self.assertEqual(panel.result["title"], "Output new")
                self.assertEqual(panel.file_id, "new")
        finally:
            loader.release.set()

    async def test_clearing_selection_discards_inflight_result(self):
        loader = Loader()
        loader.block_id = "old"
        app = PreviewApp(loader)
        try:
            async with app.run_test(size=(90, 30)) as pilot:
                panel = app.query_one(PreviewPanel)
                panel.set_file("old")
                self.assertTrue(await asyncio.to_thread(loader.started.wait, 1))
                panel.set_file(None)
                loader.release.set()
                await self.wait_preview(app, pilot)
                self.assertIsNone(panel.result)
                self.assertIsNone(panel.file_id)
                self.assertFalse(panel.loading)
                self.assertIn("Select an output", str(panel.query_one(".preview-text", Static).render()))
                self.assertFalse(panel.query_one(".preview-image").display)
        finally:
            loader.release.set()

    async def test_only_known_safe_errors_are_visible_and_markup_is_literal(self):
        loader = Loader()
        app = PreviewApp(loader)
        async with app.run_test(size=(90, 30)) as pilot:
            panel = app.query_one(PreviewPanel)
            for error, expected in ((ResultsError("[red]File unavailable[/red]"), "[red]File unavailable[/red]"),
                                    (PreviewError("Unsupported PDF."), "Unsupported PDF."),
                                    (RuntimeError("private-token-value"), "Preview is unavailable")):
                loader.error = error
                panel.set_file("bad", force=True)
                await self.wait_preview(app, pilot)
                notice = panel.query_one(".preview-text", Static)
                self.assertTrue(notice.has_class("error"))
                self.assertIn(expected, str(notice.render()))
                self.assertNotIn("private-token-value", str(notice.render()))
            loader.error = None
            loader.kind = "text"
            panel.set_file("good")
            await self.wait_preview(app, pilot)
            self.assertFalse(panel.query_one(".preview-text").has_class("error"))

    async def test_markdown_links_are_consumed_and_never_open_browser(self):
        loader = Loader("markdown")
        app = PreviewApp(loader)
        async with app.run_test(size=(90, 30)) as pilot:
            panel = app.query_one(PreviewPanel)
            panel.set_file("markdown-id")
            await self.wait_preview(app, pilot)
            markdown = panel.query_one(".preview-markdown", Markdown)
            self.assertTrue(markdown.display)
            with patch.object(app, "open_url") as open_url:
                markdown.post_message(Markdown.LinkClicked(markdown, "https://invalid.example/private"))
                await pilot.pause()
                open_url.assert_not_called()
            self.assertIn("Outputs actions", str(panel.query_one(".preview-details", Static).render()))

    async def test_modal_close_and_escape_return_to_existing_view(self):
        loader = Loader()
        app = PreviewApp(loader)
        async with app.run_test(size=(90, 30)) as pilot:
            base_screen = app.screen
            for close_by in ("button", "escape"):
                modal = PreviewScreen(loader, "selected")
                await app.push_screen(modal)
                await self.wait_preview(app, pilot)
                self.assertIs(app.screen, modal)
                self.assertEqual(modal.query_one(PreviewPanel).result["title"], "Output selected")
                close = modal.query_one("#preview-close", Button).region
                self.assertLessEqual(close.right, 90)
                self.assertLessEqual(close.bottom, 30)
                if close_by == "button":
                    await pilot.click("#preview-close")
                else:
                    await pilot.press("escape")
                await pilot.pause()
                self.assertIs(app.screen, base_screen)
                self.assertTrue(app.is_running)

    async def test_close_modal_during_load_does_not_reopen_or_update_base(self):
        loader = Loader()
        loader.block_id = "slow"
        app = PreviewApp(loader)
        try:
            async with app.run_test(size=(90, 30)) as pilot:
                base = app.screen
                await app.push_screen(PreviewScreen(loader, "slow"))
                self.assertTrue(await asyncio.to_thread(loader.started.wait, 1))
                await pilot.press("escape")
                loader.release.set()
                await self.wait_preview(app, pilot)
                self.assertIs(app.screen, base)
                self.assertIsNone(app.query_one(PreviewPanel).file_id)
        finally:
            loader.release.set()

    async def test_resize_reloads_current_file_with_new_geometry(self):
        loader = Loader()
        app = PreviewApp(loader)
        async with app.run_test(size=(90, 30)) as pilot:
            panel = app.query_one(PreviewPanel)
            panel.set_file("image-id")
            await self.wait_preview(app, pilot)
            before = loader.calls[-1][1]
            await pilot.resize_terminal(60, 24)
            await pilot.pause(0.2)
            await self.wait_preview(app, pilot)
            after = loader.calls[-1][1]
            self.assertLess(after["columns"], before["columns"])
            self.assertLess(after["rows"], before["rows"])
            self.assertEqual(loader.calls[-1][0], "image-id")

    @unittest.skipUnless(Image, "Pillow is optional")
    async def test_real_raster_expands_after_hidden_tab_mount_modal_and_resize(self):
        from preview import preview as render_preview

        calls = []
        with tempfile.TemporaryDirectory(prefix="watchtower-preview-test-") as temp:
            path = Path(temp).resolve() / "synthetic.png"
            with Image.new("RGB", (640, 360), (225, 114, 70)) as image:
                image.save(path)

            def loader(file_id, **options):
                calls.append((file_id, options))
                return render_preview(path, **options)

            class TabApp(App):
                def compose(self):
                    with TabbedContent(initial="first"):
                        with TabPane("First", id="first"):
                            yield Static("Select Outputs")
                        with TabPane("Outputs", id="outputs"):
                            yield PreviewPanel(loader, id="real-preview")

            def assert_image(panel):
                self.assertIsNotNone(panel.result)
                lines = panel.result["body"].plain.splitlines()
                self.assertGreater(len(lines), 5)
                self.assertGreater(max(map(len, lines)), 10)
                self.assertTrue(panel.query_one(".preview-image").display)

            app = TabApp()
            async with app.run_test(size=(120, 40)) as pilot:
                panel = app.query_one(PreviewPanel)
                panel.set_file("opaque-image")
                app.query_one(TabbedContent).active = "outputs"
                await pilot.pause(0.2)
                await self.wait_preview(app, pilot)
                assert_image(panel)
                self.assertTrue(all(call[1]["columns"] > 1 and call[1]["rows"] > 1 for call in calls))
                await app.push_screen(PreviewScreen(loader, "opaque-image"))
                await pilot.pause(0.2)
                await self.wait_preview(app, pilot)
                assert_image(app.screen.query_one(PreviewPanel))
                await pilot.resize_terminal(90, 35)
                await pilot.pause(0.2)
                await self.wait_preview(app, pilot)
                assert_image(app.screen.query_one(PreviewPanel))


if __name__ == "__main__":
    unittest.main()
