"""``run --plan`` 接线的测试。

重点不是"能不能读进计划"，而是三件事：

1. **跳过的必须留痕**——少跑的那些条不留痕，"跑了 12 条"会被读成计划只有 12 条；
2. **坏计划必须炸**——静默返回空的表现是"跑完了，零条数据"，
   而零条数据与"全被跳过"在终端上长得一模一样；
3. **provenance 必须进存档**——不带它，persona 切片就只剩一份人工对表，
   而对表错位是静默的。
"""

from __future__ import annotations

import asyncio
import json

import pytest

from trajectory_pipeline.executor.orchestrator import RunRecord
from trajectory_pipeline.executor.plan import (
    PlanError,
    load_plan,
    parse_plan,
)


def _row(**over) -> dict:
    base = {
        "task_id": "T001",
        "persona_id": "parent-喜剧",
        "scenario_id": "media_lookup_standard",
        "prompt_text": "我想找到《功夫》电影在线观看的可播放网址",
        "search_query": "功夫 在线观看",
        "has_standard": True,
        "retrieval_mode": "single_title",
        "title": "功夫",
        "rewritten": True,
        "provenance": {"persona_id": "parent-喜剧", "genre": "喜剧", "urgency": "本周"},
    }
    base.update(over)
    return base


def _plan(*rows) -> dict:
    return {"report": {"seed": 7, "library_digest": "abc123", "by_mode": {"single_title": 2}},
            "instances": list(rows)}


class TestParsePlan:
    def test_读出可执行项(self):
        plan = parse_plan(_plan(_row()))
        assert [t.task_id for t in plan.tasks] == ["T001"]
        assert plan.tasks[0].search_query == "功夫 在线观看"
        assert plan.tasks[0].provenance["genre"] == "喜剧"

    def test_保留原始索引(self):
        """跳过的条目不重排索引——否则计划与存档对不上号。"""
        plan = parse_plan(_plan(_row(retrieval_mode="aggregate"), _row()))
        assert plan.tasks[0].index == 1
        assert plan.skips[0].index == 0

    def test_报告随行(self):
        plan = parse_plan(_plan(_row()))
        assert plan.report["seed"] == 7


class TestSkipAccounting:
    """三类跳过，每一类都必须**计数**而不是静默丢。"""

    def test_非单片模式跳过并记账(self):
        plan = parse_plan(_plan(_row(retrieval_mode="aggregate"),
                                _row(retrieval_mode="unknown_title")))
        assert plan.tasks == ()
        assert plan.skip_summary() == {"mode_not_runnable": 2}

    def test_空检索式跳过(self):
        plan = parse_plan(_plan(_row(search_query="   ")))
        assert plan.skip_summary() == {"empty_query": 1}

    def test_未归一默认跳过(self):
        plan = parse_plan(_plan(_row(rewritten=False)))
        assert plan.skip_summary() == {"not_rewritten": 1}

    def test_未归一可显式放行(self):
        plan = parse_plan(_plan(_row(rewritten=False)), include_unrewritten=True)
        assert len(plan.tasks) == 1
        assert plan.tasks[0].rewritten is False

    def test_总数不被跳过数掩盖(self):
        """total_in_file 要算跳过的——否则"20 条计划跑了 8 条"看不出来。"""
        plan = parse_plan(_plan(_row(), _row(retrieval_mode="aggregate"), _row(search_query="")))
        assert plan.total_in_file == 3
        assert len(plan.tasks) == 1

    def test_跳过理由带原因(self):
        plan = parse_plan(_plan(_row(search_query="")))
        assert "搜空串" in plan.skips[0].detail


class TestFailClosed:
    def test_不是对象(self):
        with pytest.raises(PlanError, match="不是 JSON 对象"):
            parse_plan([1, 2, 3])

    def test_缺instances(self):
        with pytest.raises(PlanError, match="缺 instances"):
            parse_plan({"report": {}})

    def test_instances不是列表(self):
        with pytest.raises(PlanError, match="缺 instances"):
            parse_plan({"instances": "T001"})

    def test_缺task_id(self):
        with pytest.raises(PlanError, match="缺 task_id"):
            parse_plan(_plan(_row(task_id="")))

    def test_缺persona_id(self):
        """没有 persona_id 的条目进不了切片，硬跑等于产生一条不可归因的样本。"""
        with pytest.raises(PlanError, match="缺 persona_id"):
            parse_plan(_plan(_row(persona_id="")))

    def test_缺retrieval_mode(self):
        """不给就默认 single_title 的话，那等于静默把计划当成单片任务。"""
        with pytest.raises(PlanError, match="缺 retrieval_mode"):
            parse_plan(_plan(_row(retrieval_mode="")))

    def test_条目不是对象(self):
        with pytest.raises(PlanError, match="不是对象"):
            parse_plan(_plan("T001"))

    def test_空计划合法但产出零条(self):
        plan = parse_plan(_plan())
        assert plan.tasks == () and plan.skips == ()

    def test_文件不存在(self, tmp_path):
        with pytest.raises(PlanError, match="找不到执行计划"):
            load_plan(tmp_path / "nope.json")

    def test_文件不是json(self, tmp_path):
        p = tmp_path / "p.json"
        p.write_text("{ not json", encoding="utf-8")
        with pytest.raises(PlanError, match="不是合法 JSON"):
            load_plan(p)

    def test_可回读落盘计划(self, tmp_path):
        p = tmp_path / "plan.json"
        p.write_text(json.dumps(_plan(_row()), ensure_ascii=False), encoding="utf-8")
        assert len(load_plan(p).tasks) == 1


