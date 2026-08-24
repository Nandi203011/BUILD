"""Tests for `backend.spatial_reasoning.areas`."""

from __future__ import annotations

from backend.schemas.enums import ConfidenceLevel
from backend.schemas.evidence import Confidence, ValueField
from backend.spatial_reasoning.areas import coverage_field, far_field, find_labeled_areas, polygon_area_field
from tests.fixtures.geometry_builders import rect_polygon, text


def _hi(value):
    return ValueField[float](value=value, confidence=Confidence(level=ConfidenceLevel.HIGH))


def test_rectangular_plot_area_uses_width_times_depth():
    poly = rect_polygon(0, 0, 10, 12)  # rectangularity = 1.0
    width = _hi(10.0)
    depth = _hi(12.0)
    area = polygon_area_field(poly, width, depth, rectangularity_score=1.0, label="plot.area")
    assert area.value == 120.0
    assert area.confidence.level == ConfidenceLevel.HIGH


def test_irregular_plot_area_uses_shoelace_polygon_area():
    from backend.schemas.geometry import Point, Polygon

    # L-shaped polygon, area = 400*200 + 150*200 = 110000 in whatever units
    l_shape = Polygon(
        points=[
            Point(x=0, y=0),
            Point(x=400, y=0),
            Point(x=400, y=200),
            Point(x=250, y=200),
            Point(x=250, y=400),
            Point(x=0, y=400),
        ]
    )
    area = polygon_area_field(l_shape, None, None, rectangularity_score=0.5, label="plot.area")
    assert area.value == l_shape.area
    assert area.confidence.level == ConfidenceLevel.MEDIUM


def test_area_missing_when_no_evidence():
    area = polygon_area_field(None, None, None, rectangularity_score=0.0, label="plot.area")
    assert area.value is None
    assert area.confidence.level == ConfidenceLevel.MISSING


def test_coverage_is_footprint_over_plot_area_times_100():
    footprint = _hi(50.0)
    plot_area = _hi(200.0)
    cov = coverage_field(footprint, plot_area)
    assert cov.value == 25.0


def test_coverage_missing_when_plot_area_zero_or_missing():
    footprint = _hi(50.0)
    missing_plot = ValueField[float].missing("no plot area")
    cov = coverage_field(footprint, missing_plot)
    assert cov.value is None


def test_far_is_diagnostic_gross_far_not_regulatory():
    footprint = _hi(100.0)
    plot_area = _hi(200.0)
    floor_count = ValueField[int](value=2, confidence=Confidence(level=ConfidenceLevel.MEDIUM))
    far = far_field(footprint, plot_area, floor_count)
    # gross_built_up = footprint * floors = 200; far = 200/200 = 1.0
    assert far.value == 1.0
    assert "regulatory" in far.confidence.reason.lower() or "NOT a regulatory" in far.confidence.reason


def test_far_without_floor_count_assumes_single_storey_at_low_confidence():
    footprint = _hi(100.0)
    plot_area = _hi(200.0)
    far = far_field(footprint, plot_area, None)
    assert far.value == 0.5
    assert far.confidence.level == ConfidenceLevel.LOW


def test_labeled_areas_kept_separate_carpet_vs_builtup():
    evs = [
        text("CARPET AREA 85.5 SQ.M"),
        text("BUILT UP AREA 120.0 SQ.M"),
    ]
    found = find_labeled_areas(evs)
    labels = {f.label for f in found}
    assert any("carpet" in l for l in labels)
    assert any("built" in l for l in labels)
    # Confirm the two values were NOT merged/conflated into one number.
    values = {f.value for f in found}
    assert 85.5 in values and 120.0 in values
