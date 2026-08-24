"""Deterministic, independent CV/native-text site-plan resolver.

This module is intentionally separate from the legacy candidate-resolution
pipeline.  It answers one narrow validation question:

    "Can the PDF/CV side independently recover the canonical site-plan
     dimensions without Vision?"

For vector PDFs it uses only PDF-native text and vector line geometry.  It
never imports the Vision stack.  It detects a SITE PLAN region, reconstructs
nested axis-aligned rectangles from vector lines, anchors explicit dimension
labels to the outer rectangle, derives the building footprint from the inner
rectangle, and assigns setback labels by their spatial position in the gaps.

The resolver is conservative: if the evidence is not strong enough it returns
None for that field instead of guessing.
"""
from __future__ import annotations

import math
import re
from dataclasses import dataclass
from typing import Iterable

from backend.cv_extraction.raw_types import RawLine, RawTextItem
from backend.schemas.geometry import BoundingBox
from backend.schemas.independent_measurements import IndependentCVResult, IndependentMeasurement

_SITE_RE = re.compile(r"\bSITE\s+PLAN\b", re.I)
_ROAD_VALUE_RE = re.compile(r"(?P<value>\d+(?:\.\d+)?)\s*(?:m\s*)?(?:WIDE\s+)?R\s*O\s*A\s*D\b", re.I)
_NUM_RE = re.compile(r"(?<![A-Za-z])(?P<value>\d*\.\d+|\d+)(?:\s*(?P<unit>mm|cm|m|ft|feet|in|\"|'))?", re.I)
_FEET_RE = re.compile(r"(?P<ft>\d+(?:\.\d+)?)\s*'\s*(?P<inch>\d+(?:\.\d+)?)?\s*\"?", re.I)
# "2.00X1.35", "1.20 X 1.20", "0.90X2.10" -- door/window/room schedule
# notation ("width X height"), always two numbers joined by a bare X/x with
# no unit. A plot/building edge dimension is never written this way (it's
# always a single number with an optional unit), so any text matching this
# shape is schedule-table noise, not a site-plan dimension. Confirmed on a
# real plan: a "2.00X1.35" window-schedule entry was picked up as
# plot.width=2.0 because nothing filtered this pattern out before the first
# number inside it got treated as a normal edge-dimension candidate.
_SCHEDULE_SIZE_RE = re.compile(r"\d+(?:\.\d+)?\s*[xX]\s*\d+(?:\.\d+)?")


@dataclass(frozen=True)
class _Rect:
    bbox: BoundingBox

    @property
    def width(self) -> float:
        return self.bbox.width

    @property
    def height(self) -> float:
        return self.bbox.height

    @property
    def area(self) -> float:
        return self.width * self.height


def _bbox_gap(a: BoundingBox, b: BoundingBox) -> float:
    dx = max(a.min_x - b.max_x, b.min_x - a.max_x, 0.0)
    dy = max(a.min_y - b.max_y, b.min_y - a.max_y, 0.0)
    return math.hypot(dx, dy)


def _inside(a: BoundingBox, b: BoundingBox, tol: float = 2.0) -> bool:
    return (
        a.min_x >= b.min_x - tol and a.max_x <= b.max_x + tol
        and a.min_y >= b.min_y - tol and a.max_y <= b.max_y + tol
    )


def _cluster_lines(lines: Iterable[RawLine], region: BoundingBox, min_length: float = 35.0):
    horizontals: list[tuple[float, float, float]] = []  # y, x0, x1
    verticals: list[tuple[float, float, float]] = []  # x, y0, y1
    for raw in lines:
        l = raw.line
        x0, x1 = sorted((l.start.x, l.end.x))
        y0, y1 = sorted((l.start.y, l.end.y))
        if not region.intersects(BoundingBox(min_x=x0, min_y=y0, max_x=x1, max_y=y1)):
            continue
        dx, dy = x1 - x0, y1 - y0
        if dx >= min_length and dy <= 1.5:
            horizontals.append(((y0 + y1) / 2.0, x0, x1))
        elif dy >= min_length and dx <= 1.5:
            verticals.append(((x0 + x1) / 2.0, y0, y1))
    return horizontals, verticals


