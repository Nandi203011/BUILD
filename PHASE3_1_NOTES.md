# BUILDCheck India — Phase 3.1 Notes (hardening pass)

**Scope of this pass:** targeted fixes for the failure modes described in
`phase3_1.md`, prioritized by (a) how directly they were pinpointable in
the actual code, and (b) whether they could be regression-tested without
real plan PDFs (see "Known gap" below).

## What was fixed, and where

| # | Spec item | Root cause found | Fix location |
|---|---|---|---|
| 1 | Page-frame / sheet-border picked as plot | `candidate_geometry.py` took `sized[0]` (largest closed polygon) as *the* plot, full stop | `candidate_geometry.py` rewritten: validate → dedupe → reject page-frame-like polygons → emit **every** surviving polygon as both a plot and building candidate |
| 2 | No multi-candidate plot ranking | Generation only ever emitted one plot candidate, so `plot_resolution.py`'s existing scoring (rectangularity, label proximity, road adjacency, nesting penalty) never had more than one option | Same fix as #1 — `plot_resolution.py` itself needed no changes, its scoring was already sound once it receives >1 candidate |
| 4 | Dimension bound to wrong geometry line | `pdf_extractor.py` used `nearby_geometry_ids[0]` where `[0]` meant "first found in list order", not "best match" | `dimension_candidates.py`: added `_line_association_score` (distance + orientation alignment + line length) and made `_nearby_geometry_ids` sort best-first; `pdf_extractor._best_dimension_geometry` additionally gates on a minimum confidence derived from distance, leaving the association `None` rather than guessing when even the best candidate is weak |
| 5, 6 | Scale mixes detail-view and site-scale dimensions | `estimate_scale` had no way to scope samples to a region (`Dimension` has no `page` field by Phase 1 contract) | `scale.py`: added `region_bbox`/`region_padding_factor` to `collect_scale_samples`/`estimate_scale`, scoping samples geometrically to (a padded) plot bounding box; falls back to whole-document samples (with an explicit note) if no local samples exist. Wired into `pipeline.py` using the resolved plot's bbox |
| 7 | No polygon validity check | Only rectangularity was checked, which says nothing about degenerate/self-intersecting geometry | `geometry_utils.is_valid_polygon` — rejects too-few-vertices, near-zero area, extreme perimeter²/area ratio, and self-intersecting rings |
| 9 | Impossible coverage (e.g. 47070%) | `areas.coverage_field`/`far_field` had no upper bound on footprint/plot ratio | Added `_MAX_PHYSICALLY_PLAUSIBLE_COVERAGE_RATIO` (1.02, small tolerance for rounding). Both functions now return `ValueField.conflicting(...)` instead of a nonsense percentage. `pipeline.py` also marks `building.footprint_area` itself CONFLICTING at the source (not just downstream), and threads `footprint_area`/`coverage`/`far` conflicts into the plan's conflict list (they weren't collected there before) |
| 10 | No timeout / unbounded processing | No wall-clock budget anywhere in `extract()`; no cap on OpenCV contour/line counts or dimension-association search space | New `backend/tools/bounded_execution.py` (`run_with_timeout`), wrapping `PDFHybridExtractor.extract()` with `settings.extraction_timeout_seconds` — returns an explicit `ExtractionResult` with a `TIMEOUT:` warning instead of hanging or silently succeeding. Added `settings.max_raster_dpi` (clamped in `__init__`), `max_opencv_contours`/`max_opencv_lines` (longest/largest-first truncation after OpenCV evidence extraction), and `max_dimension_association_lines` (bounds the dimension-to-line search) |

All of the above are configurable via `backend/config.py` (`Settings`) —
no thresholds are hardcoded inline.

## Regression tests

