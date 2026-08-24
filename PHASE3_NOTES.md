# BUILDCheck India — Phase 3 Notes

**Owner:** Teammate 3
**Scope:** computational geometry & spatial reasoning layer only
(`backend/spatial_reasoning/`). Converts Phase 2's `ExtractionResult`
(raw, page-space candidates) into the single, resolved Phase 1
`NormalizedPlan` contract (`backend/schemas/normalized_plan.py`), in
metric plan space. No RAG/RASE or RuleEngine logic is implemented here.

## Entry point

```python
from backend.spatial_reasoning.pipeline import build_normalized_plan

plan = build_normalized_plan(extraction_result, plan_id="optional-id")
# plan: NormalizedPlan
```

This is the only function later phases (RuleEngine, report generation,
frontend) should call from this package.

## What was built

```
backend/spatial_reasoning/
    geometry_utils.py           Generic polygon/segment geometry helpers
                                 (point-in-polygon, segment/segment and
                                 point/segment distance, rectangularity,
                                 aspect ratio, polygon-to-edge-group
                                 distance, scaling PAGE_POINTS -> METRIC_PLAN)
    scale.py                    Points-per-metre estimation from multiple
                                 labelled dimensions, robust median/MAD
                                 aggregation, outlier rejection, confidence
    dimension_classification.py Semantic labeling of raw Dimension
                                 candidates (PLOT_WIDTH, PLOT_DEPTH,
                                 BUILDING_WIDTH/DEPTH, FRONT/REAR/LEFT/
                                 RIGHT_SETBACK, ROAD_WIDTH, ROOM_DIMENSION,
                                 OTHER) — geometry-first, not "find number
                                 -> nearby keyword"
    plot_resolution.py          Multi-feature plot candidate scoring
                                 (area, rectangularity, aspect ratio,
                                 boundary annotation completeness, label
                                 match, road adjacency, nesting penalty)
    building_resolution.py      Building footprint filtering (excludes
                                 rooms, furniture, compound wall, dimension-
                                 line artifacts, nested internals) +
                                 multi-block support
    road_access.py              Road candidate selection + gate/entry/
                                 access/street/front text evidence
    front_side.py                Front/rear/left/right edge resolution,
                                 NEVER assumes page-top = front; priority:
                                 FRONT label > ROAD > STREET > ACCESS >
                                 MAIN ENTRY > GATE > deterministic LOW-
                                 confidence fallback
    setbacks.py                  Geometry-first setback derivation
                                 (building-to-plot-edge distance) +
                                 dimension-evidence reconciliation per side
    areas.py                     Plot/building area (width*depth for
                                 rectangular, shoelace polygon area for
                                 irregular), coverage, diagnostic gross
                                 FAR; keeps carpet/built-up/plinth/floor
                                 areas found in text separate from
                                 footprint_area (never substituted)
    evidence_reconciliation.py   Combines competing values for one field
                                 into AGREED / MINOR_VARIANCE /
                                 OUTLIER_DETECTED / CONFLICTING_EVIDENCE /
                                 MISSING — conflicting evidence is never
                                 silently resolved to one value
    consistency.py               Physical plausibility checks (building
                                 can't exceed plot, no negative setbacks,
                                 rectangular width/depth cross-check —
                                 skipped for irregular plots) -> Conflicts
    pipeline.py                   Orchestration: ExtractionResult ->
                                   NormalizedPlan
```

Tests: `tests/test_scale.py`, `test_dimension_classification.py`,
`test_plot_resolution.py`, `test_building_resolution.py`,
`test_road_and_front_side.py`, `test_setbacks.py`, `test_areas.py`,
`test_evidence_reconciliation.py`, `test_consistency.py`,
`test_pipeline_integration.py` (64 tests total), using both new synthetic
geometry fixtures (`tests/fixtures/geometry_builders.py`, precise
hand-built `ExtractionResult`/candidate objects for exact-value
assertions) and Phase 2's existing synthetic PDF fixtures
(`tests/fixtures/pdf_builders.py`) run end-to-end through
`PDFHybridExtractor -> build_normalized_plan` for every PDF variant
(vector, scanned/OCR, rotated, multi-page-size, text-only, missing-text)
to avoid tuning to one plan shape, per phase3.md's "do NOT tune
specifically to five plans."

## How resolution works, end to end

1. **Plot resolution** (`plot_resolution.py`): every `PlotCandidate` with
   polygon geometry is scored on a weighted combination of area rank,
   rectangularity, aspect ratio, boundary-dimension-annotation coverage,
   plot/site/property label proximity, road adjacency, and a nesting
   penalty. The highest scorer wins; confidence reflects the margin over
   the runner-up, not just "a candidate existed." **Largest-rectangle-
   wins is explicitly not the algorithm** — see
   `test_plot_resolution.py::test_largest_rectangle_is_not_automatically_the_winner`.
2. **Scale** (`scale.py`): every `Dimension` with both a resolvable
   length unit and an associated geometry line is a scale sample
   (`line_length_pts / magnitude_in_metres`). Samples are reduced with
   median + MAD-based robust outlier rejection; confidence reflects
   sample count and spread. Falls back to a configured default at LOW
   confidence — never silently trusted as HIGH — when no sample exists.
