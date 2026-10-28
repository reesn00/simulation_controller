"""``executor/steps/agent_visit`` 的离线单测（方案 13 §3.7，A2a-3）。

测的是**薄壳本身**的三件事，不是 :func:`agent_loop`（那 74 例在
``test_agent_loop.py``，这里只测它上下游的接线）：

1. **进站段与 ⑦ 段是共用的，不是复制**——两条链的口径若在某天分叉，
   这里要红（:class:`Test与scripted同形`）；
2. **站内的那一段确实交给了循环**——本文件里没有页面语义；
3. **落出的 P1 与 scripted 同形**——下游（分支记账、P2/P3 重建）看不出
   这批是 agent 批。这不只是整洁：A3 的 A/B 能只加一个
   ``executor_mode`` 字段就成立，前提就是两批在别的字段上全同形。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field

import pytest

from trajectory_pipeline.executor.actions import TOOLS
from trajectory_pipeline.executor.agent_loop import AgentBackend
from trajectory_pipeline.executor.branches import RunLedger
from trajectory_pipeline.executor.browser.page_driver import (
    DriverError,
    DriverUnreachableError,
)
from trajectory_pipeline.executor.orchestrator import RunRecord
from trajectory_pipeline.executor.steps.agent_visit import agent_visit
from trajectory_pipeline.executor.steps.search import Candidate
from trajectory_pipeline.executor.steps.visit import visit_site
from trajectory_pipeline.llm.schema_parse import extract_json, strip_think
from trajectory_pipeline.perception.base import (
    Decision,
    InteractiveElement,
    LinkItem,
    Observation,
    Q,
)
from trajectory_pipeline.tests.fakes import FakeAgentLLM

CAND_URL = "https://x.test/"


# ═══════════════════════════════════════════════════════════════════════
# Fake 与工具
# ═══════════════════════════════════════════════════════════════════════


def _obs(
    url: str = CAND_URL,
    title: str = "首页",
    body: str = "欢迎来到本站，这里有好多影视资源",
    refs: tuple[str, ...] = ("e1", "e2"),
    links: tuple[tuple[str, str], ...] = (),
    **kw: object,
) -> Observation:
    return Observation(
        url=url,
        page_title=title,
        body_text=body,
        interactive_elements=tuple(
            InteractiveElement(ref=r, role="a", name=f"入口{r}") for r in refs
        ),
        links=tuple(LinkItem(text=t, href=h) for t, h in links),
        **kw,  # type: ignore[arg-type]
    )


_SITE = _obs(url=CAND_URL, title="首页", refs=("e1", "e2"))
_PLAY = _obs(
    url="https://x.test/play/1",
    title="播放页",
    body="正在播放 功夫 第一集，画质高清",
    refs=("e9",),
)
_PLAY_BROKEN = _obs(
    url="https://x.test/play/1",
    title="播放页",
    body="影片加载失败，请稍后再试，当前没有可播放的组件",
    refs=("e9",),
)
_LOGIN = _obs(
    url="https://x.test/login",
    title="登录",
    body="请登录后再观看本片，未登录用户只能试看六分钟",
    refs=("e1",),
)
_LOGIN2 = _obs(
    url="https://x.test/login2",
    title="登录",
    body="请登录后再观看，登录后即可播放完整版",
    refs=("e2",),
)
#: 站内搜索的结果页。``e1``/``e2`` 都在，于是这条链上的每一步
#: 都能通过 I6——测的是动作流形态，不是 ref 校验（那在 test_agent_loop）。
_RESULT = _obs(
    url="https://x.test/s?q=%E5%8A%9F%E5%A4%AB",
    title="搜索结果",
    body="功夫 在线观看 第一集 高清",
    refs=("e1", "e2"),
)
#: 步数预算那条的**粘住页**：refs 与结果页相同，于是交替点两个元素
#: 时不会因为「页面上没有这个 ref」被 I6 打断——那会把预算测试
#: 变成 I6 测试。
_STUCK = _obs(
    url="https://x.test/play/1",
    title="播放页",
    body="正在播放 功夫 第一集",
    refs=("e1", "e2"),
)


class FakeDriver:
    """``URL → 页面`` 的主干 + 点击/按键按队列推进。

    :class:`test_agent_loop.FakeDriver` 是纯队列式（进站那一段由调用方
    做完了）；这里必须**按 URL 判可达**——``enter_site`` 要靠
    "未登记的 URL 报导航失败" 才能测到 ``unreachable_hard`` 那一支。
    站内几步仍用队列：那部分与循环体共用同一个道理。
    """

    name = "fake-visit"

    def __init__(
        self,
        pages: dict[str, Observation] | None = None,
        *,
        after: tuple[Observation, ...] = (),
        goto_fails: tuple[str, ...] = (),
        goto_unreachable: tuple[str, ...] = (),
        observe_fails: int = 0,
    ) -> None:
        self._pages = dict(pages or {})
        self._queue = list(after)
        self._goto_fails = set(goto_fails)
        #: 导航失败但**机器码证明**站点不可达（M-44）⇒ 记
        #: ``unreachable_hard``（真负样本）。
        self._goto_unreachable = set(goto_unreachable)
        self._observe_fails = observe_fails
        self.current: Observation | None = None
        self.visited: list[str] = []
        self.clicked: list[str] = []
        self.typed: list[tuple[str, str]] = []
        self.pressed: list[str] = []
        self.tabs: list[str] = []
        self.observe_calls = 0

    async def goto(self, url: str, *, wait_until: str = "load") -> None:
        self.visited.append(url)
        if url in self._goto_unreachable:
            raise DriverUnreachableError(
                f"fake: 站点不可达 {url}", code="NS_ERROR_UNKNOWN_HOST"
            )
        if url in self._goto_fails or url not in self._pages:
            raise DriverError(f"fake: 导航失败 {url}")
        self.current = self._pages[url]

    async def click(
        self, *, ref: str | None = None, selector: str | None = None
    ) -> None:
        self.clicked.append(ref or selector or "")
        self._advance()

    async def fill(
        self,
        *,
        text: str,
        ref: str | None = None,
        selector: str | None = None,
        role: str | None = None,
        name: str | None = None,
    ) -> None:
        self.typed.append((ref or "", text))

    async def press(self, key: str, *, role: str | None = None, name: str | None = None) -> None:
        self.pressed.append(key)
        self._advance()

    def _advance(self) -> None:
        if self._queue:
            self.current = self._queue.pop(0)

    async def observe(self, *, max_chars: int | None = None) -> Observation:
        self.observe_calls += 1
        if self._observe_fails > 0:
            self._observe_fails -= 1
            raise DriverError("fake: 取不到快照")
        if self.current is None:
            raise DriverError("fake: 没有当前页")
        return self.current

    async def new_tab(self, url: str | None = None) -> str:
        self.tabs.append(url or "")
        return f"tab{len(self.tabs)}"

    async def close(self) -> None: ...


def _driver(
    entry: Observation | None = None,
    *,
    after: tuple[Observation, ...] = (),
    goto_fails: tuple[str, ...] = (),
    goto_unreachable: tuple[str, ...] = (),
    observe_fails: int = 0,
) -> FakeDriver:
    """进站页登记在候选 URL 上，``after`` 是点击/按键依次抵达的页面。"""
    return FakeDriver(
        {CAND_URL: entry if entry is not None else _SITE},
        after=after,
        goto_fails=goto_fails,
        goto_unreachable=goto_unreachable,
        observe_fails=observe_fails,
    )


def _decision(question: str, answer: bool | None, payload=None, evidence="e"):
    return Decision(question, answer, 1.0, evidence, "rule", payload or {})


class ScriptedPerceptor:
    """按题返回预设答案。**未配置的题一律返回 ``None``**（不猜）。

    两条链共用同一个假实现，正是「⑦ 那一段共用」在测试侧的对应物：
    agent 链只问 ``PLAYER_OK``，scripted 链要问
    ``IS_REACHABLE`` / ``FIND_PLAY_CONTROL`` / ``PLAYER_OK``。
    若某天 agent 链开始问别的题，这里会立刻暴露——它拿到 None。
    """

    name = "scripted"

    def __init__(self, script: dict[str, Decision] | None = None) -> None:
        self.script = dict(script or {})
        self.calls: list[str] = []

    def decide(self, question: str, obs: Observation) -> Decision:
        self.calls.append(question)
        d = self.script.get(question)
        if d is None:
            return _decision(question, None, evidence="未配置")
        return d


def _player(answer: bool | None) -> ScriptedPerceptor:
    """只答 ⑦ 的 perceptor（agent 链的形态）。"""
    return ScriptedPerceptor({Q.PLAYER_OK: _decision(Q.PLAYER_OK, answer)})


def _cand(url: str = CAND_URL, text: str = "站点") -> Candidate:
    return Candidate(url=url, text=text, rank=1)


@dataclass
class _Ctx:
    """一次 ``agent_visit`` 的全部可观察产物。"""

    result: object
    ledger: RunLedger
    events: list[dict] = field(default_factory=list)
    driver: FakeDriver | None = None
    llm: FakeAgentLLM | None = None

    @property
    def actions(self) -> list[str]:
        return [s.action.tool for s in self.result.steps.steps]

    @property
    def branches(self) -> list[str]:
        return [o.branch or "" for o in self.ledger.outcomes]

    @property
    def notes(self) -> str:
        return " | ".join(self.result.notes or [])


def _backend(llm: FakeAgentLLM) -> AgentBackend:
    return AgentBackend(
        client=llm,
        json_of=lambda raw: extract_json(strip_think(raw)),
    )


async def _run(
    llm: FakeAgentLLM,
    driver: FakeDriver,
    perceptor: ScriptedPerceptor,
    *,
    title: str = "功夫",
) -> _Ctx:
    ledger = RunLedger(task_id="T1")
    events: list[dict] = []
    result = await agent_visit(
        driver,
        perceptor,
        ledger,
        _cand(),
        _backend(llm),
        title=title,
        gatekeeper_events=events,
    )
    return _Ctx(result=result, ledger=ledger, events=events,
                driver=driver, llm=llm)


def _done(reached: bool = True, why: str = "到了播放页") -> dict:
    return {"done": True, "report": {"reached": reached, "why": why}}


# ═══════════════════════════════════════════════════════════════════════
# 进站段（与 scripted 共用）
# ═══════════════════════════════════════════════════════════════════════


class Test进站段:
    async def test_导航失败记unresolved且不问模型(self):
        """站都进不去就不该让模型进来——那一步的 llm_call 是白烧的。

        ⚠️ **分支是 ``unresolved`` 而不是 ``unreachable_hard``**（M-44
        裁定，owner 2026-10-27）：超时 / 打断 / 建页失败这些形态
        **证明不了**站点不可达 ⇒ 记「没判出来」那一支（**不进池**）。
        """
        ctx = await _run(
            FakeAgentLLM([{"tool": "click", "ref": "e1"}]),
            _driver(goto_fails=(CAND_URL,)),
            _player(True),
        )
        assert ctx.branches == ["unresolved"]
        assert ctx.llm.calls == []
        # 试过且失败：动作流里那一步必须带 error，否则负样本池里这条
        # 会变成「没有动作的判决」。
        assert ctx.result.steps.steps[0].action.tool == "goto"
        assert ctx.result.steps.steps[0].error

    async def test_真站点不可达记unreachable_hard且不问模型(self):
        """**机器码证明**站点不可达（DNS 解析不出来 / 连接被拒）
        ⇒ 记 ``unreachable_hard``（真负样本要留住）。"""
        ctx = await _run(
            FakeAgentLLM([{"tool": "click", "ref": "e1"}]),
            _driver(goto_unreachable=(CAND_URL,)),
            _player(True),
        )
        assert ctx.branches == ["unreachable_hard"]
        assert ctx.llm.calls == []

    async def test_进站快照取不到同上(self):
        """导航**成功**了 ⇒ 站点是通的 ⇒ 快照失败**证明不了**站点不可达
        ⇒ 记 ``unresolved``（**不进池**，M-44）。"""
        ctx = await _run(
            FakeAgentLLM([{"tool": "click", "ref": "e1"}]),
            _driver(observe_fails=1),
            _player(True),
        )
        assert ctx.branches == ["unresolved"]
        assert "取不到快照" in ctx.notes
        assert ctx.llm.calls == []

    async def test_进站后开独立tab(self):
        """会话隔离与 scripted 同款：站点页留在原 tab，站内几步在新 tab。"""
        ctx = await _run(
            FakeAgentLLM([{"tool": "click", "ref": "e1"}, _done()]),
            _driver(after=(_PLAY,)),
            _player(True),
        )
        assert ctx.driver.tabs == [CAND_URL]
        assert "new_tab" in ctx.actions

    async def test_重定向照实记landed_url(self):
        entry = _obs(url="https://x.test/home", title="首页")
        driver = _driver(entry)
        ctx = await _run(
            FakeAgentLLM([{"tool": "click", "ref": "e1"}, _done()]),
            driver,
            _player(True),
        )
        assert ctx.result.landed_url == "https://x.test/home"
        assert "被重定向" in ctx.notes


# ═══════════════════════════════════════════════════════════════════════
# ⑦ 段：PLAYER_OK 与记账（与 scripted 共用）
# ═══════════════════════════════════════════════════════════════════════


class Test播放页判定:
    async def test_到了且可用则成功(self):
        ctx = await _run(
            FakeAgentLLM([{"tool": "click", "ref": "e1"}, _done()]),
            _driver(after=(_PLAY,)),
            _player(True),
        )
        assert ctx.result.success
        assert ctx.result.player_obs is _PLAY
        # ⑦ 只判一次，且判的是**循环看到的那一页**——不多取一次快照。
        assert ctx.result.player_obs is ctx.result.steps.steps[-1].observation
        assert ctx.driver.observe_calls == 2     # 进站 1 + 点击后 1

    async def test_到了但不可用记component_unverified(self):
        ctx = await _run(
            FakeAgentLLM([{"tool": "click", "ref": "e1"}, _done()]),
            _driver(after=(_PLAY_BROKEN,)),
            _player(False),
        )
        assert not ctx.result.success
        assert ctx.branches == ["component_unverified"]

    async def test_落地即在播放页且判不可用时降级为unresolved(self):
        """深层链接候选：一步没走，进站页就是播放页。

        守卫（``branches._precondition_failed``）看到「没换页」，于是
        ``component_unverified`` 的前置事实不成立 → 降级 unresolved。
        这是**刻意的保守**：agent 说「到了」是自报，未经跨页证实。
        """
        ctx = await _run(
            FakeAgentLLM([_done()]),
            _driver(entry=_PLAY_BROKEN),
            _player(False),
        )
        assert not ctx.result.success
        assert ctx.branches == ["unresolved"]
        assert "本该记 component_unverified" in " ".join(
            o.evidence for o in ctx.ledger.outcomes
        )

    async def test_落地即在播放页且判可用则照常成功(self):
        """同形态下 PLAYER_OK=True 不受守卫影响——成功不贴负标签。"""
        ctx = await _run(
            FakeAgentLLM([_done()]),
            _driver(entry=_PLAY),
            _player(True),
        )
        assert ctx.result.success
        assert ctx.actions == ["goto", "new_tab"]

    async def test_判不出来记player_unverified(self):
        ctx = await _run(
            FakeAgentLLM([{"tool": "click", "ref": "e1"}, _done()]),
            _driver(after=(_PLAY,)),
            _player(None),
        )
        assert not ctx.result.success
        assert ctx.branches == ["player_unverified"]

    async def test_站内多步的动作流按序记录(self):
        """fill/press 这条站内搜索链——scripted 里是专用代码路径，
        agent 里是涌现行为，落进 P1 的形态必须一样。"""
        ctx = await _run(
            FakeAgentLLM([
                {"tool": "fill", "ref": "e1", "params": {"text": "功夫"}},
                {"tool": "press", "params": {"key": "Enter"}},
                {"tool": "click", "ref": "e2"},
                _done(),
            ]),
            # 填搜索框**不换页**（本 fake 里 fill 不推进队列），
            # 回车到结果页，点结果进播放页。
            _driver(after=(_RESULT, _PLAY)),
            _player(True),
        )
        assert ctx.actions == ["goto", "new_tab", "fill", "press", "click"]
        assert ctx.driver.typed == [("e1", "功夫")]
        assert ctx.driver.pressed == ["Enter"]
        assert ctx.result.success


# ═══════════════════════════════════════════════════════════════════════
# 没到达的两种形态
# ═══════════════════════════════════════════════════════════════════════


class Test收口:
    async def test_自报放弃记unresolved(self):
        ctx = await _run(
            FakeAgentLLM([_done(False, "翻了三页没找到正片入口")]),
            _driver(),
            _player(True),
        )
        assert ctx.branches == ["unresolved"]
        assert not ctx.result.success
        # 模型原话要留在证据里：那是人工复核时唯一能区分
        # 「站里真没有」与「模型没找到」的东西。
        assert "翻了三页没找到正片入口" in ctx.notes or "翻了三页没找到正片入口" in " ".join(
            o.evidence or "" for o in ctx.ledger.outcomes
        )

    async def test_登录墙二次拦截记login_required(self):
        ctx = await _run(
            FakeAgentLLM([{"tool": "click", "ref": "e1"}]),
            _driver(entry=_LOGIN, after=(_LOGIN2,)),
            _player(True),
        )
        assert ctx.branches == ["login_required"]
        assert not ctx.result.success

    async def test_死循环收口记unresolved(self):
        """``never_done`` = 卡在原地只点第一个元素：死循环检测先于
        步数预算命中（3 步 vs 8 步），收口原因就写「被打转拦下」。"""
        ctx = await _run(
            FakeAgentLLM([], never_done=True),
            _driver(after=(_PLAY,)),
            _player(True),
        )
        assert ctx.branches == ["unresolved"]
        assert "dead_loop" in ctx.notes

    async def test_步数耗尽记unresolved(self):
        """交替点两个元素：三步尾部不同形，**绕开死循环检测**，
        于是命中步数预算而不是行为闸——两个闸各测一次才知道
        收口原因写的是哪一个。"""
        ctx = await _run(
            FakeAgentLLM(
                [{"tool": "click", "ref": r} for r in ("e1", "e2") * 4]
            ),
            _driver(after=(_RESULT, _STUCK)),
            _player(True),
        )
        assert ctx.branches == ["unresolved"]
        assert "步数预算耗尽" in ctx.notes
        # 预算在**花钱之前**闸住：步数用完就不再烧下一次 llm_call。
        assert len(ctx.llm.calls) == 8

    async def test_后端没作答则收口(self):
        ctx = await _run(FakeAgentLLM([], down=True), _driver(), _player(True))
        assert ctx.branches == ["unresolved"]
        assert "后端未作答" in ctx.notes

    async def test_每一步拦截都进事件账本(self):
        """D-A2：没有静默路径。InjectGuidance 也要落——凡是代码插手过的
        地方，账本上必须有一条。"""
        ctx = await _run(
            FakeAgentLLM([{"tool": "click", "ref": "e1"}]),
            _driver(entry=_LOGIN, after=(_LOGIN2,)),
            _player(True),
        )
        assert len(ctx.events) == 2
        assert all(e["stage"] == "agent" for e in ctx.events)
        assert [e["disposition"]["kind"] for e in ctx.events] == [
            "inject_guidance", "fail_closed"
        ]
        assert ctx.events[0]["verdict"] == "login_wall"

    async def test_收口后不再烧llm_call(self):
        ctx = await _run(FakeAgentLLM([], down=True), _driver(), _player(True))
        assert len(ctx.llm.calls) == 1


# ═══════════════════════════════════════════════════════════════════════
# 与 scripted 同形
# ═══════════════════════════════════════════════════════════════════════


def _scripted_ctx() -> tuple[object, RunLedger]:
    """跑一条等价的 scripted 链（首页 → 点播放控件 → 播放页 → 判可用）。"""
    perceptor = ScriptedPerceptor({
        Q.IS_REACHABLE: _decision(Q.IS_REACHABLE, None),
        Q.FIND_PLAY_CONTROL: _decision(
            Q.FIND_PLAY_CONTROL, True, payload={"ref": "e1"}
        ),
        Q.PLAYER_OK: _decision(Q.PLAYER_OK, True),
    })
    ledger = RunLedger(task_id="T1")
    driver = _driver(after=(_PLAY,))
    return (driver, perceptor, ledger)


async def _run_scripted() -> tuple[object, RunLedger, FakeDriver]:
    driver, perceptor, ledger = _scripted_ctx()
    result = await visit_site(driver, perceptor, ledger, _cand())
    return result, ledger, driver


async def _run_agent() -> tuple[object, RunLedger, FakeDriver]:
    driver = _driver(after=(_PLAY,))
    ledger = RunLedger(task_id="T1")
    result = await agent_visit(
        driver,
        _player(True),
        ledger,
        _cand(),
        _backend(FakeAgentLLM([{"tool": "click", "ref": "e1"}, _done()])),
        title="功夫",
    )
    return result, ledger, driver


class Test与scripted同形:
    async def test_两条链落的动作工具序列一致(self):
        sr, _, _ = await _run_scripted()
        ar, _, _ = await _run_agent()
        # 差别只有 new_tab 的位置（scripted 在点击前，agent 在进站后），
        # 工具集则完全相同——P1 的六值封闭集对两条链是同一个。
        assert sorted({s.action.tool for s in ar.steps.steps}) == sorted(
            {s.action.tool for s in sr.steps.steps}
        )
        assert set(s.action.tool for s in ar.steps.steps) <= set(TOOLS)

    async def test_两条链的visit落进P1是同一个形状(self):
        sr, sl, _ = await _run_scripted()
        ar, al, _ = await _run_agent()
        s_json = _visit_json(sr, sl)
        a_json = _visit_json(ar, al)
        assert set(s_json) == set(a_json)
        assert s_json["landed_url"] == a_json["landed_url"]
        assert s_json["success"] == a_json["success"] is True
        # 两条链的 steps 逐条同键：下游（P2/P3 重建）不需要知道是谁跑的。
        for s, a in zip(s_json["steps"], a_json["steps"]):
            assert set(s) == set(a)

    async def test_两步都有act与settle成对(self):
        """动作流的逐步形状：每条 step 都带 action / observation / error。
        P2 的六件套第 ④⑤ 件就是读它。"""
        _, _, _ = await _run_scripted()
        ar, _, _ = await _run_agent()
        for st in ar.steps.steps:
            j = st.to_json(lambda o: {"url": o.url} if o else None)
            assert {"action", "observation"} <= set(j)


def _visit_json(result, ledger: RunLedger) -> dict:
    """借 :class:`RunRecord` 的序列化落一次 P1——**只取 visit 那一段**。"""
    rec = RunRecord(
        task_id="T1", title="功夫", query="q", search_url="u",
        ledger=ledger, visits=[result], candidates=[result.candidate],
        executor_mode="agent",
    )
    return rec.to_json()["visits"][0]


class TestP1落盘:
    async def test_存档写下executor_mode(self):
        ar, al, _ = await _run_agent()
        rec = RunRecord(
            task_id="T1", title="功夫", query="q", search_url="u",
            ledger=al, visits=[ar], executor_mode="agent",
        )
        j = rec.to_json()
        assert j["executor_mode"] == "agent"
        assert j["visits"][0]["steps"]

    async def test_没有executor_mode的存档仍是scripted批次(self):
        """默认值必须是 scripted：**既有批次零改动**（加配置的第一纪律）。"""
        rec = RunRecord(task_id="T1", title="功夫", query="q", search_url="u")
        assert rec.to_json()["executor_mode"] == "scripted"

    async def test_切得出的样本动作与scripted同形(self, tmp_path):
        """端到端：agent 链落的 P1 能被 P2 切出六件套，
        且动作视图与 scripted 切出来的**逐字段同形**。

        这是 §3.9 端到端那一行的落点：A3 的 A/B 双跑要能只靠
        ``executor_mode`` 一个字段区分两批，前提是其余全部同形。
        """
        from trajectory_pipeline.assembler.schema import split_archive

        sr, sl, _ = await _run_scripted()
        ar, al, _ = await _run_agent()
        s_acts = _actions_of_archive(_full_archive(sr, sl, "scripted"),
                                     tmp_path / "s.json", split_archive)
        a_acts = _actions_of_archive(_full_archive(ar, al, "agent"),
                                     tmp_path / "a.json", split_archive)
        assert a_acts, "agent 链落的 P1 切不出样本——形状对不上契约"
        assert s_acts, "scripted 基线自己切不出样本——这条对比没意义"
        # 逐字段比：工具名与 origin 都要一样。**origin 尤其要一样**——
        # 「哪一步是执行器自己做的会话管理、哪一步是模型选的」是 P3
        # tool_calls 重建的输入，两条链给不同答案的话，A3 的 A/B
        # 就分不清差异来自模型还是来自记账。
        assert [(a["tool"], a["origin"]) for a in a_acts] == [
            (a["tool"], a["origin"]) for a in s_acts
        ]


def _full_archive(result, ledger: RunLedger, mode: str) -> dict:
    rec = RunRecord(
        task_id="T1", title="功夫", query="功夫 在线观看",
        search_url="https://search.example/?q=1",
        user_prompt="功夫在哪能看",
        ledger=ledger, visits=[result], candidates=[result.candidate],
        run_config={"max_candidates": 5}, executor_mode=mode,
    )
    return rec.to_json()


def _actions_of_archive(doc: dict, path, split_archive) -> list:
    """切一份 P1，取第一个有动作的单元的 P2 动作视图。

    六件套的形状顺手核一次：它与 P1 契约是两张表，
    ``test_真实存档切得出的样本形状与契约一致`` 盯着真存档，
    这里盯的是**新落盘的那一份**。
    """
    from trajectory_pipeline.assembler.builder import _sample_to_json

    path.write_text(json.dumps(doc, ensure_ascii=False), encoding="utf-8")
    for sample in split_archive(path):
        six = _sample_to_json(sample)["six"]
        assert set(six) == {
            "system_prompt", "tools", "user_prompt", "provenance",
            "actions", "observations", "rationales",
        }
        if six["actions"]:
            return list(six["actions"])
    return []