`tests/test_phase3_1_fixes.py` — 16 new tests, synthetic geometry only
(see "Known gap"). Covers: page-frame rejection, multi-candidate
emission, degenerate-polygon rejection, bbox dedup, dimension
association (nearest-not-first, low-confidence-unresolved),
impossible-coverage/FAR conflict, near-100%-coverage-not-flagged, local
vs. global scale scoping + fallback, polygon validity (self-intersecting
bowtie), page-frame heuristic (touch AND area both required), and the
timeout wrapper (explicit failure vs. within-budget success).

Full suite: **160 passed** (144 pre-existing + 16 new), no existing
tests modified or weakened.

## Known gap — please read before treating this as "done"

**The real `PLAN1`/`PLAN2`/`PLAN4`/`PLAN5`/`PLAN6` acceptance PDFs
referenced throughout `phase3_1.md` (sections 17, 22, 23) were not in
the uploaded zip** — `data/test_plans/` contained only a `.gitkeep`.
Everything above was validated against synthetic fixtures that
reproduce the *described* failure patterns (a full-bleed border
rectangle, a detail-view dimension cluster at a different implied
scale, an impossible footprint/plot ratio, etc.), not against the real
drawings. If you have those PDFs, drop them into `data/test_plans/` and
re-run — I'd expect some of the thresholds above (particularly
`page_frame_area_fraction_threshold` and
`dimension_association_min_confidence`) to need tuning against real
data, since they were chosen to be safe against the existing synthetic
fixtures rather than calibrated against real scans/OCR noise.

## Update — validated against a real PDF (`ARJUN_22_4_25-Model-1.pdf` / their PLAN1.pdf)

You ran the phase3.1 zip against a real plan and got garbage output
(plot area 0.0315 m², coverage 48116%). That real data surfaced two
bugs my synthetic fixtures never exercised, and confirmed one gap I'd
already flagged as open. Investigated directly against your uploaded
PDF:

### New bug #1 (the dominant one): rotated-page coordinate mismatch

The page is a 270°-rotated, huge (3024×2160pt) composite architectural
sheet. `page.get_text("dict")` (used for all text) returns coordinates
in the page's **raw, unrotated mediabox** space (2160×3024), while
`page.get_drawings()` (used for all vector geometry — lines, rects,
polygons) already returns coordinates in the **rotated display** space
matching `page.rect` (3024×2160) / `PageMetadata.width_pts/height_pts`.
For an unrotated page these are identical, so nothing caught this in
testing. On a rotated page, text and geometry were silently living in
two different coordinate frames — every text-to-geometry distance
computation (dimension-line association, plot/site label-proximity
scoring, room-label rejection) was comparing effectively unrelated
positions. This is very likely the single largest contributor to the
garbage output you saw.

**Fix:** `pdf_native.py::extract_text_items` now transforms every text
bounding box (and orientation) through `page.rotation_matrix` before
storing it — identity transform for rotation 0 (no behavior change for
the common case), correct alignment for rotated pages. New test:
`test_extract_text_items_applies_rotation_matrix` (builds a real
rotated PDF via fitz and checks the resulting bbox lands in the
rotated-page bounds).

### New bug #2: page-level default-unit convention not handled

221 of 225 dimension candidates on your real plan had `unit_hint=None`
— because the sheet states "ALL DIMENSIONS ARE IN METRE" once, then
gives bare numbers everywhere ("3.35", "0.91", ...) rather than
labelling every single one "3.35 m". Phase 2's per-candidate unit regex
had no way to see that page-level note, so scale estimation had almost
nothing to work with (1-2 usable samples out of 225).

**Fix:** `pdf_extractor.py` now scans page text once for that
convention (`_detect_default_dimension_unit`) and applies it as a
**fallback only** — never overriding an explicit per-label unit, and
only to candidates whose raw text plausibly IS a single length reading
(contains a decimal point, magnitude in a plausible 0.05–60m range) —
this deliberately excludes area figures (96.34 sq.m, which share the
same text blocks as real dimensions on these drawings) and stray digits
picked out of door/window codes ("D2" → 2.0). New tests:
`test_default_unit_note_detected_and_applied_to_plausible_bare_numbers`,
`test_default_unit_not_applied_to_area_figures_or_bare_integer_codes`,
`test_default_unit_never_overrides_an_explicit_per_label_unit`.

