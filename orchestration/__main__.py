"""orchestration CLI 入口.

子命令（设计见 ``docs/orchestration-design.md`` §8）：

* ``start``   ：启动 master + workers；``--detach`` 后台化，默认前台
* ``status``  ：打印 health.json + queue_counts + dead 列表
* ``stop``    ：SIGTERM 给 master（PID 文件），超时后强杀
* ``replay``  ：``state=dead`` 的 task 重置为 ``pending``；``--batch N`` 仅限该批次

公共参数：
    ``--config PATH``：orchestration config.yaml 路径；默认 ``orchestration/config.yaml``
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
import threading
from pathlib import Path
from typing import Sequence

from orchestration.config_loader import OrchestrationConfig, load_config
from orchestration.daemon import (
    is_running,
    read_pid_file,
    remove_pid_file,
    setup_logging,
    start_detached,
    start_foreground,
    stop as daemon_stop,
    write_pid_file,
)
from orchestration.health import collect_batches, write_health
from orchestration.master import Master, OrchestrationError
from orchestration.queue import STATE_DEAD, SQLiteQueue

_log = logging.getLogger(__name__)


def _load_all_task_ids(config_path: str | None) -> list[str]:
    """从 simulate_serve config 加载全部 task_id 列表."""
    from simulate_serve.config import load_config as load_ss_config
    from simulate_serve.task_manager import TaskManager

    ss_cfg = load_ss_config(config_path)
    manager = TaskManager(
        ss_cfg.tasks_file,
        ss_cfg.scenarios_file,
        config_dir=ss_cfg.config_dir,
        max_guide_rounds=ss_cfg.max_guide_rounds,
    )
    return [t.task_id for t in manager.compiled_tasks]


def _split_batches(task_ids: list[str], batch_size: int) -> list[list[str]]:
    """按 batch_size 切分 task_ids 为多个批次."""
    if batch_size <= 0:
        return [list(task_ids)]
    return [list(task_ids[i:i + batch_size])
            for i in range(0, len(task_ids), batch_size)]


# ---------------------------------------------------------------------------
# 子命令
# ---------------------------------------------------------------------------


def _cmd_start(args: argparse.Namespace) -> int:
    cfg = load_config(args.config)
    pid_file = Path(cfg.paths.pid_file)
    log_dir = Path(cfg.paths.log_dir)
    sqlite_db = Path(cfg.paths.sqlite_db)

    alive, existing_pid = is_running(pid_file)
    if alive:
        print(f"[orchestration] already running: pid={existing_pid} (pid_file={pid_file})")
        return 1

    if pid_file.exists():
        remove_pid_file(pid_file)

    batch_size = args.batch_size if args.batch_size is not None else cfg.settings.batch_size

    task_ids: list[str] = []
    if args.tasks:
        task_ids = [t.strip() for t in args.tasks.split(",") if t.strip()]
    elif args.all_tasks:
        task_ids = _load_all_task_ids(cfg.paths.simulate_serve_config)

    batches = _split_batches(task_ids, batch_size) if task_ids else []

    if args.dry_run:
        print("[orchestration] dry-run:")
        print(f"  config         = {args.config}")
        print(f"  sqlite_db      = {sqlite_db}")
        print(f"  pid_file       = {pid_file}")
        print(f"  log_dir        = {log_dir}")
        print(f"  qf_workers     = {cfg.settings.qf_workers}")
        print(f"  gdr_workers    = {cfg.settings.gdr_workers}")
        print(f"  batch_size     = {batch_size}")
        print(f"  detach         = {args.detach}")
        if task_ids:
            print(f"  tasks          = {len(task_ids)} ({','.join(task_ids[:5])}{'...' if len(task_ids) > 5 else ''})")
            print(f"  batches        = {len(batches)}")
        else:
            print(f"  tasks          = (none, idle mode)")
        print(f"  exit_when_done = {args.exit_when_done}")
        return 0

    if args.detach:
        detach_argv = [sys.executable, "-m", "orchestration", "start",
                       "--config", str(args.config), "--foreground"]
        if args.tasks:
            detach_argv += ["--tasks", args.tasks]
        if args.all_tasks:
            detach_argv += ["--all-tasks"]
        if args.exit_when_done:
            detach_argv += ["--exit-when-done"]
        if args.batch_size is not None:
            detach_argv += ["--batch-size", str(args.batch_size)]
        handle = start_detached(
            argv=detach_argv,
            pid_file=pid_file,
            log_dir=log_dir,
            child_mode_arg="--foreground",
        )
        print(f"[orchestration] detached: pid={handle.pid} pid_file={pid_file} log={log_dir}")
        return 0

    def run(stop_event: threading.Event) -> None:
        queue = SQLiteQueue(
            sqlite_db,
            max_retry_qf=cfg.settings.max_retry_qf,
            max_retry_gdr=cfg.settings.max_retry_gdr,
        )
        master = Master(cfg=cfg, queue=queue)

        master.start_workers()
        try:
            if batches:
                _log.info("start: submitting %d batch(es), %d task(s) total",
                          len(batches), len(task_ids))
                summaries = master.run(batches)
                for s in summaries:
                    print(f"[orchestration] batch_id={s.batch_id} "
                          f"runs={len(s.run_ids)} drained={s.drained} dead={s.dead_count}")
                _log.info("start: all batches completed")

                if args.exit_when_done:
                    _log.info("start: --exit-when-done, shutting down")
                    return
            else:
                _log.info("start: idle mode, no tasks submitted")

            stop_event.wait()
        finally:
            master.shutdown(timeout=10.0)

    start_foreground(
        cfg_path=Path(args.config) if args.config else None,
        pid_file=pid_file, log_dir=log_dir, run_callback=run,
    )
    return 0


def _cmd_status(args: argparse.Namespace) -> int:
    cfg = load_config(args.config)
    pid_file = Path(cfg.paths.pid_file)
    sqlite_db = Path(cfg.paths.sqlite_db)
    health_path = Path(cfg.paths.log_dir) / "health.json"

    alive, pid = is_running(pid_file)
    print("[orchestration] status")
    print(f"  pid_file       = {pid_file}  (alive={alive}, pid={pid})")

    if sqlite_db.exists():
        queue = SQLiteQueue(sqlite_db)
        print(f"  sqlite_db      = {sqlite_db}")
        print(f"  queue_counts   = {queue.count_by_state()}")
        batches = collect_batches(queue)
        if batches:
            print(f"  batches        = {len(batches)}")
            for bid in sorted(batches):
                b = batches[bid]
                print(f"    batch={bid} status={b['status']} "
                      f"qf={b['qf_count']} gdr={b['gdr_count']} dead={b['dead_count']}")
        else:
            print("  batches        = (none)")
        dead_tasks = [t for t in queue.list_tasks_for_batch(-1)] if False else []
        # 简化：列出所有 state=dead 的 run_id
        with queue._conn() as conn:  # noqa: SLF001
            rows = conn.execute(
                "SELECT id, run_id, batch_id, error_msg FROM tasks "
                "WHERE state = ? ORDER BY id", (STATE_DEAD,),
            ).fetchall()
        if rows:
            print(f"  dead_tasks     = {len(rows)}")
            for r in rows:
                err = (r["error_msg"] or "").splitlines()[0][:60] if r["error_msg"] else ""
                print(f"    task_id={r['id']} run_id={r['run_id']} "
                      f"batch={r['batch_id']} err={err!r}")
        else:
            print("  dead_tasks     = 0")
    else:
        print(f"  sqlite_db      = (missing: {sqlite_db})")

    if health_path.exists():
        try:
            data = json.loads(health_path.read_text(encoding="utf-8"))
            print(f"  health.json    = {health_path}  (last_updated={data.get('last_updated')})")
        except (OSError, json.JSONDecodeError) as exc:
            print(f"  health.json    = (unreadable: {exc})")
    else:
        print(f"  health.json    = (missing: {health_path})")

    return 0


def _cmd_stop(args: argparse.Namespace) -> int:
    cfg = load_config(args.config)
    pid_file = Path(cfg.paths.pid_file)
    alive, pid = is_running(pid_file)
    if not alive:
        print(f"[orchestration] not running (pid_file={pid_file})")
        if pid_file.exists():
            remove_pid_file(pid_file)
        return 0
    print(f"[orchestration] stopping pid={pid} ...")
    ok = daemon_stop(pid_file, timeout=args.timeout)
    if ok:
        print(f"[orchestration] stopped pid={pid}")
        return 0
    print(f"[orchestration] stop timed out (pid={pid})")
    return 1


def _cmd_replay(args: argparse.Namespace) -> int:
    cfg = load_config(args.config)
    sqlite_db = Path(cfg.paths.sqlite_db)
    if not sqlite_db.exists():
        print(f"[orchestration] sqlite_db missing: {sqlite_db}")
        return 1
    queue = SQLiteQueue(sqlite_db)
    n = queue.requeue_dead(batch_id=args.batch)
    target = f"batch={args.batch}" if args.batch is not None else "all"
    print(f"[orchestration] replay {target}: requeued {n} dead task(s)")
    # 顺便刷一次 health
    try:
        write_health(queue, output_path=Path(cfg.paths.log_dir) / "health.json")
    except Exception:
        pass
    return 0


# ---------------------------------------------------------------------------
# parser
# ---------------------------------------------------------------------------


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="orchestration",
        description="simulate_serve / qwenformat / gdr 三阶段流水线调度器",
    )
    parser.add_argument(
        "--config", type=str, default=None,
        help="orchestration config.yaml 路径；默认包内 config.yaml",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_start = sub.add_parser("start", help="启动 master + workers")
    p_start.add_argument("--detach", action="store_true",
                         help="后台化（子进程），返回立即")
    p_start.add_argument("--foreground", action="store_true",
                         help="前台运行（默认；detach 子进程会传此标志）")
    p_start.add_argument("--dry-run", action="store_true",
                         help="仅打印计划，不真的启动")
    p_start.add_argument("--tasks", type=str, default=None,
                         help="逗号分隔的 task_id 列表；指定后启动即提交批次")
    p_start.add_argument("--all-tasks", action="store_true",
                         help="加载 simulate_serve 全部 task_id 并提交")
    p_start.add_argument("--exit-when-done", action="store_true",
                         help="批次跑完后退出；不指定则继续常驻")
    p_start.add_argument("--batch-size", type=int, default=None,
                         help="覆盖 config 中的 batch_size")
    p_start.set_defaults(func=_cmd_start)

    p_status = sub.add_parser("status", help="打印队列/进程/dead 状态")
    p_status.set_defaults(func=_cmd_status)

    p_stop = sub.add_parser("stop", help="优雅停止")
    p_stop.add_argument("--timeout", type=float, default=10.0,
                        help="超时秒数（默认 10）")
    p_stop.set_defaults(func=_cmd_stop)

    p_replay = sub.add_parser("replay", help="dead 任务重新入队")
    p_replay.add_argument("--batch", type=int, default=None,
                          help="仅重放指定 batch_id 的 dead；缺省 = 全部")
    p_replay.set_defaults(func=_cmd_replay)

    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
