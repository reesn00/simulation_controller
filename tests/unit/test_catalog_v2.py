from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from simulate_serve.application.task_compiler import TaskCompiler
from simulate_serve.configuration.catalog_loader import CatalogLoader, CatalogValidationError
from simulate_serve.interaction.models import InteractionContext
from simulate_serve.interaction.prompt_builder import build_system_prompt


def _write_catalog(tmp_path: Path, task: dict, scenario: dict) -> tuple[Path, Path]:
    tasks_path = tmp_path / "tasks.yaml"
    scenarios_path = tmp_path / "scenarios.yaml"
    tasks_path.write_text(
        yaml.safe_dump({"schema_version": "2", "tasks": [task]}, allow_unicode=True),
        encoding="utf-8",
    )
    scenarios_path.write_text(
        yaml.safe_dump({"schema_version": "2", "scenarios": [scenario]}, allow_unicode=True),
        encoding="utf-8",
    )
    return tasks_path, scenarios_path


def test_v2_compiles_structured_contract_without_leaking_fixture(tmp_path: Path) -> None:
    task = {
        "task_id": "T1",
        "task_type": "verified_lookup",
        "dimension": "verification",
        "explain": "fixture isolation",
        "scenario": "verified_lookup",
        "initial_request": "请帮我验证两个候选链接",
        "intent": {
            "goal": "取得两个经过验证的候选结果",
            "context": ["用户希望结果可以直接使用"],
            "priorities": [
                {"priority": "required", "requirement": "至少两个链接"},
            ],
        },
        "test_fixture": {
            "kind": "scripted_executor",
            "description": "SECRET_FIXTURE_MARKER",
            "payload": {"first_response": "partial"},
        },
        "output_contract": {
            "format": "table",
            "required_fields": ["平台", "网址"],
            "min_results": 2,
            "count_unit": "table_rows",
            "min_urls": 2,
        },
        "acceptance_criteria": [
            {
                "criterion_id": "task.verified",
                "item": "结果经过验证",
                "description": "必须说明验证结果",
                "remediation": {
                    "owner": "executor",
                    "guidance": "请补充每个链接的验证结果",
                },
            }
        ],
        "reference": {
            "evaluation_notes": ["不接受只声称已验证但没有结果说明"],
        },
    }
    scenario = {
        "scenario_id": "verified_lookup",
        "name": "verified",
        "description": "verified lookup",
        "dialogue_policy": {
            "max_guide_rounds": 2,
            "max_gaps_per_turn": 1,
            "acknowledge_progress": True,
        },
        "guidance_policy": {"URL_MISSING": "请补充完整网址"},
    }
    tasks_path, scenarios_path = _write_catalog(tmp_path, task, scenario)

    compiled = TaskCompiler().compile(CatalogLoader().load(tasks_path, scenarios_path)).tasks[0]

    assert compiled.task_prompt == task["initial_request"]
    assert compiled.intent.goal == task["intent"]["goal"]
    assert compiled.test_fixture.description == "SECRET_FIXTURE_MARKER"
    assert compiled.interaction_policy.max_guide_rounds == 2
    assert compiled.interaction_policy.max_gaps_per_turn == 1
    assert compiled.criteria[0].remediation.guidance == "请补充每个链接的验证结果"
    assert {item.validator for item in compiled.criteria} >= {"semantic", "format", "fields", "count", "url_syntax"}

    prompt = build_system_prompt(InteractionContext(task=compiled))
    assert "取得两个经过验证的候选结果" in prompt
    assert "SECRET_FIXTURE_MARKER" not in prompt
    assert "first_response" not in prompt


@pytest.mark.parametrize(
    "task_update",
    [
        {"initial_request": None},
        {"intent": None},
        {"validation_rules": {"required_format": "text"}},
    ],
)
def test_v2_rejects_missing_contract_or_legacy_rules(tmp_path: Path, task_update: dict) -> None:
    task = {
        "task_id": "T1",
        "task_type": "x",
        "scenario": "base",
        "initial_request": "请处理",
        "intent": {"goal": "完成任务"},
        **task_update,
    }
    scenario = {"scenario_id": "base", "dialogue_policy": {}}
    tasks_path, scenarios_path = _write_catalog(tmp_path, task, scenario)

    with pytest.raises(CatalogValidationError):
        CatalogLoader().load(tasks_path, scenarios_path)


def test_builtin_v2_catalog_has_no_legacy_validation_rules(project_root: Path) -> None:
    bundle = CatalogLoader().load(
        project_root / "simulate_serve" / "config" / "tasks.yaml",
        project_root / "simulate_serve" / "config" / "scenarios.yaml",
    )
    assert bundle.schema_version == "2"
    assert len(bundle.tasks) == 58
    assert len(bundle.scenarios) >= 10
    assert all(task.scenario for task in bundle.tasks)
    assert all(task.validation_rules is None for task in bundle.tasks)
    assert all(task.initial_request and task.intent for task in bundle.tasks)
    assert not any(task.expected_reference for task in bundle.tasks)
    assert not any("（注：" in task.initial_request for task in bundle.tasks)

    compiled = TaskCompiler().compile(bundle).tasks
    fixture_markers = {
        task.task_id: task.test_fixture.description
        for task in compiled
        if task.test_fixture.description
    }
    assert fixture_markers
    for task in compiled:
        prompt = build_system_prompt(InteractionContext(task=task))
        if task.test_fixture.description:
            assert task.test_fixture.description not in prompt
            assert not any(
                item in prompt
                for item in task.test_fixture.payload
            )
        assert all(
            criterion.remediation.guidance
            for criterion in task.criteria
            if criterion.remediation.owner == "executor"
        )

    by_id = {task.task_id: task for task in compiled}
    assert "count" not in {item.validator for item in by_id["T019"].criteria}
    assert by_id["T019"].output_contract.format == "card"
    assert by_id["T019"].output_contract.min_urls == 2
    assert by_id["T055"].output_contract.format is None
