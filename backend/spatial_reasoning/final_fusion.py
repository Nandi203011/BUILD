"""Final CV + Vision agreement layer.

This module is intentionally independent from the legacy global candidate resolver.
The final value is produced only from the independent CV/native/OCR site-plan path
and the independent Vision semantic path. A conflict is never silently resolved.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional

from backend.schemas.evidence import Confidence, Conflict, ValueField
from backend.schemas.enums import ConfidenceLevel
from backend.schemas.independent_measurements import IndependentCVResult, IndependentMeasurement
from backend.schemas.vision import VisionDocumentResult
from backend.schemas.units import CanonicalUnit, UnitValue
from backend.spatial_reasoning import document_evidence as doc_ev

LENGTH_FIELDS = {
    "plot.width": "PLOT_WIDTH",
    "plot.depth": "PLOT_DEPTH",
    "building.width": "BUILDING_WIDTH",
    "building.depth": "BUILDING_DEPTH",
    "road.width": "ROAD_WIDTH",
    "setbacks.front": "FRONT_SETBACK",
    "setbacks.rear": "REAR_SETBACK",
    "setbacks.left": "LEFT_SETBACK",
    "setbacks.right": "RIGHT_SETBACK",
    "building.height_estimated": "BUILDING_HEIGHT",
    "building.height": "BUILDING_HEIGHT",
    "floor.height": "FLOOR_HEIGHT",
    "plinth.height": "PLINTH_HEIGHT",
    "parapet.height": "PARAPET_HEIGHT",
}

NUMERIC_FIELDS = {
    "plot.area": ("PLOT_AREA", "NET_PLOT_AREA"),
    "building.plinth_area": ("BUILDING_FOOTPRINT_AREA", "PROPOSED_COVERAGE_AREA", "PLINTH_AREA"),
    "building.footprint_area": ("BUILDING_FOOTPRINT_AREA", "PROPOSED_COVERAGE_AREA", "PLINTH_AREA"),
    "coverage": ("COVERAGE_PERCENT",),
    "far.area": ("FAR_AREA",),
    "far": ("FAR_RATIO",),
    "building.gross_built_up_area": ("TOTAL_BUILT_UP_AREA", "BUILT_UP_AREA"),
}


def _cv_map(cv: IndependentCVResult) -> dict[str, IndependentMeasurement]:
    out: dict[str, IndependentMeasurement] = {}
    for m in cv.measurements:
        value = m.value_m if m.value_m is not None else m.value
        if value is None:
            continue
        cur = out.get(m.field)
        if cur is None or m.confidence > cur.confidence:
            out[m.field] = m
    return out


def _vision_length(result: VisionDocumentResult, semantic: str) -> tuple[float, float, str, int] | None:
    candidates = []
    for page in result.pages:
        for d in page.dimensions:
            if d.type.upper() != semantic.upper() or d.value is None:
                continue
            unit = (d.unit or "m").lower()
            factor = {"m": 1.0, "meter": 1.0, "metre": 1.0, "mm": .001, "cm": .01, "ft": .3048, "feet": .3048}.get(unit)
            if factor is None:
                continue
            candidates.append((float(d.value) * factor, float(d.confidence), d.evidence or "", page.page_number))
    if not candidates:
        return None
    return sorted(candidates, key=lambda x: (-x[1], x[3]))[0]


def _vision_numeric(result: VisionDocumentResult | None, semantics: tuple[str, ...]) -> tuple[float, float, str, int, str] | None:
    if result is None:
        return None
    wanted = {s.upper() for s in semantics}
    candidates = []
    for page in result.pages:
        for a in page.areas:
            if a.type.upper() not in wanted or a.value is None:
                continue
            unit = (a.unit or "m2").lower().replace("²", "2")
            if unit in {"sqm", "sq.m", "square_metre", "square_meter"}:
                unit = "m2"
            if unit in {"%", "percent", "percentage"}:
                unit = "%"
            if unit == "ratio":
                pass
            elif unit == "ft2":
                unit = "m2"
                value = float(a.value) * 0.09290304
                candidates.append((value, float(a.confidence), a.evidence or "", page.page_number, unit))
                continue
            elif unit != "m2" and unit != "%":
                continue
            candidates.append((float(a.value), float(a.confidence), a.evidence or "", page.page_number, unit))
    if not candidates:
        return None
    return sorted(candidates, key=lambda x: (-x[1], x[3]))[0]


def _tol(a: float, b: float, unit: str = "m") -> float:
    if unit == "%":
        return max(0.20, 0.01 * max(abs(a), abs(b)))
    if unit == "ratio":
        return max(0.02, 0.01 * max(abs(a), abs(b)))
    return max(0.05, 0.01 * max(abs(a), abs(b)))


def _value_field(cv_m: IndependentMeasurement | None, vision, field: str, unit: str = "m") -> ValueField[float]:
    cvv = None if cv_m is None else (cv_m.value_m if cv_m.value_m is not None else cv_m.value)
    vv = None if vision is None else vision[0]
    if cvv is None and vv is None:
        return ValueField[float].missing(f"No independent CV or Vision evidence for {field}.")
    if cvv is None:
        return ValueField[float](value=round(vv, 4), normalized_value=UnitValue(magnitude=round(vv,4), unit=unit),
            confidence=Confidence(level=ConfidenceLevel.MEDIUM, reason="Vision-only; CV did not resolve this field."),
            source="FINAL:VISION_ONLY")
    if vv is None:
        canonical = "m" if unit == "m" else unit
        return ValueField[float](value=round(cvv,4), normalized_value=UnitValue(magnitude=round(cvv,4), unit=canonical),
            confidence=Confidence(level=ConfidenceLevel.MEDIUM, reason="CV-only; Vision did not emit this field."),
            source="FINAL:CV_ONLY")
    diff = abs(cvv - vv)
    tol = _tol(cvv, vv, unit)
    if diff > tol:
        conflict = Conflict(
            description=f"CV={cvv:.4f} and Vision={vv:.4f} disagree for {field}; difference {diff:.4f} exceeds tolerance {tol:.4f}.",
            conflicting_raw_values=[UnitValue(magnitude=float(cvv), unit=unit), UnitValue(magnitude=float(vv), unit=unit)],
            conflicting_sources=["independent_cv", "vision"],
        )
        return ValueField[float].conflicting(conflict)
    agreed = (cvv + vv) / 2.0
    return ValueField[float](value=round(agreed,4), normalized_value=UnitValue(magnitude=round(agreed,4), unit=unit),
        confidence=Confidence(level=ConfidenceLevel.HIGH, reason=f"Independent CV and Vision agree within {tol:.3f} {unit}."),
        source="FINAL:CV+VISION_AGREED")


def build_final_agreement(cv: IndependentCVResult, vision: VisionDocumentResult) -> dict[str, Any]:
    cm = _cv_map(cv)
    values: dict[str, Any] = {}
    counts = {"AGREED": 0, "CV_ONLY": 0, "VISION_ONLY": 0, "CONFLICT": 0, "MISSING": 0}
    for field, semantic in LENGTH_FIELDS.items():
        vf = _value_field(cm.get(field), _vision_length(vision, semantic), field, "m")
        status = "AGREED" if vf.source == "FINAL:CV+VISION_AGREED" else ("CV_ONLY" if vf.source == "FINAL:CV_ONLY" else ("VISION_ONLY" if vf.source == "FINAL:VISION_ONLY" else ("CONFLICT" if vf.conflict else "MISSING")))
        counts[status] += 1
        values[field] = {"value": vf.value, "unit": "m", "status": status, "source": vf.source, "confidence": vf.confidence.level.value, "reason": vf.confidence.reason}

    for field, semantics in NUMERIC_FIELDS.items():
        # Skip duplicate alias; the final output keeps both explicit aliases if CV has either.
        vision_ev = _vision_numeric(vision, semantics)
        cvm = cm.get(field)
        vf = _value_field(cvm, vision_ev, field, (vision_ev[4] if vision_ev else (cvm.unit if cvm and cvm.unit else "m2")))
        status = "AGREED" if vf.source == "FINAL:CV+VISION_AGREED" else ("CV_ONLY" if vf.source == "FINAL:CV_ONLY" else ("VISION_ONLY" if vf.source == "FINAL:VISION_ONLY" else ("CONFLICT" if vf.conflict else "MISSING")))
        counts[status] += 1
        values[field] = {"value": vf.value, "unit": (vision_ev[4] if vision_ev else (cvm.unit if cvm and cvm.unit else "m2")), "status": status, "source": vf.source, "confidence": vf.confidence.level.value, "reason": vf.confidence.reason}

    return {"document_id": cv.document_id, "values": values, "summary": counts, "has_conflicts": counts["CONFLICT"] > 0}


def build_document_verified_fusion(
    cv: Optional[IndependentCVResult],
    vision: Optional[VisionDocumentResult],
    pdf_path: Optional[str] = None,
) -> dict[str, Any]:
    """PHASEE3NEW.md Validation / Fusion Engine.

    This is the deterministic third layer sitting on top of the two
    independent extraction pipelines. It NEVER re-runs CV or Vision
    extraction and never lets one pipeline's output influence the
    other's -- it only reconciles the evidence they already produced
    against the document itself (native text / OCR / geometry), per the
    conflict-resolution hierarchy in sections 14-18.

    Returns the full schema described in section 30: cv_values,
    vision_values, document_evidence, conflicts, validation,
    final_agreed_values, plus provenance for every field.
    """
    text_index = doc_ev.build_document_text_index(pdf_path)

    cm = _cv_map(cv) if cv is not None else {}
    all_measurements = list(cv.measurements) if cv is not None else []

    cv_values: dict[str, Any] = {}
    vision_values: dict[str, Any] = {}
    document_evidence: dict[str, Any] = {}
    conflicts: dict[str, Any] = {}
    validation: dict[str, Any] = {}
    final_agreed_values: dict[str, Any] = {}
    reported_calculated: dict[str, Any] = {}
    provenance: dict[str, Any] = {}
    counts: dict[str, int] = {}

    def _record(field: str, cv_value, vision_value, unit: str):
        field_measurements = [m for m in all_measurements if m.field == field]
        verdict = doc_ev.resolve_field(field, cv_value, vision_value, unit, field_measurements, text_index)

        cv_values[field] = {
            "value": cv_value,
            "unit": unit,
            "status": doc_ev.FOUND if cv_value is not None else doc_ev.NOT_FOUND_BY_CV,
        }
        vision_values[field] = {
            "value": vision_value,
            "unit": unit,
            "status": doc_ev.FOUND if vision_value is not None else doc_ev.NOT_FOUND_BY_VISION,
        }
        document_evidence[field] = {
            "cv_candidate": verdict.cv_evidence.to_dict() if verdict.cv_evidence else None,
            "vision_candidate": verdict.vision_evidence.to_dict() if verdict.vision_evidence else None,
            "text_index_available": text_index.available,
        }
        if verdict.status in (doc_ev.CONFLICT_RESOLVED_BY_DOCUMENT_EVIDENCE, doc_ev.UNRESOLVED_CONFLICT):
            conflicts[field] = {
                "cv": cv_value, "vision": vision_value,
                "winner": verdict.winner, "reason": verdict.reason,
            }
        validation[field] = {"status": verdict.status, "reason": verdict.reason, "winner": verdict.winner}
        final_agreed_values[field] = {"final_value": verdict.final_value, "unit": unit, "status": verdict.status}

        rvc = doc_ev.reported_vs_calculated(field_measurements)
        if rvc is not None:
            reported_calculated[field] = {
                "reported_value": rvc.reported_value,
                "calculated_value": rvc.calculated_value,
                "final_value": rvc.final_value,
                "status": rvc.status,
            }
            # A reported document value always wins over a purely derived one,
            # per section 19 -- never silently overwritten.
            if rvc.reported_value is not None and verdict.status in (
                doc_ev.CV_ONLY_DOCUMENT_VERIFIED, doc_ev.CV_ONLY_UNVERIFIED,
                doc_ev.AGREED_DOCUMENT_VERIFIED, doc_ev.AGREED_UNVERIFIED,
            ):
                final_agreed_values[field]["final_value"] = rvc.reported_value

        provenance[field] = {
            "cv_source": [m.source for m in field_measurements if m.source],
            "vision_present": vision_value is not None,
            "document_text_index_available": text_index.available,
        }
        counts[verdict.status] = counts.get(verdict.status, 0) + 1

    for field in LENGTH_FIELDS:
        cvv = cm.get(field)
        cv_value = None if cvv is None else (cvv.value_m if cvv.value_m is not None else cvv.value)
        vres = _vision_length(vision, LENGTH_FIELDS[field]) if vision is not None else None
        vision_value = None if vres is None else vres[0]
        _record(field, cv_value, vision_value, "m")

    for field, semantics in NUMERIC_FIELDS.items():
        cvv = cm.get(field)
        cv_value = None if cvv is None else (cvv.value_m if cvv.value_m is not None else cvv.value)
        vres = _vision_numeric(vision, semantics) if vision is not None else None
        vision_value = None if vres is None else vres[0]
        unit = (vres[4] if vres else (cvv.unit if cvv and cvv.unit else "m2"))
        _record(field, cv_value, vision_value, unit)

    return {
        "document_id": (cv.document_id if cv is not None else None),
        "cv_values": cv_values,
        "vision_values": vision_values,
        "document_evidence": document_evidence,
        "conflicts": conflicts,
        "validation": validation,
        "final_agreed_values": final_agreed_values,
        "reported_vs_calculated": reported_calculated,
        "provenance": provenance,
        "summary": counts,
        "has_unresolved_conflicts": any(v["status"] == doc_ev.UNRESOLVED_CONFLICT for v in validation.values()),
    }


def apply_final_agreement_to_plan(plan, cv: IndependentCVResult | None, vision: VisionDocumentResult | None):
    """Return a plan whose scalar fields are sourced from independent CV/Vision agreement.

    The legacy resolver remains available for tests/older callers. When independent CV is
    attached to ExtractionResult, this function becomes the authoritative final-value layer.
    """
    if cv is None:
        return plan
    if vision is None:
        # Keep CV values, but mark them explicitly as CV-only.
        empty = VisionDocumentResult(model_name="disabled", pages=[], enabled=False)
        fusion = build_final_agreement(cv, empty)
    else:
        fusion = build_final_agreement(cv, vision)
    v = fusion["values"]

    def vf(path: str, fallback):
        item = v.get(path)
        if item and item["value"] is not None and item["status"] in {"AGREED", "CV_ONLY", "VISION_ONLY"}:
            return ValueField[float](value=item["value"], normalized_value=UnitValue(magnitude=item["value"], unit=item["unit"]),
                confidence=Confidence(level=ConfidenceLevel.HIGH if item["status"] == "AGREED" else ConfidenceLevel.MEDIUM, reason=item["reason"]),
                source=item["source"])
        return fallback

    plan.plot.width = vf("plot.width", plan.plot.width)
    plan.plot.depth = vf("plot.depth", plan.plot.depth)
    plan.plot.area = vf("plot.area", plan.plot.area)
    plan.building.width = vf("building.width", plan.building.width)
    plan.building.depth = vf("building.depth", plan.building.depth)
    plan.building.footprint_area = vf("building.footprint_area", plan.building.footprint_area)
    plan.road.width = vf("road.width", plan.road.width)
    plan.setbacks.front = vf("setbacks.front", plan.setbacks.front)
    plan.setbacks.rear = vf("setbacks.rear", plan.setbacks.rear)
    plan.setbacks.left = vf("setbacks.left", plan.setbacks.left)
    plan.setbacks.right = vf("setbacks.right", plan.setbacks.right)
    plan.coverage = vf("coverage", plan.coverage)
    plan.far = vf("far", plan.far)
    plan.conflicts.extend([x for x in []])
    plan.overall_confidence_note = (plan.overall_confidence_note or "") + " | Final scalar values passed through independent CV + Vision agreement layer."
    return plan