def _coverage(interval_start: float, interval_end: float, segments: list[tuple[float, float]], tol: float = 2.5) -> float:
    if interval_end <= interval_start:
        return 0.0
    clipped = []
    for a, b in segments:
        lo, hi = max(interval_start, a), min(interval_end, b)
        if hi >= lo - tol:
            clipped.append((lo, hi))
    if not clipped:
        return 0.0
    clipped.sort()
    total = 0.0
    cur_a, cur_b = clipped[0]
    for a, b in clipped[1:]:
        if a <= cur_b + tol:
            cur_b = max(cur_b, b)
        else:
            total += max(0.0, cur_b - cur_a)
            cur_a, cur_b = a, b
    total += max(0.0, cur_b - cur_a)
    return total


def _rectangles_from_lines(lines: list[RawLine], region: BoundingBox) -> list[_Rect]:
    hs, vs = _cluster_lines(lines, region)
    # Keep only strong, distinct axis lines.  Architectural drawing sheets
    # often repeat the same edge several times with tiny coordinate noise.
    ys = sorted({round(y, 1) for y, _, _ in hs})
    xs = sorted({round(x, 1) for x, _, _ in vs})
    rects: list[_Rect] = []
    for x0 in xs:
        for x1 in xs:
            if x1 - x0 < 45:
                continue
            for y0 in ys:
                for y1 in ys:
                    if y1 - y0 < 45:
                        continue
                    bbox = BoundingBox(min_x=x0, min_y=y0, max_x=x1, max_y=y1)
                    if bbox.width / max(bbox.height, 1e-6) > 8 or bbox.height / max(bbox.width, 1e-6) > 8:
                        continue
                    top = _coverage(x0, x1, [(a, b) for y, a, b in hs if abs(y - y0) <= 2.0])
                    bottom = _coverage(x0, x1, [(a, b) for y, a, b in hs if abs(y - y1) <= 2.0])
                    left = _coverage(y0, y1, [(a, b) for x, a, b in vs if abs(x - x0) <= 2.0])
                    right = _coverage(y0, y1, [(a, b) for x, a, b in vs if abs(x - x1) <= 2.0])
                    w, h = bbox.width, bbox.height
                    if min(top, bottom) < 0.82 * w or min(left, right) < 0.82 * h:
                        continue
                    rects.append(_Rect(bbox))
    # Deduplicate nearly identical rectangles.
    unique: list[_Rect] = []
    for r in sorted(rects, key=lambda x: x.area, reverse=True):
        def nearly_same(a: BoundingBox, b: BoundingBox) -> bool:
            return (
                abs(a.min_x - b.min_x) <= 3.0 and
                abs(a.min_y - b.min_y) <= 3.0 and
                abs(a.max_x - b.max_x) <= 3.0 and
                abs(a.max_y - b.max_y) <= 3.0
            )
        if any(nearly_same(r.bbox, u.bbox) for u in unique):
            continue
        unique.append(r)
    return unique


def _find_site_anchor(text_items: list[RawTextItem]) -> RawTextItem | None:
    matches = [t for t in text_items if _SITE_RE.search(t.text or "")]
    if not matches:
        return None
    # Prefer the clearest/longest SITE PLAN label.
    return max(matches, key=lambda t: (len(t.text), -t.bounding_box.min_y))


def _site_region(anchor: RawTextItem, page_width: float, page_height: float) -> BoundingBox:
    c = anchor.bounding_box.center
    # Site-plan labels are normally below/alongside the drawing.  Use a
    # generous local window rather than the whole sheet, which is exactly
    # what prevents floor-plan walls and title blocks from becoming CV
    # candidates for this validation.
    return BoundingBox(
        min_x=max(0.0, c.x - 420.0),
        min_y=max(0.0, c.y - 470.0),
        max_x=min(page_width, c.x + 420.0),
        max_y=min(page_height, c.y + 140.0),
    )


