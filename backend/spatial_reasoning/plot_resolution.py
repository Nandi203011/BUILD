"""
Plot candidate scoring/selection.

Deliberately NOT "largest rectangle = plot". Every candidate on the
winning page is scored across multiple weighted features (area rank,
perimeter, rectangularity, aspect ratio, position, boundary-annotation
completeness, plot/site label proximity, road adjacency, nesting), and
the highest-scoring candidate wins. Confidence reflects the margin
between the winner and the runner-up, not just "a candidate existed".
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Optional

from backend.schemas.candidates import PlotCandidate
from backend.schemas.enums import ConfidenceLevel
from backend.schemas.evidence import TextEvidence
from backend.schemas.extraction import ExtractionResult
from backend.spatial_reasoning import geometry_utils as geo
from backend.vision_extraction.spatial import region_score, semantic_dimension_score, semantic_regions

import re as _re

_LABEL_RE_PLOT = _re.compile(r"\b(plot|site|property)\b", _re.I)
_ROAD_RE = _re.compile(r"\broad\b", _re.I)

_WEIGHTS = {
    "area_rank": 0.20,
    "rectangularity": 0.15,
    "aspect_ratio": 0.10,
    "boundary_annotation": 0.20,
    "label_match": 0.20,
    "road_adjacency": 0.10,
    "not_nested": 0.05,
}


@dataclass
class ScoredPlotCandidate:
    candidate: PlotCandidate
    score: float
    breakdown: dict[str, float] = field(default_factory=dict)


def _aspect_score(bbox) -> float:
    ar = geo.aspect_ratio(bbox)
    if not math.isfinite(ar):
        return 0.0
    # Most real plots are well under a 6:1 aspect ratio; score decays past that.
    return max(0.0, 1.0 - max(0.0, ar - 1.0) / 6.0)


def _label_match_score(bbox, text_evidence: list[TextEvidence], page: Optional[int]) -> float:
    """Reward candidates whose bounding box is near text mentioning plot/site/property."""
    hits = 0
    tol = max(bbox.width, bbox.height) * 0.35
    for t in text_evidence:
        if page is not None and t.page != page:
            continue
        if not _LABEL_RE_PLOT.search(t.raw_text or ""):
            continue
        if t.bounding_box is None:
            continue
        if _bbox_gap(bbox, t.bounding_box) <= tol:
            hits += 1
    return min(1.0, hits * 0.5)


def _boundary_annotation_score(bbox, dimensions, page: Optional[int]) -> float:
    """Fraction of the candidate's 4 notional sides that have a nearby dimension."""
    if not dimensions:
        return 0.0
    tol = max(bbox.width, bbox.height) * 0.08
    sides_hit = set()
    for dim in dimensions:
        if dim.geometry is None:
            continue
        mid = geo.edge_midpoint(dim.geometry)
        if abs(mid.x - bbox.min_x) <= tol:
            sides_hit.add("left")
        if abs(mid.x - bbox.max_x) <= tol:
            sides_hit.add("right")
        if abs(mid.y - bbox.min_y) <= tol:
            sides_hit.add("top")
        if abs(mid.y - bbox.max_y) <= tol:
            sides_hit.add("bottom")
    return len(sides_hit) / 4.0


def _road_adjacency_score(bbox, road_bboxes) -> float:
    if not road_bboxes:
        return 0.0
    best = min(_bbox_gap(bbox, rb) for rb in road_bboxes)
    tol = max(bbox.width, bbox.height) * 0.15
    if best <= 0:
        return 1.0
    return max(0.0, 1.0 - best / max(tol, 1e-6))


def _bbox_gap(a, b) -> float:
    dx = max(a.min_x - b.max_x, b.min_x - a.max_x, 0.0)
    dy = max(a.min_y - b.max_y, b.min_y - a.max_y, 0.0)
    return math.hypot(dx, dy)


def _vision_plot_score(extraction: ExtractionResult, bbox, page: Optional[int]) -> float:
    if page is None or not extraction.vision_pages:
        return 0.0
    site_regions = semantic_regions(extraction, page, {"SITE_PLAN"})
    region = region_score(bbox, site_regions)
    dimensions = semantic_dimension_score(
        extraction, page, bbox, {"PLOT_WIDTH", "PLOT_DEPTH"}
    )
    return min(1.0, 0.45 * region + 0.55 * dimensions)


