"""最小站内搜索——首页型候选在 B/C 路径下的「先找片、再核验」。

为什么必须有它（§5.14 前置事实污染）：聚合站入口 / 文章外链给出来的
候选很多是**站点首页**。首页上的控件是推荐位内容，与目标片名无关——
直接跑 FIND_PLAY_CONTROL 会把「首页推荐位」当播放控件判，产出
「这个站看不了」的**错误负样本**：理由通顺（首页确实没有目标片名的
控件）、结论全错（站里可能有，只是不在首页）。所以 B/C 路径的首页型
候选**跳过控件判定**，先用站内搜索把片名定位到深层页面，子候选再走
标准 visit 链。

四条设计口径
------------

1. **失败形态分四种，不合并**。``no_box``（没搜索框，负样本）、
   ``no_results``（搜了没有，负样本）、``routed``（派发了子候选，
   路由记账）、``unresolved``（提交失败 / 判不出来，fail-closed）。
   「提交失败」与「没结果」必须分开：前者是**我们的动作没生效**
   （页面没变），后者是**站点的真实回答**——混在一起会把技术故障
   记成站点结论。

2. **结果页复用 ``SELECT_PLAY_SITES``**。站内搜索结果也是
   「提供在线观看该作品的站点列表」，①的题意原样成立；None 时
   启发式全量，与直搜路径 A 同一条 fail-closed 纪律。

3. **启发式不按 host 去重**。站内搜索的结果**主要指向本站深层页**
   （``/v/xxx``、``/play/xxx``），结果页全是同一个 host——按 host
   去重会把整页结果折成一条。这与直搜链的
   :func:`~trajectory_pipeline.executor.steps.search.extract_candidates`
   刻意不同：那边去重要防的是「同站多入口反复卡」，这边要的就是
   同站的多个深层入口。

4. **子候选带溯源**。``origin="site_search"``、``parent=结果页 URL``，
   批次出了问题能从候选回溯到「哪次站内搜索派发的它」。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any, Mapping
from urllib.parse import urlsplit

from trajectory_pipeline.executor import actions
from trajectory_pipeline.executor.actions import StepLog
from trajectory_pipeline.executor.branches import RunLedger, _same_page
from trajectory_pipeline.executor.browser.page_driver import DriverError, PageDriver
from trajectory_pipeline.executor.steps import search
from trajectory_pipeline.common.elements import (
    ELEMENT_VOCAB_ARIA_ROLE,
    ELEMENT_VOCAB_HTML_TAG,
)
from trajectory_pipeline.perception.base import (
    InteractiveElement,
    Observation,
    Perceptor,
    Q,
)

#: 四种归宿。
ROUTED = "routed"
NO_BOX = "no_box"
NO_RESULTS = "no_results"
UNRESOLVED = "unresolved"


def is_site_homepage(url: str) -> bool:
    """URL 是否指向**站点首页**（path 为空或 ``/``）。

    只看 path，不看 host——path 为 ``/`` 的 URL 一定是首页，而 query 与
    fragment 不参与：``https://www.hao123.com/?from=baidu`` 仍是首页。
    解析失败（畸形 URL）返回 ``False``：拿不准时**不判**，交给后面
    的环节用真实观察去判——这条判据的价值全在「确定」，不确定就不算。

    本函数与 :func:`orchestrator._is_site_homepage` 同源（后者委托到这里，
    visit 链的首页分叉与 direct 路径的首页丢弃必须同一把尺子）——
    ``_is_site_homepage`` 留在 orchestrator 是 mutation_check 的锚点
    与既有测试的 import 面，两侧漂移由单测断言委托关系。
    """
    try:
        path = urlsplit(url).path
    except ValueError:
        return False
    return path in ("", "/")

#: 「搜索」按钮的词表。回车提交失败（页面未变）时的第二条提交路径。
#:
#: 刻意只认**显式写着搜索词**的可点击元素：``type=submit`` 的 input
#: label 常为空，猜它是搜索按钮的误点风险大于收益（与 INC43 之后
#: 「词表救济必须核信号位置」同一条纪律）。
SEARCH_BUTTON_HINTS: tuple[str, ...] = ("搜索", "检索", "搜 索", "search")
#: 可当搜索按钮提交的 kind 槽词表，**一套词表一个方言**（P4 收窄；
#: P3-α 的并集已退役）。
#:
#: 并集会**在两个方言下都命中错的东西**：``role`` 槽装 ``button`` 时两面看着
#: 一样（``<button>`` 的 role 也是 ``button``），于是「换了执行层」在这条路上
#: 没有任何痕迹——而 ``a`` 与 ``link`` 这种值本来就不通用。
#:
#: 词汇表认不出时**抛**（:func:`_searchable_words`），不回落：回落等于猜
#: 「这份观察大概是什么面」，猜错的症状是词表命中 0 个、然后被读成
#: 「这页确实没有搜索按钮」。
#:
#: **ARIA 那套只补 ``link``**：role 下的提交控件本来就叫 ``button``
#: （``<input type=submit>`` 的 role 也是 ``button``）；``textbox`` 之类的
#: **点下去不会提交**，收进来会把「回车失败后的第二条路」变成
#: 「点一下搜索框，然后什么都没发生」。
_SEARCHABLE_WORDS: Mapping[str, frozenset[str]] = MappingProxyType({
    ELEMENT_VOCAB_HTML_TAG: frozenset({"button", "input", "a"}),
    ELEMENT_VOCAB_ARIA_ROLE: frozenset({"button", "link"}),
})


def _searchable_words(obs: Observation) -> frozenset[str]:
    """该观察的元素词表对应的「可提交控件」词集。认不出**抛**（不猜）。"""
    try:
        return _SEARCHABLE_WORDS[obs.element_vocabulary]
    except KeyError:
        raise ValueError(
            f"认不出的元素词汇表 {obs.element_vocabulary!r}——"
            f"搜索按钮词表无从确定。已知的：{sorted(_SEARCHABLE_WORDS)}"
        ) from None


@dataclass
class SiteSearchResult:
    """一次站内搜索的结果。"""

    status: str
    #: 派发的子候选（仅 ``routed`` 非空）。构造上带
    #: ``origin="site_search"`` 与 ``parent=结果页 URL``。
    selected: list[search.Candidate] = field(default_factory=list)
    #: 提交成功后的结果页观察。``no_box`` / 提交失败时为 ``None``
    #: ——那是「没有这份证据」，与「结果页是空的」不同物。
    results_obs: Observation | None = None
    notes: list[str] = field(default_factory=list)

    def note(self, text: str) -> None:
        self.notes.append(text)

    def to_json(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "selected": [
                {
                    "url": c.url,
                    "text": c.text[:80],
                    "rank": c.rank,
                    "origin": c.origin,
                    "parent": c.parent,
                }
                for c in self.selected
            ],
            "results_url": self.results_obs.url if self.results_obs else "",
            "notes": list(self.notes),
        }


def _find_search_button(
    elements: tuple[InteractiveElement, ...],
    *,
    vocabulary: str,
    skip_ref: str = "",
) -> InteractiveElement | None:
    """词表找「搜索」按钮。找不到返回 ``None``（不猜）。

    ``vocabulary``：这批元素的词表（``obs.element_vocabulary``）。**必填**——
    给它一个 ``html_tag`` 的默认值就等于「不知道就按 obscura 猜」，
    而猜错的症状是这条路静默失效（命中 0 个不报错）。

    ``skip_ref``：已填过词的搜索框 ref。回车没触发提交时，词表按
    观察原序会先命中**输入框自己**（placeholder「搜索片名」也含
    「搜索」）——再点输入框一遍没有意义，重试路径要的是真正的
    提交按钮（单测抓出的形态：点击记成了 e1 框而不是 e3 按钮）。
    """
    try:
        words = _SEARCHABLE_WORDS[vocabulary]
    except KeyError:
        raise ValueError(
            f"认不出的元素词汇表 {vocabulary!r}——搜索按钮词表无从确定。"
            f"已知的：{sorted(_SEARCHABLE_WORDS)}"
        ) from None
    for el in elements or ():
        if skip_ref and el.ref == skip_ref:
            continue
        name = (el.name or "").strip().lower()
        if not name or str(el.role or "").lower() not in words:
            continue
        if any(hint.lower() in name for hint in SEARCH_BUTTON_HINTS):
            return el
    return None


def _heuristic_results(
    obs: Observation, *, engine: str, limit: int
) -> list[search.Candidate]:
    """启发式全量提取结果链接（感知层 ``None`` 时的兜底，与路径 A 同口径）。

    与 :func:`~trajectory_pipeline.executor.steps.search.extract_candidates`
    的两点刻意差异（见模块 docstring 第 3 条）：

    - **按 href 去重**而不是按 host——站内结果页全是本站深层页；
    - **首页型照收**——结果页里的 path 为 ``/`` 的链接是站内导航，
      但那由结构剔除（``path_noise`` 等）与后续 visit 兜底，
      不在这里二次猜测。真正要剔的是引擎域 / 社交域 / 功能页路径
      （与直搜链同一张表）。
    """
    out: list[search.Candidate] = []
    seen_hrefs: set[str] = set()
    for link in obs.links:
        raw = search._unwrap_redirect(link.href)
        if raw in seen_hrefs:
            continue
        if search.exclusion_reason(raw, engine):
            continue
        seen_hrefs.add(raw)
        out.append(
            search.Candidate(
                url=raw,
                text=link.text,
                rank=len(out) + 1,
                source_href=link.href,
                origin="site_search",
                parent=obs.url,
            )
        )
        if len(out) >= limit:
            break
    return out


def _selected_candidates(
    selection_urls: list[str],
    obs: Observation,
    *,
    limit: int,
) -> tuple[list[search.Candidate], int]:
    """把选题点名的 URL 对到结果页链接上，构造子候选。

    返回 ``(子候选, 点名了但页面上找不到的条数)``。匹配同时试
    **原始 href** 与**解包后 URL**——与 orchestrator
    ``_apply_selection`` 的匹配口径同源：感知层读的是观察（原始
    href），这里少试一个键就会「整个结果集丢失」。
    """
    wanted: dict[str, str] = {}
    for url in selection_urls:
        url = url.strip()
        if url:
            wanted[url] = url
    out: list[search.Candidate] = []
    unmatched = len(wanted)
    for link in obs.links:
        raw = search._unwrap_redirect(link.href)
        if link.href in wanted or raw in wanted:
            key = link.href if link.href in wanted else raw
            wanted.pop(key)
            unmatched = len(wanted)
            out.append(
                search.Candidate(
                    url=raw,
                    text=link.text,
                    rank=len(out) + 1,
                    source_href=link.href,
                    origin="site_search",
                    parent=obs.url,
                )
            )
            if len(out) >= limit:
                break
    return out, unmatched


async def site_search(
    driver: PageDriver,
    perceptor: Perceptor,
    ledger: RunLedger,
    steplog: StepLog,
    candidate: search.Candidate,
    site_obs: Observation,
    title: str,
    *,
    engine: str = search.DEFAULT_ENGINE,
    max_chars: int | None = None,
    max_results: int = 10,
    stats: dict[str, int] | None = None,
) -> SiteSearchResult:
    """在**当前已到达的站点首页**上按片名做站内搜索。

    前置：``site_obs`` 是**当前活动 tab** 的首页观察（调用方已导航）；
    动作流记入所属站点单元的 ``steplog``。**本函数不抛异常**，
    任何失败折算成 ``unresolved`` + note。

    ``title`` 为空时直接 ``unresolved``：没有片名的站内搜索是
    「随便搜搜」，那不是本步骤的题——空片名由调用方在更早处拦截，
    这里再拦一次是防拼装错误（fail-closed 的最后一道）。
    """
    result = SiteSearchResult(status=UNRESOLVED)
    landed = site_obs.url or candidate.url

    def bump(reason: str) -> None:
        if stats is not None:
            stats[reason] = stats.get(reason, 0) + 1

    if not (title or "").strip():
        ledger.record(
            landed, "unresolved",
            "站内搜索收到空片名——不是站点结论，是调用方拼装错误",
            site_url=candidate.url,
        )
        result.note("title 为空，未执行搜索")
        bump("site_search_empty_title")
        return result

    # ── 找搜索框：判断点（语义层），词表交叉印证由感知层自理 ────
    box = perceptor.decide(Q.FIND_SITE_SEARCH, site_obs)
    if box.answer is False:
        # 真结论：这个站没有站内搜索框 → no_site_search（负样本）。
        ledger.record_from(landed, box, site_url=candidate.url)
        result.status = NO_BOX
        result.note(f"FIND_SITE_SEARCH=False: {box.evidence}")
        return result
    if box.answer is None:
        ledger.record_from(landed, box, site_url=candidate.url)
        result.note(f"FIND_SITE_SEARCH=None（fail-closed）: {box.evidence}")
        return result
    ref = str(box.payload.get("ref") or "")
    if not ref:
        # I6 违约：answer=True 却无 ref。不猜框在哪，交人工。
        ledger.record(
            landed, "unresolved",
            f"判定说有搜索框但未回传 ref（违反 I6）: {box.evidence}",
            decision=box, site_url=candidate.url,
        )
        result.note("answer=True 但无 ref——契约违规，fail-closed")
        return result

    # ── 填词 + 提交（动作流记录，控制输入仍是 ref）──────────────
    target = actions.target_of(ref, site_obs.interactive_elements)
    if target is None:
        result.note(f"ref={ref} 未在 interactive_elements 中找到（I6 存疑）")

    step_fill = steplog.act(actions.fill(target, title))
    try:
        await driver.fill(
            text=title,
            **actions.target_params(
                site_obs.element_vocabulary, target, ref=ref),
        )
    except (DriverError, actions.UnaddressableTarget) as exc:
        steplog.settle(step_fill, error=f"填词失败: {exc}")
        ledger.record(
            landed, "unresolved", f"站内搜索填词失败: {exc}",
            decision=box, site_url=candidate.url,
        )
        result.note(f"填词失败: {exc}")
        return result

    step_press = steplog.act(actions.press("Enter"))
    try:
        await driver.press("Enter")
    except DriverError as exc:
        steplog.settle(step_press, error=f"回车失败: {exc}")
        ledger.record(
            landed, "unresolved", f"站内搜索回车失败: {exc}",
            decision=box, site_url=candidate.url,
        )
        result.note(f"回车失败: {exc}")
        return result

    # ── 重新观察：判的是结果页，不是首页 ─────────────────────────
    results_obs = await _observe(driver, max_chars)
    if results_obs is None:
        steplog.settle(step_press, error="提交后取不到快照")
        ledger.record(
            landed, "unresolved",
            "站内搜索提交后取不到结果页快照",
            decision=box, site_url=candidate.url,
        )
        result.note("提交后取不到快照")
        return result
    steplog.settle(step_press, results_obs)

    if _same_page(site_obs, results_obs):
        # 页面没变：回车多半没触发提交。第二条路：点「搜索」按钮。
        results_obs = await _retry_via_button(
            driver, steplog, results_obs, result,
            max_chars=max_chars, skip_ref=ref,
        )
        if results_obs is None or _same_page(site_obs, results_obs):
            # 两条提交路径都失败 → 提交失败 ≠ 没结果，unresolved。
            if results_obs is not None:
                result.results_obs = results_obs
            ledger.record(
                landed, "unresolved",
                "站内搜索提交后页面未变化（回车与搜索按钮均未触发跳转）"
                "——提交失败不是站点结论",
                decision=box, site_url=candidate.url,
            )
            result.note("提交失败：页面未变化")
            bump("site_search_submit_failed")
            return result

    result.results_obs = results_obs
    results_url = results_obs.url or landed

    # ── 结果页选题：复用 ①（题意原样成立）──────────────────────
    selection = perceptor.decide(Q.SELECT_PLAY_SITES, results_obs)
    if selection.answer is True:
        urls = [
            str(r.get("url") or "").strip()
            for r in (selection.payload.get("selected") or [])
            if isinstance(r, dict)
        ]
        kept, unmatched = _selected_candidates(
            urls, results_obs, limit=max_results,
        )
        if unmatched:
            result.note(
                f"{unmatched} 个选中项在结果页 links 里找不到"
                f"（可能被采集 limit 截断），已丢弃"
            )
        return _finish(result, ledger, kept, title, results_url,
                       candidate, bump)
    if selection.answer is False:
        # 真结论：这个站的搜索结果里没有该片的可看页面。
        return _finish(result, ledger, [], title, results_url,
                       candidate, bump)

    # None：启发式全量（与路径 A 同口径，不中断采集）。
    result.note(f"结果页选题 fail-closed（{selection.evidence}），启发式全量")
    kept = _heuristic_results(results_obs, engine=engine, limit=max_results)
    return _finish(result, ledger, kept, title, results_url,
                   candidate, bump)


def _finish(
    result: SiteSearchResult,
    ledger: RunLedger,
    kept: list[search.Candidate],
    title: str,
    results_url: str,
    candidate: search.Candidate,
    bump,
) -> SiteSearchResult:
    """按子候选数量收尾记账。

    ``site_search_no_results`` / ``site_search_routed`` 都由代码选用
    （见 ``perception/questions.py`` 的 CODE_ONLY_BRANCHES 注释），
    记账**不带 question**——选题那步的 question 是 ``select.play_sites``，
    而这两支的归属是发现链（``site.find_site_search`` 之后的代码路径）。
    D-5 对它们的放行走 :data:`CODE_ONLY_BRANCHES` 免配对（与
    ``_drop_site_homepages`` 的 ``not_play_site`` 不同机制——那支是
    ① 的注册表负分支、不在 CODE_ONLY 集里，才需要 QUESTIONLESS_PAIRS
    按组合放行；这两支本就是代码判定分支，注册表里没有同名题绑定）。
    """
    if kept:
        result.status = ROUTED
        result.selected = kept
        ledger.record(
            results_url, "site_search_routed",
            f"站内搜索派发 {len(kept)} 个子候选（首个: {kept[0].url[:80]}）",
            site_url=candidate.url,
        )
        return result
    result.status = NO_RESULTS
    evidence = result_note_evidence(result) or "结果页未产出子候选"
    ledger.record(
        results_url, "site_search_no_results",
        f"站内搜索《{title}》未找到可看的资源页。{evidence}",
        site_url=candidate.url,
    )
    bump("site_search_no_results")
    return result


def result_note_evidence(result: SiteSearchResult) -> str:
    """从过程 notes 里取最后一条非噪音说明作 evidence 兜底。"""
    for note in reversed(result.notes):
        if "fail-closed" in note or "启发式全量" in note:
            return note
    return ""


async def _observe(
    driver: PageDriver, max_chars: int | None
) -> Observation | None:
    try:
        return await driver.observe(max_chars=max_chars)
    except DriverError:
        return None


async def _retry_via_button(
    driver: PageDriver,
    steplog: StepLog,
    obs: Observation,
    result: SiteSearchResult,
    *,
    max_chars: int | None = None,
    skip_ref: str = "",
) -> Observation | None:
    """回车未触发跳转时的第二条提交路径：点「搜索」按钮。

    找不到按钮（词表不中）直接返回原观察——调用方按「仍同页」
    处理为提交失败，不在这里编造「试过」的记录。
    """
    button = _find_search_button(
        obs.interactive_elements,
        vocabulary=obs.element_vocabulary,
        skip_ref=skip_ref,
    )
    if button is None:
        result.note("页面上找不到可点击的「搜索」按钮（词表未命中）")
        return obs
    result.note(f"回车未触发跳转，改点搜索按钮 ref={button.ref}")
    target = actions.target_of(button.ref, obs.interactive_elements)
    step_click = steplog.act(actions.click(target))
    try:
        await driver.click(**actions.target_params(
            obs.element_vocabulary, target, ref=button.ref))
    except (DriverError, actions.UnaddressableTarget) as exc:
        steplog.settle(step_click, error=f"点搜索按钮失败: {exc}")
        result.note(f"点搜索按钮失败: {exc}")
        return obs
    after = await _observe(driver, max_chars)
    if after is None:
        steplog.settle(step_click, error="点按钮后取不到快照")
        return obs
    steplog.settle(step_click, after)
    return after


__all__ = [
    "SiteSearchResult", "site_search", "SEARCH_BUTTON_HINTS",
    "is_site_homepage",
    "ROUTED", "NO_BOX", "NO_RESULTS", "UNRESOLVED",
]
