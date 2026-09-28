"""Bounded, local-only previews for the Results UI; never launch a browser or shell.

Images/PDF pages use true-colour half blocks so they also work inside the Windows
terminal transport. ``body`` is Rich Text for raster previews and a string for
Markdown/plain text. PDF page indices are zero-based. Call this module in a UI
worker, not in the render loop. No native decoder objects remain in the cache.
"""
from __future__ import annotations

from collections import OrderedDict
from contextlib import closing
from io import BytesIO
import math
import re
import threading
import warnings

from rich.color import Color
from rich.style import Style
from rich.text import Text

from files import IMAGES, LIMIT, local_file


MAX_PIXELS = 20_000_000
MAX_DIMENSION = 20_000
MAX_COLUMNS = 160
MAX_ROWS = 60
MAX_TEXT = 100_000
MAX_PDF_PAGES = 1_000
CACHE_LIMIT = 32
BACKGROUND = (23, 29, 39)
_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f]")
_CACHE: OrderedDict[tuple, dict] = OrderedDict()
_CACHE_LOCK = threading.Lock()
# PDFium forbids concurrent calls, even for separate documents. This includes
# closing native resources, so the entire PDF lifetime is protected.
_PDF_LOCK = threading.Lock()


class PreviewError(ValueError):
    """A short, user-facing preview failure suitable for the UI notice."""

    def __init__(self, message):
        super().__init__(message)
        self.message = message


def clear_cache():
    """Drop rendered previews without retaining paths after the view closes."""
    with _CACHE_LOCK:
        _CACHE.clear()


def _pillow():
    try:
        from PIL import Image, ImageOps
        return Image, ImageOps
    except ImportError:
        raise PreviewError("Image preview is unavailable. Repair the Watchtower Results setup or use Open.") from None


def _pdfium():
    try:
        import pypdfium2
        return pypdfium2
    except ImportError:
        raise PreviewError("PDF preview is unavailable. Repair the Watchtower Results setup or use Open.") from None


def _copy_result(value):
    value = dict(value)
    if isinstance(value["body"], Text):
        value["body"] = value["body"].copy()
    return value


def _geometry(columns, rows):
    try:
        return max(1, min(MAX_COLUMNS, int(columns))), max(1, min(MAX_ROWS, int(rows)))
    except (TypeError, ValueError, OverflowError):
        raise PreviewError("Preview size is invalid.") from None


def _read(path, maximum):
    # Check again at the actual read so a growing/replaced file cannot cause an
    # unbounded read after local_file's stat check.
    with path.open("rb") as stream:
        data = stream.read(maximum + 1)
    return data


def _halfblocks(image, columns, rows):
    """Fit an image in terminal cells; every glyph draws two vertical pixels."""
    Image, _ = _pillow()
    with image.copy() as resized:
        resized.thumbnail((columns, rows * 2), Image.Resampling.LANCZOS)
        with resized.convert("RGBA") as rgba, Image.new("RGBA", resized.size, BACKGROUND + (255,)) as base:
            base.alpha_composite(rgba)
            with base.convert("RGB") as rgb:
                width, height = rgb.size
                pixels = list(rgb.get_flattened_data())
    body = Text(no_wrap=True, overflow="crop")
    for y in range(0, height, 2):
        for x in range(width):
            upper = pixels[y * width + x]
            lower = pixels[(y + 1) * width + x] if y + 1 < height else BACKGROUND
            body.append("▀", Style(color=Color.from_rgb(*upper), bgcolor=Color.from_rgb(*lower)))
        if y + 2 < height:
            body.append("\n")
    return body


def _image(data, columns, rows):
    Image, ImageOps = _pillow()
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(BytesIO(data)) as source:
                width, height = source.size
                if (width <= 0 or height <= 0 or width > MAX_DIMENSION or height > MAX_DIMENSION
                        or width * height > MAX_PIXELS):
                    raise PreviewError("Image is too large to preview safely. Use Open for the original.")
                # GIF n_frames walks the entire stream; is_animated only needs
                # to establish that a second frame exists.
                animated = getattr(source, "is_animated", False)
                source.seek(0)
                source.draft("RGB", (columns, rows * 2))
                with ImageOps.exif_transpose(source) as oriented:
                    body = _halfblocks(oriented, columns, rows)
        details = f"{width} × {height} px · image preview"
        if animated:
            details += " · first frame"
        return body, details
    except PreviewError:
        raise
    except (OSError, ValueError, SyntaxError, Image.DecompressionBombError, Image.DecompressionBombWarning):
        raise PreviewError("This image could not be decoded for preview. Use Open for the original.") from None


