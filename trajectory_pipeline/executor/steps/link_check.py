"""外链校验——路径 B/C 发现链共用的「点过去之前先核一遍」。

聚合站页 / 文章页上的链接**不是终点**：它们可能是

    直接落地       点开就是目标站          → 候选 = 落地 URL
    重定向落地     点开被 302 到别处       → 候选 = 落地观测解析出的 URL
    跳转确认页     「即将离开本站，点击继续」→ 候选 = 从确认页正文提取的目标
    丢弃           打不开 / 提不出目标      → **不是候选，也不是负分支**

最后一条是本模块与 visit 链的关键差异：**外链不是站点结论**。一个外链
解析失败，只说明「这条引荐路走不通」，说不出「这个站看不了」——
把它记成负样本会把「引荐链接失效」伪装成「站点失败」，而负样本池
一旦混进这种样本，「为什么看不了」就再也答不准了。所以 ``dropped``
只进 :func:`resolve_referral` 的 ``stats`` 记账（与
:func:`~trajectory_pipeline.executor.steps.search.extract_candidates`
的 ``candidate_filter`` 同一套可见性纪律），不碰 ``RunLedger``。

跳转确认页的判定是**纯代码**（词表 + URL 正则），不过感知层：
「页面在说『即将跳转』」是文本事实不是语义判断，而确认页的典型
形态（短正文 + 提示语 + 目标地址）用词表抓比用 LLM 稳——LLM 在
这种**结构简单但变体多**的页面上反而容易过度解读。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlparse

from trajectory_pipeline.executor import actions
from trajectory_pipeline.executor.actions import StepLog
from trajectory_pipeline.executor.browser.page_driver import DriverError, PageDriver
from trajectory_pipeline.executor.steps import reachability, search
from trajectory_pipeline.perception.base import Observation

#: 确认页提示词表。命中**任意一条**即按确认页处理。
#:
#: 全部是「跳转提示」的固定句式，不是站点业务词——刻意不含「观看」
#: 「播放」这类内容词，它们会命中正常落地页的正文。
CONFIRM_PAGE_HINTS: tuple[str, ...] = (
    "即将跳转",
    "跳转至",
    "秒后自动跳转",
    "秒后跳转",
    "继续访问",
    "点击进入",
    "离开本站",
    "前往站点",
    "进入网站",
    "安全提示",
)

#: 确认页正文长度上限。真确认页是一句提示 + 一个目标地址，
#: 不会有两千字正文；超长的页即使命中词表也按**正常落地页**处理——
#: 长正文里出现「点击进入」多半是内容的一部分（ INC43 教训的词表
#: 通用坑：error.html 页脚命中付费墙词表的形态在这里同样存在）。
CONFIRM_PAGE_MAX_CHARS = 2000

#: 从正文提取目标 URL 的正则。截断集是中文标点 + 空白 + 引号 +
#: 尖括号（HTML 属性形态）——URL 里这些字符都不合法，出现在边界
#: 上就是终点。末尾的 ``[.,;:!?)]`` 再剥一层（句子里的逗号句号）。
#:
#: ⚠️ 全角标点必须**显式**列区间（单测抓出的真缺口）：``，`` 是
#: U+FF0C、``。`` 是 U+3002，都**不在** ``\u4e00-\u9fff`` 汉字区间里——
#: 不列的话「https://v.test/p/1。」会把句号一起吞进 URL，结构校验
#: 过了但地址是坏的。``\u3000-\u303f``（、。〈〉「」）与
#: ``\uff00-\uffef``（，！？（））覆盖中文正文里全部常见收尾形态。
_URL_IN_TEXT = re.compile(
    r"https?://[^\s\"'<>\u4e00-\u9fff\u3000-\u303f\uff00-\uffef]+"
)
_TRAILING_PUNCT = ".,;:!?)]}'\""

#: 解析结果的四种归宿。
DIRECT = "direct"
REDIRECTED = "redirected"
CONFIRM_PAGE = "confirm_page"
DROPPED = "dropped"


@dataclass(frozen=True, slots=True)
class ReferralResolution:
    """一条外链的解析结果。

    ``url`` 是**解析后的候选 URL**（direct/redirected 为落地观测的
    URL、confirm_page 为提取目标），``source_href`` 保留中间页观察里
    的原始链接——与 :class:`~trajectory_pipeline.executor.steps.search.Candidate`
    的 url/source_href 同一对字段、同一个理由（感知层读观察、遍历用
    解析后地址，两侧要对得上）。
    """

    status: str
    url: str
    source_href: str
    evidence: str
    note: str = ""

    @property
    def ok(self) -> bool:
        """是否产出了候选（dropped 为否）。"""
        return self.status != DROPPED and bool(self.url)

    def to_json(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "url": self.url,
            "source_href": self.source_href,
            "evidence": self.evidence,
            "note": self.note,
        }


def is_confirm_page(body_text: str) -> bool:
    """词表 + 长度双门槛的确认页判定（纯文本事实）。"""
    body = body_text or ""
    if len(body) > CONFIRM_PAGE_MAX_CHARS:
        return False
    return any(hint in body for hint in CONFIRM_PAGE_HINTS)


def _drop_reason(url: str, base_host: str, engine: str) -> str:
    """解析出的目标为什么不能当候选。空串 = 可以。

    与 :func:`~trajectory_pipeline.executor.steps.search.exclusion_reason`
    同源（同一套结构剔除：引擎域 / 社交域 / gov.cn / 功能页路径），
    外加一条它管不到的：**回到中间页自己**（``same_host``）——聚合站
    上的「站内分类页」点开还是聚合站，那不是一条新引荐。
    """
    raw = search._unwrap_redirect(url)
    if not raw.startswith(("http://", "https://")):
        return "non_http_scheme"
    try:
        host = (urlparse(raw).hostname or "").lower()
    except ValueError:
        return "unparseable"
    if not host:
        return "no_host"
    if base_host and host == base_host:
        return "same_host"
    reason = search.exclusion_reason(raw, engine)
    return reason


def _extract_redirect_target(
    body_text: str, *, base_host: str, engine: str
) -> str:
    """从确认页正文提取跳转目标。**第一个**通过结构校验的 URL。

    按出现顺序取第一个而不是「最像的」——「哪个 URL 是目标」的语义
    判断没有可靠依据，而确认页的目标地址几乎总是正文里唯一 / 首个
    完整 URL。提不出目标返回空串，由调用方落 ``dropped``。
    """
    for match in _URL_IN_TEXT.finditer(body_text or ""):
        candidate = match.group(0).rstrip(_TRAILING_PUNCT)
        if candidate and not _drop_reason(candidate, base_host, engine):
            return candidate
    return ""


def eligible_referral_hrefs(
    obs: Observation,
    *,
    base_host: str,
    engine: str = search.DEFAULT_ENGINE,
    limit: int = 5,
) -> list[str]:
    """启发式提取**结构性合法**的引荐外链（感知层 None 时的兜底）。

    与 :func:`~trajectory_pipeline.executor.steps.search.extract_candidates`
    同一条 fail-closed 纪律：感知层判不出（W1 规则版 / 后端不可用）
    时**不中断采集**，退回代码的结构性过滤——「哪些链接值得过外链
    校验」的语义判断留给下一次选题，链接本身的合法性（http(s)、
    非中间页自域、非引擎/社交/功能页）是结构事实，代码判就是对的。

    解包后的 URL 按 href 去重（同一条链接的包裹与裸形态只算一次），
    保持观察原序——顺序是页面的推荐权重，代码没有依据重排。
    """
    out: list[str] = []
    seen: set[str] = set()
    for link in obs.links or ():
        raw = search._unwrap_redirect(link.href)
        if not raw or raw in seen:
            continue
        if _drop_reason(raw, base_host, engine):
            continue
        seen.add(raw)
        out.append(raw)
        if len(out) >= limit:
            break
    return out


async def resolve_referral(
    driver: PageDriver,
    steplog: StepLog,
    link_href: str,
    *,
    base_host: str,
    engine: str = search.DEFAULT_ENGINE,
    max_chars: int | None = None,
    stats: dict[str, int] | None = None,
) -> ReferralResolution:
    """解析一条引荐外链：开 tab → 观察 → 判定 → 关 tab。

    动作（``new_tab`` / ``close_tab``）记入**所属中间页**的
    ``steplog``——外链校验是中间页单元的一部分，不是独立决策单元。
    两个动作都是 ``infrastructure``：模型不选它们。

    **本函数不抛异常**（与 :func:`~trajectory_pipeline.executor.steps.visit.visit_site`
    同一条批处理纪律）：任何失败都折算成 ``dropped`` + note。
    """

    def bump(reason: str) -> None:
        if stats is not None:
            stats[reason] = stats.get(reason, 0) + 1

    def _dropped(note: str, evidence: str = "") -> ReferralResolution:
        return ReferralResolution(
            status=DROPPED, url="", source_href=link_href,
            evidence=evidence, note=note,
        )

    # ── 开 tab：外链校验必须隔离会话 ─────────────────────────────
    # 中间页（聚合站页 / 文章页）后续还要继续提取其它外链，当前 tab
    # 不能被导航走——开不了新 tab 就只能放弃这条外链（fail-closed：
    # 采集不到落地 ≠ 负结论，dropped 而不是记账）。
    step_open = steplog.act(actions.new_tab(link_href))
    try:
        await driver.new_tab(link_href)
    except Exception as exc:
        steplog.settle(step_open, error=f"开 tab 失败: {exc}")
        bump("referral_new_tab_failed")
        return _dropped(f"开 tab 失败: {type(exc).__name__}: {exc}")

    # ── 观察：直接落地 / 重定向 / 确认页的分叉全在这份观察上 ──────
    try:
        obs: Observation = await driver.observe(max_chars=max_chars)
    except DriverError as exc:
        steplog.settle(step_open, error=f"外链观察失败: {exc}")
        await _close(driver, steplog)
        return _dropped(f"外链观察失败: {exc}")
    steplog.settle(step_open, obs)

    resolution = _classify(obs, link_href, base_host=base_host, engine=engine)
    if resolution.status == DROPPED:
        # 记账键与 extract_candidates 的 candidate_filter 同一风格：
        # 原因即键名，落进 P1 的 candidate_filter 段，批次可归因。
        bump(
            "confirm_page_no_target"
            if resolution.note == "confirm_page_no_target"
            else f"referral_dropped:{resolution.note}"
        )
    await _close(driver, steplog)
    return resolution


def _classify(
    obs: Observation,
    link_href: str,
    *,
    base_host: str,
    engine: str,
) -> ReferralResolution:
    """落地观察 → 解析结果。纯函数，不碰驱动。"""

    def dropped(note: str, evidence: str = "") -> ReferralResolution:
        return ReferralResolution(
            status=DROPPED, url="", source_href=link_href,
            evidence=evidence, note=note,
        )

    body = obs.body_text or ""

    # ── 分支一：跳转确认页 ────────────────────────────────────────
    if is_confirm_page(body):
        target = _extract_redirect_target(body, base_host=base_host, engine=engine)
        if not target:
            # 确认页但提不出目标：词表命中的页不一定是真确认页
            # （正常落地页命中词表的误伤面也走这里），**不猜**。
            # 记 confirm_page_no_target 供批次归因，这条外链到此为止。
            return dropped(
                "confirm_page_no_target",
                f"页面命中确认页词表但正文提取不到合法目标 URL"
                f"（url={obs.url[:80]}）",
            )
        return ReferralResolution(
            status=CONFIRM_PAGE, url=target, source_href=link_href,
            evidence=f"确认页提取目标: {target[:120]}",
            note=f"确认页落地 {obs.url[:80]}",
        )

    # ── 分支二：直接 / 重定向落地 ────────────────────────────────
    landed = obs.url or link_href
    reason = _drop_reason(landed, base_host, engine)
    if reason:
        return dropped(
            reason,
            f"外链落地被结构剔除（{reason}）: {landed[:120]}",
        )
    # 落地与原始链接是否同一地址：去尾斜杠 / fragment 再比
    # （reachability._normalize 的同一条口径），``/`` 与空 fragment
    # 不该算重定向。
    same = reachability._normalize(landed) == reachability._normalize(link_href)
    status = DIRECT if same else REDIRECTED
    note = "" if same else f"重定向: {link_href[:80]} → {landed[:80]}"
    return ReferralResolution(
        status=status, url=landed, source_href=link_href,
        evidence=f"外链落地: {landed[:120]}",
        note=note,
    )


async def _close(driver: PageDriver, steplog: StepLog) -> None:
    """关掉校验用 tab。失败只记动作流 error，不影响解析结果。"""
    step = steplog.act(actions.close_tab())
    try:
        await driver.close_tab()
    except Exception as exc:
        steplog.settle(step, error=f"关 tab 失败: {exc}")


__all__ = [
    "ReferralResolution", "resolve_referral", "is_confirm_page",
    "eligible_referral_hrefs",
    "CONFIRM_PAGE_HINTS", "CONFIRM_PAGE_MAX_CHARS",
    "DIRECT", "REDIRECTED", "CONFIRM_PAGE", "DROPPED",
]
