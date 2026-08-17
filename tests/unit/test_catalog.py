from __future__ import annotations

from pathlib import Path

import pytest

from simulate_serve.application.task_compiler import TaskCompiler
from simulate_serve.configuration.catalog_loader import CatalogLoader, CatalogValidationError
from simulate_serve.task_manager import TaskManager


def test_builtin_catalog_compiles_all_tasks(project_root: Path) -> None:
    manager = TaskManager("tasks.yaml", "scenarios.yaml", config_dir=project_root / "simulate_serve" / "config")
    assert len(manager.compiled_tasks) == 58
    assert manager.diagnostics == ()
    assert all(task.criteria for task in manager.compiled_tasks)
    assert not any(
        item.parameters.get("legacy")
        for task in manager.compiled_tasks
        for item in task.criteria
    )


def test_special_dimensions_and_replace_policy(project_root: Path) -> None:
    manager = TaskManager("tasks.yaml", "scenarios.yaml", config_dir=project_root / "simulate_serve" / "config")
    tasks = {task.task_id: task for task in manager.compiled_tasks}
    assert tasks["F001"].dimension == "文件操作"
    assert tasks["T055"].dimension == "内容载体类型"
    assert "video.free" not in {item.criterion_id for item in tasks["T034"].criteria}
    assert "task.identify-nonexistent" in {item.criterion_id for item in tasks["T034"].criteria}
    assert "video.free" not in {item.criterion_id for item in tasks["T046"].criteria}


@pytest.mark.parametrize(
    "tasks,scenarios,code",
    [
        ([{"task_id": "T1", "task_type": "x", "task_prompt": "x", "unknown": 1}], [], "SCHEMA_INVALID"),
        ([{"task_id": "T1", "task_type": "x", "task_prompt": "x"}] * 2, [], "DUPLICATE_ID"),
        ([{"task_id": "T1", "task_type": "x", "task_prompt": "x", "scenario": "missing"}], [], "UNKNOWN_SCENARIO"),
    ],
)
def test_catalog_strict_failures(tmp_path: Path, tasks: list[dict], scenarios: list[dict], code: str) -> None:
    import yaml

    tasks_path = tmp_path / "tasks.yaml"
    scenarios_path = tmp_path / "scenarios.yaml"
    tasks_path.write_text(yaml.safe_dump({"schema_version": "1", "tasks": tasks}), encoding="utf-8")
    scenarios_path.write_text(yaml.safe_dump({"schema_version": "1", "scenarios": scenarios}), encoding="utf-8")
    with pytest.raises(CatalogValidationError) as caught:
        CatalogLoader().load(tasks_path, scenarios_path)
    assert any(item.code == code for item in caught.value.diagnostics)


def test_persona_field_merge_and_provenance(tmp_path: Path) -> None:
    import yaml

    tasks = {
        "schema_version": "1",
        "tasks": [
            {
                "task_id": "T1",
                "task_type": "x",
                "task_prompt": "x",
                "scenario": "base",
                "user_persona": {"tone": "direct"},
                "validation_rules": {"required_format": "text"},
            }
        ],
    }
    scenarios = {
        "schema_version": "1",
        "scenarios": [
            {
                "scenario_id": "base",
                "user_persona": {"role_description": "expert", "background": "domain"},
            }
        ],
    }
    task_path, scenario_path = tmp_path / "tasks.yaml", tmp_path / "scenarios.yaml"
    task_path.write_text(yaml.safe_dump(tasks), encoding="utf-8")
    scenario_path.write_text(yaml.safe_dump(scenarios), encoding="utf-8")
    result = TaskCompiler().compile(CatalogLoader().load(task_path, scenario_path)).tasks[0]
    assert result.persona.role_description == "expert"
    assert result.persona.tone == "direct"
    assert result.provenance.fields["persona.tone"].source_type == "task"
    assert result.provenance.fields["persona.background"].source_type == "scenario"


@pytest.mark.parametrize(
    "criteria",
    [
        [
            {"criterion_id": "same", "item": "first"},
            {"criterion_id": "same", "item": "second"},
        ],
        [{"criterion_id": "typo", "item": "value", "validator": "keywrod"}],
        [{"criterion_id": "browser", "item": "value", "validator": "browser_evidence"}],
    ],
)
def test_invalid_acceptance_criterion_contracts_fail_compilation(tmp_path: Path, criteria: list[dict]) -> None:
    import yaml

    tasks_path = tmp_path / "tasks.yaml"
    scenarios_path = tmp_path / "scenarios.yaml"
    tasks_path.write_text(
        yaml.safe_dump(
            {
                "schema_version": "1",
                "tasks": [
                    {
                        "task_id": "T1",
                        "task_type": "x",
                        "task_prompt": "x",
                        "acceptance_criteria": criteria,
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    scenarios_path.write_text(yaml.safe_dump({"schema_version": "1", "scenarios": []}), encoding="utf-8")

    bundle = CatalogLoader().load(tasks_path, scenarios_path)
    with pytest.raises(CatalogValidationError):
        TaskCompiler().compile(bundle)
