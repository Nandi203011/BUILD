# Phase 3.3 — Independent CV + Vision Validation Contract

## Goal

Phase 3.3 is an intermediate validation phase. CV and Vision are **independent evidence sources**. Neither source is allowed to overwrite, average, or select the other source's value.

## Independent commands

### CV only

```powershell
python -m backend.tools.run_cv data\test_plans\PLAN2.pdf --output cv_res2.json
```

This path uses the deterministic site-plan resolver and native PDF/OCR evidence. It does **not** instantiate Vision.

### Vision only

```powershell
python -m backend.tools.run_vision data\test_plans\PLAN2.pdf --backend api --no-grounding
```

`--no-grounding` is intentional: the Vision result must remain independent of native PDF/CV evidence. The API backend performs a full-page pass and, in independent mode, a Vision-derived site-plan focus pass. The focus pass uses a smaller token budget (`VISION_FOCUS_MAX_NEW_TOKENS`, default 1200) to reduce provider rate-limit pressure.

### Deterministic agreement

```powershell
python -m backend.tools.run_validation data\test_plans\PLAN2.pdf --vision-backend api --output validation.json
```

The agreement engine compares independent CV and independent Vision fields. It never fuses values.

## Fields under validation

- Plot width/depth
- Building width/depth
- Road width
- Front/rear/left/right setbacks
- Plot area
- Building footprint / coverage area
- Coverage percentage
- FAR area
- FAR ratio
- Total built-up area

## PLAN2 acceptance values

The real fixture is `data/test_plans/PLAN2.pdf` and its contract is `data/test_plans/PLAN2.expected.json`.

The expected values are approximately:

- plot: 12.19 m × 18.28 m
- building: 10.59 m × 16.48 m
- road: 9.20 m
- setbacks: front 1.00 m, rear 0.80 m, left 0.80 m, right 0.80 m
- plot area: 222.83 m²
- coverage area: 174.52 m²
- coverage: 78.32%
- FAR area: 386.55 m²
- FAR: 1.73
- total built-up area: 579.90 m²

## Important architectural separation

The old `PDFHybridExtractor` / `legacy_cv_plan` is **not part of Phase 3.3 agreement**. It is intentionally excluded so old full-page/global geometry cannot contaminate the independent CV result.

Area fields are also separated by meaning:

1. geometric site-plan dimensions -> geometric area where appropriate;
2. explicitly printed regulatory/area-statement values -> native-text evidence;
3. Vision area values -> independent VLM evidence;
4. agreement -> field-by-field comparison only.

A later phase may add a fusion layer, but Phase 3.3 must first prove that each evidence source works independently.
