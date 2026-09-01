"""orchestration.master: 主循环 — 串起 producer / watcher / qf / gdr workers.

设计见 ``docs/orchestration-design.md`` §6.1、§9。

进程模型：
    * 主进程跑主循环
    * qf / gdr 用常驻 Thread + ``stop_event`` 关闭
    * watcher 按批次起新 Thread，每批独立 stop_event
    * ``reap_stale`` 用独立 Thread 定时跑

主循环每批：
    1. ``producer_simulate.run_batch(task_ids, ...)`` → ``(batch_id, [TaskRun])``
    2. ``batch_tracker.wait_for_terminal(run_ids)``（防御性二次确认）
    3. 启动本批 watcher（轮询 trajectory_dir → SQLite，batch_id 锁定）
    4. ``wait_batch_drained(batch_id)``：本批所有 task 走到 ``done`` 或 ``dead``
    5. 关闭本批 watcher
    6. ``reap_dead(...)`` 把 dead 产物归档
    7. ``write_health(...)``

边界：
    * ``producer_simulate`` 是阻塞调用，跑在主线程
    * worker / watcher / reap_stale 异常不会让主循环死；run_forever 内 try/except 已覆盖
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Sequence

from gdr.config.settings import Settings as GdrSettings

from orchestration.batch_tracker import wait_for_terminal
from orchestration.config_loader import OrchestrationConfig
from orchestration.failure_handler import reap_dead
from orchestration.health import write_health
from orchestration.producer_simulate import run_batch as producer_run_batch
from orchestration.queue import (
    STATE_DEAD,
    STATE_DONE,
    STATE_PENDING,
    STATE_PENDING_GDR,
    SQLiteQueue,
)
from orchestration.watcher import TrajectoryWatcher
from orchestration.workers.gdr_worker import GdrWorker
from orchestration.workers.qf_worker import QfWorker

_log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# 主类
# ---------------------------------------------------------------------------


class OrchestrationError(RuntimeError):
    """master 启动 / 运行期错误."""


@dataclass(frozen=True)
class BatchSummary:
    batch_id: int
    run_ids: tuple[str, ...]
    drained: bool
    dead_count: int


# producer_runner 签名：(config_path, task_ids, limit, queue) → (batch_id, [TaskRun])
ProducerRunner = Callable[..., tuple[int, list]]


class Master:
    """orchestration 主循环."""

    def __init__(
        self,
        *,
        cfg: OrchestrationConfig,
        queue: SQLiteQueue,
        producer_runner: ProducerRunner | None = None,
        gdr_settings: GdrSettings | None = None,
    ) -> None:
        self._cfg = cfg
        self._queue = queue
        self._producer_runner: ProducerRunner = producer_runner or self._default_producer
        self._gdr_settings = gdr_settings

        self._stop_event = threading.Event()
        self._threads: list[tuple[str, threading.Thread, threading.Event]] = []
        self._reaper_thread: threading.Thread | None = None
        self._workers_started = False

    # ------------------------------------------------------------------
    # 启动 / 关闭
    # ------------------------------------------------------------------

    def start_workers(self) -> None:
        """启动 qf × K、gdr × M 常驻 worker 和 reap_stale 周期."""
        if self._workers_started:
            raise OrchestrationError("workers already started")
        self._workers_started = True

        s = self._cfg.settings
        qf_out = Path(self._cfg.paths.qf_output_dir)
        gdr_out = Path(self._cfg.paths.gdr_output_dir)
        # worker 内 poll 用一个保守的间隔；master 主循环会用 batch_drain_poll_seconds
        # 等终态，所以这里不必太快；过长只是首次响应慢一点
        worker_poll = min(0.1, s.batch_drain_poll_seconds)

        for i in range(s.qf_workers):
            w = QfWorker(
                queue=self._queue, worker_id=f"qf_{i}",
                qf_output_dir=qf_out, n=1, poll_seconds=worker_poll,
            )
            self._add_thread(f"qf_{i}", w)

        for i in range(s.gdr_workers):
            w = GdrWorker(
                queue=self._queue, worker_id=f"gdr_{i}",
                gdr_output_dir=gdr_out,
                gdr_settings=self._build_gdr_settings(),
                llm_concurrency=self._cfg.gdr.llm_concurrency,
                n=1, poll_seconds=worker_poll,
            )
            self._add_thread(f"gdr_{i}", w)

        # reap_stale 周期
        self._reaper_thread = threading.Thread(
            target=self._reaper_loop, name="reaper", daemon=True,
        )
        self._reaper_thread.start()
        _log.info("master: started %d worker(s)", len(self._threads))

    def _add_thread(self, name: str, worker) -> None:
        """起一个 worker thread；用独立 stop_event 便于按 worker 关闭."""
        ev = threading.Event()
        t = threading.Thread(
            target=worker.run_forever, args=(ev,),
            name=f"worker-{worker.__class__.__name__}-{name}", daemon=True,
        )
        t.start()
        self._threads.append((name, t, ev))

    def _build_gdr_settings(self) -> GdrSettings:
        if self._gdr_settings is not None:
            return self._gdr_settings.model_copy(update={
                "batch_output_dir": Path(self._cfg.paths.gdr_output_dir),
                "workers": 1,
                "max_files": 1,
            })
        g = self._cfg.gdr
        return GdrSettings(
            batch_output_dir=Path(self._cfg.paths.gdr_output_dir),
            workers=1,
            llm_concurrency=g.llm_concurrency,
            max_files=1,
        )

    def shutdown(self, *, timeout: float = 10.0) -> None:
        """设置所有 stop_event 并 join."""
        for name, _t, ev in self._threads:
            ev.set()
        if self._stop_event is not None:
            self._stop_event.set()
        deadline = time.monotonic() + timeout
        for name, t, _ev in self._threads:
            remaining = max(0.0, deadline - time.monotonic())
            t.join(timeout=remaining)
            if t.is_alive():
                _log.warning("master: worker %s did not exit in time", name)
        if self._reaper_thread is not None:
            self._reaper_thread.join(timeout=2.0)
        _log.info("master: shutdown complete")

    # ------------------------------------------------------------------
    # 主循环
    # ------------------------------------------------------------------

    def run(
        self,
        task_batches: Sequence[Sequence[str]],
    ) -> list[BatchSummary]:
        """按 ``task_batches`` 顺序跑每批；返回 BatchSummary 列表."""
        if not self._workers_started:
            self.start_workers()

        summaries: list[BatchSummary] = []
        for batch in task_batches:
            if self._stop_event.is_set():
                _log.info("master: stop requested, break batch loop")
                break
            summaries.append(self._run_one_batch(list(batch)))
        return summaries

    def _run_one_batch(self, task_ids: list[str]) -> BatchSummary:
        s = self._cfg.settings
        config_path = self._cfg.paths.simulate_serve_config
        runs_dir = Path(self._cfg.paths.runs_dir)

        # 1. producer
        batch_id, runs = self._producer_runner(
            config_path=config_path, task_ids=task_ids,
            limit=s.batch_size, queue=self._queue,
        )
        run_ids = tuple(r.run_id for r in runs)

        # 2. 防御性二次确认 run.json 终态
        wait_for_terminal(
            list(run_ids), runs_dir=runs_dir,
            poll_seconds=s.batch_drain_poll_seconds, timeout=None,
        )

        # 3. 启动本批 watcher
        watcher_stop = self._start_batch_watcher(batch_id)

        try:
            # 4. 等 SQLite 本批全 done / dead
            drained = self.wait_batch_drained(
                batch_id,
                poll_seconds=s.batch_drain_poll_seconds,
                timeout=s.batch_drain_timeout_seconds,
            )
        finally:
            watcher_stop.set()
            _log.info("master: batch_id=%d watcher stopped", batch_id)

        # 5. 死信归档
        archives = reap_dead(
            self._queue,
            dead_dir=Path(self._cfg.paths.dead_dir),
            dead_log_path=Path(self._cfg.paths.log_dir) / "dead.log",
        )
        dead_count = sum(1 for a in archives if a.moved_to)
        self._queue.update_batch(
            batch_id,
            dead_count=dead_count,
            qf_count=self._count_terminal_for_batch(batch_id, STATE_DONE)
            + self._count_terminal_for_batch(batch_id, STATE_DEAD),
            status="done",
        )

        # 6. 写 health.json
        try:
            write_health(
                self._queue,
                output_path=Path(self._cfg.paths.log_dir) / "health.json",
            )
        except Exception as exc:
            _log.warning("master: write_health failed: %s", exc)

        return BatchSummary(
            batch_id=batch_id, run_ids=run_ids,
            drained=drained, dead_count=dead_count,
        )

    def _count_terminal_for_batch(self, batch_id: int, state: str) -> int:
        tasks = self._queue.list_tasks_for_batch(batch_id)
        return sum(1 for t in tasks if t.state == state)

    # ------------------------------------------------------------------
    # watcher 按批
    # ------------------------------------------------------------------

    def _start_batch_watcher(self, batch_id: int) -> threading.Event:
        s = self._cfg.settings
        w = TrajectoryWatcher(
            trajectory_dir=Path(self._cfg.paths.trajectory_dir),
            queue=self._queue, batch_id=batch_id,
            poll_seconds=s.watcher_poll_seconds,
            dead_log_path=Path(self._cfg.paths.log_dir) / "watcher_dead.log",
        )
        stop_ev = threading.Event()
        t = threading.Thread(
            target=w.run_forever, args=(stop_ev,),
            name=f"watcher-{batch_id}", daemon=True,
        )
        t.start()
        self._threads.append((f"watcher-{batch_id}", t, stop_ev))
        return stop_ev

    # ------------------------------------------------------------------
    # batch_drained 轮询
    # ------------------------------------------------------------------

    def wait_batch_drained(
        self,
        batch_id: int,
        *,
        poll_seconds: float = 5.0,
        timeout: float | None = None,
    ) -> bool:
        """本批所有 task 全部 ``done`` 或 ``dead`` 才返回 True.

        timeout 非 None 时超返回 False 不抛。
        """
        deadline = time.monotonic() + timeout if timeout else None
        while True:
            tasks = self._queue.list_tasks_for_batch(batch_id)
            if not tasks:
                return True
            if all(t.state in (STATE_DONE, STATE_DEAD) for t in tasks):
                return True
            if deadline is not None and time.monotonic() >= deadline:
                _log.warning(
                    "master: batch_id=%d not drained after %.1fs "
                    "(pending=%d, pending_gdr=%d, dead=%d)",
                    batch_id, timeout or 0.0,
                    sum(1 for t in tasks if t.state == STATE_PENDING),
                    sum(1 for t in tasks if t.state == STATE_PENDING_GDR),
                    sum(1 for t in tasks if t.state == STATE_DEAD),
                )
                return False
            time.sleep(poll_seconds)

    # ------------------------------------------------------------------
    # reap_stale 周期
    # ------------------------------------------------------------------

    def _reaper_loop(self) -> None:
        s = self._cfg.settings
        while not self._stop_event.wait(s.reap_stale_interval_seconds):
            try:
                n = self._queue.reap_stale(older_than_seconds=s.reap_stale_seconds)
                if n:
                    _log.info("master: reaped %d stale lock(s)", n)
            except Exception as exc:
                _log.exception("master: reaper failed: %s", exc)

    # ------------------------------------------------------------------
    # 默认 producer
    # ------------------------------------------------------------------

    def _default_producer(
        self, *, config_path, task_ids, limit, queue,
    ) -> tuple[int, list]:
        return producer_run_batch(
            config_path=config_path, task_ids=list(task_ids),
            limit=int(limit), queue=queue,
        )

    # ------------------------------------------------------------------
    # 状态辅助
    # ------------------------------------------------------------------

    @property
    def alive_workers(self) -> tuple[str, ...]:
        return tuple(name for name, t, _ev in self._threads if t.is_alive())
