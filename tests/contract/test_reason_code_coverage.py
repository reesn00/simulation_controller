"""Contract tests binding scenario guidance_policy keys to the reason_code registry.

A guidance_policy key is only useful if some layer can actually emit it as a
`reason_code` and the guidance lookup can resolve it back to the scenario's
customised phrasing. These tests fail when a key is added to a scenario
without a matching emission path, or when the registry drifts away from the
catalog (renamed scenarios, criteria, or pass_action strings).

Emission tracks (see simulate_serve/validation/reason_codes.py):

- DETERMINISTIC: emitted by a validator / pipeline stage. Reachable only in
  scenarios that carry at least one evidence-validator criterion (browser or
  tool), because the deterministic validators only run for such criteria.
- SEMANTIC: emitted by the SemanticJudge; reachable in any scenario.
- CLOSING: injected when the run terminates with the mapped closing action;
  the (scenario_id, action, criterion_id) triple must exist in the catalog.
"""
from __future__ import annotations

from simulate_serve.configuration.catalog_schema import ScenarioDocument
from simulate_serve.validation.reason_codes import (
    CLOSING_REASON_CODES,
    DETERMINISTIC_REASON_CODES,
    SEMANTIC_REASON_CODE_HINTS,
    is_closing,
)

# Evidence validators run through the EvidenceCollector path; every other
# validator (or none) routes the criterion to the semantic judge.
_EVIDENCE_VALIDATORS = frozenset({"browser_evidence", "tool_evidence"})


def _registry_codes() -> set[str]:
    return (
        set(DETERMINISTIC_REASON_CODES)
        | set(SEMANTIC_REASON_CODE_HINTS)
        | {
            code
            for actions in CLOSING_REASON_CODES.values()
            for _, code in actions.values()
        }
    )


def test_every_guidance_key_is_registered(scenarios: list[ScenarioDocument]) -> None:
    registered = _registry_codes()
    offenders = [
        f"{scenario.scenario_id} guidance key '{key}' is not registered in reason_codes.py"
        for scenario in scenarios
        for key in (scenario.guidance_policy or {})
        if key not in registered
    ]
    assert not offenders, "guidance key without registry entry:\n" + "\n".join(offenders)


def test_every_guidance_key_has_a_live_emission_path(scenarios: list[ScenarioDocument]) -> None:
    """A key is live when the judge can emit it (semantic/closing track), or
    when a deterministic emitter exists AND the scenario has evidence
    criteria that route through the deterministic validators. A deterministic
    only key inside a semantic-only scenario is unreachable dead config."""
    offenders: list[str] = []
    for scenario in scenarios:
        has_evidence = any(
            criterion.validator in _EVIDENCE_VALIDATORS
            for criterion in scenario.acceptance_criteria or []
        )
        for key in scenario.guidance_policy or {}:
            if key in SEMANTIC_REASON_CODE_HINTS or is_closing(key):
                continue
            if key in DETERMINISTIC_REASON_CODES and has_evidence:
                continue
            offenders.append(
                f"{scenario.scenario_id} guidance key '{key}' has no live emission path"
                + ("" if has_evidence else " (scenario has no evidence-validator criteria)")
            )
    assert not offenders, "unreachable guidance keys:\n" + "\n".join(offenders)


def test_closing_targets_exist_in_catalog(scenarios: list[ScenarioDocument]) -> None:
    by_id = {scenario.scenario_id: scenario for scenario in scenarios}
    for scenario_id, actions in CLOSING_REASON_CODES.items():
        scenario = by_id.get(scenario_id)
        assert scenario is not None, f"closing map references unknown scenario '{scenario_id}'"
        criterion_ids = {criterion.criterion_id for criterion in scenario.acceptance_criteria or []}
        for action, (criterion_id, code) in actions.items():
            assert criterion_id in criterion_ids, (
                f"closing map for '{scenario_id}' references unknown criterion '{criterion_id}'"
            )
            assert code in (scenario.guidance_policy or {}), (
                f"closing map for '{scenario_id}' emits '{code}' which the scenario does not declare"
            )
            assert scenario.dialogue_policy is not None, f"'{scenario_id}' has no dialogue_policy"
            assert scenario.dialogue_policy.pass_action == action, (
                f"closing map for '{scenario_id}' is keyed on action '{action}' "
                f"but the scenario pass_action is '{scenario.dialogue_policy.pass_action}'"
            )


def test_semantic_hints_are_referenced_by_scenarios(scenarios: list[ScenarioDocument]) -> None:
    """A hint nobody references can never reach a judge payload; keep the
    registry free of dead entries."""
    referenced = {
        key for scenario in scenarios for key in (scenario.guidance_policy or {})
    }
    dead = set(SEMANTIC_REASON_CODE_HINTS) - referenced
    assert not dead, f"semantic hints not referenced by any scenario: {sorted(dead)}"