def _numbers_in_region(text_items: list[RawTextItem], region: BoundingBox):
    out = []
    for item in text_items:
        if getattr(item, "is_line_group", False):
            # Numeric candidates need the precise per-word bounding box for
            # distance-based edge/gap picking (`_pick_edge_dimension`); a
            # merged multi-word line's wider bbox would add imprecise/
            # duplicate candidates for the same value already present via
            # its own word-level item. Line-group items exist only for
            # phrase matching (site anchor, road/ID/area labels), never for
            # numeric-value extraction -- see `RawTextItem.is_line_group`.
            continue
        if not region.intersects(item.bounding_box):
            continue
        text = item.text.strip()
        if not text:
            continue
        # Skip obvious area statements and IDs.
        if any(tok in text.lower() for tok in ("sq.m", "sqm", "sq.ft", "sqft", "bearing", "scale")):
            continue
        # Skip door/window/room schedule size notation ("2.00X1.35") -- see
        # `_SCHEDULE_SIZE_RE` above. Must come before the feet/inches and
        # general numeric regexes below, since both would otherwise happily
        # extract the first number out of a schedule entry as if it were a
        # real edge dimension.
        if _SCHEDULE_SIZE_RE.search(text):
            continue
        m = _FEET_RE.search(text)
        if m:
            ft = float(m.group("ft")); inch = float(m.group("inch") or 0)
            if inch < 12:
                out.append(((ft + inch / 12.0) * 0.3048, item, "ft_in"))
                continue
        for m in _NUM_RE.finditer(text):
            try:
                value = float(m.group("value"))
            except ValueError:
                continue
            unit = (m.group("unit") or "").lower()
            if unit in ("mm",): value /= 1000.0
            elif unit in ("cm",): value /= 100.0
            elif unit in ("ft", "feet", "'"): value *= 0.3048
            elif unit in ("in", '"'): value *= 0.0254
            if 0 < value <= 60:
                out.append((value, item, unit or "m"))
    return out


def _near(values, target: float, tol: float = 0.08):
    return [v for v in values if abs(v[0] - target) <= tol]


def _pick_edge_dimension(nums, outer: BoundingBox, orientation: str, road_text_boxes: list | None = None):
    candidates = []
    road_text_boxes = road_text_boxes or []
    for value, item, unit in nums:
        text = (item.text or "").strip()
        if re.search(r"r\s*o\s*a\s*d", text, re.I):
            continue
        # The substring check above only catches "road" landing inside the
        # SAME token as the number, which basically never happens with
        # OCR-sourced numbers (Tesseract emits "10" and "ROAD" as separate
        # word-level items -- see `RawTextItem.is_line_group`). Without this,
        # a road-width label like "10m WIDE ROAD" gets misread as a
        # plot/building edge dimension whenever it sits just outside the
        # candidate rectangle, which is exactly where road labels live.
        # `road_text_boxes` comes from scanning ALL text items (word +
        # line-group) for the full "<value> WIDE ROAD" phrase, so this
        # exclusion works regardless of which granularity supplied it.
        if any(item.bounding_box.intersects(rb) for rb in road_text_boxes):
            continue
        c = item.bounding_box.center
        if orientation == "horizontal":
            outside = c.y < outer.min_y - 1.0 or c.y > outer.max_y + 1.0
            overlap = c.x >= outer.min_x - 30 and c.x <= outer.max_x + 30
            d = min(abs(c.y - outer.min_y), abs(c.y - outer.max_y))
            if outside and overlap and d <= 90:
                candidates.append((d, value, item))
        else:
            outside = c.x < outer.min_x - 1.0 or c.x > outer.max_x + 1.0
            overlap = c.y >= outer.min_y - 30 and c.y <= outer.max_y + 30
            d = min(abs(c.x - outer.min_x), abs(c.x - outer.max_x))
            if outside and overlap and d <= 90:
                candidates.append((d, value, item))
    candidates.sort(key=lambda x: x[0])
    return candidates[0][1:] if candidates else None


