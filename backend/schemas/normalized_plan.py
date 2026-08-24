"""
NormalizedPlan — the single, resolved, municipality-independent
representation of a building plan.

This is THE contract. Every teammate downstream of extraction (RAG, RASE,
RuleEngine, report generation, frontend) builds against this shape and
NOTHING else. It intentionally contains no municipality-specific
thresholds — those live in externally-loaded runtime rules (see
backend/runtime_rules/contracts.py) and are matched against these fields.
"""

from __future__ import annotations

from typing import Optional

from pydantic import BaseModel, Field

from backend.schemas.candidates import SetbackMeasurement
from backend.schemas.evidence import Conflict, ValueField
from backend.schemas.geometry import NormalizedGeometry


class PlotSection(BaseModel):
    geometry: Optional[NormalizedGeometry] = None
    width: ValueField[float]
    depth: ValueField[float]
    area: ValueField[float]


class BuildingSection(BaseModel):
    geometry: Optional[NormalizedGeometry] = None
    width: ValueField[float]
    depth: ValueField[float]
    footprint_area: ValueField[float]
    floor_count: Optional[ValueField[int]] = None


class RoadSection(BaseModel):
    geometry: Optional[NormalizedGeometry] = None
    width: ValueField[float]


class SetbackSection(BaseModel):
    front: ValueField[float]
    rear: ValueField[float]
    left: ValueField[float]
    right: ValueField[float]

    def as_measurements(self) -> list[SetbackMeasurement]:
        return [
            SetbackMeasurement(side=side, distance=getattr(self, side))
            for side in ("front", "rear", "left", "right")
        ]


class NormalizedPlan(BaseModel):
    """
    Fully resolved plan, one instance per submitted building plan.

    `evidence` and `confidence` at the top level summarize plan-wide
    extraction quality; per-field evidence/confidence lives inside each
    ValueField. `conflicts` aggregates every Conflict raised anywhere in
    the plan so the RuleEngine (or a human reviewer) can act on them
    without walking the whole tree.
    """

    plan_id: str
    source_document_id: str

    plot: PlotSection
    building: BuildingSection
    road: RoadSection
    setbacks: SetbackSection

    coverage: ValueField[float] = Field(
        ..., description="Ground coverage, canonical unit = %"
    )
    far: ValueField[float] = Field(
        ..., description="Floor Area Ratio, canonical unit = ratio"
    )

    conflicts: list[Conflict] = Field(default_factory=list)
    overall_confidence_note: Optional[str] = None

    def missing_field_names(self) -> list[str]:
        """Convenience for report generation / REQUIRES_REVIEW triage."""
        from backend.schemas.enums import ConfidenceLevel

        missing = []
        candidates = {
            "plot.width": self.plot.width,
            "plot.depth": self.plot.depth,
            "plot.area": self.plot.area,
            "building.width": self.building.width,
            "building.depth": self.building.depth,
            "building.footprint_area": self.building.footprint_area,
            "road.width": self.road.width,
            "setbacks.front": self.setbacks.front,
            "setbacks.rear": self.setbacks.rear,
            "setbacks.left": self.setbacks.left,
            "setbacks.right": self.setbacks.right,
            "coverage": self.coverage,
            "far": self.far,
        }
        for name, field in candidates.items():
            if field.confidence.level == ConfidenceLevel.MISSING:
                missing.append(name)
        return missing
