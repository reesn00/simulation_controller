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
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from pathlib import Path

from gdr.config.settings import Settings
from gdr.parsers import from_trajectory
from gdr.pipeline.runner import _process_one_file

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
    """
    src_path = Path(src_path)
    refined_dir = Path(refined_dir)

    t0 = time.perf_counter()

    # --- 1. src 存在性 + 解析前置校验 (永久性错误直接抛 GdrNonRetryableError) ---
    if not src_path.exists():
        raise GdrNonRetryableError(
            f"run_gdr_once: trajectory missing for task {task_id}: {src_path}"
        )
    try:
        from_trajectory(src_path)
    except (ValueError, UnicodeDecodeError, FileNotFoundError) as exc:
        # 解析失败是永久性错误 (重试不会改变文件内容)
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
        raise GdrNonRetryableError(
            f"run_gdr_once: gdr status={status!r} (task={task_id}) {err}"
        )

    # save_error / 其他未分类 → 可重试.
    raise RetryableGdrError(
        f"run_gdr_once: gdr returned non-retryable-error status={status!r} "
        f"(task={task_id}) {err}"
    )