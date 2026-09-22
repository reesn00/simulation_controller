"""orchestration.workers.base_worker: ST-3 worker 公共工具.

新架构 ``simulation server → gdr → etl`` 下 worker 是无状态函数:
    * ``run_gdr_once`` (orchestration.workers.gdr_worker)
    * ``run_etl_once`` (orchestration.workers.etl_worker)

本模块只保留产物命名的 ``_output_filename`` 工具 ——
由 ST-5 PipelineExecutor 在子进程入口 ``_run_one_task_pipeline`` 内调用,
为 gdr 阶段算 C2 refined Session 的输出文件名.

不再保留 ``BaseWorker`` 抽象类 / ``run_forever`` 主循环 / ``pull`` /
``run_once`` 等方法. 调度循环归 PipelineExecutor (multiprocessing.Pool);
retry 循环归 PipelineExecutor (基于 max_retry_gdr / max_retry_etl);
SQLite 写归 PipelineExecutor (调 SQLiteQueue.mark_phase).
"""

from __future__ import annotations

import re

# 文件名合法字符: ASCII 字母 / 数字 / 点 / 下划线 / 连字符.
# 与 simulate_serve.infrastructure.trajectory_archiver.sanitize_filename_part
# 保持一致; 不复用是因为该函数在 simulate_serve 路径下,
# orchestration 引用 simulate_serve 反而打破层依赖.
_UNSAFE_FILENAME_CHARS = re.compile(r"[^A-Za-z0-9._-]+")


def _sanitize_filename_part(value: str) -> str:
    """Make an arbitrary id safe for embedding in a file name.

    非法字符 (含路径分隔符 / 空格 / 控制字符) 替换为 ``_``; 空字符串原样
    返回 (调用方按业务决定 fallback).
    """
    return _UNSAFE_FILENAME_CHARS.sub("_", value)


def _output_filename(
    task_id: str,
    session_id: str,
    *,
    suffix: str = "",
) -> str:
    """产物文件命名: ``<safe_task_id>__<safe_session_id><suffix>``.

    Parameters
    ----------
    task_id:
        Catalog 内的 task_id, 通常以 ``TXXX`` 形态. 内部做 sanitize,
        文件名非法字符替换为 ``_``.
    session_id:
        QwenPaw 返回的远端 session_id. 同样 sanitize.
    suffix:
        文件扩展名/尾缀, 例如 ``""`` / ``".messages.json"`` /
        ``".openai.json"`` / ``".meta.json"``. 默认空串 → 用于 gdr
        阶段 C2 refined Session 单文件 (``.json`` 由 caller 加).

    Returns
    -------
    文件名 (不含目录), 例如 ``"T001__abc123.json"``.
    """
    safe_task = _sanitize_filename_part(task_id) if task_id else ""
    safe_session = _sanitize_filename_part(session_id) if session_id else ""
    prefix = f"{safe_task}__" if safe_task else ""
    return f"{prefix}{safe_session}{suffix}"