"""
Phase 3 orchestration: `ExtractionResult` -> `NormalizedPlan`.

    build_normalized_plan(extraction_result, plan_id) -> NormalizedPlan

This is the ONLY function later phases (RuleEngine, report generation,
frontend) should call from this package. Everything else in
`backend/spatial_reasoning/` is an implementation detail.

Known limitation (Phase 1 contract, not fixable here without a breaking
schema change): `schemas.geometry.Dimension` carries no `page` field, so
when a document has multiple pages this pipeline reasons about
dimensions/candidates across the whole document rather than strictly
per-page. Real-world plan PDFs in this project are one plan per sheet,
so in practice this only matters for multi-sheet documents, and is
surfaced in `overall_confidence_note` when more than one page exists.
"""

from __future__ import annotations

import re
from typing import Optional

from backend.config import get_settings
from backend.schemas.candidates import BuildingCandidate
from backend.schemas.enums import ConfidenceLevel
from backend.schemas.evidence import Confidence, Conflict, GeometryEvidence, ValueField
from backend.schemas.extraction import ExtractionResult
from backend.schemas.geometry import CoordinateSpace, NormalizedGeometry, Polygon
from backend.schemas.normalized_plan import (
    BuildingSection,
    NormalizedPlan,
    PlotSection,
    RoadSection,
    SetbackSection,
)
from backend.schemas.units import CanonicalUnit, UnitValue
from backend.spatial_reasoning import geometry_utils as geo
from backend.spatial_reasoning.areas import (
    _MAX_PHYSICALLY_PLAUSIBLE_COVERAGE_RATIO,
    coverage_field,
    far_field,
    find_labeled_areas,
    polygon_area_field,
)
from backend.spatial_reasoning.building_resolution import filter_and_rank_buildings, surviving_candidates
from backend.spatial_reasoning.consistency import check_physical_consistency
from backend.spatial_reasoning.dimension_classification import (
    ClassifiedDimension,
    DimensionSemanticType,
    classify_dimensions,
    vision_only_dimensions,
)
from backend.spatial_reasoning.evidence_reconciliation import EvidenceCandidate, to_value_field
from backend.spatial_reasoning.front_side import FrontSideResolution, resolve_front_side
from backend.spatial_reasoning.plot_resolution import plot_confidence, score_plot_candidates
from backend.spatial_reasoning.road_access import best_road_candidate, collect_access_evidence
from backend.spatial_reasoning.scale import dimension_length_metres, estimate_scale
from backend.spatial_reasoning.setbacks import compute_setbacks
from backend.spatial_reasoning.final_fusion import apply_final_agreement_to_plan
from backend.spatial_reasoning.vision_semantics import (
    floor_count_from_vision,
    preferred_vision_area,
    preferred_vision_value,
    semantic_frame_from_dimensions,
    site_small_dimension_candidates,
    vision_setbacks,
    setback_values_from_native_site,
)

_FLOOR_COUNT_RE = re.compile(
    r"\b(?:g\s*\+\s*(?P<plus>\d+)|no\.?\s*of\s*floors?\s*[:\-]?\s*(?P<explicit>\d+)|"
    r"(?P<stories>\d+)\s*(?:storey|story|floors?)\b)",
    re.I,
)


def _empty_plan(plan_id: str, document_id: str, reason: str) -> NormalizedPlan:
    missing = lambda r=reason: ValueField.missing(r)  # noqa: E731
    return NormalizedPlan(
        plan_id=plan_id,
        source_document_id=document_id,
        plot=PlotSection(width=missing(), depth=missing(), area=missing()),
        building=BuildingSection(width=missing(), depth=missing(), footprint_area=missing()),
        road=RoadSection(width=missing()),
        setbacks=SetbackSection(front=missing(), rear=missing(), left=missing(), right=missing()),
        coverage=missing(),
        far=missing(),
        overall_confidence_note=reason,
    )


def _building_polygon(bc: BuildingCandidate) -> Optional[Polygon]:
    if bc.geometry is None:
        return None
    if bc.geometry.polygon is not None:
        return bc.geometry.polygon
    if bc.geometry.bounding_box is not None:
        return geo.bbox_to_polygon(bc.geometry.bounding_box)
    return None


def _value_metres_lookup(points_per_metre: float):
    def _lookup(dim) -> Optional[float]:
        text_value = dimension_length_metres(dim)
        if text_value is not None:
            return text_value
        if dim.geometry is not None:
            return dim.geometry.length / points_per_metre
        return None

    return _lookup


