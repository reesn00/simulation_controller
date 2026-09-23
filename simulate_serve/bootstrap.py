from __future__ import annotations

import logging
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

from simulate_serve.application.run_batch import BatchRunner
from simulate_serve.application.run_task import TaskRuntime
from simulate_serve.config import AppConfig
from simulate_serve.domain.task import CompiledTask
from simulate_serve.infrastructure.camel_model_factory import build_camel_model
from simulate_serve.infrastructure.json_run_repository import JsonRunRepository
from simulate_serve.infrastructure.qwenpaw_client import AsyncQwenPawExecutor
from simulate_serve.infrastructure.trajectory_archiver import (
    QwenPawTrajectoryArchiver,
    default_qwenpaw_trajectory_dir,
)
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


def filter_unready_tasks(
    tasks: Sequence[CompiledTask],
    gaps: dict[str, tuple[str, ...]],
) -> tuple[list[CompiledTask], list[tuple[str, tuple[str, ...]]]]:
    """Split tasks into runnable and readiness-blocked ``(task_id, missing)`` pairs."""
    blocked = [(task.task_id, gaps[task.task_id]) for task in tasks if task.task_id in gaps]
    runnable = [task for task in tasks if task.task_id not in gaps]
    return runnable, blocked


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
    # task_id -> capabilities that keep the task from reaching PASS with the
    # currently started local tools/judge; empty when everything is ready.
    readiness_gaps: dict[str, tuple[str, ...]] = field(default_factory=dict)
    # Langfuse client (process-local singleton from PR 1). None when the
    # module is disabled, missing credentials, or SDK import failed — the
    # archiver's ``_emit_trail`` already no-ops in that case, but we keep
    # the reference here so the close hook can flush / shutdown the
    # background thread safely at process exit.
    langfuse: Any | None = None

    async def close(self) -> None:
        await self.executor.close()
        await self.registry.close()
        # Langfuse shutdown: flush pending spans and reset the
        # process-local singleton. Any failure is logged at WARNING — the
        # close path must not raise because callers (CLI / orchestration
        # worker) treat close() as best-effort cleanup.
        try:
            from simulate_serve.observability.langfuse_client import shutdown as _lf_shutdown
            _lf_shutdown()
        except Exception:
            logger.warning("Langfuse shutdown raised; ignored", exc_info=True)


async def build_application(config: AppConfig) -> ApplicationServices:
    manager = TaskManager(
        config.tasks_file,
        config.scenarios_file,
        config_dir=config.config_dir,
        max_guide_rounds=config.max_guide_rounds,
    )
    repository = JsonRunRepository(config.output_dir)
    repository.mark_interrupted()
    registry = create_default_registry(config.model)
    await registry.start(config.tools)

    actor: InteractionActor
    judge = None
    try:
        actor = CamelInteractionActor(
            build_camel_model(config.model),
            timeout_seconds=config.interaction.actor_timeout_seconds,
        )
    except Exception as exc:
        logger.warning("CAMEL interaction actor unavailable; using deterministic actor: %s", exc)
        actor = DeterministicInteractionActor()
    if not config.validation.enabled:
        logger.warning(
            "Validation disabled (record-only mode): runs execute remotely and archive "
            "trajectories, but always end INCONCLUSIVE/VALIDATION_DISABLED — never SUCCESS."
        )
        readiness_gaps: dict[str, tuple[str, ...]] = {}
    else:
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
    validator = None
    if config.validation.enabled:
        validator = ValidationPipeline(judge=judge, evidence_collector=BrowserEvidenceCollector(registry, repository))
    executor = AsyncQwenPawExecutor(config.agent_endpoint)
    trajectory_archiver = None
    if config.agent_endpoint.trajectory_capture_enabled:
        # PR 2: forward ``langfuse_config`` to the archiver so every copy
        # round-trips through ``_emit_trail``. ``None`` keeps the
        # pre-PR-2 behavior (no Langfuse activity at all).
        trajectory_archiver = QwenPawTrajectoryArchiver(
            config.output_dir,
            user_id=config.agent_endpoint.user_id,
            source_dir=config.agent_endpoint.trajectory_source_dir or None,
            langfuse_config=config.langfuse,
        )
        if config.agent_endpoint.trajectory_source_dir:
            logger.info("Trajectory capture source: %s", config.agent_endpoint.trajectory_source_dir)
        else:
            logger.info(
                "Trajectory capture source (default): %s",
                default_qwenpaw_trajectory_dir(config.agent_endpoint.execution_agent_id),
            )
    runtime = TaskRuntime(
        executor=executor,
        actor=actor,
        validator=validator,
        repository=repository,
        trajectory_archiver=trajectory_archiver,
    )
    # PR 2: instantiate the process-local Langfuse client once at start-up
    # so the close hook can flush it. ``get_client`` returns ``None`` for
    # disabled configs / missing credentials / SDK failures — we propagate
    # that to ``ApplicationServices.langfuse`` verbatim.
    langfuse_client: Any | None = None
    if config.langfuse and getattr(config.langfuse, "enabled", False):
        from simulate_serve.observability.langfuse_client import get_client

        langfuse_client = get_client(config.langfuse)
    return ApplicationServices(
        config=config,
        task_manager=manager,
        repository=repository,
        registry=registry,
        executor=executor,
        batch_runner=BatchRunner(runtime),
        readiness_gaps=readiness_gaps,
        langfuse=langfuse_client,
    )
