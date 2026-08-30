from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Iterable

from pydantic import BaseModel, ConfigDict

from simulate_serve.configuration.catalog_loader import CatalogBundle
from simulate_serve.configuration.catalog_schema import CriterionDocument, PersonaOverride, ScenarioDocument, TaskDocument
from simulate_serve.configuration.diagnostics import CatalogDiagnostic, DiagnosticSeverity
from simulate_serve.domain.provenance import SourceRef, TaskProvenance
from simulate_serve.domain.task import (
    AcceptanceCriterion,
    CompiledTask,
    FallbackBranch,
    IntentPriority,
    InteractionPolicy,
    OutputContract,
    PersonaSpec,
    RemediationSpec,
    TaskIntent,
    TaskConstraint,
    TestFixtureSpec,
    ValidationPolicy,
)
from simulate_serve.interaction.guidance_policy import normalize_verbosity

_TASK_ID_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_-]{0,63}$")
_SCENARIO_ID_RE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
_CRITERION_ID_RE = re.compile(r"^[a-z][a-z0-9_.-]{0,127}$")
_DETERMINISTIC_VALIDATORS = frozenset({"keyword", "format", "fields", "count", "url_syntax", "constraint"})
_EVIDENCE_VALIDATORS = frozenset({"browser_evidence", "tool_evidence"})
_KNOWN_VALIDATORS = _DETERMINISTIC_VALIDATORS | _EVIDENCE_VALIDATORS | {"semantic"}

# Injected when a scenario does not declare REQUIREMENT_UNMATCHED phrasing:
# the generic user-owned wording for semantic failures the judge could not
# map to a scenario-specific reason code. User-owned because it only points
# back at what the user already said, never at criterion text.
_DEFAULT_UNMATCHED_GUIDANCE = "你说的这个跟我一开始要的对不上，请对照我最开始提的要求，把缺的地方补齐。"


class CompileResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    tasks: tuple[CompiledTask, ...]
    diagnostics: tuple[CatalogDiagnostic, ...]


def _normalize_guidance(policy: dict[str, str | list[str]] | None) -> dict[str, tuple[str, ...]]:
    """Normalize scenario guidance_policy values into variant pools."""
    normalized: dict[str, tuple[str, ...]] = {}
    for key, value in (policy or {}).items():
        entries = value if isinstance(value, list) else [value]
        variants = tuple(text.strip() for text in entries if isinstance(text, str) and text.strip())
        if variants:
            normalized[key] = variants
    return normalized