### Measured effect of both fixes together, on your real PDF

| | Before | After |
|---|---|---|
| Usable scale samples | 1 | 97 (17 rejected as outliers) |
| Plot width | 0.217 m | **11.83 m** (drawing states 11.72 m) |
| Plot area | 0.0315 m² | 340.7 m² |
| Coverage | 48116.5% (impossible) | 46.4% |
| FAR | 481.2 | 0.46 |

Scale confidence is still LOW (163.9% spread even after outlier
rejection) and 2 physical-consistency conflicts remain
(building+setbacks don't fully reconcile against plot width/depth) —
**this is the system correctly surfacing genuine remaining ambiguity,
not a new bug.** It's the expected symptom of the still-open item
below.

### Confirms the open gap already flagged: FIX #3 (drawing-region reasoning)

Your real plan is a single 3024×2160pt page containing SITE PLAN +
4 FLOOR PLANS + a SECTION + a COLUMN DETAIL + a septic-tank detail, all
at once (400 closed polygons on one page). Plot resolution reported
"Winner only narrowly (0.00) beats the runner-up (0.89); ambiguous" —
correct, honest uncertainty, because nothing in the pipeline knows
which of those sub-drawings is the actual site plan. The
`page_frame_touch_tolerance_pts` fix I made earlier (relative-to-page
margin) helped rule out the sheet's own border, but the remaining
candidate pool (floor-plan panel borders, detail-view rectangles) is
still one flat, undifferentiated pool. This is exactly FIX #3, still
not implemented (see below) — it's the highest-leverage remaining piece
of work if you want confidence to move past LOW/MEDIUM on multi-view
sheets like this one.

Full suite after these two fixes: **164 passed** (144 original + 20
phase3.1 regression tests), still nothing weakened.


- **FIX #3 — drawing-region reasoning.** The spec's fuller vision
  (classifying regions of a sheet as SITE PLAN / FLOOR PLAN / ELEVATION
  / SCHEDULE / TITLE BLOCK via keyword + spatial clustering) is a
  genuinely new subsystem, not a fix to an existing one. FIX #5/#6's
  local-scale-region logic is a narrower, geometry-only substitute
  (padded plot bbox) that helps the specific scale-mixing symptom but
  does not give plot/building resolution or dimension classification
  the same region awareness.
- **Title-block-specific rejection** (as distinct from the page-frame
  check) — title blocks that don't touch the full page border (e.g. a
  compact block in one corner) won't be caught by
  `is_page_frame_like`. `plot_resolution.py`'s existing scoring
  (rectangularity/label-proximity) provides some indirect protection
  but there's no explicit heuristic for this shape class.
- Sections 11–19 of `phase3_1.md` beyond what's covered above (I read
  through section 15 in detail; later sections weren't fully reviewed
  against code this pass).

## Ground truth for the ARJUN plan (read directly off the rendered PDF)

