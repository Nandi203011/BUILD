# Phase 3 (RAG) + RASE + Phase 4 (Compliance Engine) — Notes

This implements the remaining pipeline stages after `NormalizedPlan`:

```
NORMALIZED PLAN
   -> RAG                (backend/rag)          regulation text -> retrievable chunks
   -> RASE                (backend/rase)          retrieved chunks -> RuntimeRuleDefinition (DRAFT)
   -> [human review + promote_draft()]            DRAFT -> live ruleset (data/runtime_rules/<M>/rules.json)
   -> DETERMINISTIC RULE ENGINE  (backend/compliance/engine.py)  plan + live ruleset -> ComplianceResult
```

The hard boundary from `ARCHITECTURE.md` is preserved end-to-end: **the LLM
never sets `ComplianceStatus`.** Groq only drafts a *candidate*
`RuntimeRuleDefinition`, tagged `DRAFT`, cited back to the exact
`chunk_id`s it was built from, in a file (`drafts.json`) the engine never
reads. A rule only affects a compliance check after a human calls
`promote_draft()`, which copies it into `rules.json` — the one file
`JsonFileRuleEngine` reads — and `DeterministicRuleEvaluator.evaluate()`
makes zero network/LLM calls.

## What's new

| Path | Purpose |
|---|---|
| `backend/rag/ingestion/pdf_parser.py` | PyMuPDF + pdfplumber extraction of regulation PDFs (and plain `.txt`, for a smoke-test corpus), doc-type/city inferred from filename via configurable keyword maps (`Settings.rag_doc_type_keywords` / `rag_city_keywords`) |
| `backend/rag/ingestion/chunker.py` | Clause-aware splitter (NBC `7.3.1`, DCR `Regulation N.` / `Section N` / `Chapter N`, plain numbered headings), sub-splits long clauses with paragraph-boundary overlap |
| `backend/rag/ingestion/embedder.py` | MiniLM sentence-transformer embeddings (`Settings.embedding_model_name`), L2-normalised; `set_stub_encoder()` lets tests/offline runs skip the real model download |
| `backend/rag/ingestion/indexer.py` | FAISS `IndexFlatIP` + JSONL metadata, **one index per municipality** (`data/vector_store/<MUNICIPALITY>/`), idempotent via content hash |
| `backend/rag/ingestion/pipeline.py` | Orchestrates parse → chunk → embed → index for one municipality |
| `backend/rag/retrieval/{bm25,dense,hybrid}_retriever.py` | BM25 (rank-bm25) + dense (FAISS) + Reciprocal Rank Fusion, all municipality-scoped and independently cached/reloadable |
| `backend/rase/schema.py` | The condition DSL that fills `RuntimeRuleDefinition.applies_when`/`.threshold` (explicitly left as `dict[str, Any]`, "schema owned by RASE" in `runtime_rules/contracts.py`). Leaf `{"field", "op", "value"}` + `all`/`any`/`not` combinators. **Three-valued**: a condition referencing a MISSING or CONFLICTING `ValueField` evaluates to `None` (indeterminate), never guessed as `False` |
| `backend/rase/extractor.py` | `draft_rule(query, municipality)`: hybrid-searches the municipality's corpus, asks Groq to draft one rule from the retrieved text only, refuses if too few chunks retrieved or no concrete threshold found. `save_draft` / `load_drafts` / `promote_draft` manage the draft → live lifecycle |
| `backend/compliance/engine.py` | `DeterministicRuleEvaluator` (implements `RuleEvaluator`) + `JsonFileRuleEngine` (implements `RuleEngine`), reading `data/runtime_rules/<MUNICIPALITY>/rules.json` |
| `backend/tools/run_ingest_regulations.py` | CLI: ingest a municipality's `data/regulations/<M>/*.pdf`+`*.txt` |
| `backend/tools/run_rase_draft.py` | CLI: `draft` / `list` / `promote` |
| `backend/tools/run_compliance.py` | CLI: evaluate a `NormalizedPlan` JSON against a municipality's live ruleset |
| `data/regulations/BBMP/synthetic_bbmp_byelaws.txt` | **Synthetic, clearly labeled** sample corpus (front/rear/side setback, coverage, FAR, road width, floor count) so the pipeline is smoke-testable without a real scanned byelaw PDF |
| `data/runtime_rules/BBMP/rules.json` | Hand-authored (already-"promoted") ruleset matching the synthetic corpus above, for compliance-engine smoke testing |

