"""
OCR fallback extraction.

OCR is used ONLY when PDF-native text is missing/insufficient (scanned
page) or when raster interpretation is otherwise required. It is never
the first choice — see `pdf_native.has_sufficient_native_text`.

Backed by Tesseract via `pytesseract`. Swapping in PaddleOCR later only
requires a new function with the same return shape
(`list[RawTextItem]`), since nothing outside this module should know
which OCR engine ran.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from backend.cv_extraction.coordinates import pixel_bbox_to_page_bbox
from backend.cv_extraction.raw_types import RawTextItem, SourceKind
from backend.tools.logging_config import get_logger

if TYPE_CHECKING:  # pragma: no cover
    from PIL import Image

logger = get_logger(__name__)

DEFAULT_OCR_DPI = 300.0


def _import_ocr_stack():
    try:
        import pytesseract
        from PIL import Image  # noqa: F401
    except ImportError as exc:  # pragma: no cover - environment issue, not logic
        raise RuntimeError(
            "pytesseract and Pillow are required for OCR fallback. Install with "
            "`pip install pytesseract pillow` and ensure the tesseract binary is on PATH."
        ) from exc

    # `pip install pytesseract` only installs the Python wrapper -- the actual
    # Tesseract binary is a separate OS-level install and is frequently not on
    # PATH (very common on Windows: pytesseract imports fine, then every call
    # fails with `TesseractNotFoundError`, which looks identical to "OCR is
    # missing" from the outside). Point pytesseract at an explicit path if the
    # operator configured one, instead of silently trusting PATH.
    try:
        from backend.config import get_settings

        cmd = get_settings().tesseract_cmd
        if cmd:
            pytesseract.pytesseract.tesseract_cmd = cmd
    except Exception:  # pragma: no cover - config not available in some test contexts
        pass
    return pytesseract


def rasterize_page(page, dpi: float = DEFAULT_OCR_DPI) -> "Image.Image":
    """Rasterize a PyMuPDF page to a PIL Image at the given DPI."""
    from PIL import Image

    zoom = dpi / 72.0
    matrix = _matrix(zoom)
    pix = page.get_pixmap(matrix=matrix, alpha=False)
    mode = "RGB" if pix.n < 4 else "RGBA"
    img = Image.frombytes(mode, (pix.width, pix.height), pix.samples)
    return img.convert("RGB")


def _matrix(zoom: float):
    import fitz  # type: ignore

    return fitz.Matrix(zoom, zoom)


def ocr_page(
    image: "Image.Image",
    page_number: int,
    dpi: float = DEFAULT_OCR_DPI,
    min_confidence: float = 0.0,
) -> list[RawTextItem]:
    """
    Run OCR on a rasterized page image and return text items with bounding
    boxes converted back to PAGE_POINTS space.

    Returns TWO granularities, both in the same list:
      - one `RawTextItem` per individual word (Tesseract's native output
        granularity) -- precise bounding boxes, good for numeric-value
        candidates that need tight position information.
      - one additional `RawTextItem` per OCR *line* (`is_line_group=True`),
        built by grouping words sharing the same (block_num, par_num,
        line_num) and joining them in reading order. Multi-word phrase
        regexes ("SITE PLAN", "WIDE ROAD", area/height table labels) need
        this -- matching them against individual words can never succeed,
        since "SITE" and "PLAN" are two separate word-level detections.
    """
    pytesseract = _import_ocr_stack()
    try:
        data = pytesseract.image_to_data(image, output_type=pytesseract.Output.DICT)
    except Exception as exc:
        # Previously swallowed to `[]` here with only a logger.warning, which
        # meant a genuine, fixable problem (most commonly: pytesseract
        # imported fine but the `tesseract` binary itself isn't installed or
        # isn't on PATH -- pytesseract.TesseractNotFoundError, easy to
        # confuse with "OCR just found nothing") was invisible to callers.
        # Call sites already wrap this in their own try/except and turn it
        # into a proper warning message, so re-raising here surfaces the
        # real cause instead of hiding it.
        logger.warning("OCR failed for page", extra={"page": page_number, "error": str(exc)})
        raise

    items: list[RawTextItem] = []
    # (block_num, par_num, line_num) -> ordered list of (word_num, text, x, y, w, h, confidence)
    line_groups: dict[tuple[int, int, int], list[tuple[int, str, int, int, int, int, float]]] = {}

    n = len(data.get("text", []))
    for i in range(n):
        text = data["text"][i]
        if not text or not text.strip():
            continue
        try:
            conf = float(data["conf"][i])
        except (ValueError, TypeError):
            conf = -1.0
        confidence = max(0.0, min(1.0, conf / 100.0)) if conf >= 0 else 0.0
        if confidence < min_confidence:
            continue
        x, y, w, h = (
            data["left"][i],
            data["top"][i],
            data["width"][i],
            data["height"][i],
        )
        bbox = pixel_bbox_to_page_bbox(x, y, x + w, y + h, dpi)
        items.append(
            RawTextItem(
                text=text,
                bounding_box=bbox,
                page=page_number,
                source=SourceKind.OCR,
                ocr_confidence=confidence,
            )
        )
        key = (
            data.get("block_num", [0] * n)[i],
            data.get("par_num", [0] * n)[i],
            data.get("line_num", [0] * n)[i],
        )
        word_num = data.get("word_num", [0] * n)[i]
        line_groups.setdefault(key, []).append((word_num, text, x, y, w, h, confidence))

    for words in line_groups.values():
        if len(words) < 2:
            continue  # a single-word "line" is identical to its own word-level item already above
        words_sorted = sorted(words, key=lambda t: t[0])
        joined_text = " ".join(w[1] for w in words_sorted)
        min_x = min(w[2] for w in words_sorted)
        min_y = min(w[3] for w in words_sorted)
        max_x = max(w[2] + w[4] for w in words_sorted)
        max_y = max(w[3] + w[5] for w in words_sorted)
        avg_confidence = sum(w[6] for w in words_sorted) / len(words_sorted)
        bbox = pixel_bbox_to_page_bbox(min_x, min_y, max_x, max_y, dpi)
        items.append(
            RawTextItem(
                text=joined_text,
                bounding_box=bbox,
                page=page_number,
                source=SourceKind.OCR,
                ocr_confidence=avg_confidence,
                is_line_group=True,
            )
        )
    return items


def needs_ocr(has_native_text: bool, native_text_char_count: int, force: bool = False) -> bool:
    """
    Decide whether a page needs the OCR fallback.

    Used when: PDF is scanned, vector text is missing, extraction is
    incomplete, or the caller explicitly requests raster interpretation.
    """
    if force:
        return True
    return (not has_native_text) or native_text_char_count < 3


__all__ = [
    "DEFAULT_OCR_DPI",
    "rasterize_page",
    "ocr_page",
    "needs_ocr",
]
