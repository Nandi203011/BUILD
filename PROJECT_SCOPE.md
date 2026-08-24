# BUILDCheck India — Project Scope

## Phase 1 (this phase) — Foundation, Architecture & Shared Data Contracts

**Owner:** Teammate 1

**Delivered:**
- Directory structure (`backend/{app,schemas,cv_extraction,compliance,rag,
  runtime_rules,tools}`, `data/{regulations,runtime_rules,test_plans,
  vector_store}`, `tests/`)
- Shared enums: `ConfidenceLevel`, `ComplianceStatus`, `SourceType`,
  `DocumentType`, `EntityKind`, `SpatialRelationType`
- Core geometry: `Point`, `BoundingBox`, `Line`, `Polygon`, `Dimension`,
  `NormalizedGeometry`, `SpatialRelation`
- Evidence/confidence model: `TextEvidence`, `GeometryEvidence`,
  `Confidence`, `Conflict`, `ValueField`
- Unit contract: `LengthUnit`, `AreaUnit`, `CanonicalUnit`, `FeetInches`,
  conversion functions + `backend/tools/unit_conversion.py`
- Candidates: `PlotCandidate`, `BuildingCandidate`, `RoadCandidate`,
  `SetbackMeasurement`
- `ExtractionResult` + forward-looking `DocumentSet`/`DocumentEntity`/
  `Contradiction`/`ContradictionReport` interfaces
- `NormalizedPlan` and its sections (`PlotSection`, `BuildingSection`,
  `RoadSection`, `SetbackSection`)
- `ComplianceResult`, `RuleResult`, with conservative status rollup
- Runtime rule interfaces: `RuntimeRuleDefinition`, `RuleContext`,
  `RuleEvaluator`, `RuleEngine` (no evaluation logic)
- Extraction interface: `GeometryExtractor` + `DXFGeometryExtractor` /
  `BIMGeometryExtractor` placeholders (no extraction logic)
- Centralized `Settings` (`backend/config.py`) + `.env.example`
- Logging conventions (`backend/tools/logging_config.py`)
- Minimal FastAPI skeleton (`/health` only)
- 33 passing unit tests covering unit conversion, geometry, evidence/
  confidence, and `NormalizedPlan` serialization/missing-field handling
- This document + `ARCHITECTURE.md`

**Explicitly NOT delivered in Phase 1** (belongs to later phases):
sophisticated CV extraction, RAG, the RASE compiler, RuleEngine
evaluation logic, the frontend, Docker/deployment, DXF/BIM ingestion,
cross-document contradiction detection logic.

## Phase 3 / RASE / Phase 4 — RAG, rule drafting, and deterministic compliance

**Status:** Implemented. See `PHASE3_RAG_COMPLIANCE_NOTES.md` for the full
breakdown (`backend/rag/`, `backend/rase/`, `backend/compliance/engine.py`,
CLIs, synthetic BBMP sample data, and test coverage).

## Later phases (indicative — owned by teammates 2–7)

| Phase | Scope |
|---|---|
| 2 | PDF/vector/OCR extraction against `GeometryExtractor`; produces `ExtractionResult` |
| 2/3 | Spatial reasoning: resolves candidates -> single `NormalizedPlan` |
| 3 | RAG over BBMP regulations (MiniLM embeddings + FAISS/BM25 + RRF) |
| 3 | RASE: compiles regulation text -> `RuntimeRuleDefinition` records |
| 4 | Deterministic `RuleEngine` implementation -> `ComplianceResult` |
| 5 | Report generation from `ComplianceResult` |
| 6 | Frontend consuming the API |
| 7 | Deployment (Docker Compose, CI) |

## Non-negotiable constraints across all phases

- The LLM never sets `ComplianceStatus`. Only a deterministic
  `RuleEvaluator` may.
- No municipality-specific legal threshold is ever hard-coded in Python —
  it is loaded from `data/runtime_rules/`.
- No stage downstream of extraction may depend on document format (PDF vs
  image vs future DXF/BIM) — always go through `GeometryExtractor` /
  `ExtractionResult`.
- No numeric field skips the `ValueField` wrapper — raw value, normalized
  value, confidence, source, and evidence must all be representable, and
  "missing" / "conflicting" must be representable without inventing a
  number.
