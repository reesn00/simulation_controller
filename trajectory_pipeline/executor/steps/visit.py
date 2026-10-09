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
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from trajectory_pipeline.executor.browser.page_driver import DriverError, PageDriver
from trajectory_pipeline.executor.branches import RunLedger
from trajectory_pipeline.executor.steps import reachability
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
) -> VisitResult:
    """遍历一个候选站点，把结果记进 ``ledger``。**本函数不抛异常**。

    任何异常都被折算成分支——包括看起来「不该发生」的类型。
    理由：批处理里一个站点的异常不该中断整批，而异常若穿透出去，
    上层就得写 try/except，而那正是「静默失败」的开始。
    """
    result = VisitResult(candidate=candidate)
    url = candidate.url

    # ── ① 导航：唯一的硬事实 ──────────────────────────────────────
    reach = await reachability.probe(driver, url)
    if not reach.reachable:
        ledger.record(url, "unreachable_hard", reach.reason)
        result.note(f"导航失败: {reach.reason}")
        return result

    # ── ② 观察站点页 ──────────────────────────────────────────────
    try:
        result.site_obs = await driver.observe(max_chars=max_chars)
    except DriverError as exc:
        ledger.record(url, "unreachable_hard", f"导航成功但取不到快照: {exc}")
        result.note(f"快照失败: {exc}")
        return result

    landing = reachability.check_landing(result.site_obs, url)
    result.landed_url = landing.landed_url or url
    if landing.redirected:
        result.note(landing.reason)

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
        ledger.record_from(result.landed_url, reachable)
        result.note("IS_REACHABLE=False")
        return result
    if reachable.answer is None:
        result.note("IS_REACHABLE=None（不中断，继续采集）")

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
            decision=control,
        )
        result.note(f"trailer_suspect: {suspects}")
        return result
    if control.payload.get("trailer_only"):
        ledger.record(
            result.landed_url, "trailer_only",
            control.evidence, decision=control,
        )
        result.note("候选控件全为预告片")
        return result

    ref = str(control.payload.get("ref") or "")
    if control.answer is not True:
        # FIND_PLAY_CONTROL 拿不到确定性结论 → unresolved，负样本到此为止
        ledger.record_from(result.landed_url, control)
        result.note(f"FIND_PLAY_CONTROL={control.answer}")
        return result
    if not ref:
        # I6 被违反：answer=True 却没有可点的 ref。fail-closed 而不是硬点一个
        ledger.record(
            result.landed_url, "component_unverified",
            f"判定说有播放控件但未回传 ref（违反 I6）: {control.evidence}",
            decision=control,
        )
        result.note("answer=True 但无 ref——契约违规，按 fail-closed 记账")
        return result

    # ── ⑤ 点击：每站点一个 tab，隔离会话 ─────────────────────────
    await _open_tab(driver, url, result)
    try:
        await driver.click(ref=ref)
    except DriverError as exc:
        ledger.record(
            result.landed_url, "component_unverified",
            f"点击 ref={ref} 失败: {exc}", decision=control,
        )
        result.note(f"点击失败: {exc}")
        return result

    # ── ⑥ 重新观察：判的是播放页，不是站点页 ────────────────────
    try:
        result.player_obs = await driver.observe(max_chars=max_chars)
    except DriverError as exc:
        ledger.record(
            result.landed_url, "component_unverified",
            f"点击后取不到页面快照: {exc}", decision=control, reached=True,
        )
        result.note(f"点击后快照失败: {exc}")
        return result

    # ── ⑦ 判断点 ④：播放页是否可用 ─────────────────────────────
    player = perceptor.decide(Q.PLAYER_OK, result.player_obs)
    if player.answer is True:
        ledger.record(result.player_obs.url or url, None,
                      player.evidence, decision=player, reached=True)
        result.success = True
        return result

    ledger.record_from(result.player_obs.url or result.landed_url, player, reached=True)
    result.note(f"PLAYER_OK={player.answer}")
    return result


async def _open_tab(driver: PageDriver, url: str, result: VisitResult) -> None:
    """为该站点开独立 tab。失败不阻断——就在当前页继续。

    独立 tab 的作用是**会话隔离**：站点 A 弹出的登录框 / 提示不影响
    站点 B 的观察。开不了就退化到当前 tab，不因此放弃该站点。

    捕获 ``Exception`` 而非 ``DriverError``：obscura 的 tab 相关 tool
    失败时返回的文本形态尚未实测（见 §7 待验证），窄捕获会让它直接穿透
    到批处理层。这里是「不阻断」的地方，宁可宽。
    """
    try:
        await driver.new_tab(url)
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


__all__ = ["VisitResult", "visit_site", "summarize"]