"""
Setback derivation — geometry-first, per phase3.md.
...
"""

from __future__ import annotations

from typing import Optional

from backend.config import get_settings
from backend.schemas.evidence import GeometryEvidence
from backend.schemas.evidence import ValueField
from backend.schemas.geometry import Polygon
from backend.schemas.normalized_plan import SetbackSection
from backend.spatial_reasoning import geometry_utils as geo
from backend.spatial_reasoning.dimension_classification import ClassifiedDimension, DimensionSemanticType
from backend.spatial_reasoning.evidence_reconciliation import EvidenceCandidate, to_value_field
from backend.spatial_reasoning.front_side import FrontSideResolution

_SIDE_TO_SETBACK_TYPE = {
    "front": DimensionSemanticType.FRONT_SETBACK,
    "rear": DimensionSemanticType.REAR_SETBACK,
    "left": DimensionSemanticType.LEFT_SETBACK,
    "right": DimensionSemanticType.RIGHT_SETBACK,
}

# front/rear setbacks eat into plot.depth - building.depth;
# left/right setbacks eat into plot.width - building.width.
# (Matches the pairing consistency.py already assumes: "building.width +
# left + right ~= plot.width", "building.depth + front + rear ~= plot.depth".)
_SIDE_TO_AXIS_BUDGET = {
    "front": "depth",
    "rear": "depth",
    "left": "width",
    "right": "width",
}


def _axis_budget(
    axis: str,
    plot_width: Optional[ValueField[float]],
    plot_depth: Optional[ValueField[float]],
    building_width: Optional[ValueField[float]],
    building_depth: Optional[ValueField[float]],
) -> Optional[float]:
    """Total gap available on one axis (front+rear, or left+right), if both
    the plot and building extents on that axis are known."""
    plot_field = plot_depth if axis == "depth" else plot_width
    building_field = building_depth if axis == "depth" else building_width
    if plot_field is None or building_field is None:
        return None
    if plot_field.value is None or building_field.value is None:
        return None
    return plot_field.value - building_field.value


def compute_setbacks(
    building_polygon_metric: Optional[Polygon],
    plot_edges_metric: FrontSideResolution,
    classified_dimensions: list[ClassifiedDimension],
    points_per_metre: float,
    plot_width: Optional[ValueField[float]] = None,
    plot_depth: Optional[ValueField[float]] = None,
    building_width: Optional[ValueField[float]] = None,
    building_depth: Optional[ValueField[float]] = None,
) -> SetbackSection:
    side_edge_groups = {
        "front": plot_edges_metric.front_edges,
        "rear": plot_edges_metric.rear_edges,
        "left": plot_edges_metric.left_edges,
        "right": plot_edges_metric.right_edges,
    }

    tol = get_settings().geometry_tolerance_m
    fields = {}
    rejected_notes: dict[str, list[str]] = {}

    for side, edges in side_edge_groups.items():
        candidates: list[EvidenceCandidate] = []
        rejected_notes[side] = []

        budget = _axis_budget(_SIDE_TO_AXIS_BUDGET[side], plot_width, plot_depth, building_width, building_depth)
        # A little slack: two setbacks split a budget unevenly (e.g. 0 / 0.47),
        # but neither one can ever exceed the *whole* budget for that axis.
        budget_cap = budget + tol if budget is not None else None

        if building_polygon_metric is not None and edges:
            dist = geo.polygon_to_edges_distance(building_polygon_metric, edges)
            if dist != float("inf"):
                if budget_cap is not None and dist > budget_cap:
                    rejected_notes[side].append(
                        f"geometry distance {dist:.3f} m exceeds the {_SIDE_TO_AXIS_BUDGET[side]}-axis "
                        f"budget ({budget_cap:.3f} m) — discarded as implausible"
                    )
                else:
                    candidates.append(
                        EvidenceCandidate(
                            value=round(dist, 4),
                            source=f"geometry (building-to-{side}-boundary distance)",
                            evidence=GeometryEvidence(
                                description=f"Metric-space distance from building footprint to the {side} plot edge."
                            ),
                        )
                    )

        setback_type = _SIDE_TO_SETBACK_TYPE[side]
        for cd in classified_dimensions:
            if cd.semantic_type is setback_type and cd.value_metres is not None:
                if budget_cap is not None and cd.value_metres > budget_cap:
                    rejected_notes[side].append(
                        f"dimension label ('{cd.dimension.label}'={cd.value_metres:.3f} m) exceeds the "
                        f"{_SIDE_TO_AXIS_BUDGET[side]}-axis budget ({budget_cap:.3f} m) — almost certainly a "
                        f"misclassified plot/building dimension rather than a real setback — discarded"
                    )
                    continue
                candidates.append(
                    EvidenceCandidate(
                        value=round(cd.value_metres, 4),
                        source=f"dimension label ('{cd.dimension.label}')",
                        weight=cd.confidence,
                    )
                )

        missing_reason = f"No geometry-derived distance or classified dimension found for the {side} setback."
        if rejected_notes[side]:
            missing_reason += " Rejected candidate(s): " + "; ".join(rejected_notes[side])

        fields[side] = to_value_field(
            candidates,
            field_label=f"setbacks.{side}",
            missing_reason=missing_reason,
        )

    return SetbackSection(front=fields["front"], rear=fields["rear"], left=fields["left"], right=fields["right"])


__all__ = ["compute_setbacks"]