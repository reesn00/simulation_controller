"""all80 批次事故的两条 **P0 回归门禁**（2026-11-11）。

复盘存档：``trajectory_pipeline/docs/11-all80批次执行事故复盘.md``。
本文件只钉「**不许再犯**」的那几条，不复述复盘。

P0-1 **执行层故障被降级成 warning ⇒ 整批 exit 0**
    坏的是**我们这一侧**（浏览器会话 / 建页路由 / 上游配额）⇒ 那个站点
    **一次都没被试过** ⇒ 那条 run **没有发生**，不是「跑完但没结果」。
    原来 ``orchestrator.run`` 用 ``except Exception`` 把它兜住、记一条
    warning、**提前 return 一份已封口的记录** ⇒ 批次层拿到的是一份
    0 候选 0 访问的**退化壳** ⇒ ``failed`` 清单恒空 ⇒ **exit 0**
    （实测 53/80 条退化壳、日志 0 处 ``[FAIL]``）。

P0-2 **camofox tab 泄漏 ⇒ 上游配额打满**
    上游 tab 配额 = 100 且**跨 session 全局**（上游 ``MAX_TABS_GLOBAL``）
    ⇒ 每候选开一张不关 ⇒ 第 28 条起整批建页 429。根因两处：
    (a) ``CamofoxSession.close()`` 调的是**不存在**的 ``close_tab``
        （真名 ``close_page``）⇒ 每次抛 ``AttributeError`` 被吞
        ⇒ **关页从未发起**；
    (b) ``visit`` / ``agent_visit`` 每候选开一张页**从不关**。

⚠️ **P0-1 的处置不许被「重新降级」**：执行层故障**必须**冒到批次层——
批次层是**逐条**兜的（一条失败只毁那一条），而 orchestrator 里兜掉它
等于「这条没发生」被写成「这条跑完了但没结果」。
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

import pytest

from trajectory_pipeline.executor import cli
from trajectory_pipeline.executor.agent_loop import AgentBackend
from trajectory_pipeline.executor.branches import RunLedger
from trajectory_pipeline.executor.browser.page_driver import (
    DriverError,
    DriverSessionError,
)
from trajectory_pipeline.executor.orchestrator import Orchestrator, RunConfig, RunRecord
from trajectory_pipeline.executor.steps.agent_visit import agent_visit
from trajectory_pipeline.executor.steps.search import Candidate
from trajectory_pipeline.executor.steps.visit import visit_site
from trajectory_pipeline.perception.base import (
    Decision,
    InteractiveElement,
    LinkItem,
    Observation,
    Q,
)
from trajectory_pipeline.tests.fakes import FakeAgentLLM


# ═══════════════════════════════════════════════════════════════════════
# 替身
# ═══════════════════════════════════════════════════════════════════════

CAND_URL = "https://x.test/movie"


class _ScriptedPerceptor:
    """按题返回预设答案。**未配置的题一律 ``None``**（不猜）。"""

    name = "scripted"

    def __init__(self, script: dict[str, Decision]) -> None:
        self.script = script
        self.calls: list[str] = []

    def decide(self, question: str, obs: Observation) -> Decision:
        self.calls.append(question)
        d = self.script.get(question)
        if d is None:
            return Decision(question, None, 1.0, "未配置", "rule", {})
        return Decision(question, d.answer, 1.0, d.evidence, "rule", d.payload)


def _d(question: str, answer: bool | None, payload: dict | None = None) -> Decision:
    return Decision(question, answer, 1.0, "证据", "rule", payload or {})


def _site_obs() -> Observation:
    """站点页：带一个可点的播放控件（``e1``）。"""
    return Observation(
        url=CAND_URL,
        page_title="电影站",
        body_text="正文",
        interactive_elements=(
            InteractiveElement(ref="e1", role="button", name="播放"),
        ),
    )


class _FakeDriver:
    """visit / agent 链的假驱动。

    **开页失败可切换**——那是 P0-2 的靶子（开页失败时这一站必须中止，
    而不是「就在当前页继续」）。``closed`` 记关掉的页句柄。
    """

    def __init__(
        self,
        *,
        new_tab_fails: bool = False,
        new_tab_session_fails: bool = False,
    ) -> None:
        self.new_tab_fails = new_tab_fails
        #: **建页撞上上游配额** ⇒ 执行层故障（缺口 A 的现场形状）。
        self.new_tab_session_fails = new_tab_session_fails
        self.visited: list[str] = []
        #: 开过的页（URL）。
        self.tabs: list[str] = []
        #: 开过的页的**句柄**——``close_tab`` 收的是它。
        self.opened: list[str] = []
        #: 关掉的页的句柄。
        self.closed: list[str] = []
        self.clicked: list[str] = []
        self.current: Observation = _site_obs()

    async def goto(self, url: str, *, wait_until: str = "load") -> None:
        self.visited.append(url)

    async def observe(self, *, max_chars: int | None = None) -> Observation:
        return self.current

    async def click(self, *, ref: str | None = None, **kw: Any) -> None:
        self.clicked.append(ref or "")

    async def fill(self, *, text: str, **kw: Any) -> None: ...

    async def press(self, key: str, **kw: Any) -> None: ...

    async def new_tab(self, url: str | None = None) -> str:
        if self.new_tab_session_fails:
            raise DriverSessionError(
                "camofox 建页失败（执行层，不是站点打不开）: 429 "
                "Maximum tabs per session reached"
            )
        if self.new_tab_fails:
            raise DriverError("camofox 开新页失败: 429 Maximum tabs per session reached")
        self.tabs.append(url or "")
        handle = f"tab{len(self.tabs)}"
        self.opened.append(handle)
        return handle

    async def close_tab(self, tab_id: str | None = None) -> None:
        self.closed.append(tab_id or "")

    async def close(self) -> None: ...


class _会话失效驱动:
    """搜索页导航就**执行层故障**（坏的是我们这一侧）。

    复现事故里那一招的**形状**：建页路由 429 ⇒ 驱动抛
    :class:`DriverSessionError` ⇒ 站点**一次都没被试过**。
    """

    fingerprint: dict[str, Any] = {}

    async def goto(self, url: str, *, wait_until: str = "load") -> None:
        raise DriverSessionError(
            "camofox 建页失败（执行层，不是站点打不开）: 429 "
            "Maximum tabs per session reached"
        )

    async def observe(self, *, max_chars: int | None = None) -> Observation:
        raise AssertionError("导航失败后不该再观察")


# ═══════════════════════════════════════════════════════════════════════
# P0-1 · 执行层故障不许降级成 warning
# ═══════════════════════════════════════════════════════════════════════


class TestP0_1_执行层故障不许降级:
    async def test_搜索页执行层故障原样抛出(self):
        """**原样抛出**，不返回一份「已封口的记录」。

        ⚠️ 原来这里兜住异常 → 记一条 warning → **提前 return 一份 0 候选
        0 访问的记录** ⇒ 批次层以为这条跑完了 ⇒ 整批 exit 0。
        """
        orch = Orchestrator(_会话失效驱动(), _ScriptedPerceptor({}), RunConfig())
        with pytest.raises(DriverSessionError):
            await orch.run("T001", "功夫")

    async def test_站点层异常仍折算成warning(self):
        """**处置要窄**：只有执行层故障原样抛，其它异常照旧折算进 warnings。

        「一条没跑成不许毁整批」说的是**批次层逐条兜**，不是「所有异常
        都往上抛」——后者会让一条站点的普通失败毁掉整批。
        """

        class _普通失败驱动(_会话失效驱动):
            async def goto(self, url: str, *, wait_until: str = "load") -> None:
                raise DriverError("导航失败（这次没打开·证明不了站点不可达）: 超时")

        orch = Orchestrator(_普通失败驱动(), _ScriptedPerceptor({}), RunConfig())
        rec = await orch.run("T001", "功夫")
        assert rec.candidates == []
        assert rec.warnings, "站点层失败仍要折算进 warnings"
        assert rec.succeeded is False


class TestP0_1_批次层逐条兜:
    """批次层是**逐条**兜的：一条执行层故障只毁那一条。"""

    @staticmethod
    def _stub(monkeypatch, *, fail_on: set[str], exc: type[Exception] = DriverSessionError):
        """打桩 ``cli.Orchestrator``：指定 task_id 抛执行层故障。"""
        ran: list[str] = []

        class _Orch:
            def __init__(self, driver, perceptor, cfg, **kw):
                self.driver = driver

            async def run(self, task_id, title, **kw):
                ran.append(task_id)
                if task_id in fail_on:
                    raise exc(
                        "camofox 建页失败（执行层，不是站点打不开）: 429"
                    )
                return RunRecord(
                    task_id=task_id, title=title, query="q", search_url="u",
                    driver="camofox",
                )

        class _FakeSession:
            fingerprint = {"engine": "camoufox"}

            async def close(self) -> None: ...

        async def _launch():
            return _FakeSession()

        monkeypatch.setattr(cli, "Orchestrator", _Orch)
        monkeypatch.setattr(cli, "_launch_camofox", _launch)
        monkeypatch.setattr(cli, "make_driver", lambda *a, **kw: object())
        monkeypatch.setattr(cli, "_execution_layer_missing_reason", lambda kind: "")
        monkeypatch.setattr(cli, "_make_perceptor",
                            lambda *a, **kw: type("P", (), {"name": "rule"})())
        monkeypatch.setattr(cli, "_dispatch_l6_summary", lambda *a, **kw: None)
        monkeypatch.setattr(cli, "_print_summary", lambda *a, **kw: None)
        return ran

    @staticmethod
    def _stub_real_orchestrator(monkeypatch, *, driver_cls=None,
                                perceptor=None):
        """**不桩 orchestrator**——让真的那个跑，端到端。

        ⚠️ 桩掉 orchestrator 的那些用例只钉「批次层拿到异常之后怎么办」，
        **碰不到 P0-1 的病根**（病根在 orchestrator 里：它把异常吃掉、
        返回一份已封口的记录）⇒ 必须有一条**让真 orchestrator 跑**的用例，
        否则「orchestrator 又开始兜异常」这条退化**没人拦得住**。

        ``driver_cls`` / ``perceptor`` 让调用方换掉「哪种故障」与「感知层
        答什么」——**病根在不同的路径上**（搜索页 vs 候选派发）⇒
        两条路径各要一条端到端用例。
        """
        cls = driver_cls if driver_cls is not None else _会话失效驱动

        class _FakeSession:
            fingerprint = {"engine": "camoufox"}

            async def close(self) -> None: ...

        async def _launch():
            return _FakeSession()

        class _DriverCM:
            """**异步上下文管理器**——``cli`` 用的是
            ``task_stack.enter_async_context(_driver_for_run(...))``
            ⇒ 桩成异步生成器会在进上下文时抛 TypeError，
            那样这条用例会**空跑通过**（浏览器压根没起来 ⇒ 走的是
            「浏览器没起来」那条路 ⇒ 断言照样成立 ⇒ **假阳性门禁**）。"""

            async def __aenter__(self):
                return cls()

            async def __aexit__(self, *exc):
                return False

        def _driver_for_run(kind, *, shared=None, collect_element_basis=False):
            return _DriverCM()

        monkeypatch.setattr(cli, "_launch_camofox", _launch)
        monkeypatch.setattr(cli, "_driver_for_run", _driver_for_run)
        monkeypatch.setattr(cli, "make_driver", lambda *a, **kw: object())
        monkeypatch.setattr(cli, "_execution_layer_missing_reason", lambda kind: "")
        monkeypatch.setattr(cli, "_make_perceptor",
                            lambda *a, **kw: perceptor
                            if perceptor is not None
                            else type("P", (), {"name": "rule"})())
        monkeypatch.setattr(cli, "_dispatch_l6_summary", lambda *a, **kw: None)
        monkeypatch.setattr(cli, "_print_summary", lambda *a, **kw: None)

    @staticmethod
    def _plan_doc(task_ids: list[str]) -> dict:
        return {
            "report": {"seed": 7, "library_digest": "abc123",
                       "by_mode": {"single_title": len(task_ids)}},
            "instances": [
                {
                    "task_id": tid,
                    "persona_id": "parent-喜剧",
                    "scenario_id": "media_lookup_standard",
                    "prompt_text": "我想找到《功夫》电影在线观看的可播放网址",
                    "search_query": "功夫 在线观看",
                    "has_standard": True,
                    "retrieval_mode": "single_title",
                    "title": "功夫",
                    "rewritten": True,
                    "provenance": {"persona_id": "parent-喜剧"},
                }
                for tid in task_ids
            ],
        }

    @staticmethod
    def _args(plan: Path, out: Path) -> argparse.Namespace:
        return argparse.Namespace(
            plan=str(plan), title=None, include_unrewritten=False,
            limit=None, dry_run=False, driver="camofox", executor_mode=None,
            engine="bing", max_candidates=1, max_chars=None,
            stop_after_success=0, discovery_path="direct",
            max_per_host=0,
            max_intermediates=3, max_referral_links=5,
            per_site_timeout=None, out=str(out), task_id="T001",
            element_basis=False, headful=False,
        )

    async def test_一条执行层故障只毁那一条(self, tmp_path, monkeypatch, capsys):
        plan = tmp_path / "plan.json"
        plan.write_text(
            json.dumps(self._plan_doc(["T001", "T002", "T003"]), ensure_ascii=False),
            encoding="utf-8")
        out = tmp_path / "out"
        ran = self._stub(monkeypatch, fail_on={"T002"})

        rc = await cli._run_plan(self._args(plan, out))
        text = capsys.readouterr().out

        assert ran == ["T001", "T002", "T003"], (
            f"一条失败不该中断后面的条目：实际跑了 {ran}"
        )
        assert rc == 1, "有条目没跑成 ⇒ 退出码必须非 0"
        assert "[FAIL]" in text, f"日志里没有 [FAIL] 行：\n{text}"
        assert "T002" in text, f"失败清单里没有 T002：\n{text}"

    async def test_失败的那条不落盘(self, tmp_path, monkeypatch):
        """**退化壳不进存档**——落盘了就会被下游当正常样本入库。"""
        plan = tmp_path / "plan.json"
        plan.write_text(
            json.dumps(self._plan_doc(["T001", "T002"]), ensure_ascii=False),
            encoding="utf-8")
        out = tmp_path / "out"
        self._stub(monkeypatch, fail_on={"T002"})

        await cli._run_plan(self._args(plan, out))
        archives = sorted(p.name for p in out.glob("T*__*.json"))
        assert len(archives) == 1, f"存档落了 {archives}——失败那条不该有档"

    async def test_全部成功时退出码是0(self, tmp_path, monkeypatch):
        """**没有失败不许给非 0**——否则「没跑成」与「跑成了」分不开。"""
        plan = tmp_path / "plan.json"
        plan.write_text(
            json.dumps(self._plan_doc(["T001", "T002"]), ensure_ascii=False),
            encoding="utf-8")
        out = tmp_path / "out"
        self._stub(monkeypatch, fail_on=set())

        rc = await cli._run_plan(self._args(plan, out))
        assert rc == 0
        assert len(list(out.glob("T*__*.json"))) == 2

    async def test_手工路径同款(self, tmp_path, monkeypatch, capsys):
        """**手工路径与计划路径是两处独立调用点**——漏一处 ⇒ 批量入口不炸。"""
        out = tmp_path / "out"
        self._stub(monkeypatch, fail_on={"T001"})
        args = argparse.Namespace(
            task_id="T001", title=["功夫"], out=str(out),
            engine="bing", max_candidates=1, max_chars=None,
            stop_after_success=0, perceptor=None, discovery_path="direct",
            max_per_host=0,
            max_intermediates=3, max_referral_links=5, executor_mode=None,
            driver="camofox", per_site_timeout=None, element_basis=False,
            headful=False,
        )
        rc = await cli._run_manual(args)
        text = capsys.readouterr().out
        assert rc == 1
        assert "[FAIL]" in text, f"日志里没有 [FAIL] 行：\n{text}"
        assert not list(out.glob("T*__*.json")), "失败的那条不该落盘"

    async def test_端到端_真orchestrator的执行层故障不落盘(self, tmp_path, monkeypatch):
        """**端到端**：真 orchestrator + 真批次层 ⇒ 执行层故障不许出存档。

        ⚠️ 这是 P0-1 的**病根本身**所在的那一层：orchestrator 把异常兜住
        ⇒ 返回一份 0 候选 0 访问的**已封口记录** ⇒ 批次层以为跑完了
        ⇒ 存档照落 + 退出码 0 ⇒ 实测 53/80 条退化壳。

        ⇒ 本用例**不桩 orchestrator**：桩掉它的那些用例只钉「拿到异常之后
        怎么办」，碰不到「异常有没有冒出来」这件事。
        """
        plan = tmp_path / "plan.json"
        plan.write_text(
            json.dumps(self._plan_doc(["T001"]), ensure_ascii=False),
            encoding="utf-8")
        out = tmp_path / "out"
        self._stub_real_orchestrator(monkeypatch)

        rc = await cli._run_plan(self._args(plan, out))

        assert rc == 1, "执行层故障 ⇒ 退出码必须非 0"
        assert not list(out.glob("T*__*.json")), (
            "退化壳落盘了——下游按「文件存在」筛选会把它当正常样本入库"
        )

    async def test_执行层故障在日志里与普通失败分得开(self, tmp_path, monkeypatch, capsys):
        """**「我们这边坏了」与「站点打不开」在日志里必须分得开**。

        ⚠️ 两条分支的**处置后果不同**：执行层故障要查的是**我们的**执行层
        （配额 / 会话 / 建页路由），站点失败查的是**那个站**。
        ⇒ 日志里都写成同一句 ⇒ 排障时先找错方向。
        """
        plan = tmp_path / "plan.json"
        plan.write_text(
            json.dumps(self._plan_doc(["T001"]), ensure_ascii=False),
            encoding="utf-8")
        out = tmp_path / "out"
        self._stub(monkeypatch, fail_on={"T001"})

        await cli._run_plan(self._args(plan, out))
        text = capsys.readouterr().out
        fail_lines = [l for l in text.splitlines() if "[FAIL]" in l]
        assert fail_lines, f"没有 [FAIL] 行：\n{text}"
        assert any("执行层故障" in l for l in fail_lines), (
            f"执行层故障没被标出来：\n{text}"
        )

        plan2 = tmp_path / "plan2.json"
        plan2.write_text(
            json.dumps(self._plan_doc(["T001"]), ensure_ascii=False),
            encoding="utf-8")
        out2 = tmp_path / "out2"
        self._stub(monkeypatch, fail_on={"T001"}, exc=RuntimeError)

        await cli._run_plan(self._args(plan2, out2))
        text2 = capsys.readouterr().out
        fail_lines2 = [l for l in text2.splitlines() if "[FAIL]" in l]
        assert fail_lines2
        assert not any("执行层故障" in l for l in fail_lines2), (
            f"普通失败被标成了执行层故障（会把排障引到错方向）：\n{text2}"
        )


# ═══════════════════════════════════════════════════════════════════════
# P0-2 · 页必须关
# ═══════════════════════════════════════════════════════════════════════


class TestP0_2_每个候选的页必须关:
    """上游 tab 配额 = 100 且**跨 session 全局**⇒ 每候选开一张不关，
    一条轨迹就吃光整批的配额 ⇒ 第 28 条起整批 429。"""

    async def test_候选跑完它开的那张页被关掉(self):
        """**关的是本候选开的那张**（句柄对得上，不是「关当前页」）。"""
        driver = _FakeDriver()
        ledger = RunLedger(task_id="T1")
        perceptor = _ScriptedPerceptor({
            Q.IS_REACHABLE: _d(Q.IS_REACHABLE, True),
            Q.FIND_PLAY_CONTROL: _d(Q.FIND_PLAY_CONTROL, True, {"ref": "e1"}),
            Q.PLAYER_OK: _d(Q.PLAYER_OK, True),
        })
        result = await visit_site(
            driver, perceptor, ledger,
            Candidate(url=CAND_URL, text="电影", rank=1),
        )
        assert driver.opened, "这个用例的前提是确实开过页"
        assert driver.closed == driver.opened, (
            f"开过的页 {driver.opened} 没被关掉（只关了 {driver.closed}）"
        )
        assert result.success is True

    async def test_开页失败记unresolved不进负样本池(self):
        """**执行层故障不是站点性质** ⇒ 记「没判出来」那一支。

        ⚠️ 记成任何一支**负样本**分支，都是把「我们这边坏了」写成
        「这个站看不了」⇒ 训练信号被污染。
        """
        driver = _FakeDriver(new_tab_fails=True)
        ledger = RunLedger(task_id="T1")
        perceptor = _ScriptedPerceptor({
            Q.IS_REACHABLE: _d(Q.IS_REACHABLE, True),
            Q.FIND_PLAY_CONTROL: _d(Q.FIND_PLAY_CONTROL, True, {"ref": "e1"}),
            Q.PLAYER_OK: _d(Q.PLAYER_OK, True),
        })
        await visit_site(
            driver, perceptor, ledger,
            Candidate(url=CAND_URL, text="电影", rank=1),
        )
        assert [o.branch for o in ledger.outcomes] == ["unresolved"]
        assert driver.clicked == [], (
            "开页失败后不许在**上一个候选留下的页**上点击——判分是错的"
        )

    async def test_开页失败的动作流里留证(self):
        """开页失败**要留在动作流里**——否则事后没人知道它试过。"""
        driver = _FakeDriver(new_tab_fails=True)
        ledger = RunLedger(task_id="T1")
        perceptor = _ScriptedPerceptor({
            Q.IS_REACHABLE: _d(Q.IS_REACHABLE, True),
            Q.FIND_PLAY_CONTROL: _d(Q.FIND_PLAY_CONTROL, True, {"ref": "e1"}),
            Q.PLAYER_OK: _d(Q.PLAYER_OK, True),
        })
        result = await visit_site(
            driver, perceptor, ledger,
            Candidate(url=CAND_URL, text="电影", rank=1),
        )
        failed = [s for s in result.steps.steps if s.action.tool == "new_tab"]
        assert failed, "动作流里没有开页这一步"
        assert failed[0].error, (
            "开页失败必须带 error——否则它看起来像成功开过页"
        )

    async def test_agent链的页也必须关(self):
        """**两条链都漏**（agent 链的 ``open_tab`` 同样从不 close）。

        ⚠️ agent 链的站内动作**全都**落在那张页上 ⇒ 不关它等于把整段
        站内轨迹留在上游，而上游配额是全局的。
        """
        driver = _FakeDriver()
        ledger = RunLedger(task_id="T1")
        perceptor = _ScriptedPerceptor({
            Q.PLAYER_OK: _d(Q.PLAYER_OK, True),
        })
        llm = FakeAgentLLM([{"done": True, "report": {"reached": True,
                                                      "why": "到了播放页"}}])
        backend = AgentBackend(
            client=llm,
            json_of=lambda raw: json.loads(raw) if raw else None,
        )
        await agent_visit(
            driver, perceptor, ledger,
            Candidate(url=CAND_URL, text="电影", rank=1),
            backend,
            title="功夫",
        )
        assert driver.opened, "agent 链也应该开过页"
        assert driver.closed == driver.opened, (
            f"agent 链开过的页 {driver.opened} 没被关掉（只关了 {driver.closed}）"
        )


class TestP0_2_会话收尾失败要上报:
    """关不掉的页**仍然占着上游 tab 配额**（配额 = 100，跨 session 全局）。

    ⚠️ 本地栈会把它们摘掉 ⇒ **账实不符** ⇒ 下一批从更低的余量起步
    ⇒ 不上报就等于**让下一批莫名其妙地失败**。
    """

    @staticmethod
    def _plan_doc(task_ids: list[str]) -> dict:
        return {
            "report": {"seed": 7, "library_digest": "abc123",
                       "by_mode": {"single_title": len(task_ids)}},
            "instances": [
                {
                    "task_id": tid, "persona_id": "parent-喜剧",
                    "scenario_id": "media_lookup_standard",
                    "prompt_text": "我想找到《功夫》电影在线观看的可播放网址",
                    "search_query": "功夫 在线观看", "has_standard": True,
                    "retrieval_mode": "single_title", "title": "功夫",
                    "rewritten": True, "provenance": {"persona_id": "parent-喜剧"},
                }
                for tid in task_ids
            ],
        }

    async def test_关不掉的页进失败清单也进存档(self, tmp_path, monkeypatch, capsys):
        """**两处都要有**：失败清单（⇒ 退出码非 0）+ 存档的 warnings
        （⇒ 读档的人看得见「这一批有页没关掉」）。"""

        class _FakeDriver:
            fingerprint: dict = {}
            close_failures = ("c1: DriverError: camofox 关页失败: 404",)

            async def close(self) -> None: ...

        class _DriverCM:
            async def __aenter__(self):
                return _FakeDriver()

            async def __aexit__(self, *exc):
                return False

        class _Orch:
            def __init__(self, driver, perceptor, cfg, **kw):
                pass

            async def run(self, task_id, title, **kw):
                return RunRecord(
                    task_id=task_id, title=title, query="q", search_url="u",
                    driver="camofox",
                )

        monkeypatch.setattr(cli, "Orchestrator", _Orch)
        monkeypatch.setattr(cli, "_driver_for_run",
                            lambda *a, **kw: _DriverCM())
        monkeypatch.setattr(cli, "make_driver", lambda *a, **kw: object())
        monkeypatch.setattr(cli, "_execution_layer_missing_reason", lambda kind: "")
        monkeypatch.setattr(cli, "_make_perceptor",
                            lambda *a, **kw: type("P", (), {"name": "rule"})())
        monkeypatch.setattr(cli, "_dispatch_l6_summary", lambda *a, **kw: None)
        monkeypatch.setattr(cli, "_print_summary", lambda *a, **kw: None)

        plan = tmp_path / "plan.json"
        plan.write_text(
            json.dumps(self._plan_doc(["T001"]), ensure_ascii=False),
            encoding="utf-8")
        out = tmp_path / "out"
        args = argparse.Namespace(
            plan=str(plan), title=None, include_unrewritten=False,
            limit=None, dry_run=False, driver="camofox", executor_mode=None,
            engine="bing", max_candidates=1, max_chars=None,
            stop_after_success=0, discovery_path="direct", max_per_host=0,
            max_intermediates=3, max_referral_links=5, per_site_timeout=None,
            out=str(out), task_id="T001", element_basis=False, headful=False,
        )

        rc = await cli._run_plan(args)
        text = capsys.readouterr().out

        assert rc == 1, "关不掉页 ⇒ 退出码必须非 0（下一批会撞配额）"
        assert "[FAIL]" in text, f"没有 [FAIL] 行：\n{text}"
        archives = list(out.glob("T*__*.json"))
        assert len(archives) == 1, f"存档数不对：{archives}"
        blob = archives[0].read_text(encoding="utf-8")
        assert "会话收尾失败" in blob, (
            "存档里没有这条 warning——读档的人就看不见「这批有页没关掉」"
        )



# ═══════════════════════════════════════════════════════════════════════
# 缺口 A · 站点遍历阶段的执行层故障（P0-1 的续集）
# ═══════════════════════════════════════════════════════════════════════
#
# P0-1 修的是**搜索页**那一段：导航就炸 ⇒ 原样抛 ⇒ 批次层记失败。
# **缺口 A 是同一件事的另一半**：搜索页**正常**、候选**取到了**，
# **建页**才撞上上游配额（all80 那 53 条的现场形状）⇒
#
#   visit.open_tab → 降级成 per-site ``unresolved``
#   → 整条 run 正常收口、落盘、**退出码 0**
#   → 产出「结构完整、全是 unresolved、字节还很大」的档
#     （看着最像正常轨迹，实则**判分不可信**）
#
# ⚠️ 这是 all80 事故里**最危险的一类**：报告上看不出任何异常，
# 而样本池里已经混进了「我们这边坏了」的产物。


class _候选建页故障驱动:
    """搜索页**正常**，**候选建页**才是执行层故障（缺口 A 的现场形状）。

    ⚠️ 必须与 :class:`_会话失效驱动`（导航就炸）分开：两者的**病根**
    在不同的代码路径上（搜索页兜底 vs 候选派发兜底）⇒ 一条用例只拦得住
    其中一条。
    """

    fingerprint: dict[str, Any] = {"engine": "camoufox"}

    def __init__(self) -> None:
        self._obs_n = 0
        self.visited: list[str] = []

    async def goto(self, url: str, *, wait_until: str = "load") -> None:
        self.visited.append(url)

    async def observe(self, *, max_chars: int | None = None) -> Observation:
        self._obs_n += 1
        if self._obs_n == 1:
            # 搜索页：带一个可取候选的链接。
            return Observation(
                url="https://search.test/s?q=gongfu",
                page_title="搜索结果",
                body_text="结果列表",
                links=(
                    LinkItem(text="功夫 在线观看",
                             href="https://x.test/movie"),
                ),
            )
        return _site_obs()

    async def new_tab(self, url: str | None = None) -> str:
        raise DriverSessionError(
            "camofox 建页失败（执行层，不是站点打不开）: 429 "
            "Maximum tabs per session reached"
        )


def _站点感知() -> _ScriptedPerceptor:
    """站点页的**肯定**答案（②可达、③有播放控件）。

    ⚠️ **分类题故意不配**：分类题 fail-closed ⇒ 候选按启发式**全遍历**
    （既有纪律：``None`` 不中断采集）⇒ 流程才会走到候选建页那一步。
    """
    return _ScriptedPerceptor({
        Q.IS_REACHABLE: _d(Q.IS_REACHABLE, True),
        Q.FIND_PLAY_CONTROL: _d(Q.FIND_PLAY_CONTROL, True, {"ref": "e1"}),
        Q.PLAYER_OK: _d(Q.PLAYER_OK, True),
    })


class Test缺口A_候选建页失败不许降级:
    async def test_visit层原样抛出(self):
        """**visit 层不许把执行层故障兜成 per-site 结论**。"""
        driver = _FakeDriver(new_tab_session_fails=True)
        ledger = RunLedger(task_id="T1")
        perceptor = _ScriptedPerceptor({
            Q.IS_REACHABLE: _d(Q.IS_REACHABLE, True),
            Q.FIND_PLAY_CONTROL: _d(Q.FIND_PLAY_CONTROL, True, {"ref": "e1"}),
            Q.PLAYER_OK: _d(Q.PLAYER_OK, True),
        })
        with pytest.raises(DriverSessionError):
            await visit_site(
                driver, perceptor, ledger,
                Candidate(url=CAND_URL, text="电影", rank=1),
            )

    async def test_普通建页失败仍记unresolved(self):
        """**对照**：普通异常 ⇒ 仍是「这一站没打开」⇒ 记 ``unresolved``。

        ⚠️ 这条对照很重要：执行层故障与站点故障的**处置不同**，
        但**处置的边界要窄**——把普通失败也升级成执行层故障，
        会让「这个站这次没打开」也毁掉整批（每站都会）。
        """
        driver = _FakeDriver(new_tab_fails=True)
        ledger = RunLedger(task_id="T1")
        perceptor = _ScriptedPerceptor({
            Q.IS_REACHABLE: _d(Q.IS_REACHABLE, True),
            Q.FIND_PLAY_CONTROL: _d(Q.FIND_PLAY_CONTROL, True, {"ref": "e1"}),
            Q.PLAYER_OK: _d(Q.PLAYER_OK, True),
        })
        await visit_site(
            driver, perceptor, ledger,
            Candidate(url=CAND_URL, text="电影", rank=1),
        )
        assert [o.branch for o in ledger.outcomes] == ["unresolved"]

    async def test_执行层故障在异常里留证(self):
        """**异常里要留痕**——否则事后没人知道建页试过、怎么失败的。

        ⚠️ 异常一路冒到批次层 ⇒ 这一条**不落盘** ⇒ 动作流也一起没了 ⇒
        证据**只存在于异常消息与 ``[FAIL]`` 行**里 ⇒ 那两处必须写清楚
        「这是执行层故障」，否则读日志的人会以为「那个站打不开」。
        """
        driver = _FakeDriver(new_tab_session_fails=True)
        ledger = RunLedger(task_id="T1")
        perceptor = _ScriptedPerceptor({
            Q.IS_REACHABLE: _d(Q.IS_REACHABLE, True),
            Q.FIND_PLAY_CONTROL: _d(Q.FIND_PLAY_CONTROL, True, {"ref": "e1"}),
            Q.PLAYER_OK: _d(Q.PLAYER_OK, True),
        })
        with pytest.raises(DriverSessionError) as caught:
            await visit_site(
                driver, perceptor, ledger,
                Candidate(url=CAND_URL, text="电影", rank=1),
            )
        assert "执行层" in str(caught.value), (
            f"异常消息里没写「执行层」：{caught.value}"
        )
        assert "429" in str(caught.value), (
            f"异常消息里没有上游状态码：{caught.value}"
        )

    async def test_整条run不许正常收口(self):
        """**真 orchestrator**：候选建页炸 ⇒ 整条 run 冒异常出去。

        ⚠️ 这是缺口 A 的**病根本身**：orchestrator 的候选派发处原来
        用 ``except Exception`` 兜住 ⇒ 记一条 ``unresolved`` ⇒
        ``orch.run`` **正常返回** ⇒ 批次层以为跑完了。
        """
        orch = Orchestrator(
            _候选建页故障驱动(), _站点感知(), RunConfig(),
        )
        with pytest.raises(DriverSessionError):
            await orch.run("T001", "功夫")


class Test缺口A_端到端不进存档:
    """**端到端**：真 orchestrator + 真批次层 ⇒ 不落盘 + 退出码非 0。"""

    async def test_候选建页失败不落盘且退出码非0(self, tmp_path, monkeypatch, capsys):
        """**候选建页**炸（搜索页正常）⇒ 同样不许落盘。

        ⚠️ 与「搜索页就炸」那条**分开放**：两者的病根在不同路径上
        （搜索页兜底 vs 候选派发兜底）⇒ 只测一条 ⇒ 另一半的退化没人拦。
        """
        plan = tmp_path / "plan.json"
        plan.write_text(
            json.dumps(
                TestP0_1_批次层逐条兜._plan_doc(["T001"]),
                ensure_ascii=False),
            encoding="utf-8")
        out = tmp_path / "out"
        TestP0_1_批次层逐条兜._stub_real_orchestrator(
            monkeypatch, driver_cls=_候选建页故障驱动,
            perceptor=_站点感知(),
        )

        rc = await cli._run_plan(
            TestP0_1_批次层逐条兜._args(plan, out))
        text = capsys.readouterr().out

        assert rc == 1, "执行层故障 ⇒ 退出码必须非 0"
        assert not list(out.glob("T*__*.json")), (
            "退化壳落盘了——下游按「文件存在」筛选会把它当正常样本入库"
        )
        assert any("执行层故障" in l for l in text.splitlines() if "[FAIL]" in l), (
            f"日志里没标出「执行层故障」：\n{text}"
        )


# ═══════════════════════════════════════════════════════════════════════
# 缺口 B · 失败路径上的关页失败不许被吞
# ═══════════════════════════════════════════════════════════════════════
#
# 缺口 B 的根因：**``close_failures`` 只有在 ``session.close()`` 跑过之后
# 才读得到**（会话收尾发生在 ``async with task_stack`` 退出时），
# 而失败分支是从 with-block **里面** ``continue`` 的 ⇒
# **检查恰好在最需要它的路径上被跳过**。
#
# ⇒ 后果：run 失败 + 页没关掉 ⇒ 失败清单里只有「run 失败」，
# **没人知道上游配额被占着** ⇒ 下一批从更低的余量起步
# ⇒ 又一次「莫名其妙地失败」。


class Test缺口B_失败路径也要报关页失败:
    async def test_失败那条的关页失败也进失败清单(self, tmp_path, monkeypatch, capsys):
        """**run 失败 + 有页没关掉** ⇒ 失败清单里**两件事都要有**。"""

        class _FakeDriver:
            fingerprint: dict = {}
            close_failures = ("c1: DriverError: camofox 关页失败: 404",)

            async def close(self) -> None: ...

        class _DriverCM:
            async def __aenter__(self):
                return _FakeDriver()

            async def __aexit__(self, *exc):
                return False

        class _Orch:
            def __init__(self, driver, perceptor, cfg, **kw):
                pass

            async def run(self, task_id, title, **kw):
                raise DriverSessionError(
                    "camofox 建页失败（执行层，不是站点打不开）: 429"
                )

        monkeypatch.setattr(cli, "Orchestrator", _Orch)
        monkeypatch.setattr(cli, "_driver_for_run",
                            lambda *a, **kw: _DriverCM())
        monkeypatch.setattr(cli, "make_driver", lambda *a, **kw: object())
        monkeypatch.setattr(cli, "_execution_layer_missing_reason", lambda kind: "")
        monkeypatch.setattr(cli, "_make_perceptor",
                            lambda *a, **kw: type("P", (), {"name": "rule"})())
        monkeypatch.setattr(cli, "_dispatch_l6_summary", lambda *a, **kw: None)
        monkeypatch.setattr(cli, "_print_summary", lambda *a, **kw: None)

        plan = tmp_path / "plan.json"
        plan.write_text(
            json.dumps(
                TestP0_1_批次层逐条兜._plan_doc(["T001"]),
                ensure_ascii=False),
            encoding="utf-8")
        out = tmp_path / "out"

        rc = await cli._run_plan(
            TestP0_1_批次层逐条兜._args(plan, out))
        text = capsys.readouterr().out

        assert rc == 1
        fail_lines = [l for l in text.splitlines() if "[FAIL]" in l]
        assert fail_lines, f"没有 [FAIL] 行：\n{text}"
        assert any("会话收尾失败" in l for l in fail_lines), (
            f"关页失败被吞了——上游配额被占着，下一批会莫名其妙地失败：\n{text}"
        )

    async def test_成功那条的关页失败也进失败清单(self, tmp_path, monkeypatch, capsys):
        """**成功路径原本就报**（P0-2 已修）——这条钉住「别改回去」。"""
        plan = tmp_path / "plan.json"
        plan.write_text(
            json.dumps(
                TestP0_1_批次层逐条兜._plan_doc(["T001"]),
                ensure_ascii=False),
            encoding="utf-8")
        out = tmp_path / "out"

        class _FakeDriver:
            fingerprint: dict = {}
            close_failures = ("c1: DriverError: camofox 关页失败: 404",)

            async def close(self) -> None: ...

        class _DriverCM:
            async def __aenter__(self):
                return _FakeDriver()

            async def __aexit__(self, *exc):
                return False

        class _Orch:
            def __init__(self, driver, perceptor, cfg, **kw):
                pass

            async def run(self, task_id, title, **kw):
                return RunRecord(
                    task_id=task_id, title=title, query="q", search_url="u",
                    driver="camofox",
                )

        monkeypatch.setattr(cli, "Orchestrator", _Orch)
        monkeypatch.setattr(cli, "_driver_for_run",
                            lambda *a, **kw: _DriverCM())
        monkeypatch.setattr(cli, "make_driver", lambda *a, **kw: object())
        monkeypatch.setattr(cli, "_execution_layer_missing_reason", lambda kind: "")
        monkeypatch.setattr(cli, "_make_perceptor",
                            lambda *a, **kw: type("P", (), {"name": "rule"})())
        monkeypatch.setattr(cli, "_dispatch_l6_summary", lambda *a, **kw: None)
        monkeypatch.setattr(cli, "_print_summary", lambda *a, **kw: None)

        rc = await cli._run_plan(
            TestP0_1_批次层逐条兜._args(plan, out))
        text = capsys.readouterr().out

        assert rc == 1
        fail_lines = [l for l in text.splitlines() if "[FAIL]" in l]
        assert any("会话收尾失败" in l for l in fail_lines), (
            f"成功路径的关页失败没上报：\n{text}"
        )
