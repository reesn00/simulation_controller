"""orchestration CLI 入口 (新架构 simulation server → gdr → etl).

设计依据 ``docs/设计方案/pipeline-contracts.md`` §7.

子命令:

* ``start``   : 启动 master 跑流水线; ``--detach`` 后台化, 默认前台
* ``status``  : 打印 phases 分布 + 最近 10 task
* ``stop``    : 写 STOP 哨兵文件让 master 优雅 shutdown; 超时后强杀
* ``replay``  : ``phase=dead`` 的 task 重置为 ``pending``

公共参数:
    ``--config PATH`` : 配置 yaml 路径; 默认读仓库根 ``config/config.yaml``
        (统一配置入口, SIMCTL_CONFIG env 可重定向), 缺失时报错 (无兜底)

start 选项 (契约 §7.2):
    ``--tasks T1,T2,T3``       : 可选, 逗号分隔子集过滤
    ``--all-tasks``            : 默认行为, 拉全 catalog
    ``--parallelism N``        : 默认 1, 设 ≥2 启用子进程并行
    ``--detach`` / ``--foreground``
    ``--dry-run``              : 只打印计划, 不真跑
    ``--stay``                 : 跑完不退出 master
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Sequence

from orchestration.config_loader import OrchestrationConfig, load_config
from orchestration.daemon import (
    is_running,
    remove_pid_file,
    start_detached,
    start_foreground,
    stop as daemon_stop,
)
from orchestration.health import collect_tasks
from orchestration.master import Master
from orchestration.queue import SQLiteQueue

_log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# 工具
# ---------------------------------------------------------------------------


def _load_all_task_ids(config_path: str | None) -> list[str]:
    """从 simulate_serve config 加载全部 task_id 列表.

    注: 当 catalog 文件不存在 / 解析失败时返空列表; dry-run 路径不要求
    catalog 必须可用, 真实跑任务时再抛错。
    """
    try:
        from simulate_serve.config import load_config as load_ss_config
        from simulate_serve.task_manager import TaskManager
    except Exception:
        return []
    try:
        ss_cfg = load_ss_config(config_path)
    except Exception as exc:
        _log.warning("orchestration: load simulate_serve config failed: %s", exc)
        return []
    try:
        manager = TaskManager(
            ss_cfg.tasks_file,
            ss_cfg.scenarios_file,
            config_dir=ss_cfg.config_dir,
            max_guide_rounds=ss_cfg.max_guide_rounds,
        )
        return [t.task_id for t in manager.compiled_tasks]
    except Exception as exc:
        _log.warning("orchestration: load task catalog failed: %s", exc)
        return []


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


# ---------------------------------------------------------------------------
# 子命令实现
# ---------------------------------------------------------------------------


def _cmd_start(args: argparse.Namespace, cfg: OrchestrationConfig) -> int:
    """``start`` 子命令入口 (契约 §7.3)."""
    pid_file = Path(cfg.paths.pid_file)
    log_dir = Path(cfg.paths.log_dir)
    sqlite_db = Path(cfg.paths.sqlite_db)

    alive, existing_pid = is_running(pid_file)
    if alive:
        print(
            f"[orchestration] already running: pid={existing_pid} (pid_file={pid_file})"
        )
        return 1

    if pid_file.exists():
        remove_pid_file(pid_file)

    # 解析 task_ids (契约 §7.3)
    task_ids: list[str] = []
    if args.tasks:
        task_ids = [t.strip() for t in args.tasks.split(",") if t.strip()]
    else:
        # 既不传 --tasks 也未传 --all-tasks 时, 默认 --all-tasks 行为
        task_ids = _load_all_task_ids(cfg.paths.simulate_serve_config)

    if not task_ids:
        if args.dry_run:
            print("[orchestration] dry-run: no task_ids resolved (catalog empty?)")
            print(f"  parallelism    = {args.parallelism or cfg.settings.max_parallelism}")
            print(f"  sqlite_db      = {sqlite_db}")
            return 0
        # 前台 (含 detached 子进程): 必须起 master 等待停信号 — 即便没任务也走 idle.
        # 仅 dry-run 跳过; 其他情况让 Master 起来 idle 等停.
        print("[orchestration] start: idle mode (no task_ids)")

    # 校验 task_ids 全部在 catalog 内 — 防止 typo; dry-run 跳过此校验
    # 注: detach 也跳过此校验, 因为子进程会重新加载 catalog.
    if not args.dry_run and not args.detach:
        known = set(_load_all_task_ids(cfg.paths.simulate_serve_config))
        unknown = [tid for tid in task_ids if tid not in known]
        if unknown:
            raise ValueError(f"unknown task_id(s): {', '.join(unknown)}")

    if args.dry_run:
        parallelism = args.parallelism if args.parallelism is not None else cfg.settings.max_parallelism
        print("[orchestration] dry-run:")
        print(f"  config         = {args.config}")
        print(f"  sqlite_db      = {sqlite_db}")
        print(f"  pid_file       = {pid_file}")
        print(f"  log_dir        = {log_dir}")
        print(f"  parallelism    = {parallelism}")
        print(f"  detach         = {args.detach}")
        preview = (
            ','.join(task_ids[:5]) + ('...' if len(task_ids) > 5 else '')
        )
        print(f"  tasks          = {len(task_ids)} ({preview})")
        print(f"  stay           = {args.stay}")
        return 0

    if args.detach:
        detach_argv = [sys.executable, "-m", "orchestration"]
        if args.config:
            detach_argv += ["--config", str(args.config)]
        detach_argv += ["start", "--foreground"]
        if args.tasks:
            detach_argv += ["--tasks", args.tasks]
        # 注: --all-tasks 在 detached child 重读 catalog, 与父进程一致
        detach_argv += ["--all-tasks"]
        if args.stay:
            detach_argv += ["--stay"]
        if args.parallelism is not None:
            detach_argv += ["--parallelism", str(args.parallelism)]
        handle = start_detached(
            argv=detach_argv,
            pid_file=pid_file,
            log_dir=log_dir,
            child_mode_arg="--foreground",
        )
        print(
            f"[orchestration] detached: pid={handle.pid} "
            f"pid_file={pid_file} log={log_dir}"
        )
        return 0

    # 前台路径
    def run(stop_event: threading.Event) -> None:
        master = Master(cfg=cfg)
        try:
            # 空 task_ids → idle 模式: 不跑 pipeline, 单纯等停信号
            if not task_ids:
                print("[orchestration] idle (no task_ids, waiting for stop)")
                stop_event.wait()
                return
            summary = master.run(task_ids)
            print(
                f"[orchestration] total={summary.total} "
                f"done={summary.done} dead={summary.dead} "
                f"duration={summary.duration_seconds:.2f}s"
            )
            _log.info(
                "start: complete total=%d done=%d dead=%d duration=%.2fs",
                summary.total, summary.done, summary.dead,
                summary.duration_seconds,
            )
            if not args.stay:
                _log.info("start: complete, shutting down (use --stay to keep resident)")
                return
            stop_event.wait()
        finally:
            master.shutdown()

    start_foreground(
        cfg_path=Path(args.config) if args.config else None,
        pid_file=pid_file, log_dir=log_dir, run_callback=run,
    )
    return 0


def _cmd_status(args: argparse.Namespace, cfg: OrchestrationConfig) -> int:
    """``status`` 子命令入口 (契约 §7.4)."""
    pid_file = Path(cfg.paths.pid_file)
    sqlite_db = Path(cfg.paths.sqlite_db)
    health_path = Path(cfg.paths.log_dir) / "health.json"

    alive, pid = is_running(pid_file)
    print("[orchestration] status")
    print(f"  pid_file       = {pid_file}  (alive={alive}, pid={pid})")

    if sqlite_db.exists():
        queue = SQLiteQueue(sqlite_db)
        tasks_summary = collect_tasks(queue)
        print(f"  sqlite_db      = {sqlite_db}")
        print(f"  phases         = {tasks_summary['phases']}")
        print(f"  total          = {tasks_summary['total']}")
        # 最近 10 个 task (契约 §7.4)
        recent = queue.list_tasks(limit=10)
        if recent:
            print(f"  recent_tasks   = {len(recent)}")
            for t in recent:
                err = (
                    (t.error_msg or "").splitlines()[0][:60]
                    if t.error_msg else ""
                )
                print(
                    f"    task_id={t.task_id} phase={t.phase} "
                    f"run_id={t.run_id or '-'} err={err!r}"
                )
        else:
            print("  recent_tasks   = 0")
    else:
        print(f"  sqlite_db      = (missing: {sqlite_db})")

    if health_path.exists():
        try:
            data = json.loads(health_path.read_text(encoding="utf-8"))
            print(
                f"  health.json    = {health_path}  "
                f"(last_updated={data.get('last_updated')})"
            )
        except (OSError, json.JSONDecodeError) as exc:
            print(f"  health.json    = (unreadable: {exc})")
    else:
        print(f"  health.json    = (missing: {health_path})")

    return 0


def _cmd_stop(args: argparse.Namespace, cfg: OrchestrationConfig) -> int:
    """``stop`` 子命令入口."""
    pid_file = Path(cfg.paths.pid_file)
    alive, pid = is_running(pid_file)
    if not alive:
        print(f"[orchestration] not running (pid_file={pid_file})")
        if pid_file.exists():
            remove_pid_file(pid_file)
        return 0
    print(
        f"[orchestration] stopping pid={pid} (graceful, timeout={args.timeout}s) ..."
    )
    ok = daemon_stop(pid_file, timeout=args.timeout)
    if ok:
        print(f"[orchestration] stopped pid={pid}")
        return 0
    print(f"[orchestration] stop timed out (pid={pid})")
    return 1


def _cmd_replay(args: argparse.Namespace, cfg: OrchestrationConfig) -> int:
    """``replay`` 子命令入口 (契约 §7.5): 把 phase=dead 复活为 pending.

    默认归档先于复活:
      1. reap_dead 把 dead task 的产物 (src_path / gdr_refined_path /
         etl_*_path) move 到 ``cfg.paths.dead_dir``, 写 dead.log 与
         dead_index.jsonl. 顺序必须在 requeue 之前 (requeue 会清空 src_path)
      2. queue.requeue_dead() 把 phase=dead 复位为 pending
      3. 写 health.json
    --no-archive: 跳过 reap, 直接 requeue (保留源文件位置, 适合想重新跑
                  但不想清空当前 dead 目录的场景).
    """
    sqlite_db = Path(cfg.paths.sqlite_db)
    if not sqlite_db.exists():
        print(f"[orchestration] sqlite_db missing: {sqlite_db}")
        return 1
    queue = SQLiteQueue(sqlite_db)

    # 1. 归档 dead 产物 (默认开启)
    if not getattr(args, "no_archive", False):
        from orchestration.failure_handler import reap_dead

        log_dir = Path(cfg.paths.log_dir)
        archives = reap_dead(
            queue,
            dead_dir=Path(cfg.paths.dead_dir),
            dead_log_path=log_dir / "dead.log",
            dead_index_path=log_dir / "dead_index.jsonl",
        )
        moved = sum(len(a.moved_to) for a in archives)
        print(
            f"[orchestration] replay: archived {len(archives)} dead task(s) "
            f"({moved} file(s) moved to {cfg.paths.dead_dir})"
        )

    # 2. 复活 dead → pending
    n = queue.requeue_dead()
    print(f"[orchestration] replay: requeued {n} dead task(s)")
    # 顺便刷一次 health
    try:
        from orchestration.health import write_health
        write_health(queue, log_dir=Path(cfg.paths.log_dir))
    except Exception:
        pass
    return 0


# ---------------------------------------------------------------------------
# parser
# ---------------------------------------------------------------------------


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="orchestration",
        description="simulate_serve / gdr / etl 三阶段流水线调度器 (2026-09-22 起的 simulation server → gdr → etl 新架构)",
    )
    parser.add_argument(
        "--config", type=str, default=None,
        help="配置 yaml 路径 (根配置或 orchestration 格式均可); 默认仓库根 config/config.yaml",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_start = sub.add_parser("start", help="启动 master 跑流水线")
    p_start.add_argument("--tasks", type=str, default=None,
                         help="逗号分隔的 task_id 列表; 不传则拉全 catalog")
    p_start.add_argument("--all-tasks", action="store_true",
                         help="显式声明全 catalog (默认行为)")
    p_start.add_argument("--parallelism", type=int, default=None,
                         help="子进程并行度; 默认 1 (严格串行), 设 ≥2 启用并行")
    p_start.add_argument("--detach", action="store_true",
                         help="后台化 (子进程), 返回立即")
    p_start.add_argument("--foreground", action="store_true",
                         help="前台运行 (默认; detach 子进程会传此标志)")
    p_start.add_argument("--dry-run", action="store_true",
                         help="仅打印计划, 不真的启动")
    p_start.add_argument("--stay", action="store_true",
                         help="跑完不退出 master; 默认跑完即退")
    p_start.set_defaults(func=_cmd_start)

    p_status = sub.add_parser("status", help="打印队列 / 进程状态")
    p_status.set_defaults(func=_cmd_status)

    p_stop = sub.add_parser("stop", help="优雅停止 (STOP 哨兵 + 超时强杀)")
    p_stop.add_argument("--timeout", type=float, default=10.0,
                        help="优雅停止超时秒数; 超时后 taskkill 强杀 (默认 10)")
    p_stop.set_defaults(func=_cmd_stop)

    p_replay = sub.add_parser("replay", help="dead 任务重新入队")
    p_replay.add_argument(
        "--no-archive",
        action="store_true",
        help="跳过归档 (默认会把 dead 产物先 move 到 dead/ 再 requeue)",
    )
    p_replay.set_defaults(func=_cmd_replay)

    return parser


# ---------------------------------------------------------------------------
# 入口
# ---------------------------------------------------------------------------


def main(argv: Sequence[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)

    # 全局 --config 解析一次, 子命令 handler 共用
    try:
        cfg = load_config(args.config)
    except (FileNotFoundError, ValueError) as exc:
        print(f"[orchestration] config error: {exc}", file=sys.stderr)
        return 2

    try:
        return args.func(args, cfg)
    except (ValueError, KeyError) as exc:
        # 可预期的输入错误 (task_id 未在 catalog / catalog 空 / skip_unready_tasks 全过滤):
        # 干净报错, 不打 traceback.
        _log.error("start aborted: %s", exc)
        print(f"[orchestration] error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())