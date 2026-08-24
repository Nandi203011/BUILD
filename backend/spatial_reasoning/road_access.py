"""
Road + access evidence resolution.

Picks the best road candidate for the plot's page (if any) and collects
text evidence for gate/main-entry/access mentions, which `front_side.py`
uses as fallback signals when there's no explicit road candidate.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Optional

from backend.schemas.candidates import RoadCandidate
from backend.schemas.evidence import TextEvidence, ValueField
from backend.schemas.geometry import BoundingBox, CoordinateSpace, NormalizedGeometry
from backend.schemas.extraction import ExtractionResult

_ACCESS_PATTERNS = {
    "road": re.compile(r"\broad\b", re.I),
    "street": re.compile(r"\bstreet\b", re.I),
    "access": re.compile(r"\baccess\b", re.I),
    "main_entry": re.compile(r"\bmain\s*entry\b|\bmain\s*entrance\b|\bentry\b|\bentrance\b", re.I),
    "gate": re.compile(r"\bgate\b", re.I),
    "front": re.compile(r"\bfront\b", re.I),
}


@dataclass
class AccessEvidence:
    kind: str  # matches keys of _ACCESS_PATTERNS
    text_evidence: TextEvidence


def collect_access_evidence(text_evidence: list[TextEvidence], page: Optional[int]) -> list[AccessEvidence]:
    found: list[AccessEvidence] = []
    for t in text_evidence:
        if page is not None and t.page != page:
            continue
        for kind, pattern in _ACCESS_PATTERNS.items():
            if pattern.search(t.raw_text or ""):
                found.append(AccessEvidence(kind=kind, text_evidence=t))
    return found


def best_road_candidate(
    extraction: ExtractionResult, page: Optional[int]
) -> Optional[RoadCandidate]:
    candidates = [
        r for r in extraction.road_candidates if r.geometry and r.geometry.bounding_box is not None
    ]
    if page is not None:
        candidates = [r for r in candidates if r.geometry.source_page == page]
    if not candidates:
        # Fallback for common site-plan drawings where ROAD is printed inside
        # a large road area but OpenCV does not classify that area as a
        # long/thin rectangle. The text anchor is sufficient for front-side
        # orientation; it is deliberately NOT used to estimate road width.
        road_texts = [t for t in extraction.text_evidence if t.page == page and re.search(r"\broad\b", t.raw_text or "", re.I) and t.bounding_box]
        if road_texts:
            t = min(road_texts, key=lambda x: x.bounding_box.width * x.bounding_box.height)
            b = t.bounding_box
            pad = max(b.width, b.height) * 1.5
            bbox = BoundingBox(
                min_x=b.min_x - pad, min_y=b.min_y - pad,
                max_x=b.max_x + pad, max_y=b.max_y + pad,
            )
            return RoadCandidate(
                id=f"text-road-p{page}",
                geometry=NormalizedGeometry(
                    coordinate_space=CoordinateSpace.PAGE_POINTS,
                    bounding_box=bbox,
                    points_per_metre=1.0,
                    source_page=page,
                ),
                confidence_note="ROAD text anchor used only for front-side orientation; no road width inferred.",
                width=ValueField[float].missing("Road width is not explicitly dimensioned; ROAD text is used only for front-side orientation."),
                name_or_label=t.raw_text,
            )
        return None
    # Prefer the widest labeled candidate; road width is only ever a
    # rough page-space proxy here (real conversion happens once scale is known).
    candidates.sort(
        key=lambda r: (r.name_or_label is not None, r.geometry.bounding_box.width * r.geometry.bounding_box.height),
        reverse=True,
    )
    return candidates[0]


__all__ = ["AccessEvidence", "collect_access_evidence", "best_road_candidate"]
