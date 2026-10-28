"""``executor/agent_loop`` 的离线单测（方案 12 A2a-2 / 方案 13 §3）。

**不碰真浏览器、不碰真 LLM。** 值得测的是三件事：

1. **契约**——wire format 的每条字段约束、I6 白名单。它们防的都是
   **不可逆**错误（点错页面、编造 ref），漏一条就是污染一批存档；
2. **纪律**——D-A2 之后没有静默路径：每条处置都要能在事件账本里找到，
   「被拦 → 引导 → 重试」这条链要能在对话里逐字看到。这一条最容易被
   改坏：把引导塞进 system 而不是 user，或者干脆不落事件，功能全绿
   但训练信号没了；
3. **收敛**——§3.3 映射表逐行 + 双向钉死。

动作脚本由 :class:`FakeAgentLLM` 显式给定（与 :class:`FakeLLM` 的
``classify`` 表同哲学）：**测试不靠碰巧的 URL 措辞**，断言写的是
「这条路径产出了什么轨迹」。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field

import pytest

from trajectory_pipeline.assembler.schema import TOOL_SPECS
from trajectory_pipeline.executor import actions
from trajectory_pipeline.executor.actions import StepLog
from trajectory_pipeline.executor.agent_loop import (
    AGENT_TOOLS,
    TASK_ENTRIES,
    TASK_KINDS,
    TASK_PLAY,
    TASK_REFERRAL,
    _FALLCLOSED_BRANCH,
    _LINK_TASKS,
    _NONTERMINATING_VERDICTS,
    _REPORT_FORMAT,
    _TASK_GOAL,
    _WIRE_FORMAT,
    _canonical,
    _invented_url,
    _system_prompt,
    _validate_selected,
    ActionInstruction,
    AgentBudget,
    AgentTask,
    SelectedRef,
    agent_loop,
    parse_instruction,
)
from trajectory_pipeline.executor.branches import BRANCH_LABELS
from trajectory_pipeline.executor.browser.page_driver import DriverError
from trajectory_pipeline.executor.gatekeeper import DeadLoopDetector, Verdict
from trajectory_pipeline.llm.schema_parse import extract_json, strip_think
from trajectory_pipeline.perception.base import (
    InteractiveElement,
    LinkItem,
    Observation,
)
from trajectory_pipeline.tests.fakes import FakeAgentLLM


# ═══════════════════════════════════════════════════════════════════════
# Fake 与工具
# ═══════════════════════════════════════════════════════════════════════


def _obs(
    url: str = "https://x.test/",
    title: str = "首页",
    body: str = "欢迎来到本站，这里有好多影视资源",
    refs: tuple[str, ...] = ("e1", "e2"),
    links: tuple[tuple[str, str], ...] = (),
    **kw: object,
) -> Observation:
    """构造一份观察。``refs`` 直接决定 I6 白名单的内容。"""
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


class FakeDriver:
    """观察队列式 driver。

    与 ``test_visit.py`` 里那个 ``URL → 页面`` 映射的 driver 不同：agent
    会走到测试预想不到的 URL 上（那正是它自主的部分），按 URL 登记会让
    每个测试都得先猜一遍路径。**按队列给页面**，测试只描述「它先后看到
    了什么」；队列空了就停在最后一页（粘住）——「什么都没变」这类形态
    正是要它粘住。
    """

    name = "fake-agent"

    def __init__(
        self,
        pages: tuple[Observation, ...] = (),
        *,
        sticky: Observation | None = None,
        goto_fails: tuple[str, ...] = (),
        click_fails: tuple[str, ...] = (),
        observe_fails: int = 0,
    ) -> None:
        self._queue = list(pages)
        #: 队列耗尽后停在这一页。**有默认值**：不给页面的 driver 也能跑，
        #: 而「没预设页面就抛异常」会让每个只想测决策逻辑的用例都先
        #: 去凑一份页面。
        self.current: Observation | None = sticky or _obs()
        self.visited: list[str] = []
        self.clicked: list[str] = []
        self.typed: list[tuple[str, str]] = []
        self.pressed: list[str] = []
        self.observe_calls = 0
        self._goto_fails = set(goto_fails)
        self._click_fails = set(click_fails)
        self._observe_fails = observe_fails

    async def goto(self, url: str, *, wait_until: str = "load") -> None:
        self.visited.append(url)
        if url in self._goto_fails:
            raise DriverError(f"fake: 导航失败 {url}")

    async def click(self, *, ref: str | None = None, selector: str | None = None) -> None:
        self.clicked.append(ref or selector or "")
        if (ref or "") in self._click_fails:
            raise DriverError(f"fake: 点不动 {ref}")

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

    async def observe(self, *, max_chars: int | None = None) -> Observation:
        self.observe_calls += 1
        if self._observe_fails > 0:
            self._observe_fails -= 1
            raise DriverError("fake: 取不到快照")
        if self._queue:
            self.current = self._queue.pop(0)
        return self.current

    async def close(self) -> None:
        pass


_LOGIN_PAGE = _obs(
    url="https://x.test/play",
    title="播放页",
    body="请登录后再观看本片，未登录用户只能试看六分钟",
    refs=("e1",),
)
_PAY_PAGE = _obs(
    url="https://x.test/play",
    title="播放页",
    body="本片为 VIP 专享内容，续费后可观看",
    refs=("e1",),
)
_CF_PAGE = _obs(url="https://x.test/", title="Just a Moment", body="")
_CAPTCHA_PAGE = _obs(url="https://x.test/", title="百度安全验证", body="")


def _json_of(raw: str) -> dict | None:
    """生产侧注入的解析器（agent_loop 不 import llm，见其模块 docstring）。

    测试与生产用**同一个**——换成裸 ``json.loads`` 会让下面的用例在
    「模型套了 fence / 带了 think」时全部通过，而生产里那条路径根本
    不是那样跑的。
    """
    return extract_json(strip_think(raw))


@dataclass
class _Ctx:
    """一次 ``agent_loop`` 的全部可观察产物。"""

    outcome: object
    steps: StepLog
    events: list[dict] = field(default_factory=list)
    calls: list[dict] = field(default_factory=list)
    driver: FakeDriver | None = None
    llm: FakeAgentLLM | None = None

    @property
    def actions(self) -> list[str]:
        return [s.action.tool for s in self.steps.steps]

    @property
    def kinds(self) -> list[str]:
        return [e["disposition"]["kind"] for e in self.events]

    @property
    def roles(self) -> list[str]:
        return [m["role"] for m in self.outcome.transcript]

    def text_of(self, needle: str) -> str:
        for m in self.outcome.transcript:
            if needle in m["content"]:
                return m["content"]
        raise AssertionError(f"对话里没有包含 {needle!r} 的消息")


async def _run(
    llm: FakeAgentLLM,
    driver: FakeDriver | None = None,
    *,
    initial: Observation | None = None,
    budget: AgentBudget | None = None,
    escalate: FakeAgentLLM | None = None,
    max_chars: int | None = None,
    dead_loop: object | None = None,
    task: AgentTask | None = None,
    steps: StepLog | None = None,
) -> _Ctx:
    steps = steps if steps is not None else StepLog()
    events: list[dict] = []
    calls: list[dict] = []
    drv = driver if driver is not None else FakeDriver()
    out = await agent_loop(
        drv,
        llm,
        escalate,
        task or AgentTask(title="功夫", url="https://x.test/"),
        steps,
        budget or AgentBudget(),
        initial_obs=initial or _obs(),
        json_of=_json_of,
        gatekeeper_events=events,
        llm_calls=calls,
        max_chars=max_chars,
        dead_loop=dead_loop,  # type: ignore[arg-type]
    )
    return _Ctx(out, steps, events, calls, drv, llm)


# ═══════════════════════════════════════════════════════════════════════
# 协议解析
# ═══════════════════════════════════════════════════════════════════════


class Test协议解析:
    """wire format 的形状校验——**只管形状，不管语义**（语义在 I6 那组）。"""

    @pytest.mark.parametrize("tool", ["goto", "click", "fill", "press"])
    def test_四工具都认(self, tool):
        got = parse_instruction({"tool": tool, "done": False, "params": {}})
        assert got is not None and got.tool == tool

    def test_缺_tool就不认(self):
        assert parse_instruction({"ref": "e1", "done": False}) is None

    def test_缺_done就不认(self):
        """契约里写了的字段必须在——悄悄替模型补上 ``done``，它就永远
        不知道自己漏了字段。"""
        assert parse_instruction({"tool": "click", "ref": "e1"}) is None

    def test_工具名越界在解析层放行(self):
        """越界的工具名**能解析出来**，交给 I6 那组去拒。

        这样分工的理由：解析层不知道工具清单（那是执行器的事），而
        拒绝时要给出「可用工具只有……」的**具体引导**——只有拿得到
        清单的那一层说得出这句话。
        """
        got = parse_instruction({"tool": "new_tab", "done": False})
        assert got is not None and got.tool == "new_tab"

    def test_done为真时report必填(self):
        assert parse_instruction({"tool": "click", "done": True}) is None

    def test_done为真时report必须带why(self):
        assert parse_instruction(
            {"done": True, "report": {"reached": True}}
        ) is None

    def test_done为真时reached必须是布尔(self):
        assert parse_instruction(
            {"done": True, "report": {"reached": "是", "why": "到了"}}
        ) is None

    def test_done为真时工具字段不校验(self):
        """那一轮不执行动作，``tool`` / ``ref`` / ``params`` 都是空壳
        ——拿它们卡住「报到了」是本末倒置。"""
        got = parse_instruction({"tool": "", "done": True,
                                 "report": {"reached": True, "why": "到了"}})
        assert got is not None and got.reached is True

    def test_done为假时多余的report忽略(self):
        """多了 report 不算违约：动作本身没有歧义，为一段多余文字
        废掉一步不划算（与「done=true 缺 report」的不对称，见
        ``parse_instruction`` 的 docstring）。"""
        got = parse_instruction(
            {"tool": "click", "done": False,
             "report": {"reached": False, "why": "还没到"}}
        )
        assert got is not None and got.done is False
        assert got.report is None  # 不把它带进对话

    def test_done非布尔不认(self):
        assert parse_instruction({"tool": "click", "done": "true"}) is None

    def test_空与非字典不认(self):
        assert parse_instruction(None) is None
        assert parse_instruction({}) is None
        assert parse_instruction([{"tool": "click"}]) is None

    def test_ref非标量按空串(self):
        """不 ``str()`` 一个 dict——``"{'a': 1}"`` 在 ref 字段上会被
        当成一次「看起来有目标」的点击。"""
        got = parse_instruction({"tool": "click", "done": False, "ref": {"a": 1}})
        assert got is not None and got.ref == ""

    def test_think原样保留(self):
        got = parse_instruction(
            {"tool": "click", "done": False, "think": "先点进去看看"}
        )
        assert got is not None and got.think == "先点进去看看"


# ═══════════════════════════════════════════════════════════════════════
# I6：ref 白名单（工具层强制）
# ═══════════════════════════════════════════════════════════════════════


class TestI6:
    """编 ref 是**不可逆**错误——点错了改不回来，所以根本不执行。"""

    async def test_合法ref放行(self):
        ctx = await _run(
            FakeAgentLLM([{"tool": "click", "ref": "e1"}]),
            FakeDriver(),
        )
        assert ctx.actions == ["click"]
        assert ctx.driver.clicked == ["e1"]

    async def test_编ref不执行(self):
        """被拒的那一步**不进动作流**：轨迹里没有「点了个不存在的东西」
        这一步，只有「模型想点 e99 但被挡住了」这个事实（在对话里）。"""
        ctx = await _run(
            FakeAgentLLM([], invent_ref=True),
            FakeDriver(),
        )
        assert ctx.actions == []
        assert ctx.driver.clicked == []

    async def test_编ref给引导而不是直接收口(self):
        ctx = await _run(
            FakeAgentLLM([], invent_ref=True),
            FakeDriver(),
        )
        ctx.text_of("ref='e99' 不在最近一次观察的 interactive 列表里")
        assert ctx.calls  # 白问的那次也算钱

    async def test_连续两次编ref收口为unresolved(self):
        """行为失败 ≠ 页面负样本：它不入负样本池。"""
        ctx = await _run(
            FakeAgentLLM([], invent_ref=True),
            FakeDriver(),
        )
        assert ctx.outcome.fail_closed
        assert ctx.outcome.branch == "unresolved"
        assert ctx.outcome.reason == "动作指令连续解析失败"

    async def test_click没有ref拒(self):
        ctx = await _run(
            FakeAgentLLM([{"tool": "click", "params": {}}]),
            FakeDriver(),
        )
        assert ctx.actions == []

    async def test_press没有ref放行(self):
        """回车提交是全局按键，不指向具体元素——press 是唯一
        ref 可空的工具。"""
        ctx = await _run(
            FakeAgentLLM([{"tool": "press", "params": {"key": "Enter"}}]),
            FakeDriver(),
        )
        assert ctx.actions == ["press"]
        assert ctx.driver.pressed == ["Enter"]

    async def test_press编了ref照样拒(self):
        """press 的 ref 可空，但**给了就必须是真的**——「可空」不是
        「给什么都可以」。"""
        ctx = await _run(
            FakeAgentLLM([{"tool": "press", "ref": "e99",
                           "params": {"key": "Enter"}}]),
            FakeDriver(),
        )
        assert ctx.actions == []

    async def test_goto缺url拒(self):
        ctx = await _run(
            FakeAgentLLM([{"tool": "goto", "params": {}}]),
            FakeDriver(),
        )
        assert ctx.actions == []
        assert ctx.driver.visited == []

    async def test_fill缺text拒(self):
        ctx = await _run(
            FakeAgentLLM([{"tool": "fill", "ref": "e1", "params": {}}]),
            FakeDriver(),
        )
        assert ctx.actions == []

    async def test_白名单取自渲染进对话的那一份(self):
        """模型没见过的 ref 不算合法引用——否则 I6 只能挡住最笨的那次
        编造（e999），挡不住「编一个在第 51 位、页面上确实存在但没
        给他看的 e51」。"""
        many = _obs(refs=tuple(f"e{n}" for n in range(1, 61)))
        ctx = await _run(
            FakeAgentLLM([{"tool": "click", "ref": "e51"}]),
            FakeDriver(),
            initial=many,
        )
        assert ctx.actions == []


class Test工具越界:
    async def test_new_tab被拒(self):
        """``new_tab`` 是执行器自己的会话管理动作，模型选它就是协议
        违反——这不是「先记下来再说」的场合，是不执行。"""
        ctx = await _run(
            FakeAgentLLM([{"tool": "new_tab", "params": {"url": "https://x.test/p"}}]),
            FakeDriver(),
        )
        assert ctx.actions == []
        ctx.text_of("可用工具只有 goto/click/fill/press")

    async def test_不存在的工具被拒(self):
        ctx = await _run(
            FakeAgentLLM([{"tool": "scroll"}]),
            FakeDriver(),
        )
        assert ctx.actions == []


# ═══════════════════════════════════════════════════════════════════════
# gatekeeper 接线
# ═══════════════════════════════════════════════════════════════════════


class TestGatekeeper接线:
    async def test_进站首观察就是CF页_一次都不问(self):
        """环境级阻断**不进对话**：问模型「怎么过 CF」没有意义，
        那次 llm_call 是纯浪费。"""
        ctx = await _run(
            FakeAgentLLM([]),
            FakeDriver(),
            initial=_CF_PAGE,
        )
        assert ctx.outcome.branch == "blocked"
        assert ctx.outcome.llm_calls == 0
        assert not ctx.llm.calls

    async def test_站内撞上CF_一步收口(self):
        ctx = await _run(
            FakeAgentLLM([{"tool": "click", "ref": "e1"}]),
            FakeDriver((_CF_PAGE,)),
        )
        assert ctx.outcome.branch == "blocked"
        assert ctx.outcome.verdict == Verdict.BLOCKED_CF.value
        assert ctx.actions == ["click"]

    async def test_验证码收口为blocked(self):
        ctx = await _run(
            FakeAgentLLM([{"tool": "click", "ref": "e1"}]),
            FakeDriver((_CAPTCHA_PAGE,)),
        )
        assert ctx.outcome.verdict == Verdict.CAPTCHA.value
        assert ctx.outcome.branch == "blocked"

    async def test_CF不给引导机会(self):
        """反爬/验证码是一次性收口：引导只会烧一次 llm_call 然后
        得到一句「我换不了」。"""
        ctx = await _run(
            FakeAgentLLM([{"tool": "click", "ref": "e1"}]),
            FakeDriver((_CF_PAGE,)),
        )
        assert ctx.kinds == ["fail_closed"]
        assert ctx.outcome.llm_calls == 1

    async def test_登录墙首次给引导_且对话里看得见(self):
        ctx = await _run(
            FakeAgentLLM([{"tool": "click", "ref": "e1"}]),
            FakeDriver((_LOGIN_PAGE,)),
        )
        assert ctx.kinds == ["inject_guidance"]
        assert ctx.events[0]["verdict"] == Verdict.LOGIN_WALL.value
        assert ctx.events[0]["stage"] == "agent"
        ctx.text_of("当前页面是登录墙")

    async def test_登录墙二次收口为login_required(self):
        ctx = await _run(
            FakeAgentLLM([{"tool": "click", "ref": "e1"},
                          {"tool": "click", "ref": "e1"}]),
            FakeDriver((_LOGIN_PAGE, _LOGIN_PAGE)),
        )
        assert ctx.outcome.branch == "login_required"
        assert ctx.outcome.verdict == Verdict.LOGIN_WALL.value
        assert ctx.kinds == ["inject_guidance", "fail_closed"]

    async def test_付费墙二次收口为subscription_wall(self):
        ctx = await _run(
            FakeAgentLLM([{"tool": "click", "ref": "e1"},
                          {"tool": "click", "ref": "e1"}]),
            FakeDriver((_PAY_PAGE, _PAY_PAGE)),
        )
        assert ctx.outcome.branch == "subscription_wall"

    async def test_死循环二次收口为unresolved(self):
        """死循环是**行为**失败，不是页面负样本——那不入负样本池。"""
        ctx = await _run(
            FakeAgentLLM([], never_done=True),
            FakeDriver((_obs(url="https://x.test/play", refs=("e1",)),)),
        )
        assert ctx.outcome.branch == "unresolved"
        assert ctx.outcome.verdict == Verdict.DEAD_LOOP.value
        assert ctx.kinds == ["inject_guidance", "fail_closed"]

    async def test_死循环引导里带具体原因(self):
        """告诉模型它卡在哪一种（重复动作 / 空正文），它才知道该换
        动作还是换目标。"""
        ctx = await _run(
            FakeAgentLLM([], never_done=True),
            FakeDriver((_obs(url="https://x.test/play", refs=("e1",)),)),
        )
        assert "repeat_action" in ctx.text_of("最近几步在原地打转")

    async def test_观察降级不拦_但降级事实进对话(self):
        """DEGRADED 是分类陈述不是处置：降级观察照进判定
        （fail-closed 由判定层自己收口），但**让模型自己看到**它拿到的
        是一份残骸。"""
        degraded = _obs(degraded=("interactive",))
        ctx = await _run(
            FakeAgentLLM([{"tool": "click", "ref": "e1"}]),
            FakeDriver((degraded,)),
        )
        assert ctx.events == []  # 没有处置，也就没有事件
        ctx.text_of('"degraded": ["interactive"]')


# ═══════════════════════════════════════════════════════════════════════
# 预算
# ═══════════════════════════════════════════════════════════════════════


class Test预算:
    async def test_步数耗尽收口为unresolved(self):
        """关掉死循环检测，专测步数闸——否则两条闸会互相抢着收口，
        测出来的不知道是哪一条。"""
        ctx = await _run(
            FakeAgentLLM([], never_done=True),
            FakeDriver(),
            budget=AgentBudget(max_steps=3, max_llm_calls=99),
            dead_loop=DeadLoopDetector(repeat_threshold=99, empty_body_threshold=99),
        )
        assert ctx.outcome.branch == "unresolved"
        assert ctx.outcome.reason == "步数预算耗尽"
        assert ctx.outcome.steps_taken == 3

    async def test_llm_calls耗尽收口为unresolved(self):
        ctx = await _run(
            FakeAgentLLM([], never_done=True),
            FakeDriver(),
            budget=AgentBudget(max_steps=99, max_llm_calls=2),
        )
        assert ctx.outcome.reason == "llm_calls 预算耗尽"
        assert ctx.outcome.llm_calls == 2

    async def test_预算闸在花钱之前(self):
        """步数用完就不再烧下一次 llm_call——先问再发现没步数了，
        那次调用的钱花得毫无意义。"""
        ctx = await _run(
            FakeAgentLLM([], never_done=True),
            FakeDriver(),
            budget=AgentBudget(max_steps=2, max_llm_calls=99),
            dead_loop=DeadLoopDetector(repeat_threshold=99, empty_body_threshold=99),
        )
        assert ctx.outcome.llm_calls == 2


# ═══════════════════════════════════════════════════════════════════════
# 分支收敛映射（§3.3）
# ═══════════════════════════════════════════════════════════════════════


class Test分支收敛:
    async def test_报到了不落分支(self):
        """成功与否是 PLAYER_OK 判断题的事（D-A5），不是 agent 的事。"""
        ctx = await _run(
            FakeAgentLLM([{"tool": "click", "ref": "e1"}]),
            FakeDriver((_obs(url="https://x.test/play", refs=("e1",)),)),
        )
        assert ctx.outcome.reached is True
        assert ctx.outcome.branch == ""

    async def test_自报放弃落unresolved(self):
        """agent 说不清的，不进负样本池：站里真没有与模型没找到
        在数据上完全同形，合并成一支负样本就是把「没找到」记成
        「这里看不了」。"""
        ctx = await _run(
            FakeAgentLLM([{"done": True,
                           "report": {"reached": False, "why": "翻了两页没有正片"}}]),
            FakeDriver(),
        )
        assert ctx.outcome.branch == "unresolved"
        assert ctx.outcome.reason == "翻了两页没有正片"
        assert ctx.outcome.llm_calls == 1

    async def test_放弃但没说原因_照实记成没说(self):
        """``why`` 是契约必填字段（缺了整条报告作废），但**可以是空串**
        ——那时不能有一步看起来像「放弃了但不知道为什么」却什么都不记。"""
        ctx = await _run(
            FakeAgentLLM([{"done": True, "report": {"reached": False, "why": ""}}]),
            FakeDriver(),
        )
        assert ctx.outcome.branch == "unresolved"
        assert "未给出放弃原因" in ctx.outcome.reason

    @pytest.mark.parametrize(
        "verdict,branch",
        [
            (Verdict.LOGIN_WALL, "login_required"),
            (Verdict.PAYWALL, "subscription_wall"),
            (Verdict.BLOCKED_CF, "blocked"),
            (Verdict.CAPTCHA, "blocked"),
            (Verdict.DEAD_LOOP, "unresolved"),
        ],
    )
    def test_映射表逐行(self, verdict, branch):
        assert _FALLCLOSED_BRANCH[verdict] == branch

    def test_映射表与豁免集恰好覆盖Verdict七值(self):
        """给 Verdict 加值的人必须来这里登记它该怎么收口——否则
        agent 模式会出现「拦下来了却不知道算哪一支」的静默分支。"""
        covered = set(_FALLCLOSED_BRANCH) | set(_NONTERMINATING_VERDICTS)
        assert covered == set(Verdict)

    def test_映射值全在22分支封闭集内(self):
        assert set(_FALLCLOSED_BRANCH.values()) <= set(BRANCH_LABELS)

    def test_非拦截类收口落unresolved(self):
        """预算 / I6 / 格式 / 动作执行失败：共同点是「代码能力不足」，
        不是页面属性——它们一律不入负样本池。"""
        from trajectory_pipeline.executor.agent_loop import _branch_of

        assert _branch_of(None) == "unresolved"


# ═══════════════════════════════════════════════════════════════════════
# 终局形态收口（Q-25 甲 · owner 裁定 2026-10-28）
# ═══════════════════════════════════════════════════════════════════════
#
# 口径：收口前对**最后一页正文**跑一次感知层现成判据
# （``infer_access_defect``）——命中就落那条分支，不命中仍落 ``unresolved``。
#
# ⚠️ **只用页面自己的正文**，不用 agent 的叙述（自述是模型输出，不是页面
# 证据）。两道护栏（前置事实 / 归因对象）都在
# :func:`~trajectory_pipeline.executor.branches.terminal_shape_branch` 里。

#: nginx 默认 404 页——实测里最常见的一种（正文 19 字、零元素）。
_404页 = _obs(
    url="https://x.test/v/31445",
    title="404 Not Found",
    body="404 Not Found nginx",
    refs=(),
)

#: **正常页里提到「已下架」**——正文命中终局形态，但这一页**有内容**
#: ⇒ 前置事实不成立 ⇒ 不配记 ``broken``。
_提到下架的正常页 = _obs(
    url="https://x.test/v/31445",
    title="影片详情",
    body="该剧已下架，请浏览其它片源",
    refs=("e1",),
    elements_total=12,
)

#: 最后一页落在**别的站**上——归因对象不明 ⇒ 不配记 ``broken``。
_外站的404页 = _obs(
    url="https://other.test/v/31445",
    title="404 Not Found",
    body="404 Not Found nginx",
    refs=(),
)


class Test终局形态收口:
    async def test_放弃在404页上落broken(self):
        """⚠️ **页面自己说清楚了** ⇒ 那就不是「agent 说不清」。

        agent 放弃的常见形态是「这一页就是 404」——证据在**页面**上，
        不在 agent 的自述里 ⇒ 落 ``broken``（进池）。
        """
        ctx = await _run(
            FakeAgentLLM([
                {"tool": "click", "ref": "e1"},
                {"done": True,
                 "report": {"reached": False, "why": "点进去是空的"}},
            ]),
            FakeDriver((_404页,)),
        )
        assert ctx.outcome.branch == "broken"
        assert "404 Not Found" in ctx.outcome.reason
        assert "点进去是空的" in ctx.outcome.reason

    async def test_正常页提到下架不落broken(self):
        """⚠️ **假阳代价**：正文命中词表 ≠ 这一页是错误页。

        「已下架」在正常详情页里会出现（角标、公告）⇒ 有 12 个可交互元素
        的那一页**不是**错误页 ⇒ 降回 ``unresolved``，并把「为什么不成立」
        写进 reason（查过了、没过，得看得见）。
        """
        ctx = await _run(
            FakeAgentLLM([
                {"tool": "click", "ref": "e1"},
                {"done": True,
                 "report": {"reached": False, "why": "没有正片"}},
            ]),
            FakeDriver((_提到下架的正常页,)),
        )
        assert ctx.outcome.branch == "unresolved"
        assert "前置事实不成立" in ctx.outcome.reason

    async def test_外站的404页不落broken(self):
        """⚠️ **归因对象**：agent 能 ``goto`` 任意 URL ⇒ 收口时它看着的
        那一页**可能压根不是候选站**。那一页是 404 不等于候选站是 404。
        """
        ctx = await _run(
            FakeAgentLLM([
                {"tool": "click", "ref": "e1"},
                {"done": True,
                 "report": {"reached": False, "why": "找不到"}},
            ]),
            FakeDriver((_外站的404页,)),
        )
        assert ctx.outcome.branch == "unresolved"
        assert "归因对象不明" in ctx.outcome.reason

    async def test_步数预算耗尽也查终局形态(self):
        """预算耗尽意味着 agent **真的把这一页看过了**——最后一页就是
        它放弃的那一页，形态是**页面自己说的** ⇒ 落 ``broken``。"""
        ctx = await _run(
            FakeAgentLLM([], never_done=True),
            FakeDriver((_404页,)),
            budget=AgentBudget(max_steps=2, max_llm_calls=99),
            dead_loop=DeadLoopDetector(repeat_threshold=99, empty_body_threshold=99),
        )
        assert ctx.outcome.branch == "broken"
        assert "步数预算耗尽" in ctx.outcome.reason

    async def test_llm_calls预算耗尽也查终局形态(self):
        ctx = await _run(
            FakeAgentLLM([], never_done=True),
            FakeDriver((_404页,)),
            budget=AgentBudget(max_steps=99, max_llm_calls=2),
            dead_loop=DeadLoopDetector(repeat_threshold=99, empty_body_threshold=99),
        )
        assert ctx.outcome.branch == "broken"

    async def test_后端未作答不查终局形态(self):
        """⚠️ **「我们自己的故障」不动**：后端挂了是**运维问题**，
        那一批该重跑（M-46）——把它算成页面负样本会让池里混进
        「我们坏了」的样本，也会盖掉「该重跑」这个信号。"""
        ctx = await _run(
            FakeAgentLLM(down=True),
            FakeDriver(),
            initial=_404页,
        )
        assert ctx.outcome.branch == "unresolved"
        assert "后端未作答" in ctx.outcome.reason

    async def test_动作解析失败不查终局形态(self):
        """同上：格式类失败是**模型能力**问题，不是页面属性。"""
        ctx = await _run(
            FakeAgentLLM([], bad_json=True),
            FakeDriver(),
            initial=_404页,
        )
        assert ctx.outcome.branch == "unresolved"

    async def test_普通页放弃仍是unresolved(self):
        """**没命中终局形态** ⇒ 照旧 ``unresolved``（回归护栏）。"""
        ctx = await _run(
            FakeAgentLLM([{"done": True,
                           "report": {"reached": False, "why": "翻了两页没有正片"}}]),
            FakeDriver(),
        )
        assert ctx.outcome.branch == "unresolved"
        assert ctx.outcome.reason == "翻了两页没有正片"

    async def test_地区限制页落blocked(self):
        """地区 / 验证码 / 反爬形态 ⇒ 落 ``blocked``（**采集侧账**，不进池）。

        ⚠️ 与 ``broken`` 的处置不同：``blocked`` 是「**这次没看到**」，
        一次拦截对「这站能不能看」零信息（方案 20 §1）。
        """
        地区页 = _obs(
            url="https://x.test/v/31445",
            title="提示",
            body="当前地区不支持播放该内容",
            refs=(),
        )
        ctx = await _run(
            FakeAgentLLM([
                {"tool": "click", "ref": "e1"},
                {"done": True,
                 "report": {"reached": False, "why": "看不了"}},
            ]),
            FakeDriver((地区页,)),
        )
        assert ctx.outcome.branch == "blocked"


# ═══════════════════════════════════════════════════════════════════════
# 升配（§3.4）
# ═══════════════════════════════════════════════════════════════════════


class Test升配:
    async def test_连续两次格式失败切到升配后端(self):
        esc = FakeAgentLLM([], model="escalate-7b")
        ctx = await _run(
            FakeAgentLLM([], flaky=2),
            FakeDriver(),
            escalate=esc,
        )
        assert ctx.outcome.reached is True
        assert len(ctx.llm.calls) == 2      # 主后端问了两次（都吐散文）
        assert len(esc.calls) == 1          # 升配后端接管第三次
        assert [c["model"] for c in ctx.calls] == [
            "fake-agent", "fake-agent", "escalate-7b",
        ]

    async def test_升配事件带后端名(self):
        esc = FakeAgentLLM([], model="escalate-7b")
        ctx = await _run(FakeAgentLLM([], flaky=2), FakeDriver(), escalate=esc)
        ev = [e for e in ctx.events if e["disposition"]["kind"] == "escalate_model"]
        assert len(ev) == 1
        assert ev[0]["disposition"]["backend"] == "escalate-7b"

    async def test_未配升配时只落事件不切后端(self):
        """**升配是能力不是依赖**：没配不该 fail，只是记一条「当时
        想过要升配」。"""
        ctx = await _run(FakeAgentLLM([], flaky=2), FakeDriver())
        ev = [e for e in ctx.events if e["disposition"]["kind"] == "escalate_model"]
        assert len(ev) == 1
        assert ev[0]["disposition"]["backend"] == ""
        assert ctx.outcome.branch == "unresolved"

    async def test_升配每个候选只发生一次(self):
        """升完还啃不动，就说明不是模型不够强的问题——再升一次
        只是把成本翻倍。"""
        esc = FakeAgentLLM([], invent_ref=True, model="escalate-7b")
        ctx = await _run(
            FakeAgentLLM([], invent_ref=True),
            FakeDriver(),
            escalate=esc,
        )
        ev = [e for e in ctx.events if e["disposition"]["kind"] == "escalate_model"]
        assert len(ev) == 1
        assert ctx.outcome.branch == "unresolved"


# ═══════════════════════════════════════════════════════════════════════
# 账本（§3.6）
# ═══════════════════════════════════════════════════════════════════════


class Test账本:
    async def test_字段齐全(self):
        ctx = await _run(
            FakeAgentLLM([{"tool": "click", "ref": "e1"}]),
            FakeDriver((_obs(url="https://x.test/play", refs=("e1",)),)),
        )
        entry = ctx.calls[0]
        assert entry["caller"] == "agent_loop"
        assert entry["candidate"] == "https://x.test/"
        assert entry["model"] == "fake-agent"
        assert entry["prompt_tokens"] == 128
        assert entry["completion_tokens"] == 32
        assert entry["error"] == ""
        assert "ts" not in entry  # 循环体不引入时间源，见 _record

    async def test_后端没给usage就记None不补0(self):
        """写 0 会让批次成本被那几个 0 拉低——报表上「便宜」是假的。"""
        ctx = await _run(
            FakeAgentLLM([{"tool": "click", "ref": "e1"}], no_usage=True),
            FakeDriver((_obs(url="https://x.test/play", refs=("e1",)),)),
        )
        assert ctx.calls[0]["prompt_tokens"] is None
        assert ctx.calls[0]["completion_tokens"] is None

    async def test_没作答记账时带错误码(self):
        ctx = await _run(FakeAgentLLM([], down=True), FakeDriver())
        assert ctx.calls[0]["error"] == "transport:ConnectError"
        assert ctx.calls[0]["prompt_tokens"] is None

    async def test_没作答直接收口且带错误码(self):
        """``content is None`` 是「后端没答」，不是「模型说找不到」，
        也不是「答得不合契约」——三种事实三种处置：

        - 后端没答 → 收口（client 内部已重试过，再问一遍结果一样）；
        - 答得不合契约 → 引导重试；
        - 说找不到 → ``give_up`` → unresolved。

        混成一条路的代价：后端挂掉时白烧一轮 llm_call 再升一次配。
        """
        ctx = await _run(FakeAgentLLM([], down=True), FakeDriver())
        assert ctx.outcome.fail_closed  # 收口，不是 give_up
        assert ctx.outcome.branch == "unresolved"
        assert "transport:ConnectError" in ctx.outcome.reason
        assert ctx.outcome.llm_calls == 1  # 不重试

    async def test_没作答不触发升配(self):
        """升配是为「模型啃不动」准备的，不是为「连不上」准备的——
        更强的模型也连不上同一个挂掉的端点。"""
        esc = FakeAgentLLM([], model="escalate-7b")
        ctx = await _run(FakeAgentLLM([], down=True), FakeDriver(), escalate=esc)
        assert ctx.events == []  # 连事件都不该有
        assert esc.calls == []


# ═══════════════════════════════════════════════════════════════════════
# 动作流与 P1 形态
# ═══════════════════════════════════════════════════════════════════════


class Test动作流:
    async def test_站内搜索链三步都进动作流(self):
        """fill + press 是 agent 化相对 scripted 固定序列的增量收益：
        scripted 的站内搜索是专用代码路径，agent 把它变成涌现行为。"""
        ctx = await _run(
            FakeAgentLLM([
                {"tool": "fill", "ref": "e1", "params": {"text": "功夫"}},
                {"tool": "press", "params": {"key": "Enter"}},
                {"tool": "click", "ref": "e2"},
            ]),
            FakeDriver((_obs(url="https://x.test/s", refs=("e2",)),)),
        )
        assert ctx.actions == ["fill", "press", "click"]
        assert ctx.driver.typed == [("e1", "功夫")]
        assert ctx.driver.pressed == ["Enter"]

    async def test_动作流是P1形态(self):
        ctx = await _run(
            FakeAgentLLM([{"tool": "click", "ref": "e1"}]),
            FakeDriver((_obs(url="https://x.test/play", refs=("e1",)),)),
        )
        got = ctx.steps.steps[0].to_json(lambda o: {"url": o.url} if o else None)
        assert got["action"]["tool"] == "click"
        assert got["action"]["origin"] == "model"
        assert got["action"]["params"] == {}
        # ref **不在**动作流里（会话内句柄，换个 session 就失效）
        assert "ref" not in json.dumps(got["action"])
        assert got["action"]["target"] == {"tag": "a", "label": "入口e1"}
        assert got["observation"] == {"url": "https://x.test/play"}
        assert got["error"] == ""

    async def test_每步都带观察(self):
        ctx = await _run(
            FakeAgentLLM([{"tool": "click", "ref": "e1"},
                          {"tool": "click", "ref": "e1"}]),
            FakeDriver((_obs(url="https://x.test/a", refs=("e1",)),
                       _obs(url="https://x.test/b", refs=("e1",)))),
        )
        assert [s.observation.url for s in ctx.steps.steps] == [
            "https://x.test/a", "https://x.test/b",
        ]

    async def test_动作执行失败不收口_落error且继续(self):
        """「点了但点不动」正是负样本最该留的证据，且**要让 agent 看着
        失败原因自己换路**——那才是完整的行为链。"""
        ctx = await _run(
            FakeAgentLLM([{"tool": "click", "ref": "e1"},
                          {"tool": "click", "ref": "e1"}]),
            FakeDriver(click_fails=("e1",)),
        )
        assert ctx.outcome.reached is True
        assert ctx.steps.steps[0].error.startswith("click 失败")
        assert ctx.steps.steps[0].observation is None
        assert "请换一个动作" in ctx.text_of("执行失败")

    async def test_快照取不到不收口_落error且继续(self):
        """快照取不到是**能力问题**（I4 fail-closed），但不整站收口：
        页面还在，agent 可以换个动作继续。"""
        ctx = await _run(
            FakeAgentLLM([{"tool": "click", "ref": "e1"}]),
            FakeDriver(observe_fails=1),
        )
        assert ctx.outcome.reached is True
        assert ctx.steps.steps[0].error.startswith("动作后快照失败")

    async def test_动作流不碰进站那一步(self):
        """进站 goto 是调用方的活——循环只往调用方给的动作流里**追加**
        自己走的那几步，不覆盖、也不替它落进站那一步。"""
        steps = StepLog()
        steps.act(actions.goto("https://x.test/"))
        ctx = await _run(
            FakeAgentLLM([{"tool": "click", "ref": "e1"}]),
            FakeDriver((_obs(url="https://x.test/play", refs=("e1",)),)),
            steps=steps,
        )
        # 循环只**追加**了自己走的那一步，没有动调用方已经落好的进站 goto
        assert [s.action.tool for s in steps.steps] == ["goto", "click"]
        assert ctx.actions == ["goto", "click"]  # ctx.steps 就是调用方那一条


# ═══════════════════════════════════════════════════════════════════════
# 训练信号：被拦 → 引导 → 重试（D-A2）
# ═══════════════════════════════════════════════════════════════════════


class Test训练信号:
    async def test_坏输出后纠正重试的完整链在对话里(self):
        """D-A2 删除 L1 静默自愈的本意：**被拦 → 纠正 → 重试**这条链
        本身就是要教给模型的正确行为。抹掉它等于把轨迹伪造成
        「从未出错」。"""
        ctx = await _run(
            FakeAgentLLM([{"tool": "click", "ref": "e1"}], flaky=1),
            FakeDriver((_obs(url="https://x.test/play", refs=("e1",)),)),
        )
        texts = [m["content"] for m in ctx.outcome.transcript]
        assert ctx.outcome.reached is True
        guide = next(i for i, t in enumerate(texts)
                     if "不是合法的动作指令 JSON" in t)
        act = next(i for i, t in enumerate(texts) if '"tool": "click"' in t)
        assert guide < act, "引导必须在被纠正的那一步之前"

    async def test_模型原文不进对话(self):
        """进对话的是**重新序列化**的规范 JSON，原文（含 raw CoT）没有
        路径流进 P1/P2——不靠调用方记得先剥。"""
        ctx = await _run(
            FakeAgentLLM([{"tool": "click", "ref": "e1"}], flaky=1),
            FakeDriver((_obs(url="https://x.test/play", refs=("e1",)),)),
        )
        for m in ctx.outcome.transcript:
            assert "我想先在页面上找找看" not in m["content"]

    async def test_规范输出按契约重排(self):
        ctx = await _run(
            FakeAgentLLM([{"tool": "click", "ref": "e1"}]),
            FakeDriver((_obs(url="https://x.test/play", refs=("e1",)),)),
        )
        got = json.loads(ctx.outcome.transcript[-1]["content"])
        assert list(got) == ["think", "tool", "ref", "params", "done", "report"]

    async def test_对话以system和任务上下文开头(self):
        ctx = await _run(
            FakeAgentLLM([{"tool": "click", "ref": "e1"}]),
            FakeDriver((_obs(url="https://x.test/play", refs=("e1",)),)),
        )
        assert ctx.roles[:2] == ["system", "user"]
        assert "功夫" in ctx.outcome.transcript[0]["content"]
        assert "https://x.test/" in ctx.outcome.transcript[1]["content"]


# ═══════════════════════════════════════════════════════════════════════
# 契约漂移
# ═══════════════════════════════════════════════════════════════════════


class Test任务类型:
    """A2b：三类任务共用一个循环，差异只有「终局的产出形态」。

    这一组测的是**协议层**（什么形态算合法），不测链接最终活没活下来
    （那是 ``test_agent_intermediate`` 的外链校验那一段）。
    """

    def test_构造期拒认未知kind(self):
        with pytest.raises(ValueError) as ei:
            AgentTask(title="功夫", url="https://x.test/", kind="play2")
        assert "play2" in str(ei.value)

    def test_三kind的终局契约各自成文(self):
        """三类各有一份终局契约原文，**且契约与提示词同源**——
        改一处改两处必然分叉，而分叉时模型看到的是旧契约。"""
        for kind in TASK_KINDS:
            assert kind in _REPORT_FORMAT
            assert kind in _TASK_GOAL
        # 链接类的契约里必须出现 selected，任务② 的契约里不能出现
        for kind in _LINK_TASKS:
            assert "selected" in _REPORT_FORMAT[kind]
        assert "selected" not in _REPORT_FORMAT[TASK_PLAY]

    def test_提示词里带着终局契约原文(self):
        for kind in TASK_KINDS:
            prompt = _system_prompt(
                AgentTask(title="功夫", url="https://x.test/", kind=kind),
                AgentBudget(),
            )
            assert _REPORT_FORMAT[kind] in prompt
            assert _WIRE_FORMAT in prompt
            assert _TASK_GOAL[kind].format(title="功夫") in prompt

    def test_链接类的提示词多一条selected纪律(self):
        def rules(kind: str) -> str:
            return _system_prompt(
                AgentTask(title="功夫", url="https://x.test/", kind=kind),
                AgentBudget(),
            ).split("纪律：\n")[1]

        assert "selected" in rules(TASK_ENTRIES)
        assert "selected" not in rules(TASK_PLAY)
        # 多了一条，编号也要跟着走——不然模型看到两个「3.」
        assert "4." in rules(TASK_ENTRIES)
        assert "4." not in rules(TASK_PLAY)

    def test_max_selected进提示词(self):
        prompt = _system_prompt(
            AgentTask(title="功夫", url="https://x.test/",
                      kind=TASK_ENTRIES, max_selected=2),
            AgentBudget(),
        )
        assert "最多 2 条" in prompt

    def test_链接类报了selected才算合法(self):
        got = parse_instruction(
            {"done": True, "report": {"reached": True, "why": "找到了",
                                      "selected": [{"url": "https://v.test/"}]}},
            TASK_ENTRIES,
        )
        assert got is not None
        assert got.selected == (SelectedRef(url="https://v.test/"),)

    def test_链接类报了却没selected不合法(self):
        assert parse_instruction(
            {"done": True, "report": {"reached": True, "why": "到了"}},
            TASK_ENTRIES,
        ) is None

    def test_play任务报了selected也忽略(self):
        """终局是一个位置，多报一串链接不影响「到了没到」——
        为一段多余内容废掉整步不划算（与「done=false 多了 report」
        那条不对称同款）。"""
        got = parse_instruction(
            {"done": True, "report": {"reached": True, "why": "到了",
                                      "selected": [{"url": "https://v.test/"}]}},
            TASK_PLAY,
        )
        assert got is not None and got.selected == ()

    def test_放弃形态不要求selected(self):
        got = parse_instruction(
            {"done": True, "report": {"reached": False, "why": "没找到"}},
            TASK_ENTRIES,
        )
        assert got is not None and got.selected == ()

    def test_selected里的非字典整条作废(self):
        assert parse_instruction(
            {"done": True, "report": {"reached": True, "why": "x",
                                      "selected": ["https://v.test/"]}},
            TASK_ENTRIES,
        ) is None

    def test_selected的why是审计字段可以没有(self):
        """``why`` 是纯审计字段（落 P1 的 text、进 rationale 的素材），
        **不是**可执行部分——缺了它不该整条作废。"""
        got = parse_instruction(
            {"done": True, "report": {"reached": True, "why": "x",
                                      "selected": [{"url": "https://v.test/"}]}},
            TASK_REFERRAL,
        )
        assert got is not None and got.selected[0].why == ""

    def test_规范化输出按契约重排selected(self):
        """raw CoT 的那道闸：模型原文**从不**进对话，进的是重新
        序列化后的规范 JSON。"""
        got = parse_instruction(
            {"done": True, "report": {"reached": True, "why": "找到了",
                                       "selected": [{"url": "https://v.test/",
                                                     "why": "x",
                                                     "note": "一整段理由"}]}},
            TASK_ENTRIES,
        )
        assert got is not None
        text = _canonical(got)
        assert "一整段理由" not in text
        assert json.loads(text)["report"]["selected"] == [
            {"url": "https://v.test/", "why": "x"}
        ]

    def test_白名单只认轨迹里见过的URL(self):
        seen = {"https://v1.test/", "https://v2.test/"}
        assert _invented_url(
            (SelectedRef("https://v1.test/"), SelectedRef("https://v2.test/")),
            seen,
        ) == ""
        assert _invented_url(
            (SelectedRef("https://v1.test/"), SelectedRef("https://made.up/")),
            seen,
        ) == "https://made.up/"

    def test_白名单的引导文案带具体URL(self):
        text = _validate_selected(
            (SelectedRef("https://made.up/"),), frozenset()
        )
        assert "https://made.up/" in text
        # 与 I6 的 ref 引导同款：说清「从哪儿取」，而不是只说「不行」
        assert "观察里出现过的链接" in text
        assert "如实报告放弃" in text


class Test契约漂移:
    """§3.9 最后一行：agent_loop 实际发出的 tool 集必须与训练动作
    空间的模型可见集**一致**。

    ⚠️ **2026-10-20 起这条口径从「相等」改成「包含」**（增量六十二）。

    旧口径是「**同一个集合**」：模型可见集 == agent 实际能发的 4 个。
    增量六十二给训练侧的 `tools[]` 补了 7 条**能力声明**（模型被告知
    存在，但本项目没有任何真实运行产生过它们的调用）——于是模型可见集
    变成 11 条，而采集期的 agent 仍然只能发 4 个。

    这不是漂移，是**两个不同的集合**，各有各的职责：

    - :data:`AGENT_TOOLS` —— **执行端**的白名单。采集期的模型发出
      白名单外的动作，是**协议违反**（不是「先记下来」），要打回。
    - 模型可见集 —— **训练侧**的声明。样本的 `tools[]` 告诉模型
      「这个工具空间长这样」。

    所以关系是 **包含**而不是相等：agent 能发的**每一个**动作都必须在
    模型可见集里（否则模型被告知的工具它发不出），反过来不要求——
    多出来的那几条是能力声明，采集期会按 ``_GUIDANCE_BAD_TOOL`` 打回。

    ⚠️ 代价是明说的：训练出的模型**可能**去调那 7 条能力声明。若部署
    时给它的工具空间就是这 11 条，它调 `web_search` 不会被 v2 的
    `agent_loop` 接受。这是「能力声明」这个设计的已知代价，靠
    ``meta.tools_unused`` 逐个列名让人工复核看得见。
    """

    def test_agent能发的动作都在模型可见集里(self):
        visible = frozenset(
            spec.name for spec in TOOL_SPECS if spec.visible_to_model()
        )
        assert set(AGENT_TOOLS) <= set(visible), (
            "agent 能发的动作必须都声明给模型——否则模型被告知了一个"
            "它发不出的工具"
        )

    def test_能力声明多出来的那些在动作流词汇表外(self):
        """能力声明**不是**动作空间的一部分：控制流真会发的原语清单
        是 :data:`actions.TOOLS`，能力声明那 7 条不在里面。

        这条守住「声明不冒充能力」——多列一个进 ``actions.TOOLS``，
        就会训出一批永远不会被真实运行验证过的动作。
        """
        visible = {
            spec.name for spec in TOOL_SPECS if spec.visible_to_model()
        }
        declared_only = visible - set(AGENT_TOOLS)
        assert declared_only, "当前口径：模型可见集应当大于 agent 的四工具"
        assert not (declared_only & set(actions.TOOLS)), (
            f"能力声明混进了动作流词汇表: {declared_only & set(actions.TOOLS)}"
        )

    def test_四工具都在动作流词汇表内(self):
        """A2 不引入任何 P1 未见过的新动作——控制流真会发的原语清单
        是 TOOLS，agent 只是它的子集。"""
        assert set(AGENT_TOOLS) <= set(actions.TOOLS)

    async def test_实际发出的动作都落在四工具内(self):
        ctx = await _run(
            FakeAgentLLM([
                {"tool": "fill", "ref": "e1", "params": {"text": "功夫"}},
                {"tool": "press", "params": {"key": "Enter"}},
                {"tool": "click", "ref": "e2"},
            ]),
            FakeDriver((_obs(url="https://x.test/s", refs=("e2",)),)),
        )
        assert set(ctx.actions) <= set(AGENT_TOOLS)
