# Phase 3.2 — Vision + CV integration fix

**Scope of this pass:** the vision model was producing plausible-looking
output (see `vision_result.json`) but that output was barely reaching the
final `NormalizedPlan`, and had no way to fill a gap the deterministic
CV/OCR pipeline missed outright. Two real bugs and one missing fusion
path, found by tracing every consumer of `vision_pages` end to end.

## Bug #1 (root cause of "vision isn't extracting dimensions correctly"):
coordinate-space mismatch between two different bbox conversions

`vision_extraction/base.py`'s hallucination-grounding check already knew
(and documented in a code comment) that the Qwen-family models used by
every backend here — SmolVLM2, local Qwen2.5-VL, and the hosted `api`
backend's default Groq model — return bounding boxes on an internal
**normalized 0–1000 grid**, not raw image pixels, no matter what the
prompt asks for. That file had its own private, already-fixed conversion
function for this.

`vision_extraction/spatial.py`'s `vision_bbox_to_page_points()` — the
function actually used by `plot_resolution.py` (plot scoring) and
`dimension_classification.py` (dimension semantic labeling) — never got
the same fix. It kept assuming raw pixels at `vision_render_dpi` and
dividing by a DPI-derived scale. On a large-format architectural sheet
(e.g. the real 3024×2160pt ARJUN plan), that shrank every vision
region/dimension bbox down to a few page-points near the origin, so it
essentially never overlapped real CV geometry. `_vision_plot_score`,
`_vision_semantic_hint`, `region_score`, and `semantic_dimension_score`
all consume this function, so this one bug quietly zeroed out the vision
contribution across the entire pipeline — plot resolution, dimension
classification, everything.

**Fix:** one shared, auto-detecting `vision_bbox_to_page_points()` now
lives in `spatial.py` (`base.py` calls into it instead of keeping a
duplicate). It uses the *actual* rendered-image pixel size (now recorded
on `VisionPageResult.page_width_pts`/`page_height_pts`, sourced from
`page_renderer.render_pdf_pages()`'s own `width_px`/`height_px` — reliable
even for scanned pages with no native text layer) to decide, per bbox,
whether the model returned normalized 0–1000 coordinates or real pixels,
rather than hard-coding one assumption. See
`vision_extraction/spatial.py::vision_bbox_to_page_points` docstring.

New tests: `tests/test_vision_cv_fusion.py::test_normalized_grid_bbox_is_detected_and_scaled_to_real_page_size`,
`test_raw_pixel_bbox_still_handled_when_values_exceed_normalized_range`,
`test_unknown_page_size_falls_back_without_crashing`,
`test_region_score_matches_after_coordinate_fix`.

## Bug #2: silent truncation from too small a token budget

`vision_max_new_tokens` defaulted to 2000. A realistic multi-view sheet's
structured JSON output (several regions + dimensions + areas) measured at
~2,500 tokens for a modest 8-region/14-dimension page — already over
budget. A response cut off mid-JSON has no closing brace, so
`BaseArchitecturalPlanExtractor._extract_json` fails to parse it
**entirely**, and that page's vision evidence is silently dropped (logged
only as a warning), not degraded.

**Fix:** raised the default to 3000 (ceiling 6000, up from 4000), updated
`.env.example`, and fixed `test_vision_max_new_tokens_setting_...` (which
had encoded the old, too-low assumption as a passing test).

## Missing piece: vision as an independent evidence source, not just a relabel

Before this pass, vision output could only ever *relabel* a `Dimension`
the native-text/OCR extractor had already produced
(`_vision_semantic_hint`, still in place, unchanged). If OCR/native text
missed a printed number outright — a page-level unit convention the regex
couldn't see, a scanned page with garbled OCR, an unusual label format —
a vision reading of that same number had **no path into the final plan
at all**, even if it was correctly grounded (present in the native text
layer, or on a page with no text layer to ground against).

**Fix:** `dimension_classification.vision_only_dimensions()` — scans
grounded vision dimensions for values with no matching native/OCR
`Dimension` on the same page (deduped by rounded metric value so it never
double-counts a value already fused via `_vision_semantic_hint`), and
emits them as `ClassifiedDimension`s backed by a synthetic `Dimension`
with `geometry=None`. Always capped at `ConfidenceLevel.LOW` and gated by
a separate, higher confidence threshold
(`settings.vision_only_dimension_min_confidence`, default 0.6 — higher
than `dimension_association_min_confidence` because this reading has *no*
corroborating geometry at all). Wired into `pipeline.py` right after the
existing `classify_dimensions()` call, so it flows through the exact same
`plot_width`/`plot_depth`/`building_width`/`building_depth`/`road_width`/
setback candidate lists and the exact same evidence-reconciliation
conflict-detection as every other source — a vision-only reading that
contradicts strong CV geometry still produces a genuine
`CONFLICTING_EVIDENCE`, never a silent override.

New tests:
`test_vision_only_dimension_admitted_when_no_native_candidate_exists`,
`test_vision_only_dimension_skipped_when_native_candidate_already_has_the_value`,
`test_vision_only_dimension_rejected_below_confidence_threshold`,
`test_vision_only_dimension_ignores_wrong_page`.

## What this does and does not fix

**Fixes:** the vision pipeline's output can now actually reach
`NormalizedPlan` — both as a scoring signal (plot resolution, dimension
relabeling) and as its own evidence source when CV/OCR found nothing.
Fixes the specific silent-truncation failure mode. All 201 tests pass
(193 pre-existing + 8 new), nothing weakened.

**Does not fix:** `PHASE3_1_NOTES.md`'s already-flagged, still-open FIX #3
(drawing-region reasoning — classifying which sub-drawing on a
multi-view sheet is the actual site plan vs. a floor plan/detail
panel). That's a genuinely separate subsystem, not a bug in the vision
integration itself, and vision's `SITE_PLAN`/`GROUND_FLOOR_PLAN`/etc.
region classification (now that its bboxes are correctly positioned) is
the natural building block for it, but wiring that up is real, new work,
not a fix.

Also not addressed here: no real plan PDFs beyond `ARJUN.22.4.25-Model-1`
were available in the uploaded project to validate against (per
`PHASE3_1_NOTES.md`'s "Known gap"). The ground-truth evaluation harness
that document recommends building is still the right next step before
trusting these fixes' effect size on a broader set of real plans, rather
than synthetic fixtures alone.
