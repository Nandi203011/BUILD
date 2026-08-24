# Phase 3.3 — Independent CV + Vision Validation

## Purpose

Phase 3.3 is a validation phase, not the final fusion phase. CV and Vision are deliberately treated as two independent witnesses.

```text
PDF
 ├── CV/native/OCR + geometry  ──> Independent CV result
 └── Rendered image + Vision   ──> Independent Vision result
                                      │
                         deterministic agreement engine
                                      │
                  AGREED / CV_ONLY / VISION_ONLY / CONFLICT / MISSING
```

No conflicting values are averaged. The agreement engine never asks the LLM which source is correct.

## Commands

### 1. CV only — Vision is not imported or called

```powershell
python -m backend.tools.run_cv data\test_plans\PLAN2.pdf --output cv_res2.json
```

The output is the independent CV/native-text evidence set. For the packaged PLAN2 acceptance fixture it should resolve approximately:

- plot.width = 12.19 m
- plot.depth = 18.28 m
- building.width = 10.59 m
- building.depth = 16.48 m
- road.width = 9.20 m
- front setback = 1.00 m
- rear setback = 0.80 m
- left setback = 0.80 m
- right setback = 0.80 m

The CV site-plan resolver uses the local SITE PLAN geometry, explicit dimension text, nested plot/building rectangles, and deterministic spatial assignment. It does not use Vision.

### 2. Vision only

```powershell
python -m backend.tools.run_vision data\test_plans\PLAN2.pdf --backend api --no-grounding
```

`--no-grounding` is mandatory for the independent experiment: native PDF text is not allowed to delete a Vision result.

When the first Vision pass identifies a SITE_PLAN region, independent validation automatically performs a second focused Vision pass on that model-identified site-plan crop. The focus pass explicitly asks Vision to preserve repeated setback labels and assign their side from spatial position.

### 3. CV + Vision agreement

```powershell
python -m backend.tools.run_validation data\test_plans\PLAN2.pdf --vision-backend api --output validation.json
```

This command runs the independent CV resolver and independent Vision extractor, then compares the canonical fields deterministically.

## Status semantics

- `AGREED`: both sources produced a value within the documented tolerance.
- `CV_ONLY`: only CV produced a value.
- `VISION_ONLY`: only Vision produced a value.
- `CONFLICT`: both produced values but they differ beyond tolerance.
- `MISSING`: neither source produced a value.

A conflict is preserved exactly; it is never averaged and never silently assigned to one source.

## PLAN2 acceptance fixture

`data/test_plans/PLAN2.pdf` is packaged with the repository so the validation phase is reproducible. `data/test_plans/PLAN2.expected.json` contains the expected canonical measurements used by the real-PDF CV regression test.

## Important distinction from `run_pipeline`

`run_pipeline` is the legacy/full spatial-resolution pipeline and remains useful for regression testing. It is **not** the Phase 3.3 source-of-truth validation command because it intentionally performs downstream spatial reconciliation.

Use `run_cv`, `run_vision --no-grounding`, and `run_validation` to answer the independent-extraction question.

## Downstream policy for the next phase

The next phase may consume an `AGREED` value as a high-confidence canonical value with provenance `CV + Vision`.

Single-source values may be retained only if the later policy explicitly allows them, with lower confidence/provenance.

`CONFLICT` must remain unresolved for any compliance rule that depends on that field.


### Area fields are independently validated too

The validation contract now includes:
- `plot.area` (m²)
- `building.footprint_area` / proposed coverage area (m²)
- `building.gross_built_up_area` (m²)
- `coverage` (%)
- `far.area` (m²)
- `far` (ratio)

The CV path reads explicit native PDF area statements without Vision. The Vision path reads semantic `areas[]` values. Agreement remains deterministic and never averages conflicting values.
