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

    # 清理旧 PID 文件（防止之前崩溃留下）
    if pid_file.exists():
        remove_pid_file(pid_file)

    if args.dry_run:
        print("[orchestration] dry-run:")
        print(f"  config         = {args.config}")
        print(f"  sqlite_db      = {sqlite_db}")
        print(f"  pid_file       = {pid_file}")
        print(f"  log_dir        = {log_dir}")
        print(f"  qf_workers     = {cfg.settings.qf_workers}")
        print(f"  gdr_workers    = {cfg.settings.gdr_workers}")
        print(f"  batch_size     = {cfg.settings.batch_size}")
        print(f"  detach         = {args.detach}")
        return 0

    if args.detach:
        # detach：当前进程 fork 出一个子进程；父进程立即返回
        handle = start_detached(
            argv=[sys.executable, "-m", "orchestration", "start",
                  "--config", str(args.config), "--foreground"],
            pid_file=pid_file,
            log_dir=log_dir,
            child_mode_arg="--foreground",
        )
        print(f"[orchestration] detached: pid={handle.pid} pid_file={pid_file} log={log_dir}")
        return 0

    # 前台
    def run(stop_event: threading.Event) -> None:
        queue = SQLiteQueue(
            sqlite_db,
            max_retry_qf=cfg.settings.max_retry_qf,
            max_retry_gdr=cfg.settings.max_retry_gdr,
        )
        master = Master(cfg=cfg, queue=queue)

        def _on_stop(_signum, _frame):
            stop_event.set()

        # 信号已经由 daemon.install_signal_handlers 设了；这里不必重复
        master.start_workers()
        try:
            # 跑空 batch 列表：master 只是常驻 + reap_stale
            # 实际批次由外部调度触发（replay / 手工调用）
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