class TaskCompiler:
    def __init__(self, max_guide_rounds: int = 3):
        self.max_guide_rounds = max_guide_rounds

    def compile(self, bundle: CatalogBundle) -> CompileResult:
        diagnostics = list(bundle.diagnostics)
        scenarios = {item.scenario_id: item for item in bundle.scenarios}
        for scenario in bundle.scenarios:
            if not _SCENARIO_ID_RE.fullmatch(scenario.scenario_id):
                diagnostics.append(self._error("SCENARIO_ID_INVALID", scenario.scenario_id, bundle.scenarios_path))

        compiled: list[CompiledTask] = []
        for document in bundle.tasks:
            if not _TASK_ID_RE.fullmatch(document.task_id):
                diagnostics.append(self._error("TASK_ID_INVALID", document.task_id, bundle.tasks_path))
                continue
            scenario = scenarios.get(document.scenario) if document.scenario else None
            compiled.append(self._compile_task(document, scenario, bundle.schema_version, diagnostics))

        errors = [d for d in diagnostics if d.severity is DiagnosticSeverity.ERROR]
        if errors:
            from simulate_serve.configuration.catalog_loader import CatalogValidationError

            raise CatalogValidationError(errors)
        return CompileResult(tasks=tuple(compiled), diagnostics=tuple(diagnostics))

    def _compile_task(
        self,
        task: TaskDocument,
        scenario: ScenarioDocument | None,
        schema_version: str,
        diagnostics: list[CatalogDiagnostic],
    ) -> CompiledTask:
        provenance: dict[str, SourceRef] = {}
        persona = self._merge_persona(task, scenario, provenance)
        criteria = self._merge_criteria(task, scenario, diagnostics)
        criteria.extend(self._compile_output_contract(task))
        criteria.extend(self._compile_legacy_rules(task))
        if not criteria:
            criteria.append(
                AcceptanceCriterion(
                    criterion_id=f"legacy.{task.task_id.lower()}.nonempty",
                    description="远端 Agent 必须返回非空结果",
                    validator="format",
                    parameters={"format": "text"},
                    source=self._source("derived", task.task_id, "validation_rules"),
                )
            )

        dimension = task.dimension
        if not dimension:
            dimension = "未分类"
            diagnostics.append(self._warning("DIMENSION_DERIVED", f"Task {task.task_id} has no dimension; using default", task.task_id, "dimension"))
        explain = task.explain or ""
        if not explain:
            diagnostics.append(self._warning("EXPLAIN_EMPTY", f"Task {task.task_id} has no explain text", task.task_id, "explain"))

        protocol, protocol_source = self._choose_scalar(
            task.interaction_protocol,
            scenario.interaction_protocol if scenario else None,
            "以真实用户口吻自然交流，不暴露内部验证机制。",
            task.task_id,
            scenario.scenario_id if scenario else "",
            "interaction_protocol",
        )
        provenance["interaction_policy.protocol"] = protocol_source
        fallback = (
            tuple(task.fallback_guidance)
            if task.fallback_guidance is not None
            else tuple(scenario.fallback_guidance or ()) if scenario else ()
        )
        provenance["interaction_policy.fallback_guidance"] = self._source(
            "task" if task.fallback_guidance is not None else "scenario" if scenario and scenario.fallback_guidance is not None else "default",
            task.task_id if task.fallback_guidance is not None else scenario.scenario_id if scenario else "global",
            "fallback_guidance",
        )
        constraints = self._dedupe((scenario.constraints or ()) if scenario else (), task.constraints or ())
        excluded = self._dedupe((scenario.excluded_platforms or ()) if scenario else (), task.excluded_platforms or ())
        if constraints:
            criteria.append(
                AcceptanceCriterion(
                    criterion_id=f"derived.{task.task_id.lower()}.constraints",
                    description="结果必须满足任务约束",
                    validator="semantic",
                    parameters={"constraints": constraints},
                    remediation=RemediationSpec(owner="executor", guidance="请重新检查并满足任务中的必选约束"),
                    source=self._source("derived", task.task_id, "constraints"),
                )
            )
        if excluded:
            criteria.append(
                AcceptanceCriterion(
                    criterion_id=f"derived.{task.task_id.lower()}.excluded-platforms",
                    description="结果不得推荐已排除的平台",
                    validator="constraint",
                    parameters={"excluded_platforms": excluded},
                    remediation=RemediationSpec(owner="executor", guidance="请移除被排除的平台，并更换为符合要求的来源"),
                    source=self._source("derived", task.task_id, "excluded_platforms"),
                )
            )
        if task.validation_prompt:
            diagnostics.append(self._warning("VALIDATION_PROMPT_DEPRECATED", "validation_prompt is ignored by the local pipeline", task.task_id, "validation_prompt"))
        criterion_ids = [item.criterion_id for item in criteria]
        for criterion_id in sorted({item for item in criterion_ids if criterion_ids.count(item) > 1}):
            diagnostics.append(self._error("CRITERION_ID_DUPLICATE", criterion_id, task.task_id, "criteria"))

        dialogue = scenario.dialogue_policy if scenario and scenario.dialogue_policy else None
        guidance = _normalize_guidance(scenario.guidance_policy if scenario else None)
        guidance.setdefault("REQUIREMENT_UNMATCHED", (_DEFAULT_UNMATCHED_GUIDANCE,))
        request = task.initial_request or task.task_prompt or ""
        intent = task.intent
        fixture = task.test_fixture
        output = task.output_contract
        reference_text = task.expected_reference
        if task.reference is not None:
            reference_text = json.dumps(task.reference.model_dump(mode="json"), ensure_ascii=False)

        return CompiledTask(
            task_id=task.task_id,
            task_type=task.task_type,
            dimension=dimension,
            explain=explain,
            scenario_id=task.scenario,
            offline_only=task.offline_only,
            task_prompt=request,
            intent=TaskIntent(
                goal=intent.goal if intent else request,
                context=tuple(intent.context) if intent else (),
                priorities=tuple(IntentPriority(priority=item.priority, requirement=item.requirement) for item in intent.priorities) if intent else (),
                assumptions=tuple(intent.assumptions) if intent else (),
                uncertainties=tuple(intent.uncertainties) if intent else (),
            ),
            test_fixture=TestFixtureSpec(
                kind=fixture.kind if fixture else "",
                description=fixture.description if fixture else "",
                payload=dict(fixture.payload) if fixture else {},
            ),
            output_contract=OutputContract(
                format=output.format if output else None,
                required_fields=tuple(output.required_fields) if output else (),
                min_results=output.min_results if output else 0,
                count_unit=output.count_unit if output else "list_items",
                min_urls=output.min_urls if output else 0,
            ),
            fallback_plan=tuple(
                FallbackBranch(trigger=item.trigger, outcome=item.outcome, guidance=item.guidance)
                for item in task.fallback_plan
            ),
            persona=persona,
            criteria=tuple(criteria),
            constraints=tuple(TaskConstraint(text=value) for value in constraints),
            excluded_platforms=tuple(excluded),
            interaction_policy=InteractionPolicy(
                protocol=protocol,
                fallback_guidance=fallback,
                max_guide_rounds=dialogue.max_guide_rounds if dialogue else self.max_guide_rounds,
                max_gaps_per_turn=dialogue.max_gaps_per_turn if dialogue else 2,
                acknowledge_progress=dialogue.acknowledge_progress if dialogue else True,
                preserve_satisfied_criteria=dialogue.preserve_satisfied_criteria if dialogue else True,
                never_expose_internal_rules=dialogue.never_expose_internal_rules if dialogue else True,
                guidance_by_reason=guidance,
                pass_action=dialogue.pass_action if dialogue else "thank_and_finish",
                blocked_action=dialogue.blocked_action if dialogue else "accept_honest_limitation",
                environment_error_action=dialogue.environment_error_action if dialogue else "stop_without_blame_executor",
            ),
            validation_policy=ValidationPolicy(source_schema_version=schema_version),
            reference_text=reference_text,
            provenance=TaskProvenance(fields=provenance),
        )

    def _merge_persona(
        self,
        task: TaskDocument,
        scenario: ScenarioDocument | None,
        provenance: dict[str, SourceRef],
    ) -> PersonaSpec:
        defaults = PersonaSpec()
        task_persona = task.user_persona or PersonaOverride()
        scenario_persona = scenario.user_persona if scenario and scenario.user_persona else PersonaOverride()
        values: dict[str, str] = {}
        for field in ("role_description", "background", "tone", "verbosity"):
            task_value = getattr(task_persona, field)
            scenario_value = getattr(scenario_persona, field)
            if task_value is not None:
                values[field] = task_value
                provenance[f"persona.{field}"] = self._source("task", task.task_id, f"user_persona.{field}")
            elif scenario_value is not None:
                values[field] = scenario_value
                provenance[f"persona.{field}"] = self._source("scenario", scenario.scenario_id, f"user_persona.{field}")
            else:
                values[field] = getattr(defaults, field)
                provenance[f"persona.{field}"] = self._source("default", "global", f"persona.{field}")
        values["verbosity"] = normalize_verbosity(values["verbosity"])
        return PersonaSpec(**values)

    def _merge_criteria(
        self,
        task: TaskDocument,
        scenario: ScenarioDocument | None,
        diagnostics: list[CatalogDiagnostic],
    ) -> list[AcceptanceCriterion]:
        mode = task.acceptance_policy.mode if task.acceptance_policy else "extend"
        merged: dict[str, AcceptanceCriterion] = {}
        if mode == "extend" and scenario:
            seen_scenario: set[str] = set()
            for index, item in enumerate(scenario.acceptance_criteria or ()):
                criterion = self._criterion(item, "scenario", scenario.scenario_id, index, diagnostics)
                if criterion.criterion_id in seen_scenario:
                    diagnostics.append(self._error("CRITERION_ID_DUPLICATE", criterion.criterion_id, scenario.scenario_id, f"acceptance_criteria[{index}]"))
                seen_scenario.add(criterion.criterion_id)
                merged[criterion.criterion_id] = criterion
        seen_task: set[str] = set()
        for index, item in enumerate(task.acceptance_criteria or ()):
            criterion = self._criterion(item, "task", task.task_id, index, diagnostics)
            if criterion.criterion_id in seen_task:
                diagnostics.append(self._error("CRITERION_ID_DUPLICATE", criterion.criterion_id, task.task_id, f"acceptance_criteria[{index}]"))
            seen_task.add(criterion.criterion_id)
            merged[criterion.criterion_id] = criterion
        return list(merged.values())

    def _criterion(
        self,
        document: CriterionDocument,
        source_type: str,
        source_id: str,
        index: int,
        diagnostics: list[CatalogDiagnostic],
    ) -> AcceptanceCriterion:
        criterion_id = document.criterion_id
        if not criterion_id:
            digest = hashlib.sha256(f"{source_type}:{source_id}:{document.item}".encode()).hexdigest()[:8]
            criterion_id = f"legacy-{digest}"
            diagnostics.append(self._warning("CRITERION_ID_DERIVED", f"Generated {criterion_id}", source_id, f"acceptance_criteria[{index}]"))
        if not _CRITERION_ID_RE.fullmatch(criterion_id):
            diagnostics.append(self._error("CRITERION_ID_INVALID", criterion_id, None, f"{source_id}.acceptance_criteria[{index}]"))
        validator = document.validator or "semantic"
        if validator not in _KNOWN_VALIDATORS:
            diagnostics.append(self._error("VALIDATOR_UNKNOWN", validator, source_id, f"acceptance_criteria[{index}].validator"))
        if validator in _EVIDENCE_VALIDATORS and not document.required_capabilities:
            diagnostics.append(self._error("EVIDENCE_CAPABILITY_MISSING", criterion_id, source_id, f"acceptance_criteria[{index}].required_capabilities"))
        if document.required_capabilities and validator not in _EVIDENCE_VALIDATORS:
            diagnostics.append(self._error("CAPABILITY_VALIDATOR_MISMATCH", criterion_id, source_id, f"acceptance_criteria[{index}]"))
        return AcceptanceCriterion(
            criterion_id=criterion_id,
            description=document.description or document.item,
            required=document.must_satisfy,
            validator=validator,
            parameters={"item": document.item, **document.parameters},
            required_capabilities=frozenset(document.required_capabilities),
            remediation=RemediationSpec(
                owner=document.remediation.owner if document.remediation else "executor",
                guidance=document.remediation.guidance if document.remediation else "",
                retryable=document.remediation.retryable if document.remediation else True,
            ),
            source=self._source(source_type, source_id, f"acceptance_criteria[{index}]"),
        )

    def _compile_output_contract(self, task: TaskDocument) -> list[AcceptanceCriterion]:
        contract = task.output_contract
        if contract is None:
            return []
        source = self._source("derived", task.task_id, "output_contract")
        prefix = f"contract.{task.task_id.lower()}"
        remediation = RemediationSpec(owner="executor", guidance="请按用户要求补全结果结构和内容")
        result: list[AcceptanceCriterion] = []
        if contract.format:
            result.append(
                AcceptanceCriterion(
                    criterion_id=f"{prefix}.format",
                    description=f"输出使用 {contract.format} 格式",
                    validator="format",
                    parameters={"format": contract.format},
                    remediation=remediation,
                    source=source,
                )
            )
        if contract.required_fields:
            result.append(
                AcceptanceCriterion(
                    criterion_id=f"{prefix}.fields",
                    description="输出包含必需字段",
                    validator="fields",
                    parameters={"fields": contract.required_fields},
                    remediation=RemediationSpec(owner="executor", guidance=f"请补充这些字段：{'、'.join(contract.required_fields)}"),
                    source=source,
                )
            )
        if contract.min_results:
            validator = "url_syntax" if contract.count_unit == "urls" else "count"
            parameters = {"min_items": contract.min_results}
            if validator == "count":
                parameters["unit"] = contract.count_unit
            result.append(
                AcceptanceCriterion(
                    criterion_id=f"{prefix}.min-results",
                    description=f"至少包含 {contract.min_results} 个结果",
                    validator=validator,
                    parameters=parameters,
                    remediation=RemediationSpec(owner="executor", guidance=f"结果数量还不足，请补充到至少 {contract.min_results} 个"),
                    source=source,
                )
            )
        if contract.min_urls and not (contract.count_unit == "urls" and contract.min_results == contract.min_urls):
            result.append(
                AcceptanceCriterion(
                    criterion_id=f"{prefix}.urls",
                    description=f"至少包含 {contract.min_urls} 个完整网址",
                    validator="url_syntax",
                    parameters={"min_items": contract.min_urls},
                    remediation=RemediationSpec(owner="executor", guidance=f"请补充至少 {contract.min_urls} 个可以直接打开的完整网址"),
                    source=source,
                )
            )
        return result

    def _compile_legacy_rules(self, task: TaskDocument) -> list[AcceptanceCriterion]:
        rules = task.validation_rules
        if rules is None:
            return []
        source = self._source("derived", task.task_id, "validation_rules")
        prefix = f"legacy.{task.task_id.lower()}"
        result: list[AcceptanceCriterion] = []
        if rules.keywords:
            result.append(AcceptanceCriterion(criterion_id=f"{prefix}.keywords", description="包含任务要求的关键信息", validator="keyword", parameters={"mode": "all", "keywords": rules.keywords}, source=source))
        if rules.required_format:
            result.append(AcceptanceCriterion(criterion_id=f"{prefix}.format", description=f"输出使用 {rules.required_format} 格式", validator="format", parameters={"format": rules.required_format}, source=source))
        if rules.required_fields:
            result.append(AcceptanceCriterion(criterion_id=f"{prefix}.fields", description="输出包含必需字段", validator="fields", parameters={"fields": rules.required_fields}, source=source))
        if rules.min_length:
            result.append(AcceptanceCriterion(criterion_id=f"{prefix}.min-length", description=f"输出长度不少于 {rules.min_length} 个字符", validator="count", parameters={"min_chars": rules.min_length, "legacy": True}, source=source))
        if rules.min_chars:
            result.append(AcceptanceCriterion(criterion_id=f"{prefix}.min-chars", description=f"输出长度不少于 {rules.min_chars} 个字符", validator="count", parameters={"min_chars": rules.min_chars}, source=source))
        if rules.min_items:
            result.append(AcceptanceCriterion(criterion_id=f"{prefix}.min-items", description=f"输出至少包含 {rules.min_items} 个结果项", validator="count", parameters={"min_items": rules.min_items}, source=source))
        if rules.semantic_requirements:
            result.append(AcceptanceCriterion(criterion_id=f"{prefix}.semantic", description=rules.semantic_requirements, validator="semantic", source=source))
        return result

    @staticmethod
    def _choose_scalar(task_value: str | None, scenario_value: str | None, default: str, task_id: str, scenario_id: str, path: str) -> tuple[str, SourceRef]:
        if task_value is not None:
            return task_value, TaskCompiler._source("task", task_id, path)
        if scenario_value is not None:
            return scenario_value, TaskCompiler._source("scenario", scenario_id, path)
        return default, TaskCompiler._source("default", "global", path)

    @staticmethod
    def _dedupe(*groups: Iterable[str]) -> list[str]:
        values: list[str] = []
        seen: set[str] = set()
        for group in groups:
            for raw in group:
                value = raw.strip()
                key = value.casefold()
                if value and key not in seen:
                    values.append(value)
                    seen.add(key)
        return values

    @staticmethod
    def _source(source_type: str, source_id: str, path: str) -> SourceRef:
        return SourceRef(source_type=source_type, source_id=source_id, path=path)

    @staticmethod
    def _warning(code: str, message: str, source: str, path: str) -> CatalogDiagnostic:
        return CatalogDiagnostic(severity=DiagnosticSeverity.WARNING, code=code, message=message, source=source, path=path)

    @staticmethod
    def _error(code: str, message: str, source: object, path: str = "") -> CatalogDiagnostic:
        return CatalogDiagnostic(severity=DiagnosticSeverity.ERROR, code=code, message=message, source=str(source or ""), path=path)
