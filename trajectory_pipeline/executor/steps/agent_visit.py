"""逐站点遍历的 **agent 形态**——薄壳（方案 13 §3.7）。

与 :mod:`.visit` 的分工：**进站段与 PLAYER_OK 段共用，站内的那一段换人**。

    visit_site     ① ② ③ ③.5 ④ ⑤ ⑥ ⑦       七段固定序列
    agent_visit    ① ② ────────── [agent] ⑦   站内交给循环

共享的两段是 :func:`~.visit.enter_site`（① ②）与
:func:`~.visit.judge_player`（⑦）——**抽函数共用，不复制**。理由见
:mod:`.visit` 文件头：``unreachable_hard`` 的口径与
``component_unverified`` 的前置事实守卫，两条链必须给同一个答案。

## 本文件里没有的东西

- **没有页面语义**：「哪个是播放控件」「这页能不能看」全部在
  :func:`~trajectory_pipeline.executor.agent_loop.agent_loop` 与
  :class:`~trajectory_pipeline.perception.base.Perceptor` 里；
- **没有分支判断**：出来的分支是 :class:`AgentOutcome` 查映射表查出来的
  （§3.3），本文件只负责把它交给 :class:`RunLedger`——**agent 永不直接
  产分支**，本文件也不产；
- **没有 fallback**：agent 没走到，就按它报的形态记账。不在内部退回
  scripted 重跑一遍（§6.1：混合轨迹不可归因）。批次级 fallback 由 A/B
  双跑本身承担。
"""

from __future__ import annotations

from typing import Any

from trajectory_pipeline.executor.agent_loop import (
    UNRESOLVED,
    AgentBackend,
    AgentTask,
    agent_loop,
)
from trajectory_pipeline.executor.branches import RunLedger
from trajectory_pipeline.executor.browser.page_driver import PageDriver
from trajectory_pipeline.executor.steps.search import Candidate
from trajectory_pipeline.executor.steps.visit import (
    VisitResult,
    enter_site,
    judge_player,
    open_tab,
)
from trajectory_pipeline.perception.base import Observation, Perceptor


async def agent_visit(
    driver: PageDriver,
    perceptor: Perceptor,
    ledger: RunLedger,
    candidate: Candidate,
    backend: AgentBackend,
    *,
    max_chars: int | None = None,
    title: str = "",
    gatekeeper_events: list[dict[str, Any]] | None = None,
    llm_calls: list[dict[str, Any]] | None = None,
) -> VisitResult:
    """在一个候选站点内让 agent 自主导航，**把结果与 :func:`~.visit.visit_site`
    记成同一个形状**。**本函数不抛异常**（与 ``visit_site`` 同纪律）。

    Args:
        driver: 页面驱动。
        perceptor: 只用于 ⑦ 的 ``PLAYER_OK``。**站内那六步不经过它**——
            agent 的自主性就在于它自己看着页面走，不靠判断题中转。
        ledger: 记账本。落进去的分支与 scripted 出自同一个封闭集。
        candidate: 候选站点。
        backend: :class:`AgentBackend`——后端四件套，由 CLI 在边界组装
            （``executor`` 不 import ``llm``，见 :mod:`.agent_loop`）。
        max_chars: 观察正文预算。**与 scripted 同一个口径**——观察口径
            分叉会让 agent 与判断题看到的不是同一份页面投影（§3.8）。
        title: 目标片名。进 :class:`AgentTask`，是任务的属性不是页面的
            属性（与 ``perceptor`` 的构造注入同一条纪律）。
        gatekeeper_events: ``RunRecord.gatekeeper_events`` 透传容器。
            ``None`` = 丢弃（单测用）。
        llm_calls: ``RunRecord.llm_calls`` 账本透传容器（§3.6，A2a-4 起由
            orchestrator 传入同一个 list 对象）。``None`` = 丢弃（单测用）。

    Returns:
        :class:`~.visit.VisitResult`——与 scripted **同一个类型**：P1 落盘
        对下游零差异（§3.7 最后一条）。
    """
    result = VisitResult(candidate=candidate)
    url = candidate.url

    # ── ① ② 进站：与 scripted 共用的那一段 ──────────────────────
    site_obs = await enter_site(driver, ledger, result, max_chars=max_chars)
    if site_obs is None:
        return result

    # 进站后**立刻**开独立 tab（scripted 在点击前才开）：agent 在站内
    # 要走好几步，每一步都该落在它自己的会话里。
    await open_tab(driver, url, result)

    # ── 站内：整个交给循环 ─────────────────────────────────────
    outcome = await agent_loop(
        driver,
        backend.client,
        backend.escalate,
        AgentTask(title=title, url=url, site_title=candidate.text),
        result.steps,
        backend.budget,
        initial_obs=site_obs,
        json_of=backend.json_of,
        gatekeeper_events=gatekeeper_events,
        llm_calls=llm_calls,
        max_chars=max_chars,
    )
    result.note(
        f"agent 循环：{outcome.steps_taken} 步 / {outcome.llm_calls} 次调用"
    )

    if outcome.reached:
        if outcome.final_obs is None:
            # 契约守卫：reached 形态必带最终观察（``agent_loop`` 的起始
            # 观察是必给参数）。走到这里只可能是那边改了契约而这里没跟上；
            # 那时的正确处置是记 unresolved，而不是拿 None 去判 PLAYER_OK
            # ——后者会让「没判」和「判了不能用」在账本上同形。
            ledger.record(
                result.landed_url or url, UNRESOLVED,
                f"agent 报到达但没有可判的观察（{outcome.reason}）",
                site_url=url,
            )
            result.note("agent 报到达但没有可判的观察")
            return result
        # ── ⑦ 与 scripted 同一段代码 ────────────────────────────
        judge_player(perceptor, ledger, result, outcome.final_obs,
                     pre_obs=site_obs)
        return result

    # 放弃 / 被收口。**记哪一支是映射表的事**（§3.3）：agent 只报「没到」，
    # 本文件不把它翻成分支名——翻的人已经在 ``agent_loop`` 里翻过了。
    ledger.record(
        result.landed_url or url,
        outcome.branch or UNRESOLVED,
        outcome.reason,
        site_url=url,
    )
    result.note(f"agent 未到达：{outcome.kind}（{outcome.reason}）")
    return result


__all__ = ["agent_visit"]
