"""R0（方案 23 §3）：原生 think 存活 / 补记 history 作用域。

三件事分三层测：

1. ``_write_rationales`` **跳过已有原生 think 的步**（R0-1）——本文件；
2. 覆盖守卫与 ``reason`` 取值契约（R0-3 / R0-4）——``tests/contract``
   的 ``test_think_payload_contract.py``；
3. 补记的 history **跨单元累积到 run 级**（R0-2）——本文件。

为什么这层要单独测：`_write_rationales` 的两个调用点都是「采集完一个
单元再补记」，而「补记是否覆盖原生 think」「history 是单元还是 run」
都不在 P1 的形状里——形状一样，来源和上下文却不同。只测 P1 字段存在性
看不出这两件事。
"""

from __future__ import annotations

from trajectory_pipeline.executor import actions
from trajectory_pipeline.executor.actions import NATIVE_THINK_REASON, StepLog
from trajectory_pipeline.executor.orchestrator import (
    RATIONALE_HISTORY_MAX_STEPS,
    Orchestrator,
    RunConfig,
    RunRecord,
    _rationale_history,
)
from trajectory_pipeline.executor.steps.aggregator import IntermediateResult
from trajectory_pipeline.executor.steps.visit import VisitResult
from trajectory_pipeline.perception.base import LinkItem, Observation, Q

import trajectory_pipeline.tests.unit.test_visit as tv


def _writer(sink: list[dict]):
    """记录 writer 收到的 kwargs，回一个合法 written payload。"""

    def write(**kw):
        sink.append(kw)
        return {
            "status": "written",
            "rationale": {"text": f"补记 {len(sink)}", "conclusion": ""},
            "reason": "",
            "degraded": [],
        }

    return write


def _native(text: str = "原生动机") -> dict:
    return {
        "status": "written",
        "rationale": {"text": text, "conclusion": ""},
        "reason": NATIVE_THINK_REASON,
        "degraded": [],
    }


def _record() -> RunRecord:
    """最小 RunRecord：``_vocabulary_of_run`` 要一份观察，其余不参与。"""
    rec = RunRecord(
        task_id="T001", title="功夫", query="q", search_url="https://s.test/"
    )
    rec.search_obs = tv.obs_at("https://s.test/")
    return rec


def _append_step(log: StepLog, url: str) -> int:
    index = log.act(actions.goto(url))
    log.settle(index, tv.obs_at(url))
    return index


# ═══════════════════════════════════════════════════════════════════════
# R0-1：补记不得覆盖原生 think
# ═══════════════════════════════════════════════════════════════════════


class Test原生think存活:
    async def test_补记跳过已有原生think的步(self):
        """原生 think 步**不进 writer**，也不被替换；没有它的步照常补记。"""
        sink: list[dict] = []
        record = _record()
        log = StepLog()
        native_index = _append_step(log, "https://a.test/")
        log.attach_rationale(native_index, _native())
        plain_index = _append_step(log, "https://b.test/")

        orch = Orchestrator(
            None, None, RunConfig(), rationale_writer=_writer(sink)
        )
        await orch._write_rationales(log, record, "site")

        assert len(sink) == 1, "原生 think 步也被送进了 writer"
        assert sink[0]["action"]["params"]["url"] == "https://b.test/"
        assert log.steps[native_index].rationale == _native()
        assert log.steps[plain_index].rationale["status"] == "written"

    async def test_没有writer时原生think也原样保留(self):
        """W1 规则版批次（无 writer）：原生 think 不该因为「没接 writer」丢。"""
        record = _record()
        log = StepLog()
        index = _append_step(log, "https://a.test/")
        log.attach_rationale(index, _native("决策当时的原文"))

        orch = Orchestrator(None, None, RunConfig())
        await orch._write_rationales(log, record, "site")

        assert log.steps[index].rationale == _native("决策当时的原文")


# ═══════════════════════════════════════════════════════════════════════
# R0-2：history 作用域是 run，不是站点单元
# ═══════════════════════════════════════════════════════════════════════


