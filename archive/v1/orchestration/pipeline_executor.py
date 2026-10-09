"""orchestration.pipeline_executor: 按 ``max_parallelism`` 调度 N 个 task
的单 task 三阶段流水线 (新架构 simulation server → gdr → etl).

设计依据 ``docs/设计方案/pipeline-contracts.md`` §5.

核心数据结构 ``PipelineSummary`` + ``PipelineExecutor``:

    * ``PipelineExecutor.run(task_ids)`` 起 ``multiprocessing.Pool``,维护
      ``in_flight: dict[AsyncResult, str]`` 槽位填充,直到所有 task 出结果。
    * 单 task 三阶段 (simulate → gdr → etl) 由子进程入口
      ``task_pipeline._run_one_task_pipeline`` 完整跑完。
    * 子进程崩溃被 ``future.get()`` 抛 ``Exception``,主进程捕获并 dead++,继续。
    * 子进程返回 ``dict{phase, stage, error}``;``phase="done"`` 计入 done,
      其余(含异常 / 标 dead)计入 dead。
"""

from __future__ import annotations

import logging
import multiprocessing
import time
from dataclasses import dataclass
from multiprocessing.pool import AsyncResult
from pathlib import Path
from typing import Any

from gdr.config.settings import Settings as GdrSettings

from orchestration._windows import install_no_window_policy
from orchestration.queue import (
    PHASE_AUDITED,
    PHASE_DEAD,
    SQLiteQueue,
    TaskAlreadyTerminal,
)
from orchestration.settings import Paths, PipelineSettings
from orchestration.task_pipeline import (
    _run_one_task_pipeline,
    _worker_init,
)

_log = logging.getLogger(__name__)

# Pool 创建前幂等安装:Windows 禁止 spawn worker 弹 cmd 窗口
# (CPython 3.12 multiprocessing 默认行为会弹,见 orchestration/_windows.py)。
install_no_window_policy()


@dataclass(frozen=True)
class PipelineSummary:
    """``PipelineExecutor.run`` 的返回 (契约 §5.2).

    字段:
        total:           提交的总 task 数
        done:            phase=done 的 task 数
        dead:            phase=dead 的 task 数 (含子进程崩溃兜底)
        audited:         phase=audited 的 task 数 (评分低但结构合格,
                         CLAUDE.md "数据保留原则")
        duration_seconds: 主循环总耗时
    """

    total: int
    done: int
    dead: int
    audited: int
    duration_seconds: float


