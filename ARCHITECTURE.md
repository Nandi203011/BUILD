# BUILDCheck India — Architecture

## Pipeline

```
PLAN PDF
   -> PDF / VECTOR / TEXT / OCR extraction   (backend/cv_extraction, Phase 2+)
   -> RAW EXTRACTION                          (schemas/extraction.py: ExtractionResult)
   -> NORMALIZATION
   -> GEOMETRIC MODEL                         (schemas/geometry.py)
   -> SPATIAL REASONING                       (Phase 2/3, produces candidates)
   -> NORMALIZED PLAN                         (schemas/normalized_plan.py: NormalizedPlan)
   -> RAG                                     (backend/rag, Phase 3+)
   -> RASE                                    (Phase 3+, compiles regulations -> RuntimeRuleDefinition)
   -> VALIDATED RULES                         (runtime_rules/contracts.py)
   -> DETERMINISTIC RULE ENGINE               (backend/compliance, Phase 4+)
   -> COMPLIANCE RESULT                       (schemas/compliance.py: ComplianceResult)
   -> REPORT
   -> FRONTEND
```

**The LLM is never the compliance authority.** Groq (or any LLM) may assist
extraction, retrieval, or rule *drafting* from regulation text, but only the
deterministic `RuleEngine` (interface in `backend/runtime_rules/contracts.py`)
is permitted to produce a `ComplianceStatus`.

## Why the boundaries are where they are

- **Extraction never leaks into anything downstream.** Every extractor
  (PDF, image/OCR, and — in the future — DXF/BIM) implements
  `GeometryExtractor` (`backend/cv_extraction/interfaces.py`) and returns the
  same `ExtractionResult`. Spatial reasoning is the only stage that reads
  `ExtractionResult`; everything after it reads `NormalizedPlan` only.
- **Municipality-specific thresholds are never Python code.** They are
  loaded at runtime as `RuntimeRuleDefinition` records from
  `data/runtime_rules/`. Swapping BBMP for another municipality's byelaws
  should require zero code changes.
- **Uncertainty is a first-class value, not an exception.** Every important
  field is a `ValueField` (`backend/schemas/evidence.py`) carrying a
  `Confidence` (`HIGH/MEDIUM/LOW/CONFLICTING/MISSING`), evidence, and — for
  raw/normalized numeric fields — both the original and canonical
  `UnitValue`. `ComplianceStatus` mirrors this: `INSUFFICIENT_DATA`,
  `CONFLICTING_EVIDENCE`, and `REQUIRES_REVIEW` are as valid an outcome as
  `PASS`/`FAIL`.
- **Units are converted once, at the normalization boundary**
  (`backend/tools/unit_conversion.py`, built on `backend/schemas/units.py`).
  Everything from `NormalizedPlan` onward is in canonical units (metres,
  sq m, %, ratio). Raw values are preserved alongside normalized ones for
  audit purposes.

## Module ownership (Phase 1)

| Path | Owns | Status |
|---|---|---|
| `backend/schemas/` | All shared models/enums/contracts | Implemented |
| `backend/config.py` | Centralized settings | Implemented |
| `backend/tools/` | Unit conversion, logging | Implemented |
| `backend/cv_extraction/interfaces.py` | Extractor interface (no logic) | Interface only |
| `backend/runtime_rules/contracts.py` | Rule/engine interfaces (no logic) | Interface only |
| `backend/compliance/interfaces.py` | Stable import surface for compliance | Implemented (re-exports Phase 4 engine) |
| `backend/app/main.py` | App wiring, `/health` | Skeleton only |
| `backend/rag/` | Regulation ingestion (parse/chunk/embed/index) + BM25/dense/hybrid retrieval, per municipality | Implemented — see `PHASE3_RAG_COMPLIANCE_NOTES.md` |
| `backend/rase/` | Condition DSL (`applies_when`/`threshold`) + Groq-assisted rule drafting with human-reviewed promotion | Implemented — see `PHASE3_RAG_COMPLIANCE_NOTES.md` |
| `backend/compliance/engine.py` | Deterministic `RuleEngine`/`RuleEvaluator` producing `ComplianceResult` | Implemented — see `PHASE3_RAG_COMPLIANCE_NOTES.md` |
| `frontend/` | — | Not started, later phases |

## Data flow contract for downstream teammates

1. **Extraction (Phase 2)** produces `ExtractionResult` per document. It may
   emit multiple competing `PlotCandidate` / `BuildingCandidate` /
   `RoadCandidate` for the same region — resolution happens next.
2. **Spatial reasoning (Phase 2/3)** resolves candidates + dimensions +
   spatial relations into exactly one `NormalizedPlan`. Any field it cannot
   resolve confidently must be set via `ValueField.missing(...)` or
   `ValueField.conflicting(...)` — never guessed.
3. **RAG/RASE (Phase 3)** turns regulation text into `RuntimeRuleDefinition`
   records, stored under `data/runtime_rules/<municipality>/`. The `applies_when`
   and `threshold` fields are intentionally untyped `dict[str, Any]` in
   Phase 1 — RASE owns that expression schema.
4. **RuleEngine (Phase 4)** implements `RuleEngine`/`RuleEvaluator`, reading
   a `NormalizedPlan` + `RuntimeRuleDefinition`s and producing a
   `ComplianceResult`. It must be deterministic — no network/LLM calls
   inside `RuleEvaluator.evaluate()`.
5. **API/report/frontend** consume `NormalizedPlan` and `ComplianceResult`
   only, via `backend/compliance/interfaces.py` and future routers mounted
   on `backend/app/main.py:app`.

## Coordinate & unit contracts

- Page-space geometry (`CoordinateSpace.PAGE_POINTS`) uses the source
  document's native point/pixel space, origin top-left, X right, Y down.
- Metric plan-space geometry (`CoordinateSpace.METRIC_PLAN`) is what
  `NormalizedPlan` and the RuleEngine consume. The `points_per_metre` scale
  factor and `rotation_degrees` used for the transform are always recorded
  on `NormalizedGeometry` for auditability — never discarded.
- Canonical units: length = metres, area = sq m, percentage = %,
  FAR = dimensionless ratio. See `backend/schemas/units.py` for the full
  list of supported input units (mm, cm, m, ft, in, ft+in, sq ft, sq m).

## What Phase 1 deliberately does not implement

Sophisticated CV extraction, RAG, the RASE compiler, RuleEngine logic,
the frontend, and Docker deployment are out of scope. Phase 1 provides the
interfaces (`GeometryExtractor`, `RuleEngine`, `RuleEvaluator`) those phases
build against, plus forward-looking, not-yet-implemented abstractions for
DXF/BIM ingestion and cross-document contradiction detection
(`DocumentSet`, `DocumentEntity`, `Contradiction`, `ContradictionReport` in
`backend/schemas/extraction.py`) so later phases don't require breaking
changes to this contract.
