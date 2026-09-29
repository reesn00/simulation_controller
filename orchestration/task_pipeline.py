"""orchestration.task_pipeline: 单 task 三阶段 (simulate → gdr → etl) 子进程入口.

设计依据 ``docs/设计方案/pipeline-contracts.md`` §5.4 — §5.6.

子进程入口必须 ``picklable`` (multiprocessing.Pool.apply_async 要求),所以:

* ``_worker_init`` 与 ``_run_one_task_pipeline`` 是模块顶层函数 (非闭包)
* 入参仅传可 pickle 的浅 dataclass / Path / Settings 实例 (无 SQLite connection)
* SQLiteQueue 在子进程内 ``SQLiteQueue(paths.sqlite_db)`` 重新构造
* 业务模块 (``producer_simulate`` / ``workers.gdr_worker`` / ``workers.etl_worker``)
  在函数体内延迟 import,避免父进程 import 时触发不必要的初始化

返回: ``{"task_id": str, "phase": "done"|"dead", "stage": str, "error": str|None}``

任何未捕获异常都走顶层 try/except → ``mark_failed(stage=<current_stage>, error_msg=str(exc))``
+ 返回 ``{"phase": "dead", ...}``;**不抛异常给主进程**。
"""

from __future__ import annotations

import logging
import traceback
from pathlib import Path
from typing import Any

from gdr.config.settings import Settings as GdrSettings

from orchestration.settings import Paths, PipelineSettings

_log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# 子进程初始化 (契约 §5.4)
# ---------------------------------------------------------------------------

# 子进程全局:paths 仅用于子进程内可重连的 SQLite db / refined_dir / etc.
_WORKER_PATHS: Paths | None = None


def _worker_init(paths: Paths) -> None:
    """子进程初始化:把 ``paths`` 写入子进程模块全局,便于异常日志引用.

    **不要**在此处读 SQLite / 起 logging handler (logging 多进程不安全,
    子进程用默认 stderr 即可,handler 在 Pool 父级初始化)。
    """
    global _WORKER_PATHS
    _WORKER_PATHS = paths


# ---------------------------------------------------------------------------
# 单 task 三阶段流水线 (契约 §5.4)
# ---------------------------------------------------------------------------

# 哪些 RunState 算 simulate 失败 (不入 gdr 阶段)
# 参考 simulate_serve.domain.state_machine.TERMINAL_STATES — 但 SUCCESS 应进 gdr
# 所以这里排除 SUCCESS。
#
# 判据是 CLAUDE.md "数据保留原则", 不是验证通过与否:
# 远端 Agent 验证不通过 (guide_exhausted / inconclusive) ≠ 数据不可用。
# 这类轨迹结构完整、含 assistant 实质回复 (含拒答), 是有价值的 SFT 素材,
# 必须继续走 gdr -> etl; 其质量信号由「验证不通过原因 + agent 回复内容」
# 交 LLM 评价后自动记 0 分承载 (见 docs/设计方案/simulate-fail-scoring.md)。
# 真正进死信的只剩基础设施故障 / 人为中断 / 完成度截断这三类:
_SIMULATE_FAIL_STATES: frozenset[str] = frozenset({
    "validation_error",
    "executor_error",
    "actor_error",
    "cancelled",
    "interrupted",
    "completion_incomplete",
})


def _mark_dead(
    queue: Any, task_id: str, *, stage: str, error_msg: str,
) -> None:
    """统一 ``mark_failed`` 入口;异常被吞,不抛给主进程."""
    try:
        queue.mark_failed(task_id, stage=stage, error_msg=error_msg)
    except Exception as exc:  # pragma: no cover - 兜底
        _log.warning("task_pipeline: mark_failed(%s, %s) failed: %s",
                     task_id, stage, exc)


