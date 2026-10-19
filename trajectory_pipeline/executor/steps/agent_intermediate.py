"""B/C 中间页访问的 **agent 形态**——薄壳（方案 13 §4）。

与 :mod:`.aggregator` / :mod:`.article` 的分工：**进站段与外链校验段共用，
页面内的那一段换人**。

    visit_aggregator        ① ⑤ IS_AGGREGATOR ⑥ SELECT_SITE_ENTRIES 外链校验
    visit_aggregator_agent  ① ───────── [agent 任务③] ───────── 外链校验
    visit_article           ① ⑥C SELECT_REFERRAL_LINKS        外链校验
    visit_article_agent     ① ───────── [agent 任务④] ───────── 外链校验
                            └ 共用 ┘                            └ 共用 ┘

## 内化掉了什么

``IS_AGGREGATOR`` / ``SELECT_SITE_ENTRIES`` / ``SELECT_REFERRAL_LINKS``
三道判断题在 agent 模式下**一道都不跑**，它们的语义进任务指令。连带
的后果是三支负分支在 agent 模式下没有产地（见 :mod:`.agent_loop` 的
:data:`~.agent_loop._FALLCLOSED_BRANCH` 注释与 ``test_agent_intermediate``
的 ``Test分支收敛`` 一组）：

- ``not_aggregator``：它的前提是「判断题判了 False」这个**真结论**；
- ``aggregator_no_entries`` / ``article_no_links``：同上。

agent 说「没找到」一律落 ``unresolved``——与任务②的自报放弃同一条
口径（§3.3）：**agent 自报的不是负结论**。三支负分支在 A2b 期间由
scripted 独占产出，这与 A2a-3 记下的「``site_search_no_results`` 在
agent 模式下没有产地」是同一类收窄，已经连续第三次撞上，说明它是
agent 化的结构代价而不是某条链的疏漏。

## 本文件里没有的东西

- **没有感知层**：签名里根本没有 ``perceptor`` 的位置——三道题都不跑，
  收一个不用是更坏的信号（读代码的人会以为某处还在判）；
- **没有 link_check 的替代品**：自报链接一律过
  :func:`~.link_check.resolve_referral`，**这是刻意的**（§4「与 scripted
  link_check 并存期」）。跳转确认页、结构剔除、落地观测都是代码事实，
  模型猜不出来；
- **没有 fallback**：没找到就按报的形态记账，不在内部退回 scripted
  重跑一遍（§6.1：混合轨迹不可归因）。
"""

from __future__ import annotations

from typing import Any

from trajectory_pipeline.executor.agent_loop import (
    TASK_ENTRIES,
    TASK_REFERRAL,
    UNRESOLVED,
    AgentBackend,
    AgentTask,
    agent_loop,
)
from trajectory_pipeline.executor.branches import RunLedger
from trajectory_pipeline.executor.browser.page_driver import PageDriver
from trajectory_pipeline.executor.steps.aggregator import (
    ROLE_AGGREGATOR,
    ROLE_ARTICLE,
    IntermediateResult,
    probe_and_observe,
    resolve_into_candidates,
)
from trajectory_pipeline.executor.steps.search import (
    DEFAULT_ENGINE,
    Candidate,
)