## Compliance status derivation (the actual answer to "is this value compliant")

For each rule, `DeterministicRuleEvaluator.evaluate()`:

1. Evaluates `applies_when` against the plan.
   - `False` → **NOT_APPLICABLE**
   - indeterminate (a needed field is MISSING/CONFLICTING) → **INSUFFICIENT_DATA**
2. If applicable, resolves the `threshold`'s field on the plan.
   - field MISSING → **INSUFFICIENT_DATA**
   - field CONFLICTING → **CONFLICTING_EVIDENCE** (explanation carries the plan's own `Conflict.description`)
   - field LOW confidence → **REQUIRES_REVIEW**, even if the comparison itself would pass or fail — a low-confidence measurement is never silently turned into a verdict
   - otherwise → **PASS** or **FAIL** based on the comparison

`ComplianceResult.overall_status` (already defined in Phase 1's
`schemas/compliance.py`) rolls these up conservatively: any `FAIL`
dominates; otherwise any `CONFLICTING_EVIDENCE`/`INSUFFICIENT_DATA`/
`REQUIRES_REVIEW` dominates over `PASS`.

## Verified end-to-end (see `tests/`)

- `test_chunker_regulations.py` — clause boundary detection, self-contained
  chunks, chunk-id scoping, sub-splitting with overlap (5 tests)
- `test_rase_schema.py` — field resolution, all/any/not combinators,
  three-valued logic on missing/conflicting fields, operator coverage
  (14 tests)
- `test_rase_extractor.py` — successful draft, refusal on too-few chunks,
  refusal on the model's own "no threshold found", missing-API-key error,
  save/load/promote round-trip (6 tests)
- `test_compliance_engine.py` — every `ComplianceStatus` branch individually,
  overall-status rollup, and a `JsonFileRuleEngine` end-to-end run against a
  real ruleset file on disk (11 tests)
- Manually verified: ingesting the synthetic BBMP corpus (7 chunks from 1
  file) → hybrid search for "minimum front setback residential plot"
  correctly ranks the front-setback clause first; evaluating
  `tests/conftest.sample_normalized_plan` against
  `data/runtime_rules/BBMP/rules.json` produces the expected mixed
  PASS/FAIL/NOT_APPLICABLE/INSUFFICIENT_DATA result

**271/271 tests pass** (236 pre-existing Phase 1/2/3-CV tests + 35 new).

## Using it against a real BBMP byelaw PDF

```bash
cp your_real_bbmp_byelaws.pdf data/regulations/BBMP/
python -m backend.tools.run_ingest_regulations BBMP

export GROQ_API_KEY=...   # or set in .env
python -m backend.tools.run_rase_draft draft BBMP "minimum front setback for residential plots"
python -m backend.tools.run_rase_draft list BBMP
python -m backend.tools.run_rase_draft promote BBMP <rule_id>   # after reviewing the citation

python -m backend.tools.run_pipeline your_plan.pdf --output plan.json
python -m backend.tools.run_compliance plan.json BBMP --output result.json
```

## Known gaps / next steps

- `sentence-transformers` (and therefore a real, non-stub `embed_texts`)
  needs a Hugging Face model download the first time it runs — not
  exercised in this sandbox (no HF network egress here); `embedder.set_stub_encoder()`
  is what let ingestion/retrieval be smoke-tested offline. It will work
  as-is once `pip install -r requirements.txt` runs somewhere with normal
  internet access.
- `backend/rase/extractor.py` drafts exactly one rule per call, scoped to
  one `threshold` field. A regulation clause covering several fields at
  once (e.g. "front setback 3m, rear 2m, sides 1.5m each" in one
  paragraph) currently needs one `draft_rule()` call per field — RASE
  doesn't yet split a multi-threshold clause into several drafts
  automatically.
- No FastAPI routes were added for RAG/RASE/compliance yet — only CLIs.
  `backend/app/main.py` still only has `/health`. Wiring `run_compliance`'s
  logic behind a route is straightforward next work, following the same
  pattern as the CLI.
- `rank_bm25`/`faiss-cpu` were installed and exercised in this sandbox;
  `sentence-transformers`/`groq` were not (network-restricted sandbox) —
  their code paths are covered by unit tests with stubs/mocks, but not by
  a real live call in this environment.
