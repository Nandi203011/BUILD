"""
Area / coverage / FAR computation.

Keeps distinct area concepts separate rather than silently substituting
one for another (room/carpet/built-up/footprint areas). The
`NormalizedPlan` contract only has room for `building.footprint_area`
(the FAR-relevant plinth/footprint area used for coverage), so any other
labeled areas found in the source text (carpet area, built-up area,
etc.) are surfaced via `label_notes` for the caller to fold into
`overall_confidence_note` — never merged into `footprint_area` itself.

FAR here is always the *diagnostic* gross FAR defined in phase3.md:

    gross_FAR = gross_built_up_area / plot_area

never a regulatory/sanctioned FAR (that belongs to the RuleEngine, which
compares this diagnostic against a municipality's `RuntimeRuleDefinition`
threshold).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Optional

from backend.schemas.enums import ConfidenceLevel
from backend.schemas.evidence import Confidence, Conflict, GeometryEvidence, TextEvidence, ValueField
from backend.schemas.geometry import Polygon
from backend.schemas.units import CanonicalUnit, UnitValue

# A building footprint can legitimately be very close to the plot area
# (near-100% coverage happens on real, tightly-built urban plots), but it
# can NEVER exceed it — footprint is physically contained within the
# plot. A small tolerance absorbs rounding/digitisation noise without
# masking a genuine resolution error (e.g. mismatched plot/building
# candidates from different drawing regions being paired together).
_MAX_PHYSICALLY_PLAUSIBLE_COVERAGE_RATIO = 1.02

_LABELED_AREA_RE = re.compile(
    r"\b(?P<label>carpet\s*area|built[\s-]?up\s*area|builtup\s*area|plinth\s*area|"
    r"floor\s*area)\b[^\d]{0,15}(?P<value>\d+(?:\.\d+)?)\s*(?P<unit>sq\.?\s?ft|sqft|sq\.?\s?m|sqm)?",
    re.I,
)


@dataclass
class LabeledArea:
    label: str
    value: float
    unit: Optional[str]
    source_text: str


def find_labeled_areas(text_evidence: list[TextEvidence]) -> list[LabeledArea]:
    """Detect explicitly labeled areas (carpet/built-up/plinth/floor) in plan text, kept separate."""
    found: list[LabeledArea] = []
    for t in text_evidence:
        for m in _LABELED_AREA_RE.finditer(t.raw_text or ""):
            found.append(
                LabeledArea(
                    label=re.sub(r"\s+", " ", m.group("label")).strip().lower(),
                    value=float(m.group("value")),
                    unit=(m.group("unit") or None),
                    source_text=t.raw_text,
                )
            )
    return found


def polygon_area_field(
    polygon_metric: Optional[Polygon],
    width_field: Optional[ValueField[float]],
    depth_field: Optional[ValueField[float]],
    rectangularity_score: float,
    label: str,
) -> ValueField[float]:
    """
    Rectangular plot: area = width * depth (once both are known, HIGH confidence).
    Irregular plot: area = polygon_area(polygon) via the shoelace formula.
    Both are preserved where available: if width/depth are known AND the
    shape is clearly rectangular, prefer width*depth (matches how the
    dimensions themselves were read); otherwise use the geometric polygon
    area directly.
    """
    geom_area = polygon_metric.area if polygon_metric is not None else None

    if (
        rectangularity_score >= 0.9
        and width_field is not None
        and depth_field is not None
        and width_field.value is not None
        and depth_field.value is not None
    ):
        value = width_field.value * depth_field.value
        note = f"Rectangular plot: {label} = width * depth ({width_field.value:.3f} x {depth_field.value:.3f})."
        if geom_area is not None and geom_area > 0:
            rel_diff = abs(value - geom_area) / geom_area
            note += f" Geometry-derived polygon area is {geom_area:.3f} (relative diff {rel_diff:.1%})."
        return ValueField[float](
            value=round(value, 4),
            normalized_value=UnitValue(magnitude=round(value, 4), unit=CanonicalUnit.SQUARE_METRE.value),
            confidence=Confidence(level=ConfidenceLevel.HIGH, reason=note),
            source=f"{label} (width x depth)",
        )

    if geom_area is not None and geom_area > 0:
        return ValueField[float](
            value=round(geom_area, 4),
            normalized_value=UnitValue(magnitude=round(geom_area, 4), unit=CanonicalUnit.SQUARE_METRE.value),
            confidence=Confidence(
                level=ConfidenceLevel.MEDIUM,
                reason=f"Irregular/unclear-rectangularity {label}; using polygon_area(shoelace) directly.",
            ),
            source=f"{label} (polygon geometry)",
        )

    return ValueField[float].missing(f"No geometry or width/depth pair available to compute {label}.")


def coverage_field(footprint_area: ValueField[float], plot_area: ValueField[float]) -> ValueField[float]:
    if footprint_area.value is None or plot_area.value is None or plot_area.value <= 0:
        return ValueField[float].missing(
            "Coverage requires both building.footprint_area and plot.area to be resolved."
        )

    ratio = footprint_area.value / plot_area.value
    if ratio > _MAX_PHYSICALLY_PLAUSIBLE_COVERAGE_RATIO:
        # FIX #9 (phase3.1): footprint > plot area is not "unusually high
        # coverage", it is physically impossible — almost always a sign
        # that the resolved plot and building candidates are from
        # mismatched/incorrect geometry (e.g. a detail-view rectangle
        # paired with a site-scale plot). Surface this as an explicit
        # conflict rather than a nonsense percentage the caller has to
        # notice is wrong on their own.
        return ValueField[float].conflicting(
            Conflict(
                description=(
                    f"Physically impossible coverage: building.footprint_area "
                    f"({footprint_area.value:.3f}) exceeds plot.area ({plot_area.value:.3f}) "
                    f"by a factor of {ratio:.2f}x. This indicates the resolved plot and building "
                    "candidates likely do not both refer to the same real-world footprint "
                    "(e.g. mismatched drawing regions/scale) rather than genuinely high site "
                    "coverage."
                ),
                conflicting_raw_values=[
                    UnitValue(magnitude=footprint_area.value, unit=CanonicalUnit.SQUARE_METRE.value),
                    UnitValue(magnitude=plot_area.value, unit=CanonicalUnit.SQUARE_METRE.value),
                ],
                conflicting_sources=["building.footprint_area", "plot.area"],
            )
        )

    pct = ratio * 100.0
    level = ConfidenceLevel.HIGH if (footprint_area.confidence.level, plot_area.confidence.level) == (
        ConfidenceLevel.HIGH,
        ConfidenceLevel.HIGH,
    ) else ConfidenceLevel.MEDIUM
    return ValueField[float](
        value=pct,  # full precision preserved internally, not rounded
        normalized_value=UnitValue(magnitude=pct, unit=CanonicalUnit.PERCENTAGE.value),
        confidence=Confidence(
            level=level,
            reason=f"coverage = footprint_area({footprint_area.value:.3f}) / plot_area({plot_area.value:.3f}) * 100",
        ),
        source="coverage = building.footprint_area / plot.area * 100",
    )


def far_field(
    footprint_area: ValueField[float],
    plot_area: ValueField[float],
    floor_count: Optional[ValueField[int]],
) -> ValueField[float]:
    if footprint_area.value is None or plot_area.value is None or plot_area.value <= 0:
        return ValueField[float].missing("FAR requires both building.footprint_area and plot.area to be resolved.")

    if footprint_area.value / plot_area.value > _MAX_PHYSICALLY_PLAUSIBLE_COVERAGE_RATIO:
        # Same hard physical boundary as coverage_field: FAR is built on
        # the same footprint/plot relationship, so a physically
        # impossible footprint invalidates FAR too rather than producing
        # an inflated-but-plausible-looking ratio.
        return ValueField[float].conflicting(
            Conflict(
                description=(
                    f"FAR is not computable: building.footprint_area ({footprint_area.value:.3f}) "
                    f"exceeds plot.area ({plot_area.value:.3f}), which is physically impossible — "
                    "see building.footprint_area/coverage conflict for detail."
                ),
                conflicting_raw_values=[
                    UnitValue(magnitude=footprint_area.value, unit=CanonicalUnit.SQUARE_METRE.value),
                    UnitValue(magnitude=plot_area.value, unit=CanonicalUnit.SQUARE_METRE.value),
                ],
                conflicting_sources=["building.footprint_area", "plot.area"],
            )
        )

    if floor_count is not None and floor_count.value is not None and floor_count.value > 0:
        gross_built_up = footprint_area.value * floor_count.value
        note = (
            f"Diagnostic gross FAR = gross_built_up_area({gross_built_up:.3f}, "
            f"footprint x {floor_count.value} floors) / plot_area({plot_area.value:.3f}). "
            "NOT a regulatory/sanctioned FAR — that comparison belongs to the RuleEngine."
        )
        level = ConfidenceLevel.MEDIUM
    else:
        gross_built_up = footprint_area.value
        note = (
            f"Diagnostic gross FAR = gross_built_up_area({gross_built_up:.3f}, footprint area, "
            "floor count NOT resolved so a single storey is assumed) / "
            f"plot_area({plot_area.value:.3f}). Treat as a lower bound. NOT a regulatory/sanctioned FAR."
        )
        level = ConfidenceLevel.LOW

    ratio = gross_built_up / plot_area.value
    return ValueField[float](
        value=ratio,
        normalized_value=UnitValue(magnitude=ratio, unit=CanonicalUnit.RATIO.value),
        confidence=Confidence(level=level, reason=note),
        source="far = gross_built_up_area / plot.area (diagnostic gross FAR)",
    )


__all__ = ["LabeledArea", "find_labeled_areas", "polygon_area_field", "coverage_field", "far_field"]
