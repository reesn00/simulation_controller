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
    assert len(bundle.tasks) == 98
    assert len(bundle.scenarios) == 10
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


def test_v2_strict_excluded_platforms_derives_constraint_criterion(tmp_path: Path) -> None:
    """默认 strict 行为: excluded_platforms 派生 ``derived.<task>.excluded-platforms`` 硬 FAIL 准则."""
    task = {
        "task_id": "T1",
        "task_type": "x",
        "scenario": "base",
        "initial_request": "请处理",
        "intent": {"goal": "完成任务"},
        "excluded_platforms": ["腾讯视频", "爱奇艺"],
    }
    scenario = {"scenario_id": "base", "dialogue_policy": {}}
    tasks_path, scenarios_path = _write_catalog(tmp_path, task, scenario)

    compiled = TaskCompiler().compile(
        CatalogLoader().load(tasks_path, scenarios_path)
    ).tasks[0]

    derived = [c for c in compiled.criteria if c.criterion_id.endswith(".excluded-platforms")]
    assert len(derived) == 1
    assert derived[0].validator == "constraint"
    assert list(derived[0].parameters["excluded_platforms"]) == ["腾讯视频", "爱奇艺"]
    # CompiledTask.excluded_platforms 仍写入 (tuple), prompt_builder 可读
    assert compiled.excluded_platforms == ("腾讯视频", "爱奇艺")


def test_v2_advisory_excluded_platforms_skips_constraint_criterion(tmp_path: Path) -> None:
    """advisory: 不派生硬 FAIL 准则, 但 prompt 引导信号保留."""
    task = {
        "task_id": "T1",
        "task_type": "x",
        "scenario": "base",
        "initial_request": "请处理",
        "intent": {"goal": "完成任务"},
        "excluded_platforms": ["腾讯视频", "爱奇艺"],
        "excluded_platforms_severity": "advisory",
    }
    scenario = {"scenario_id": "base", "dialogue_policy": {}}
    tasks_path, scenarios_path = _write_catalog(tmp_path, task, scenario)

    compiled = TaskCompiler().compile(
        CatalogLoader().load(tasks_path, scenarios_path)
    ).tasks[0]

    derived = [c for c in compiled.criteria if c.criterion_id.endswith(".excluded-platforms")]
    assert derived == []
    # 关键不变量: CompiledTask.excluded_platforms 仍写入 (供 prompt_builder 读)
    assert compiled.excluded_platforms == ("腾讯视频", "爱奇艺")
    # prompt 仍包含引导文字
    prompt = build_system_prompt(InteractionContext(task=compiled))
    assert "腾讯视频" in prompt
    assert "爱奇艺" in prompt


def test_v2_advisory_excluded_platforms_emits_warning_diagnostic(tmp_path: Path) -> None:
    """advisory 触发 EXCLUDED_PLATFORMS_ADVISORY 警告诊断."""
    from simulate_serve.configuration.diagnostics import DiagnosticSeverity

    task = {
        "task_id": "T1",
        "task_type": "x",
        "scenario": "base",
        "initial_request": "请处理",
        "intent": {"goal": "完成任务"},
        "excluded_platforms": ["哔哩哔哩"],
        "excluded_platforms_severity": "advisory",
    }
    scenario = {"scenario_id": "base", "dialogue_policy": {}}
    tasks_path, scenarios_path = _write_catalog(tmp_path, task, scenario)

    result = TaskCompiler().compile(CatalogLoader().load(tasks_path, scenarios_path))
    warnings = [d for d in result.diagnostics if d.code == "EXCLUDED_PLATFORMS_ADVISORY"]
    assert len(warnings) == 1
    assert warnings[0].severity is DiagnosticSeverity.WARNING
    assert warnings[0].source == "T1"
    assert warnings[0].path == "excluded_platforms_severity"