def _pdf(data, columns, rows, index):
    _pillow()
    pdfium = _pdfium()
    try:
        with _PDF_LOCK, closing(pdfium.PdfDocument(data)) as document:
            pages = len(document)
            if pages < 1 or pages > MAX_PDF_PAGES:
                raise PreviewError(f"PDF preview supports 1–{MAX_PDF_PAGES} pages. Use Open for this document.")
            if index < 0 or index >= pages:
                raise PreviewError("This PDF page is unavailable.")
            with closing(document[index]) as pdf_page:
                width, height = pdf_page.get_size()
                if (not all(math.isfinite(v) and 0 < v <= MAX_DIMENSION for v in (width, height))):
                    raise PreviewError("PDF page dimensions are unsupported. Use Open for this document.")
                scale = min(columns / width, (rows * 2) / height)
                # Forms/JavaScript are never initialized. Rendering only one
                # page at the bounded terminal resolution keeps allocation low.
                with closing(pdf_page.render(scale=scale, may_draw_forms=False)) as bitmap:
                    with bitmap.to_pil() as image:
                        body = _halfblocks(image, columns, rows)
        return body, pages, f"Page {index + 1} of {pages} · PDF preview"
    except PreviewError:
        raise
    except (pdfium.PdfiumError, OSError, ValueError, RuntimeError):
        raise PreviewError("This PDF could not be previewed. It may be damaged or password protected. Use Open.") from None


def preview(path, columns=72, rows=24, page=0):
    """Return ``{kind,title,body,page,pages,details}`` for one validated local file.

    Only image/PDF bodies are Rich Text; use Markdown(open_links=False) for the
    Markdown body and a non-markup Static for text. Caller-owned dict/Rich Text
    copies prevent UI changes from mutating cached results. Metadata changes
    invalidate every cached geometry/page for that file.
    """
    try:
        path = local_file(str(path))
        stat = path.stat()
    except (ValueError, OSError) as error:
        raise PreviewError(str(error) if isinstance(error, ValueError) else "The output file is no longer available.") from None
    columns, rows = _geometry(columns, rows)
    if not isinstance(page, int) or isinstance(page, bool) or page < 0:
        raise PreviewError("This preview page is invalid.")
    metadata = (str(path), stat.st_mtime_ns, stat.st_size, stat.st_ctime_ns)
    suffix = path.suffix.lower()
    is_raster = suffix in IMAGES or suffix == ".pdf"
    key = (*metadata, columns if is_raster else 0, rows if is_raster else 0, page if suffix == ".pdf" else 0)
    with _CACHE_LOCK:
        for previous in list(_CACHE):
            if previous[0] == str(path) and previous[:4] != metadata:
                del _CACHE[previous]
        if key in _CACHE:
            _CACHE.move_to_end(key)
            return _copy_result(_CACHE[key])
    try:
        data = _read(path, LIMIT if is_raster else MAX_TEXT * 4)
        result = {"title": _CONTROL.sub("", path.name), "page": 0, "pages": 1}
        if is_raster and len(data) > LIMIT:
            raise PreviewError("The file grew beyond the 32 MB preview limit.")
        if suffix in IMAGES:
            result["kind"] = "image"
            result["body"], result["details"] = _image(data, columns, rows)
        elif suffix == ".pdf":
            result["kind"], result["page"] = "pdf", page
            result["body"], result["pages"], result["details"] = _pdf(data, columns, rows, page)
        else:
            result["kind"] = "markdown" if suffix in {".md", ".markdown"} else "text"
            decoded = _CONTROL.sub("", data.decode("utf-8-sig", errors="replace"))
            result["body"] = decoded[:MAX_TEXT]
            truncated = len(decoded) > MAX_TEXT or stat.st_size > len(data)
            result["details"] = "Local document preview" + (f" · first {MAX_TEXT:,} characters" if truncated else "")
    except PreviewError:
        raise
    except OSError:
        raise PreviewError("The output file could not be read. It may have moved or be in use.") from None
    # Do not cache content if a worker is still replacing/writing this output.
    try:
        after = path.stat()
        unchanged = (after.st_mtime_ns, after.st_size, after.st_ctime_ns) == metadata[1:]
    except OSError:
        unchanged = False
    if unchanged:
        with _CACHE_LOCK:
            _CACHE[key] = _copy_result(result)
            _CACHE.move_to_end(key)
            while len(_CACHE) > CACHE_LIMIT:
                _CACHE.popitem(last=False)
    return result
