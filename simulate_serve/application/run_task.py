from __future__ import annotations

import asyncio
import logging
import time
import uuid

from simulate_serve.domain.run import ConversationTurn, RunFailure, TaskRun
from simulate_serve.domain.state_machine import RunState, RunStateMachine
from simulate_serve.domain.task import CompiledTask
from simulate_serve.domain.validation import ValidationReport, Verdict
from simulate_serve.interaction.actor import InteractionActor
from simulate_serve.interaction.models import ClosingTrigger, InteractionContext
from simulate_serve.validation.decline_detector import detect_honest_limitation

from .ports import ExecutorGateway, RunRepositoryPort, ValidationPort
from .errors import ExecutorPortError
from .run_guard import RuntimeGuardPolicy, evaluate_runtime_guard


logger = logging.getLogger(__name__)

class TaskRuntime:
    def __init__(
        self,
        executor: ExecutorGateway,
        actor: InteractionActor,
        validator: ValidationPort,
        repository: RunRepositoryPort | None = None,
        *,
        guard_policy: RuntimeGuardPolicy | None = None,
    ):
        self.executor = executor
        self.actor = actor
        self.validator = validator
        self.repository = repository
        self.guard_policy = guard_policy or RuntimeGuardPolicy()

    async def run(self, task: CompiledTask, *, rerun_of: str | None = None) -> TaskRun:
        started_monotonic = time.monotonic()
        run = TaskRun(
            run_id=f"run_{uuid.uuid4().hex}",
            task_id=task.task_id,
            task_type=task.task_type,
            catalog_schema_version=task.validation_policy.source_schema_version,
            dimension=task.dimension,
            scenario_id=task.scenario_id,
            persona_role=task.persona.role_description,
            rerun_of=rerun_of,
        )
        self._move(run, RunState.PREPARING, "RUN_PREPARING")
        try:
            self._move(run, RunState.GENERATING_OPENING, "OPENING_REQUESTED")
            opening = await self.actor.create_opening(self._context(task, run))
            run.conversation.append(ConversationTurn(role="user", content=opening.content))
            self._move(run, RunState.WAITING_EXECUTOR, "OPENING_CREATED")
            response = await self.executor.open_session(opening.content)
            self._record_response(run, response)

            while True:
                self._move(run, RunState.VALIDATING, "EXECUTOR_RESPONDED")
                report = await self.validator.validate(task, run, response.text)
                run.validation_rounds.append(report)
                self._persist(run)

                if report.verdict is Verdict.PASS:
                    await self._append_closing(run, task, ClosingTrigger.PASS)
                    self._move(
                        run,
                        RunState.SUCCESS,
                        "VALIDATION_PASSED",
                        self._decision_detail(task, report, "complete"),
                    )
                    return run
                if report.verdict is Verdict.ERROR:
                    run.failure = RunFailure(code="VALIDATION_ERROR", message="; ".join(report.missing_items), stage="validation")
                    if self._environment_owned(task, report):
                        await self._append_closing(run, task, ClosingTrigger.ENVIRONMENT_STOP)
                    self._move(
                        run,
                        RunState.VALIDATION_ERROR,
                        "VALIDATION_ERROR",
                        self._decision_detail(task, report, "stop_error"),
                    )
                    return run
                if report.verdict is Verdict.INCONCLUSIVE:
                    run.failure = RunFailure(code="VALIDATION_INCONCLUSIVE", message="; ".join(report.missing_items), stage="validation")
                    if self._environment_owned(task, report):
                        await self._append_closing(run, task, ClosingTrigger.ENVIRONMENT_STOP)
                    self._move(
                        run,
                        RunState.INCONCLUSIVE,
                        "VALIDATION_INCONCLUSIVE",
                        self._decision_detail(task, report, "stop_inconclusive"),
                    )
                    return run
                # Decline interpretation runs only after validation failed: a
                # passing reply is never terminated for its phrasing, and the
                # report is already available for the decision audit.
                if (
                    task.interaction_policy.blocked_action != "no_decline_check"
                    and detect_honest_limitation(response.text)
                ):
                    await self._append_closing(run, task, ClosingTrigger.AGENT_DECLINED)
                    run.failure = RunFailure(
                        code="AGENT_DECLINED",
                        message="远端 Agent 明确声明无法完成",
                        stage="executor_response",
                    )
                    self._move(
                        run,
                        RunState.GUIDE_EXHAUSTED,
                        "AGENT_DECLINED",
                        self._decision_detail(task, report, "stop_declined"),
                    )
                    return run

                guard = evaluate_runtime_guard(
                    run,
                    self.guard_policy,
                    started_monotonic=started_monotonic,
                )
                if guard is not None:
                    run.failure = RunFailure(
                        code=guard.code,
                        message=guard.message,
                        stage="runtime_guard",
                    )
                    detail = self._decision_detail(task, report, "stop_guard")
                    detail.update({"guard_code": guard.code, **guard.detail})
                    self._move(run, RunState.GUIDE_EXHAUSTED, "RUN_GUARD_TRIGGERED", detail)
                    return run
                if not report.retryable or run.guide_rounds >= task.interaction_policy.max_guide_rounds:
                    run.failure = RunFailure(code="GUIDE_EXHAUSTED", message="; ".join(report.missing_items), stage="validation")
                    self._move(
                        run,
                        RunState.GUIDE_EXHAUSTED,
                        "GUIDE_EXHAUSTED",
                        self._decision_detail(task, report, "stop_exhausted"),
                    )
                    return run

                self._move(run, RunState.GENERATING_FOLLOWUP, "FOLLOWUP_REQUESTED")
                context = self._context(task, run)
                followup = await self.actor.create_followup(context, report)
                run.conversation.append(ConversationTurn(role="user", content=followup.content))
                self._move(
                    run,
                    RunState.WAITING_EXECUTOR,
                    "FOLLOWUP_CREATED",
                    detail={
                        "report_id": report.report_id,
                        "reason_codes": list(followup.reason_codes),
                        "target_criteria": list(followup.target_criteria),
                        "regressed_criteria": list(context.regressed_criteria),
                        "guidance_level": followup.guidance_level,
                        "decision_action": "retry",
                    },
                )
                response = await self.executor.continue_session(run.remote_session_id, followup.content)
                run.guide_rounds += 1
                self._record_response(run, response)
        except asyncio.CancelledError:
            if not run.is_terminal:
                self._move(run, RunState.CANCELLED, "RUN_CANCELLED")
            raise
        except ExecutorPortError as exc:
            if exc.remote_task_id and exc.remote_task_id not in run.remote_task_ids:
                run.remote_task_ids.append(exc.remote_task_id)
            if exc.remote_session_id:
                run.remote_session_id = exc.remote_session_id
            if exc.agent_id:
                run.remote_agent_id = exc.agent_id
            run.failure = RunFailure(code="EXECUTOR_ERROR", message=str(exc), stage=exc.stage, retryable=exc.retryable)
            if not run.is_terminal:
                self._move(run, RunState.EXECUTOR_ERROR, "EXECUTOR_ERROR")
            return run
        except Exception as exc:
            run.failure = RunFailure(code="ACTOR_ERROR", message=str(exc), stage="interaction")
            if run.state in {RunState.PREPARING, RunState.GENERATING_OPENING, RunState.GENERATING_FOLLOWUP}:
                self._move(run, RunState.ACTOR_ERROR, "ACTOR_ERROR")
                return run
            if run.state is RunState.VALIDATING:
                run.failure = RunFailure(code="VALIDATION_ERROR", message=str(exc), stage="validation")
                self._move(run, RunState.VALIDATION_ERROR, "VALIDATION_ERROR")
                return run
            raise
        finally:
            self._persist(run)

    @staticmethod
    def _environment_owned(task: CompiledTask, report: ValidationReport) -> bool:
        """True when the current failure is attributed to the environment side."""
        criteria = {item.criterion_id: item for item in task.criteria}
        return any(
            item.criterion_id in criteria
            and criteria[item.criterion_id].remediation.owner == "environment"
            and item.verdict is not Verdict.PASS
            for item in report.criteria
        )

    # Closing turns are only produced for the known default actions; a scenario
    # declaring any other action string keeps today's no-closing behaviour.
    _CLOSING_ACTIONS = {
        ClosingTrigger.PASS: frozenset({"thank_and_finish", "provide_clarification_and_continue"}),
        ClosingTrigger.ENVIRONMENT_STOP: frozenset({"stop_without_blame_executor"}),
        ClosingTrigger.AGENT_DECLINED: frozenset({"accept_honest_limitation"}),
    }

    async def _append_closing(self, run: TaskRun, task: CompiledTask, trigger: ClosingTrigger) -> None:
        action = {
            ClosingTrigger.PASS: task.interaction_policy.pass_action,
            ClosingTrigger.ENVIRONMENT_STOP: task.interaction_policy.environment_error_action,
            ClosingTrigger.AGENT_DECLINED: task.interaction_policy.blocked_action,
        }[trigger]
        if action not in self._CLOSING_ACTIONS[trigger]:
            return
        try:
            closing = await self.actor.create_closing(self._context(task, run), trigger)
        except Exception as exc:
            logger.warning("closing message unavailable for %s: %s", trigger, exc)
            return
        run.conversation.append(ConversationTurn(role="user", content=closing.content))
        self._persist(run)

    @staticmethod
    def _context(task: CompiledTask, run: TaskRun) -> InteractionContext:
        regressed: tuple[str, ...] = ()
        if len(run.validation_rounds) >= 2:
            previously_passed = {
                item.criterion_id
                for report in run.validation_rounds[:-1]
                for item in report.criteria
                if item.verdict is Verdict.PASS
            }
            current = {
                item.criterion_id: item
                for item in run.validation_rounds[-1].criteria
            }
            regressed = tuple(
                criterion.criterion_id
                for criterion in task.criteria
                if criterion.criterion_id in previously_passed
                and criterion.criterion_id in current
                and current[criterion.criterion_id].verdict is not Verdict.PASS
                and not current[criterion.criterion_id].reason_code.startswith("DEFERRED_AFTER_")
            )
        return InteractionContext(
            task=task,
            conversation=tuple(run.conversation),
            guide_rounds=run.guide_rounds,
            regressed_criteria=regressed,
        )

    @staticmethod
    def _record_response(run: TaskRun, response: object) -> None:
        from .ports import ExecutorResponse

        if not isinstance(response, ExecutorResponse):
            raise TypeError("Executor returned an invalid response")
        if run.remote_session_id and run.remote_session_id != response.session_id:
            raise ExecutorPortError("Executor changed session_id during continuation", stage="poll")
        run.remote_session_id = response.session_id
        run.remote_agent_id = response.agent_id
        run.remote_task_ids.append(response.remote_task_id)
        run.executor_turns += 1
        run.conversation.append(
            ConversationTurn(role="assistant", content=response.text, remote_task_id=response.remote_task_id)
        )

    def _move(self, run: TaskRun, target: RunState, event: str, detail: dict | None = None) -> None:
        RunStateMachine.transition(run, target, event, detail)
        self._persist(run)

    def _persist(self, run: TaskRun) -> None:
        if self.repository:
            self.repository.save_run(run)

    @staticmethod
    def _decision_detail(
        task: CompiledTask,
        report: object,
        action: str,
    ) -> dict[str, object]:
        criteria = getattr(report, "criteria", ())
        return {
            "decision_action": action,
            "report_id": getattr(report, "report_id", ""),
            "reason_codes": [
                item.reason_code
                for item in criteria
                if item.verdict is not Verdict.PASS
            ],
            "ambiguity_declared": bool(task.intent.uncertainties),
            "fallback_outcomes": [item.outcome for item in task.fallback_plan],
        }
