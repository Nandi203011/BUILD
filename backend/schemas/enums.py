"""
Shared enums used across the entire BUILDCheck India pipeline.

These enums are the vocabulary every teammate's module must speak.
Do NOT redefine equivalent enums locally in cv_extraction / rag / rasE /
runtime_rules / compliance — import from here.
"""

from enum import Enum


class ConfidenceLevel(str, Enum):
    """How much we trust a single extracted/derived value."""

    HIGH = "HIGH"
    MEDIUM = "MEDIUM"
    LOW = "LOW"
    CONFLICTING = "CONFLICTING"
    MISSING = "MISSING"


class ComplianceStatus(str, Enum):
    """
    Outcome of checking ONE rule against the normalized plan.

    Uncertainty must never be silently collapsed into PASS/FAIL.
    """

    PASS = "PASS"
    FAIL = "FAIL"
    INSUFFICIENT_DATA = "INSUFFICIENT_DATA"
    NOT_APPLICABLE = "NOT_APPLICABLE"
    CONFLICTING_EVIDENCE = "CONFLICTING_EVIDENCE"
    REQUIRES_REVIEW = "REQUIRES_REVIEW"


class SourceType(str, Enum):
    """Where a piece of evidence originated in the source document."""

    TEXT = "TEXT"
    TABLE = "TABLE"
    DIMENSION_LINE = "DIMENSION_LINE"
    VECTOR_GEOMETRY = "VECTOR_GEOMETRY"
    RASTER_GEOMETRY = "RASTER_GEOMETRY"
    OCR = "OCR"
    ANNOTATION = "ANNOTATION"
    DERIVED = "DERIVED"  # computed from other fields, not read directly


class DocumentType(str, Enum):
    """Kind of source document a plan was extracted from."""

    VECTOR_PDF = "VECTOR_PDF"
    RASTER_PDF = "RASTER_PDF"
    SCANNED_IMAGE = "SCANNED_IMAGE"
    DXF = "DXF"          # future — interface only, not implemented in Phase 1
    BIM = "BIM"           # future — interface only, not implemented in Phase 1


class EntityKind(str, Enum):
    """High-level category of a geometric candidate on a plan."""

    PLOT = "PLOT"
    BUILDING = "BUILDING"
    ROAD = "ROAD"
    SETBACK = "SETBACK"
    DIMENSION = "DIMENSION"
    ANNOTATION = "ANNOTATION"
    UNKNOWN = "UNKNOWN"


class SpatialRelationType(str, Enum):
    """Qualitative spatial relation between two geometric entities."""

    ADJACENT = "ADJACENT"
    OVERLAPPING = "OVERLAPPING"
    CONTAINS = "CONTAINS"
    CONTAINED_BY = "CONTAINED_BY"
    DISJOINT = "DISJOINT"
    FACING = "FACING"
    UNKNOWN = "UNKNOWN"