def score_plot_candidates(
    extraction: ExtractionResult,
) -> tuple[Optional[ScoredPlotCandidate], list[ScoredPlotCandidate], str]:
    """
    Returns (winner, all_scored, page_selection_note). Candidates are
    grouped by source page (a plan may have multiple sheets); the page
    with the highest-scoring candidate is selected.
    """
    candidates = [c for c in extraction.plot_candidates if c.geometry and c.geometry.polygon]
    if not candidates:
        return None, [], "No plot candidates with polygon geometry were extracted."

    road_bboxes_by_page: dict[int, list] = {}
    for r in extraction.road_candidates:
        if r.geometry and r.geometry.bounding_box is not None:
            road_bboxes_by_page.setdefault(r.geometry.source_page or -1, []).append(r.geometry.bounding_box)

    areas = [c.geometry.polygon.area for c in candidates]
    max_area = max(areas) if areas else 1.0

    scored: list[ScoredPlotCandidate] = []
    for cand, area in zip(candidates, areas):
        bbox = cand.geometry.bounding_box or cand.geometry.polygon.bounding_box
        page = cand.geometry.source_page
        breakdown = {
            "area_rank": area / max_area if max_area else 0.0,
            "rectangularity": geo.rectangularity(cand.geometry.polygon),
            "aspect_ratio": _aspect_score(bbox),
            "boundary_annotation": _boundary_annotation_score(bbox, extraction.dimensions, page),
            "label_match": _label_match_score(bbox, extraction.text_evidence, page),
            "road_adjacency": _road_adjacency_score(bbox, road_bboxes_by_page.get(page, [])),
            "not_nested": 1.0,  # penalized below if nested inside another candidate
        }
        for other in candidates:
            if other is cand or other.geometry is None or other.geometry.bounding_box is None:
                continue
            if other.geometry.source_page != page:
                continue
            other_bbox = other.geometry.bounding_box
            if (
                other_bbox.width * other_bbox.height > bbox.width * bbox.height * 1.02
                and bbox.min_x >= other_bbox.min_x
                and bbox.min_y >= other_bbox.min_y
                and bbox.max_x <= other_bbox.max_x
                and bbox.max_y <= other_bbox.max_y
            ):
                breakdown["not_nested"] = 0.0
                break

        total = sum(_WEIGHTS[k] * v for k, v in breakdown.items())
        if extraction.vision_pages:
            vision_score = _vision_plot_score(extraction, bbox, page)
            total += 0.20 * vision_score
            breakdown["vision_semantics"] = vision_score
        scored.append(ScoredPlotCandidate(candidate=cand, score=total, breakdown=breakdown))

    scored.sort(key=lambda s: s.score, reverse=True)
    winner = scored[0]
    note = f"Selected from {len(scored)} candidate(s) across {len({c.candidate.geometry.source_page for c in scored})} page(s)."
    return winner, scored, note


def plot_confidence(winner: ScoredPlotCandidate, all_scored: list[ScoredPlotCandidate]) -> tuple[ConfidenceLevel, str]:
    if len(all_scored) == 1:
        if winner.score >= 0.5:
            return ConfidenceLevel.MEDIUM, "Only one plot candidate was found; accepted on its own merits."
        return ConfidenceLevel.LOW, "Only one plot candidate was found and it scored poorly on plot-likeness features."
    runner_up = all_scored[1]
    margin = winner.score - runner_up.score
    if winner.score >= 0.6 and margin >= 0.15:
        return ConfidenceLevel.HIGH, f"Clear winner (score {winner.score:.2f}) with a {margin:.2f} margin over the runner-up."
    if margin >= 0.05:
        return ConfidenceLevel.MEDIUM, f"Winner (score {winner.score:.2f}) beats the runner-up by {margin:.2f}."
    return ConfidenceLevel.LOW, f"Winner only narrowly ({margin:.2f}) beats the runner-up ({runner_up.score:.2f}); ambiguous."


__all__ = ["ScoredPlotCandidate", "score_plot_candidates", "plot_confidence"]