def _pick_gap_dimension(nums, outer: BoundingBox, inner: BoundingBox, side: str):
    candidates = []
    for value, item, unit in nums:
        c = item.bounding_box.center
        if value > 3.0:
            continue  # setback labels on normal urban plans are small; large labels are plot/building dims.
        if side == "top" and outer.min_y <= c.y <= inner.min_y and outer.min_x <= c.x <= outer.max_x:
            distance = abs(c.y - (outer.min_y + inner.min_y) / 2)
        elif side == "bottom" and inner.max_y <= c.y <= outer.max_y and outer.min_x <= c.x <= outer.max_x:
            distance = abs(c.y - (inner.max_y + outer.max_y) / 2)
        elif side == "left" and outer.min_x <= c.x <= inner.min_x and outer.min_y <= c.y <= outer.max_y:
            distance = abs(c.x - (outer.min_x + inner.min_x) / 2)
        elif side == "right" and inner.max_x <= c.x <= outer.max_x and outer.min_y <= c.y <= outer.max_y:
            distance = abs(c.x - (inner.max_x + outer.max_x) / 2)
        else:
            continue
        candidates.append((distance, value, item))
    candidates.sort(key=lambda x: x[0])
    return candidates[0][1:] if candidates else None


_AREA_LABELS = [
    ("plot.area", re.compile(r"^AREA\s+OF\s+PLOT\s*\(\s*Minimum\s*\)", re.I), "m2"),
    ("plot.area", re.compile(r"^NET\s+AREA\s+OF\s+PLOT", re.I), "m2"),
    ("building.footprint_area", re.compile(r"^PROPOSED\s+COVERAGE\s+AREA", re.I), "m2"),
    ("coverage", re.compile(r"^PROPOSED\s+COVERAGE\s+AREA", re.I), "%"),
    ("far.area", re.compile(r"^PROPOSED\s+FAR\s+AREA", re.I), "m2"),
    ("far", re.compile(r"^ACHIEVED\s+NET\s+FAR\s+AREA", re.I), "ratio"),
    ("far.area", re.compile(r"^ACHIEVED\s+NET\s+FAR\s+AREA", re.I), "m2"),
    ("building.gross_built_up_area", re.compile(r"^PROPOSED\s+BUILTUP\s+AREA", re.I), "m2"),
]
_NUM_ITEM_RE = re.compile(r"(?<![A-Za-z])(?:\d+(?:\.\d+)?|\.\d+)(?![A-Za-z])")


def _nearest_numeric_item(label_item: RawTextItem, items: list[RawTextItem]) -> RawTextItem | None:
    """Find the numeric value in the same table row, normally far to the right."""
    best = None
    best_score = float("inf")
    ly = label_item.bounding_box.center.y
    for item in items:
        if item is label_item:
            continue
        text = (item.text or "").strip()
        if not _NUM_ITEM_RE.fullmatch(text):
            continue
        c = item.bounding_box.center
        dy = abs(c.y - ly)
        dx = c.x - label_item.bounding_box.max_x
        if dx < 80 or dy > 18:
            continue
        score = dy * 20.0 + dx * 0.01
        if score < best_score:
            best_score = score
            best = item
    return best


