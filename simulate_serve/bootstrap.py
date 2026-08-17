from __future__ import annotations

import logging
from collections.abc import Sequence
from dataclasses import dataclass

from simulate_serve.application.run_batch import BatchRunner
from simulate_serve.application.run_task import TaskRuntime
from simulate_serve.config import AppConfig
from simulate_serve.domain.task import CompiledTask
from simulate_serve.infrastructure.camel_model_factory import build_camel_model
from simulate_serve.infrastructure.json_run_repository import JsonRunRepository
from simulate_serve.infrastructure.qwenpaw_client import AsyncQwenPawExecutor
from simulate_serve.interaction.actor import CamelInteractionActor, DeterministicInteractionActor, InteractionActor
from simulate_serve.task_manager import TaskManager
from simulate_serve.tools.evidence_adapter import BrowserEvidenceCollector
from simulate_serve.tools.factories import create_default_registry
from simulate_serve.tools.registry import ToolRegistry
from simulate_serve.validation.pipeline import ValidationPipeline
from simulate_serve.validation.semantic_judge import CamelSemanticJudge

logger = logging.getLogger(__name__)


def validation_readiness_gaps(
    tasks: Sequence[CompiledTask],
    registry: ToolRegistry,
    *,
    judge_available: bool,
) -> dict[str, tuple[str, ...]]:
    """Return capabilities that make each compiled task unable to reach PASS."""
    gaps: dict[str, tuple[str, ...]] = {}
    for task in tasks:
        missing: set[str] = set()
        for criterion in task.criteria:
            if criterion.validator == "semantic" and not judge_available:
                missing.add("semantic_judge")
            if criterion.required_capabilities and not registry.select_all(
                criterion.required_capabilities,
                task.task_type,
            ):
                missing.update(criterion.required_capabilities)
        if missing:
            gaps[task.task_id] = tuple(sorted(missing))
    return gaps


def render_validation_readiness(
    tasks: Sequence[CompiledTask],
    gaps: dict[str, tuple[str, ...]],
) -> str:
    by_capability: dict[str, list[str]] = {}
    for task_id, capabilities in gaps.items():
        for capability in capabilities:
            by_capability.setdefault(capability, []).append(task_id)
    lines = [
        "Validation readiness",
        f"  tasks={len(tasks)} ready={len(tasks) - len(gaps)} blocked={len(gaps)}",
    ]
    for capability, task_ids in sorted(by_capability.items()):
        lines.append(
            f"  {capability}: {len(task_ids)} task(s) [{','.join(sorted(task_ids))}]"
        )
    return "\n".join(lines)


@dataclass
class ApplicationServices:
    config: AppConfig
    task_manager: TaskManager
    repository: JsonRunRepository
    registry: ToolRegistry
    executor: AsyncQwenPawExecutor
    batch_runner: BatchRunner

    async def close(self) -> None:
        await self.executor.close()
        await self.registry.close()


async def build_application(config: AppConfig) -> ApplicationServices:
    manager = TaskManager(
        config.tasks_file,
        config.scenarios_file,
        config_dir=config.config_dir,
        max_guide_rounds=config.max_guide_rounds,
    )
    repository = JsonRunRepository(config.output_dir)
    repository.mark_interrupted()
    registry = create_default_registry()
    await registry.start(config.tools)

    actor: InteractionActor
    judge = None
    try:
        actor = CamelInteractionActor(build_camel_model(config.model))
    except Exception as exc:
        logger.warning("CAMEL interaction actor unavailable; using deterministic actor: %s", exc)
        actor = DeterministicInteractionActor()
    if config.validation.semantic_judge_enabled:
        try:
            judge = CamelSemanticJudge(
                build_camel_model(config.model, temperature=0),
                timeout_seconds=config.validation.judge_timeout_seconds,
            )
        except Exception as exc:
            logger.warning("Semantic Judge unavailable; semantic criteria will be INCONCLUSIVE: %s", exc)
    readiness_gaps = validation_readiness_gaps(
        manager.compiled_tasks,
        registry,
        judge_available=judge is not None,
    )
    if readiness_gaps:
        summary = "; ".join(
            f"{task_id}={','.join(capabilities)}"
            for task_id, capabilities in sorted(readiness_gaps.items())
        )
        logger.warning(
            "Validation readiness: %d/%d tasks cannot currently reach PASS: %s",
            len(readiness_gaps),
            len(manager.compiled_tasks),
            summary,
        )
    validator = ValidationPipeline(judge=judge, evidence_collector=BrowserEvidenceCollector(registry, repository))
    executor = AsyncQwenPawExecutor(config.agent_endpoint)
    runtime = TaskRuntime(executor=executor, actor=actor, validator=validator, repository=repository)
    return ApplicationServices(
        config=config,
        task_manager=manager,
        repository=repository,
        registry=registry,
        executor=executor,
        batch_runner=BatchRunner(runtime),
    )
