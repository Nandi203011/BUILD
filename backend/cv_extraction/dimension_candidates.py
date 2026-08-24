"""
Dimension candidate detection.

A number is NOT automatically a dimension. This module only flags text
that LOOKS like a measurement (a numeric value plausibly paired with a
length/area unit, or a feet-inches mark) and preserves it as an
unresolved `DimensionCandidate` — raw text, numeric value, unit hint,
bounding box, page, nearby geometry, orientation, confidence.

It NEVER decides what the number means (FRONT_SETBACK, PLOT_WIDTH,
BUILDING_WIDTH, ...). That semantic assignment is a spatial-reasoning /
Phase 3 concern.
"""

from __future__ import annotations

import math
import re

from backend.cv_extraction.raw_types import DimensionCandidate, RawLine, RawTextItem

# Feet-inches marks: 12'-6", 12' 6", 12'6"
_FEET_INCHES_RE = re.compile(
    r"(?P<feet>\d+(?:\.\d+)?)\s*'\s*-?\s*(?P<inches>\d+(?:\.\d+)?)\s*\"?"
)

# Plain numeric value optionally followed by a unit token.
_NUMERIC_UNIT_RE = re.compile(
    r"(?P<value>\d*\.\d+|\d+)\s*(?P<unit>mm|cm|m|ft|feet|in|inch|inches|sq\.?\s?ft|sqft|"
    r"sq\.?\s?m|sqm|sq\.?\s?mt|'|\")?",
    re.IGNORECASE,
)

_UNIT_NORMALIZATION = {
    "mm": "mm",
    "cm": "cm",
    "m": "m",
    "ft": "ft",
    "feet": "ft",
    "in": "in",
    "inch": "in",
    "inches": "in",
    "'": "ft",
    '"': "in",
}


def _normalize_unit(raw_unit: str | None) -> str | None:
    if not raw_unit:
        return None
    key = raw_unit.strip().lower().replace(" ", "")
    if key in ("sqft", "sq.ft", "sq.ft."):
        return "sq_ft"
    if key in ("sqm", "sq.m", "sq.mt", "sq.m."):
        return "sq_m"
    return _UNIT_NORMALIZATION.get(key, key or None)


def _confidence_for(unit: str | None, is_feet_inches: bool, ocr_confidence: float | None) -> float:
    base = 0.55
    if is_feet_inches:
        base = 0.85
    elif unit is not None:
        base = 0.75
    if ocr_confidence is not None:
        # Blend text-plausibility confidence with OCR read confidence.
        base = (base + ocr_confidence) / 2.0
    return round(min(1.0, max(0.0, base)), 3)


def detect_dimension_candidates(
    text_items: list[RawTextItem],
    lines: list[RawLine] | None = None,
    proximity_pts: float = 24.0,
    max_lines_considered: int | None = None,
) -> list[DimensionCandidate]:
    """
    Scan raw text items for plausible numeric measurements.

    `lines` (dimension/geometry lines already extracted) is used to
    populate `nearby_geometry_ids` (every plausible nearby line, sorted
    best-association-first — see `_nearby_geometry_ids`) for
    auditability. It does not gate whether something counts as a
    candidate, since not every dimension annotation sits next to an
    explicit dimension line. The single best association (if confident
    enough) is exposed separately via `nearby_geometry_ids[0]` — callers
    must not assume "first found" is "best"; it is guaranteed to be
    best-scored, not first-encountered.
    """
    lines = lines or []
    # (original_index, RawLine) pairs — indices returned in
    # `nearby_geometry_ids` are always original indices into the `lines`
    # list the CALLER passed in, even if we bound the search below to
    # avoid an unrestricted every-text x every-line comparison.
    indexed_lines = list(enumerate(lines))
    if max_lines_considered is not None and len(indexed_lines) > max_lines_considered:
        # Bound worst-case cost: keep the longest lines, which are the
        # most plausible dimension/boundary lines anyway.
        indexed_lines = sorted(
            indexed_lines, key=lambda pair: pair[1].line.length, reverse=True
        )[:max_lines_considered]
    candidates: list[DimensionCandidate] = []

    for item in text_items:
        text = item.text.strip()
        if not text:
            continue

        fi_match = _FEET_INCHES_RE.search(text)
        if fi_match:
            feet = float(fi_match.group("feet"))
            inches = float(fi_match.group("inches"))
            if 0 <= inches < 12:
                numeric_value = feet + inches / 12.0
                candidates.append(
                    _build_candidate(
                        item=item,
                        numeric_value=numeric_value,
                        unit_hint="ft_in",
                        indexed_lines=indexed_lines,
                        proximity_pts=proximity_pts,
                        is_feet_inches=True,
                    )
                )
                continue

        num_match = _NUMERIC_UNIT_RE.search(text)
        if not num_match:
            continue
        try:
            value = float(num_match.group("value"))
        except ValueError:
            continue
        unit = _normalize_unit(num_match.group("unit"))
        candidates.append(
            _build_candidate(
                item=item,
                numeric_value=value,
                unit_hint=unit,
                indexed_lines=indexed_lines,
                proximity_pts=proximity_pts,
                is_feet_inches=False,
            )
        )

    return candidates


