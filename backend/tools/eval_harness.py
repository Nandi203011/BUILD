"""
Eval harness: the missing piece identified after a long round of manual
bug-hunting on individual plans -- there was no repeatable way to tell
whether a change actually improved extraction accuracy across the whole
test-plan set, only whether one specific number changed on one specific
plan. This fixes that.

For every `data/test_plans/PLANn.pdf` that has a matching
`PLANn.expected.json` (hand-verified ground truth, field -> value, same
keys as `final_fusion.LENGTH_FIELDS`/`NUMERIC_FIELDS`), this:

  1. Runs the independent CV pipeline (`extract_independent_cv`).
  2. Runs the independent Vision pipeline (`get_vision_extractor()`),
     if `VISION_ENABLED`.
  3. Runs the document-evidence fusion engine
     (`build_document_verified_fusion`).
  4. Scores CV alone, Vision alone, and the fusion's `final_agreed_values`
     against `expected.json`, per field, per plan.

A field counts CORRECT when a value was produced and it's within
tolerance of expected (absolute or relative, whichever is looser -- see
`_within_tolerance`); WRONG when a value was produced but outside
tolerance; MISSING when no value was produced. This distinction matters:
a MISSING field is honest uncertainty, a WRONG field is a confident
mistake -- the two should never be conflated into one "not correct"
bucket, since a fusion engine that never guesses will show more MISSING
and less WRONG than one that does, and that trade-off should be visible,
not hidden behind a single pass/fail number.

Usage:
    python -m backend.tools.eval_harness
    python -m backend.tools.eval_harness --plans PLAN2 PLAN5
    python -m backend.tools.eval_harness --output eval_results.json
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass, field as dc_field
from pathlib import Path
from typing import Any, Optional

from backend.config import get_settings

TEST_PLANS_DIR = Path(__file__).resolve().parent.parent.parent / "data" / "test_plans"

# Looser of (absolute, relative) -- small values (setbacks) need an
# absolute floor; large values (areas) need a relative allowance, or a
# fixed absolute tolerance would be simultaneously too strict for areas
# and too loose for setbacks.
DEFAULT_ABS_TOL = 0.15
DEFAULT_REL_TOL = 0.05


def _within_tolerance(actual: float, expected: float, abs_tol: float = DEFAULT_ABS_TOL, rel_tol: float = DEFAULT_REL_TOL) -> bool:
    tol = max(abs_tol, abs(expected) * rel_tol)
    return abs(actual - expected) <= tol


@dataclass
class FieldResult:
    field: str
    expected: float
    actual: Optional[float]
    verdict: str  # "CORRECT" | "WRONG" | "MISSING"


@dataclass
class PlanResult:
    plan_id: str
    pdf_path: str
    field_results: list[FieldResult] = dc_field(default_factory=list)
    error: Optional[str] = None

    def counts(self) -> dict[str, int]:
        out = {"CORRECT": 0, "WRONG": 0, "MISSING": 0}
        for fr in self.field_results:
            out[fr.verdict] += 1
        return out

    def accuracy(self) -> Optional[float]:
        total = len(self.field_results)
        if total == 0:
            return None
        return self.counts()["CORRECT"] / total


def discover_plans() -> list[tuple[str, Path, Path]]:
    """Return (plan_id, pdf_path, expected_json_path) for every plan with both files present."""
    out = []
    if not TEST_PLANS_DIR.exists():
        return out
    for expected_path in sorted(TEST_PLANS_DIR.glob("*.expected.json")):
        plan_id = expected_path.name.removesuffix(".expected.json")
        pdf_path = TEST_PLANS_DIR / f"{plan_id}.pdf"
        if pdf_path.exists():
            out.append((plan_id, pdf_path, expected_path))
    return out


def _run_fusion_for_plan(pdf_path: Path, plan_id: str) -> dict[str, Any]:
    from backend.cv_extraction.site_plan import extract_independent_cv
    from backend.spatial_reasoning.final_fusion import build_document_verified_fusion
    from backend.vision_extraction import get_vision_extractor

    settings = get_settings()
    cv_result = extract_independent_cv(pdf_path, plan_id)
    vision_result = None
    vision_error: Optional[str] = None
    if settings.vision_enabled:
        try:
            vision_result = get_vision_extractor().analyze_pdf(pdf_path, ground_against_native_text=False)
        except Exception as exc:  # noqa: BLE001 -- eval harness must not crash on one plan's vision failure
            vision_error = str(exc)
    fusion = build_document_verified_fusion(cv_result, vision_result, pdf_path=str(pdf_path))
    return {"cv": cv_result, "vision": vision_result, "vision_error": vision_error, "fusion": fusion}


def _cv_only_values(cv_result) -> dict[str, Optional[float]]:
    from backend.spatial_reasoning.final_fusion import _cv_map

    cm = _cv_map(cv_result)
    out: dict[str, Optional[float]] = {}
    for field_name, m in cm.items():
        out[field_name] = m.value_m if m.value_m is not None else m.value
    return out


def _vision_only_values(vision_result, expected_fields: list[str]) -> dict[str, Optional[float]]:
    from backend.spatial_reasoning.final_fusion import LENGTH_FIELDS, NUMERIC_FIELDS, _vision_length, _vision_numeric

    out: dict[str, Optional[float]] = {}
    if vision_result is None:
        return out
    for field_name in expected_fields:
        if field_name in LENGTH_FIELDS:
            res = _vision_length(vision_result, LENGTH_FIELDS[field_name])
            out[field_name] = res[0] if res else None
        elif field_name in NUMERIC_FIELDS:
            res = _vision_numeric(vision_result, NUMERIC_FIELDS[field_name])
            out[field_name] = res[0] if res else None
    return out


def _fusion_only_values(fusion: dict[str, Any]) -> dict[str, Optional[float]]:
    return {
        field_name: entry.get("final_value")
        for field_name, entry in fusion.get("final_agreed_values", {}).items()
    }


def _score(expected: dict[str, float], actual: dict[str, Optional[float]]) -> list[FieldResult]:
    results = []
    for field_name, expected_value in expected.items():
        actual_value = actual.get(field_name)
        if actual_value is None:
            verdict = "MISSING"
        elif _within_tolerance(float(actual_value), float(expected_value)):
            verdict = "CORRECT"
        else:
            verdict = "WRONG"
        results.append(FieldResult(field=field_name, expected=expected_value, actual=actual_value, verdict=verdict))
    return results


def evaluate_plan(plan_id: str, pdf_path: Path, expected_path: Path) -> dict[str, PlanResult]:
    """Returns {'cv': PlanResult, 'vision': PlanResult, 'fusion': PlanResult} for one plan."""
    expected: dict[str, float] = json.loads(expected_path.read_text(encoding="utf-8"))

    try:
        run = _run_fusion_for_plan(pdf_path, plan_id)
    except Exception as exc:  # noqa: BLE001 -- one plan's crash must not abort the whole eval run
        err = f"{type(exc).__name__}: {exc}"
        empty = PlanResult(plan_id=plan_id, pdf_path=str(pdf_path), error=err)
        return {"cv": empty, "vision": empty, "fusion": empty}

    cv_values = _cv_only_values(run["cv"])
    vision_values = _vision_only_values(run["vision"], list(expected.keys()))
    fusion_values = _fusion_only_values(run["fusion"])

    return {
        "cv": PlanResult(plan_id, str(pdf_path), _score(expected, cv_values)),
        "vision": PlanResult(
            plan_id, str(pdf_path), _score(expected, vision_values),
            error=run["vision_error"],
        ),
        "fusion": PlanResult(plan_id, str(pdf_path), _score(expected, fusion_values)),
    }


def _print_report(all_results: dict[str, dict[str, PlanResult]]) -> None:
    pipelines = ["cv", "vision", "fusion"]
    print(f"\n{'Plan':<10}{'Pipeline':<10}{'Correct':<10}{'Wrong':<8}{'Missing':<10}{'Accuracy':<10}")
    print("-" * 58)
    totals = {p: {"CORRECT": 0, "WRONG": 0, "MISSING": 0} for p in pipelines}
    for plan_id, per_pipeline in all_results.items():
        for pipeline in pipelines:
            result = per_pipeline[pipeline]
            if result.error:
                print(f"{plan_id:<10}{pipeline:<10}ERROR: {result.error}")
                continue
            counts = result.counts()
            for k in totals[pipeline]:
                totals[pipeline][k] += counts[k]
            acc = result.accuracy()
            acc_str = f"{acc:.0%}" if acc is not None else "n/a"
            print(f"{plan_id:<10}{pipeline:<10}{counts['CORRECT']:<10}{counts['WRONG']:<8}{counts['MISSING']:<10}{acc_str:<10}")
    print("-" * 58)
    for pipeline in pipelines:
        c = totals[pipeline]
        total = sum(c.values())
        acc_str = f"{c['CORRECT'] / total:.0%}" if total else "n/a"
        print(f"{'TOTAL':<10}{pipeline:<10}{c['CORRECT']:<10}{c['WRONG']:<8}{c['MISSING']:<10}{acc_str:<10}")

    print("\nPer-field detail (fusion pipeline):")
    for plan_id, per_pipeline in all_results.items():
        fusion_result = per_pipeline["fusion"]
        if fusion_result.error:
            continue
        print(f"\n  {plan_id}:")
        for fr in fusion_result.field_results:
            actual_str = f"{fr.actual:.3f}" if fr.actual is not None else "MISSING"
            print(f"    {fr.field:<30} expected={fr.expected:<10} actual={actual_str:<12} {fr.verdict}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--plans", nargs="*", default=None, help="Limit to specific plan ids (e.g. PLAN2 PLAN5).")
    parser.add_argument("--output", type=Path, default=None, help="Write full results as JSON here.")
    args = parser.parse_args()

    plans = discover_plans()
    if args.plans:
        wanted = set(args.plans)
        plans = [p for p in plans if p[0] in wanted]

    if not plans:
        print(
            f"No plans found with both a .pdf and a .expected.json in {TEST_PLANS_DIR}. "
            "Add a data/test_plans/PLANn.expected.json (see PLAN2.expected.json for the field "
            "schema) for any plan you want scored.",
            file=sys.stderr,
        )
        sys.exit(1)

    settings = get_settings()
    print(f"vision_enabled={settings.vision_enabled}  |  {len(plans)} plan(s) with ground truth: "
          f"{', '.join(p[0] for p in plans)}")

    all_results: dict[str, dict[str, PlanResult]] = {}
    for plan_id, pdf_path, expected_path in plans:
        print(f"\nEvaluating {plan_id}...", flush=True)
        all_results[plan_id] = evaluate_plan(plan_id, pdf_path, expected_path)

    _print_report(all_results)

    if args.output:
        serializable = {
            plan_id: {
                pipeline: {
                    "error": result.error,
                    "counts": result.counts(),
                    "accuracy": result.accuracy(),
                    "fields": [
                        {"field": fr.field, "expected": fr.expected, "actual": fr.actual, "verdict": fr.verdict}
                        for fr in result.field_results
                    ],
                }
                for pipeline, result in per_pipeline.items()
            }
            for plan_id, per_pipeline in all_results.items()
        }
        args.output.write_text(json.dumps(serializable, indent=2, ensure_ascii=False), encoding="utf-8")
        print(f"\nFull results written to {args.output}")


if __name__ == "__main__":
    main()
