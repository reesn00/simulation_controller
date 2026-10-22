"""逐站点遍历——一个候选站点的完整判断链。

这里是**控制流的主体**，但它只做编排：判断全部交给 :class:`Perceptor`，
失败分支全部交给 :class:`RunLedger`。本文件不含任何「什么算播放按钮」
的语义——那种东西一旦漏进来，切 Perceptor 实现时就会连带改这里，
而「切实现时 executor 的 git diff 必须为空」是模块 3 可替换的前提。

三条与直觉相反但都是实测/纪律要求的做法：

1. **点击后必须重新 observe**。不重新 observe 就拿旧观察判播放页，
   等于判的是**站点页**而不是播放页——一个非常常见的静默错误：
   代码明明点了，结论却完全没用到点击的结果。
2. **点击不抛异常**。元素不存在是业务事实（该站没播放控件），
   让它变成异常会把一个正常负样本变成一次运行失败。
3. **重定向不判负**。见 :mod:`.reachability`——那是语言层的题。

## 与 agent 链的分工（2026-10-17 方案 13 §3.7）

站内的那一段是两条链的**唯一**差别：本文件走七段固定序列，
:mod:`.agent_visit` 把「③ 找控件 → ⑤ 点击」换成 :func:`agent_loop`。
其余两段**抽成函数共用，不复制**：

- :func:`enter_site`（① 导航 + ② 观察）：进站是硬事实，两条链对
  「打不开算什么」必须给同一个答案，否则负样本池会混进两种成因的
  ``unreachable_hard``——它们是同一支分支，混了就看不出来；
- :func:`judge_player`（⑦ PLAYER_OK + 记账）：负样本纯度的最后一道
  闸（``component_unverified`` 的前置事实守卫）长在这段里，两条链
  各写一份的话闸只会在一条上生效，而漏掉的那条产出的假负样本
  **理由通顺、看着专业**，人工也未必看得出来。

``visit_site`` 本身**不删不改**——A3 的 fallback 与黄金基线都是它。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from trajectory_pipeline.executor import actions
from trajectory_pipeline.executor.actions import StepLog
from trajectory_pipeline.executor.browser.page_driver import DriverError, PageDriver
from trajectory_pipeline.executor.branches import RunLedger
from trajectory_pipeline.executor.steps import reachability, site_search as site_search_mod
from trajectory_pipeline.executor.steps.search import Candidate
from trajectory_pipeline.perception.base import Observation, Perceptor, Q


@dataclass
class VisitResult:
    """一次站点遍历的结果。"""

    candidate: Candidate
    landed_url: str = ""
    site_obs: Observation | None = None
    player_obs: Observation | None = None
    success: bool = False
    notes: list[str] | None = None
    #: 动作流。P2 六件套第 ④⑤ 件的唯一数据源，语义见
    #: :mod:`trajectory_pipeline.executor.actions`。**不参与任何判定**——
    #: 本文件对「点了什么」的行动仍只看 ``control.payload['ref']``，
    #: 动作流是**记录**，不是控制输入（两者一旦耦合，改记录方式就改控制流）。
    steps: StepLog = field(default_factory=StepLog)
    #: **本站点单元的终局反思**（INC46）。与 ``steps`` 平级，
    #: 与 :attr:`~trajectory_pipeline.executor.orchestrator.RunRecord.reflection`
    #: 同款：LLM 产物，不进 ``outcomes``（那是采集事实层）。
    #: ``None`` = 没生成，与「生成了但内容是空的」不同——三态原样落盘。
    reflection: dict[str, Any] | None = None
    #: **站内搜索的结果**（2026-10-16 路径 B/C）。``None`` = 没走站内
    #: 搜索（direct 路径 / 深层链接候选 / 非首页落地）——与「走了但
    #: 四种归宿之一」是两回事，P1 落盘靠它区分「没启用」与「启用了
    #: 但 fail-closed」。``ROUTED`` 时 ``.selected`` 是派发的子候选，
    #: 由 orchestrator 追加进遍历队列。
    search: site_search_mod.SiteSearchResult | None = None

    def note(self, text: str) -> None:
        if self.notes is None:
            self.notes = []
        self.notes.append(text)


async def visit_site(
    driver: PageDriver,
    perceptor: Perceptor,
    ledger: RunLedger,
    candidate: Candidate,
    *,
    max_chars: int | None = None,
    allow_site_search: bool = False,
    title: str = "",
) -> VisitResult:
    """遍历一个候选站点，把结果记进 ``ledger``。**本函数不抛异常**。

    任何异常都被折算成分支——包括看起来「不该发生」的类型。
    理由：批处理里一个站点的异常不该中断整批，而异常若穿透出去，
    上层就得写 try/except，而那正是「静默失败」的开始。
    """
    result = VisitResult(candidate=candidate)
    url = candidate.url

    # ── ① ② 进站：与 agent 链共用的那一段（见本文件头部分工说明）──
    site_obs = await enter_site(driver, ledger, result, max_chars=max_chars)
    if site_obs is None:
        return result

    # ── ③ 判断点 ②：页面是否正常访问（语言层）──────────────────
    #    **None 时不中断，继续往下走。** 这一点是实测逼出来的：
    #    W1 的规则版对 IS_REACHABLE 只会返回 None，若据此中断，
    #    整条链在第一步就断了——5 个真实候选站点全部落 unresolved，
    #    W1 验收要的「负样本池非空」一条都产不出来。
    #
    #    这不违反 fail-closed：**fail-closed 管的是「结论」不是「采集」**。
    #    继续走不等于断言「页面可达」，而是继续收集证据；最终分支仍按
    #    「有没有得到确定性结论」来定——拿不到就是 unresolved，
    #    拿到了（如下面 FIND_PLAY_CONTROL 判 False）就是货真价实的
    #    no_play_control，而「登录墙」与「无播放控件」在人工复核时
    #    本来就可由 evidence 区分。
    reachable = perceptor.decide(Q.IS_REACHABLE, result.site_obs)
    if reachable.answer is False:
        # ⚠️ **必须传 ``obs``**：前置事实核验（``branches._precondition_failed``）
        # 要拿观察里的**页面原文**核「门禁语境」——② 的付费墙救济路径
        # （``payload["paywall"]`` → ``subscription_wall``）也走这里。
        # 不传 obs ⇒ 核验被跳过（守卫的既有约定：调用方没传就放行），
        # 于是「② 判否 + 正文有门禁证据」这条路径的前置事实**从来没被核过**。
        ledger.record_from(result.landed_url, reachable, obs=result.site_obs,
                           site_url=url)
        result.note("IS_REACHABLE=False")
        return result
    if reachable.answer is None:
        result.note("IS_REACHABLE=None（不中断，继续采集）")

    # ── ③.5 首页分叉：站内搜索（路径 B/C，2026-10-16）──────────
    # 首页上的控件是**推荐位内容**，与目标片名无关——直接跑
    # FIND_PLAY_CONTROL 会把「首页推荐位」当播放控件判，产出理由
    # 通顺的假负样本（站里可能有，只是不在首页，§5.14 前置事实
    # 污染）。所以 B/C 路径的首页型候选跳过控件判定，先用站内
    # 搜索把片名定位到深层页面，子候选再走标准 visit 链。
    #
    # 判的是**落地页**（``landed_url``）不是候选 URL：候选是深层
    # 链接但被重定向回首页的形态，页面上看到的就是首页，控件同样
    # 是推荐位——判候选 URL 会漏掉这种。
    #
    # direct 路径不进来：它的首页候选在环节①已被 ``_drop_site_homepages``
    # 记 not_play_site 剔除（「找错站」与「站看不了」分开，见其
    # docstring），双重防线不重叠。
    if allow_site_search and site_search_mod.is_site_homepage(
        result.landed_url
    ):
        result.search = await site_search_mod.site_search(
            driver, perceptor, ledger, result.steps, candidate,
            result.site_obs, title, max_chars=max_chars,
        )
        return result

    # ── ④ 判断点 ③：找播放控件 ──────────────────────────────────
    control = perceptor.decide(Q.FIND_PLAY_CONTROL, result.site_obs)
    suspects = list(control.payload.get("trailer_suspect") or [])
    if suspects:
        # 词表判不准的疑似预告——记 suspect 交人工，不自动判负。
        # 自动判负会把「讲解预告的攻略页」误杀成负样本，
        # 而误杀是不可逆的：样本没了，且没人知道它曾存在。
        #
        # ⚠️ 这个判断**必须独立于 trailer_only**，不能嵌在里面。
        # 感知层在「候选全是预告」时给的是 ``trailer_only = not suspects``——
        # 两者互斥，所以嵌着写的话 ``trailer_suspect`` 这条分支永远走不到，
        # 疑似预告会掉进下面的「answer=True 但无 ref」去记成
        # ``component_unverified``（一条货真价实的负样本）。
        # 实测：这条不可达分支曾让 trailer_suspect 全程 0 次出现。
        ledger.record(
            result.landed_url, "trailer_suspect",
            f"候选控件疑似预告，词表判不准: {suspects}",
            decision=control, site_url=url,
        )
        result.note(f"trailer_suspect: {suspects}")
        return result
    if control.payload.get("trailer_only"):
        ledger.record(
            result.landed_url, "trailer_only",
            control.evidence, decision=control, site_url=url,
        )
        result.note("候选控件全为预告片")
        return result

    ref = str(control.payload.get("ref") or "")
    if control.answer is not True:
        # FIND_PLAY_CONTROL 拿不到确定性结论 → unresolved，负样本到此为止
        ledger.record_from(result.landed_url, control, obs=result.site_obs,
                           site_url=url)
        result.note(f"FIND_PLAY_CONTROL={control.answer}")
        return result
    if not ref:
        # I6 被违反：answer=True 却没有可点的 ref。fail-closed 而不是硬点一个
        ledger.record(
            result.landed_url, "component_unverified",
            f"判定说有播放控件但未回传 ref（违反 I6）: {control.evidence}",
            decision=control, site_url=url,
        )
        result.note("answer=True 但无 ref——契约违规，按 fail-closed 记账")
        return result

    # ── ⑤ 点击：每站点一个 tab，隔离会话 ─────────────────────────
    # 语义化目标在**点击之前**解析：它来自站点页观察（``site_obs``），
    # 不是点击后的观察。解析不出来也要照实记 ``target=None`` 并继续点——
    # 那说明 I6 被违反（ref 溯源不到 interactive_elements），是**要留证的现场**，
    # 而 fail-closed 在这里的表现是「不编一个目标」，不是「不点」。
    target = actions.target_of(ref, result.site_obs.interactive_elements)
    if target is None:
        result.note(f"ref={ref} 未在 interactive_elements 中找到（I6 存疑），"
                    f"动作流按 target=None 记录")
    await open_tab(driver, url, result)
    step_click = result.steps.act(actions.click(target))
    try:
        await driver.click(**actions.target_params(
            result.site_obs.element_vocabulary, target, ref=ref))
    except (DriverError, actions.UnaddressableTarget) as exc:
        result.steps.settle(step_click, error=f"点击失败: {exc}")
        ledger.record(
            result.landed_url, "component_unverified",
            f"点击 ref={ref} 失败: {exc}", decision=control, site_url=url,
        )
        result.note(f"点击失败: {exc}")
        return result

    # ── ⑥ 重新观察：判的是播放页，不是站点页 ────────────────────
    try:
        player_obs = await driver.observe(max_chars=max_chars)
    except DriverError as exc:
        result.steps.settle(step_click, error=f"点击后快照失败: {exc}")
        ledger.record(
            result.landed_url, "component_unverified",
            f"点击后取不到页面快照: {exc}", decision=control, reached=True,
            site_url=url,
            # play_page_url 留空：点击了但快照取不到，我们**不知道**那是
            # 不是播放页。按 D-6 的口径，「播放页 URL」在没到达播放页时
            # 不适用、不扣分——编一个出来才是把猜测写成事实。
        )
        result.note(f"点击后快照失败: {exc}")
        return result
    result.steps.settle(step_click, player_obs)

    # ── ⑦ 判断点 ④：播放页是否可用 ─────────────────────────────
    # 与 agent 链共用（pre_obs = 点击前的站点页观察，守卫靠「点击前后
    # 是否换页」核它）。
    judge_player(perceptor, ledger, result, player_obs,
                 pre_obs=result.site_obs)
    return result


async def enter_site(
    driver: PageDriver,
    ledger: RunLedger,
    result: VisitResult,
    *,
    max_chars: int | None = None,
) -> Observation | None:
    """① 导航 + ② 观察站点页。**进站段，两条执行链共用**。

    返回 ``None`` = 已经记过 ``unreachable_hard``，调用方直接返回即可。

    它落的是「这个 URL 打不打得开」这个**硬事实**，与「页面能不能看」
    （``IS_REACHABLE`` / gatekeeper 拦截）是两件事，谁都不该替谁下结论
    ——所以 agent 链也走这里：站都进不去的候选，没有让模型进来找
    播放控件的道理。

    ⚠️ 记账口径**写在本函数里**而不是留给调用方记：两条链对
    「进站失败算什么」必须给同一个答案，否则负样本池里会混进两种
    成因的 ``unreachable_hard``，而它们是同一支分支，混了看不出来。
    """
    url = result.candidate.url
    reach = await reachability.probe(driver, url)
    step_goto = result.steps.act(actions.goto(url))
    if not reach.reachable:
        # 记「试过且失败」。unreachable_hard 在动作流里若整段消失，
        # 负样本池里那条就会变成一条没有动作的判决，说不清模型该学什么。
        result.steps.settle(step_goto, error=reach.reason)
        ledger.record(url, "unreachable_hard", reach.reason, site_url=url)
        result.note(f"导航失败: {reach.reason}")
        return None

    try:
        obs = await driver.observe(max_chars=max_chars)
    except DriverError as exc:
        result.steps.settle(step_goto, error=f"快照失败: {exc}")
        ledger.record(url, "unreachable_hard", f"导航成功但取不到快照: {exc}",
                      site_url=url)
        result.note(f"快照失败: {exc}")
        return None
    result.steps.settle(step_goto, obs)
    result.site_obs = obs

    landing = reachability.check_landing(obs, url)
    result.landed_url = landing.landed_url or url
    if landing.redirected:
        result.note(landing.reason)
    return obs


def judge_player(
    perceptor: Perceptor,
    ledger: RunLedger,
    result: VisitResult,
    player_obs: Observation,
    *,
    pre_obs: Observation | None,
) -> None:
    """⑦ 判断点 ④：播放页是否可用——**并记账**。两条执行链共用。

    ``pre_obs`` 是**进站观察**（点进播放页之前看到的那一页）。agent 链
    也传它，于是**落地即在播放页**的形态（深层链接候选，agent 一步没走
    就报「到了」）在守卫眼里是「没换页」——``PLAYER_OK`` 判 False 时
    会被 :func:`~trajectory_pipeline.executor.branches._precondition_failed`
    降级成 ``unresolved`` 而不是 ``component_unverified``。

    ⚠️ 这是**刻意的保守**，不是漏判：agent 说「到了」是自报，未经
    跨页证实。拿自报的前提去贴负标签，等于把「模型没找到播放页」
    写成「这个站看不了」——负样本池里最贵的一类错。让那批去
    ``unresolved``，人工复核时 transcript 与动作流都还在，丢的是
    一个负样本而不是一条假负样本。
    """
    result.player_obs = player_obs
    player = perceptor.decide(Q.PLAYER_OK, player_obs)
    play_url = player_obs.url or result.landed_url
    if player.answer is True:
        ledger.record(play_url, None, player.evidence, decision=player,
                      reached=True, site_url=result.candidate.url,
                      play_page_url=play_url)
        result.success = True
        return

    if player.answer is None:
        # 判不出来，**且播放页就在眼前** → player_unverified，不是 unresolved。
        # 两者都不是负样本，但成因不同：这里不确定的是「组件能不能用」
        # （页面证据不足），环节①–④ 的 unresolved 不确定的是「能不能到」
        # （代码能力不足）。分开的理由见 RunLedger.record_unresolved_reached。
        #
        # ⚠️ 必须在 record_from **之前**分流：record_from 会把 None 一律
        # 记成 unresolved，而这条恰好是它管不到的情形。
        ledger.record_unresolved_reached(
            play_url, player, site_url=result.candidate.url,
            play_page_url=play_url,
        )
        result.note("PLAYER_OK=None（有播放组件，确认不了能不能用）")
        return

    ledger.record_from(play_url, player, reached=True, obs=player_obs,
                       pre_obs=pre_obs,
                       site_url=result.candidate.url,
                       play_page_url=play_url)
    result.note(f"PLAYER_OK={player.answer}")


async def open_tab(driver: PageDriver, url: str, result: VisitResult) -> None:
    """为该站点开独立 tab。失败不阻断——就在当前页继续。

    独立 tab 的作用是**会话隔离**：站点 A 弹出的登录框 / 提示不影响
    站点 B 的观察。开不了就退化到当前 tab，不因此放弃该站点。

    agent 链在**进站之后立刻**调用它（scripted 在点击之前调），为的是
    让 agent 在站内走的那几步（搜索 → 详情 → 播放）全程落在自己的
    会话里——站点页留在原 tab，前一个候选的弹窗也跟不进来。

    捕获 ``Exception`` 而非 ``DriverError``：obscura 的 tab 相关 tool
    失败时返回的文本形态尚未实测（见 §7 待验证），窄捕获会让它直接穿透
    到批处理层。这里是「不阻断」的地方，宁可宽。
    """
    try:
        await driver.new_tab(url)
        result.steps.act(actions.new_tab(url))
        result.note("已开独立 tab")
    except Exception as exc:
        result.note(f"开 tab 失败（继续用当前页）: {type(exc).__name__}: {exc}")


def summarize(results: list[VisitResult]) -> dict[str, Any]:
    """一批站点遍历的概要。落进 P1 存档供离线分析。"""
    ok = [r for r in results if r.success]
    return {
        "sites": len(results),
        "succeeded": len(ok),
        "succeeded_urls": [r.candidate.url for r in ok],
        "reached_play_page": sum(1 for r in results if r.player_obs is not None),
    }


__all__ = [
    "VisitResult",
    "visit_site",
    "enter_site",
    "judge_player",
    "open_tab",
    "summarize",
]