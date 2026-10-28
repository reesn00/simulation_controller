"""``executor/steps/agent_intermediate`` 的离线单测（方案 13 §4，A2b）。

测的是**薄壳本身**，不是 :func:`agent_loop`（那 74 例在
``test_agent_loop.py``）。分工与 ``test_agent_visit.py`` 同源：

1. **进站段与外链校验段是共用的，不是复制**——若哪天两条链对
   「进站失败算什么」给出不同答案，这里要红；
2. **终局自报必须过代码的外链校验**——agent 只决定「哪些链接值得
   引荐」，存不存在由 ``link_check`` 说了算（§4「与 scripted link_check
   并存期」）；
3. **三支负分支在 agent 模式下没有产地**——这是 A2b 最重要的一条，
   也是第三次撞上同一类收窄（前两次：A2a-3 的 ``site_search_no_results``
   与 A2a-2 的任务②）。它不该被悄悄抹平，要显式钉住。
"""

from __future__ import annotations

import inspect
from dataclasses import dataclass, field
from typing import Any

import pytest

from trajectory_pipeline.executor.agent_loop import (
    TASK_ENTRIES,
    TASK_KINDS,
    TASK_PLAY,
    TASK_REFERRAL,
    AgentBackend,
    AgentBudget,
    AgentTask,
)
from trajectory_pipeline.executor.branches import BRANCH_LABELS, RunLedger
from trajectory_pipeline.executor.browser.page_driver import (
    DriverError,
    DriverUnreachableError,
)
from trajectory_pipeline.executor.steps.agent_intermediate import (
    visit_aggregator_agent,
    visit_article_agent,
)
from trajectory_pipeline.executor.steps.aggregator import (
    ROLE_AGGREGATOR,
    IntermediateResult,
)
from trajectory_pipeline.executor.steps.article import ROLE_ARTICLE, visit_article
from trajectory_pipeline.executor.steps.search import Candidate
from trajectory_pipeline.llm.schema_parse import extract_json, strip_think
from trajectory_pipeline.perception.base import (
    InteractiveElement,
    LinkItem,
    Observation,
)
from trajectory_pipeline.tests.fakes import FakeAgentLLM

AGG_URL = "https://agg.test/"
POST_URL = "https://post.test/p/1"

#: 聚合站页上指向两个正式影视站的入口。**二元组是 ``(text, href)``**——
#: 与 :class:`~trajectory_pipeline.perception.base.LinkItem` 的字段顺序
#: 一致，反过来写会让「链接文本是 URL、URL 是文本」，而那种错在
#: 断言里看起来只是顺序不同。
_ENTRY_LINKS = (
    ("V1 站", "https://v1.test/"),
    ("V2 站", "https://v2.test/"),
)
#: 指向聚合站自己的站内分类页——外链校验会按 ``same_host`` 剔掉它，
#: 用来造「报了但一条都没活下来」。
_SELF_LINKS = (("分类", "https://agg.test/cate/1"),)


# ═══════════════════════════════════════════════════════════════════════
# Fake 与工具
# ═══════════════════════════════════════════════════════════════════════


