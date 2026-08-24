"""
Runtime rule contract.

Municipality-specific legal thresholds (e.g. "front setback >= 3m for
plots > 300 sq m") are NEVER hard-coded in Python. They are loaded at
runtime from data files (see data/runtime_rules/) and matched against a
NormalizedPlan through the interfaces below.

Phase 1 defines these interfaces only. The actual rule-loading and
evaluation logic (RASE compiler, deterministic RuleEngine) is built in
later phases by other teammates, against this contract.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any, Optional

from pydantic import BaseModel, Field

from backend.schemas.compliance import ComplianceResult, RuleResult
from backend.schemas.normalized_plan import NormalizedPlan


class RuntimeRuleDefinition(BaseModel):
    """
    Declarative, data-driven representation of a single municipal rule.

    This is what gets loaded from data/runtime_rules/*.json (or .yaml) —
    never authored as Python code. `condition` and `threshold` are kept
    generic (dict) in Phase 1 because the DSL/expression language for
    encoding conditions is a later-phase decision (RASE).
    """

    rule_id: str
    municipality: str = Field(..., description="e.g. 'BBMP'")
    citation: Optional[str] = None
    description: str
    applies_when: dict[str, Any] = Field(
        default_factory=dict, description="Applicability condition, schema owned by RASE"
    )
    threshold: dict[str, Any] = Field(
        default_factory=dict, description="Threshold/expression, schema owned by RASE"
    )
    version: str = "0.0.0"


class RuleContext(BaseModel):
    """Everything a single rule evaluation needs besides the plan itself."""

    plan: NormalizedPlan
    rule: RuntimeRuleDefinition
    extra: dict[str, Any] = Field(default_factory=dict)


class RuleEvaluator(ABC):
    """
    Interface for evaluating ONE RuntimeRuleDefinition against ONE
    NormalizedPlan. Implementations MUST be deterministic — no LLM calls
    are permitted inside a RuleEvaluator.
    """

    @abstractmethod
    def evaluate(self, context: RuleContext) -> RuleResult: ...


class RuleEngine(ABC):
    """
    Interface for the deterministic rule engine that evaluates an entire
    ruleset against a plan and produces a ComplianceResult.

    NOT implemented in Phase 1. Downstream teammates build the concrete
    engine against this interface.
    """

    @abstractmethod
    def load_ruleset(self, municipality: str, version: Optional[str] = None) -> list[RuntimeRuleDefinition]: ...

    @abstractmethod
    def evaluate_plan(self, plan: NormalizedPlan, municipality: str) -> ComplianceResult: ...