3. **Building resolution** (`building_resolution.py`): `BuildingCandidate`s
   are filtered by page match, aspect-ratio sanity, size-relative-to-plot
   bounds (too big -> compound wall/plot outline, too small -> furniture/
   annotation), adjacent room-label text, and bounding-box nesting inside
   an already-accepted block. Multiple surviving blocks are all kept
   (multi-block buildings supported); their footprint areas are summed.
4. **Road + front-side** (`road_access.py`, `front_side.py`): front is
   resolved from an explicit FRONT label first, then a ROAD candidate,
   then STREET/ACCESS/MAIN-ENTRY/GATE text, and only as an explicitly
   LOW-confidence, clearly-reasoned fallback if none of that evidence
   exists — the plot polygon's first ring edge, which is a deterministic
   fallback, not an assumption that page-top is front. Rear = farthest
   edge from front; left/right are assigned as if standing at the front
   edge facing into the plot (arriving from the road).
5. **Dimension classification** (`dimension_classification.py`): each raw
   `Dimension` is classified primarily from geometry — proximity/
   orientation relative to the plot boundary, building boundary(ies), the
   front/rear/left/right edge groups, and road candidates — with label
   keywords as a secondary, confidence-adjusting signal. A dimension with
   no geometry line falls back to keywords alone, at LOW confidence.
6. **Setbacks** (`setbacks.py`): per side, both a geometry-derived
   distance (building polygon to that side's plot-boundary edge group,
   via `geometry_utils.polygon_to_edges_distance` — real polygon/segment
   distance, not bounding-box subtraction) and any classified setback
   dimension are reconciled into one `ValueField`.
7. **Areas / coverage / FAR** (`areas.py`): plot/building area prefers
   width*depth for near-rectangular shapes (rectangularity >= 0.9),
   falling back to the shoelace polygon area for irregular shapes — both
   are cross-referenced in the confidence reason when both are available.
   `coverage = footprint_area / plot_area * 100` at full internal
   precision. `far` is always the diagnostic
   `gross_built_up_area / plot_area` (gross_built_up_area = footprint x
   floor_count when a floor count was found in text, else footprint area
   alone at LOW confidence) — **never** conflated with a
   regulatory/sanctioned FAR, which belongs to the future RuleEngine.
   Carpet/built-up/plinth/floor areas mentioned in plan text are detected
   separately (`find_labeled_areas`) and never merged into
   `footprint_area`.
8. **Evidence reconciliation** (`evidence_reconciliation.py`): every
   multi-source field (plot width/depth, building width/depth, road
   width, each setback) goes through `reconcile()`, which returns one of
   AGREED / MINOR_VARIANCE / OUTLIER_DETECTED / CONFLICTING_EVIDENCE /
   MISSING. **Disagreeing sources beyond tolerance are never silently
   collapsed to one value** — the resulting `ValueField` has `value=None`
   and a populated `conflict`, exactly matching phase3.md's example ("If
   text says 5m and geometry says 2.2m: DO NOT silently choose one.").
9. **Physical consistency** (`consistency.py`): flags impossible geometry
   (building wider/deeper than plot, negative setbacks) and — for
   rectangular plots only (`rectangularity >= 0.9`; skipped for irregular
   plots, which use the polygon-distance setback logic instead) — cross-
   checks `building_width + left + right ≈ plot_width` and
   `building_depth + front + rear ≈ plot_depth` within a configurable
   tolerance (`settings.geometry_tolerance_m`, or 5% of the plot
   dimension, whichever is larger).
10. **Orchestration** (`pipeline.py`) wires all of the above together,
    aggregates every raised `Conflict` into `NormalizedPlan.conflicts`,
    and writes a human-readable `overall_confidence_note` summarizing the
    plot/scale/front-side/building-filtering decisions made.

## Known limitations / what Phase 4 (RuleEngine) should expect

- `far` on `NormalizedPlan` is always the diagnostic gross FAR defined
  above — comparing it against a municipality's sanctioned/regulatory FAR
  threshold is the RuleEngine's job, not this layer's.
- `schemas.geometry.Dimension` has no `page` field (a Phase 1 contract
  constraint), so multi-sheet documents are reasoned about at the
  whole-document level rather than strictly per-page; this is noted in
  `overall_confidence_note` when a document has more than one page.
- Building footprint area for multi-block buildings is the **sum** of
  each surviving block's polygon area (an approximation of the true
  union), since Phase 2 does not currently emit overlapping candidates
  for the same physical block.
- `front_side.py`'s deterministic fallback (no FRONT/ROAD/STREET/ACCESS/
  ENTRY/GATE evidence at all) always resolves at LOW confidence with an
  explicit reasoning string — it is a documented fallback, never a
  silent "top of page = front" assumption.

## Running it

```
pip install -r requirements.txt
pytest tests/ -v
```

144 tests pass (80 from Phase 2, 64 new in Phase 3).