def _area_measurements(text_items: list[RawTextItem], page_number: int) -> list[IndependentMeasurement]:
    """Extract explicitly labelled area/coverage/FAR values from native PDF text.

    Label/value association is spatial, not a loose regex over the whole page.
    This prevents unrelated numbers (room dimensions, doors, etc.) from being
    accidentally captured as an area value.
    """
    ordered = sorted(text_items, key=lambda t: (t.bounding_box.min_y, t.bounding_box.min_x))
    found: list[IndependentMeasurement] = []
    seen: set[tuple[str, float]] = set()
    for field, pattern, unit in _AREA_LABELS:
        for label_item in ordered:
            label = (label_item.text or "").strip()
            if not pattern.search(label):
                continue
            numeric_item = _nearest_numeric_item(label_item, ordered)
            if numeric_item is None:
                continue
            try:
                numeric_value = float(numeric_item.text.strip())
            except ValueError:
                continue
            if field == "coverage":
                pct_match = re.search(r"\(\s*(\d+(?:\.\d+)?)\s*%", label)
                if not pct_match:
                    continue
                value = float(pct_match.group(1))
                evidence = f"{label} -> {numeric_item.text.strip()}"
            elif field == "far":
                ratio_match = re.search(r"\(\s*(\d+(?:\.\d+)?)\s*\)", label)
                if not ratio_match:
                    continue
                value = float(ratio_match.group(1))
                evidence = label
            else:
                value = numeric_value
                evidence = f"{label} -> {numeric_item.text.strip()}"
            key = (field, round(value, 4))
            if key in seen:
                continue
            seen.add(key)
            found.append(IndependentMeasurement(
                field=field,
                value=value,
                unit=unit,
                source="NATIVE_TEXT",
                confidence=0.99,
                evidence=[evidence],
                note="Explicit labelled area/coverage/FAR value from the PDF native text layer; no Vision input.",
                page=page_number,
                geometry_bbox_pts=[round(numeric_item.bounding_box.min_x,2), round(numeric_item.bounding_box.min_y,2), round(numeric_item.bounding_box.max_x,2), round(numeric_item.bounding_box.max_y,2)],
            ))
    return found

def _measurement(field: str, value: float | None, source: str | None, confidence: float, evidence: list[str], note: str, page: int, bbox: BoundingBox | None = None):
    return IndependentMeasurement(
        field=field,
        value_m=None if value is None else round(float(value), 4),
        source=source,
        confidence=confidence,
        evidence=evidence,
        note=note,
        page=page,
        geometry_bbox_pts=(None if bbox is None else [round(bbox.min_x,2),round(bbox.min_y,2),round(bbox.max_x,2),round(bbox.max_y,2)]),
    )