def _scale_front_side(fsr: FrontSideResolution, points_per_metre: float) -> FrontSideResolution:
    return FrontSideResolution(
        front_edges=[geo.scale_line(e, points_per_metre) for e in fsr.front_edges],
        rear_edges=[geo.scale_line(e, points_per_metre) for e in fsr.rear_edges],
        left_edges=[geo.scale_line(e, points_per_metre) for e in fsr.left_edges],
        right_edges=[geo.scale_line(e, points_per_metre) for e in fsr.right_edges],
        confidence=fsr.confidence,
        reasoning=fsr.reasoning,
        evidence_level=fsr.evidence_level,
    )


def _detect_floor_count(text_evidence) -> Optional[ValueField[int]]:
    for t in text_evidence:
        m = _FLOOR_COUNT_RE.search(t.raw_text or "")
        if not m:
            continue
        if m.group("plus") is not None:
            value = int(m.group("plus")) + 1  # "G+1" = ground + 1 upper floor = 2 floors
            note = f"Floor count parsed from 'G+{m.group('plus')}' label."
        elif m.group("explicit") is not None:
            value = int(m.group("explicit"))
            note = "Floor count parsed from an explicit 'No. of floors' label."
        else:
            value = int(m.group("stories"))
            note = "Floor count parsed from a '<N> storey/floors' label."
        return ValueField[int](
            value=value,
            confidence=Confidence(level=ConfidenceLevel.MEDIUM, reason=note),
            source="text label",
        )
    return None


def _vision_document(extraction: ExtractionResult):
    if not extraction.vision_pages:
        return None
    from backend.schemas.vision import VisionDocumentResult
    return VisionDocumentResult(pages=extraction.vision_pages, model_name="pipeline", enabled=True)

