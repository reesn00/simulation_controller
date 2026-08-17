from __future__ import annotations

from simulate_serve.bootstrap import render_validation_readiness, validation_readiness_gaps
from simulate_serve.domain.task import AcceptanceCriterion


class FakeRegistry:
    def __init__(self, available: bool = False):
        self.available = available

    def select_all(self, capabilities, task_type=""):
        return (object(),) if self.available else ()


def test_validation_readiness_reports_missing_judge_and_evidence(compiled_task, source_ref) -> None:
    task = compiled_task.model_copy(
        update={
            "criteria": (
                AcceptanceCriterion(
                    criterion_id="task.semantic",
                    description="semantic",
                    validator="semantic",
                    source=source_ref,
                ),
                AcceptanceCriterion(
                    criterion_id="task.evidence",
                    description="evidence",
                    validator="browser_evidence",
                    required_capabilities=frozenset({"browser.navigate", "browser.snapshot"}),
                    source=source_ref,
                ),
            )
        }
    )

    gaps = validation_readiness_gaps((task,), FakeRegistry(), judge_available=False)

    assert gaps == {
        task.task_id: ("browser.navigate", "browser.snapshot", "semantic_judge")
    }


def test_validation_readiness_is_empty_when_dependencies_are_available(compiled_task, source_ref) -> None:
    task = compiled_task.model_copy(
        update={
            "criteria": (
                AcceptanceCriterion(
                    criterion_id="task.evidence",
                    description="evidence",
                    validator="browser_evidence",
                    required_capabilities=frozenset({"browser.snapshot"}),
                    source=source_ref,
                ),
            )
        }
    )

    assert validation_readiness_gaps((task,), FakeRegistry(available=True), judge_available=True) == {}


def test_validation_readiness_render_groups_tasks_by_missing_capability(compiled_task) -> None:
    rendered = render_validation_readiness(
        (compiled_task,),
        {compiled_task.task_id: ("browser.snapshot", "semantic_judge")},
    )

    assert "ready=0 blocked=1" in rendered
    assert f"browser.snapshot: 1 task(s) [{compiled_task.task_id}]" in rendered
    assert f"semantic_judge: 1 task(s) [{compiled_task.task_id}]" in rendered
