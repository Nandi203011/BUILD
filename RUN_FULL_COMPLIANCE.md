# Full CLI compliance pipeline

This phase adds `backend.tools.run_full_compliance`.

## 1. Ingest regulations

```bash
python -m backend.tools.run_ingest_regulations BBMP
```

## 2. Run the full pipeline

```bash
python -m backend.tools.run_full_compliance data/test_plans/PLAN5.pdf --municipality BBMP --vision --backend api --output plan5_full_compliance.json
```

The pipeline performs:

1. PDF -> native/OpenCV + optional Vision extraction
2. Extraction -> `NormalizedPlan`
3. Per-field FAISS + BM25 + RRF retrieval
4. RASE drafts a structured rule from retrieved regulation text
5. Deterministic evaluator checks the extracted plan against the drafted rule
6. A complete JSON report is written

The RASE rules are ephemeral for the run. They are NOT promoted to
`data/runtime_rules/<municipality>/rules.json`.

## Requirements

- Run from the `build` directory.
- Regulation documents must be ingested first.
- RASE requires `GROQ_API_KEY` in `.env`.
- For API vision, configure `VISION_BACKEND=api` / `VISION_API_MODEL` and the appropriate key,
  or pass `--vision --backend api`.
