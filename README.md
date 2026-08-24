# BUILDCheck India

Pre-submission advisory system for checking architectural building plans
against Indian municipal building regulations (current target: Bengaluru /
BBMP-style regulations, kept externally configurable — see
`ARCHITECTURE.md`).

## Status

Phase 1 complete: foundation, architecture, and shared data contracts.
See `PROJECT_SCOPE.md` for what's implemented vs. what's owned by later
phases, and `ARCHITECTURE.md` for the full pipeline and module boundaries.

## Setup

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env   # fill in GROQ_API_KEY etc. locally; never commit .env
```

## Run tests

```bash
pytest tests/ -v
```

## Run the API skeleton

```bash
uvicorn backend.app.main:app --reload
# -> GET http://localhost:8000/health
```

## Directory structure

```
backend/
    app/            # FastAPI app wiring
    schemas/        # all shared models/enums — THE contract
    cv_extraction/  # extractor interfaces (Phase 2 implements logic)
    compliance/     # compliance interfaces (Phase 4 implements logic)
    rag/            # Phase 3
    runtime_rules/  # runtime rule + rule engine interfaces
    tools/          # unit conversion, logging
    config.py       # centralized settings
frontend/           # Phase 6
data/
    regulations/    # source regulation documents
    runtime_rules/  # compiled RuntimeRuleDefinition records (JSON/YAML)
    test_plans/     # sample plans for testing
    vector_store/   # FAISS/BM25 index artifacts
tests/
```

## Contributing (team contract)

If you're building against this: import from `backend/schemas/`,
`backend/compliance/interfaces.py`, `backend/cv_extraction/interfaces.py`,
or `backend/runtime_rules/contracts.py` — not from each other's
work-in-progress modules. See `ARCHITECTURE.md` for exactly which stage
owns which contract.

## Optional multimodal architectural-plan extraction

The project now contains an optional Qwen2.5-VL semantic extraction layer. It is disabled by default so the existing PDF/OCR/OpenCV test suite remains unchanged.

Install the optional stack on a machine with a suitable GPU:

```bash
pip install -r requirements-vision.txt
```

Enable it in `.env`:

```env
VISION_ENABLED=true
VISION_MODEL_NAME=Qwen/Qwen2.5-VL-7B-Instruct
VISION_RENDER_DPI=200
```

Run the semantic extractor directly against a plan before integrating its candidates into final resolution:

```bash
python -m backend.tools.run_vision path/to/plan.pdf --output vision_result.json
```

The vision layer returns semantic regions, dimension associations, area candidates, evidence text, bounding boxes and confidence. It is an evidence provider; the existing PDF-native/OCR/OpenCV geometry remains responsible for measurement and validation.

## Phase 3.3 independent validation

Use the three-stage validation workflow rather than treating `run_pipeline` as the validation oracle:

```powershell
python -m backend.tools.run_cv data\test_plans\PLAN2.pdf --output cv_res2.json
python -m backend.tools.run_vision data\test_plans\PLAN2.pdf --backend api --no-grounding
python -m backend.tools.run_validation data\test_plans\PLAN2.pdf --vision-backend api --output validation.json
```

See `PHASE3_3_INDEPENDENT_VALIDATION.md` for the evidence contract and PLAN2 acceptance values.

## Phase 3.3 independent validation

Use the independent tools before judging the legacy/full pipeline:

```powershell
python -m backend.tools.run_cv data\test_plans\PLAN2.pdf --output cv_res2.json
python -m backend.tools.run_vision data\test_plans\PLAN2.pdf --backend api --no-grounding
python -m backend.tools.run_validation data\test_plans\PLAN2.pdf --vision-backend api --output validation.json
```

See `PHASE3_3_FINAL_VALIDATION_CONTRACT.md` for the source-of-truth fields, PLAN2 acceptance values, and the rule that legacy CV is excluded from agreement.
