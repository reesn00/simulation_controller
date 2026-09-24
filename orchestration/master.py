"""orchestration.master: 新架构 (simulation server → gdr → etl) 主入口.

设计依据 ``docs/设计方案/pipeline-contracts.md`` §6.

边界:
* Master 仅持有配置 / queue / stop_event, **不直接起 worker 线程**;
  调度全部由 ``PipelineExecutor`` (multiprocessing.Pool) 完成.
* Master ``shutdown()`` 仅 set stop_event, 已在跑的子进程会跑完单个 task
  然后正常退出 (子进程跑一个 task = 一次 apply_async 调).
* ``status()`` 走 SQLite 直接读 ``tasks`` 表.
* ``run()`` 前后各调一次 ``write_health``, 落 ``log_dir/health.json``.

删除 (契约 §6.3 明确):
* start_workers / _add_thread / _start_batch_watcher / _first_scan_watcher
* wait_batch_drained / register_active_batch / unregister_active_batch
* _reaper_loop / _reaper_thread
* _active_batch_ids / _threads / _workers_started
* alive_workers / _count_terminal_for_batch / _run_one_batch
"""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from gdr.config.settings import Settings as GdrSettings

from orchestration.config_loader import OrchestrationConfig
from orchestration.health import collect_tasks, write_health
from orchestration.pipeline_executor import PipelineExecutor, PipelineSummary
from orchestration.queue import SQLiteQueue

_log = logging.getLogger(__name__)


class OrchestrationError(RuntimeError):
    """master 启动 / 运行期错误."""


# ---------------------------------------------------------------------------
# 主类
# ---------------------------------------------------------------------------


class Master:
    """orchestration 主入口 (新架构三阶段流水线)."""

    def __init__(self, cfg: OrchestrationConfig) -> None:
        self._cfg = cfg
        self._queue = SQLiteQueue(cfg.paths.sqlite_db)
        self._stop_event = threading.Event()

    # ------------------------------------------------------------------
    # 公开 API
    # ------------------------------------------------------------------

    def run(self, task_ids: list[str]) -> PipelineSummary:
        """创建 PipelineExecutor 跑完 task_ids, 返回 PipelineSummary.

        副作用:
            - run() 前 ``write_health`` (status=starting)
            - run() 后 ``write_health`` (done/dead 已统计)
        """
        # 启动前先落一次 health (空 / 启动态)
        try:
            write_health(
                self._queue,
                log_dir=Path(self._cfg.paths.log_dir),
                extra={"status": "running", "submitted": list(task_ids)},
            )
        except Exception as exc:
            _log.warning("master: pre-run write_health failed: %s", exc)

        executor = PipelineExecutor(
            queue=self._queue,
            settings=self._cfg.settings,
            paths=self._cfg.paths,
            gdr_settings=self._build_gdr_settings(),
        )
        try:
            summary = executor.run(task_ids)
        finally:
            # run 后落 health — 此时 tasks 表已全 done/dead/audited
            try:
                write_health(
                    self._queue,
                    log_dir=Path(self._cfg.paths.log_dir),
                    extra={
                        "status": "completed",
                        "summary": {
                            "total": summary.total,
                            "done": summary.done,
                            "dead": summary.dead,
                            "audited": summary.audited,
                            "duration_seconds": summary.duration_seconds,
                        },
                    },
                )
            except Exception as exc:
                _log.warning("master: post-run write_health failed: %s", exc)
        return summary

    def shutdown(self) -> None:
        """设置 stop_event; 正在跑的子进程仍会跑完当前 task."""
        self._stop_event.set()
        _log.info("master: shutdown signal set")

    def status(self) -> dict:
        """返回 ``{phases, total, last_updated}`` (契约 §6.2).

        数据源: ``SQLiteQueue.count_by_phase()``.
        """
        phases = collect_tasks(self._queue)
        phases["last_updated"] = datetime.now(timezone.utc).strftime(
            "%Y-%m-%dT%H:%M:%S.%fZ"
        )
        return phases

    # ------------------------------------------------------------------
    # 内部
    # ------------------------------------------------------------------

    def _build_gdr_settings(self) -> GdrSettings:
        """构造 gdr Settings 实例, workers=1, llm_concurrency 透传 (契约 §6.2)."""
        refined_dir = Path(self._cfg.paths.refined_dir)
        anchored = {
            "batch_output_dir": refined_dir,
            "output_path": refined_dir / "output.json",
            "deferred_output_path": refined_dir / "deferred.jsonl",
            "judge_low_output_path": refined_dir / "judge_low.jsonl",
            "routing_abstain_audit_path": refined_dir / "routing_low.jsonl",
            "log_dir": Path(self._cfg.paths.log_dir),
            "workers": 1,
            "max_files": 1,
        }
        # gdr_settings 是 pydantic BaseSettings, 用 model_copy 覆盖派生字段
        return self._cfg.gdr_settings.model_copy(update=anchored)