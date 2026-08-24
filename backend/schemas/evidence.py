"""
Evidence + confidence contract.

Every important extracted or derived field in the system is represented as
a `ValueField`, never as a bare float/str. This is the single most
important contract in Phase 1: it is what lets a compliance verdict be
explained and audited later, and it is what lets INSUFFICIENT_DATA /
CONFLICTING_EVIDENCE be represented instead of silently guessing.
"""

from __future__ import annotations

from typing import Generic, Optional, TypeVar

from pydantic import BaseModel, Field

from backend.schemas.enums import ConfidenceLevel, SourceType
from backend.schemas.geometry import BoundingBox, Line
from backend.schemas.units import UnitValue


class TextEvidence(BaseModel):
    """Evidence anchored in extracted text (OCR'd or native PDF text)."""

    source_type: SourceType = SourceType.TEXT
    raw_text: str
    page: Optional[int] = None
    bounding_box: Optional[BoundingBox] = None
    ocr_confidence: Optional[float] = Field(default=None, ge=0, le=1)


class GeometryEvidence(BaseModel):
    """Evidence anchored in extracted geometry (a dimension line, an outline segment)."""

    source_type: SourceType = SourceType.VECTOR_GEOMETRY
    page: Optional[int] = None
    line: Optional[Line] = None
    bounding_box: Optional[BoundingBox] = None
    description: Optional[str] = None


class Confidence(BaseModel):
    """
    Structured confidence, not just a single enum label.

    `level` is what rule authors and the frontend read. `score` is an
    optional continuous value (0-1) for modules that can produce one
    (e.g. OCR, geometric fit quality) — it's advisory, `level` is
    authoritative for any downstream branching logic.
    """

    level: ConfidenceLevel
    score: Optional[float] = Field(default=None, ge=0, le=1)
    reason: Optional[str] = None


class Conflict(BaseModel):
    """Records that two or more sources disagree about a field's value."""

    description: str
    conflicting_raw_values: list[UnitValue] = Field(default_factory=list)
    conflicting_sources: list[str] = Field(
        default_factory=list, description="Free-text identifiers of the disagreeing sources"
    )


T = TypeVar("T")


class ValueField(BaseModel, Generic[T]):
    """
    The universal wrapper for any important extracted/derived value.

    Fields:
        value               canonical, typed value (e.g. float in metres) — may be
                             None if status is MISSING or CONFLICTING
        raw_value           value+unit exactly as read from the source, before conversion
        normalized_value    value+unit after conversion to canonical units
        confidence          structured confidence
        status               ConfidenceLevel, mirrors confidence.level for quick filtering
        source               free-text description of where this came from
                             (e.g. "sheet 2, dimension line near south boundary")
        evidence             list of TextEvidence / GeometryEvidence backing this value
        conflict             populated only when status == CONFLICTING
    """

    value: Optional[T] = None
    raw_value: Optional[UnitValue] = None
    normalized_value: Optional[UnitValue] = None
    confidence: Confidence
    source: Optional[str] = None
    evidence: list[TextEvidence | GeometryEvidence] = Field(default_factory=list)
    conflict: Optional[Conflict] = None

    @property
    def status(self) -> ConfidenceLevel:
        return self.confidence.level

    @classmethod
    def missing(cls, reason: str = "Field not found in source document") -> "ValueField":
        return cls(
            value=None,
            confidence=Confidence(level=ConfidenceLevel.MISSING, reason=reason),
        )

    @classmethod
    def conflicting(cls, conflict: Conflict) -> "ValueField":
        return cls(
            value=None,
            confidence=Confidence(
                level=ConfidenceLevel.CONFLICTING, reason=conflict.description
            ),
            conflict=conflict,
        )