def build_normalized_plan(extraction: ExtractionResult, plan_id: Optional[str] = None) -> NormalizedPlan:
    plan_id = plan_id or f"plan-{extraction.document_id}"

    winner, all_scored, plot_note = score_plot_candidates(extraction)
    if winner is None:
        # Independent CV is the Phase-3.3 authority. If the legacy/global
        # candidate pool cannot identify a plot, still build the plan from
        # the independent site-plan evidence instead of returning all fields
        # as MISSING.
        if extraction.independent_cv is not None and extraction.independent_cv.measurements:
            from backend.spatial_reasoning.final_fusion import build_final_agreement
            from backend.schemas.vision import VisionDocumentResult
            empty_vision = VisionDocumentResult(model_name="disabled", pages=[], enabled=False)
            fusion = build_final_agreement(extraction.independent_cv, _vision_document(extraction) or empty_vision)
            missing = lambda reason: ValueField.missing(reason)
            def fv(name, unit):
                item = fusion["values"].get(name)
                if item and item["value"] is not None and item["status"] != "CONFLICT":
                    return ValueField(value=item["value"], normalized_value=UnitValue(magnitude=item["value"], unit=unit),
                        confidence=Confidence(level=ConfidenceLevel.HIGH if item["status"] == "AGREED" else ConfidenceLevel.MEDIUM, reason=item["reason"]), source=item["source"])
                return missing(f"No non-conflicting independent evidence for {name}.")
            plan = NormalizedPlan(
                plan_id=plan_id, source_document_id=extraction.document_id,
                plot=PlotSection(width=fv("plot.width","m"), depth=fv("plot.depth","m"), area=fv("plot.area","m2")),
                building=BuildingSection(width=fv("building.width","m"), depth=fv("building.depth","m"), footprint_area=fv("building.footprint_area","m2")),
                road=RoadSection(width=fv("road.width","m")),
                setbacks=SetbackSection(front=fv("setbacks.front","m"), rear=fv("setbacks.rear","m"), left=fv("setbacks.left","m"), right=fv("setbacks.right","m")),
                coverage=fv("coverage","%"), far=fv("far","ratio"),
                overall_confidence_note="Legacy global plot resolver had no winner; final values came from independent CV + Vision agreement layer.",
            )
            return plan
        return _empty_plan(
            plan_id, extraction.document_id, f"Cannot resolve a NormalizedPlan: {plot_note}"
        )

    plot_candidate = winner.candidate
    page = plot_candidate.geometry.source_page
    plot_conf_level, plot_conf_reason = plot_confidence(winner, all_scored)

    # Vision is allowed to supply semantics, while native dimension lines
    # supply the actual page-space anchors. This is the key distinction that
    # prevents a bad OpenCV contour from turning into a bad plot/building
    # measurement.
    vision_plot_width = preferred_vision_value(extraction, page, "PLOT_WIDTH")
    vision_plot_depth = preferred_vision_value(extraction, page, "PLOT_DEPTH")
    vision_building_width = preferred_vision_value(extraction, page, "BUILDING_WIDTH")
    vision_building_depth = preferred_vision_value(extraction, page, "BUILDING_DEPTH")

    semantic_plot_bbox = None
    if vision_plot_width is not None and vision_plot_depth is not None:
        semantic_plot_bbox = semantic_frame_from_dimensions(
            extraction, page, vision_plot_width, vision_plot_depth
        )

    semantic_building_bbox = None
    if semantic_plot_bbox is not None and vision_building_width is not None and vision_building_depth is not None:
        semantic_building_bbox = semantic_frame_from_dimensions(
            extraction, page, vision_building_width, vision_building_depth, semantic_plot_bbox
        )

    plot_polygon_page = (
        geo.bbox_to_polygon(semantic_plot_bbox)
        if semantic_plot_bbox is not None
        else plot_candidate.geometry.polygon
    )

    # --- scale --------------------------------------------------------
    plot_bbox_page = semantic_plot_bbox or (plot_candidate.geometry.bounding_box if plot_candidate.geometry else None)
    scale_est = estimate_scale(
        extraction.dimensions,
        page=page,
        region_bbox=plot_bbox_page,
        region_padding_factor=get_settings().local_scale_region_padding_factor,
    )
    # If semantic plot dimensions are explicit metres, derive the page scale
    # directly from the semantic frame. This prevents unrelated floor/detail
    # dimensions from contaminating scale estimation.
    if semantic_plot_bbox is not None and vision_plot_width and vision_plot_depth:
        x_ppm = semantic_plot_bbox.width / vision_plot_width
        y_ppm = semantic_plot_bbox.height / vision_plot_depth
        if x_ppm > 1e-6 and y_ppm > 1e-6:
            ppm = (x_ppm + y_ppm) / 2.0
            scale_est.reason = (
                f"Semantic plot frame scale: {x_ppm:.3f} pt/m horizontally and "
                f"{y_ppm:.3f} pt/m vertically; unrelated sheet dimensions excluded."
            )
        else:
            ppm = scale_est.points_per_metre
    else:
        ppm = scale_est.points_per_metre

    # --- building -------------------------------------------------------
    filtered_buildings = filter_and_rank_buildings(extraction, plot_candidate)
    survivors = surviving_candidates(filtered_buildings)
    survivor_polygons_page = [p for p in (_building_polygon(b) for b in survivors) if p is not None]
    primary_building = (
        max(survivors, key=lambda b: (_building_polygon(b).area if _building_polygon(b) else 0))
        if survivors
        else None
    )
    if semantic_building_bbox is not None:
        semantic_building_polygon = geo.bbox_to_polygon(semantic_building_bbox)
    else:
        semantic_building_polygon = _building_polygon(primary_building) if primary_building else None

    # --- road / access ---------------------------------------------------
    road_candidate = best_road_candidate(extraction, page)
    road_bbox_page = road_candidate.geometry.bounding_box if (road_candidate and road_candidate.geometry) else None
    road_bboxes_page = [road_bbox_page] if road_bbox_page is not None else []
    access_evidence = collect_access_evidence(extraction.text_evidence, page)

    # --- front-side reasoning (page space, then scaled) -------------------
    fsr_page = resolve_front_side(plot_polygon_page, road_bbox_page, access_evidence)
    fsr_metric = _scale_front_side(fsr_page, ppm)

    # --- dimension classification (page space) ----------------------------
    classified: list[ClassifiedDimension] = classify_dimensions(
        extraction.dimensions,
        plot_polygon_page,
        survivor_polygons_page,
        fsr_page.front_edges,
        fsr_page.rear_edges,
        fsr_page.left_edges,
        fsr_page.right_edges,
        road_bboxes_page,
        _value_metres_lookup(ppm),
        extraction.vision_pages,
    )
    # Vision <-> CV fusion, part 2: dimensions the vision model read that
    # the deterministic native/OCR pipeline never produced a candidate for
    # at all (as opposed to the vision-as-hint fusion already wired into
    # classify_dimensions() above, which only re-labels dimensions that
    # already existed). See vision_only_dimensions()'s docstring.
    classified = classified + vision_only_dimensions(extraction.vision_pages, extraction.dimensions, page)

    def _classified_of(t: DimensionSemanticType) -> list[ClassifiedDimension]:
        return [c for c in classified if c.semantic_type is t and c.value_metres is not None]

    # --- metric-space geometry --------------------------------------------
    plot_polygon_metric = geo.scale_polygon(plot_polygon_page, ppm)
    building_polygon_metric = (
        geo.scale_polygon(semantic_building_polygon, ppm) if semantic_building_polygon is not None else
        (geo.scale_polygon(_building_polygon(primary_building), ppm) if primary_building else None)
    )

    # --- plot width/depth (front/rear edge length vs classified dims) -----
    def _edge_group_length_candidates(edges_page, label: str) -> list[EvidenceCandidate]:
        out = []
        for e in edges_page:
            out.append(
                EvidenceCandidate(
                    value=round(e.length / ppm, 4),
                    source=f"geometry ({label} edge)",
                    evidence=GeometryEvidence(description=f"{label} plot boundary edge length, scaled to metres."),
                )
            )
        return out

    # Prefer semantically grounded VLM values for the core dimensions. The
    # native dimension layer is the measurement evidence; CV edge lengths are
    # only a fallback because this sheet contains many unrelated rectangles.
    if vision_plot_width is not None:
        plot_width = to_value_field(
            [EvidenceCandidate(value=round(vision_plot_width, 4), source="vision semantic PLOT_WIDTH + native text grounding")],
            "plot.width",
        )
    else:
        width_candidates = _edge_group_length_candidates(fsr_page.front_edges, "front") + _edge_group_length_candidates(
            fsr_page.rear_edges, "rear"
        )
        for c in _classified_of(DimensionSemanticType.PLOT_WIDTH):
            width_candidates.append(
                EvidenceCandidate(value=round(c.value_metres, 4), source=f"dimension label ('{c.dimension.label}')")
            )
        plot_width = to_value_field(width_candidates, "plot.width", "No frontage edge or PLOT_WIDTH dimension found.")

    if vision_plot_depth is not None:
        plot_depth = to_value_field(
            [EvidenceCandidate(value=round(vision_plot_depth, 4), source="vision semantic PLOT_DEPTH + native text grounding")],
            "plot.depth",
        )
    else:
        depth_candidates = _edge_group_length_candidates(fsr_page.left_edges, "left") + _edge_group_length_candidates(
            fsr_page.right_edges, "right"
        )
        for c in _classified_of(DimensionSemanticType.PLOT_DEPTH):
            depth_candidates.append(
                EvidenceCandidate(value=round(c.value_metres, 4), source=f"dimension label ('{c.dimension.label}')")
            )
        plot_depth = to_value_field(depth_candidates, "plot.depth", "No lateral edge or PLOT_DEPTH dimension found.")

    plot_rectangularity = geo.rectangularity(plot_polygon_page)
    if plot_width.value is not None and plot_depth.value is not None:
        plot_area = ValueField[float](
            value=round(plot_width.value * plot_depth.value, 4),
            normalized_value=UnitValue(magnitude=round(plot_width.value * plot_depth.value, 4), unit=CanonicalUnit.SQUARE_METRE.value),
            confidence=Confidence(level=ConfidenceLevel.HIGH, reason="Plot area computed from semantically resolved plot width x depth."),
            source="plot.area = plot.width x plot.depth",
        )
    else:
        plot_area = polygon_area_field(plot_polygon_metric, plot_width, plot_depth, plot_rectangularity, "plot.area")

    plot_section = PlotSection(
        geometry=NormalizedGeometry(
            coordinate_space=CoordinateSpace.METRIC_PLAN,
            polygon=plot_polygon_metric,
            bounding_box=plot_polygon_metric.bounding_box,
            points_per_metre=ppm,
            rotation_degrees=plot_candidate.geometry.rotation_degrees,
            source_page=page,
        ),
        width=plot_width,
        depth=plot_depth,
        area=plot_area,
    )

    # --- building width/depth/footprint ------------------------------------
    if (primary_building is not None and building_polygon_metric is not None) or semantic_building_bbox is not None:
        frontage_deg = (
            geo.line_orientation_degrees(fsr_page.front_edges[0]) if fsr_page.front_edges else 0.0
        )
        bbox_page = semantic_building_bbox or (_building_polygon(primary_building).bounding_box if primary_building else None)
        horiz_deg = 0.0
        align_horizontal = geo.orientation_alignment(frontage_deg, horiz_deg)
        if bbox_page is not None and semantic_building_bbox is None:
            if align_horizontal >= 0.5:
                width_px, depth_px = bbox_page.width, bbox_page.height
            else:
                width_px, depth_px = bbox_page.height, bbox_page.width
        else:
            width_px = bbox_page.width if bbox_page is not None else 0.0
            depth_px = bbox_page.height if bbox_page is not None else 0.0

        if vision_building_width is not None:
            building_width = to_value_field(
                [EvidenceCandidate(value=round(vision_building_width, 4), source="vision semantic BUILDING_WIDTH + native text grounding")],
                "building.width",
            )
        else:
            b_width_candidates = [EvidenceCandidate(value=round(width_px / ppm, 4), source="geometry (building bounding box, frontage-aligned side)", evidence=GeometryEvidence(description="Building footprint extent aligned with the plot frontage axis."))]
            for c in _classified_of(DimensionSemanticType.BUILDING_WIDTH):
                b_width_candidates.append(EvidenceCandidate(value=round(c.value_metres, 4), source=f"dimension label ('{c.dimension.label}')"))
            building_width = to_value_field(b_width_candidates, "building.width")

        if vision_building_depth is not None:
            building_depth = to_value_field(
                [EvidenceCandidate(value=round(vision_building_depth, 4), source="vision semantic BUILDING_DEPTH + native text grounding")],
                "building.depth",
            )
        else:
            b_depth_candidates = [EvidenceCandidate(value=round(depth_px / ppm, 4), source="geometry (building bounding box, depth-aligned side)", evidence=GeometryEvidence(description="Building footprint extent aligned with the plot depth axis."))]
            for c in _classified_of(DimensionSemanticType.BUILDING_DEPTH):
                b_depth_candidates.append(EvidenceCandidate(value=round(c.value_metres, 4), source=f"dimension label ('{c.dimension.label}')"))
            building_depth = to_value_field(b_depth_candidates, "building.depth")

        explicit_plinth = preferred_vision_area(extraction, page, "PLINTH_AREA")
        footprint_source = "building.footprint_area (sum of validated building block polygons)"
        if explicit_plinth is not None:
            footprint_area_m2 = explicit_plinth
            footprint_note = "Explicit PLINTH_AREA from vision, grounded to native page text."
            footprint_level = ConfidenceLevel.HIGH
            footprint_source = "building.footprint_area = explicit PLINTH_AREA"
        elif building_width.value is not None and building_depth.value is not None:
            footprint_area_m2 = building_width.value * building_depth.value
            footprint_note = "Building footprint computed from semantically resolved building width x depth."
            footprint_level = ConfidenceLevel.HIGH
            footprint_source = "building.footprint_area = building.width x building.depth"
        else:
            total_footprint_pts2 = sum(p.area for p in survivor_polygons_page)
            footprint_area_m2 = total_footprint_pts2 / (ppm**2)
            footprint_note = f"Sum of {len(survivors)} validated building block(s)' footprint polygon area(s), scaled to square metres."
            footprint_level = ConfidenceLevel.HIGH if len(survivors) <= 2 else ConfidenceLevel.MEDIUM
        if plot_area.value is not None and plot_area.value > 0 and (
            footprint_area_m2 / plot_area.value > _MAX_PHYSICALLY_PLAUSIBLE_COVERAGE_RATIO
        ):
            # FIX #9 (phase3.1): a building footprint that exceeds the
            # resolved plot area is physically impossible, not just
            # "high coverage" — almost always a sign the plot/building
            # candidates that got paired together don't actually refer to
            # the same real-world site (mismatched drawing regions/scale
            # rather than a true measurement). Mark the value itself
            # CONFLICTING (not a bogus number) so it doesn't silently
            # propagate into coverage/FAR either.
            footprint_area = ValueField[float].conflicting(
                Conflict(
                    description=(
                        f"Computed building.footprint_area ({footprint_area_m2:.3f} m²) exceeds "
                        f"the resolved plot.area ({plot_area.value:.3f} m²), which is physically "
                        "impossible. This most likely means the selected building candidate does "
                        "not actually belong to the same drawing region as the selected plot "
                        "candidate."
                    ),
                    conflicting_raw_values=[
                        UnitValue(magnitude=round(footprint_area_m2, 4), unit=CanonicalUnit.SQUARE_METRE.value),
                        UnitValue(magnitude=plot_area.value, unit=CanonicalUnit.SQUARE_METRE.value),
                    ],
                    conflicting_sources=["building.footprint_area (computed)", "plot.area"],
                )
            )
        else:
            footprint_area = ValueField[float](
                value=round(footprint_area_m2, 4),
                normalized_value=UnitValue(
                    magnitude=round(footprint_area_m2, 4), unit=CanonicalUnit.SQUARE_METRE.value
                ),
                confidence=Confidence(level=footprint_level, reason=footprint_note),
                source=footprint_source,
            )

        floor_count_value = floor_count_from_vision(extraction, page)
        floor_count = _detect_floor_count(extraction.text_evidence) if floor_count_value is None else ValueField[int](
            value=floor_count_value,
            confidence=Confidence(level=ConfidenceLevel.HIGH, reason="Distinct floor-plan regions identified by the vision semantic layer."),
            source="vision floor-plan region count",
        )

        building_section = BuildingSection(
            geometry=NormalizedGeometry(
                coordinate_space=CoordinateSpace.METRIC_PLAN,
                polygon=building_polygon_metric,
                bounding_box=building_polygon_metric.bounding_box,
                points_per_metre=ppm,
                rotation_degrees=(primary_building.geometry.rotation_degrees if primary_building is not None else 0.0),
                source_page=(primary_building.geometry.source_page if primary_building is not None else page),
            ),
            width=building_width,
            depth=building_depth,
            footprint_area=footprint_area,
            floor_count=floor_count,
        )
    else:
        building_section = BuildingSection(
            width=ValueField.missing("No building candidate survived filtering (rooms/furniture/compound-wall exclusion)."),
            depth=ValueField.missing("No building candidate survived filtering."),
            footprint_area=ValueField.missing("No building candidate survived filtering."),
        )

    # --- road ---------------------------------------------------------------
    road_candidates_for_width: list[EvidenceCandidate] = []
    road_geometry = None
    if (
        road_candidate is not None
        and road_candidate.geometry is not None
        and not road_candidate.id.startswith("text-road-")
        and re.search(r"\d", road_candidate.name_or_label or "")
    ):
        bbox = road_candidate.geometry.bounding_box
        if bbox is not None:
            road_width_px = min(bbox.width, bbox.height)
            road_candidates_for_width.append(
                EvidenceCandidate(
                    value=round(road_width_px / ppm, 4),
                    source="geometry (road candidate short-side width; explicitly labeled road candidate)",
                    evidence=GeometryEvidence(description="Road candidate with an explicit numeric road label."),
                )
            )
            road_geometry = NormalizedGeometry(
                coordinate_space=CoordinateSpace.METRIC_PLAN,
                bounding_box=geo.scale_bbox(bbox, ppm),
                points_per_metre=ppm,
                rotation_degrees=road_candidate.geometry.rotation_degrees,
                source_page=road_candidate.geometry.source_page,
            )
    vision_road_width = preferred_vision_value(extraction, page, "ROAD_WIDTH")
    if vision_road_width is not None:
        road_candidates_for_width.append(
            EvidenceCandidate(value=round(vision_road_width, 4), source="vision semantic ROAD_WIDTH + native text grounding")
        )
    else:
        # Geometry-only proximity to a road is not enough to call a number a
        # road width; floor/parking dimensions frequently sit beside the road
        # region. Require an explicit textual road association here.
        for c in _classified_of(DimensionSemanticType.ROAD_WIDTH):
            if re.search(r"\broad\b", c.dimension.label or "", re.I):
                road_candidates_for_width.append(
                    EvidenceCandidate(value=round(c.value_metres, 4), source=f"dimension label ('{c.dimension.label}')")
                )
    road_width = to_value_field(
        road_candidates_for_width, "road.width", "No explicitly dimensioned road width found."
    )
    road_section = RoadSection(geometry=road_geometry, width=road_width)

    # --- setbacks -------------------------------------------------------------
        # --- setbacks -------------------------------------------------------------
    setbacks_section = compute_setbacks(
        building_polygon_metric,
        fsr_metric,
        classified,
        ppm,
        plot_width=plot_width,
        plot_depth=plot_depth,
        building_width=building_section.width,
        building_depth=building_section.depth,
    )

    # Replace noisy geometry-derived setbacks with explicit small site-plan
    # dimensions when the semantic site/building frames are available. This
    # is still evidence-first: values come from printed dimension labels and
    # their spatial orientation, never from a regulation-specific default.
    explicit_setbacks = vision_setbacks(extraction, page)
    native_site_setbacks = setback_values_from_native_site(
        extraction, page, semantic_plot_bbox, road_bbox_page, ppm
    )
    explicit_setbacks = {**native_site_setbacks, **explicit_setbacks}
    if semantic_plot_bbox is not None:
        # Once a semantic site frame has been established, do not retain a
        # noisy CV distance for a side that has no explicit setback label.
        # Missing is preferable to an apparently precise but unsupported
        # number.
        for side in ("front", "rear", "left", "right"):
            value = explicit_setbacks.get(side)
            if value is None:
                setattr(setbacks_section, side, ValueField[float].missing(
                    "No explicit setback dimension was found for this plot edge."
                ))
            else:
                setattr(
                    setbacks_section,
                    side,
                    ValueField[float](
                        value=round(value, 4),
                        normalized_value=UnitValue(magnitude=round(value, 4), unit=CanonicalUnit.METRE.value),
                        confidence=Confidence(level=ConfidenceLevel.HIGH, reason="Explicit site-plan setback dimension, spatially assigned to the plot edge."),
                        source="site-plan setback dimension",
                    ),
                )
    else:
        for side, value in explicit_setbacks.items():
            setattr(
                setbacks_section,
                side,
                ValueField[float](
                    value=round(value, 4),
                    normalized_value=UnitValue(magnitude=round(value, 4), unit=CanonicalUnit.METRE.value),
                    confidence=Confidence(level=ConfidenceLevel.HIGH, reason="Explicit site-plan setback dimension, spatially assigned to the plot edge."),
                    source="site-plan setback dimension",
                ),
            )

    # --- coverage / FAR ---------------------------------------------------------
    coverage = coverage_field(building_section.footprint_area, plot_area)
    far = far_field(building_section.footprint_area, plot_area, building_section.floor_count)

    # --- evidence reconciliation conflicts + physical consistency ---------------
    conflicts = []
    for vf in (
        plot_width,
        plot_depth,
        plot_area,
        building_section.width,
        building_section.depth,
        building_section.footprint_area,
        coverage,
        far,
        road_width,
        setbacks_section.front,
        setbacks_section.rear,
        setbacks_section.left,
        setbacks_section.right,
    ):
        if vf.conflict is not None:
            conflicts.append(vf.conflict)
    conflicts.extend(check_physical_consistency(plot_section, building_section, setbacks_section))

    # --- overall confidence note ------------------------------------------------
    labeled_areas = find_labeled_areas(extraction.text_evidence)
    labeled_area_note = (
        "; ".join(f"{a.label}={a.value}{a.unit or ''}" for a in labeled_areas)
        if labeled_areas
        else "none found"
    )
    rejected_buildings = [f for f in filtered_buildings if not f.kept]
    note_parts = [
        f"Plot: {plot_note} {plot_conf_reason}",
        f"Scale: {scale_est.reason}",
        f"Front-side: {fsr_page.reasoning}",
        f"Building blocks kept: {len(survivors)}, rejected: {len(rejected_buildings)}.",
        f"Distinct labeled areas in source text (kept separate from footprint_area): {labeled_area_note}.",
    ]
    if len({p for p in (page,) }) and page is not None:
        note_parts.append(f"Resolved from page {page}.")
    overall_note = " | ".join(note_parts)

    plan = NormalizedPlan(
        plan_id=plan_id,
        source_document_id=extraction.document_id,
        plot=plot_section,
        building=building_section,
        road=road_section,
        setbacks=setbacks_section,
        coverage=coverage,
        far=far,
        conflicts=conflicts,
        overall_confidence_note=overall_note,
    )
    # Authoritative Phase-3.3+ output: if the independent CV result exists,
    # reconcile it directly with independent Vision evidence. This prevents
    # the legacy 400+ candidate pool from contaminating final values.
    return apply_final_agreement_to_plan(plan, extraction.independent_cv, _vision_document(extraction))


__all__ = ["build_normalized_plan"]
