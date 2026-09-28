"""Reusable in-app output preview; callers pass only a validated file-ID loader."""
from __future__ import annotations

import asyncio
import re
from collections.abc import Callable

from rich.text import Text
from textual import work
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Button, Markdown, Static


def _plain(value, limit=500):
    return re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f]", "", str(value or ""))[:limit]


def _safe_error(error):
    from preview import PreviewError
    from bridge import ResultsError
    if isinstance(error, (PreviewError, ResultsError)) and error.message:
        return _plain(error.message)
    return "Preview is unavailable. The file may have moved or be unsupported. Use Open for the original."


class PreviewPanel(Vertical):
    """Read-only preview embedded in Results or enlarged in PreviewScreen."""

    DEFAULT_CSS = """
    PreviewPanel { width: 1fr; height: 1fr; min-height: 8; padding: 0 1;
        background: #171d27; border: round #344055; }
    PreviewPanel > .preview-title { height: 1; color: #9ecbff; text-style: bold; }
    PreviewPanel > .preview-details { height: 2; color: #8995a7; }
    PreviewPanel > .preview-scroll { height: 1fr; width: 1fr; }
    PreviewPanel .preview-image { height: auto; width: 100%; content-align: center middle; }
    PreviewPanel .preview-text { height: auto; width: 100%; }
    PreviewPanel .preview-markdown { height: auto; width: 100%; margin: 0; padding: 0; }
    PreviewPanel .preview-text.error { color: #ff929d; }
    PreviewPanel > .preview-navigation { height: 3; width: 100%; margin-top: 0; }
    PreviewPanel .preview-navigation Button { width: 12; min-width: 8; }
    PreviewPanel .preview-page { width: 1fr; height: 3; content-align: center middle; color: #b3c5db; }
    """

    def __init__(self, load_preview: Callable, *, id=None):
        super().__init__(id=id)
        self.load_preview = load_preview
        self.file_id = None
        self.page = 0
        self.pages = 1
        self.kind = None
        self.loading = False
        self.result = None
        self._generation = 0
        self._ready = False
        self._resize_timer = None
        self._requested_size = None

    def compose(self) -> ComposeResult:
        yield Static("Preview", classes="preview-title", markup=False)
        yield Static("", classes="preview-details", markup=False)
        with VerticalScroll(classes="preview-scroll"):
            yield Static("", classes="preview-image", markup=False)
            yield Markdown("", classes="preview-markdown", open_links=False)
            yield Static("Select an output to see its preview.", classes="preview-text", markup=False)
        with Horizontal(classes="preview-navigation"):
            yield Button("Previous", id="preview-previous", disabled=True)
            yield Static("", classes="preview-page", markup=False)
            yield Button("Next", id="preview-next", disabled=True)

    def on_mount(self):
        self._ready = True
        self._show_message("Select an output to see its preview.")
        if self.file_id is not None:
            self.call_after_refresh(self._reload)

    def on_unmount(self):
        self._ready = False
        self._generation += 1
        if self._resize_timer is not None:
            self._resize_timer.stop()

    def set_file(self, file_id, force=False):
        """Select an opaque service file ID; None immediately clears stale work."""
        if file_id == self.file_id and not force:
            return
        changed = file_id != self.file_id
        self.file_id = file_id
        self._generation += 1
        self.result = None
        if changed:
            self.page, self.pages, self.kind = 0, 1, None
        if not self._ready:
            return
        self.workers.cancel_group(self, "preview-load")
        self.loading = False
        self.query_one(".preview-title", Static).update("Preview")
        self.query_one(".preview-details", Static).update("")
        self._show_message("Select an output to see its preview." if file_id is None else "Loading preview…")
        if file_id is not None:
            self._reload()

    def _geometry(self):
        # Reserve title/details, border and PDF navigation before loading; page
        # navigation appearing must not make the new raster taller than its box.
        return max(1, min(160, self.size.width - 4)), max(1, min(60, self.size.height - 8))

    def on_resize(self, _event):
        if not self._ready or self.file_id is None or self._requested_size == self._geometry():
            return
        if self._requested_size is None:
            # A newly shown tab/modal has no usable geometry at set_file time.
            # Draw its first preview after layout instead of flashing a 1×1 image.
            self.call_after_refresh(self._reload_if_resized)
            return
        if self._resize_timer is not None:
            self._resize_timer.stop()
        self._resize_timer = self.set_timer(0.15, self._reload_if_resized)

    def _reload_if_resized(self):
        if self._ready and self.file_id is not None and self._requested_size != self._geometry():
            self._reload()

    def _reload(self):
        if not self._ready or self.file_id is None or self.size.width <= 0 or self.size.height <= 0:
            return
        self._generation += 1
        self.loading = True
        self._requested_size = self._geometry()
        self._update_navigation()
        self._load(self._generation, self.file_id, self.page, *self._requested_size)

    def _show_message(self, message, error=False):
        self.query_one(".preview-image").display = False
        self.query_one(".preview-markdown").display = False
        text = self.query_one(".preview-text", Static)
        text.display = True
        text.set_class(error, "error")
        text.update(Text(_plain(message, 100_000)))
        self.query_one(".preview-navigation").display = False
        self.query_one(".preview-scroll", VerticalScroll).scroll_home(animate=False)

    def _update_navigation(self):
        if not self._ready:
            return
        self.query_one(".preview-navigation").display = self.kind == "pdf" and self.pages > 1
        self.query_one("#preview-previous", Button).disabled = self.loading or self.page <= 0
        self.query_one("#preview-next", Button).disabled = self.loading or self.page + 1 >= self.pages
        self.query_one(".preview-page", Static).update(f"{self.page + 1} / {self.pages}")

    def _current(self, generation, file_id):
        return self._ready and generation == self._generation and file_id == self.file_id

    @work(exclusive=True, group="preview-load")
    async def _load(self, generation, file_id, page, columns, rows):
        try:
            result = await asyncio.to_thread(self.load_preview, file_id, columns=columns, rows=rows, page=page)
            if not self._current(generation, file_id):
                return
            if (not isinstance(result, dict) or result.get("kind") not in {"image", "pdf", "markdown", "text"}
                    or not isinstance(result.get("body"), (str, Text))):
                raise ValueError("Unexpected preview result")
            self.kind = result["kind"]
            self.page = max(0, int(result.get("page", 0)))
            self.pages = max(1, int(result.get("pages", 1)))
            self.query_one(".preview-title", Static).update(Text(_plain(result.get("title"), 200), no_wrap=True, overflow="ellipsis"))
            self.query_one(".preview-details", Static).update(Text(_plain(result.get("details"))))
            self.query_one(".preview-image").display = self.kind in {"image", "pdf"}
            self.query_one(".preview-markdown").display = self.kind == "markdown"
            self.query_one(".preview-text").display = self.kind == "text"
            if self.kind in {"image", "pdf"}:
                self.query_one(".preview-image", Static).update(result["body"])
            elif self.kind == "markdown":
                await self.query_one(".preview-markdown", Markdown).update(_plain(result["body"], 100_000))
            else:
                text = self.query_one(".preview-text", Static)
                text.remove_class("error")
                text.update(Text(_plain(result["body"], 100_000)))
            if not self._current(generation, file_id):
                return
            self.result = result
            self.query_one(".preview-scroll", VerticalScroll).scroll_home(animate=False)
        except Exception as error:
            if self._current(generation, file_id):
                self.result, self.kind, self.page, self.pages = None, None, 0, 1
                self.query_one(".preview-details", Static).update("")
                self._show_message(_safe_error(error), error=True)
        finally:
            if self._current(generation, file_id):
                self.loading = False
                self._update_navigation()

    def on_button_pressed(self, event: Button.Pressed):
        if event.button.id not in {"preview-previous", "preview-next"}:
            return
        event.stop()
        if self.loading or self.kind != "pdf":
            return
        candidate = self.page + (-1 if event.button.id == "preview-previous" else 1)
        if 0 <= candidate < self.pages:
            self.page = candidate
            self._reload()

    def on_markdown_link_clicked(self, event: Markdown.LinkClicked):
        event.stop()
        self.query_one(".preview-details", Static).update("Links stay inside this preview. Use the Outputs actions to open listed files.")


