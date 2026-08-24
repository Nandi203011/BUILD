"""
Run the end-to-end architectural-plan -> RAG -> deterministic compliance pipeline.

This CLI intentionally keeps the LLM out of the final compliance decision.

Pipeline:
    PDF
      -> native/CV + optional Vision extraction
      -> NormalizedPlan
      -> one RAG query per compliance-relevant plan field
      -> FAISS + BM25 + RRF hybrid retrieval
      -> RASE LLM drafts RuntimeRuleDefinition from retrieved clauses
      -> deterministic RuleEvaluator evaluates the extracted plan
      -> JSON report containing plan, retrieved evidence, drafted rules and results

The RASE drafts are EPHEMERAL for this run. They are NOT promoted to
data/runtime_rules/<municipality>/rules.json. This makes the command useful
for testing RAG-grounded compliance without silently changing the authoritative
ruleset.

Usage:
    python -m backend.tools.run_full_compliance data/test_plans/PLAN5.pdf \
        --municipality BBMP --vision --backend api

Requirements:
    1. Regulation corpus has been ingested:
         python -m backend.tools.run_ingest_regulations BBMP
    2. GROQ_API_KEY is configured because RASE uses the configured Groq model.
"""

from __future__ import annotations

import argparse
import json
import sys
import traceback
from pathlib import Path
from typing import Any

from backend.config import get_settings
from backend.compliance.engine import DeterministicRuleEvaluator
from backend.runtime_rules.contracts import RuleContext
from backend.rag.retrieval.hybrid_retriever import hybrid_search
from backend.rase.extractor import draft_rule
from backend.schemas.enums import ConfidenceLevel
from backend.cv_extraction.pdf_extractor import PDFHybridExtractor
from backend.spatial_reasoning.pipeline import build_normalized_plan


FIELD_QUERIES: dict[str, str] = {
    "plot.width": "minimum plot width requirement residential building",
    "plot.depth": "minimum plot depth requirement residential building",
    "plot.area": "minimum plot area requirement residential building",
    "building.width": "minimum building width requirement residential building",
    "building.depth": "minimum building depth requirement residential building",
    "building.footprint_area": "maximum ground floor built up footprint area",
    "road.width": "minimum road width requirement for residential building access",
    "setbacks.front": "minimum front setback residential building",
    "setbacks.rear": "minimum rear setback residential building",
    "setbacks.left": "minimum left side setback residential building",
    "setbacks.right": "minimum right side setback residential building",
    "coverage": "maximum ground coverage percentage residential building",
    "far": "maximum floor area ratio FAR residential building",
}


def _fmt(vf: Any) -> str:
    if vf is None:
        return "n/a"
    if getattr(vf, "value", None) is None:
        return f"MISSING ({vf.confidence})"
    return f"{vf.value} ({vf.confidence})"


def _plan_summary(plan: Any) -> str:
    return "\n".join(
        [
            f"plot:     width={_fmt(plan.plot.width)}  depth={_fmt(plan.plot.depth)}  area={_fmt(plan.plot.area)}",
            f"building: width={_fmt(plan.building.width)}  depth={_fmt(plan.building.depth)}  footprint_area={_fmt(plan.building.footprint_area)}",
            f"road:     width={_fmt(plan.road.width)}",
            f"setbacks: front={_fmt(plan.setbacks.front)}  rear={_fmt(plan.setbacks.rear)}  left={_fmt(plan.setbacks.left)}  right={_fmt(plan.setbacks.right)}",
            f"coverage: {_fmt(plan.coverage)}   far: {_fmt(plan.far)}",
        ]
    )


def _field_value(plan: Any, field: str) -> Any:
    obj = plan
    for part in field.split("."):
        obj = getattr(obj, part)
    return obj


