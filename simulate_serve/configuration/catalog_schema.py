from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class StrictDocument(BaseModel):
    model_config = ConfigDict(extra="forbid")


class PersonaOverride(StrictDocument):
    role_description: str | None = None
    background: str | None = None
    tone: str | None = None
    verbosity: Literal["concise", "moderate", "detailed"] | None = None


class RemediationDocument(StrictDocument):
    owner: Literal["executor", "simulator", "environment", "user"] = "executor"
    guidance: str = ""
    retryable: bool = True


class IntentPriorityDocument(StrictDocument):
    priority: Literal["required", "preferred", "optional"] = "required"
    requirement: str


class IntentDocument(StrictDocument):
    goal: str
    context: list[str] = Field(default_factory=list)
    priorities: list[IntentPriorityDocument] = Field(default_factory=list)
    assumptions: list[str] = Field(default_factory=list)
    uncertainties: list[str] = Field(default_factory=list)


class TestFixtureDocument(StrictDocument):
    kind: str
    description: str = ""
    payload: dict[str, Any] = Field(default_factory=dict)


class OutputContractDocument(StrictDocument):
    format: Literal["json", "list", "table", "card", "text"] | None = None
    required_fields: list[str] = Field(default_factory=list)
    min_results: int = Field(default=0, ge=0)
    count_unit: Literal["list_items", "table_rows", "urls"] = "list_items"
    min_urls: int = Field(default=0, ge=0)


class ReferenceDocument(StrictDocument):
    as_of: str | None = None
    known_facts: list[str] = Field(default_factory=list)
    evaluation_notes: list[str] = Field(default_factory=list)
    acceptable_alternatives: list[str] = Field(default_factory=list)
    forbidden_assumptions: list[str] = Field(default_factory=list)


class FallbackBranchDocument(StrictDocument):
    trigger: str
    outcome: Literal["partial_success", "blocked", "clarification_required"]
    guidance: str


class DialoguePolicyDocument(StrictDocument):
    max_guide_rounds: int = Field(default=3, ge=0, le=10)
    max_gaps_per_turn: int = Field(default=2, ge=1, le=10)
    acknowledge_progress: bool = True
    preserve_satisfied_criteria: bool = True
    never_expose_internal_rules: bool = True
    pass_action: str = "thank_and_finish"
    # accept_honest_limitation: decline phrasing after a failed validation ends
    # the run as AGENT_DECLINED. no_decline_check: refusal/clarification/honest
    # degradation is the task goal itself, so decline detection is disabled.
    blocked_action: Literal["accept_honest_limitation", "no_decline_check"] = "accept_honest_limitation"
    environment_error_action: str = "stop_without_blame_executor"


class CriterionDocument(StrictDocument):
    criterion_id: str | None = None
    item: str
    description: str = ""
    must_satisfy: bool = True
    validator: str | None = None
    parameters: dict[str, Any] = Field(default_factory=dict)
    required_capabilities: list[str] = Field(default_factory=list)
    remediation: RemediationDocument | None = None

    @field_validator("criterion_id", "item", mode="before")
    @classmethod
    def strip_required_strings(cls, value: object) -> object:
        return value.strip() if isinstance(value, str) else value


class AcceptancePolicyDocument(StrictDocument):
    mode: Literal["extend", "replace"] = "extend"


class LegacyValidationRulesDocument(StrictDocument):
    keywords: list[str] = Field(default_factory=list)
    required_format: Literal["json", "list", "table", "card", "text"] | None = None
    required_fields: list[str] = Field(default_factory=list)
    min_length: int = Field(default=0, ge=0)
    min_chars: int = Field(default=0, ge=0)
    min_items: int = Field(default=0, ge=0)
    semantic_requirements: str = ""

    @model_validator(mode="after")
    def count_semantics_are_unambiguous(self) -> "LegacyValidationRulesDocument":
        configured = sum(value > 0 for value in (self.min_length, self.min_chars, self.min_items))
        if configured > 1:
            raise ValueError("Only one of min_length, min_chars and min_items may be configured")
        return self


class ScenarioDocument(StrictDocument):
    scenario_id: str
    name: str = ""
    description: str = ""
    user_persona: PersonaOverride | None = None
    acceptance_criteria: list[CriterionDocument] | None = None
    constraints: list[str] | None = None
    excluded_platforms: list[str] | None = None
    interaction_protocol: str | None = None
    fallback_guidance: list[str] | None = None
    dialogue_policy: DialoguePolicyDocument | None = None
    # reason_code -> one phrasing or a variant pool. Pools must carry at least
    # two entries (contract test) so the simulated user can rotate phrasing
    # across rounds and runs without repeating herself.
    guidance_policy: dict[str, str | list[str]] = Field(default_factory=dict)


class TaskDocument(StrictDocument):
    task_id: str
    task_type: str
    # Excluded from live batches by default; fixtures-driven anomaly tasks and
    # tasks whose evidence capability has no local provider go here.
    offline_only: bool = False
    dimension: str | None = None
    explain: str | None = None
    scenario: str | None = None
    task_prompt: str | None = None
    initial_request: str | None = None
    intent: IntentDocument | None = None
    test_fixture: TestFixtureDocument | None = None
    output_contract: OutputContractDocument | None = None
    fallback_plan: list[FallbackBranchDocument] = Field(default_factory=list)
    reference: ReferenceDocument | None = None
    user_persona: PersonaOverride | None = None
    acceptance_policy: AcceptancePolicyDocument | None = None
    acceptance_criteria: list[CriterionDocument] | None = None
    constraints: list[str] | None = None
    excluded_platforms: list[str] | None = None
    interaction_protocol: str | None = None
    fallback_guidance: list[str] | None = None
    validation_rules: LegacyValidationRulesDocument | None = None
    expected_reference: str | None = None
    validation_prompt: str | None = None

    @field_validator("task_id", "task_type", "task_prompt", "initial_request", mode="before")
    @classmethod
    def strip_required_strings(cls, value: object) -> object:
        return value.strip() if isinstance(value, str) else value

    @model_validator(mode="after")
    def request_is_present(self) -> "TaskDocument":
        if not self.initial_request and not self.task_prompt:
            raise ValueError("Either initial_request or task_prompt must be configured")
        return self


class TaskCatalogDocument(StrictDocument):
    schema_version: str = "1"
    tasks: list[TaskDocument]


class ScenarioCatalogDocument(StrictDocument):
    schema_version: str = "1"
    scenarios: list[ScenarioDocument]
