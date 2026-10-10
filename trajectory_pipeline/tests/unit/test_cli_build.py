"""``cli build`` 的冒烟测试——**新命令必须自己测**，不能靠「跑过一次」。

§5.16 的原话：「命令写完了」不等于「命令能跑」。那条坑是
``purge-run --apply`` 预演正常、执行炸掉——两条路径都绿，
因为执行路径**一条测试都没有**。

这里盯四件：两段都能单独跑、退出码、口径（默认不覆盖）、
以及**它落在哪个目录**（D8 产物隔离——落错不报错）。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from trajectory_pipeline.executor import cli


def _obs(url="u", **kw):
    base = {"url": url, "page_title": "T", "body_text": "正文",
            "truncated": False, "degraded": [], "video_tag_count": 0,
            "iframe_count": 0, "interactive_elements": [], "links": []}
    base.update(kw)
    return base


def _archive(task_id="T001", run_id="a1", **over):
    base = {
        "task_id": task_id, "title": "功夫", "query": "功夫 在线观看",
        "user_prompt": "有没有能看正片的", "search_url": "https://s.test",
        "search_observation": _obs("https://s.test"), "search_blocked": "",
        "steps": [{"action": {"tool": "goto", "params": {"url": "https://s.test"},
                              "origin": "model", "target": None},
                   "observation": _obs("https://s.test"), "error": ""}],
        "run_config": {"engine": "bing", "max_candidates": 20},
        "provenance": {}, "candidates": [], "visits": [], "outcomes": [],
        "ledger": None, "warnings": [],
    }
    base.update(over)
    return base


def _write_batch(root: Path, *arcs):
    for arc in arcs:
        name = f"{arc['task_id']}__{arc.get('_run', 'a1')}.json"
        (root / name).write_text(json.dumps(arc, ensure_ascii=False),
                                 encoding="utf-8")
    return root


def _ns(**kw):
    import argparse
    defaults = {"out": None, "stage": "all", "overwrite": False,
                "limit": 0, "show": 0}
    defaults.update(kw)
    return argparse.Namespace(**defaults)


class Test两段独立可跑:
    def test_全流程(self, tmp_path, capsys):
        _write_batch(tmp_path, _archive())
        assert cli.cmd_build(_ns(out=str(tmp_path))) == 0
        out = capsys.readouterr().out
        assert "P1 → P2" in out
        assert "P2 → P3" in out
        assert len(list((tmp_path / "samples").glob("*.json"))) == 1
        assert len(list((tmp_path / "views").glob("*.json"))) == 3

    def test_只跑P2(self, tmp_path):
        _write_batch(tmp_path, _archive())
        assert cli.cmd_build(_ns(out=str(tmp_path), stage="p2")) == 0
        assert len(list((tmp_path / "samples").glob("*.json"))) == 1
        assert not (tmp_path / "views").exists(), "只跑 P2 时不该建 views/"

    def test_只跑P3(self, tmp_path):
        """P3 的输入是 P2。先造 P2 再单独跑 P3——
        P1 变了才需要重跑 P2；只是视图口径改了只需重跑 P3。"""
        _write_batch(tmp_path, _archive())
        cli.cmd_build(_ns(out=str(tmp_path), stage="p2"))
        assert len(list((tmp_path / "views").glob("*.json"))) == 0
        assert cli.cmd_build(_ns(out=str(tmp_path), stage="p3")) == 0
        assert len(list((tmp_path / "views").glob("*.json"))) == 3

    def test_退出码0(self, tmp_path):
        _write_batch(tmp_path, _archive())
        assert cli.cmd_build(_ns(out=str(tmp_path))) == 0


class Test默认不覆盖:
    def test_二次跑不覆盖(self, tmp_path, capsys):
        _write_batch(tmp_path, _archive())
        cli.cmd_build(_ns(out=str(tmp_path), overwrite=True))
        # 把 P2 改成明显坏的内容
        (tmp_path / "samples" / "T001__a1__search.json").write_text(
            "坏", encoding="utf-8")
        cli.cmd_build(_ns(out=str(tmp_path)))
        # 没被覆盖回去
        assert (tmp_path / "samples" / "T001__a1__search.json").read_text(
            encoding="utf-8") == "坏"

    def test_显式overwrite才覆盖(self, tmp_path):
        _write_batch(tmp_path, _archive())
        cli.cmd_build(_ns(out=str(tmp_path), overwrite=True))
        (tmp_path / "samples" / "T001__a1__search.json").write_text(
            "坏", encoding="utf-8")
        cli.cmd_build(_ns(out=str(tmp_path), overwrite=True))
        json.loads((tmp_path / "samples" / "T001__a1__search.json")
                   .read_text(encoding="utf-8"))


class Test坏档可见:
    def test_坏档逐条点名(self, tmp_path, capsys):
        """被反爬拦下的 run 产出零条是**正常形态**，
        但「哪几份没切出」必须看得见——否则批次看起来只是少了几条。"""
        blocked = _archive(task_id="T002")
        blocked["search_blocked"] = "captcha"
        blocked["search_observation"] = None
        blocked["steps"] = []
        _write_batch(tmp_path, _archive(), blocked)
        cli.cmd_build(_ns(out=str(tmp_path), stage="p2"))
        out = capsys.readouterr().out
        assert "切不出样本的存档" in out
        assert "T002__a1.json" in out

    def test_空目录不报错(self, tmp_path, capsys):
        """产物目录为空时也要正常退出——「没有样本」不是错误。"""
        (tmp_path).mkdir(exist_ok=True)
        assert cli.cmd_build(_ns(out=str(tmp_path))) == 0


class Test产物隔离:
    def test_不落仓库根output(self, tmp_path):
        """D8：产物落 ``<out>/samples`` 与 ``<out>/views``，
        绝不碰仓库根的 ``output/``（存量目录）。"""
        _write_batch(tmp_path, _archive())
        cli.cmd_build(_ns(out=str(tmp_path)))
        assert (tmp_path / "samples").is_dir()
        assert (tmp_path / "views").is_dir()
        # 默认根（DEFAULT_ROOT）下是 trajectory_pipeline/output/，
        # 由 builder/views 的 Test产物根 盯，不在这里重复断言。

    def test_跑完check_archives仍认得这批(self, tmp_path):
        """build 之后门禁还认这批档案——选档口径没被 builder 改窄。"""
        from trajectory_pipeline.executor.integrity import check_root
        _write_batch(tmp_path, _archive())
        cli.cmd_build(_ns(out=str(tmp_path), stage="p2"))
        rep = check_root(tmp_path)
        assert len(rep.archives) == 1
