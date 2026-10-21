"""``cli purge-run`` 的**回滚完整性**测试。

回滚一个 run 的意思是「这个 run 的数据没了」。旧口径只删两样东西：

- P1 存档；
- 它并进负样本池的行。

**派生的训练数据（P2 样本 / P3 视图 / T 视图）留在原地**——那批样本的
标签已经被判为「前提不成立」，却还躺在 ``samples/`` 里等着被渲染进 P3、
被上传给 Label Studio、被喂进 SFT。

而且它们**再也重建不了**：源存档没了，重建只会跳过（造不出新样本），
于是这批旧样本就成了永久的、与 catalog 漂移的僵尸。训练集里一半样本
带新工具声明、一半带旧的，而**报告上看不出任何异常**。

更荒唐的是**那条命令当时也清不掉它们**：``purge-run`` 只数 P1 存档，
于是对已回滚的 run 报「这个 run_id 下没有东西」就退出了——孤儿**永久
留下**，只能手工删文件。

这里盯三件：

1. 预演**只列不删**（派生产物也要列出来，让人看清要动什么）；
2. ``--apply`` 把派生产物**一起删干净**；
3. 已回滚的 run（存档没了但派生产物还在）**仍能被 purge 收干净**。
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import pytest

from trajectory_pipeline.executor import cli


def _ns(**kw):
    defaults = {"out": None, "run_id": "a1", "apply": False}
    defaults.update(kw)
    return argparse.Namespace(**defaults)


def _seed(root: Path, run_id: str = "a1", task_id: str = "T001"):
    """造一个 run 的全套产物：P1 存档 + P2 样本 + P3 视图 + T 视图。"""
    root.mkdir(parents=True, exist_ok=True)
    (root / f"{task_id}__{run_id}.json").write_text(
        json.dumps({"task_id": task_id, "run_id": run_id}), encoding="utf-8")
    (root / "samples").mkdir(exist_ok=True)
    (root / "views").mkdir(exist_ok=True)
    (root / "task_views").mkdir(exist_ok=True)
    (root / "samples" / f"{task_id}__{run_id}__search.json").write_text(
        "{}", encoding="utf-8")
    for suffix in ("messages", "openai", "meta"):
        (root / "views" / f"{task_id}__{run_id}__search_{suffix}.json").write_text(
            "{}", encoding="utf-8")
    (root / "task_views" / f"{task_id}__{run_id}.json").write_text(
        "{}", encoding="utf-8")
    return root


class Test预演只列不删:
    def test_派生产物被列出来(self, tmp_path, capsys):
        _seed(tmp_path)
        assert cli.cmd_purge_run(_ns(out=str(tmp_path))) == 0
        out = capsys.readouterr().out
        assert "派生产物" in out
        assert "T001__a1__search.json" in out
        assert "T001__a1__search_openai.json" in out

    def test_预演不删任何东西(self, tmp_path):
        _seed(tmp_path)
        cli.cmd_purge_run(_ns(out=str(tmp_path)))
        assert (tmp_path / "samples" / "T001__a1__search.json").exists()
        assert (tmp_path / "views" / "T001__a1__search_openai.json").exists()
        assert (tmp_path / "task_views" / "T001__a1.json").exists()
        assert (tmp_path / "T001__a1.json").exists()


class Testapply删干净:
    def test_派生产物被删(self, tmp_path):
        _seed(tmp_path)
        assert cli.cmd_purge_run(_ns(out=str(tmp_path), apply=True)) == 0
        assert not (tmp_path / "samples" / "T001__a1__search.json").exists()
        assert not (tmp_path / "views" / "T001__a1__search_openai.json").exists()
        assert not (tmp_path / "views" / "T001__a1__search_meta.json").exists()
        assert not (tmp_path / "task_views" / "T001__a1.json").exists()

    def test_不误伤别的run(self, tmp_path):
        """run_id 是**精确**匹配——``a1`` 不该碰到 ``a12``。"""
        _seed(tmp_path, run_id="a1")
        _seed(tmp_path, run_id="a12")
        cli.cmd_purge_run(_ns(out=str(tmp_path), run_id="a1", apply=True))
        assert (tmp_path / "samples" / "T001__a12__search.json").exists()
        assert (tmp_path / "task_views" / "T001__a12.json").exists()

    def test_已回滚的run仍能收干净(self, tmp_path, capsys):
        """**孤儿场景**：P1 存档已经没了（上一轮回滚删的），派生产物还在。

        旧口径下这条命令会报「这个 run_id 下没有东西」就退出——
        孤儿**永久留下**。现在它必须能收干净。
        """
        _seed(tmp_path)
        (tmp_path / "T001__a1.json").unlink()
        assert cli.cmd_purge_run(_ns(out=str(tmp_path), apply=True)) == 0
        assert not (tmp_path / "samples" / "T001__a1__search.json").exists()
        assert not (tmp_path / "views" / "T001__a1__search_openai.json").exists()
        assert not (tmp_path / "task_views" / "T001__a1.json").exists()

    def test_真的没有就说没有(self, tmp_path, capsys):
        assert cli.cmd_purge_run(_ns(out=str(tmp_path))) == 1
        assert "没有东西" in capsys.readouterr().out


class Test匹配面收紧:
    """``run_id`` 会被拼进 glob 模式——glob 里的 ``*`` 会匹配到别的 run。
    这条命令会**删文件**，所以字符集必须收紧。"""

    @pytest.mark.parametrize("bad", ["*", "a*", "?", "[ab]", "a/b", "..", ""])
    def test_非法run_id匹配不到任何东西(self, tmp_path, bad):
        _seed(tmp_path)
        assert cli._derived_products(tmp_path, bad) == ()

    def test_合法run_id正常匹配(self, tmp_path):
        _seed(tmp_path, run_id="3a06f63d")
        found = cli._derived_products(tmp_path, "3a06f63d")
        assert found, "合法的 run_id 应当匹配到派生产物"