async def _visit(
    driver: PageDriver,
    ledger: RunLedger,
    candidate: Candidate,
    backend: AgentBackend,
    *,
    kind: str,
    role: str,
    max_chars: int | None = None,
    max_links: int = 5,
    engine: str = "",
    stats: dict[str, int] | None = None,
    title: str = "",
    gatekeeper_events: list[dict[str, Any]] | None = None,
    llm_calls: list[dict[str, Any]] | None = None,
) -> IntermediateResult:
    """任务③④ 的共用体。**本函数不抛异常**（批处理纪律）。

    与两个 scripted 兄弟的差别只有中间那一段，两端共用：进站用
    :func:`~.aggregator.probe_and_observe`（``unreachable_hard`` 的口径
    必须与 scripted 一致），出链用
    :func:`~.aggregator.resolve_into_candidates`（外链校验与候选构造
    必须与 scripted 一致）。抽函数共用而不是各写一份的理由，与
    :mod:`.agent_visit` 那两段的理由同源。
    """
    result = IntermediateResult(role=role, candidate=candidate)
    obs = await probe_and_observe(
        driver, ledger, result, max_chars=max_chars
    )
    if obs is None:
        return result

    outcome = await agent_loop(
        driver,
        backend.client,
        backend.escalate,
        AgentTask(
            title=title,
            url=candidate.url,
            kind=kind,
            site_title=candidate.text,
            max_selected=max_links,
        ),
        result.steps,
        backend.budget,
        initial_obs=obs,
        json_of=backend.json_of,
        gatekeeper_events=gatekeeper_events,
        llm_calls=llm_calls,
        max_chars=max_chars,
    )
    result.note(
        f"agent 循环：{outcome.steps_taken} 步 / {outcome.llm_calls} 次调用"
    )

    if outcome.reached:
        # 自报的链接过**代码的外链校验**——agent 只决定「哪些链接值得
        # 引荐」，存不存在由校验说了算。一条都没活下来的话，
        # result.candidates 就是空的，与 scripted 的外链全 dropped 同形。
        #
        # ⚠️ parent 用**报出链接的那一页**而不是落地页：agent 在站内
        # 导航过，站外链接的 same_host 剔除要以「它当时站在哪」为准，
        # 否则它会把自己当前所在站的链接也报上来且不被剔除。
        parent = (
            outcome.final_obs.url
            if outcome.final_obs is not None and outcome.final_obs.url
            else (result.landed_url or candidate.url)
        )
        if parent != (result.landed_url or candidate.url):
            result.note(
                f"agent 在 {parent} 报出链接（进站落地 "
                f"{result.landed_url or candidate.url}）"
            )
        await resolve_into_candidates(
            driver, result,
            [(r.url, r.why) for r in outcome.selected],
            origin=role, engine=engine or DEFAULT_ENGINE,
            max_chars=max_chars, max_links=max_links, stats=stats,
            parent=parent,
        )
        return result

    # 放弃 / 被收口。**记哪一支是映射表的事**（§3.3）：本文件只负责
    # 把 outcome 交给 RunLedger，不把它翻成分支名。
    ledger.record(
        result.landed_url or candidate.url,
        outcome.branch or UNRESOLVED,
        outcome.reason,
        site_url=candidate.url,
    )
    result.note(f"agent 未找到：{outcome.kind}（{outcome.reason}）")
    return result


async def visit_aggregator_agent(
    driver: PageDriver,
    ledger: RunLedger,
    candidate: Candidate,
    backend: AgentBackend,
    *,
    max_chars: int | None = None,
    max_links: int = 5,
    engine: str = "",
    stats: dict[str, int] | None = None,
    title: str = "",
    gatekeeper_events: list[dict[str, Any]] | None = None,
    llm_calls: list[dict[str, Any]] | None = None,
) -> IntermediateResult:
    """路径 B 的中间页单元（agent 形态）：聚合站落地 → 自主找入口。

    Returns:
        :class:`~.aggregator.IntermediateResult`——与
        :func:`~.aggregator.visit_aggregator` **同一个类型**，P1 落盘
        对下游零差异（§4）。
    """
    return await _visit(
        driver, ledger, candidate, backend,
        kind=TASK_ENTRIES, role=ROLE_AGGREGATOR,
        max_chars=max_chars, max_links=max_links, engine=engine,
        stats=stats, title=title,
        gatekeeper_events=gatekeeper_events, llm_calls=llm_calls,
    )


async def visit_article_agent(
    driver: PageDriver,
    ledger: RunLedger,
    candidate: Candidate,
    backend: AgentBackend,
    *,
    max_chars: int | None = None,
    max_links: int = 5,
    engine: str = "",
    stats: dict[str, int] | None = None,
    title: str = "",
    gatekeeper_events: list[dict[str, Any]] | None = None,
    llm_calls: list[dict[str, Any]] | None = None,
) -> IntermediateResult:
    """路径 C 的中间页单元（agent 形态）：文章页落地 → 自主找引荐外链。

    与 :func:`visit_aggregator_agent` 只有 kind / role 两个取值不同——
    **刻意不合并成一个函数**：调用方（orchestrator 的分派表）要的是
    两个具名入口，合并之后分派表那边就得传 kind 进去，而 kind 的取值
    与 discovery_path 一一对应，那层对应关系放在本文件里比放在
    orchestrator 里更不容易错。
    """
    return await _visit(
        driver, ledger, candidate, backend,
        kind=TASK_REFERRAL, role=ROLE_ARTICLE,
        max_chars=max_chars, max_links=max_links, engine=engine,
        stats=stats, title=title,
        gatekeeper_events=gatekeeper_events, llm_calls=llm_calls,
    )


__all__ = ["visit_aggregator_agent", "visit_article_agent"]