def extract_site_plan_measurements(
    text_items: list[RawTextItem],
    lines: list[RawLine],
    *,
    page_number: int,
    page_width: float,
    page_height: float,
) -> tuple[list[IndependentMeasurement], BoundingBox | None, float | None, float | None, list[str]]:
    notes: list[str] = []
    anchor = _find_site_anchor(text_items)
    if anchor is None:
        notes.append("no 'SITE PLAN' anchor text found; cannot locate the site-plan sub-region at all.")
        return [], None, None, None, notes
    region = _site_region(anchor, page_width, page_height)
    rects = _rectangles_from_lines(lines, region)
    substantial = [r for r in rects if min(r.width, r.height) >= 70]
    if not substantial:
        notes.append(
            f"anchor found and site-plan region located, but 0 rectangles of sufficient size "
            f"(>=70pt on the shorter side) were reconstructed from {len(lines)} line(s) in that "
            "region -- geometry reconstruction itself is the failure point, before any numeric "
            "labels are even considered."
        )
        return [], region, None, None, notes

    nums = _numbers_in_region(text_items, region)

    # Select the plot from geometry + explicit edge dimensions, not by raw
    # area.  This is the key fix for architectural sheets containing many
    # unrelated rectangles/dimension frames around the actual site plan.
    #
    # IMPORTANT: scan ALL `text_items` here (word-level AND is_line_group
    # merged-line items), not just `nums` -- `nums` deliberately excludes
    # line-group items (see `_numbers_in_region`), and the full phrase
    # "10m WIDE ROAD" almost never appears inside a single OCR word-level
    # token. Restricting this to `nums` meant road labels were silently
    # invisible to the exclusion logic below on any OCR-sourced page,
    # letting a road-width label get picked up as a plot/building edge
    # dimension instead of being excluded.
    road_text_boxes = [
        item.bounding_box for item in text_items
        if region.intersects(item.bounding_box) and _ROAD_VALUE_RE.search(item.text or "")
    ]
    scored_rects = []
    for candidate in substantial:
        wd = _pick_edge_dimension(nums, candidate.bbox, "horizontal", road_text_boxes)
        dd = _pick_edge_dimension(nums, candidate.bbox, "vertical", road_text_boxes)
        nested_for_candidate = []
        for r in substantial:
            if r is candidate or not _inside(r.bbox, candidate.bbox, tol=3.0):
                continue
            ratio = r.area / max(candidate.area, 1.0)
            gaps = [
                r.bbox.min_x - candidate.bbox.min_x,
                candidate.bbox.max_x - r.bbox.max_x,
                r.bbox.min_y - candidate.bbox.min_y,
                candidate.bbox.max_y - r.bbox.max_y,
            ]
            if 0.45 <= ratio <= 0.98 and min(gaps) >= 2.0:
                nested_for_candidate.append(r)
        score = 0.0
        if wd and 5.0 <= wd[0] <= 60.0:
            score += 4.0
        if dd and 5.0 <= dd[0] <= 60.0:
            score += 4.0
        if nested_for_candidate:
            score += 3.0
            inner_probe = max(nested_for_candidate, key=lambda r: r.area)
            gaps = [
                inner_probe.bbox.min_x - candidate.bbox.min_x,
                candidate.bbox.max_x - inner_probe.bbox.max_x,
                inner_probe.bbox.min_y - candidate.bbox.min_y,
                candidate.bbox.max_y - inner_probe.bbox.max_y,
            ]
            if min(gaps) >= 2.0:
                score += 1.0
            # A real building footprint is normally inset on all four sides;
            # dimension frames are often much more asymmetric.
            mean_gap = sum(gaps) / 4.0
            if mean_gap > 0:
                spread = max(gaps) / mean_gap
                if spread <= 1.7:
                    score += 1.0
        # A dimension frame can be larger than the actual plot and may
        # swallow the road-width annotation below the plot.  A true plot
        # boundary must not contain its own road label.
        if any(candidate.bbox.intersects(rb) for rb in road_text_boxes):
            score -= 4.0
        score += min(1.0, candidate.area / max(r.area for r in substantial))
        scored_rects.append((score, candidate, wd, dd, nested_for_candidate))

    scored_rects.sort(key=lambda x: (x[0], x[1].area), reverse=True)
    _score, outer, width_dim, depth_dim, nested = scored_rects[0]
    inner = max(nested, key=lambda r: r.area) if nested else None
    # Avoid accidentally choosing a nearby 1.00/0.80 label if the dimension
    # text is not clearly outside the outer boundary.
    if width_dim is None or width_dim[0] < 2.0:
        width_dim = None
    if depth_dim is None or depth_dim[0] < 2.0:
        depth_dim = None
    if width_dim is None and depth_dim is None:
        notes.append(
            f"picked a candidate rectangle ({len(substantial)} candidate(s) considered near the "
            f"site-plan anchor, score={_score:.1f}), but found NO numeric label within range on "
            "either its horizontal or vertical edges (see `_pick_edge_dimension`'s distance/"
            "overlap/road-exclusion checks) -- plot.width and plot.depth cannot be resolved. "
            "Since scale (pt/m) is derived from plot.width/plot.depth, this also blocks "
            "building.width/depth and all 4 setbacks even if a nested building rectangle exists."
        )
    elif width_dim is None or depth_dim is None:
        missing_side = "horizontal (plot.width)" if width_dim is None else "vertical (plot.depth)"
        notes.append(
            f"picked a candidate rectangle and found one edge dimension, but the {missing_side} "
            "edge had no matching numeric label within range -- that field stays MISSING, and "
            "since scale needs BOTH plot.width and plot.depth, it could not be computed either "
            "(blocking building.width/depth and all 4 setbacks even though one plot dimension "
            "was found)."
        )

    measurements: list[IndependentMeasurement] = []
    plot_w = width_dim[0] if width_dim else None
    plot_d = depth_dim[0] if depth_dim else None
    if plot_w is not None:
        measurements.append(_measurement("plot.width", plot_w, "NATIVE_TEXT", 0.99, [width_dim[1].text.strip()], "Explicit site-plan width label anchored to outer plot rectangle.", page_number, outer.bbox))
    if plot_d is not None:
        measurements.append(_measurement("plot.depth", plot_d, "NATIVE_TEXT", 0.99, [depth_dim[1].text.strip()], "Explicit site-plan depth label anchored to outer plot rectangle.", page_number, outer.bbox))

    scale_samples = []
    if plot_w and outer.width > 0: scale_samples.append(outer.width / plot_w)
    if plot_d and outer.height > 0: scale_samples.append(outer.height / plot_d)
    scale = sum(scale_samples) / len(scale_samples) if scale_samples else None
    scale_conf = 0.99 if len(scale_samples) == 2 and abs(scale_samples[0]-scale_samples[1])/scale < 0.02 else (0.8 if scale else None)

    if inner is not None and scale:
        b_w = inner.width / scale
        b_d = inner.height / scale
        measurements.append(_measurement("building.width", b_w, "VECTOR_GEOMETRY", 0.97, [f"inner rectangle width {inner.width:.2f} pt", f"outer scale {scale:.3f} pt/m"], "Building width derived from the nested building footprint geometry and site-plan scale; no Vision input.", page_number, inner.bbox))
        measurements.append(_measurement("building.depth", b_d, "VECTOR_GEOMETRY", 0.97, [f"inner rectangle height {inner.height:.2f} pt", f"outer scale {scale:.3f} pt/m"], "Building depth derived from the nested building footprint geometry and site-plan scale; no Vision input.", page_number, inner.bbox))

        for side, label in (("top", "setbacks.rear"), ("bottom", "setbacks.front"), ("left", "setbacks.left"), ("right", "setbacks.right")):
            picked = _pick_gap_dimension(nums, outer.bbox, inner.bbox, side)
            if picked:
                val, item = picked
                measurements.append(_measurement(label, val, "NATIVE_TEXT", 0.99, [item.text.strip()], f"Explicit setback label spatially located in the {side} plot/building gap.", page_number, outer.bbox))
            else:
                # Geometry-derived fallback only if the gap is substantial and
                # both edges are clearly parallel. This is still independent CV
                # geometry, but is marked DERIVED and lower confidence.
                if side == "top": gap = (inner.bbox.min_y - outer.bbox.min_y) / scale
                elif side == "bottom": gap = (outer.bbox.max_y - inner.bbox.max_y) / scale
                elif side == "left": gap = (inner.bbox.min_x - outer.bbox.min_x) / scale
                else: gap = (outer.bbox.max_x - inner.bbox.max_x) / scale
                if 0 <= gap <= 5:
                    measurements.append(_measurement(label, gap, "DERIVED", 0.88, [f"{side} geometric gap"], f"No explicit label was associated; setback derived from nested rectangles. Marked lower confidence.", page_number, outer.bbox))
    elif inner is None and scale:
        notes.append(
            "plot.width/depth resolved and scale computed, but no nested (building footprint) "
            "rectangle was found inside the winning outer candidate -- building.width/depth and "
            "all 4 setbacks cannot be derived without one, even though the plot dimensions "
            "themselves succeeded."
        )

    road = None
    # Scan ALL text_items (word + line-group), not just `nums` -- same
    # reasoning as `road_text_boxes` above: the full "<value> WIDE ROAD"
    # phrase needs a merged line-group item to match at all on an
    # OCR-sourced page, since `nums` (word-level only) never contains it.
    for item in text_items:
        if not region.intersects(item.bounding_box):
            continue
        m = _ROAD_VALUE_RE.search(item.text or "")
        if m:
            road = (float(m.group("value")), item)
            break
    if road is not None:
        measurements.append(_measurement("road.width", road[0], "NATIVE_TEXT", 0.99, [road[1].text.strip()], "Explicit road-width label; not inferred from the road rectangle.", page_number, outer.bbox))

    return measurements, outer.bbox, scale, scale_conf, notes


