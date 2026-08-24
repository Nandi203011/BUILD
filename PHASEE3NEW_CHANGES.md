# PHASEE3NEW.md implementation notes

This phase adds the **Validation / Fusion Engine** ("Document Evidence
Layer") that PHASEE3NEW.md specifies on top of the already-existing
independent CV pipeline (`backend/cv_extraction/`, `run_cv.py`) and
independent Vision pipeline (`backend/vision_extraction/`, `run_vision.py`)
from Phase 3.1–3.3. It does **not** change how CV or Vision extract —
they remain fully independent observers, per section 32 — it only adds
the deterministic third layer that decides what the *document* actually
supports.

## What's new

### `backend/spatial_reasoning/document_evidence.py`

A new, generic module (no PLAN-specific logic anywhere):

- **Status vocabulary** matching PHASEE3NEW.md section 12 exactly:
  `FOUND`, `NOT_FOUND_BY_CV`, `NOT_FOUND_BY_VISION`, `NOT_FOUND_BY_EITHER`,
  `AGREED_DOCUMENT_VERIFIED`, `AGREED_UNVERIFIED`,
  `CV_ONLY_DOCUMENT_VERIFIED`, `VISION_ONLY_DOCUMENT_VERIFIED`,
  `CONFLICT_RESOLVED_BY_DOCUMENT_EVIDENCE`, `UNRESOLVED_CONFLICT`.
- **`DocumentTextIndex`** — a full-document native-text index (via
  PyMuPDF), used only for targeted re-checks/occurrence counting. Missing
  or unavailable is explicitly modeled (`available=False`) and never
  conflated with "value absent from PDF" (section 13).
- **`evaluate_candidate`** — implements the evidence-scoring hierarchy in
  section 18 (exact printed value +5, dimension-line association +4,
  semantic association +3, OCR confirmation +3, geometry confirmation +3,
  repeated occurrence +2, CV extraction +1, Vision extraction +1). Weights
  and the acceptance threshold (`DEFAULT_SCORE_THRESHOLD = 6`) are named
  constants, not silently hardcoded magic numbers.
- **`resolve_field`** — the conflict-resolution decision tree from
  sections 14–17: agreement -> document-verify; one-sided -> document
  verify or leave unverified; conflict -> pick the side with a score
  above threshold **and** direct document evidence; if both sides clear
  the bar, pick the higher score; if genuinely tied/insufficient,
  **`UNRESOLVED_CONFLICT`** with `final_value = None` rather than
  guessing (section 32: "NEVER fabricate a value merely to avoid null").
- **`reported_vs_calculated`** — section 19: a `NATIVE_TEXT` measurement
  (an explicitly printed value) is never silently overwritten by a
  `DERIVED` (geometry-calculated) one for the same field. Both are kept;
  `status` is `REPORTED_VALUE_WITH_GEOMETRIC_CHECK` when they agree, or
  `REPORTED_CALCULATION_DIFFER` when they don't.

### `backend/spatial_reasoning/final_fusion.py`

Added `build_document_verified_fusion(cv, vision, pdf_path=None)`, which
produces the full section-21/section-30 output schema:

```json
{
  "document_id": "...",
  "cv_values": {...},
  "vision_values": {...},
  "document_evidence": {...},
  "conflicts": {...},
  "validation": {...},
  "final_agreed_values": {...},
  "reported_vs_calculated": {...},
  "provenance": {...},
  "summary": {"CV_ONLY_DOCUMENT_VERIFIED": 15, "NOT_FOUND_BY_EITHER": 6, ...},
  "has_unresolved_conflicts": false
}
```

This is purely additive: the existing `build_final_agreement` /
`apply_final_agreement_to_plan` (Phase 3.3's simpler CV+Vision agreement
layer) are untouched, so every existing caller and all 218 pre-existing
tests keep working unchanged.

### Wiring

- `backend/schemas/validation.py`: `ExtractionValidationReport` gained an
  optional `document_verified_fusion` field.
- `backend/validation.py`: `validate_extractions()` now also computes
  `document_verified_fusion` and attaches it to the report.
- `backend/tools/run_validation.py`: prints the document-verified summary
  and final values alongside the existing Phase 3.3 output.
- `backend/tools/run_pipeline.py`: new opt-in `--document-verified-fusion`
  flag. When passed, it runs the independent CV path
  (`extract_independent_cv`) and, if `VISION_ENABLED`, the independent
  Vision path, then the fusion engine — entirely separately from the
  legacy full-page `PDFHybridExtractor` path that flag-less
  `run_pipeline.py` already ran. Output is written to
  `<output>.fusion.json` (or printed if no `--output`).
- `run_cv.py` / `run_vision.py` were already independent commands from
  Phase 3.3 (section 29) and are unchanged.

## Verified on PLAN2 (the only plan bundled in this project's `data/test_plans/`)

```
python -m backend.tools.run_cv data/test_plans/PLAN2.pdf --output cv_res.json
python -m backend.tools.run_pipeline data/test_plans/PLAN2.pdf --output plan2.json --document-verified-fusion
```

`plan2.json.fusion.json` → `final_agreed_values`, all `CV_ONLY_DOCUMENT_VERIFIED`
(Vision was not run — no `VISION_ENABLED`/API key configured in this
environment):

| field | final_value | status |
|---|---|---|
| plot.width | 12.192 | CV_ONLY_DOCUMENT_VERIFIED |
| plot.depth | 18.28 | CV_ONLY_DOCUMENT_VERIFIED |
| building.width | 10.5952 | CV_ONLY_DOCUMENT_VERIFIED |
| building.depth | 16.4822 | CV_ONLY_DOCUMENT_VERIFIED |
| road.width | 9.2 | CV_ONLY_DOCUMENT_VERIFIED |
| setbacks.front | 1.0 | CV_ONLY_DOCUMENT_VERIFIED |
| setbacks.rear | 0.8 | CV_ONLY_DOCUMENT_VERIFIED |
| setbacks.left | 0.8 | CV_ONLY_DOCUMENT_VERIFIED |
| setbacks.right | 0.8 | CV_ONLY_DOCUMENT_VERIFIED |
| plot.area | 222.83 | CV_ONLY_DOCUMENT_VERIFIED |
| building.footprint_area | 174.52 | CV_ONLY_DOCUMENT_VERIFIED |
| coverage | 78.32 | CV_ONLY_DOCUMENT_VERIFIED |
| far.area | 386.55 | CV_ONLY_DOCUMENT_VERIFIED |
| far | 1.73 | CV_ONLY_DOCUMENT_VERIFIED |
| building.gross_built_up_area | 579.90 | CV_ONLY_DOCUMENT_VERIFIED |
| building/floor/plinth/parapet height, floor_count, plinth_area | — | NOT_FOUND_BY_EITHER |

Matches the PLAN2 acceptance values already documented in
`PHASE3_3_FINAL_VALIDATION_CONTRACT.md`.

The AGREED / CONFLICT_RESOLVED_BY_DOCUMENT_EVIDENCE / UNRESOLVED_CONFLICT
/ VISION_ONLY paths (which need a real Vision candidate to exercise) are
covered with synthetic Vision fixtures in
`tests/test_document_evidence_fusion.py`, matching the worked examples in
PHASEE3NEW.md sections 14–17 exactly (e.g. CV=17.59 vs Vision=17.95 with
only CV backed by native text/geometry → `final_value = 17.59`,
`CONFLICT_RESOLVED_BY_DOCUMENT_EVIDENCE`).

## ⚠️ Important scope note: PLAN1 / PLAN4 / PLAN5 / PLAN6

PHASEE3NEW.md's testing sections (25–27) require running CV, Vision, and
fusion independently against **PLAN1, PLAN2, PLAN4, PLAN5, PLAN6** and
generating `cv_result.json` / `vision_result.json` / `fusion_result.json`
for each.

**Only `data/test_plans/PLAN2.pdf` was present in the uploaded ZIP.**
PLAN1, PLAN4, PLAN5, and PLAN6 were not included, so they could not be
run or verified in this pass — every routine above is written generically
(no PLAN-specific branches, crop regions, or fallback values of any kind,
per section 23), but the multi-plan generalization claim in section 31
("do not declare completion merely because PLAN2 works") cannot be
independently confirmed here without those files.

To finish section 25–27's test matrix, add the missing PDFs to
`data/test_plans/` and run:

```
python -m backend.tools.run_cv       data/test_plans/PLAN<N>.pdf --output cv_result_PLAN<N>.json
python -m backend.tools.run_vision   data/test_plans/PLAN<N>.pdf --backend api --no-grounding --output vision_result_PLAN<N>.json
python -m backend.tools.run_pipeline data/test_plans/PLAN<N>.pdf --output plan_PLAN<N>.json --document-verified-fusion
```

## Test results

```
233 passed  (218 pre-existing + 15 new, in tests/test_document_evidence_fusion.py)
```

No existing tests were modified or deleted.