class TestProvenanceReachesArchive:
    """provenance 必须一路走到 ``RunRecord.to_json()``。"""

    def test_进存档顶层(self):
        rec = RunRecord(task_id="T001", title="功夫", query="功夫 在线观看",
                        search_url="https://x", provenance={"genre": "喜剧"})
        js = rec.to_json()
        assert js["provenance"] == {"genre": "喜剧"}

    def test_空provenance是空对象而非缺字段(self):
        """缺字段与空 dict 在切片脚本里长得一样——缺字段会被当成"未知"。"""
        js = RunRecord(task_id="T001", title="x", query="q", search_url="u").to_json()
        assert js["provenance"] == {}

    def test_序列化后仍是副本(self):
        """调用方后续改自己的 dict，不该改到存档里的内容。"""
        prov = {"genre": "喜剧"}
        rec = RunRecord(task_id="T001", title="x", query="q", search_url="u",
                        provenance=prov)
        prov["genre"] = "改过了"
        assert rec.to_json()["provenance"]["genre"] == "喜剧"


class TestUserPrompt:
    """用户原话必须一路走到 ``RunRecord.to_json()``。

    它曾经只活在计划文件里，于是 **P2 六件套第 ③ 件恒为空**——
    而存档里看不出任何异常：字段不存在与「用户什么都没说」长得一样。
    模型于是拿一句检索式当用户轮次去学「怎么回应这句检索式」，
    那是 persona 制造偏差的地方，恰恰最不该被学成输入分布。
    """

    def test_进存档顶层(self):
        rec = RunRecord(task_id="T001", title="功夫", query="功夫 在线观看",
                        search_url="https://x",
                        user_prompt="哥 有没有能看正片的 给个链接呗")
        assert rec.to_json()["user_prompt"] == "哥 有没有能看正片的 给个链接呗"

    def test_空串而非缺字段(self):
        """手工路径没有 persona 渲染的提问。空串 = 「不是 taskgen 跑的」，
        与 ``provenance`` 空 dict 同款约定——缺字段会被切片脚本读成"未知"。"""
        js = RunRecord(task_id="T001", title="x", query="q", search_url="u").to_json()
        assert js["user_prompt"] == ""

    def test_与检索式是两件事不能互相顶替(self):
        """``query`` 是拿去搜的字符串，``user_prompt`` 是用户开口的那句。
        两者混用＝让模型从关键词反推意图。"""
        rec = RunRecord(task_id="T001", title="功夫", query="功夫 在线观看",
                        search_url="https://x", user_prompt="功夫在哪看啊")
        js = rec.to_json()
        assert js["query"] != js["user_prompt"]

    def test_老存档缺这个键也能被消费(self, tmp_path):
        """字段引入之前跑的存档全都没有它，而那批存档正是唯一的真实证据。"""
        from trajectory_pipeline.executor.review_queue import collect

        old = {"task_id": "T001", "search_blocked": "",
               "ledger": {"total_sites": 1, "by_branch": {"unresolved": 1},
                          "missing_branches": []},
               "outcomes": [{"url": "https://a.test/x", "branch": "unresolved",
                             "evidence": "e", "reached_play_page": False}],
               "visits": [{"url": "https://a.test/x", "landed_url": "https://a.test/x",
                           "success": False, "notes": [], "site_obs": None,
                           "player_obs": None}]}
        (tmp_path / "T001__old.json").write_text(
            json.dumps(old, ensure_ascii=False), encoding="utf-8")
        assert len(collect(tmp_path)) == 1, "老存档在复核队列这一步整批消失了"


