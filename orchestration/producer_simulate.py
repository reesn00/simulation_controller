"""orchestration.producer_simulate: 包装 ``simulate_serve.bootstrap.build_application``
跑一批 ``CompiledTask`` 并写 SQLite ``batches`` 表.

设计见 ``docs/orchestration-design.md`` §6.1。

职责：
    1. 加载 ``simulate_serve/config/config.yaml``
    2. ``build_application(config)`` 拿到 services（含 BatchRunner）
    3. 按 ``task_ids`` 从 ``TaskManager.compiled_tasks`` 找对应 ``CompiledTask``
    4. ``queue.insert_batch(task_ids_str)`` 拿到 batch_id
    5. ``queue.update_batch(simulate_started_at=...)``
    6. ``await batch_runner.run(tasks, limit=limit)`` → ``list[TaskRun]``
    7. ``queue.update_batch(simulate_done_at=...)``
    8. 返回 ``(batch_id, [TaskRun])``

边界：
    - 不调 trajectory_archiver；trajectory 是 simulate_serve 内部 copy 的
    - 不写 run.json；由 ``JsonRunRepository.save_run`` 负责
    - 不轮询终态；那是 ``batch_tracker.wait_for_terminal`` 的职责（master 调）
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Sequence

from simulate_serve.bootstrap import build_application
from simulate_serve.config import AppConfig, load_config
from simulate_serve.domain.run import TaskRun
from simulate_serve.domain.task import CompiledTask

from orchestration.queue import SQLiteQueue

_log = logging.getLogger(__name__)


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _select_tasks(
    catalog: Sequence[CompiledTask],
    task_ids: Sequence[str],
) -> list[CompiledTask]:
    """按 task_id 从 catalog 中筛 task；找不到的抛 KeyError."""
    by_id = {t.task_id: t for t in catalog}
    missing = [tid for tid in task_ids if tid not in by_id]
    if missing:
        raise KeyError(f"task_id(s) not found in catalog: {missing}")
    return [by_id[tid] for tid in task_ids]


async def _async_run_batch(
    *,
    config_path: str | Path,
    task_ids: list[str],
    limit: int,
    queue: SQLiteQueue,
) -> tuple[int, list[TaskRun]]:
    """async 入口；由 ``run_batch`` 顶层包装 ``asyncio.run``."""
    cfg: AppConfig = load_config(str(config_path))
    services = await build_application(cfg)
    try:
        tasks = _select_tasks(services.task_manager.compiled_tasks, task_ids)
        batch_id = queue.insert_batch([t.task_id for t in tasks])
        queue.update_batch(batch_id, simulate_started_at=_utc_now_iso())
        _log.info(
            "producer_simulate: batch_id=%d start tasks=%d limit=%d",
            batch_id, len(tasks), limit,
        )
        runs = await services.batch_runner.run(tasks, limit=limit)
        queue.update_batch(batch_id, simulate_done_at=_utc_now_iso())
        _log.info(
            "producer_simulate: batch_id=%d done runs=%d terminal=%d",
            batch_id, len(runs),
            sum(1 for r in runs if r.is_terminal),
        )
        return batch_id, runs
    finally:
        await services.close()


def run_batch(
    *,
    config_path: str | Path,
    task_ids: list[str],
    limit: int,
    queue: SQLiteQueue,
) -> tuple[int, list[TaskRun]]:
    """同步入口；用 ``asyncio.run`` 跑 async 链路.

    Parameters
    ----------
    config_path:
        ``simulate_serve/config/config.yaml`` 路径
    task_ids:
        本批要跑的 task_id 列表
    limit:
        实际跑的 task 数（``BatchRunner.run(limit=...)``）；<=0 表示不限
    queue:
        SQLite 队列；用于写 batches 表

    Returns
    -------
    ``(batch_id, [TaskRun])``
    """
    return asyncio.run(
        _async_run_batch(
            config_path=config_path,
            task_ids=list(task_ids),
            limit=int(limit),
            queue=queue,
        )
    )
