# Vision extraction implementation

## What was added

1. `backend/vision_extraction/`
   - `base.py`: shared plumbing for any VLM backend -- page-loop error
     handling, prompt formatting, and raw-model-text -> JSON parsing
     (`BaseArchitecturalPlanExtractor`). Concrete backends only implement
     `_load()` and `_generate_raw_response()`.
   - `smolvlm.py`: **default local backend.** SmolVLM2-2.2B-Instruct
     (`SmolVLMArchitecturalPlanExtractor`) -- lazy loader/inference.
   - `qwen_vl.py`: **optional, heavier local backend.** Qwen2.5-VL-7B-Instruct
     (`QwenArchitecturalPlanExtractor`) -- lazy loader/inference.
   - `api_vlm.py`: **hosted backend, no local weights.**
     (`ApiArchitecturalPlanExtractor`) -- calls an OpenAI-Chat-Completions-
     compatible vision endpoint (Groq by default) over plain `urllib`, so
     it works on machines without enough RAM/VRAM to load SmolVLM or Qwen
     locally. See "Backend: api (hosted, no local weights)" below.
   - `prompts.py`: architectural-plan semantic extraction prompt (shared,
     model-agnostic).
   - `page_renderer.py`: PDF-to-PNG rendering for VLM input (shared).
   - `spatial.py`: converts VLM image coordinates back to PDF page points
     and computes semantic spatial scores (shared).
   - `__init__.py`: `get_vision_extractor()` factory -- selects the backend
     class from `settings.vision_backend` (`smolvlm` | `qwen` | `api`).
     Callers (`pdf_extractor.py`, `run_vision.py`) go through this instead
     of importing a specific backend, so switching backends is a config
     change, not a code change.

2. `backend/schemas/vision.py`
   - Structured `VisionRegion`, `VisionDimension`, `VisionArea`,
     `VisionPageResult`, `VisionDocumentResult`.

3. `ExtractionResult` and `RawExtractionBundle`
   - Both now carry `vision_pages` so the VLM output remains auditable and
     can be consumed by spatial reasoning.

4. `Dimension`
   - Now stores the source `page`, enabling page-aware VLM association.

5. `PDFHybridExtractor`
   - If `VISION_ENABLED=true`, it runs the configured vision backend after
     the native/OCR/OpenCV evidence collection.
   - If the VLM is unavailable, extraction continues with a warning instead
     of failing the document.

6. `dimension_classification.py`
   - VLM semantic labels are an additional signal for dimension
     classification. They do not replace geometry.

7. `plot_resolution.py`
   - VLM SITE_PLAN region and PLOT_WIDTH/PLOT_DEPTH associations provide
     an additive score when vision evidence exists.

8. `backend/tools/run_vision.py`
   - Standalone command for inspecting VLM output before enabling it in
     the full pipeline. Supports `--backend smolvlm|qwen` to override the
     configured backend for a single run.

## Why SmolVLM2-2.2B-Instruct is the default local backend (not Qwen2.5-VL-7B-Instruct)

Qwen2.5-VL-7B-Instruct is a real download-size/memory liability for local
iteration: ~16GB of safetensors, ~16GB+ GPU RAM for comfortable inference.
On a flaky connection or constrained disk, that download can fail
mid-transfer (this is what happened during initial testing -- a Hugging
Face Hub Xet-backend write error partway through the multi-file download).

SmolVLM2-2.2B-Instruct is ~4.4GB, needs roughly 5GB of GPU RAM, uses the
native `transformers` `AutoModelForImageTextToText`/`AutoProcessor` API
(no extra `*_vl_utils` package, no `trust_remote_code`), and is
Apache-2.0 licensed. It is a genuinely weaker model on raw benchmarks than
Qwen2.5-VL-7B -- if semantic region/dimension accuracy turns out to be
insufficient in practice on real plans, the `qwen` backend remains fully
available as a drop-in swap (same prompt, same schema, same downstream
integration -- only `VISION_BACKEND` changes).

## Backend: `api` (hosted, no local weights)

If SmolVLM2 (~4.4GB weights, ~5GB RAM/VRAM to run) still doesn't fit on
your machine -- or generation on CPU is too slow to iterate with -- use
`VISION_BACKEND=api` instead. This backend (`api_vlm.py`) loads nothing
locally: it renders each page to PNG exactly like the other backends,
then POSTs the image (base64 data URI) plus the same
`ARCHITECTURAL_PLAN_PROMPT` to a remote OpenAI-Chat-Completions-compatible
endpoint using only stdlib `urllib` -- no `torch`, no `transformers`, no
extra dependency at all.

It defaults to Groq's OpenAI-compatible endpoint
(`https://api.groq.com/openai/v1`), since this project already has
`GROQ_API_KEY` configured for text/RAG -- if `VISION_API_KEY` is left
blank and `VISION_API_BASE_URL` is still the Groq default, it reuses
`GROQ_API_KEY` automatically. Point `VISION_API_BASE_URL` at any other
OpenAI-compatible provider (OpenAI, OpenRouter, Together, Fireworks, a
self-hosted vLLM/Ollama server, etc.) and set `VISION_API_KEY` explicitly
to use that instead -- the Groq-key fallback only ever applies to Groq's
own default URL, so a key never accidentally leaks to a different
provider.