def _mark_audited(
    queue: Any, task_id: str, *, stage: str, error_msg: str,
) -> None:
    """统一 ``mark_audited`` 入口;异常被吞,不抛给主进程.

    评分低但结构合格的 session 走 audited 终态, 不进 dead —
    见 CLAUDE.md "数据保留原则".
    """
    try:
        queue.mark_audited(task_id, stage=stage, error_msg=error_msg)
    except Exception as exc:  # pragma: no cover - 兜底
        _log.warning("task_pipeline: mark_audited(%s, %s) failed: %s",
                     task_id, stage, exc)


def _safe_run_gdr(
    *, task_id: str, src_path: Path, refined_dir: Path,
    session_id: str, gdr_settings: GdrSettings,
    max_retry: int, queue: Any,
) -> tuple[Path | None, str]:
    """gdr 阶段重试循环 (契约 §3.3).

    返回: (refined_path, status) 元组:
        - (path, "success")  → C2 已写, 进 etl 阶段
        - (None, "dead")     → 结构性问题 (load_error/discard/incomplete),
                                已 mark_failed, 任务终止
        - (None, "audited")  → 评分低 (judge_discard/scoring_reject),
                                已 mark_audited, 任务终止但**不进 dead**
                                (CLAUDE.md "数据保留原则")
    """
    # 延迟 import,避免父进程触发 gdr / httpx / asyncio 初始化
    from orchestration.workers.gdr_worker import (
        GdrAuditedError,
        GdrNonRetryableError,
        run_gdr_once,
    )

    last_exc: BaseException | None = None
    for attempt in range(max_retry + 1):
        try:
            queue.increment_attempts(task_id, stage="gdr")
            result = run_gdr_once(
                src_path=src_path,
                refined_dir=refined_dir,
                gdr_settings=gdr_settings,
                task_id=task_id,
                session_id=session_id,
            )
            return result.refined_path, "success"
        except GdrAuditedError as exc:
            # 评分低 (judge_discard / scoring_reject) → audited 终态,
            # 不进 dead (CLAUDE.md "数据保留原则"). 单次判定, 不重试
            # (拒收是确定性结果, 重试无意义).
            _mark_audited(
                queue, task_id, stage="gdr",
                error_msg=f"[audited:{exc.audit_reason}] {exc}",
            )
            return None, "audited"
        except GdrNonRetryableError as exc:
            _mark_dead(
                queue, task_id, stage="gdr",
                error_msg=f"[non-retryable] {type(exc).__name__}: {exc}",
            )
            return None, "dead"
        except Exception as exc:
            last_exc = exc
            _log.warning(
                "task_pipeline: %s gdr attempt %d/%d failed: %s",
                task_id, attempt + 1, max_retry + 1, exc,
            )
            continue
    # 重试用尽
    _mark_dead(
        queue, task_id, stage="gdr",
        error_msg=(
            f"gdr retry exhausted ({max_retry + 1} attempts): "
            f"{type(last_exc).__name__ if last_exc else 'Unknown'}: "
            f"{last_exc}"
        ),
    )
    return None, "dead"


def _safe_run_etl(
    *, task_id: str, c2_path: Path, etl_outputs_dir: Path,
    session_id: str, max_retry: int, queue: Any,
    runs_dir: Path | None = None,
    run_id: str | None = None,
    src_path: Path | None = None,
    gdr_settings: Any = None,
) -> tuple[Path, Path, Path | None, Path] | None:
    """etl 阶段重试循环 (契约 §3.4).

    返回: (messages_path, openai_path, qwenjina_path, meta_path) 成功;None 失败。
    """
    # 延迟 import,避免父进程触发 etl / save_session_v2 初始化
    from orchestration.workers.etl_worker import (
        EtlNonRetryableError,
        run_etl_once,
    )

    last_exc: BaseException | None = None
    for attempt in range(max_retry + 1):
        try:
            queue.increment_attempts(task_id, stage="etl")
            outputs = run_etl_once(
                c2_path=c2_path,
                etl_outputs_dir=etl_outputs_dir,
                task_id=task_id,
                session_id=session_id,
                attempt=attempt,
                runs_dir=runs_dir,
                run_id=run_id,
                src_path=src_path,
                gdr_settings=gdr_settings,
            )
            return (
                outputs.messages_path,
                outputs.openai_path,
                outputs.qwenjina_path,
                outputs.meta_path,
            )
        except EtlNonRetryableError as exc:
            _mark_dead(
                queue, task_id, stage="etl",
                error_msg=f"[non-retryable] {type(exc).__name__}: {exc}",
            )
            return None
        except Exception as exc:
            last_exc = exc
            _log.warning(
                "task_pipeline: %s etl attempt %d/%d failed: %s",
                task_id, attempt + 1, max_retry + 1, exc,
            )
            continue
    # 重试用尽
    _mark_dead(
        queue, task_id, stage="etl",
        error_msg=(
            f"etl retry exhausted ({max_retry + 1} attempts): "
            f"{type(last_exc).__name__ if last_exc else 'Unknown'}: "
            f"{last_exc}"
        ),
    )
    return None


