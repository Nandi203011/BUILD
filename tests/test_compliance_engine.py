from __future__ import annotations

import json

import pytest

from backend.compliance.engine import DeterministicRuleEvaluator, JsonFileRuleEngine
from backend.runtime_rules.contracts import RuleContext, RuntimeRuleDefinition
from backend.schemas.enums import ComplianceStatus, ConfidenceLevel
from tests.conftest import make_value_field


def _rule(rule_id, threshold, applies_when=None, citation="Regulation X"):
    return RuntimeRuleDefinition(
        rule_id=rule_id,
        municipality="BBMP",
        citation=citation,
        description=f"Test rule {rule_id}",
        applies_when=applies_when or {},
        threshold=threshold,
        version="1.0.0",
    )


def test_pass(sample_normalized_plan):
    rule = _rule("r1", {"field": "setbacks.front", "op": ">=", "value": 3.0})
    result = DeterministicRuleEvaluator().evaluate(RuleContext(plan=sample_normalized_plan, rule=rule))
    assert result.status == ComplianceStatus.PASS
    assert result.observed_value.value == 3.0
    assert result.citation == "Regulation X"


def test_fail(sample_normalized_plan):
    rule = _rule("r2", {"field": "coverage", "op": "<=", "value": 50.0})
    result = DeterministicRuleEvaluator().evaluate(RuleContext(plan=sample_normalized_plan, rule=rule))
    assert result.status == ComplianceStatus.FAIL


def test_not_applicable(sample_normalized_plan):
    rule = _rule(
        "r3",
        {"field": "road.width", "op": ">=", "value": 12.0},
        applies_when={"field": "plot.area", "op": ">", "value": 100000},
    )
    result = DeterministicRuleEvaluator().evaluate(RuleContext(plan=sample_normalized_plan, rule=rule))
    assert result.status == ComplianceStatus.NOT_APPLICABLE


def test_insufficient_data_missing_field(sample_normalized_plan):
    rule = _rule("r4", {"field": "building.floor_count", "op": "<=", "value": 4})
    result = DeterministicRuleEvaluator().evaluate(RuleContext(plan=sample_normalized_plan, rule=rule))
    assert result.status == ComplianceStatus.INSUFFICIENT_DATA


def test_insufficient_data_indeterminate_applicability(sample_normalized_plan):
    sample_normalized_plan.building.floor_count = None
    rule = _rule(
        "r5",
        {"field": "setbacks.front", "op": ">=", "value": 3.0},
        applies_when={"field": "building.floor_count", "op": ">", "value": 2},
    )
    result = DeterministicRuleEvaluator().evaluate(RuleContext(plan=sample_normalized_plan, rule=rule))
    assert result.status == ComplianceStatus.INSUFFICIENT_DATA


def test_conflicting_evidence(sample_normalized_plan):
    from backend.schemas.evidence import Conflict

    sample_normalized_plan.setbacks.front = make_value_field(0.0, level=ConfidenceLevel.CONFLICTING)
    sample_normalized_plan.setbacks.front.conflict = Conflict(
        description="Two dimension lines disagree on the front setback."
    )
    rule = _rule("r6", {"field": "setbacks.front", "op": ">=", "value": 3.0})
    result = DeterministicRuleEvaluator().evaluate(RuleContext(plan=sample_normalized_plan, rule=rule))
    assert result.status == ComplianceStatus.CONFLICTING_EVIDENCE
    assert "disagree" in result.explanation


def test_requires_review_on_low_confidence(sample_normalized_plan):
    sample_normalized_plan.setbacks.front = make_value_field(3.0, level=ConfidenceLevel.LOW)
    rule = _rule("r7", {"field": "setbacks.front", "op": ">=", "value": 3.0})
    result = DeterministicRuleEvaluator().evaluate(RuleContext(plan=sample_normalized_plan, rule=rule))
    assert result.status == ComplianceStatus.REQUIRES_REVIEW


def test_malformed_threshold_is_insufficient_data_not_a_crash(sample_normalized_plan):
    rule = _rule("r8", {"all": [{"field": "setbacks.front", "op": ">=", "value": 3.0}]})
    result = DeterministicRuleEvaluator().evaluate(RuleContext(plan=sample_normalized_plan, rule=rule))
    assert result.status == ComplianceStatus.INSUFFICIENT_DATA


def test_overall_rollup_fail_dominates(sample_normalized_plan):
    from backend.schemas.compliance import ComplianceResult

    rules = [
        _rule("pass1", {"field": "setbacks.front", "op": ">=", "value": 3.0}),
        _rule("fail1", {"field": "coverage", "op": "<=", "value": 50.0}),
    ]
    evaluator = DeterministicRuleEvaluator()
    results = [evaluator.evaluate(RuleContext(plan=sample_normalized_plan, rule=r)) for r in rules]
    cr = ComplianceResult(plan_id="p", ruleset_id="BBMP", rule_results=results)
    assert cr.overall_status == ComplianceStatus.FAIL


def test_json_file_rule_engine_end_to_end(tmp_path, sample_normalized_plan, monkeypatch):
    from backend.config import Settings

    rules_dir = tmp_path / "runtime_rules" / "BBMP"
    rules_dir.mkdir(parents=True)
    (rules_dir / "rules.json").write_text(
        json.dumps(
            [
                json.loads(_rule("front", {"field": "setbacks.front", "op": ">=", "value": 3.0}).model_dump_json()),
                json.loads(_rule("cov", {"field": "coverage", "op": "<=", "value": 50.0}).model_dump_json()),
            ]
        )
    )
    settings = Settings(runtime_rules_dir=tmp_path / "runtime_rules")
    engine = JsonFileRuleEngine(settings=settings)
    result = engine.evaluate_plan(sample_normalized_plan, "BBMP")
    assert result.overall_status == ComplianceStatus.FAIL
    assert result.count_by_status() == {"PASS": 1, "FAIL": 1}


def test_json_file_rule_engine_no_ruleset_returns_empty(tmp_path, sample_normalized_plan):
    from backend.config import Settings

    settings = Settings(runtime_rules_dir=tmp_path / "empty")
    engine = JsonFileRuleEngine(settings=settings)
    result = engine.evaluate_plan(sample_normalized_plan, "NOWHERE")
    assert result.rule_results == []
    assert result.overall_status == ComplianceStatus.NOT_APPLICABLE
