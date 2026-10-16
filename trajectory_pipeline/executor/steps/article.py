"""文章页访问链——路径 C 的中间页单元（2026-10-16 路径 B/C 发现链）。

C 路径在搜索页选出「文章 / 社区帖候选」后，本模块负责**访问并开采**：
goto 探测 → ``SELECT_REFERRAL_LINKS`` 选正文引荐外链 → 逐外链
:func:`~trajectory_pipeline.executor.steps.link_check.resolve_referral`
（**跳转确认页检查发生在这里**）→ 产出最终候选（``origin="article"``）。

与 :mod:`.aggregator` 同构，唯二差异：

1. **没有页面级确认题**。①C 在搜索页选题时已判过「这是文章页」
   （rejected 落 ``not_article``），访问链不重复问——同一件事问两遍，
   两遍答案不一致时反而没法处置。
2. 语义分支换成 ``article_no_links``（文章页无可引荐外链）。

外链 ``dropped`` 同样不是负分支（理由见 :mod:`.link_check`）。
"""

from __future__ import annotations

from trajectory_pipeline.executor.branches import RunLedger
from trajectory_pipeline.executor.browser.page_driver import PageDriver
from trajectory_pipeline.executor.steps import link_check, search
from trajectory_pipeline.executor.steps.aggregator import (
    IntermediateResult,
    ROLE_ARTICLE,
    base_host_of,
    link_texts,
    probe_and_observe,
    resolve_into_candidates,
    selected_links,
)
from trajectory_pipeline.executor.steps.search import Candidate
from trajectory_pipeline.perception.base import Perceptor, Q


async def visit_article(
    driver: PageDriver,
    perceptor: Perceptor,
    ledger: RunLedger,
    candidate: Candidate,
    *,
    max_chars: int | None = None,
    max_links: int = 5,
    engine: str = search.DEFAULT_ENGINE,
    stats: dict[str, int] | None = None,
) -> IntermediateResult:
    """访问一个文章页候选并开采引荐外链。**本函数不抛异常**。"""
    result = IntermediateResult(role=ROLE_ARTICLE, candidate=candidate)
    obs = await probe_and_observe(
        driver, ledger, result, max_chars=max_chars
    )
    if obs is None:
        return result
    landed = result.landed_url

    # ── ⑥C SELECT_REFERRAL_LINKS：选正文引荐外链（语义层）──────
    links = perceptor.decide(Q.SELECT_REFERRAL_LINKS, obs)
    result.entries = links
    if links.answer is True:
        await resolve_into_candidates(
            driver, result, selected_links(links),
            origin=ROLE_ARTICLE, engine=engine,
            max_chars=max_chars, max_links=max_links, stats=stats,
        )
        return result
    if links.answer is False:
        # 真结论：这篇文章没有引荐任何影视站外链 → article_no_links。
        ledger.record_from(landed, links, site_url=candidate.url)
        result.note("SELECT_REFERRAL_LINKS=False")
        return result

    # None：fail-closed，启发式结构性提取（不中断采集，与 ① 同口径）。
    result.note(f"外链选题 fail-closed（{links.evidence}），启发式全量")
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
        origin=ROLE_ARTICLE, engine=engine,
        max_chars=max_chars, max_links=max_links, stats=stats,
    )
    return result


__all__ = ["visit_article"]