def _obs(
    url: str,
    title: str = "页面",
    body: str = "正文",
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


#: B 路径的进站页：聚合站落地页，页面上挂着两条正式站入口。
_AGG = _obs(
    AGG_URL, title="聚合站", body="全网影视聚合导航，收录各大视频站",
    links=_ENTRY_LINKS,
)
#: C 路径的进站页：文章页，正文里挂着两条引荐外链。
_POST = _obs(
    POST_URL, title="影评", body="这部片子在 V1 和 V2 都能看",
    links=_ENTRY_LINKS,
)
#: 只指向自己的聚合站（「报了但活不下来」的靶子）。
_AGG_SELF = _obs(AGG_URL, title="聚合站", body="聚合导航", links=_SELF_LINKS)
#: 外链校验真实访问到的两个站（供 new_tab → observe）。
_V1 = _obs("https://v1.test/", title="V1", body="V1 影视站首页")
_V2 = _obs("https://v2.test/", title="V2", body="V2 影视站首页")
#: 站内分类页（same_host → dropped）。
_AGG_CATE = _obs("https://agg.test/cate/1", title="分类", body="聚合分类页")
#: agent 导航到分类页后**在那一页**发现的入口：看出来它确实是在
#: 分类页报的，而不是在落地页报的（parent 的靶子）。
_AGG_CATE_ENTRIES = _obs(
    "https://agg.test/cate/1", title="分类", body="分类下的影视站",
    links=_ENTRY_LINKS,
)
_LOGIN = _obs(
    "https://agg.test/login", title="登录",
    body="请登录后再观看，登录后即可浏览全部内容",
)
_LOGIN2 = _obs(
    "https://agg.test/login2", title="登录",
    body="请先登录，登录后才能继续访问本站资源",
)


class FakeDriver:
    """``URL → 页面`` 的驱动（进站与外链校验都要按 URL 取页）。

    与 ``test_agent_visit.FakeDriver`` 的差别只有一处：**tab 也按 URL
    取页**。外链校验会真开一个 tab 并观察它落地成什么，按队列给页面
    会让「这条外链落地在哪」变成测试说了算而不是 URL 说了算。
    """

    name = "fake-intermediate"

    def __init__(
        self,
        pages: dict[str, Observation] | None = None,
        *,
        after: tuple[Observation, ...] = (),
        goto_fails: tuple[str, ...] = (),
        goto_unreachable: tuple[str, ...] = (),
        tab_fails: tuple[str, ...] = (),
        observe_fails: int = 0,
    ) -> None:
        self._pages = dict(pages or {})
        self._queue = list(after)
        self._goto_fails = set(goto_fails)
        #: 导航失败但**机器码证明**站点不可达（M-44）⇒ 记
        #: ``unreachable_hard``（真负样本），与「这次没打开」分开。
        self._goto_unreachable = set(goto_unreachable)
        self._tab_fails = set(tab_fails)
        self._observe_fails = observe_fails
        self.current: Observation | None = None
        self.visited: list[str] = []
        self.tabs: list[str] = []
        self.closed: list[str] = []
        self.clicked: list[str] = []

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
        pass

    async def press(self, key: str, *, role: str | None = None, name: str | None = None) -> None:
        self._advance()

    def _advance(self) -> None:
        if self._queue:
            self.current = self._queue.pop(0)

    async def observe(self, *, max_chars: int | None = None) -> Observation:
        if self._observe_fails > 0:
            self._observe_fails -= 1
            raise DriverError("fake: 取不到快照")
        if self.current is None:
            raise DriverError("fake: 没有当前页")
        return self.current

    async def new_tab(self, url: str | None = None) -> str:
        self.tabs.append(url or "")
        if url in self._tab_fails or url not in self._pages:
            raise DriverError(f"fake: 开 tab 失败 {url}")
        self.current = self._pages[url]
        return f"tab{len(self.tabs)}"

    async def close_tab(self, tab_id: str | None = None) -> None:
        self.closed.append(tab_id or "")

    async def close(self) -> None: ...


def _driver(
    entry: Observation,
    *,
    after: tuple[Observation, ...] = (),
    **kw: object,
) -> FakeDriver:
    """进站页登记在候选 URL 上，外链目标页一并登记。"""
    pages = {
        AGG_URL: entry,
        POST_URL: entry,
        "https://v1.test/": _V1,
        "https://v2.test/": _V2,
        "https://agg.test/cate/1": _AGG_CATE,
    }
    return FakeDriver(pages, after=after, **kw)  # type: ignore[arg-type]


def _cand(url: str = AGG_URL, text: str = "聚合站") -> Candidate:
    return Candidate(url=url, text=text, rank=1)


class _FailingDriver:
    """什么都访问不通的 driver（只用来取 scripted 链的**返回类型**）。"""

    name = "failing"

    async def goto(self, url: str, *, wait_until: str = "load") -> None:
        raise DriverError("fake: 不通")

    async def observe(self, *, max_chars: int | None = None) -> Observation:
        raise DriverError("fake: 不通")


class _NoopPerceptor:
    """不答题的感知层（用途同上：只为凑齐 scripted 链的签名）。"""

    name = "noop"

    def decide(self, question: str, obs: Observation) -> Any:
        raise AssertionError(f"本题不该调用感知层：{question}")


@dataclass
class _Ctx:
    """一次中间页访问的全部可观察产物。"""

    result: object
    ledger: RunLedger
    events: list[dict] = field(default_factory=list)
    driver: FakeDriver | None = None
    llm: FakeAgentLLM | None = None

    @property
    def actions(self) -> list[str]:
        return [s.action.tool for s in self.result.steps.steps]

    @property
    def candidates(self) -> list[Candidate]:
        return list(self.result.candidates)

    @property
    def branches(self) -> list[str]:
        return [o.branch or "" for o in self.ledger.outcomes]

    @property
    def notes(self) -> str:
        return " | ".join(self.result.notes or [])

    @property
    def urls(self) -> list[str]:
        return [c.url for c in self.result.candidates]


def _backend(
    llm: FakeAgentLLM, budget: AgentBudget | None = None
) -> AgentBackend:
    return AgentBackend(
        client=llm,
        json_of=lambda raw: extract_json(strip_think(raw)),
        budget=budget or AgentBudget(),
    )


async def _run_agg(
    llm: FakeAgentLLM,
    entry: Observation = _AGG,
    *,
    max_links: int = 5,
    budget: AgentBudget | None = None,
    after: tuple[Observation, ...] = (),
    **kw: object,
) -> _Ctx:
    ledger = RunLedger(task_id="T1")
    events: list[dict] = []
    driver = _driver(entry, after=after, **kw)  # type: ignore[arg-type]
    ir = await visit_aggregator_agent(
        driver,
        ledger,
        _cand(AGG_URL),
        _backend(llm, budget),
        max_links=max_links,
        title="功夫",
        gatekeeper_events=events,
    )
    return _Ctx(result=ir, ledger=ledger, events=events,
                driver=driver, llm=llm)


async def _run_post(
    llm: FakeAgentLLM,
    entry: Observation = _POST,
    *,
    max_links: int = 5,
    budget: AgentBudget | None = None,
    after: tuple[Observation, ...] = (),
) -> _Ctx:
    ledger = RunLedger(task_id="T1")
    events: list[dict] = []
    driver = _driver(entry, after=after)
    ir = await visit_article_agent(
        driver,
        ledger,
        _cand(POST_URL, "文章"),
        _backend(llm, budget),
        max_links=max_links,
        title="功夫",
        gatekeeper_events=events,
    )
    return _Ctx(result=ir, ledger=ledger, events=events,
                driver=driver, llm=llm)


# ═══════════════════════════════════════════════════════════════════════
# 进站段（与 scripted 共用）
# ═══════════════════════════════════════════════════════════════════════


class Test进站:
    async def test_进站导航失败记unresolved(self):
        """⚠️ **「这次没打开」不是「站点不可达」**（M-44 裁定，owner
        2026-10-27）。

        超时 / 打断 / 建页失败这些形态**证明不了**站点不可达 ⇒ 记
        ``unresolved``（**不进负样本池**）：池里那条断言是
        「**这里真的看不了**」，而事实是「**这一次没打开**」⇒
        记成前者就是**训练信号被污染**（教「这个站看不了」）。
        """
        ctx = await _run_agg(FakeAgentLLM(kind=TASK_ENTRIES),
                             goto_fails=(AGG_URL,))
        assert ctx.branches == ["unresolved"]
        assert ctx.candidates == []
        # 动作流里那一步的 error 是非空的——「试过且失败」要看得见
        assert ctx.result.steps.steps[0].action.tool == "goto"
        assert ctx.result.steps.steps[0].error

    async def test_进站真站点不可达记unreachable_hard(self):
        """**机器码证明**站点不可达（DNS 解析不出来 / 连接被拒）
        ⇒ 仍是 ``unreachable_hard``（真负样本要留住，不许被降级）。"""
        ctx = await _run_agg(FakeAgentLLM(kind=TASK_ENTRIES),
                             goto_unreachable=(AGG_URL,))
        assert ctx.branches == ["unreachable_hard"]
        assert ctx.candidates == []

    async def test_进站快照失败也记unresolved(self):
        """导航成功但取不到快照，与打不开是**同一种**记账：
        两者都是「中间页没拿到手」，而不是「这个聚合站不行」。

        ⚠️ 分支从 ``unreachable_hard`` 改成 ``unresolved``（M-44）：
        导航**已经成功** ⇒ 站点**是通的** ⇒ 快照失败**证明不了**
        站点不可达 ⇒ 不该进负样本池。
        """
        ctx = await _run_agg(
            FakeAgentLLM(kind=TASK_ENTRIES), observe_fails=1
        )
        assert ctx.branches == ["unresolved"]
        assert ctx.candidates == []

    async def test_进站失败就不问模型(self):
        """页面都没拿到，问了也是白烧一次 llm_call。"""
        llm = FakeAgentLLM(kind=TASK_ENTRIES)
        ctx = await _run_agg(llm, goto_fails=(AGG_URL,))
        assert llm.calls == []

    async def test_进站的goto与scripted同形(self):
        """进站 goto 的 origin **沿用 ``actions.goto`` 的默认值**
        （model），与 scripted 链的中间页一字不差。

        ⚠️ A2b 不改它：中间页的进站导航在 P1 上长什么样，是 scripted
        批次已经落了盘的既成事实，改它等于改存量存档的语义。真要改
        （它确实是代码做的、模型没选），那是独立的一刀，且要连
        ``visit_site`` 的 ① 一起改——两处口径必须同一天变。
        """
        ctx = await _run_agg(FakeAgentLLM(kind=TASK_ENTRIES))
        assert ctx.result.steps.steps[0].action.tool == "goto"
        assert ctx.result.steps.steps[0].action.origin == "model"


# ═══════════════════════════════════════════════════════════════════════
# 终局产出（自报 → 外链校验 → 候选）
# ═══════════════════════════════════════════════════════════════════════


class Test终局产出:
    async def test_报出的链接变成候选(self):
        ctx = await _run_agg(FakeAgentLLM(kind=TASK_ENTRIES))
        # 空脚本 → fake 默认报出它看见的那两条链接
        assert ctx.urls == ["https://v1.test/", "https://v2.test/"]

    async def test_候选带origin与parent(self):
        """候选要能回溯到「哪个中间页引荐的它」——批次出问题时
        这是唯一的回溯路径。"""
        ctx = await _run_agg(FakeAgentLLM(kind=TASK_ENTRIES))
        for c in ctx.candidates:
            assert c.origin == ROLE_AGGREGATOR
            assert c.parent == AGG_URL
        assert ctx.result.role == ROLE_AGGREGATOR

    async def test_候选的source_href是自报的那条(self):
        ctx = await _run_agg(FakeAgentLLM(kind=TASK_ENTRIES))
        assert [c.source_href for c in ctx.candidates] == [
            "https://v1.test/", "https://v2.test/",
        ]

    async def test_外链校验真的开过tab(self):
        """自报不等于候选：``link_check`` 要真去访问一遍。
        这是 agent 链与 scripted 链**共用**的那一段（§4 并存期）。"""
        ctx = await _run_agg(FakeAgentLLM(kind=TASK_ENTRIES))
        assert ctx.driver is not None
        assert ctx.driver.tabs == ["https://v1.test/", "https://v2.test/"]
        assert "new_tab" in ctx.actions and "close_tab" in ctx.actions

    async def test_外链校验的两个动作都是infrastructure(self):
        """``new_tab`` / ``close_tab`` 是**代码**的会话管理动作，
        模型不该选它们——P2 的动作视图里不该出现这两个工具名。"""
        ctx = await _run_agg(FakeAgentLLM(kind=TASK_ENTRIES))
        infra = [
            s.action.tool for s in ctx.result.steps.steps
            if s.action.origin == "infrastructure"
        ]
        assert infra == ["new_tab", "close_tab", "new_tab", "close_tab"]

    async def test_报了但一条都活不下来就不产候选(self):
        """自报了，但外链校验把所有条都剔了（same_host）。
        候选为空是正常终态，**不落分支**——没有证据说这个站不行。"""
        ctx = await _run_agg(FakeAgentLLM(kind=TASK_ENTRIES), _AGG_SELF)
        assert ctx.candidates == []
        assert ctx.branches == []
        assert all(r.status == "dropped" for r in ctx.result.referrals)

    async def test_开tab失败的那条被dropped而不是记账(self):
        ctx = await _run_agg(
            FakeAgentLLM(kind=TASK_ENTRIES),
            tab_fails=("https://v1.test/",),
        )
        assert ctx.urls == ["https://v2.test/"]
        # 掉了一条只进 stats，不是负结论
        assert ctx.branches == []

    async def test_超出上限的截断发生在外链校验之前(self):
        """多报的链接**不会**被真实访问——每条外链校验都要开一个 tab，
        那是真金白银的往返，截断必须发生在花这笔钱之前。"""
        ctx = await _run_agg(FakeAgentLLM(kind=TASK_ENTRIES), max_links=1)
        assert ctx.urls == ["https://v1.test/"]
        assert ctx.driver is not None
        assert ctx.driver.tabs == ["https://v1.test/"]
        # 截断的痕迹要留在 P1 上，否则读档的人不知道还有一条没看
        assert "截断" in ctx.notes

    async def test_C路径同样产出候选(self):
        ctx = await _run_post(FakeAgentLLM(kind=TASK_REFERRAL))
        assert ctx.urls == ["https://v1.test/", "https://v2.test/"]
        for c in ctx.candidates:
            assert c.origin == ROLE_ARTICLE
            assert c.parent == POST_URL
        assert ctx.result.role == ROLE_ARTICLE

    async def test_站内导航过的parent是报出链接的那一页(self):
        """agent 走到分类页上才报的链接，parent 要记那一页——
        ``same_host`` 的剔除基准是「当时站在哪」，用错基准会把自己
        所在站的链接也当成引荐放进来。

        顺带钉住：候选用它回溯得到的是**发现链接的那一页**，而
        ``intermediates[].landed_url`` 仍是进站落地页，两者不一致
        是刻意的（单元身份 vs 候选产地）。
        """
        llm = FakeAgentLLM(
            [{"tool": "click", "ref": "e1"}], kind=TASK_ENTRIES
        )
        ctx = await _run_agg(llm, after=(_AGG_CATE_ENTRIES,))
        assert ctx.urls == ["https://v1.test/", "https://v2.test/"]
        for c in ctx.candidates:
            assert c.parent == "https://agg.test/cate/1"
        # 单元身份还是进站那一页，没被导航改掉
        assert ctx.result.landed_url == AGG_URL
        assert "报出链接" in ctx.notes

    async def test_落地的重定向写进候选(self):
        """外链落地被重定向时，候选记的是**落地后的** URL——
        遍历时访问的是落地页，存储与遍历必须对得上。
        （这条路径的判定在 ``link_check`` 里，与 scripted 共用；这里
        只钉住它确实被 agent 链走到了。）"""
        driver = _driver(_AGG)
        driver._pages["https://v1.test/"] = _obs(
            "https://v1.test/home", title="V1", body="V1 首页"
        )
        ir = await visit_aggregator_agent(
            driver,
            RunLedger(task_id="T1"),
            _cand(AGG_URL),
            _backend(FakeAgentLLM(kind=TASK_ENTRIES)),
            title="功夫",
            gatekeeper_events=[],
        )
        assert "https://v1.test/home" in [c.url for c in ir.candidates]
        assert "https://v1.test/" not in [c.url for c in ir.candidates]


# ═══════════════════════════════════════════════════════════════════════
# 终局自报的白名单（A2b 新增的一档处置）
# ═══════════════════════════════════════════════════════════════════════


class Test自报白名单:
    async def test_编造的URL进不了候选(self):
        ctx = await _run_agg(
            FakeAgentLLM(kind=TASK_ENTRIES, invent_url=True)
        )
        # 编造的那条被白名单挡下，循环最终收口（重试也编）
        assert "https://invented.test/nope" not in ctx.urls

    async def test_挡下来也要记账(self):
        """**挡下来是处置，记账是事实**——A3 的批次级幻觉计数读的
        就是这条事件。循环内挡住了不等于没发生过。"""
        ctx = await _run_agg(
            FakeAgentLLM(kind=TASK_ENTRIES, invent_url=True)
        )
        kinds = [e["disposition"]["kind"] for e in ctx.events]
        assert "reject_report" in kinds
        url = next(
            e["disposition"]["url"] for e in ctx.events
            if e["disposition"]["kind"] == "reject_report"
        )
        assert url == "https://invented.test/nope"

    async def test_引导文案进了对话(self):
        """反驳的理由要进对话——D-A2 删掉 L1 静默自愈之后，
        「被拦 → 纠正」这条链本身就是训练信号，不能抹。"""
        llm = FakeAgentLLM(kind=TASK_ENTRIES, invent_url=True)
        await _run_agg(llm)
        joined = " ".join(
            m["content"] for c in llm.calls for m in c
            if m["role"] == "user"
        )
        assert "不在你观察过的 links 里" in joined

    async def test_显式脚本里编造的URL也被挡(self):
        """默认报告走的是 fake 的便利路径，显式脚本是另一条路——
        两条路都要挡，否则「测试用的形态」与「真实形态」会分叉。"""
        llm = FakeAgentLLM(
            [{
                "done": True,
                "report": {
                    "reached": True,
                    "why": "找到了",
                    "selected": [{"url": "https://made.up/x",
                                  "why": "我记得有"}],
                },
            }],
            kind=TASK_ENTRIES,
            invent_url=True,
        )
        ctx = await _run_agg(llm)
        assert ctx.candidates == []
        kinds = [e["disposition"]["kind"] for e in ctx.events]
        assert "reject_report" in kinds
        # 挡下来之后还会重试（重试也编 → 最终收口），所以事件不止一条；
        # 但**每条反驳都要有 URL 原文**——A3 的幻觉计数按 URL 归因
        for e in ctx.events:
            if e["disposition"]["kind"] == "reject_report":
                assert e["disposition"]["url"]
                assert e["reason"]

    async def test_编造URL最终收口为unresolved(self):
        """它**不是**负样本：模型编了一条 URL，说不出「这个聚合站
        没有入口」，只说得清「我们没拿到候选」。"""
        ctx = await _run_agg(
            FakeAgentLLM(kind=TASK_ENTRIES, invent_url=True)
        )
        assert ctx.branches == ["unresolved"]


# ═══════════════════════════════════════════════════════════════════════
# 分支收敛（§3.3）
# ═══════════════════════════════════════════════════════════════════════


class Test分支收敛:
    async def test_自报放弃落unresolved(self):
        llm = FakeAgentLLM(
            [{"done": True, "report": {"reached": False,
                                       "why": "这个聚合站没有入口"}}],
            kind=TASK_ENTRIES,
        )
        ctx = await _run_agg(llm)
        assert ctx.branches == ["unresolved"]
        assert "没有入口" in ctx.notes

    async def test_步数耗尽落unresolved(self):
        ctx = await _run_agg(
            FakeAgentLLM([{"tool": "click", "ref": "e1"}],
                         kind=TASK_ENTRIES),
            budget=AgentBudget(max_steps=1),
            after=(_AGG,),
        )
        assert ctx.branches == ["unresolved"]
        assert "步数预算耗尽" in ctx.notes

    async def test_登录墙二次拦截落login_required(self):
        """拦截是**代码判的确定性证据**，所以可以记负样本——
        与自报放弃一律 unresolved 是同一条纪律的两端。"""
        llm = FakeAgentLLM(
            [{"tool": "click", "ref": "e1"}], kind=TASK_ENTRIES
        )
        ctx = await _run_agg(llm, _LOGIN, after=(_LOGIN2,))
        assert ctx.branches == ["login_required"]
        assert ctx.candidates == []

    async def test_后端没作答落unresolved(self):
        ctx = await _run_agg(
            FakeAgentLLM(kind=TASK_ENTRIES, down=True)
        )
        assert ctx.branches == ["unresolved"]

    @pytest.mark.parametrize("branch", [
        "not_aggregator", "aggregator_no_entries", "article_no_links",
    ])
    async def test_三支负分支在agent模式下没有产地(self, branch):
        """A2b 最重要的一条断言。

        这三支的成立前提是「判断题判了 False」这个**真结论**，而
        agent 模式下 ``IS_AGGREGATOR`` / ``SELECT_SITE_ENTRIES`` /
        ``SELECT_REFERRAL_LINKS`` 一道都不跑。agent 说「没找到」一律
        是 ``unresolved``——把自报翻成负标签，就是把「模型没找到」
        写成「这个聚合站没有入口」。

        三支本身还在封闭集里（scripted 仍产它们），所以这里断言的是
        **不产**，不是「名字没了」。
        """
        assert branch in BRANCH_LABELS  # 名字没被删掉
        scenarios = [
            FakeAgentLLM(
                [{"done": True, "report": {"reached": False, "why": "没有"}}],
                kind=TASK_ENTRIES,
            ),
            FakeAgentLLM(kind=TASK_ENTRIES, invent_url=True),
            FakeAgentLLM(kind=TASK_REFERRAL, down=True),
        ]
        for llm in scenarios:
            ctx = await _run_agg(llm)
            assert branch not in ctx.branches


# ═══════════════════════════════════════════════════════════════════════
# 形状：与 scripted 同型
# ═══════════════════════════════════════════════════════════════════════


class Test与scripted同形:
    async def test_产出的是IntermediateResult(self):
        """与 scripted 链**同一个类型**：P1 ``intermediates[]`` 的元素
        类型不因执行器而变，A3 的 A/B 才能只多一个 ``executor_mode``
        字段就成立。

        右侧那条 scripted 调用故意用「什么都访问不通」的驱动：本题只
        要它的**返回类型**，让它真跑起来就得为它配一整套感知层答桉，
        那是 ``test_visit`` 那批测试的活。
        """
        ctx = await _run_agg(FakeAgentLLM(kind=TASK_ENTRIES))
        assert isinstance(ctx.result, IntermediateResult)
        scripted = await visit_article(
            _FailingDriver(), _NoopPerceptor(), RunLedger(task_id="T1"),
            _cand(POST_URL),
        )
        assert isinstance(scripted, IntermediateResult)

    async def test_两条链的角色取值相同(self):
        ctx_b = await _run_agg(FakeAgentLLM(kind=TASK_ENTRIES))
        ctx_c = await _run_post(FakeAgentLLM(kind=TASK_REFERRAL))
        assert ctx_b.result.role == ROLE_AGGREGATOR
        assert ctx_c.result.role == ROLE_ARTICLE
        # role 的两个取值与 scripted 链同源（同一组常量）
        assert {ROLE_AGGREGATOR, ROLE_ARTICLE} == {"aggregator", "article"}

    def test_签名里没有perceptor(self):
        """三道判断题一道都不跑，所以**根本不收** perceptor。

        收一个不用是更坏的信号：读代码的人会以为某处还在判，而那个
        「某处」并不存在。
        """
        for fn in (visit_aggregator_agent, visit_article_agent):
            params = inspect.signature(fn).parameters
            assert "perceptor" not in params
            assert "backend" in params

    def test_两个入口的kind与role一一对应(self):
        """kind 与 role 的对应关系是**结构**上的（B→entries/aggregator，
        C→referral/article），不是参数凑出来的。"""
        assert (TASK_ENTRIES, ROLE_AGGREGATOR) == ("entries", "aggregator")
        assert (TASK_REFERRAL, ROLE_ARTICLE) == ("referral", "article")
        assert {TASK_ENTRIES, TASK_REFERRAL} < set(TASK_KINDS)


# ═══════════════════════════════════════════════════════════════════════
# 任务类型的构造期校验
# ═══════════════════════════════════════════════════════════════════════


class Test任务类型:
    def test_未知kind在构造期报错(self):
        with pytest.raises(ValueError) as ei:
            AgentTask(title="功夫", url=AGG_URL, kind="entries2")
        assert "entries2" in str(ei.value)
        assert "play" in str(ei.value)

    def test_默认kind是play(self):
        assert AgentTask(title="功夫", url=AGG_URL).kind == TASK_PLAY

    def test_三个kind都在封闭集里(self):
        assert set(TASK_KINDS) == {TASK_PLAY, TASK_ENTRIES, TASK_REFERRAL}

    async def test_报到达却交不出链接是契约违反(self):
        """链接类任务的「到了」必须交出链接——交不出就是整步作废。

        这条与 :func:`parse_instruction` 里「``done=true`` 时 report
        必填」是同一条纪律的延伸：报「到了」却不交 selected，代码无从
        判断它到了哪儿，与它说「没到」是两种事实，不能合并。

        ⚠️ 脚本里那条坏报告要**放两遍**：循环会给一次纠正机会，只放
        一遍的话第二轮 fake 就按默认报告（报出真链接）通过了，测到的
        就成了「纠正后能继续」而不是「违约要收口」。
        """
        bad = {"done": True, "report": {"reached": True, "why": "到了"}}
        ctx = await _run_agg(
            FakeAgentLLM([bad, dict(bad)], kind=TASK_ENTRIES)
        )
        assert ctx.candidates == []
        assert ctx.branches == ["unresolved"]

    async def test_交了空列表也作废(self):
        """空列表与缺失是**同一种**违约：都不说清它到了哪儿。
        分开处理会让「报了个空数组」悄悄通过。"""
        bad = {
            "done": True,
            "report": {"reached": True, "why": "到了", "selected": []},
        }
        ctx = await _run_agg(
            FakeAgentLLM([bad, dict(bad)], kind=TASK_ENTRIES)
        )
        assert ctx.branches == ["unresolved"]

    async def test_链接缺url的那条整条作废(self):
        """缺 url 的自报等于没报——不猜它想说哪一条。
        只有 why 的一行在 P2 上是一条没有目标的动作。"""
        bad = {
            "done": True,
            "report": {
                "reached": True,
                "why": "找到了",
                "selected": [{"url": "https://v1.test/", "why": "V1"},
                             {"why": "还有一个"}],
            },
        }
        ctx = await _run_agg(
            FakeAgentLLM([bad, dict(bad)], kind=TASK_ENTRIES)
        )
        assert ctx.candidates == []
        assert ctx.branches == ["unresolved"]


