"""Local raster/document previews use small synthetic files, never live outputs."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from pathlib import Path
import os
import sys
import tempfile
import unittest
from unittest.mock import patch

from rich.text import Text

import preview

try:
    from PIL import Image
except ImportError:
    Image = None
try:
    import pypdfium2 as pdfium
except ImportError:
    pdfium = None


class LocalPreviewTest(unittest.TestCase):
    def setUp(self):
        preview.clear_cache()
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.addCleanup(preview.clear_cache)
        self.root = Path(self.tmp.name).resolve()

    def file(self, name="sample.md", value="# Result"):
        path = self.root / name
        path.write_bytes(value if isinstance(value, bytes) else value.encode("utf-8"))
        return path

    def image(self, name="sample.png", size=(12, 8), color=(220, 40, 10)):
        path = self.root / name
        with Image.new("RGB", size, color) as image:
            image.save(path)
        return path

    def pdf(self, name="sample.pdf"):
        path = self.root / name
        with closing(pdfium.PdfDocument.new()) as document:
            for width, height in ((200, 300), (300, 200)):
                with closing(document.new_page(width, height)):
                    pass
            document.save(path)
        return path

    def test_markdown_body_is_bounded_and_does_not_follow_links_or_images(self):
        content = '# Report\n\n[Link](https://invalid.example/)\n![private](file:///C:/secret.png)\n'
        path = self.file(value=content)
        with patch("socket.create_connection", side_effect=AssertionError("No networking")), \
                patch("webbrowser.open", side_effect=AssertionError("No browser")):
            result = preview.preview(path)
        self.assertEqual(result["kind"], "markdown")
        self.assertEqual(result["body"], content)
        self.assertEqual(result["pages"], 1)
        self.assertEqual(result["page"], 0)

    def test_text_strips_terminal_controls_and_utf8_bom(self):
        path = self.file("sample.txt", b"\xef\xbb\xbfHello\x1b[31m\x00\x07\nworld\t.")
        result = preview.preview(path)
        self.assertEqual(result["kind"], "text")
        self.assertEqual(result["body"], "Hello[31m\nworld\t.")

    def test_large_text_is_truncated_before_rendering(self):
        path = self.file("large.md", "x" * (preview.MAX_TEXT + 100))
        result = preview.preview(path)
        self.assertEqual(len(result["body"]), preview.MAX_TEXT)
        self.assertIn("first 100,000 characters", result["details"])

    def test_cache_is_invalidated_by_metadata_change_even_at_same_length(self):
        path = self.file(value="old")
        first = preview.preview(path)
        timestamp = path.stat().st_mtime_ns
        path.write_text("new", encoding="utf-8")
        os.utime(path, ns=(timestamp + 1_000_000, timestamp + 1_000_000))
        second = preview.preview(path)
        self.assertEqual(first["body"], "old")
        self.assertEqual(second["body"], "new")
        self.assertEqual(len(preview._CACHE), 1)

    def test_cached_document_returns_copy_and_deleted_file_is_revalidated(self):
        path = self.file()
        first = preview.preview(path)
        first["title"] = "changed"
        with patch("preview._read", side_effect=AssertionError("Already cached")):
            self.assertEqual(preview.preview(path)["title"], path.name)
        path.unlink()
        with self.assertRaises(preview.PreviewError):
            preview.preview(path)

    def test_lru_is_limited_to_32_entries(self):
        for index in range(preview.CACHE_LIMIT + 5):
            preview.preview(self.file(f"{index}.txt", f"Output {index}"))
        self.assertEqual(len(preview._CACHE), 32)
        self.assertFalse(any(Path(key[0]).name == "0.txt" for key in preview._CACHE))

    def test_remote_relative_oversize_and_missing_files_are_rejected(self):
        for value in ("https://invalid.example/image.png", "file://server/image.png", "relative.png", str(self.root / "missing.png")):
            with self.subTest(value=value), self.assertRaises(preview.PreviewError):
                preview.preview(value)
        path = self.file("large.txt", "123456")
        with patch("files.LIMIT", 5), self.assertRaises(preview.PreviewError):
            preview.preview(path)

    def test_invalid_geometry_and_page_fail_cleanly(self):
        path = self.file()
        for column in (None, float("nan"), float("inf"), "bad"):
            with self.subTest(column=column), self.assertRaises(preview.PreviewError):
                preview.preview(path, columns=column)
        for page in (-1, "1", True, None):
            with self.subTest(page=page), self.assertRaises(preview.PreviewError):
                preview.preview(path, page=page)

    @unittest.skipUnless(Image, "Pillow is optional")
    def test_raster_has_truecolor_halfblocks_and_fits_requested_cells(self):
        path = self.image(size=(100, 50))
        result = preview.preview(path, columns=12, rows=4)
        self.assertEqual(result["kind"], "image")
        self.assertIsInstance(result["body"], Text)
        lines = result["body"].plain.splitlines()
        self.assertLessEqual(len(lines), 4)
        self.assertTrue(all(len(line) <= 12 for line in lines))
        self.assertTrue(result["body"].spans)
        self.assertEqual(result["body"].spans[0].style.color.triplet, (220, 40, 10))
        self.assertIn("100 × 50 px", result["details"])

    @unittest.skipUnless(Image, "Pillow is optional")
    def test_transparent_pixels_use_results_background(self):
        path = self.root / "transparent.png"
        with Image.new("RGBA", (2, 2), (255, 0, 0, 0)) as image:
            image.save(path)
        result = preview.preview(path)
        self.assertEqual(result["body"].spans[0].style.color.triplet, preview.BACKGROUND)

    @unittest.skipUnless(Image, "Pillow is optional")
    def test_cached_rich_text_cannot_be_mutated_by_consumer(self):
        path = self.image()
        first = preview.preview(path)
        original = first["body"].plain
        first["body"].append("corruption")
        second = preview.preview(path)
        self.assertEqual(second["body"].plain, original)

    @unittest.skipUnless(Image, "Pillow is optional")
    def test_animated_image_uses_only_first_frame(self):
        path = self.root / "animated.gif"
        with Image.new("RGB", (4, 4), "red") as first, Image.new("RGB", (4, 4), "blue") as second:
            first.save(path, save_all=True, append_images=[second])
        result = preview.preview(path)
        self.assertIn("first frame", result["details"])
        self.assertEqual(result["body"].spans[0].style.color.triplet, (255, 0, 0))

    @unittest.skipUnless(Image, "Pillow is optional")
    def test_exif_rotation_is_applied(self):
        path = self.root / "rotated.jpg"
        with Image.new("RGB", (12, 4), "red") as image:
            exif = Image.Exif()
            exif[274] = 6
            image.save(path, exif=exif)
        result = preview.preview(path, columns=30, rows=30)
        self.assertEqual(len(result["body"].plain.splitlines()), 6)
        self.assertEqual(len(result["body"].plain.splitlines()[0]), 4)

    @unittest.skipUnless(Image, "Pillow is optional")
    def test_pixel_limit_precedes_decode_and_corrupt_image_is_friendly(self):
        path = self.image(size=(20, 20))
        with patch("preview.MAX_PIXELS", 100), self.assertRaisesRegex(preview.PreviewError, "too large"):
            preview.preview(path)
        corrupt = self.file("corrupt.png", b"not an image")
        with self.assertRaisesRegex(preview.PreviewError, "could not be decoded"):
            preview.preview(corrupt)

    @unittest.skipUnless(Image, "Pillow is optional")
    def test_growing_raster_is_limited_during_read(self):
        path = self.image()
        with patch("preview.LIMIT", 5), self.assertRaisesRegex(preview.PreviewError, "32 MB"):
            preview.preview(path)

    @unittest.skipUnless(Image, "Pillow is optional")
    def test_missing_pillow_reports_repair_instead_of_crashing_import(self):
        path = self.image()
        with patch.dict(sys.modules, {"PIL": None}), self.assertRaisesRegex(preview.PreviewError, "Image preview is unavailable"):
            preview.preview(path)

    @unittest.skipUnless(Image and pdfium, "PDF/Pillow preview dependencies are optional")
    def test_pdf_page_navigation_and_bounds(self):
        path = self.pdf()
        first = preview.preview(path, columns=20, rows=8)
        second = preview.preview(path, columns=20, rows=8, page=1)
        self.assertEqual(first["kind"], "pdf")
        self.assertEqual(first["pages"], 2)
        self.assertEqual(second["page"], 1)
        self.assertIn("Page 2 of 2", second["details"])
        self.assertIsInstance(first["body"], Text)
        self.assertNotEqual(first["body"].plain, second["body"].plain)
        for result in (first, second):
            self.assertLessEqual(len(result["body"].plain.splitlines()), 8)
            self.assertTrue(all(len(line) <= 20 for line in result["body"].plain.splitlines()))
        with self.assertRaisesRegex(preview.PreviewError, "page is unavailable"):
            preview.preview(path, page=2)

    @unittest.skipUnless(Image and pdfium, "PDF/Pillow preview dependencies are optional")
    def test_pdf_is_safe_from_multiple_ui_workers_and_releases_resources(self):
        paths = [self.pdf(f"sample-{index}.pdf") for index in range(4)]
        with ThreadPoolExecutor(max_workers=4) as pool:
            results = list(pool.map(preview.preview, paths))
        self.assertTrue(all(result["pages"] == 2 for result in results))
        for path in paths:
            path.unlink()
            self.assertFalse(path.exists())

    @unittest.skipUnless(Image and pdfium, "PDF/Pillow preview dependencies are optional")
    def test_pdf_page_count_limit_and_corrupt_pdf_are_friendly(self):
        path = self.pdf()
        with patch("preview.MAX_PDF_PAGES", 1), self.assertRaisesRegex(preview.PreviewError, "supports 1–1 pages"):
            preview.preview(path)
        with self.assertRaisesRegex(preview.PreviewError, "could not be previewed"):
            preview.preview(self.file("corrupt.pdf", b"%PDF-not-valid"))

    @unittest.skipUnless(Image, "Pillow is optional")
    def test_missing_pdfium_reports_repair(self):
        path = self.file("sample.pdf", b"%PDF-unused")
        with patch.dict(sys.modules, {"pypdfium2": None}), self.assertRaisesRegex(preview.PreviewError, "PDF preview is unavailable"):
            preview.preview(path)


if __name__ == "__main__":
    unittest.main()