def _run_one_task_pipeline(
    task_id: str,
    paths: Paths,
    gdr_settings: GdrSettings,
    orchestration_settings: PipelineSettings,
) -> dict:
    """单 task 在子进程内完整跑 simulate → gdr → etl (契约 §5.4).

    流程:
        1. queue = SQLiteQueue(paths.sqlite_db)
        2. queue.upsert_task(task_id) (兜底:已被别人处理 → return done)
        3. queue.mark_phase(simulate)
        4. run = producer_simulate.run_one_task(task_id, config_path=paths.simulate_serve_config)
        5. run.state ∈ TERMINAL_FAIL_STATES → mark_failed; return dead
        6. queue.mark_phase(gdr, run_id=..., session_id=..., src_path=...)
        7. gdr 重试循环 (max_retry_gdr 次) → 成功 break;失败 mark_failed;return dead
        8. queue.mark_phase(etl, gdr_refined_path=...)
        9. etl 重试循环 (max_retry_etl 次)
       10. queue.mark_phase(done, etl_*_path=...)
       11. return {"phase": "done", ...}

    返回:
        {"task_id": str, "phase": "done"|"dead", "stage": str, "error": str|None}

    子进程不抛异常给主进程 — 任何未捕获都 ``mark_failed`` + 返 dead。
    """
    # 延迟 import SQLiteQueue 以避免父进程触发不必要的初始化
    from orchestration.queue import (
        PHASE_AUDITED,
        PHASE_DEAD,
        PHASE_DONE,
        PHASE_ETL,
        PHASE_GDR,
        PHASE_SIMULATE,
        SQLiteQueue,
        TaskAlreadyTerminal,
    )

    queue = SQLiteQueue(paths.sqlite_db)
    result: dict[str, Any] = {
        "task_id": task_id,
        "phase": PHASE_DEAD,
        "stage": "unknown",
        "error": None,
    }

    try:
        # 1. 兜底 upsert — 若已被另一个 worker 标 done,直接返 done
        try:
            queue.upsert_task(task_id)
        except TaskAlreadyTerminal:
            existing = queue.get_task(task_id)
            if existing is not None and existing.phase == PHASE_DONE:
                _log.info(
                    "task_pipeline: %s already done in another worker, skipping",
                    task_id,
                )
                result["phase"] = PHASE_DONE
                result["stage"] = "done"
                return result
            # 处于 dead,无需重跑
            result["phase"] = PHASE_DEAD
            result["stage"] = "unknown"
            result["error"] = "already dead in another worker"
            return result

        # 2. 标记 simulate 阶段开始
        queue.mark_phase(task_id, new_phase=PHASE_SIMULATE)
        result["stage"] = "simulate"

        # 3. simulate 阶段 (延迟 import)
        # producer_simulate.run_one_task 是 ``async def``; 子进程内不能直接
        # await, 必须用 ``run_one_task_sync`` (``asyncio.run`` 包装版).
        # 契约 §4.2 + §5.4: ST-5 子进程入口走 sync wrapper.
        from orchestration.producer_simulate import run_one_task_sync as _run_sim

        try:
            run = _run_sim(task_id, config_path=paths.simulate_serve_config)
        except KeyError as exc:
            result["error"] = f"task not in catalog: {exc}"
            _mark_dead(
                queue, task_id, stage="simulate",
                error_msg=f"[non-retryable] KeyError: {exc}",
            )
            return result
        except Exception as exc:
            result["error"] = f"{type(exc).__name__}: {exc}"
            _mark_dead(
                queue, task_id, stage="simulate",
                error_msg=f"{type(exc).__name__}: {exc}",
            )
            return result

        # 4. 判定 simulate 结果
        # run.state 是字符串 (RunState 是 str, Enum); 与 _SIMULATE_FAIL_STATES 对比
        run_state = getattr(run.state, "value", str(run.state))
        if run_state in _SIMULATE_FAIL_STATES:
            err = (
                f"simulate state={run_state!r}; "
                f"failure={getattr(run.failure, 'message', None) or 'n/a'}"
            )
            result["error"] = err
            _mark_dead(
                queue, task_id, stage="simulate",
                error_msg=err,
            )
            return result

        # 5. 解析 simulate 产物路径
        #     run_id / remote_session_id 来自 TaskRun;
        #     src_path = trajectory JSON 路径
        #     trajectory_archiver 给出的标准格式: <safe_run>__<safe_session>.json
        run_id = run.run_id
        session_id = run.remote_session_id
        if not session_id:
            # 没拿到 session_id 视为 gdr 不可继续,标 dead
            err = f"simulate returned empty session_id for {task_id}"
            result["error"] = err
            _mark_dead(
                queue, task_id, stage="simulate",
                error_msg=f"[non-retryable] {err}",
            )
            return result

        # 拼 src_path: 优先用 trajectory_archiver 标准格式
        safe_run = _safe_filename_part(run_id)
        safe_session = _safe_filename_part(session_id)
        src_path = paths.trajectory_dir / f"{safe_run}__{safe_session}.json"
        if not src_path.is_file():
            # fallback:扫描 dir 找匹配 session 的 .json
            candidates = sorted(
                paths.trajectory_dir.glob(f"*__{safe_session}.json"),
            )
            if candidates:
                src_path = candidates[0]
            else:
                err = (
                    f"trajectory file missing for task {task_id} "
                    f"(run_id={run_id}, session_id={session_id})"
                )
                result["error"] = err
                _mark_dead(
                    queue, task_id, stage="simulate",
                    error_msg=f"[non-retryable] {err}",
                )
                return result

        # 6. 标记 gdr 阶段 + 写 src_path / run_id / session_id
        queue.mark_phase(
            task_id, new_phase=PHASE_GDR,
            run_id=run_id, session_id=session_id,
            src_path=src_path,
        )
        result["stage"] = "gdr"

        # 7. gdr 重试循环
        refined_path, gdr_status = _safe_run_gdr(
            task_id=task_id,
            src_path=src_path,
            refined_dir=paths.refined_dir,
            session_id=session_id,
            gdr_settings=gdr_settings,
            max_retry=orchestration_settings.max_retry_gdr,
            queue=queue,
        )
        if gdr_status == "audited":
            # 评分低 (judge_discard / scoring_reject) → audited 终态,
            # 数据已在旁路 jsonl, src_path 保留, 不进 dead
            # (CLAUDE.md "数据保留原则"). task 终止于此.
            result["phase"] = PHASE_AUDITED
            result["stage"] = "gdr"
            result["error"] = None
            return result
        if gdr_status == "dead" or refined_path is None:
            result["error"] = "gdr failed"
            return result

        # 8. 标记 etl 阶段 + 写 gdr_refined_path
        queue.mark_phase(
            task_id, new_phase=PHASE_ETL,
            gdr_refined_path=refined_path,
        )
        result["stage"] = "etl"

        # 9. etl 重试循环
        etl_outputs = _safe_run_etl(
            task_id=task_id,
            c2_path=refined_path,
            etl_outputs_dir=paths.etl_outputs_dir,
            session_id=session_id,
            max_retry=orchestration_settings.max_retry_etl,
            queue=queue,
            # F2: 把 simulate 端 Criterion 验证结果带进 C3
            runs_dir=paths.runs_dir,
            run_id=run_id,
            # 验证不通过时: 读 agent 回复做 LLM 归因, 分数恒为 0
            src_path=src_path,
            gdr_settings=gdr_settings,
        )
        if etl_outputs is None:
            result["error"] = "etl failed"
            return result

        messages_path, openai_path, qwenjina_path, meta_path = etl_outputs

        # 10. 标记 done + 写 etl 输出路径
        queue.mark_phase(
            task_id, new_phase=PHASE_DONE,
            etl_messages_path=messages_path,
            etl_openai_path=openai_path,
            etl_qwenjina_path=qwenjina_path,
            etl_meta_path=meta_path,
        )
        result["phase"] = PHASE_DONE
        result["stage"] = "done"
        result["error"] = None

        # 11. Label Studio 自动 hook (方案 §9)。
        #     刻意放在 mark_phase(done) **之后**: 推送是旁路, 它的成败不影响
        #     task 的终态。ls_hook 内部永不抛异常, 失败只记日志 + 指标。
        _push_to_label_studio(paths, meta_path)
        return result

    except Exception as exc:
        # 顶层兜底:任何未捕获异常都标 dead, 不抛给主进程
        tb = traceback.format_exc(limit=4)
        msg = f"{type(exc).__name__}: {exc}\n{tb}"
        _log.error("task_pipeline: %s crashed: %s", task_id, msg)
        _mark_dead(
            queue, task_id, stage=result.get("stage", "unknown"),
            error_msg=f"pipeline crashed: {msg}",
        )
        result["error"] = msg
        return result


