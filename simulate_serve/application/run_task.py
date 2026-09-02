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
from simulate_serve.interaction.models import InteractionContext
from simulate_serve.validation.decline_detector import (
    detect_honest_limitation,
    looks_like_repeated_refusal,
)
from simulate_serve.validation.reason_codes import closing_target

from .ports import ExecutorGateway, RunRepositoryPort, TrajectoryArchivePort, ValidationPort
from .errors import ExecutorPortError
from .run_guard import RuntimeGuardPolicy, evaluate_runtime_guard


logger = logging.getLogger(__name__)

class TaskRuntime:
    def __init__(
        self,
        executor: ExecutorGateway,
        actor: InteractionActor,
        validator: ValidationPort | None,
        repository: RunRepositoryPort | None = None,
        *,
        guard_policy: RuntimeGuardPolicy | None = None,
        trajectory_archiver: TrajectoryArchivePort | None = None,
    ):
        self.executor = executor
        self.actor = actor
        self.validator = validator
        self.repository = repository
        self.guard_policy = guard_policy or RuntimeGuardPolicy()
        self.trajectory_archiver = trajectory_archiver

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
                if self.validator is None:
                    # Record-only 模式（validation.enabled=false）：远端已执行、
                    # 轨迹已在 _record_response 归档；不做验收、不追问，
                    # 以 INCONCLUSIVE 终态收场（绝不标 SUCCESS）。
                    run.failure = RunFailure(
                        code="VALIDATION_DISABLED",
                        message="本地验证已关闭（record-only 模式）",
                        stage="validation",
                    )
                    self._move(run, RunState.INCONCLUSIVE, "VALIDATION_DISABLED")
                    return run
                report = await self.validator.validate(task, run, response.text)
                run.validation_rounds.append(report)
                self._persist(run)

                if report.verdict is Verdict.PASS:
                    closing_signal = self._closing_signal(task)
                    self._move(
                        run,
                        RunState.SUCCESS,
                        "VALIDATION_PASSED",
                        self._decision_detail(task, report, "complete", closing_signal=closing_signal),
                    )
                    return run
                if report.verdict is Verdict.ERROR:
                    run.failure = RunFailure(code="VALIDATION_ERROR", message="; ".join(report.missing_items), stage="validation")
                    self._move(
                        run,
                        RunState.VALIDATION_ERROR,
                        "VALIDATION_ERROR",
                        self._decision_detail(task, report, "stop_error"),
                    )
                    return run
                if report.verdict is Verdict.INCONCLUSIVE:
                    run.failure = RunFailure(code="VALIDATION_INCONCLUSIVE", message="; ".join(report.missing_items), stage="validation")
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
                #
                # Two detectors, complementary:
                #   1. detect_honest_limitation — per-text regex over the reply
                #      ("我无法...", "不做。", "抱歉没法..."). High precision,
                #      narrow coverage. Fails open when an agent declines in
                #      wording the regex does not know yet.
                #   2. looks_like_repeated_refusal — trajectory heuristic:
                #      same failing reason_codes across guide rounds + the
                #      reply is shrinking / adding nothing new. Catches the
                #      "措辞漂移型持续拒绝" pattern (做 → 不列 → 重复问都
                #      一样) that the regex cannot catch deterministically.
                decline_source: str | None = None
                if task.interaction_policy.blocked_action != "no_decline_check":
                    if detect_honest_limitation(response.text):
                        decline_source = "regex"
                    elif looks_like_repeated_refusal(
                        failing_criterion_ids_by_round=[
                            frozenset(
                                item.criterion_id
                                for item in past_report.criteria
                                if item.verdict is Verdict.FAIL
                            )
                            for past_report in run.validation_rounds
                        ],
                        response_texts_by_round=[
                            turn.content
                            for turn in run.conversation
                            if turn.role == "assistant"
                        ],
                        guide_rounds=run.guide_rounds,
                    ):
                        decline_source = "repeated_refusal_trajectory"
                if decline_source is not None:
                    run.failure = RunFailure(
                        code="AGENT_DECLINED",
                        message="远端 Agent 明确声明无法完成",
                        stage="executor_response",
                    )
                    detail = self._decision_detail(task, report, "stop_declined")
                    detail["decline_source"] = decline_source
                    self._move(
                        run,
                        RunState.GUIDE_EXHAUSTED,
                        "AGENT_DECLINED",
                        detail,
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
                run.guidance_levels.append(followup.guidance_level or "L2")
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
            self._archive_trajectory(run)
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
    def _closing_signal(task: CompiledTask) -> tuple[str, str] | None:
        """Return the audit-only (criterion_id, reason_code) for a PASS termination.

        Terminal state is already recorded by ``RunState`` + ``RunFailure`` +
        ``decision_detail``; no closing turn is appended to ``run.conversation``
        so the user/assistant alternation invariant consumed by distillation
        and legacy export stays intact. ``closing_target`` returns ``None`` for
        scenarios/actions that are not declared in ``CLOSING_REASON_CODES``.
        """
        return closing_target(task.scenario_id, task.interaction_policy.pass_action)

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
        # Consecutive-FAIL counter per criterion over the rounds so far.
        # DEFERRED_* and INCONCLUSIVE rounds neither grow nor reset a streak:
        # only an actual FAIL counts, only a PASS forgives.
        fail_streaks: dict[str, int] = {}
        for round_report in run.validation_rounds:
            for item in round_report.criteria:
                if item.verdict is Verdict.FAIL and not item.reason_code.startswith("DEFERRED_"):
                    fail_streaks[item.criterion_id] = fail_streaks.get(item.criterion_id, 0) + 1
                elif item.verdict is Verdict.PASS:
                    fail_streaks[item.criterion_id] = 0
        return InteractionContext(
            task=task,
            conversation=tuple(run.conversation),
            guide_rounds=run.guide_rounds,
            regressed_criteria=regressed,
            run_id=run.run_id,
            fail_streaks=fail_streaks,
            previous_guidance_level=run.guidance_levels[-1] if run.guidance_levels else None,
        )

    def _record_response(self, run: TaskRun, response: object) -> None:
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
        self._archive_trajectory(run)

    def _archive_trajectory(self, run: TaskRun) -> None:
        # Snapshot the remote session trajectory after every executor turn so
        # an interrupted run still has its captured state; the archiver
        # overwrites one file per run and never raises.
        if not self.trajectory_archiver or not run.remote_session_id:
            return
        try:
            self.trajectory_archiver.archive(run.run_id, run.remote_agent_id, run.remote_session_id)
        except Exception:
            # Port contract says implementations must not raise; enforce the
            # invariant anyway so capture problems can never affect the run.
            logger.warning("trajectory archiver raised; capture skipped", exc_info=True)

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
        closing_signal: tuple[str, str] | None = None,
    ) -> dict[str, object]:
        criteria = getattr(report, "criteria", ())
        detail: dict[str, object] = {
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
        if closing_signal:
            # T3 behaviour label: "user accepts the agent's decline /
            # clarification" on the criterion named by the closing map.
            # Audit-only — never enters CriterionResult.verdict aggregation.
            detail["closing_target_criterion"] = closing_signal[0]
            detail["closing_reason_code"] = closing_signal[1]
        return detail
