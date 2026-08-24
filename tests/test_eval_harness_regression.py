"""
Regression test for `backend.tools.eval_harness`: every plan with a
`data/test_plans/PLANn.expected.json` must never score a field WRONG
(a confident, verified-against-ground-truth mistake) for the CV or
fusion pipeline, on every test run -- not just when someone remembers
to run the harness by hand.

Deliberately not asserting on exact accuracy percentages or MISSING
counts here: MISSING is honest uncertainty (e.g. CV correctly declining
to guess on a zero-native-text page) and is expected to fluctuate as
heuristics improve incrementally; a field flipping from MISSING to
CORRECT is progress, not a regression to guard against. A field
scoring WRONG, however, means a pipeline produced a value that
contradicts hand-verified ground truth with confidence -- that should
never happen silently.

Vision scoring is skipped here (not asserted on) when VISION_ENABLED is
off or vision can't be reached (no network/model access in CI) --
that's an environment limitation, not a pipeline bug, and asserting on
it would make this test flaky rather than useful.
"""

from __future__ import annotations

import pytest

from backend.tools.eval_harness import discover_plans, evaluate_plan


def _plan_ids():
    return [p[0] for p in discover_plans()]


@pytest.mark.parametrize("plan_id", _plan_ids())
def test_cv_and_fusion_never_confidently_wrong_against_ground_truth(plan_id):
    plans = {p[0]: p for p in discover_plans()}
    _, pdf_path, expected_path = plans[plan_id]

    results = evaluate_plan(plan_id, pdf_path, expected_path)

    for pipeline in ("cv", "fusion"):
        result = results[pipeline]
        if result.error:
            pytest.fail(f"{plan_id} {pipeline} pipeline crashed: {result.error}")
        wrong = [fr for fr in result.field_results if fr.verdict == "WRONG"]
        assert not wrong, (
            f"{plan_id} {pipeline}: field(s) confidently WRONG against verified ground truth: "
            + ", ".join(f"{fr.field} expected={fr.expected} actual={fr.actual}" for fr in wrong)
        )


def test_eval_harness_finds_at_least_one_ground_truth_plan():
    # Guards against a silent misconfiguration (e.g. data/test_plans/ moved
    # or emptied) making the parametrized test above silently collect zero
    # cases and pass trivially.
    assert len(discover_plans()) >= 1