def test_v2_severity_resolution_task_overrides_scenario(tmp_path: Path) -> None:
    """task 级 severity 覆盖 scenario 级; scenario 级 fallback 生效."""
    base_task = {
        "task_id": "T1",
        "task_type": "x",
        "scenario": "base",
        "initial_request": "请处理",
        "intent": {"goal": "完成任务"},
        "excluded_platforms": ["腾讯视频"],
    }

    # case 1: task=advisory 覆盖 scenario=strict
    scenario_strict = {
        "scenario_id": "base",
        "dialogue_policy": {},
        "excluded_platforms_severity": "strict",
    }
    task_adv = {**base_task, "excluded_platforms_severity": "advisory"}
    tasks_path, scenarios_path = _write_catalog(tmp_path, task_adv, scenario_strict)
    compiled = TaskCompiler().compile(
        CatalogLoader().load(tasks_path, scenarios_path)
    ).tasks[0]
    assert not [c for c in compiled.criteria if c.criterion_id.endswith(".excluded-platforms")]

    # case 2: task 未设, scenario=advisory 兜底
    task_default = dict(base_task)
    scenario_adv = {
        "scenario_id": "base",
        "dialogue_policy": {},
        "excluded_platforms_severity": "advisory",
    }
    tasks_path, scenarios_path = _write_catalog(tmp_path, task_default, scenario_adv)
    compiled = TaskCompiler().compile(
        CatalogLoader().load(tasks_path, scenarios_path)
    ).tasks[0]
    assert not [c for c in compiled.criteria if c.criterion_id.endswith(".excluded-platforms")]

    # case 3: task=strict 覆盖 scenario=advisory
    task_strict = {**base_task, "excluded_platforms_severity": "strict"}
    tasks_path, scenarios_path = _write_catalog(tmp_path, task_strict, scenario_adv)
    compiled = TaskCompiler().compile(
        CatalogLoader().load(tasks_path, scenarios_path)
    ).tasks[0]
    assert [c for c in compiled.criteria if c.criterion_id.endswith(".excluded-platforms")]


def test_v2_rejects_invalid_severity_value(tmp_path: Path) -> None:
    """非法 severity 值被 schema 拒绝."""
    task = {
        "task_id": "T1",
        "task_type": "x",
        "scenario": "base",
        "initial_request": "请处理",
        "intent": {"goal": "完成任务"},
        "excluded_platforms": ["腾讯视频"],
        "excluded_platforms_severity": "soft",  # 非 Literal 取值
    }
    scenario = {"scenario_id": "base", "dialogue_policy": {}}
    tasks_path, scenarios_path = _write_catalog(tmp_path, task, scenario)

    with pytest.raises(CatalogValidationError):
        CatalogLoader().load(tasks_path, scenarios_path)


def test_v2_builtin_catalog_excluded_platforms_are_advisory(project_root: Path) -> None:
    """内置 fixture 全局验证: 67 个含 excluded_platforms 的 task 全部标 advisory,
    且都不派生 ``derived.<task>.excluded-platforms`` 准则."""
    bundle = CatalogLoader().load(
        project_root / "simulate_serve" / "config" / "tasks.yaml",
        project_root / "simulate_serve" / "config" / "scenarios.yaml",
    )
    compiled = TaskCompiler().compile(bundle)
    tasks_with_excluded = [t for t in bundle.tasks if t.excluded_platforms]
    assert len(tasks_with_excluded) >= 60  # 防止 fixture 退化
    assert all(t.excluded_platforms_severity == "advisory" for t in tasks_with_excluded)

    advisory_diag_count = sum(
        1 for d in compiled.diagnostics if d.code == "EXCLUDED_PLATFORMS_ADVISORY"
    )
    assert advisory_diag_count == len(tasks_with_excluded)

    compiled_by_id = {t.task_id: t for t in compiled.tasks}
    for t in tasks_with_excluded:
        derived = [
            c for c in compiled_by_id[t.task_id].criteria
            if c.criterion_id.endswith(".excluded-platforms")
        ]
        assert derived == [], f"{t.task_id} 应不派生 excluded-platforms 准则"
        # 但 CompiledTask.excluded_platforms 必须写入, prompt 才能读到
        assert compiled_by_id[t.task_id].excluded_platforms
