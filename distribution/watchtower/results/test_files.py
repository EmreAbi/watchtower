"""Output previews/actions use synthetic files and mocked external programs."""

from pathlib import Path
import os
import subprocess
import tempfile
import unittest
from unittest.mock import Mock, patch

import files


class OutputFilesTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.cache = self.root / "previews"

    def file(self, name="result.md", data="# Output"):
        path = self.root / name
        if isinstance(data, bytes):
            path.write_bytes(data)
        else:
            path.write_text(data, encoding="utf-8")
        return path

    def test_local_file_supports_spaces_and_file_uri(self):
        path = self.file("output report.md")
        self.assertEqual(files.local_file(str(path)), path)
        self.assertEqual(files.local_file(path.as_uri()), path)

    @unittest.skipUnless(os.name == "nt", "Windows drive-prefixed Markdown links")
    def test_markdown_slash_drive_path_accepts_turkish_and_spaces(self):
        path = self.file("Gün doğuşu resmi.png", b"synthetic-image-bytes")
        self.assertEqual(files.local_file("/" + path.as_posix()), path)
        self.assertEqual(files.local_file(path.as_uri()), path)

    @unittest.skipUnless(os.name == "nt", "Windows drive-relative paths")
    def test_normalization_keeps_drive_relative_and_network_paths_rejected(self):
        path = self.file("output.png", b"synthetic-image-bytes")
        drive_relative = path.drive + str(path)[len(path.drive) + 1:]
        for value in (drive_relative, "//server/share/output.png", "\\\\server\\share\\output.png",
                      "file://server/share/output.png", "/C:/output\x00.png", "/C:/output\n.png"):
            with self.subTest(value=value), self.assertRaises((ValueError, OSError)):
                files.local_file(value)

    def test_remote_control_relative_and_executable_paths_rejected(self):
        executable = self.file("payload.exe")
        for value in ("https://example.com/output.png", "file://server/share/result.md", "../result.md", str(executable),
                      "\\\\server\\share\\result.md", "file:///C:/output%00.md", str(self.root / "a\n.md"),
                      str(self.root / "missing.md")):
            with self.subTest(value=value), self.assertRaises((ValueError, OSError)):
                files.local_file(value)

    def test_oversized_file_rejected_before_content_read(self):
        path = self.file("big.txt", "123456")
        with patch.object(files, "LIMIT", 5), self.assertRaises(ValueError):
            files.local_file(str(path))

    def test_markdown_preview_escapes_html_disables_links_and_embeds(self):
        path = self.file(data='# Title\n\n<script>alert("x")</script>\n\n'
                              '[Link](https://private.example/path)\n\n'
                              '![Secret image](file:///C:/private/image.png)\n\n'
                              '| A | B |\n|---|---|\n| 1 | 2 |')
        rendered = files.render_preview(path, self.cache)
        page = rendered.read_text(encoding="utf-8")
        self.assertIn("<h1>Title</h1>", page)
        self.assertIn("<table>", page)
        self.assertNotIn("<script>", page)
        self.assertNotIn("<a href=", page)
        self.assertNotIn("<img ", page)
        self.assertNotIn("https://private.example/path", page)
        self.assertIn("Content-Security-Policy", page)
        self.assertIn("default-src 'none'", page)
        self.assertIn("Secret image", page)

    def test_text_preview_cannot_inject_html(self):
        path = self.file("text.txt", "<img src=x onerror=run()>")
        page = files.render_preview(path, self.cache).read_text(encoding="utf-8")
        self.assertIn("&lt;img src=x onerror=run()&gt;", page)
        self.assertNotIn("<img src=x", page)

    def test_image_preview_embeds_data_without_external_resource(self):
        path = self.file("sample.png", b"synthetic-image-bytes")
        page = files.render_preview(path, self.cache).read_text(encoding="utf-8")
        self.assertIn("src=\"data:image/png;base64,", page)
        self.assertNotIn("http://", page)
        self.assertNotIn("https://", page)

    def test_preview_does_not_open_browser_until_explicit_action(self):
        path = self.file()
        with patch("files.webbrowser.open", return_value=True) as browser:
            rendered = files.render_preview(path, self.cache)
            browser.assert_not_called()
            self.assertEqual(files.action(path, "preview", self.cache), "Opened in browser.")
        browser.assert_called_once_with(rendered.as_uri(), new=2)

    def test_pdf_uses_browser_and_not_file_association(self):
        path = self.file("report.pdf", b"%PDF-synthetic")
        self.assertEqual(files.render_preview(path, self.cache), path)
        with patch("files.webbrowser.open", return_value=True) as browser, patch("files.subprocess.Popen") as process:
            files.action(path, "open", self.cache)
        browser.assert_called_once_with(path.as_uri(), new=2)
        process.assert_not_called()

    def test_browser_failure_is_reported(self):
        path = self.file()
        with patch("files.webbrowser.open", return_value=False), self.assertRaises(ValueError):
            files.action(path, "open", self.cache)

    def test_unknown_action_never_invokes_external_program(self):
        path = self.file()
        with patch("files.webbrowser.open") as browser, patch("files.subprocess.Popen") as process:
            with self.assertRaises(ValueError):
                files.action(path, "execute", self.cache)
        browser.assert_not_called()
        process.assert_not_called()

    @unittest.skipUnless(os.name == "nt", "Windows clipboard helper")
    def test_copy_uses_full_path_not_wrapped_terminal_selection(self):
        path = self.file("a very long filename.md")
        with patch("files.subprocess.run") as run:
            self.assertEqual(files.action(path, "copy_path", self.cache), "Copied full path.")
        self.assertEqual(run.call_args.kwargs["input"], str(path))
        self.assertNotIn(str(path), " ".join(run.call_args.args[0]))
        self.assertIn("-NoProfile", run.call_args.args[0])
        self.assertEqual(run.call_args.kwargs["creationflags"], subprocess.CREATE_NO_WINDOW)
        self.assertEqual(run.call_args.kwargs["timeout"], 5)

    def test_save_as_is_explicit_and_requires_same_extension(self):
        path = self.file()
        widget = Mock()
        with patch("tkinter.Tk", return_value=widget), patch("tkinter.filedialog.asksaveasfilename", return_value=""):
            self.assertEqual(files.action(path, "save_as", self.cache), "Save cancelled.")
        destination = self.root / "copied.md"
        with patch("tkinter.Tk", return_value=widget), patch("tkinter.filedialog.asksaveasfilename", return_value=str(destination)):
            self.assertEqual(files.action(path, "save_as", self.cache), "Saved output.")
        self.assertEqual(destination.read_bytes(), path.read_bytes())
        with patch("tkinter.Tk", return_value=widget), patch("tkinter.filedialog.asksaveasfilename", return_value=str(self.root / "bad.exe")):
            with self.assertRaises(ValueError):
                files.action(path, "save_as", self.cache)
        self.assertFalse((self.root / "bad.exe").exists())


if __name__ == "__main__":
    unittest.main()
