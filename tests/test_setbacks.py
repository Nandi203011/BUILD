"""Tests for `backend.spatial_reasoning.setbacks`."""

from __future__ import annotations

from backend.spatial_reasoning.dimension_classification import DimensionSemanticType, classify_dimensions
from backend.spatial_reasoning.front_side import resolve_front_side
from backend.spatial_reasoning.setbacks import compute_setbacks
from tests.fixtures.geometry_builders import rect_polygon


def test_setbacks_computed_from_building_to_plot_edge_distance():
    plot = rect_polygon(0, 0, 400, 480)
    building = rect_polygon(60, 40, 340, 420)  # inset 60 left/right, 40 from top, 60 from bottom
    fsr = resolve_front_side(plot, road_bbox=None, access_evidence=[])
    setbacks = compute_setbacks(building, fsr, classified_dimensions=[], points_per_metre=1.0)
    assert setbacks.left.value == 60.0
    assert setbacks.right.value == 60.0
    # No road evidence -> front defaults to the polygon's first ring edge
    # (the top edge, y=0); rear is the farthest edge (bottom, y=480).
    assert round(setbacks.front.value) == 40
    assert round(setbacks.rear.value) == 60


def test_setback_missing_when_no_building_or_dimension_evidence():
    plot = rect_polygon(0, 0, 400, 480)
    fsr = resolve_front_side(plot, road_bbox=None, access_evidence=[])
    setbacks = compute_setbacks(None, fsr, classified_dimensions=[], points_per_metre=1.0)
    assert setbacks.front.value is None
    assert setbacks.front.status.name == "MISSING"


def test_dimension_derived_setback_used_when_geometry_absent():
    plot = rect_polygon(0, 0, 400, 480)
    fsr = resolve_front_side(plot, road_bbox=None, access_evidence=[])
    from backend.spatial_reasoning.dimension_classification import ClassifiedDimension
    from backend.schemas.enums import ConfidenceLevel
    from tests.fixtures.geometry_builders import dim

    cd = ClassifiedDimension(
        dimension=dim(3.0, "m", label="FRONT SETBACK"),
        semantic_type=DimensionSemanticType.FRONT_SETBACK,
        confidence=ConfidenceLevel.MEDIUM,
        reasoning="test",
        value_metres=3.0,
    )
    setbacks = compute_setbacks(None, fsr, classified_dimensions=[cd], points_per_metre=1.0)
    assert setbacks.front.value == 3.0


def test_setback_reconciles_geometry_and_dimension_agreement():
    plot = rect_polygon(0, 0, 400, 480)
    building = rect_polygon(60, 40, 340, 420)
    fsr = resolve_front_side(plot, road_bbox=None, access_evidence=[])
    from backend.spatial_reasoning.dimension_classification import ClassifiedDimension
    from backend.schemas.enums import ConfidenceLevel
    from tests.fixtures.geometry_builders import dim

    # front setback geometry-derived distance will be 40 (in whatever
    # units points_per_metre=1.0 means here); supply a matching dimension.
    cd = ClassifiedDimension(
        dimension=dim(40.0, "m", label="FRONT SETBACK"),
        semantic_type=DimensionSemanticType.FRONT_SETBACK,
        confidence=ConfidenceLevel.MEDIUM,
        reasoning="test",
        value_metres=40.0,
    )
    setbacks = compute_setbacks(building, fsr, classified_dimensions=[cd], points_per_metre=1.0)
    assert setbacks.front.status.name in ("HIGH", "MEDIUM")
    assert abs(setbacks.front.value - 40.0) < 1.0
