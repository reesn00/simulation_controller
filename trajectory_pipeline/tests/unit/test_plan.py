"""``run --plan`` 接线的测试。

重点不是"能不能读进计划"，而是三件事：

1. **跳过的必须留痕**——少跑的那些条不留痕，"跑了 12 条"会被读成计划只有 12 条；
2. **坏计划必须炸**——静默返回空的表现是"跑完了，零条数据"，
   而零条数据与"全被跳过"在终端上长得一模一样；
3. **provenance 必须进存档**——不带它，persona 切片就只剩一份人工对表，
   而对表错位是静默的。
"""

from __future__ import annotations

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