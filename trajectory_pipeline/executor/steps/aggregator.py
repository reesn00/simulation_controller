"""聚合站访问链——路径 B 的中间页单元（2026-10-16 路径 B/C 发现链）。

B 路径在搜索页选出「聚合站候选」后，本模块负责**访问并开采**：
goto 探测 → ``IS_AGGREGATOR`` 确认（False 记 ``not_aggregator`` 负样本，
None 继续——fail-closed 不中断）→ ``SELECT_SITE_ENTRIES`` 选正式入口
→ 逐入口 :func:`~trajectory_pipeline.executor.steps.link_check.resolve_referral`
外链校验 → 产出最终 :class:`~trajectory_pipeline.executor.steps.search.Candidate`
（``origin="aggregator"``）。

与 :mod:`~trajectory_pipeline.executor.steps.visit` 的关键差异：
**中间页不是站点结论的产地**。一个聚合站页面上提不出入口，记的是
``aggregator_no_entries``（「这个聚合站没有可用入口」），而 visit 链里的
``no_play_control`` 说的是「这个站看不了」——两个负样本池的语义层不
同：前者是发现链的损耗（换一个聚合站还能找到），后者是终点结论。
混起来会让「为什么没找到站」与「为什么站看不了」再也分不开。

外链解析失败（``dropped``）**不是负分支**：一条引荐链接打不开，
说不出「这个聚合站不行」，只进 ``stats`` 记账（与 ``extract_candidates``
的 ``candidate_filter`` 同一套可见性纪律），见 :mod:`.link_check`。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlparse

from trajectory_pipeline.executor import actions
from trajectory_pipeline.executor.actions import StepLog
from trajectory_pipeline.executor.branches import RunLedger
from trajectory_pipeline.executor.browser.page_driver import (
    DriverError,
    PageDriver,
)
from trajectory_pipeline.executor.steps import link_check, reachability, search
from trajectory_pipeline.perception.base import Decision, Observation, Perceptor, Q

#: 中间页角色（``IntermediateResult.role`` 的两个取值）。
ROLE_AGGREGATOR = "aggregator"
ROLE_ARTICLE = "article"


@dataclass
class IntermediateResult:
    """一个中间页（聚合站页 / 文章页）的访问结果——P1 ``intermediates[]``
    的元素，B/C 路径的独立决策单元。

    ``is_intermediate`` 只在 B 路径有意义（``IS_AGGREGATOR`` 确认）；
    C 路径没有「这是不是文章页」的确认题（①C 在搜索页选题时已判过，
    访问链直接选外链），恒为 ``None``。

    ``candidates`` 是**发现链产出的最终候选**（外链校验通过的落地
    URL），不是中间页自己的链接——它们随后进 orchestrator 环节②
    走标准 visit 链。
    """

    role: str
    candidate: search.Candidate
    landed_url: str = ""
    obs: Observation | None = None
    is_intermediate: Decision | None = None
    entries: Decision | None = None
    candidates: list[search.Candidate] = field(default_factory=list)
    referrals: list[link_check.ReferralResolution] = field(default_factory=list)
    steps: StepLog = field(default_factory=StepLog)
    notes: list[str] = field(default_factory=list)

    def note(self, text: str) -> None:
        self.notes.append(text)


def selected_links(decision: Decision) -> list[tuple[str, str]]:
    """从 SELECT_* 的 ``Decision.payload`` 取选中行的 ``(href, text)``。

    感知层（:meth:`llm_perceptor._select_from_links`）已把编号回指
    解析回**观察里的原始 href**，这里只做形态收敛：url 非空才收，
    text 尽力取 ``title``/``why``（①B 的行带 title、⑥ 的行只有 why，
    都比空串可读——它是纯审计字段，不参与任何判定）。
    """
    out: list[tuple[str, str]] = []
    for row in (decision.payload.get("selected") or []):
        if not isinstance(row, dict):
            continue
        href = str(row.get("url") or "").strip()
        if not href:
            continue
        text = str(row.get("title") or row.get("why") or "").strip()
        out.append((href, text))
    return out


def link_texts(obs: Observation) -> dict[str, str]:
    """观察里的 href → 链接文本（启发式兜底时给 Candidate 补 text）。"""
    out: dict[str, str] = {}
    for link in obs.links or ():
        if link.href and link.href not in out:
            out[link.href] = link.text or ""
    return out


def base_host_of(url: str) -> str:
    """提取 host（小写）。解析失败返回空串——外链校验的 ``same_host``
    剔除对空 host 不生效（拿不准就不剔），与 fail-closed 同向。"""
    try:
        return (urlparse(url).hostname or "").lower()
    except ValueError:
        return ""


async def probe_and_observe(
    driver: PageDriver,
    ledger: RunLedger,
    result: IntermediateResult,
    *,
    max_chars: int | None = None,
) -> Observation | None:
    """中间页共用前置：导航 + 观察。失败记账并返回 ``None``。

    不可达记 ``unreachable_hard``（与 visit 链 ① 同款）——中间页打不开
    是传输层事实，**不是** ``not_aggregator``（后者要页面在手才能判）。
    动作流记 ``result.steps``：中间页是独立决策单元（P2 切条），
    导航这一步属于它自己，不属于搜索单元。
    """
    candidate = result.candidate
    reach = await reachability.probe(driver, candidate.url)
    step_goto = result.steps.act(actions.goto(candidate.url))
    if not reach.reachable:
        result.steps.settle(step_goto, error=reach.reason)
        ledger.record(
            candidate.url, "unreachable_hard", reach.reason,
            site_url=candidate.url,
        )
        result.note(f"导航失败: {reach.reason}")
        return None
    try:
        result.obs = await driver.observe(max_chars=max_chars)
    except DriverError as exc:
        result.steps.settle(step_goto, error=f"快照失败: {exc}")
        ledger.record(
            candidate.url, "unreachable_hard",
            f"导航成功但取不到快照: {exc}", site_url=candidate.url,
        )
        result.note(f"快照失败: {exc}")
        return None
    result.steps.settle(step_goto, result.obs)
    landing = reachability.check_landing(result.obs, candidate.url)
    result.landed_url = landing.landed_url or candidate.url
    if landing.redirected:
        result.note(landing.reason)
    return result.obs


async def resolve_into_candidates(
    driver: PageDriver,
    result: IntermediateResult,
    hrefs: list[tuple[str, str]],
    *,
    origin: str,
    engine: str = search.DEFAULT_ENGINE,
    max_chars: int | None = None,
    max_links: int = 5,
    stats: dict[str, int] | None = None,
) -> None:
    """逐条外链过 :func:`~.link_check.resolve_referral`，产出最终候选。

    ``origin``（"aggregator" / "article"）与 ``parent``（中间页落地 URL）
    写进候选——批次出了问题要能从候选回溯到「哪个中间页引荐的它」。
    上限 ``max_links`` 截断在**校验之前**：外链校验每条都要开 tab
    真实访问，截断是预算控制不是语义筛选（保序截断，页面推荐权重
    不可重排）。
    """
    parent = result.landed_url or result.candidate.url
    base_host = base_host_of(parent)
    for href, text in hrefs[:max_links]:
        resolution = await link_check.resolve_referral(
            driver, result.steps, href,
            base_host=base_host, engine=engine,
            max_chars=max_chars, stats=stats,
        )
        result.referrals.append(resolution)
        if resolution.ok:
            result.candidates.append(
                search.Candidate(
                    url=resolution.url,
                    text=text or resolution.source_href[:80],
                    rank=len(result.candidates) + 1,
                    source_href=resolution.source_href,
                    origin=origin,
                    parent=parent,
                )
            )
    if len(hrefs) > max_links:
        result.note(
            f"外链 {len(hrefs)} 条超上限 {max_links}，截断 "
            f"（预算控制，非语义筛选）"
        )


async def visit_aggregator(
    driver: PageDriver,
    perceptor: Perceptor,
    ledger: RunLedger,
    candidate: search.Candidate,
    *,
    max_chars: int | None = None,
    max_links: int = 5,
    engine: str = search.DEFAULT_ENGINE,
    stats: dict[str, int] | None = None,
) -> IntermediateResult:
    """访问一个聚合站候选并开采入口。**本函数不抛异常**（批处理纪律）。

    控制流：probe → observe → ``IS_AGGREGATOR`` → ``SELECT_SITE_ENTRIES``
    → 逐入口外链校验。感知层 None 一律**不中断**（fail-closed 管结论
    不管采集）：``IS_AGGREGATOR=None`` 继续选题；``SELECT_SITE_ENTRIES=None``
    退启发式结构性提取（:func:`link_check.eligible_referral_hrefs`）。
    """
    result = IntermediateResult(role=ROLE_AGGREGATOR, candidate=candidate)
    obs = await probe_and_observe(
        driver, ledger, result, max_chars=max_chars
    )
    if obs is None:
        return result
    landed = result.landed_url

    # ── ⑤ IS_AGGREGATOR：页面级确认（语义层）────────────────────
    is_agg = perceptor.decide(Q.IS_AGGREGATOR, obs)
    result.is_intermediate = is_agg
    if is_agg.answer is False:
        # 真结论：这个候选不是聚合站 → not_aggregator（负样本）。
        ledger.record_from(landed, is_agg, site_url=candidate.url)
        result.note("IS_AGGREGATOR=False")
        return result
    if is_agg.answer is None:
        result.note("IS_AGGREGATOR=None（不中断，继续采集）")

    # ── ⑥ SELECT_SITE_ENTRIES：选正式入口（语义层）──────────────
    entries = perceptor.decide(Q.SELECT_SITE_ENTRIES, obs)
    result.entries = entries
    if entries.answer is True:
        await resolve_into_candidates(
            driver, result, selected_links(entries),
            origin=ROLE_AGGREGATOR, engine=engine,
            max_chars=max_chars, max_links=max_links, stats=stats,
        )
        return result
    if entries.answer is False:
        # 真结论：是聚合站但页面上没有正式影视站入口。
        # （IS_AGGREGATOR=False 的情形在上一段已 return，能走到这里
        # 说明确认是 True 或 None——None 时贴这条负分支的结构保证
        # 见实施计划「不新增 BRANCH_PRECONDITION」一节。）
        ledger.record_from(landed, entries, site_url=candidate.url)
        result.note("SELECT_SITE_ENTRIES=False")
        return result

    # None：fail-closed，启发式结构性提取（不中断采集，与 ① 同口径）。
    result.note(f"入口选题 fail-closed（{entries.evidence}），启发式全量")
    texts = link_texts(obs)
    hrefs = [
        (href, texts.get(href, ""))
        for href in link_check.eligible_referral_hrefs(
            obs, base_host=base_host_of(landed), engine=engine,
            limit=max_links,
        )
    ]
    await resolve_into_candidates(
        driver, result, hrefs,
        origin=ROLE_AGGREGATOR, engine=engine,
        max_chars=max_chars, max_links=max_links, stats=stats,
    )
    return result


__all__ = [
    "IntermediateResult", "visit_aggregator", "selected_links",
    "link_texts", "base_host_of", "probe_and_observe",
    "resolve_into_candidates",
    "ROLE_AGGREGATOR", "ROLE_ARTICLE",
]
