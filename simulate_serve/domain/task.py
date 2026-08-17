from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from .persona import PersonaSpec
from .provenance import SourceRef, TaskProvenance


class IntentPriority(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    priority: Literal["required", "preferred", "optional"] = "required"
    requirement: str


class TaskIntent(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    goal: str = ""
    context: tuple[str, ...] = ()
    priorities: tuple[IntentPriority, ...] = ()
    assumptions: tuple[str, ...] = ()
    uncertainties: tuple[str, ...] = ()


class RemediationSpec(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    owner: Literal["executor", "simulator", "environment", "user"] = "executor"
    guidance: str = ""
    retryable: bool = True


class TestFixtureSpec(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: str = ""
    description: str = ""
    payload: dict[str, Any] = Field(default_factory=dict)


class OutputContract(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    format: str | None = None
    required_fields: tuple[str, ...] = ()
    min_results: int = 0
    count_unit: str = "list_items"
    min_urls: int = 0


class FallbackBranch(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    trigger: str
    outcome: str
    guidance: str


class AcceptanceCriterion(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    criterion_id: str
    description: str
    required: bool = True
    validator: str = "semantic"
    parameters: dict[str, Any] = Field(default_factory=dict)
    required_capabilities: frozenset[str] = frozenset()
    remediation: RemediationSpec = RemediationSpec()
    source: SourceRef


class TaskConstraint(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    text: str
    kind: str = "required"


class InteractionPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    protocol: str = "以真实用户口吻自然交流，不暴露内部验证机制。"
    fallback_guidance: tuple[str, ...] = ()
    max_guide_rounds: int = 3
    max_gaps_per_turn: int = 2
    acknowledge_progress: bool = True
    preserve_satisfied_criteria: bool = True
    never_expose_internal_rules: bool = True
    guidance_by_reason: dict[str, str] = Field(default_factory=dict)
    pass_action: str = "thank_and_finish"
    blocked_action: str = "accept_honest_limitation"
    environment_error_action: str = "stop_without_blame_executor"


class ValidationPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    fail_closed: bool = True
    source_schema_version: str = "0"


class CompiledTask(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    task_id: str
    task_type: str
    dimension: str
    explain: str
    scenario_id: str | None = None
    task_prompt: str
    intent: TaskIntent = TaskIntent()
    test_fixture: TestFixtureSpec = TestFixtureSpec()
    output_contract: OutputContract = OutputContract()
    fallback_plan: tuple[FallbackBranch, ...] = ()
    persona: PersonaSpec
    criteria: tuple[AcceptanceCriterion, ...]
    constraints: tuple[TaskConstraint, ...] = ()
    excluded_platforms: tuple[str, ...] = ()
    interaction_policy: InteractionPolicy
    validation_policy: ValidationPolicy
    reference_text: str | None = None
    provenance: TaskProvenance
