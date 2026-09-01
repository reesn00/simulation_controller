"""orchestration CLI 入口.

子命令：

* ``start``  ：daemon 化启动 master + workers（骨架阶段：no-op，待 step 8 实施）
* ``status`` ：打印队列深度、子进程存活、近期 dead 数（骨架阶段：no-op，待 step 11 实施）
* ``stop``   ：SIGTERM 给 master，传播给子进程（骨架阶段：no-op，待 step 11 实施）
* ``replay`` ：将 dead 任务重新置为 pending（骨架阶段：no-op，待 step 11 实施）

骨架阶段（step 1）所有子命令仅打印 ``TODO`` 信息并退出，不做实际工作。
"""

from __future__ import annotations

import argparse
import sys
from typing import Sequence

from orchestration import __init__ as _pkg_init  # noqa: F401  仅用于确认包路径解析


_TODO_MSG = (
    "[orchestration] TODO: 这一步尚未实施。完整实施顺序见 "
    "docs/orchestration-design.md §12。"
)


def _cmd_start(_args: argparse.Namespace) -> int:
    print(f"{_TODO_MSG} (即将实施 step 8: master.py)")
    return 0


def _cmd_status(_args: argparse.Namespace) -> int:
    print(f"{_TODO_MSG} (即将实施 step 11: CLI 完整语义)")
    return 0


def _cmd_stop(_args: argparse.Namespace) -> int:
    print(f"{_TODO_MSG} (即将实施 step 11: CLI 完整语义)")
    return 0


def _cmd_replay(_args: argparse.Namespace) -> int:
    print(f"{_TODO_MSG} (即将实施 step 11: CLI 完整语义)")
    return 0


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="orchestration",
        description="simulate_serve / qwenformat / gdr 三阶段流水线调度器",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_start = sub.add_parser("start", help="daemon 化启动 master + workers")
    p_start.add_argument("--dry-run", action="store_true",
                         help="仅打印将要做的事，不真的启动")
    p_start.set_defaults(func=_cmd_start)

    p_status = sub.add_parser("status", help="打印队列/进程/dead 状态")
    p_status.set_defaults(func=_cmd_status)

    p_stop = sub.add_parser("stop", help="优雅停止")
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
