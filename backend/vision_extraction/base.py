from __future__ import annotations

import json
import math
import re
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any

from backend.config import get_settings
from backend.tools.logging_config import get_logger
from backend.vision_extraction.page_renderer import render_pdf_pages
from backend.vision_extraction.prompts import ARCHITECTURAL_PLAN_PROMPT, SITE_PLAN_FOCUS_PROMPT
from backend.vision_extraction.spatial import vision_bbox_to_page_points
from backend.schemas.vision import VisionDocumentResult, VisionPageResult

# Grounding tolerance (PDF points) between a vision-claimed dimension's bbox
# and the nearest matching native-text span with the same value. Generous,
# since VLM bboxes are approximate -- this only needs to reject "value exists
# somewhere totally unrelated on the sheet" (e.g. a floor-height label being
# reused to justify a fabricated setback), not demand pixel-perfect alignment.
_GROUNDING_PROXIMITY_TOLERANCE_PT = 250.0

logger = get_logger(__name__)


class BaseArchitecturalPlanExtractor(ABC):
    """
    Shared plumbing for optional local vision-language semantic
    extractors (page rendering, prompt formatting, JSON-from-model-text
    parsing, page-loop error handling). Concrete backends only need to
    implement `_load()` (lazy model/processor load) and
    `_generate_raw_response()` (one image + prompt -> raw model text).

    This class is intentionally lazy: importing the backend does not
    require the large vision-model dependencies unless vision extraction
    is enabled and a concrete backend is actually instantiated.
    """

    #: Overridden per-backend; used when settings.vision_model_name is
    #: not explicitly set, so switching backends doesn't require also
    #: remembering to update a hardcoded model name in config.
    DEFAULT_MODEL_NAME: str = ""

    def __init__(self, model_name: str | None = None):
        settings = get_settings()
        self.model_name = model_name or settings.vision_model_name or self.DEFAULT_MODEL_NAME

    @abstractmethod
    def _load(self) -> None:
        """Lazily import dependencies and load the model/processor. Must be idempotent."""

    @abstractmethod
    def _generate_raw_response(self, image_path: Path, prompt: str, max_new_tokens: int | None = None) -> str:
        """Run the model on one rendered page image and return its raw text response."""

    @staticmethod
    def _native_number_spans_by_page(
        pdf_path: Path,
    ) -> dict[int, tuple[list[tuple[str, tuple[float, float]]], float, float]]:
        """
        Pull every numeric-looking text span out of the PDF's *native* text
        layer, keyed by 1-based page number, as (token, (center_x, center_y))
        in PDF point space, alongside that page's (width_pts, height_pts).
        Used as a grounding check against vision output. Returns an empty
        dict (grounding skipped everywhere) if the page can't be read
        natively (e.g. a scanned page with no text layer) -- this is a
        "don't trust an ungrounded number" safeguard, not a substitute for
        OCR, so it must never be used to wipe out a scanned page's results.
        """
        try:
            import fitz  # type: ignore
        except ImportError:
            return {}

        number_re = re.compile(r"\d+\.?\d*")
        spans_by_page: dict[int, tuple[list[tuple[str, tuple[float, float]]], float, float]] = {}
        try:
            with fitz.open(pdf_path) as doc:
                for page_index in range(doc.page_count):
                    page = doc.load_page(page_index)
                    spans: list[tuple[str, tuple[float, float]]] = []
                    for block in page.get_text("dict").get("blocks", []):
                        for line in block.get("lines", []):
                            for span in line.get("spans", []):
                                for token in number_re.findall(span.get("text", "")):
                                    x0, y0, x1, y1 = span["bbox"]
                                    spans.append((token, ((x0 + x1) / 2, (y0 + y1) / 2)))
                    spans_by_page[page_index + 1] = (spans, float(page.rect.width), float(page.rect.height))
        except Exception:
            logger.exception("native text extraction for vision grounding failed")
            return {}
        return spans_by_page

    @staticmethod
    def _value_tokens(value: float) -> set[str]:
        candidates = {f"{value:.2f}", f"{value:.1f}", f"{value:g}"}
        if 0 < value < 1:
            # Common "leading-dot" convention on these drawings (0.47 -> ".47").
            candidates.add(f"{value:.2f}"[1:])
            candidates.add(f"{value:.1f}"[1:])
        return candidates

    @staticmethod
    def _model_bbox_to_page_points(
        bbox: list[float] | None, page_width_pts: float, page_height_pts: float
    ) -> tuple[float, float] | None:
        """
        Convert a vision-model bbox to a (center_x, center_y) point in PDF
        page space. Thin wrapper around the single shared, auto-detecting
        conversion in `vision_extraction.spatial.vision_bbox_to_page_points`
        (previously this method had its own private copy of the same
        logic, which drifted from `spatial.py`'s -- see that function's
        docstring for the full explanation of the coordinate-space bug
        this guards against).
        """
        box = vision_bbox_to_page_points(bbox, page_width_pts, page_height_pts)
        if box is None:
            return None
        return box.center.x, box.center.y

    @classmethod
    def _is_item_grounded(
        cls,
        item: dict[str, Any],
        native_spans: list[tuple[str, tuple[float, float]]],
        page_width_pts: float,
        page_height_pts: float,
    ) -> bool:
        """
        Hard gate: does this value appear ANYWHERE in the page's real text
        layer at all? This alone is what reliably catches a fully invented
        value (nothing on the page prints "1.96" anywhere) without false-
        negatives on genuine values.

        NOTE: a stricter, bbox-proximity version was tried here (does the
        value appear *near* the location the model claims) and rejected --
        see `_model_bbox_to_page_points`'s docstring. The model's bbox
        coordinates are on a normalized 0-1000 grid (not raw pixels as the
        prompt requests), and even after correcting for that, the model's
        self-reported bbox positions aren't precise enough to use as a hard
        gate: it dropped genuinely correct dimensions while, by
        coincidence, keeping one of the fabricated setbacks (a real "3.00"
        floor-height label elsewhere on the sheet happened to fall inside
        the tolerance radius of the claimed setback bbox). Presence-anywhere
        is the reliable, low-false-negative check; true proximity grounding
        would need better-calibrated model bboxes or a second detector to
        anchor against, not a bigger tolerance.
        """
        value = item.get("value")
        evidence = str(item.get("evidence") or "")

        # Recover common SmolVLM failure mode: evidence contains the real
        # number (e.g. "9.14") but the numeric field is emitted as 0.0.
        if (value is None or float(value) == 0.0) and evidence:
            m = re.search(r"\d+(?:\.\d+)?", evidence)
            if m:
                value = float(m.group())
                item["value"] = value

        if value is None:
            return True
        if not native_spans:
            return True
        wanted = cls._value_tokens(float(value))
        return any(token in wanted for token, _center in native_spans)

    def _ground_against_native_text(
        self,
        payload: dict[str, Any],
        native_spans: list[tuple[str, tuple[float, float]]],
        page_width_pts: float = 0.0,
        page_height_pts: float = 0.0,
    ) -> dict[str, Any]:
        """
        Drop any dimension/area whose numeric value doesn't appear, near the
        bbox it's claimed at, in the PDF's native text layer -- this is what
        catches a VLM inventing a plausible-looking value (e.g. a "typical"
        3.00m setback) that either isn't printed anywhere on the sheet, or
        only exists as an unrelated label elsewhere (e.g. a floor-height
        dimension being repurposed to justify a fabricated setback).
        Regions are left alone (they aren't numeric claims in the same way).
        """
        warnings = list(payload.get("warnings") or [])
        for collection in ("dimensions", "areas"):
            kept = []
            for item in payload.get(collection, []) or []:
                if self._is_item_grounded(item, native_spans, page_width_pts, page_height_pts):
                    kept.append(item)
                else:
                    warnings.append(
                        f"Dropped ungrounded {collection[:-1]} "
                        f"{item.get('type')}={item.get('value')}: no matching text "
                        "found anywhere in the PDF's native text layer "
                        "(likely hallucinated)."
                    )
            payload[collection] = kept
        payload["warnings"] = warnings
        return payload

    @staticmethod
    def _extract_json(text: str) -> dict[str, Any]:
        text = text.strip()

        # Defensive stripping for reasoning/"thinking" models that inline
        # chain-of-thought in the response wrapped in <think>...</think>
        # (e.g. Groq's qwen/qwen3.6-27b in 'raw' reasoning_format, or any
        # other reasoning-capable model/provider that does this by
        # default). The 'api' backend requests 'hidden' reasoning_format
        # by default specifically to avoid this, but this stays as a
        # second line of defense for providers/overrides where a
        # <think> block still shows up in content.
        text = re.sub(r"<think>.*?</think>", "", text, flags=re.I | re.S).strip()

        if text.startswith("```"):
            text = re.sub(r"^```(?:json)?\s*", "", text, flags=re.I)
            text = re.sub(r"\s*```$", "", text)

        try:
            return json.loads(text)
        except json.JSONDecodeError:
            start = text.find("{")
            end = text.rfind("}")
            if start >= 0 and end > start:
                return json.loads(text[start : end + 1])
            raise

    def analyze_image(
        self,
        image_path: Path,
        page_number: int,
        native_spans: list[tuple[str, tuple[float, float]]] | None = None,
        page_width_pts: float = 0.0,
        page_height_pts: float = 0.0,
        *,
        ground_against_native_text: bool = True,
        prompt_override: str | None = None,
        max_new_tokens: int | None = None,
    ) -> VisionPageResult:
        self._load()
        prompt = (prompt_override or ARCHITECTURAL_PLAN_PROMPT).replace("PAGE_NUMBER", str(page_number))
        print(f"  [page {page_number}] generating response (this is the slow step on CPU)...", flush=True)
        response = self._generate_raw_response(image_path, prompt, max_new_tokens=max_new_tokens)
        print(f"  [page {page_number}] response received ({len(response)} chars), parsing JSON...", flush=True)
        payload = self._extract_json(response)
        # Grounding is an optional safety/production filter. Validation mode
        # deliberately disables it so Vision remains an independent evidence
        # source and CV/native text cannot silently delete a correct VLM value.
        if ground_against_native_text:
            payload = self._ground_against_native_text(
                payload, native_spans or [], page_width_pts, page_height_pts
            )
        return VisionPageResult.model_validate({
            **payload,
            "page_number": page_number,
            "raw_response": response,
            "page_width_pts": page_width_pts,
            "page_height_pts": page_height_pts,
        })

    @staticmethod
    def _crop_site_plan_focus(
        image_path: Path,
        site_bbox: "BoundingBox",
        page_width_pts: float,
        page_height_pts: float,
    ) -> tuple[Path, float, float, float, float] | None:
        """Create a focused site-plan image from the model's own SITE_PLAN bbox.

        This second pass is used only for independent Vision validation.  The crop
        is derived from the Vision model's first-pass region, not from PDF text/CV,
        so the second pass remains an independent Vision-only evidence path.
        """
        try:
            from PIL import Image
        except ImportError:
            return None
        if page_width_pts <= 0 or page_height_pts <= 0:
            return None
        try:
            with Image.open(image_path) as img:
                w_px, h_px = img.size
                # Widened from 12% to 25% (min bumped 12pt -> 24pt). The
                # first-pass SITE_PLAN bbox this crop is built from is itself
                # a VLM judgment and isn't perfectly stable call-to-call --
                # confirmed by run-to-run drift in the focus pass's own
                # readings on an unchanged PDF (12.22 vs 9.14 for plot.depth,
                # 2.25/1.54 vs 1.00/1.00 for setbacks) even at
                # `vision_api_temperature=0.0`. A tight crop margin means a
                # slightly-off first-pass bbox can crop a real dimension
                # label OUT of frame, or crop an unrelated adjacent label IN
                # -- and the second pass then reads whatever pixels it was
                # actually given with full confidence, no way to tell from
                # its output alone that the crop itself was off. This won't
                # fully eliminate bbox-detection instability, but it makes
                # the crop more tolerant of it at zero extra API cost.
                pad_x = max(24.0, site_bbox.width * 0.25)
                pad_y = max(24.0, site_bbox.height * 0.25)
                x0 = max(0.0, site_bbox.min_x - pad_x)
                y0 = max(0.0, site_bbox.min_y - pad_y)
                x1 = min(page_width_pts, site_bbox.max_x + pad_x)
                y1 = min(page_height_pts, site_bbox.max_y + pad_y)
                px0 = max(0, int(round(x0 / page_width_pts * w_px)))
                py0 = max(0, int(round(y0 / page_height_pts * h_px)))
                px1 = min(w_px, int(round(x1 / page_width_pts * w_px)))
                py1 = min(h_px, int(round(y1 / page_height_pts * h_px)))
                if px1 <= px0 or py1 <= py0:
                    return None
                crop_path = image_path.with_name(f"{image_path.stem}_site_focus.png")
                img.crop((px0, py0, px1, py1)).save(crop_path, format="PNG")
                # Return the crop's page-point rectangle for bbox remapping.
                return crop_path, x0, y0, x1, y1
        except Exception:
            return None

    @staticmethod
    def _remap_focus_bbox(
        bbox: list[float] | None,
        crop_bbox_pts: tuple[float, float, float, float],
        page_width_pts: float,
        page_height_pts: float,
    ) -> list[float] | None:
        if not bbox or len(bbox) != 4:
            return None
        cx0, cy0, cx1, cy1 = crop_bbox_pts
        cw, ch = max(cx1 - cx0, 1e-6), max(cy1 - cy0, 1e-6)
        x0, y0, x1, y1 = [float(v) for v in bbox]
        px0 = cx0 + min(x0, x1) / 1000.0 * cw
        py0 = cy0 + min(y0, y1) / 1000.0 * ch
        px1 = cx0 + max(x0, x1) / 1000.0 * cw
        py1 = cy0 + max(y0, y1) / 1000.0 * ch
        return [
            max(0.0, min(1000.0, px0 / page_width_pts * 1000.0)),
            max(0.0, min(1000.0, py0 / page_height_pts * 1000.0)),
            max(0.0, min(1000.0, px1 / page_width_pts * 1000.0)),
            max(0.0, min(1000.0, py1 / page_height_pts * 1000.0)),
        ]

    def _independent_site_focus_pass(
        self,
        first_pass: VisionPageResult,
        image_path: Path,
        page_number: int,
        page_width_pts: float,
        page_height_pts: float,
    ) -> VisionPageResult | None:
        if not first_pass.regions:
            return None
        site_regions = [
            vision_bbox_to_page_points(r.bbox, page_width_pts, page_height_pts)
            for r in first_pass.regions
            if r.type.upper() == "SITE_PLAN"
        ]
        site_regions = [r for r in site_regions if r is not None]
        if not site_regions:
            return None
        site_bbox = max(site_regions, key=lambda b: b.width * b.height)
        crop = self._crop_site_plan_focus(image_path, site_bbox, page_width_pts, page_height_pts)
        if crop is None:
            return None
        crop_path, x0, y0, x1, y1 = crop
        focus = self.analyze_image(
            crop_path,
            page_number,
            native_spans=[],
            page_width_pts=max(x1 - x0, 1e-6),
            page_height_pts=max(y1 - y0, 1e-6),
            ground_against_native_text=False,
            prompt_override=SITE_PLAN_FOCUS_PROMPT,
            max_new_tokens=get_settings().vision_focus_max_new_tokens,
        )
        payload = focus.model_dump(mode="python")
        crop_bbox = (x0, y0, x1, y1)
        for region in payload.get("regions", []):
            region["bbox"] = self._remap_focus_bbox(region.get("bbox"), crop_bbox, page_width_pts, page_height_pts)
        for dim in payload.get("dimensions", []):
            dim["bbox"] = self._remap_focus_bbox(dim.get("bbox"), crop_bbox, page_width_pts, page_height_pts)
        for area in payload.get("areas", []):
            # Area bboxes live in the same normalized crop coordinate system as dimensions.
            area["bbox"] = self._remap_focus_bbox(area.get("bbox"), crop_bbox, page_width_pts, page_height_pts)
        payload["raw_response"] = (first_pass.raw_response or "") + "\n\n[SITE_PLAN_FOCUS_PASS]\n" + (focus.raw_response or "")
        payload["page_number"] = page_number
        payload["page_width_pts"] = page_width_pts
        payload["page_height_pts"] = page_height_pts
        return VisionPageResult.model_validate(payload)

    def analyze_pdf(
        self,
        pdf_path: Path,
        *,
        ground_against_native_text: bool = True,
    ) -> VisionDocumentResult:
        settings = get_settings()
        render_dir = settings.resolve(settings.upload_dir) / "vision_rendered" / pdf_path.stem
        print(f"Rendering '{pdf_path.name}' at {settings.vision_render_dpi} DPI...", flush=True)
        page_files = render_pdf_pages(pdf_path, render_dir, dpi=settings.vision_render_dpi)
        print(f"Rendered {len(page_files)} page(s) to {render_dir}", flush=True)
        native_spans_by_page = self._native_number_spans_by_page(pdf_path)

        pages: list[VisionPageResult] = []
        warnings: list[str] = []
        for item in page_files:
            print(f"Analyzing page {item['page_number']}/{len(page_files)}...", flush=True)
            spans, native_page_w, native_page_h = native_spans_by_page.get(
                item["page_number"], ([], 0.0, 0.0)
            )
            # Prefer the page size derived from the actual rendered PNG's
            # own pixel dimensions over the native-text-layer page rect:
            # it's available even for scanned pages with no text layer at
            # all (native_spans_by_page returns 0.0/0.0 there), and it's
            # guaranteed consistent with the image the model actually saw.
            width_px = item.get("width_px")
            height_px = item.get("height_px")
            if width_px and height_px:
                page_w = float(width_px) * 72.0 / settings.vision_render_dpi
                page_h = float(height_px) * 72.0 / settings.vision_render_dpi
            else:
                page_w, page_h = native_page_w, native_page_h
            try:
                first_pass = self.analyze_image(
                    Path(item["image_path"]),
                    item["page_number"],
                    spans,
                    page_w,
                    page_h,
                    ground_against_native_text=ground_against_native_text,
                )
                # Independent validation gets a second, Vision-only site-plan
                # pass.  This is intentionally disabled for production/grounded
                # mode so native-PDF grounding remains the safety filter there.
                focus_pass = None
                if not ground_against_native_text:
                    try:
                        focus_pass = self._independent_site_focus_pass(
                            first_pass, Path(item["image_path"]), item["page_number"], page_w, page_h
                        )
                    except Exception as focus_exc:
                        warnings.append(
                            f"page {item['page_number']}: site-plan focus pass failed: {focus_exc}"
                        )
                if focus_pass is not None:
                    first_pass.dimensions.extend(focus_pass.dimensions)
                    first_pass.regions.extend(focus_pass.regions)
                    first_pass.areas.extend(focus_pass.areas)
                    first_pass.warnings.extend(focus_pass.warnings)
                    first_pass.raw_response = focus_pass.raw_response
                pages.append(first_pass)
            except Exception as exc:
                logger.exception("vision page analysis failed", extra={"page": item["page_number"]})
                warnings.append(f"page {item['page_number']}: vision analysis failed: {exc}")
                print(f"  [page {item['page_number']}] FAILED: {exc}", flush=True)

        return VisionDocumentResult(
            pages=pages,
            model_name=self.model_name,
            warnings=warnings,
        )