def extract_independent_cv(document_path, document_id: str) -> IndependentCVResult:
    from backend.cv_extraction import ocr_fallback, pdf_native

    doc = pdf_native.open_document(document_path)
    all_measurements: list[IndependentMeasurement] = []
    warnings: list[str] = []
    pages: list[int] = []
    site_page = None
    site_bbox = None
    scale = None
    scale_conf = None
    try:
        for page_number in range(doc.page_count):
            page = doc.load_page(page_number)
            meta = pdf_native.extract_page_metadata(page, page_number)
            text_items = pdf_native.extract_text_items(page, page_number)
            lines, _rects, _polys = pdf_native.extract_vector_geometry(page, page_number)
            image = None
            if not pdf_native.has_sufficient_native_text(text_items):
                try:
                    image = ocr_fallback.rasterize_page(page, dpi=200.0)
                    text_items = ocr_fallback.ocr_page(image, page_number, dpi=200.0)
                except Exception as exc:
                    warnings.append(f"page {page_number+1}: OCR fallback failed: {exc}")
            measurements, bbox, page_scale, page_scale_conf, notes = extract_site_plan_measurements(
                text_items, lines, page_number=page_number, page_width=meta.width_pts, page_height=meta.height_pts
            )
            for n in notes:
                warnings.append(f"page {page_number+1}: {n}")
            # Scanned/mixed plans have no PDF vector lines. Re-run the SAME
            # site-plan resolver over raster line evidence instead of falling
            # back to the legacy global candidate pool. This keeps CV independent
            # and preserves the site-plan spatial semantics.
            if not measurements and image is not None:
                try:
                    from backend.cv_extraction import opencv_geometry
                    cv_evidence = opencv_geometry.geometry_evidence_for_page(image, page_number, dpi=200.0)
                    measurements, bbox, page_scale, page_scale_conf, raster_notes = extract_site_plan_measurements(
                        text_items, cv_evidence["lines"], page_number=page_number,
                        page_width=meta.width_pts, page_height=meta.height_pts
                    )
                    for n in raster_notes:
                        warnings.append(f"page {page_number+1} (raster fallback): {n}")
                    if measurements:
                        warnings.append(f"page {page_number+1}: independent CV used raster line/contour evidence (vector geometry unavailable).")
                except Exception as exc:
                    warnings.append(f"page {page_number+1}: raster CV site-plan fallback failed: {exc}")
            measurements.extend(_area_measurements(text_items, page_number))
            if measurements:
                pages.append(page_number + 1)
                all_measurements.extend(measurements)
                site_page = page_number + 1
                site_bbox = bbox
                scale = page_scale
                scale_conf = page_scale_conf
    finally:
        doc.close()
    if not all_measurements:
        warnings.append("No site-plan geometry with sufficient independent CV/native-text evidence was resolved.")
    return IndependentCVResult(
        document_id=document_id,
        pages_analyzed=pages,
        measurements=all_measurements,
        warnings=warnings,
        site_plan_page=site_page,
        site_plan_bbox_pts=(None if site_bbox is None else [site_bbox.min_x, site_bbox.min_y, site_bbox.max_x, site_bbox.max_y]),
        scale_points_per_metre=scale,
        scale_confidence=scale_conf,
    )