class PreviewScreen(ModalScreen):
    """Larger preview above Results; closing returns to the same selected output."""

    BINDINGS = [Binding("escape", "close", "Close", priority=True)]
    DEFAULT_CSS = """
    PreviewScreen { align: center middle; background: #000000 65%; }
    PreviewScreen > .preview-dialog { width: 90%; height: 90%; background: #171d27;
        border: round #7da6da; padding: 0 1; }
    PreviewScreen .preview-screen-heading { width: 100%; height: 3; }
    PreviewScreen .preview-screen-title { width: 1fr; height: 3; padding: 1 1;
        color: #9ecbff; text-style: bold; }
    PreviewScreen #preview-close { width: 12; min-width: 8; }
    PreviewScreen PreviewPanel { height: 1fr; }
    """

    def __init__(self, load_preview: Callable, file_id):
        super().__init__()
        self.load_preview = load_preview
        self.file_id = file_id

    def compose(self) -> ComposeResult:
        with Vertical(classes="preview-dialog"):
            with Horizontal(classes="preview-screen-heading"):
                yield Static("Output preview", classes="preview-screen-title", markup=False)
                yield Button("Close", id="preview-close")
            yield PreviewPanel(self.load_preview, id="full-preview")

    def on_mount(self):
        self.query_one(PreviewPanel).set_file(self.file_id)
        self.query_one("#preview-close", Button).focus()

    def on_button_pressed(self, event: Button.Pressed):
        if event.button.id == "preview-close":
            event.stop()
            self.action_close()

    def action_close(self):
        self.query_one(PreviewPanel).set_file(None)
        self.dismiss(None)