def _run_extraction(pdf: Path, vision: bool, backend: str | None, max_new_tokens: int | None):
    settings = get_settings()
    if vision:
        settings.vision_enabled = True
    if backend:
        settings.vision_backend = backend
    if max_new_tokens:
        settings.vision_max_new_tokens = max_new_tokens

    print(f"vision_enabled={settings.vision_enabled}", flush=True)
    if settings.vision_enabled:
        from backend.vision_extraction import get_vision_extractor

        extractor = get_vision_extractor()
        print(
            f"vision backend: {type(extractor).__name__} ({extractor.model_name})",
            flush=True,
        )

    document_id = pdf.stem
    print(
        f"\n[1/4] Extracting architectural parameters "
        f"(native/OpenCV{' + vision' if settings.vision_enabled else ''})...",
        flush=True,
    )
    extraction = PDFHybridExtractor().extract(pdf, document_id)

    print(
        f"  Extracted {len(extraction.dimensions)} native/CV dimension(s), "
        f"{len(extraction.plot_candidates)} plot candidate(s), "
        f"{len(extraction.building_candidates)} building candidate(s), "
        f"{len(extraction.vision_pages)} vision page result(s).",
        flush=True,
    )

    if extraction.warnings:
        for warning in extraction.warnings:
            print(f"  WARNING: {warning}", flush=True)

    plan = build_normalized_plan(extraction, plan_id=f"plan-{document_id}")

    print("\nResolved NormalizedPlan:", flush=True)
    for line in _plan_summary(plan).splitlines():
        print(f"  {line}", flush=True)

    return settings, extraction, plan


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Architectural PDF -> CV/Vision -> NormalizedPlan -> RAG/RASE -> deterministic compliance."
    )
    parser.add_argument("pdf", type=Path)
    parser.add_argument("--municipality", required=True)
    parser.add_argument(
        "--vision",
        action="store_true",
        help="Enable vision extraction for this run.",
    )
    parser.add_argument(
        "--backend",
        choices=["smolvlm", "qwen", "api"],
        default=None,
        help="Vision backend override.",
    )
    parser.add_argument("--max-new-tokens", type=int, default=None)
    parser.add_argument(
        "--output",
        type=Path,
        default=None,
        help="Write the complete end-to-end JSON report here.",
    )
    parser.add_argument(
        "--top-k",
        type=int,
        default=None,
        help="Hybrid RAG chunks retained per field. Defaults to configured rag_top_k_final.",
    )
    parser.add_argument(
        "--all-fields",
        action="store_true",
        help="Attempt RAG/RASE for all supported fields, including fields missing from the plan. "
        "Normally missing fields are skipped.",
    )
    args = parser.parse_args()

    if not args.pdf.exists():
        print(f"ERROR: PDF not found: {args.pdf}", file=sys.stderr)
        sys.exit(2)

    municipality = args.municipality.upper()

    try:
        settings, extraction, plan = _run_extraction(
            args.pdf, args.vision, args.backend, args.max_new_tokens
        )
    except Exception:
        print("\nExtraction/resolution failed:\n", file=sys.stderr)
        traceback.print_exc()
        sys.exit(1)

    print("\n[2/4] Retrieving applicable regulation clauses with FAISS + BM25 + RRF...", flush=True)

    retrieved: dict[str, list[dict[str, Any]]] = {}
    drafts: list[dict[str, Any]] = []
    evaluator = DeterministicRuleEvaluator()
    results: list[dict[str, Any]] = []

    for field, query in FIELD_QUERIES.items():
        observed = _field_value(plan, field)

        if not args.all_fields:
            if observed is None or observed.value is None:
                print(f"  - {field}: skipped (missing measurement)", flush=True)
                continue
            if observed.confidence.level in (
                ConfidenceLevel.MISSING,
                ConfidenceLevel.CONFLICTING,
            ):
                print(
                    f"  - {field}: skipped ({observed.confidence.level.value})",
                    flush=True,
                )
                continue

        try:
            chunks = hybrid_search(
                query,
                municipality,
                top_k=args.top_k or settings.rag_top_k_final,
                settings=settings,
            )
        except Exception as exc:
            print(f"  - {field}: retrieval ERROR: {exc}", flush=True)
            retrieved[field] = []
            continue

        retrieved[field] = chunks
        print(f"  - {field}: {len(chunks)} chunks retrieved", flush=True)

    print("\n[3/4] Converting retrieved regulation text into ephemeral RASE rules...", flush=True)

    for field, query in FIELD_QUERIES.items():
        chunks = retrieved.get(field, [])
        if not chunks:
            continue

        try:
            draft = draft_rule(query, municipality, settings=settings)
        except Exception as exc:
            print(f"  - {field}: RASE ERROR: {exc}", flush=True)
            continue

        if draft is None:
            print(f"  - {field}: no grounded numeric rule could be drafted", flush=True)
            continue

        # Keep the field/query association in the report. The rule itself
        # remains an ephemeral draft and is evaluated only for this run.
        draft_payload = json.loads(draft.model_dump_json())
        draft_payload["source_field"] = field
        drafts.append(draft_payload)

        try:
            rule_result = evaluator.evaluate(
                RuleContext(plan=plan, rule=draft.rule)
            )
            result_payload = json.loads(rule_result.model_dump_json())
            result_payload["source_field"] = field
            result_payload["drafted_from_query"] = query
            result_payload["source_chunk_ids"] = draft.source_chunk_ids
            results.append(result_payload)

            print(
                f"  - {field}: {rule_result.status.value.upper()} | "
                f"{rule_result.required_value_description}",
                flush=True,
            )
        except Exception as exc:
            print(f"  - {field}: evaluation ERROR: {exc}", flush=True)

    # Conservative roll-up matching ComplianceResult semantics.
    statuses = [r["status"] for r in results]
    if "FAIL" in statuses:
        overall = "FAIL"
    elif any(
        s in statuses
        for s in (
            "CONFLICTING_EVIDENCE",
            "INSUFFICIENT_DATA",
            "REQUIRES_REVIEW",
        )
    ):
        overall = next(
            s
            for s in (
                "CONFLICTING_EVIDENCE",
                "INSUFFICIENT_DATA",
                "REQUIRES_REVIEW",
            )
            if s in statuses
        )
    elif "PASS" in statuses:
        overall = "PASS"
    else:
        overall = "NOT_APPLICABLE"

    print("\n[4/4] Final RAG-grounded compliance result", flush=True)
    print("=" * 68)
    print(f"  Municipality : {municipality}")
    print(f"  Plan         : {args.pdf.name}")
    print(f"  Rules drafted: {len(drafts)}")
    print(f"  Checks       : {len(results)}")
    print(f"  OVERALL      : {overall}")
    print("=" * 68)

    for result in results:
        print(
            f"  [{result['status']}] {result['source_field']} | "
            f"{result.get('required_value_description') or 'no requirement'}"
        )
        if result.get("citation"):
            print(f"      citation: {result['citation']}")

    report = {
        "pipeline": {
            "name": "architectural_pdf_to_rag_compliance",
            "municipality": municipality,
            "source_pdf": str(args.pdf),
            "rag": "FAISS + BM25 + Reciprocal Rank Fusion",
            "rule_generation": "RASE ephemeral draft from retrieved regulation text",
            "final_decision": "deterministic RuleEvaluator",
            "live_ruleset_modified": False,
        },
        "plan": plan.model_dump(mode="json"),
        "retrieval": retrieved,
        "draft_rules": drafts,
        "compliance": {
            "overall_status": overall,
            "rule_results": results,
        },
    }

    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(
            json.dumps(report, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
        print(f"\nComplete report written to {args.output}", flush=True)


if __name__ == "__main__":
    main()
