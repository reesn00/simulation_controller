"""orchestration.producer_simulate: ST-4 simulate 阶段单 task 入口.

新架构下 producer 是无状态 async 函数, 由 ST-5 PipelineExecutor 在子
进程入口 ``_run_one_task_pipeline`` 内调 ``asyncio.run`` 包装::

    run = await run_one_task(task_id, *, config_path=...)

不再有 ``run_batch`` / ``_split_batches`` / SQLiteQueue 写 / ``limit``
参数. 调度 / 重试 / 多阶段归属 (cross-stage phase 推进) 全部归
PipelineExecutor (调 SQLiteQueue.mark_phase).

执行链::

    config_path -> load_config -> AppConfig
                -> build_application(cfg) -> ApplicationServices
                -> services.task_manager.compiled_tasks -> 找 task_id
                -> services.batch_runner.run([task]) -> [TaskRun]
                -> 返 runs[0]
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path

from simulate_serve.bootstrap import build_application
from simulate_serve.config import AppConfig, load_config
from simulate_serve.domain.run import TaskRun
from simulate_serve.domain.task import CompiledTask

_log = logging.getLogger(__name__)


# ----------------------------------------------------------------------
# 模块函数 (契约 §4.2)
# ----------------------------------------------------------------------


async def run_one_task(
    task_id: str,
    *,
    config_path: Path,
) -> TaskRun:
    """in-process 跑 simulate_serve 单 task, 返回 ``TaskRun``.

    流程 (契约 §4.2):
      1. 读 ``config_path`` 构造 ``AppConfig``
      2. ``build_application(config)`` 构造 ``ApplicationServices``
      3. 从 ``services.task_manager.compiled_tasks`` 找 ``task_id``
         对应 ``CompiledTask``
      4. ``services.batch_runner.run([task])`` 跑单 task
      5. 返 ``runs[0]``

    Parameters
    ----------
    task_id:
        Catalog 内的 task_id, 由 ``task_manager.compiled_tasks`` 提供.
    config_path:
        simulate_serve 配置文件路径 (统一根配置 ``config/config.yaml`` 或
        独立 ``simulate_serve`` 段). 由 ST-5 透传 ``Paths.simulate_serve_config``.

    Returns
    -------
    ``TaskRun``:
      - ``run.run_id``: 用于 PipelineExecutor 拼 ``src_path`` (run 元数据 + trajectory)
      - ``run.remote_session_id``: 同上
      - ``run.state``: 由 BatchRunner / TaskRuntime 判定 (含终态 FAIL)

    Raises
    ------
    KeyError:
        ``task_id`` 不在 ``services.task_manager.compiled_tasks`` (catalog
        与 config 不一致). 调用方应直接标 dead (无 task 可跑).
    Exception:
        ``simulate_serve`` 任意未捕获异常. 调用方 (PipelineExecutor) 应
        走 ``mark_failed(stage=simulate)`` 兜底.

    Side Effects (契约 §4.2)
    ------------------------
    * ``output/runs/<run_id>/run.json`` 已落盘 (JsonRunRepository)
    * ``output/agent_trajectory/<run_id>__<session_id>.json`` 已落盘
      (QwenPawTrajectoryArchiver, 启用时)
    * ``SQLite tasks.phase`` 仍为 ``pending`` (由 PipelineExecutor 调
      ``mark_phase`` 推进; 本函数不写 SQLite).
    """
    cfg: AppConfig = load_config(str(config_path))
    services = await build_application(cfg)
    try:
        task = _find_task(services.task_manager.compiled_tasks, task_id)
        runs = await services.batch_runner.run([task])
    finally:
        await services.close()

    if not runs:
        # 理论上 BatchRunner.run 总会返回长度 = 选中任务数 = 1 的 list,
        # 但理论上空 list 也算"未跑通", 转 KeyError 让调用方标 dead.
        raise KeyError(
            f"run_one_task: BatchRunner.run returned empty list for task_id={task_id!r}"
        )

    run = runs[0]
    _log.info(
        "run_one_task: task_id=%s run_id=%s state=%s terminal=%s",
        task_id, run.run_id, run.state, run.is_terminal,
    )
    return run


# ----------------------------------------------------------------------
# 工具
# ----------------------------------------------------------------------


def _find_task(catalog: list[CompiledTask], task_id: str) -> CompiledTask:
    """从 catalog 找 task_id 对应 ``CompiledTask``; 缺失抛 KeyError.

    契约 §4.2 写"task_id 不在 catalog → KeyError"; 用 ``KeyError`` 而非
    ``ValueError`` 是因为 catalog 是索引语义, 与 dict 取键一致.
    """
    by_id = {t.task_id: t for t in catalog}
    if task_id not in by_id:
        raise KeyError(f"task_id not found in catalog: {task_id!r}")
    return by_id[task_id]


# ----------------------------------------------------------------------
# 异步入口 (供子进程入口调)
# ----------------------------------------------------------------------


def run_one_task_sync(task_id: str, *, config_path: Path) -> TaskRun:
    """``asyncio.run`` 包装版; 供 ST-5 子进程入口 ``_run_one_task_pipeline`` 调.

    PipelineExecutor 子进程入口在子进程内 pickle / spawn 重启, 不能直接
    ``await``; 用 ``asyncio.run`` 跑 async 链路. 主进程代码 (ST-5 之前)
    仍可调 ``await run_one_task(...)`` (ST-5 之后的 worker 入口是
    multiprocessing.Pool.apply_async, 但子进程内调本函数即可).
    """
    return asyncio.run(run_one_task(task_id, config_path=Path(config_path)))