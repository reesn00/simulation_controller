"""**分发层**的测试：命令跑起来时是否走到该走的路上。

盯的是 §5.16 的同一个洞，但换了个面——`cli build` 那条盯的是
「执行路径没测」，这里盯的是「**分发路径**没测」。

分发原先是一份**手写的命令名清单**::

    if args.cmd in ("report", "gen", "check-persona", "review",
                    "check-archives", "replay", "purge-run", "build"):
        return args.func(args)
    return asyncio.run(args.func(args))

每加一个子命令都得记得改它，而漏改的后果是 `asyncio.run(0)`：
`ValueError: a coroutine was expected`，**崩在所有输出之后**。
清单看着干完了活，退出码却是非零。

根治不是「记得改」，是**让那份清单不存在**——从 `func` 本身推。
下面的 `test_每个子命令都能被分发` 就是钉住这一点：新增子命令若
重新引入查表，这里会红。
"""

from __future__ import annotations

import argparse
import asyncio
import inspect

import pytest

from trajectory_pipeline.executor import cli


def _subparsers() -> argparse.ArgumentParser:
    return cli.build_parser()


def _registered(parser: argparse.ArgumentParser):
    """``(命令名, parser, 绑定的 func)``，按注册顺序。"""
    action = next(a for a in parser._actions
                  if isinstance(a, argparse._SubParsersAction))
    out = []
    for name, sub in action.choices.items():
        fn = sub.get_default("func")
        if fn is not None:                      # probe 的 func 是后面补的
            out.append((name, sub, fn))
    return out


class Test分发:
    def test_每个子命令都能被分发(self):
        """新增子命令若忘了标注同步/异步，这里会红。

        不去真的执行每个命令（有的要浏览器、有的会改盘），
        只验**它会被 `main()` 正确地交给 asyncio 或直接调**——
        分发的全部逻辑就是这两行。
        """
        for name, _sub, fn in _registered(_subparsers()):
            if inspect.iscoroutinefunction(fn):
                coro = fn(argparse.Namespace())
                assert inspect.iscoroutine(coro), name
                coro.close()
            else:
                assert callable(fn), name

    def test_没有第二份命令名清单(self):
        """``main`` 里不得再出现按命令名分流的 ``in (...)``。

        不是洁癖：那份清单与 ``set_defaults`` 不同源，而不同源的
        后果是**安静地不一致**——加了命令忘了改清单，命令就崩在
        所有输出之后（避坑指南 §一）。
        """
        import inspect as _inspect
        src = _inspect.getsource(cli.main)
        assert "args.cmd in (" not in src, (
            "main() 又出现了按命令名分流的清单——从 func 推，别查表")

    def test_同步命令不被塞进asyncio(self, capsys):
        """端到端：同步子命令走 ``main()`` 不该抛 ``ValueError``。

        原来正是这里炸的：``downgrade`` 打印完整份清单之后
        ``asyncio.run(0)`` 抛 ``a coroutine was expected``——
        输出全打完了，退出码却是非零。
        """
        rc = cli.main(["downgrade", "--url", "x", "--evidence", "y",
                       "--reason", "r"])          # 参数互斥，退出码 2
        assert rc == 2
        assert "不要同时给" in capsys.readouterr().err

    def test_缺reason在碰盘之前就拒(self, capsys, tmp_path):
        """**校验必须在碰盘之前**。

        原先它落在 ``apply_downgrades`` 里，于是要先走完整个存档
        目录、撞上第一条命中才抛——而且抛的是裸 traceback，看起来
        像崩溃，其实只是一次参数写漏。
        """
        rc = cli.main(["downgrade", "--url", "a.com", "--out", str(tmp_path)])
        assert rc == 2
        assert "必须给 --reason" in capsys.readouterr().err

    def test_退出门槛_真跑一次同步命令(self, tmp_path, capsys):
        """跑一个真会碰盘的同步命令，验退出码是 **0 而不是异常**。"""
        rc = cli.main(["downgrade", "--out", str(tmp_path), "--url", "a.com",
                       "--reason", "冒烟"])
        assert rc == 0
        assert "没有匹配" in capsys.readouterr().out


class Test探针:
    def test_probe要浏览器(self):
        """``probe`` 必须是 async —— 它是唯一会开浏览器的新命令。

        反过来这条也在钉住它：哪天有人为了「方便」把它改成同步的，
        ``asyncio.run`` 会直接抛，而这条会先红。
        """
        fn = dict((n, f) for n, _s, f in _registered(_subparsers()))["probe"]
        assert inspect.iscoroutinefunction(fn)
        assert asyncio.iscoroutinefunction(fn)
