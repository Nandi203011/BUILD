"""
backend/compliance/engine.py
=============================
Concrete deterministic RuleEngine / RuleEvaluator implementation.

This is the ONLY place in the codebase permitted to produce a
ComplianceStatus (see ARCHITECTURE.md: "The LLM is never the compliance
authority"). It reads:
  - a NormalizedPlan (from spatial reasoning / CV extraction)
  - RuntimeRuleDefinition records (authored by hand, or drafted by RASE
    from RAG-retrieved regulation text and explicitly promoted by a human
    reviewer via backend.rase.extractor.promote_draft — see that module)

and evaluates the plan's fields against each rule's applies_when/threshold
(backend/rase/schema.py) with zero network/LLM calls, per the
RuleEvaluator contract.

Status derivation for one rule:
    applies_when == False           -> NOT_APPLICABLE
    applies_when indeterminate      -> INSUFFICIENT_DATA
                                        (can't tell if the rule even applies)
    threshold's observed field
        missing / unresolvable      -> INSUFFICIENT_DATA
        CONFLICTING                 -> CONFLICTING_EVIDENCE
        LOW confidence               -> REQUIRES_REVIEW
                                        (never silently PASS/FAIL on a
                                        low-confidence measurement)
        otherwise, threshold True   -> PASS
        otherwise, threshold False  -> FAIL
"""

from __future__ import annotations

import json
from typing import Optional

from backend.config import Settings, get_settings
from backend.rase.schema import evaluate_applies_when, evaluate_threshold
from backend.runtime_rules.contracts import (
    RuleContext,
    RuleEngine,
    RuleEvaluator,
    RuntimeRuleDefinition,
)
from backend.schemas.compliance import ComplianceResult, RuleResult
from backend.schemas.enums import ComplianceStatus, ConfidenceLevel
from backend.schemas.normalized_plan import NormalizedPlan
from backend.tools.logging_config import get_logger

logger = get_logger(__name__)


class DeterministicRuleEvaluator(RuleEvaluator):
    """Evaluates ONE RuntimeRuleDefinition against ONE NormalizedPlan.
    No network/LLM calls — every branch here is pure function of the
    already-extracted plan and the already-promoted rule."""

    def evaluate(self, context: RuleContext) -> RuleResult:
        plan = context.plan
        rule = context.rule

        try:
            applies = evaluate_applies_when(rule.applies_when, plan)
        except Exception as exc:
            logger.error("Rule %s: malformed applies_when: %s", rule.rule_id, exc)
            return RuleResult(
                rule_id=rule.rule_id,
                rule_description=rule.description,
                status=ComplianceStatus.INSUFFICIENT_DATA,
                explanation=f"Could not evaluate applicability condition: {exc}",
                citation=rule.citation,
            )

        if applies is False:
            return RuleResult(
                rule_id=rule.rule_id,
                rule_description=rule.description,
                status=ComplianceStatus.NOT_APPLICABLE,
                explanation="This plan does not meet the rule's applicability condition.",
                citation=rule.citation,
            )

        if applies is None:
            return RuleResult(
                rule_id=rule.rule_id,
                rule_description=rule.description,
                status=ComplianceStatus.INSUFFICIENT_DATA,
                explanation=(
                    "Whether this rule applies to the plan could not be determined "
                    "because a field needed for the applicability check is missing "
                    "or conflicting in the extracted plan."
                ),
                citation=rule.citation,
            )

        try:
            result, observed, requirement_desc = evaluate_threshold(rule.threshold, plan)
        except Exception as exc:
            logger.error("Rule %s: malformed threshold: %s", rule.rule_id, exc)
            return RuleResult(
                rule_id=rule.rule_id,
                rule_description=rule.description,
                status=ComplianceStatus.INSUFFICIENT_DATA,
                explanation=f"Could not evaluate threshold condition: {exc}",
                citation=rule.citation,
            )

        if observed is None or observed.confidence.level == ConfidenceLevel.MISSING:
            return RuleResult(
                rule_id=rule.rule_id,
                rule_description=rule.description,
                status=ComplianceStatus.INSUFFICIENT_DATA,
                observed_value=observed,
                required_value_description=requirement_desc,
                explanation="The measurement this rule checks was not found in the extracted plan.",
                citation=rule.citation,
            )

        if observed.confidence.level == ConfidenceLevel.CONFLICTING:
            return RuleResult(
                rule_id=rule.rule_id,
                rule_description=rule.description,
                status=ComplianceStatus.CONFLICTING_EVIDENCE,
                observed_value=observed,
                required_value_description=requirement_desc,
                explanation=(
                    observed.conflict.description
                    if observed.conflict
                    else "Conflicting extracted values for this measurement."
                ),
                citation=rule.citation,
            )

        if observed.confidence.level == ConfidenceLevel.LOW:
            return RuleResult(
                rule_id=rule.rule_id,
                rule_description=rule.description,
                status=ComplianceStatus.REQUIRES_REVIEW,
                observed_value=observed,
                required_value_description=requirement_desc,
                explanation=(
                    f"Observed value {observed.value} has LOW extraction confidence; "
                    f"a human should verify against the requirement '{requirement_desc}' "
                    "before this is treated as pass or fail."
                ),
                citation=rule.citation,
            )

        if result is None:
            return RuleResult(
                rule_id=rule.rule_id,
                rule_description=rule.description,
                status=ComplianceStatus.INSUFFICIENT_DATA,
                observed_value=observed,
                required_value_description=requirement_desc,
                explanation="The threshold could not be evaluated against the observed value.",
                citation=rule.citation,
            )

        status = ComplianceStatus.PASS if result else ComplianceStatus.FAIL
        explanation = (
            f"Observed {observed.value} {'satisfies' if result else 'does not satisfy'} "
            f"requirement '{requirement_desc}'."
        )
        return RuleResult(
            rule_id=rule.rule_id,
            rule_description=rule.description,
            status=status,
            observed_value=observed,
            required_value_description=requirement_desc,
            explanation=explanation,
            citation=rule.citation,
        )


class JsonFileRuleEngine(RuleEngine):
    """
    RuleEngine that loads the live (promoted) ruleset for a municipality
    from data/runtime_rules/<MUNICIPALITY>/rules.json and evaluates a
    NormalizedPlan against every rule in it.
    """

    def __init__(
        self,
        settings: Optional[Settings] = None,
        evaluator: Optional[RuleEvaluator] = None,
    ) -> None:
        self.settings = settings or get_settings()
        self.evaluator = evaluator or DeterministicRuleEvaluator()

    def load_ruleset(
        self, municipality: str, version: Optional[str] = None
    ) -> list[RuntimeRuleDefinition]:
        path = self.settings.runtime_rules_path(municipality)
        if not path.exists():
            logger.warning("No runtime ruleset found for %s at %s", municipality, path)
            return []
        raw = json.loads(path.read_text(encoding="utf-8"))
        rules = [RuntimeRuleDefinition.model_validate(r) for r in raw]
        if version:
            rules = [r for r in rules if r.version == version]
        return rules

    def evaluate_plan(self, plan: NormalizedPlan, municipality: str) -> ComplianceResult:
        rules = self.load_ruleset(municipality)
        rule_results = [
            self.evaluator.evaluate(RuleContext(plan=plan, rule=rule)) for rule in rules
        ]
        return ComplianceResult(
            plan_id=plan.plan_id,
            ruleset_id=municipality.upper(),
            ruleset_version=rules[0].version if rules else None,
            rule_results=rule_results,
        )