```env
VISION_ENABLED=true
VISION_BACKEND=api
# VISION_API_BASE_URL=https://api.groq.com/openai/v1   # default
# VISION_API_KEY=                                      # blank = reuse GROQ_API_KEY on the Groq default URL
VISION_API_MODEL=qwen/qwen3.6-27b   # check the provider's current vision-model docs -- these change often
```

```bash
python -m backend.tools.run_vision /path/to/plan.pdf --backend api --output vision_result.json
```

**Important caveat:** hosted vision-model line-ups (especially Groq's)
change frequently, and some are explicitly labelled "preview" rather
than production-ready. `VISION_API_MODEL`/`api_vlm.py`'s
`DEFAULT_MODEL_NAME` may go stale -- check
[console.groq.com/docs/vision](https://console.groq.com/docs/vision) (or
your chosen provider's current model list) before relying on the
built-in default, and set `VISION_API_MODEL` explicitly rather than
assuming it.

Trade-offs versus the local backends:

- **Pro:** no RAM/VRAM/disk footprint, no multi-GB download, works on
  any machine with network access -- this is what fixes an OOM crash.
- **Pro:** typically much faster per page than CPU inference of a local
  VLM.
- **Con:** requires network access and an API key; sends page images to
  a third party (check the provider's data-handling terms if the plans
  are sensitive/confidential).
- **Con:** ongoing per-request cost instead of a one-time local download.
- **Con:** you're depending on the provider keeping a vision-capable
  model available at that endpoint/model id.

`api_vlm.py` retries transient `429`/`5xx`/network errors with
exponential backoff (`VISION_API_MAX_RETRIES`, default 2) and fails fast
(no retry) on `4xx` client errors like a bad API key, since those won't
resolve themselves on retry.

Large-format architectural sheets rendered at `VISION_RENDER_DPI` can
exceed a provider's per-image pixel budget (Groq, for example, rejects
images over 33,177,600px with an HTTP 400 "Image too large" error).
`api_vlm.py` downscales (preserving aspect ratio, via Pillow -- already
a project dependency) before sending if the rendered page exceeds
`VISION_API_MAX_IMAGE_PIXELS` (default matches Groq's limit; raise it
for a provider with a higher/no cap, or set to `0` to disable).

## Installation

The normal project requirements are unchanged. The `api` backend needs
**no extra install** (stdlib `urllib` only) -- if that's the backend
you're using, skip straight to "Enable" below.

For the local backends, install the optional vision stack separately:

```bash
pip install -r requirements-vision.txt
```

This installs everything needed for the default `smolvlm` backend. The
`qwen` backend additionally needs:

```bash
pip install -r requirements-vision-qwen.txt
```

## Enable

```env
VISION_ENABLED=true
VISION_BACKEND=smolvlm
# VISION_MODEL_NAME=            # optional override; empty = backend's own default
VISION_RENDER_DPI=200
```

To use Qwen2.5-VL-7B-Instruct instead:

```env
VISION_ENABLED=true
VISION_BACKEND=qwen
```

To use the hosted `api` backend instead (no local weights -- see above):

```env
VISION_ENABLED=true
VISION_BACKEND=api
```

## First test

Run only the VLM first (defaults to the configured/env backend, or pass
`--backend` to override for a single run without touching your `.env`):

```bash
python -m backend.tools.run_vision /path/to/ARJUN.22.4.25-Model-1.pdf --output vision_result.json
python -m backend.tools.run_vision /path/to/plan.pdf --backend qwen --output vision_result_qwen.json
python -m backend.tools.run_vision /path/to/plan.pdf --backend api --output vision_result_api.json
```

Inspect `vision_result.json`. The important expected semantic distinctions
for the supplied ARJUN plan (ground-truthed directly against the rendered
PDF -- see `PHASE3_1_NOTES.md`):

- `9.14` and `12.19` associated with `SITE_PLAN` / plot frontage+depth
  (the plot's outer boundary).
- `8.22` and `11.72` associated with the floor/building region (the
  building's own boundary, inset from the plot).
- room dimensions such as `3.20 x 3.66` classified as room dimensions
  rather than plot dimensions.
- `96.34` identified as a plinth-area candidate when the model associates
  it with the area statement (this exactly equals `8.22 x 11.72`, a good
  internal-consistency check).
- setback labels `.47`/`.46` associated with `RIGHT_SETBACK` (the
  road-facing side) and `FRONT_SETBACK`/`REAR_SETBACK` respectively --
  the real drawing shows the building flush with the plot boundary on the
  side away from the road (~0 left setback), which is a good edge case to
  check the model doesn't invent a value for.

## Important limitation

The VLM output is currently an **evidence source**, not the final
authority. The project still uses PDF-native geometry, OCR and OpenCV for
measurement. The next implementation stage should add a formal
evidence-fusion/conflict resolver and evaluate VLM+geometry against the
existing pipeline on a labelled set of architectural plans (ground-truth
evaluation harness, still to be built -- see PHASE3_1_NOTES.md).
