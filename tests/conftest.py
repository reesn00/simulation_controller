from __future__ import annotations

import sys
from pathlib import Path

import pytest

# 让 ``gdr.config`` 包内 ``from config.settings import Settings`` 这种
# 顶级包导入可解析：gdr 在 uv workspace 下不一定被 install 到 site-packages，
# 但其 ``config/__init__.py`` 期望 ``config.settings`` 是顶级模块可导入。
# 把 ``gdr/`` 加进 sys.path 后即可。
_PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_PROJECT_ROOT / "gdr"))

from simulate_serve.domain.provenance import SourceRef, TaskProvenance
from simulate_serve.domain.task import AcceptanceCriterion, CompiledTask, InteractionPolicy, PersonaSpec, ValidationPolicy


@pytest.fixture
def source_ref() -> SourceRef:
    return SourceRef(source_type="task", source_id="TTEST", path="acceptance_criteria[0]")


@pytest.fixture
def compiled_task(source_ref: SourceRef) -> CompiledTask:
    return CompiledTask(
        task_id="TTEST",
        task_type="test",
        dimension="test",
        explain="test task",
        task_prompt="请给出结果",
        persona=PersonaSpec(),
        criteria=(
            AcceptanceCriterion(
                criterion_id="test.text",
                description="non-empty text",
                validator="format",
                parameters={"format": "text"},
                source=source_ref,
            ),
        ),
        interaction_policy=InteractionPolicy(max_guide_rounds=2),
        validation_policy=ValidationPolicy(),
        provenance=TaskProvenance(),
    )


@pytest.fixture
def project_root() -> Path:
    return Path(__file__).parents[1]