# ---------------------------------------------------------------------------
# 工具
# ---------------------------------------------------------------------------

def _push_to_label_studio(paths: Paths, meta_path: Path) -> None:
    """step 11: C3 → Label Studio 旁路推送。**永不抛、永不影响 task 终态。**

    开关只看 ``label_studio.hook.enabled``（由 :func:`ls_hook.load_hook_settings`
    判定后以 ``None`` 表达"未启用"), 不看 ``upload.enabled`` —— 两个开关交叉
    会让 hook 静默空转（配了 upload 就以为 hook 也开了）。
    """
    try:
        from orchestration.ls_hook import load_hook_settings, run_hook
        from label_studio.config_loader import load_label_studio_config

        # 根配置定位口径: SIMCTL_CONFIG env → 仓库根 config/
        hook_settings = load_hook_settings()
        if hook_settings is None:
            return
        settings = load_label_studio_config()

        def _resolve(s: Any) -> int:
            from label_studio.client import build_client
            from label_studio.project_manager import resolve_project_id

            return resolve_project_id(build_client(s), s)

        outcome = run_hook(
            settings, project_id=None, meta_path=meta_path,
            resolve_project_id=_resolve,
        )
        if not outcome["ok"] and not outcome.get("skipped"):
            _log.warning(
                "task_pipeline: Label Studio 推送未成功 (%s); "
                "task 仍标记 done —— 推送是旁路",
                outcome.get("reason"),
            )
    except Exception as exc:  # pragma: no cover - hook 自身已 fail-safe
        _log.warning("task_pipeline: Label Studio hook 异常, 已忽略: %s", exc)


def _safe_filename_part(s: str) -> str:
    """把任意字符串清成文件名安全字符 (复用 simulate_serve 的实现)."""
    try:
        from simulate_serve.infrastructure.trajectory_archiver import (
            sanitize_filename_part,
        )
        return sanitize_filename_part(s)
    except ImportError:
        # 子进程没装 / 不可 import 时退化:替换非法字符
        import re
        return re.sub(r"[^A-Za-z0-9_.-]", "_", s)