Derived by rendering `ARJUN_22_4_25-Model-1.pdf` at 400 DPI and reading
the site-plan region directly (not OCR'd — visually confirmed), since
that's more reliable than trusting the pipeline's own jumbled raw-text
order for validation purposes. Not yet wired into a checked-in
ground-truth fixture/harness (that's the next piece of work — see
"Ground-truth evaluation harness" below) but recorded here so it doesn't
have to be re-derived:

| Field | Value | Confirmed via |
|---|---|---|
| Plot frontage (facing road) | 9.14 m | site plan dimension |
| Plot depth | 12.19 m | site plan dimension |
| Plot area | 111.42 m² (9.14 × 12.19) | computed |
| Building frontage | 8.22 m | site plan dimension, inner boundary |
| Building depth | 11.72 m | site plan dimension, inner boundary |
| Building footprint area (per floor) | 96.34 m² (8.22 × 11.72) | matches the drawing's own stated "PROP. PLINTH AREA IN G.F 96.34" exactly |
| Floors | G+3 (Ground, First, Second, Third, all RCC roof) | title block + repeated floor plans |
| Total plinth area | 385.36 m² (96.34 × 4) | matches the drawing's own stated "TOTAL PLINTH AREA 385.36" exactly |
| Front setback (road-facing side) | ~0.47 m | small setback callout at the road-side corners |
| Rear setback (side opposite the road) | ~0 m (building flush with plot boundary) | visually flush in the drawing — no gap, no callout |
| Side setbacks (both remaining sides) | ~0.46 m each | small setback callouts at the corresponding corners |
| Coverage | ~86.5% (96.34 / 111.42) | computed |
| FAR | ~3.46 (385.36 / 111.42) | computed |

Two of these are worth calling out for an eval harness specifically
because they're easy for an extractor (deterministic or VLM) to get
wrong by construction: the rear setback is genuinely ~0 (not a missing
value — the building really is drawn flush with the boundary on that
side), and the plot's "width"/"depth" labels don't align with a
generic width=horizontal/depth=vertical assumption — frontage is the
side facing the road, which here happens to be the *shorter* dimension
(9.14 m) drawn vertically in the site plan, not the longer horizontal
one.

### What the current (post rotation+unit-fix) deterministic pipeline gets on this plan

From the last real run (see the "Update — validated against a real
PDF" section above): plot width 11.83 m (true 9.14 or 12.19 depending
on which side — ambiguous which the pipeline picked), building
footprint 158.17 m² (true 96.34 m², off by ~64%), coverage 46.4% (true
~86.5%). Confirms the FIX #3 gap is real and is the dominant remaining
error source, not a rounding/calibration issue.

## Still to do: ground-truth evaluation harness

Not yet built. The recommended next step (chosen over building the VLM
layer further, or continuing region-reasoning work blind) is:

1. A checked-in `evaluation/ground_truth/plan1.yaml` (or similar) with
   the table above in a machine-comparable format.
2. A small `evaluate_extraction.py` that runs the deterministic
   pipeline (and, once ready, the VLM-augmented pipeline) against it
   and reports per-field absolute/relative error — not just pass/fail,
   since "close" (this session's 3% error on plot width) and
   "wildly wrong" (64% error on footprint) are very different signals.
3. Repeat for a handful more real plans before drawing conclusions from
   any single-plan result — one plan's ground truth (even accurate)
   isn't enough to validate a change with any statistical confidence.

## Vision backend swap (SmolVLM2-2.2B-Instruct instead of Qwen2.5-VL-7B-Instruct)

See `VISION_IMPLEMENTATION.md` for the full writeup. Summary: the vision
extraction layer (built separately, on top of this work) originally
defaulted to Qwen2.5-VL-7B-Instruct (~16GB download), which failed to
download reliably. Refactored `vision_extraction/` onto a shared
`BaseArchitecturalPlanExtractor` (page-loop, prompt formatting, and
JSON-parsing logic in one place) with two concrete backends —
`SmolVLMArchitecturalPlanExtractor` (SmolVLM2-2.2B-Instruct, ~4.4GB, now
the default) and `QwenArchitecturalPlanExtractor` (unchanged behavior,
still available via `VISION_BACKEND=qwen`) — selected through a new
`get_vision_extractor()` factory rather than hardcoded imports. 9 new
tests in `tests/test_vision_backends.py` cover backend selection and
model-name resolution precedence without requiring real model weights.
Full suite: 175 passed.

Not yet done: actually running either vision backend against the ARJUN
plan (or any real plan) to see how its region/dimension semantic
classification compares to the ground truth table above — that's the
natural next step once the SmolVLM2 download completes successfully.
