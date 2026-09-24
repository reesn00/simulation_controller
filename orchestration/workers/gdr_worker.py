"""orchestration.workers.gdr_worker: ST-3 gdr 阶段单文件处理入口.

新架构下 worker 是无状态模块函数::

    result = run_gdr_once(
        *, src_path, refined_dir, gdr_settings, task_id, session_id,
    )

不再有 ``BaseWorker`` / ``GdrWorker`` 类 / ``run_forever`` 主循环:
    * 调度由 ST-5 PipelineExecutor (multiprocessing.Pool) 负责
    * 重试由 PipelineExecutor (max_retry_gdr) 负责
    * SQLite 写由 PipelineExecutor (SQLiteQueue.mark_phase) 负责

执行链::

    C1 trajectory JSONL
      -> from_trajectory(src_path) 校验 + 解析
      -> _process_one_file(src_path, out_path, settings)
      -> C2 refined Session 单文件 (refined_path)

PR 3 (Commit 8): 外层包 ``stage_trace`` (``name="gdr:{task_id}"``) + 入口
``set_current_task_id(task_id)`` / finally 清 None; 让 21 步骤子 span
通过 ``_current_task_id()`` 读 task_id 写 metadata, UI 可按 task_id 过滤。
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from pathlib import Path

from gdr.config.settings import Settings
from gdr.parsers import from_trajectory
from gdr.pipeline.runner import _process_one_file

# PR 3: Langfuse 可观测性 import.
from simulate_serve.observability.langfuse_client import (
    get_client as _lf_get_client_for_gdr_worker,
    stage_trace as _lf_stage_trace_for_gdr_worker,
)
from gdr.observability.runner_helpers import (
    _payload_mode as _lf_payload_mode_for_gdr_worker,
    set_current_task_id as _lf_set_current_task_id_for_gdr_worker,
)

from orchestration.workers.base_worker import _output_filename

_log = logging.getLogger(__name__)


# ----------------------------------------------------------------------
# 数据契约 (契约 §3.3)
# ----------------------------------------------------------------------


@dataclass(frozen=True)
class GdrResult:
    """``run_gdr_once`` 成功返回值."""

    refined_path: Path
    task_id: str
    session_id: str
    duration_seconds: float


# ----------------------------------------------------------------------
# 异常 (契约 §3.3)
# ----------------------------------------------------------------------


class GdrNonRetryableError(Exception):
    """轨迹文件不合法 / schema 不匹配 / 远端 LLM 永久错误 — 不应重试."""


class GdrAuditedError(Exception):
    """评分低但结构合格的 session — 不应进 dead, 走 audited 终态.

    CLAUDE.md "数据保留原则": judge_discard / scoring_reject 的 session 数据
    保留在原 src_path + 旁路 jsonl, 不进 dead_dir, 供后期人工复核 / 任务调优 /
    质量问题归因. error_msg 含 audit_reason 供 master 决策.
    """

    def __init__(self, message: str, *, audit_reason: str) -> None:
        super().__init__(message)
        self.audit_reason = audit_reason


class RetryableGdrError(Exception):
    """LLM 调用失败 / 临时 IO 错误 — 可重试."""


# ----------------------------------------------------------------------
# 模块函数 (契约 §3.3)
# ----------------------------------------------------------------------


def run_gdr_once(
    *,
    src_path: Path,
    refined_dir: Path,
    gdr_settings: Settings,
    task_id: str,
    session_id: str,
    langfuse_client: Any | None = None,
    langfuse_cfg: Any | None = None,
) -> GdrResult:
    """单个 trajectory JSONL 文件 → 一个 C2 refined Session JSON.

    流程 (契约 §3.3):
      1. ``from_trajectory(src_path)`` 校验 + 解析 (前置 fail-fast)
      2. 构造 ``_process_one_file`` 的 ``Settings`` (workers=1,
         batch_output_dir 锚到 ``refined_dir``, ``max_files=1``)
      3. 调 ``_process_one_file(src_path, out_path, cfg)``
      4. 成功后返 ``GdrResult(refined_path=<out_path>, ...)``

    Parameters
    ----------
    src_path:
        C1 trajectory JSONL 路径 (由 simulate_serve archiver 落盘).
    refined_dir:
        C2 refined Session 输出目录.
    gdr_settings:
        gdr 库 ``Settings`` 实例 (由 Master._build_gdr_settings 构造);
        本函数强制覆盖 ``batch_output_dir`` / ``workers=1`` / ``max_files=1``.
    task_id:
        Catalog 内的 task_id, 用于产物文件名 + ``GdrResult.task_id``.
    session_id:
        远端 session_id, 用于产物文件名 + ``GdrResult.session_id``.
    langfuse_client:
        PR 5: 外部已构造好的 Langfuse 客户端 (主进程 ``_run_one_task_pipeline``
        入口构造); 非 None 时优先使用, 跳过本函数内 ``get_client``。
        默认 None (PR 3 旧调用兼容, 内部用 ``get_client(gdr_settings)``)。
    langfuse_cfg:
        PR 5: 外部 ``LangfuseConfig`` 容器; 与 ``langfuse_client`` 二选一,
        若 ``langfuse_client is None`` 且 ``langfuse_cfg`` 给出, 内部 fallback
        走 ``get_client(langfuse_cfg)``。保留是为了 PR 3 旧测试不破。

    Returns
    -------
    ``GdrResult`` with:
      - ``refined_path``: 写出的 C2 单文件路径
      - ``task_id``: 原样回传
      - ``session_id``: 原样回传 (若 src 文件名推不出, 用 task.session_id 兜底)
      - ``duration_seconds``: 端到端耗时 (秒, float)

    Raises
    ------
    GdrNonRetryableError:
        src_path 缺失 / 解析失败 / schema 不匹配 / gdr status ∈
        {"load_error", "discard", "incomplete"}. 调用方 (PipelineExecutor)
        看到该异常应直接标 ``dead``, 不消耗 ``max_retry_gdr``.
    RetryableGdrError:
        gdr status == "save_error" / 其他未分类错误 / 写盘 IO 失败 /
        LLM 调用临时失败. 可重试.
    Exception:
        任何未显式分类的异常, 由 PipelineExecutor 兜底标 dead.

    Notes
    -----
    PR 5 双轨客户端 (dual-track client):
      1. ``langfuse_client`` 显式注入 (主进程路径) — 复用同一个进程级 singleton
      2. ``langfuse_cfg`` 注入但 ``langfuse_client=None`` — fallback 调 ``get_client``
      3. 都为 None — 退化 ``get_client(gdr_settings)`` (PR 3 行为)
    """
    src_path = Path(src_path)
    refined_dir = Path(refined_dir)

    t0 = time.perf_counter()

    # PR 3 (Commit 8): thread-local task_id 守护 — 进入时设, finally 清空.
    # 21 步骤 helper 通过 _current_task_id() 读, 让子 span metadata 含 task_id.
    _lf_set_current_task_id_for_gdr_worker(task_id)
    _lf_final_result_holder: dict | None = {"status": None}

    # PR 5: 三段式 client 解析 — 显式 client > 显式 cfg > 旧 gdr_settings 兜底.
    if langfuse_client is not None:
        _lf_client = langfuse_client
    elif langfuse_cfg is not None:
        _lf_client = _lf_get_client_for_gdr_worker(langfuse_cfg)
    else:
        _lf_client = _lf_get_client_for_gdr_worker(gdr_settings)

    # payload_mode: 显式 cfg 优先 (主进程路径), 否则走 gdr_settings (旧路径).
    if langfuse_cfg is not None:
        _lf_payload_mode = _lf_payload_mode_for_gdr_worker(langfuse_cfg)
    else:
        _lf_payload_mode = _lf_payload_mode_for_gdr_worker(gdr_settings)
    try:
        with _lf_stage_trace_for_gdr_worker(
            _lf_client,
            session_id=session_id,
            name=f"gdr:{task_id}",
            user_id=session_id,  # 用 session_id 作 user_id, 与 simulate_serve 端一致
            task_id=task_id,
            tags=["stage:gdr", f"task:{task_id}"],
            metadata={
                "src_path": str(src_path),
                "refined_dir": str(refined_dir),
                "session_id": session_id,
            },
            input_data=None,
            output_capture=lambda: {
                "refined_path": str(_lf_final_result_holder.get("refined_path")),
                "status": _lf_final_result_holder.get("status"),
                "duration_seconds": round(time.perf_counter() - t0, 3),
            } if _lf_final_result_holder.get("status") else None,
            payload_mode=_lf_payload_mode,
        ):
            # --- 1. src 存在性 + 解析前置校验 (永久性错误直接抛 GdrNonRetryableError) ---
            if not src_path.exists():
                _lf_final_result_holder["status"] = "missing_src"
                raise GdrNonRetryableError(
                    f"run_gdr_once: trajectory missing for task {task_id}: {src_path}"
                )
            try:
                from_trajectory(src_path)
            except (ValueError, UnicodeDecodeError, FileNotFoundError) as exc:
                # 解析失败是永久性错误 (重试不会改变文件内容)
                _lf_final_result_holder["status"] = "parse_error"
                raise GdrNonRetryableError(
                    f"run_gdr_once: trajectory parse failed "
                    f"({type(exc).__name__}): {exc}"
                ) from exc

            # --- 2. 构造/复用 gdr.Settings ---
            # 强制 workers=1 / max_files=1 / batch_output_dir=refined_dir,
            # 其余 (llm_concurrency / model / endpoint / ...) 全部透传.
            cfg = gdr_settings.model_copy(update={
                "batch_output_dir": refined_dir,
                "workers": 1,
                "max_files": 1,
            })

            # --- 3. 计算输出路径 + 调 _process_one_file ---
            out_path = refined_dir / _output_filename(task_id, session_id, suffix=".json")
            out_path.parent.mkdir(parents=True, exist_ok=True)

            result = _process_one_file(src_path, out_path, cfg)

            # --- 4. 解读 gdr.status ---
            if result is None:
                _lf_final_result_holder["status"] = "gdr_returned_none"
                raise RetryableGdrError(
                    f"run_gdr_once: gdr returned None (task={task_id})"
                )

            status = result.get("status")
            err = (result or {}).get("error", "")

            if status == "success":
                refined_path = Path(result.get("output") or out_path)
                duration = time.perf_counter() - t0
                _log.info(
                    "run_gdr_once: success task=%s refined=%s duration=%.2fs",
                    task_id, refined_path, duration,
                )
                _lf_final_result_holder["refined_path"] = refined_path
                _lf_final_result_holder["status"] = "success"
                return GdrResult(
                    refined_path=refined_path,
                    task_id=task_id,
                    session_id=session_id,
                    duration_seconds=duration,
                )

            if status in ("load_error", "discard", "incomplete"):
                # load_error = 输入文件坏 (永久); discard = 结构不可用 (硬丢弃,
                # 重试结果相同); incomplete = 未闭合 session 检测已旁路到
                # refine_data/incomplete.jsonl, refine_data 跳过, 重跑只会让
                # detector 再命中一次, 不改变 outcome.
                # 三者都是结构性问题 → 走 dead (CLAUDE.md "数据保留原则" 边界).
                _lf_final_result_holder["status"] = status
                raise GdrNonRetryableError(
                    f"run_gdr_once: gdr status={status!r} (task={task_id}) {err}"
                )

            # 评分低 (结构合格, 仅质量决策) → 走 audited, 不进 dead.
            # judge_discard: C2 已写 + judge_low.jsonl 已落 + refined 保留;
            # scoring_reject: C2 不写 (C2 即为待训练产物, 拒收不写) +
            #                 audit/scoring_reject.jsonl 已落.
            if status in ("judge_discard", "scoring_reject"):
                _lf_final_result_holder["status"] = status
                raise GdrAuditedError(
                    f"run_gdr_once: gdr status={status!r} (task={task_id}) {err}",
                    audit_reason=status,
                )

            # save_error / 其他未分类 → 可重试.
            _lf_final_result_holder["status"] = status
            raise RetryableGdrError(
                f"run_gdr_once: gdr returned non-retryable-error status={status!r} "
                f"(task={task_id}) {err}"
            )
    finally:
        # PR 3 (Commit 8): finally 清 task_id thread-local, 避免 worker 复用
        # 同一线程时泄漏到下次 task.
        _lf_set_current_task_id_for_gdr_worker(None)