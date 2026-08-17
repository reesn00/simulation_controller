from __future__ import annotations

from pathlib import Path

import pytest

from simulate_serve.domain.persona import PersonaSpec
from simulate_serve.domain.provenance import SourceRef, TaskProvenance
from simulate_serve.domain.task import AcceptanceCriterion, CompiledTask, InteractionPolicy, ValidationPolicy


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
