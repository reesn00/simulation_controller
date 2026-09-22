"""orchestration.workers.etl_worker: ST-3 etl 阶段单文件处理入口.

新架构下 worker 是无状态模块函数::

    result = run_etl_once(
        *, c2_path, etl_outputs_dir, task_id, session_id,
    )

不再有 ``EtlWorker`` 类 / ``BaseWorker`` 抽象 / ``run_forever`` 主循环.
调度 / 重试 / SQLite 写全部归 ST-5 PipelineExecutor.

执行链::

    C2 refined Session 单文件 (JSON)
      -> load_refined_session(c2_path) 校验 schema_version
      -> save_session_v2(session, base_path) 拆 4 视图
      -> EtlOutputs(messages_path, openai_path, qwenjina_path|None, meta_path, ...)
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from pathlib import Path

from gdr.domain import save_session_v2

from etl.parsers import load_refined_session

from orchestration.workers.base_worker import _output_filename

_log = logging.getLogger(__name__)


# ----------------------------------------------------------------------
# 数据契约 (契约 §3.4)
# ----------------------------------------------------------------------


@dataclass(frozen=True)
class EtlOutputs:
    """``run_etl_once`` 成功返回值 (C3 4 视图路径集合)."""

    messages_path: Path
    openai_path: Path
    qwenjina_path: Path | None
    meta_path: Path
    task_id: str
    session_id: str
    duration_seconds: float


# ----------------------------------------------------------------------
# 异常 (契约 §3.4)
# ----------------------------------------------------------------------


class EtlNonRetryableError(Exception):
    """C2 文件 schema 不匹配 / load_refined_session 失败 — 不应重试."""


# ----------------------------------------------------------------------
# 模块函数 (契约 §3.4)
# ----------------------------------------------------------------------


def run_etl_once(
    *,
    c2_path: Path,
    etl_outputs_dir: Path,
    task_id: str,
    session_id: str,
) -> EtlOutputs:
    """单个 C2 refined Session JSON → 4 视图文件.

    流程 (契约 §3.4):
      1. ``load_refined_session(c2_path)`` 读 + 校验 schema_version
      2. ``save_session_v2(session, base_path)`` 写 4 视图
      3. 返 ``EtlOutputs(messages_path=..., openai_path=..., ...)``

    Parameters
    ----------
    c2_path:
        gdr 阶段产出的 C2 refined Session 单文件路径.
    etl_outputs_dir:
        C3 4 视图文件输出目录.
    task_id:
        Catalog 内的 task_id, 用于产物文件名前缀 + ``EtlOutputs.task_id``.
    session_id:
        远端 session_id, 用于产物文件名前缀 + ``EtlOutputs.session_id``.

    Returns
    -------
    ``EtlOutputs`` with:
      - ``messages_path``: ``<stem>.messages.json``
      - ``openai_path``: ``<stem>.openai.json``
      - ``qwenjina_path``: ``<stem>.qwenjina.txt`` 或 ``None`` (qf_text 缺失)
      - ``meta_path``: ``<stem>.meta.json``
      - ``task_id`` / ``session_id``: 原样回传
      - ``duration_seconds``: 端到端耗时 (秒, float)

    Raises
    ------
    EtlNonRetryableError:
        c2_path 缺失 / ``load_refined_session`` 失败 (schema 不匹配 /
        JSON 损坏). 调用方应直接标 dead.
    Exception:
        ``save_session_v2`` 写盘失败 (IO 抖动) 等未显式分类异常,
        由 PipelineExecutor 兜底 (本函数不重新分类 ——
        etl 阶段重试成本极低, PipelineExecutor 直接走 attempts_etl 计数).
    """
    c2_path = Path(c2_path)
    etl_outputs_dir = Path(etl_outputs_dir)

    t0 = time.perf_counter()

    # --- 1. C2 文件存在性 + schema 校验 ---
    if not c2_path.exists():
        raise EtlNonRetryableError(
            f"run_etl_once: C2 refined session missing for task {task_id}: {c2_path}"
        )

    # C2 契约入口: 校验 schema_version=refined_session.v1
    try:
        session = load_refined_session(c2_path)
    except (ValueError, UnicodeDecodeError, FileNotFoundError) as exc:
        # schema 不匹配 / JSON 损坏 → 永久性错误, 重试不会改变结果
        raise EtlNonRetryableError(
            f"run_etl_once: load_refined_session error ({type(exc).__name__}): {exc}"
        ) from exc

    # --- 2. 4 视图产物落 etl_outputs_dir ---
    # base_path 是无扩展名 stem; save_session_v2 会自动追加
    # ``.messages.json`` / ``.openai.json`` / ``.qwenjina.txt`` / ``.meta.json``
    # 4 个尾缀. stem = ``<safe_task_id>__<safe_session_id>`` (与 gdr
    # 写 C2 时同一前缀, 便于 C2 与 C3 归并到同一 prefix).
    base_path = etl_outputs_dir / _output_filename(task_id, session_id, suffix="")
    base_path.parent.mkdir(parents=True, exist_ok=True)

    outputs = save_session_v2(session, base_path)

    # --- 3. 构造 EtlOutputs (SessionOutputs -> EtlOutputs 字段映射) ---
    duration = time.perf_counter() - t0
    _log.info(
        "run_etl_once: success task=%s messages=%s openai=%s "
        "qwenjina=%s meta=%s duration=%.2fs",
        task_id, outputs.messages, outputs.openai, outputs.qwenjina,
        outputs.meta, duration,
    )
    return EtlOutputs(
        messages_path=outputs.messages,
        openai_path=outputs.openai,
        qwenjina_path=outputs.qwenjina,
        meta_path=outputs.meta,
        task_id=task_id,
        session_id=session_id,
        duration_seconds=duration,
    )