class PipelineExecutor:
    """按 ``max_parallelism`` 调度 N 个 task 的 simulate→gdr→etl 流水线.

    构造:
        queue:       SQLiteQueue 实例 (主进程持有)
        settings:    PipelineSettings 实例
        paths:       Paths 实例 (透传给子进程)
        gdr_settings: gdr.Settings 实例 (透传给子进程;workers=1, llm_concurrency 透传)

    入口:
        run(task_ids: list[str]) -> PipelineSummary

    关闭:
        shutdown() — 关闭 pool;若 run 已正常返回则本方法 no-op。
    """

    def __init__(
        self,
        *,
        queue: SQLiteQueue,
        settings: PipelineSettings,
        paths: Paths,
        gdr_settings: GdrSettings,
    ) -> None:
        self._queue = queue
        self._settings = settings
        self._paths = paths
        self._gdr_settings = gdr_settings
        self._process_pool: multiprocessing.Pool | None = None

    # ------------------------------------------------------------------
    # 公开 API
    # ------------------------------------------------------------------

    def run(self, task_ids: list[str]) -> PipelineSummary:
        """按并行度调度 task_ids 全跑完,返回 PipelineSummary.

        抛:
            ValueError: task_ids 为空

        抛但不致命:
            子进程内未捕获异常 → ``future.get()`` 抛 ``Exception``,
            主进程捕获 → dead++, 继续下一个 task。
        """
        if not task_ids:
            raise ValueError("task_ids must be non-empty")

        parallelism = max(1, int(self._settings.max_parallelism))
        start_ts = time.monotonic()

        self._process_pool = multiprocessing.Pool(
            processes=parallelism,
            initializer=_worker_init,
            initargs=(self._paths,),
        )
        try:
            summary = self._dispatch(task_ids)
        finally:
            self._process_pool.close()
            self._process_pool.join()
            self._process_pool = None

        duration = time.monotonic() - start_ts
        return PipelineSummary(
            total=len(task_ids),
            done=summary["done"],
            dead=summary["dead"],
            audited=summary["audited"],
            duration_seconds=duration,
        )

    def shutdown(self) -> None:
        """显式关闭 pool;正常 ``run`` 返回后调本方法是 no-op."""
        if self._process_pool is not None:
            self._process_pool.close()
            self._process_pool.join()
            self._process_pool = None

    # ------------------------------------------------------------------
    # 主循环:槽位填充 + 收集结果
    # ------------------------------------------------------------------

    def _dispatch(self, task_ids: list[str]) -> dict[str, int]:
        """维护 in_flight 槽位;填槽 + 收集,直到 pending 列表空且 in_flight 空.

        返回:
            {"done": int, "dead": int, "audited": int} — 仅统计,不含 total/duration
            (那两项由 ``run`` 包装)。
        """
        in_flight: dict[AsyncResult, str] = {}
        pending: list[str] = list(task_ids)
        done_count = 0
        dead_count = 0
        audited_count = 0

        def _fill_slots() -> None:
            """当槽位未满且 pending 非空时,投递下一个 task."""
            nonlocal done_count
            while (
                len(in_flight) < self._settings.max_parallelism
                and pending
            ):
                task_id = pending.pop(0)
                # upsert_task 可能在已 terminal 时抛 TaskAlreadyTerminal
                # (契约 §2.5);视为该 task 已被别人处理完,不计入 dead。
                try:
                    self._queue.upsert_task(task_id)
                except TaskAlreadyTerminal as exc:
                    _log.info(
                        "pipeline_executor: skip %s (already %s)",
                        task_id, exc.current_phase,
                    )
                    done_count += 1
                    continue
                future = self._process_pool.apply_async(
                    _run_one_task_pipeline,
                    (
                        task_id,
                        self._paths,
                        self._gdr_settings,
                        self._settings,
                    ),
                )
                in_flight[future] = task_id

        _fill_slots()
        while in_flight:
            # 收集已完成的 future
            for future in list(in_flight):
                if not future.ready():
                    continue
                task_id = in_flight.pop(future)
                try:
                    result = future.get(timeout=0)
                except Exception as exc:
                    # 子进程崩溃/未捕获异常 → dead++ + 兜底 mark_failed
                    dead_count += 1
                    _log.error(
                        "pipeline_executor: task %s pipeline crashed: %s",
                        task_id, exc,
                    )
                    try:
                        self._queue.mark_failed(
                            task_id, stage="simulate",
                            error_msg=f"pipeline crashed: {type(exc).__name__}: {exc}",
                        )
                    except Exception as mark_exc:
                        _log.warning(
                            "pipeline_executor: mark_failed after crash failed for %s: %s",
                            task_id, mark_exc,
                        )
                    continue
                # 正常返回 dict;按 phase 分类 (done / audited / 其他 → dead)
                if isinstance(result, dict):
                    phase = result.get("phase")
                    if phase == "done":
                        done_count += 1
                    elif phase == PHASE_AUDITED:
                        # 评分低但结构合格 → audited, 不计 dead
                        # (CLAUDE.md "数据保留原则")
                        audited_count += 1
                    else:
                        dead_count += 1
                else:
                    dead_count += 1
            # 填新槽
            _fill_slots()
            # 短睡避免忙等
            if in_flight:
                time.sleep(0.05)

        return {"done": done_count, "dead": dead_count, "audited": audited_count}