class TestPlanCliWiring:
    """**CLI 那一行**必须真的把计划里的字段传下去。

    上面那两个类测的是 ``RunRecord.to_json()``——而
    ``RunRecord(user_prompt=...)`` 一直是对的，错的可能是
    ``_run_plan`` 忘了传（或者重构时被当成冗余参数删掉）。
    那种错误没有任何单元测试能看见：字段合法、存档合法、报表正常，
    只是每一条样本的用户轮次都成了空串。所以这里跑真的 ``_run_plan``。
    """

    @staticmethod
    def _run(tmp_path, monkeypatch, rows):
        from trajectory_pipeline.executor import cli

        class _Info:
            name, version, tool_count = "fake", "0", 0

        class _Client:
            info = _Info()

            async def __aenter__(self):
                return self

            async def __aexit__(self, *a):
                return False

            @staticmethod
            def from_env():
                return _Client()

        class _Driver:
            def __init__(self, client):
                pass

        monkeypatch.setattr(cli.McpClient, "from_env", staticmethod(lambda: _Client()))
        monkeypatch.setattr(cli, "ObscuraDriver", _Driver)

        plan = tmp_path / "plan.json"
        plan.write_text(json.dumps(_plan(*rows), ensure_ascii=False), encoding="utf-8")
        out = tmp_path / "out"
        args = _ns(plan=plan, out=str(out), limit=0, title=None,
                   include_unrewritten=False, engine="bing",
                   max_candidates=5, max_chars=None, stop_after_success=0)
        assert asyncio.run(cli._run_plan(args)) == 0
        return list(out.glob("*.json"))

    def test_用户原话与检索式都进存档(self, tmp_path, monkeypatch):
        files = self._run(tmp_path, monkeypatch, [_row()])
        assert len(files) == 1
        js = json.loads(files[0].read_text(encoding="utf-8"))
        assert js["user_prompt"] == _row()["prompt_text"], (
            "用户轮次没进存档：P2 六件套第 ③ 件会恒为空，且存档看不出异常")
        assert js["query"] == _row()["search_query"]
        assert js["provenance"]["genre"] == "喜剧", "provenance 也没跟着走"


def _ns(**kw):
    import argparse

    base = dict(plan=None, out=None, limit=0, title=None, dry_run=False,
                include_unrewritten=False, engine="bing", task_id="T001",
                perceptor=None, max_candidates=5, max_chars=None,
                stop_after_success=0)
    base.update(kw)
    return argparse.Namespace(**base)


class TestPlanJsonRoundTrip:
    def test_序列化可再读回(self):
        """计划文件要能被重新消费——半年后回看切片时走的就是这条路。"""
        plan = parse_plan(_plan(_row(), _row(retrieval_mode="aggregate")))
        blob = json.loads(json.dumps(plan.to_json(), ensure_ascii=False))
        again = parse_plan({**blob, "instances": blob["tasks"]})
        assert [t.task_id for t in again.tasks] == ["T001"]
        assert again.skip_summary() == {}       # 跳过的已单独成列，不在 tasks 里
        assert len(again.skips) == 0
        assert len(plan.skips) == 1             # 原对象仍记得自己跳过了一条

class TestRunRequiresTitle:
    """CLI 两条入口都**不得**用 ``--task-id`` 顶替片名。

    实测（2026-10-09）：手工路径少给 ``--title`` 就跑成 ``T001 在线观看``，
    搜回来的是轮胎 T001，判断点 ① 交出 6 条理由通顺的 ``not_play_site``，
    零异常零空转。守卫放在 CLI 层的意义是**在开浏览器之前**就退掉——
    浏览器起来一次要几秒，真跑完才发现片名错了，那批已经落进 pools。
    """

    async def test_手工路径缺title即报错(self, capsys):
        from trajectory_pipeline.executor.cli import cmd_run

        assert await cmd_run(_ns(title=None)) == 2
        out = capsys.readouterr().out
        assert "缺 --title" in out
        assert "T001" in out, "错误串要指名道姓说明 task-id 不是片名"

    async def test_手工路径不连浏览器就退(self, capsys):
        """反例护栏：守卫不是「一律拒绝」。给了片名就该往前走——
        本机没配 OBSCURA_EXE，正好停在连接那一步，而不是停在片名那一步。"""
        from trajectory_pipeline.executor.cli import cmd_run

        assert await cmd_run(_ns(title=["功夫"])) == 2
        out = capsys.readouterr().out
        assert "缺 --title" not in out
        assert "OBSCURA_EXE" in out, f"应该停在连浏览器那一步，实际输出：{out}"

    async def test_计划路径有没片名的条目即报错(self, capsys, tmp_path):
        from trajectory_pipeline.executor.cli import cmd_run

        path = tmp_path / "plan.json"
        path.write_text(json.dumps(_plan(_row(title=""), _row(task_id="T002", title=""))),
                        encoding="utf-8")
        assert await cmd_run(_ns(plan=str(path))) == 2
        out = capsys.readouterr().out
        assert "没有片名" in out
        assert "T001" in out and "T002" in out, "要**逐条**列出来，一份坏计划通常不止一条坏"

    async def test_计划路径片名齐全不误伤(self, capsys, tmp_path):
        from trajectory_pipeline.executor.cli import cmd_run

        path = tmp_path / "plan.json"
        path.write_text(json.dumps(_plan(_row(), _row(task_id="T002"))), encoding="utf-8")
        # dry-run 让它读完计划就停，不必连浏览器
        assert await cmd_run(_ns(plan=str(path), dry_run=True)) == 0
        assert "没有片名" not in capsys.readouterr().out