def _build_candidate(
    item: RawTextItem,
    numeric_value: float,
    unit_hint: str | None,
    indexed_lines: list[tuple[int, RawLine]],
    proximity_pts: float,
    is_feet_inches: bool,
) -> DimensionCandidate:
    nearby_ids = _nearby_geometry_ids(item, indexed_lines, proximity_pts)
    confidence = _confidence_for(unit_hint, is_feet_inches, item.ocr_confidence)
    return DimensionCandidate(
        raw_text=item.text,
        numeric_value=numeric_value,
        unit_hint=unit_hint,
        bounding_box=item.bounding_box,
        page=item.page,
        source=item.source,
        nearby_geometry_ids=nearby_ids,
        orientation_degrees=item.orientation_degrees,
        confidence=confidence,
    )


def _line_association_score(item: RawTextItem, raw_line: RawLine, distance: float, proximity_pts: float) -> float:
    """
    Score a candidate geometry-line association for a dimension-text
    annotation (FIX #4, phase3.1) — replaces "first nearby line wins"
    with a weighted combination of:

      - distance: closer is better (dominant term)
      - orientation alignment: a dimension label is typically written
        parallel to (running alongside) its dimension line, so alignment
        between the text's own orientation and the line's orientation is
        rewarded when the text carries an orientation reading
      - line length: very short stray segments (hatching, noise) score
        lower than substantial lines, all else equal

    Returns a score in [0, 1]; higher is a stronger association.
    """
    if proximity_pts <= 0:
        return 0.0
    distance_score = max(0.0, 1.0 - (distance / proximity_pts))

    orientation_score = 0.5  # neutral when we have no orientation reading
    if item.orientation_degrees is not None:
        line = raw_line.line
        dx, dy = line.end.x - line.start.x, line.end.y - line.start.y
        line_deg = math.degrees(math.atan2(dy, dx)) % 180.0
        text_deg = item.orientation_degrees % 180.0
        diff = abs(line_deg - text_deg)
        diff = min(diff, 180.0 - diff)
        orientation_score = 1.0 - (diff / 90.0)

    length = raw_line.line.length
    length_score = min(1.0, length / 20.0)  # lines under ~20pt are treated as weak evidence

    return (0.6 * distance_score) + (0.25 * orientation_score) + (0.15 * length_score)


def _nearby_geometry_ids(
    item: RawTextItem, indexed_lines: list[tuple[int, RawLine]], proximity_pts: float
) -> list[int]:
    """
    Returns original-index geometry line ids near this dimension text,
    sorted BEST-ASSOCIATION-FIRST (see `_line_association_score`) — never
    "first encountered in the source list". Callers that only want the
    single strongest association should take `result[0]`, but should
    still gate on a minimum confidence rather than trusting any match.
    """
    if not indexed_lines:
        return []
    center = item.bounding_box.center
    scored: list[tuple[int, float]] = []
    for idx, raw_line in indexed_lines:
        if raw_line.page != item.page:
            continue
        distance = _point_line_distance(center.x, center.y, raw_line)
        if distance > proximity_pts:
            continue
        score = _line_association_score(item, raw_line, distance, proximity_pts)
        scored.append((idx, score))
    scored.sort(key=lambda pair: pair[1], reverse=True)
    return [idx for idx, _score in scored]


def _point_line_distance(px: float, py: float, raw_line: RawLine) -> float:
    line = raw_line.line
    ax, ay = line.start.x, line.start.y
    bx, by = line.end.x, line.end.y
    dx, dy = bx - ax, by - ay
    length_sq = dx * dx + dy * dy
    if length_sq == 0:
        return ((px - ax) ** 2 + (py - ay) ** 2) ** 0.5
    t = max(0.0, min(1.0, ((px - ax) * dx + (py - ay) * dy) / length_sq))
    proj_x, proj_y = ax + t * dx, ay + t * dy
    return ((px - proj_x) ** 2 + (py - proj_y) ** 2) ** 0.5


def _point_near_line(px: float, py: float, raw_line: RawLine, tolerance: float) -> bool:
    return _point_line_distance(px, py, raw_line) <= tolerance


__all__ = ["detect_dimension_candidates"]
