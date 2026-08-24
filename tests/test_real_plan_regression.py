"""
Regression tests against the REAL sanctioned plans in `data/test_plans/`.

The rest of the suite runs almost entirely against synthetic PDFs built by
`tests/fixtures/pdf_builders.py`. Those fixtures are drawn the way the
extractor expects plans to be drawn, so they passed (271/271) throughout a
period when 4 of the 5 real bundled plans returned null for every geometric
field, and the one plan that did resolve returned plot.width = 40.64 m for a
plot 10.00 m wide.

These tests assert against values the plans state about THEMSELVES -- the
area statement printed on the sheet -- rather than against numbers copied
out of a previous run of this code. That distinction matters: an acceptance
value harvested from the implementation only pins current behaviour, whereas
"the reconstructed plot boundary must reproduce the area the drawing says
the plot has" is a property the extraction has to earn.

Tolerances are relative and deliberately loose enough to absorb where a
boundary's centreline sits inside its stroke width, and tight enough that
the failures this suite was written for (an order-of-magnitude wrong scale,
a caption read as a dimension, the wrong rectangle chosen) cannot pass.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from backend.cv_extraction.scale_note import (
    detect_scale_notes,
    nearest_scale_note,
    points_per_metre_for_denominator,
)
from backend.cv_extraction.site_plan import extract_independent_cv

PLANS_DIR = Path(__file__).parent.parent / "data" / "test_plans"

# Ground truth, read off the sheets by eye and corroborated by each sheet's
# own printed area statement.
PLAN2 = {
    "plot.width": 12.192,   # 40 ft
    "plot.depth": 18.28,    # 60 ft
    "road.width": 9.2,
    "setbacks.front": 1.0,
    "setbacks.rear": 0.8,
    "setbacks.left": 0.8,
    "setbacks.right": 0.8,
    "stated_plot_area": 222.83,
    "stated_footprint_area": 174.52,
}
PLAN6 = {
    "plot.width": 10.00,
    "plot.depth": 13.10,
    "road.width": 7.3,
    "stated_plot_area": 131.00,       # "AREA OF PLOT (Minimum)"
    "stated_net_plot_area": 107.50,   # after the 10.00 x 2.35 road-widening strip
    "stated_footprint_area": 85.09,   # "Proposed Coverage Area"
}


def _measurements(plan_filename: str) -> dict[str, float]:
    pdf_path = PLANS_DIR / plan_filename
    if not pdf_path.exists():
        pytest.skip(f"{plan_filename} fixture not present")
    result = extract_independent_cv(pdf_path, plan_filename)
    return {
        m.field: (m.value_m if m.value_m is not None else m.value)
        for m in result.measurements
    }


def _assert_close(actual: float | None, expected: float, field: str, tolerance: float = 0.02):
    assert actual is not None, f"{field} was not resolved at all (None)"
    error = abs(actual - expected) / expected
    assert error <= tolerance, (
        f"{field}: got {actual:.4f}, expected ~{expected} ({error:.2%} off, "
        f"tolerance {tolerance:.0%})"
    )


# --- Printed scale notes ----------------------------------------------------


def test_points_per_metre_matches_the_physical_definition_of_a_drawing_scale():
    # 1pt = 1/72in = 25.4/72 mm on paper; at 1:200 that is 70.5556 mm real,
    # so 1 m contains 1000/70.5556 = 14.1732 pt.
    assert points_per_metre_for_denominator(200) == pytest.approx(14.17323, abs=1e-4)
    assert points_per_metre_for_denominator(100) == pytest.approx(28.34646, abs=1e-4)
    # Halving the denominator doubles the points per metre.
    assert points_per_metre_for_denominator(50) == pytest.approx(
        2 * points_per_metre_for_denominator(100)
    )


def test_cement_mortar_mix_ratios_are_not_read_as_drawing_scales():
    """
    "1:6" in "0.15th in C.M 1:6" is a cement-mortar proportion. Read as a
    drawing scale it yields 472 pt/m instead of 14 pt/m -- a 33x error that
    would shrink every derived dimension by the same factor. Real working
    drawings carry several of these in their specification notes.
    """
    from backend.cv_extraction.raw_types import RawTextItem, SourceKind
    from backend.schemas.geometry import BoundingBox

    def item(text: str) -> RawTextItem:
        return RawTextItem(
            text=text,
            bounding_box=BoundingBox(min_x=0, min_y=0, max_x=10, max_y=10),
            page=0,
            source=SourceKind.PDF_TEXT,
        )

    mix_notes = [
        item("0.15th in C.M 1:6"),
        item("BRICK WORK IN CM 1:5"),
        item("P.C.C in mix 1:5:10"),
        item("FLOORING CONCRETE 1:5:10"),
    ]
    assert detect_scale_notes(mix_notes, 0) == []

    # ... while a genuine scale note beside them is still found.
    found = detect_scale_notes([*mix_notes, item("Scale 1:200")], 0)
    assert [n.denominator for n in found] == [200]


def test_real_sheets_expose_their_printed_scale():
    from backend.cv_extraction import pdf_native

    for filename, expected_denominator in (("PLAN2.pdf", 200), ("PLAN6.pdf", 200)):
        pdf_path = PLANS_DIR / filename
        if not pdf_path.exists():
            pytest.skip(f"{filename} fixture not present")
        doc = pdf_native.open_document(pdf_path)
        try:
            page = doc.load_page(0)
            notes = detect_scale_notes(pdf_native.extract_text_items(page, 0), 0)
        finally:
            doc.close()
        assert notes, f"{filename}: no printed scale note found"
        assert {n.denominator for n in notes} == {expected_denominator}, (
            f"{filename}: expected only 1:{expected_denominator}, got "
            f"{sorted(n.denominator for n in notes)}"
        )


def test_multi_view_sheet_keeps_every_distinct_view_scale():
    """
    PLAN4 prints four different view scales. Collapsing them to one would be
    wrong -- which governs the site plan is a spatial question, so all of
    them must survive detection.
    """
    from backend.cv_extraction import pdf_native

    pdf_path = PLANS_DIR / "PLAN4.pdf"
    if not pdf_path.exists():
        pytest.skip("PLAN4.pdf fixture not present")
    doc = pdf_native.open_document(pdf_path)
    try:
        page = doc.load_page(0)
        notes = detect_scale_notes(pdf_native.extract_text_items(page, 0), 0)
    finally:
        doc.close()
    assert {n.denominator for n in notes} == {25, 50, 75, 100}
    # ...and the mix ratios on the same sheet are still excluded.
    assert 5 not in {n.denominator for n in notes}


# --- PLAN2: the plan that already resolved, and must not regress ------------


def test_plan2_still_resolves_every_geometric_field():
    got = _measurements("PLAN2.pdf")
    for field in (
        "plot.width", "plot.depth", "road.width",
        "setbacks.front", "setbacks.rear", "setbacks.left", "setbacks.right",
    ):
        _assert_close(got.get(field), PLAN2[field], field)
    assert got.get("building.width") is not None
    assert got.get("building.depth") is not None


def test_plan2_building_footprint_reproduces_its_stated_coverage_area():
    got = _measurements("PLAN2.pdf")
    measured = got["building.width"] * got["building.depth"]
    _assert_close(measured, PLAN2["stated_footprint_area"], "building footprint area", 0.01)


# --- PLAN6: the plan that returned nothing ----------------------------------


def test_plan6_resolves_plot_dimensions_with_no_printed_edge_labels():
    """
    PLAN6's site plan carries NO printed plot-dimension label -- the only
    text near the drawing is "SITE NO-07/08/09", "7.30m Wide Road" and
    "Scale 1:200". Every geometric field came back null because the resolver
    required an edge label to establish scale. They are recoverable from the
    printed scale alone.
    """
    got = _measurements("PLAN6.pdf")
    _assert_close(got.get("plot.width"), PLAN6["plot.width"], "plot.width")
    _assert_close(got.get("plot.depth"), PLAN6["plot.depth"], "plot.depth")
    _assert_close(got.get("road.width"), PLAN6["road.width"], "road.width")


def test_plan6_plot_rectangle_reproduces_its_stated_plot_area():
    """The closed loop: geometry and the area statement are independent."""
    got = _measurements("PLAN6.pdf")
    measured = got["plot.width"] * got["plot.depth"]
    _assert_close(measured, PLAN6["stated_plot_area"], "plot area", 0.01)


def test_plan6_building_footprint_reproduces_its_stated_coverage_area():
    got = _measurements("PLAN6.pdf")
    assert got.get("building.width") is not None, "building.width not resolved"
    measured = got["building.width"] * got["building.depth"]
    _assert_close(measured, PLAN6["stated_footprint_area"], "building footprint area", 0.01)


def test_plan6_resolves_setbacks_including_a_zero_setback_edge():
    """
    PLAN6's building abuts the plot boundary on one side. A nesting rule
    that required a positive gap on all four sides rejected the real
    footprint outright, taking building.width/depth and all four setbacks
    down with it.
    """
    got = _measurements("PLAN6.pdf")
    for side in ("front", "rear", "left", "right"):
        assert got.get(f"setbacks.{side}") is not None, f"setbacks.{side} not resolved"

    # The four setbacks must account for exactly the difference between the
    # plot and the building on each axis -- they are not independent numbers.
    across = got["setbacks.left"] + got["setbacks.right"] + got["building.width"]
    along = got["setbacks.front"] + got["setbacks.rear"] + got["building.depth"]
    _assert_close(across, got["plot.width"], "left + building.width + right", 0.01)
    _assert_close(along, got["plot.depth"], "front + building.depth + rear", 0.01)


def test_plan6_gross_and_net_plot_area_are_separate_fields():
    """
    A BBMP sheet states both an "AREA OF PLOT" and a "NET AREA OF PLOT"
    (gross minus road widening). Emitting both as `plot.area` produced two
    contradictory values for one field, and whichever was read last won.
    """
    got = _measurements("PLAN6.pdf")
    _assert_close(got.get("plot.area"), PLAN6["stated_plot_area"], "plot.area", 0.005)
    _assert_close(
        got.get("plot.net_area"), PLAN6["stated_net_plot_area"], "plot.net_area", 0.005
    )


# --- The failure mode that started all of this ------------------------------


def test_prose_and_identifiers_are_not_dimension_candidates():
    """
    Every string here was extracted from a real sheet as a `Dimension` with
    the shown magnitude, and each one then became a scale sample.
    """
    from backend.cv_extraction.dimension_candidates import looks_like_dimension_text

    not_dimensions = [
        "46.Due to non-compliance of safety precautionary measures",
        "3.Car Parking reserved in the plan should not be converted",
        "PID No. (As per Khata Extract): 3910264701",
        "Permissible F.A.R. as per zoning regulation 2015 ( 1.75 )",
        "Ward: Ward 187",
        "ISO_A1_(841.00_x_594.00_MM)",
        "Planning District: 321-Anjanapura",
        "Project No: GBA/BSCC/0748/25-26",
        "VERSION DATE: 30/03/2026",
    ]
    for text in not_dimensions:
        assert not looks_like_dimension_text(text), f"should be rejected: {text!r}"

    real_dimensions = ["3.35", "0.91", "12.19", "7.30m", "10.00", "1.20", "9'-6\"", "2400mm"]
    for text in real_dimensions:
        assert looks_like_dimension_text(text), f"should be accepted: {text!r}"


def test_real_sheet_produces_far_fewer_but_better_dimension_candidates():
    from backend.cv_extraction import pdf_native
    from backend.cv_extraction.dimension_candidates import detect_dimension_candidates

    pdf_path = PLANS_DIR / "PLAN6.pdf"
    if not pdf_path.exists():
        pytest.skip("PLAN6.pdf fixture not present")
    doc = pdf_native.open_document(pdf_path)
    try:
        page = doc.load_page(0)
        text_items = pdf_native.extract_text_items(page, 0)
    finally:
        doc.close()

    candidates = detect_dimension_candidates(text_items, [])
    # Was 366 on this sheet, from 748 text spans.
    assert len(candidates) < 300, f"{len(candidates)} candidates -- prose is leaking back in"
    # No candidate may carry a magnitude that could not be a length.
    for candidate in candidates:
        assert candidate.numeric_value <= 1000.0, (
            f"implausible magnitude {candidate.numeric_value} from {candidate.raw_text!r}"
        )


# --- Abstention: a wrong measurement is worse than a missing one ------------


@pytest.mark.parametrize(
    "plan_filename, why",
    [
        (
            "PLAN4.pdf",
            "contains no site plan at all -- floor plans, a foundation detail, a staircase "
            "detail and a percolation-pit detail, with no plot boundary drawn anywhere",
        ),
        (
            "PLAN7.pdf",
            "is a low-quality raster scan whose OCR loses decimal points "
            "('4 29X3 20' for '4.29X3.20'), with no site plan region",
        ),
    ],
)
def test_sheets_without_a_site_plan_resolve_nothing_rather_than_guessing(plan_filename, why):
    """
    A sheet always contains *some* best-scoring rectangle near whatever text
    the anchor matched. Asserting it as the plot produces a confident wrong
    answer, which is strictly worse than MISSING here: the compliance engine
    maps MISSING to INSUFFICIENT_DATA and asks for review, but treats a
    present value as measured fact and will PASS or FAIL a real building on
    it.
    """
    got = _measurements(plan_filename)
    for field in (
        "plot.width", "plot.depth", "building.width", "building.depth",
        "setbacks.front", "setbacks.rear", "setbacks.left", "setbacks.right",
    ):
        assert got.get(field) is None, (
            f"{plan_filename} {why}, so {field} must be unresolved, got {got[field]}"
        )


def test_scanned_plan_still_reports_what_it_can_read_directly():
    """
    Abstaining on geometry must not suppress values that come from an
    explicit printed label rather than from the unverified rectangle.
    PLAN5 is a scan with no usable vector geometry and no area statement,
    but its road width is printed in words.
    """
    got = _measurements("PLAN5.pdf")
    assert got.get("plot.width") is None
    _assert_close(got.get("road.width"), 10.0, "road.width", 0.05)