class Testhistory作用域:
    def test_按搜索_中间页_站点顺序取已走完的步(self):
        record = _record()
        _append_step(record.steps, "https://search.test/")

        intermediate = IntermediateResult(role="aggregator", candidate=tv.CAND)
        _append_step(intermediate.steps, "https://agg.test/")
        record.intermediates.append(intermediate)

        visit = VisitResult(candidate=tv.CAND)
        _append_step(visit.steps, "https://site.test/")
        record.visits.append(visit)

        got = [s.action.params["url"] for s in _rationale_history(record)]
        assert got == [
            "https://search.test/",
            "https://agg.test/",
            "https://site.test/",
        ]

    def test_窗口只取最近N步(self):
        """窗口上限是 prompt 预算（风险 R-2）——**非空**仍能否掉「没有历史」。"""
        record = _record()
        for i in range(RATIONALE_HISTORY_MAX_STEPS + 5):
            _append_step(record.steps, f"https://s{i}.test/")

        got = _rationale_history(record)
        assert len(got) == RATIONALE_HISTORY_MAX_STEPS
        assert got[0].action.params["url"] == "https://s5.test/"

    async def test_prefix与实际history拼接(self):
        """``_write_rationales`` 把前缀放在本单元步之前，顺序不能反。"""
        sink: list[dict] = []
        record = _record()
        _append_step(record.steps, "https://search.test/")

        log = StepLog()
        _append_step(log, "https://site.test/")

        orch = Orchestrator(
            None, None, RunConfig(), rationale_writer=_writer(sink)
        )
        await orch._write_rationales(
            log, record, "site", history_prefix=_rationale_history(record)
        )

        assert [
            action["params"]["url"] for action, _ in sink[0]["history"]
        ] == ["https://search.test/"]

    async def test_第二站首步能看到搜索与前一站(self):
        """接线测试：第二个候选站的进站步拿到的是 **run 级** history。

        不带前缀时 len == 0（单元级口径的原症状），带前缀时
        == 搜索步 1 + 第一站全部步——这正是「首次操作」不再自相矛盾的
        数据前提。
        """
        sink: list[dict] = []
        search = tv.TestClassifyCandidatesWiring.SEARCH
        pages = {
            search: Observation(
                url=search,
                page_title="百度",
                body_text="搜索结果",
                links=(
                    LinkItem(text="a站", href="https://www.a.test/movie"),
                    LinkItem(text="b站", href="https://www.b.test/movie"),
                ),
            ),
            "https://www.a.test/movie": tv.obs_at(
                "https://www.a.test/movie",
                elements=(("e1", "button", "播放"),),
            ),
            "https://www.b.test/movie": tv.obs_at(
                "https://www.b.test/movie",
                elements=(("e1", "button", "播放"),),
            ),
            "https://x.test/play/e1": tv.obs_at(
                "https://x.test/play/e1", video=1
            ),
        }
        script = {
            Q.CLASSIFY_SEARCH_LINKS: tv.ok(
                Q.CLASSIFY_SEARCH_LINKS,
                {
                    "video_site": [
                        {"url": "https://www.a.test/movie", "why": "含片名"},
                        {"url": "https://www.b.test/movie", "why": "含片名"},
                    ]
                },
            ),
            Q.IS_REACHABLE: tv.ok(Q.IS_REACHABLE),
            Q.FIND_PLAY_CONTROL: tv.ok(Q.FIND_PLAY_CONTROL, {"ref": "e1"}),
            Q.PLAYER_OK: tv.ok(Q.PLAYER_OK),
        }
        record = await Orchestrator(
            tv.FakeDriver(pages),
            tv.ScriptedPerceptor(script),
            RunConfig(),
            rationale_writer=_writer(sink),
        ).run("T001", "功夫", user_prompt="有能看的吗")

        assert len(record.visits) == 2, "fixture 没造出两个站点——没测到接线"
        site_b_gotos = [
            call
            for call in sink
            if call["action"]["tool"] == "goto"
            and call["action"]["params"].get("url")
            == "https://www.b.test/movie"
        ]
        assert site_b_gotos, "writer 没收到第二个站点的进站步"
        history = site_b_gotos[0]["history"]
        assert len(history) == 1 + len(record.visits[0].steps.steps)
        assert history[0][0]["params"]["url"] == search