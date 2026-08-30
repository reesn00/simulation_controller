"""Contract tests for the built-in scenario catalog.

These tests pin down invariants that the validator does not (yet) enforce.
They are intentionally simple structural checks; semantic checks live in the
runtime/validation layers. Adding a new built-in scenario should keep these
green by design, not by accident.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from simulate_serve.configuration.catalog_schema import ScenarioDocument


# Mapping of evidence validator -> capabilities the local tooling must expose.
# A criterion carrying one of these validators must declare a superset of the
# listed capabilities; otherwise the readiness check would silently fail-open
# on a tool the validator actually depends on.
EVIDENCE_VALIDATOR_REQUIREMENTS: dict[str, frozenset[str]] = {
    "browser_evidence": frozenset({"browser.navigate", "browser.snapshot"}),
    "tool_evidence": frozenset({"filesystem.inspect"}),
}


def test_every_scenario_declares_verbosity(scenarios: list[ScenarioDocument]) -> None:
    for scenario in scenarios:
        persona = scenario.user_persona
        assert persona is not None, f"{scenario.scenario_id} has no user_persona"
        assert persona.verbosity, f"{scenario.scenario_id} user_persona.verbosity is missing"


def test_fallback_guidance_is_non_empty(scenarios: list[ScenarioDocument]) -> None:
    for scenario in scenarios:
        guidance = scenario.fallback_guidance or []
        assert guidance, f"{scenario.scenario_id} fallback_guidance is empty"
        assert all(isinstance(line, str) and line.strip() for line in guidance), (
            f"{scenario.scenario_id} fallback_guidance has blank or non-string entries"
        )


@pytest.mark.parametrize(
    "validator,required",
    list(EVIDENCE_VALIDATOR_REQUIREMENTS.items()),
)
def test_evidence_validator_declares_required_capabilities(
    scenarios: list[ScenarioDocument], validator: str, required: frozenset[str]
) -> None:
    offenders: list[str] = []
    for scenario in scenarios:
        for criterion in scenario.acceptance_criteria or []:
            if criterion.validator != validator:
                continue
            actual = set(criterion.required_capabilities or [])
            missing = required - actual
            if missing:
                offenders.append(
                    f"{scenario.scenario_id}.{criterion.criterion_id} uses {validator} but missing {sorted(missing)}"
                )
    assert not offenders, "Evidence validator capability contract violated:\n" + "\n".join(offenders)


def test_no_decline_check_marks_decline_criterion_non_retryable(scenarios: list[ScenarioDocument]) -> None:
    """Scenarios that opt out of decline detection must declare at least one
    criterion whose success is the agent's correct refusal/clarification, and
    such a criterion must not be retried after a correct decline."""
    offenders: list[str] = []
    for scenario in scenarios:
        policy = scenario.dialogue_policy
        if policy is None or policy.blocked_action != "no_decline_check":
            continue
        non_retryable = [
            c
            for c in scenario.acceptance_criteria or []
            if c.remediation is not None and c.remediation.retryable is False
        ]
        if not non_retryable:
            offenders.append(
                f"{scenario.scenario_id} uses no_decline_check but has no non-retryable criterion"
            )
    assert not offenders, "no_decline_check contract violated:\n" + "\n".join(offenders)


def test_no_decline_check_scenarios_have_explicit_pass_action(scenarios: list[ScenarioDocument]) -> None:
    """Scenarios that opt out of decline detection should declare a
    pass_action other than the default 'thank_and_finish' so that the closing
    turn reflects the decline semantics. Today the runtime skips closing
    turns for unknown actions; once a richer set of closing actions is wired
    in, this test will already be green."""
    offenders: list[str] = []
    for scenario in scenarios:
        policy = scenario.dialogue_policy
        if policy is None or policy.blocked_action != "no_decline_check":
            continue
        if policy.pass_action == "thank_and_finish":
            offenders.append(
                f"{scenario.scenario_id} uses no_decline_check but pass_action is default 'thank_and_finish'"
            )
    assert not offenders, "no_decline_check pass_action contract violated:\n" + "\n".join(offenders)


def test_guidance_policy_keys_are_upper_snake_case(scenarios: list[ScenarioDocument]) -> None:
    """Stable reason_code contract: keys must match UPPER_SNAKE_CASE so the
    simulator can reference them by string without locale/layout surprises."""
    import re

    pattern = re.compile(r"^[A-Z][A-Z0-9_]*$")
    offenders: list[str] = []
    for scenario in scenarios:
        for key in (scenario.guidance_policy or {}).keys():
            if not pattern.match(key):
                offenders.append(f"{scenario.scenario_id} guidance_policy key '{key}' is not UPPER_SNAKE_CASE")
    assert not offenders, "guidance_policy key contract violated:\n" + "\n".join(offenders)


def test_every_scenario_declares_requirement_unmatched(scenarios: list[ScenarioDocument]) -> None:
    """Semantic FAILs the judge cannot map to a scenario-specific code fall
    back to REQUIREMENT_UNMATCHED; every scenario must phrase it so the
    simulated user always speaks from the scenario vocabulary."""
    offenders = [
        scenario.scenario_id
        for scenario in scenarios
        if "REQUIREMENT_UNMATCHED" not in (scenario.guidance_policy or {})
    ]
    assert not offenders, "scenarios without a REQUIREMENT_UNMATCHED phrasing:\n" + "\n".join(offenders)


def test_guidance_policy_values_are_variant_pools(scenarios: list[ScenarioDocument]) -> None:
    """Every reason code needs at least two phrasings so the deterministic
    fallback can rotate across rounds/runs without repeating the user."""
    offenders: list[str] = []
    for scenario in scenarios:
        for key, value in (scenario.guidance_policy or {}).items():
            if not isinstance(value, list):
                offenders.append(f"{scenario.scenario_id}.{key} must be a variant list")
                continue
            if len(value) < 2:
                offenders.append(f"{scenario.scenario_id}.{key} needs at least 2 variants")
            if any(not isinstance(entry, str) or not entry.strip() for entry in value):
                offenders.append(f"{scenario.scenario_id}.{key} has blank or non-string variants")
    assert not offenders, "guidance_policy variant-pool contract violated:\n" + "\n".join(offenders)


def test_remediation_guidance_has_no_template_prefix(scenarios: list[ScenarioDocument], tasks: list[TaskDocument]) -> None:
    """Remediation guidance is internal repair semantics and must never read
    like a canned user line; the mechanical template prefix is banned."""
    prefix = "请补充或修正这一点"
    offenders: list[str] = []
    for scenario in scenarios:
        for criterion in scenario.acceptance_criteria or []:
            guidance = criterion.remediation.guidance if criterion.remediation else ""
            if prefix in guidance:
                offenders.append(f"{scenario.scenario_id}.{criterion.criterion_id}")
    for task in tasks:
        for criterion in task.acceptance_criteria or []:
            guidance = criterion.remediation.guidance if criterion.remediation else ""
            if prefix in guidance:
                offenders.append(f"{task.task_id}.{criterion.criterion_id}")
    assert not offenders, "remediation guidance still uses the mechanical template prefix:\n" + "\n".join(offenders)
