"""训练侧收尾文本——**面向用户的答复**，单一真源（方案 23 R2-1 / R2-2）。

P3 与 T 都从这里取句子。⚠️ **2026-11-03 · re-base 到 per-Run**：per-unit
时代两者的事实不同（P3 只知道自己这一站、T 带整条 run），per-Run 之后
**两边的事实一样**（都是整条 run 的站点清单 + 同一个决定性结论）⇒ 两边
说的是**同一句话**。

同一套模板 ⇒ 两边不会各写一句再靠测试盯同步。**没有分支枚举名、没有
分支计数**：「处置词表」是并池口径，不是助手该对用户说的话（审计 G3）。

⚠️ 失败答复里的站点只写 **host**（不带协议），原因见
:func:`_excluded_reason`——带 ``http`` 的失败答复会让「答复含 URL」这个
验收指标失去意义（用户要的是**可播放**网址，不是被排除站点的地址）。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Mapping, Sequence
from urllib.parse import urlsplit

#: 分支 → 面向用户的排除原因。**本模块自己声明**（不 import executor），
#: 与 ``executor.branches.BRANCH_LABELS`` 的键集同步由契约测试盯。
#: 字面量从 ``views._CLOSING_DISPOSITION`` / ``task_view._CLOSING_DISPOSITION``
#: 迁入（2026-11-02 R2），那两处保留同名别名指向这里。
CLOSING_DISPOSITION: dict[str, str] = {
    "not_play_site": "不是播放站点，排除",
    "unreachable_hard": "导航失败，排除",
    "no_play_control": "页面上没有播放控件，排除",
    "trailer_only": "只有预告片，没有正片资源，排除",
    "trailer_suspect": "疑似预告，判不准，不予记录",
    "component_unverified": "点开后没有可验证的播放组件，排除",
    "player_unverified": "有播放组件但确认不了能不能用，标记未验证，不入正式记录",
    "subscription_wall": "有播放页但需订阅/付费，排除",
    "unresolved": "无法确认能否播放，不予记录",
    "login_required": "登录后可免费观看（无登录态），排除",
    "blocked": "地区限制/验证码/反爬阻断，排除",
    "broken": "内容损坏，排除",
    "partial_free": "完整内容需付费，排除",
    # ── 路径 B/C 发现链（2026-10-16）────────────────────────────
    "not_aggregator": "候选不是影视聚合站，排除",
    "aggregator_no_entries": "聚合站页面上没有正式影视站入口，排除",
    "not_article": "候选不是文章/社区讨论页，排除",
    "article_no_links": "文章里没有可引荐的影视站外链，排除",
    "no_site_search": "首页上没有站内搜索框，无法按片名定位，排除",
    "site_search_no_results": "站内搜索没有找到该片的资源，排除",
    "site_search_routed": "经站内搜索找到候选，转入结果页继续核验",
    # deprecated 分支：老存档里仍有，新执行端不再产出。
    "login_wall_or_blocked": "登录墙/验证码/地区限制，排除",
}

#: 未知分支的兜底原因。**不写分支名**——R2-1 的判据是「训练集收尾里
#: 分支枚举名出现次数 = 0」；分支本身仍在 ``outcome`` / ``ledger`` 里可查。
UNKNOWN_REASON = "原因未能确认，排除"


@dataclass(frozen=True, slots=True)
class ClosingFacts:
    """收尾文本的输入事实。**只装事实，不做判断**。"""

    title: str = ""
    conclusion: str = ""
    #: 站点结论（P1 outcome 形状）：``url`` / ``site_url`` / ``play_page_url``
    #: / ``branch``。``branch is None`` = 成功。
    outcomes: tuple[Mapping[str, Any], ...] = ()
    #: 有动作、但一个站点都没访问到。
    no_sites: bool = False


def _quote(title: str) -> str:
    return f"《{title}》" if title else ""


def _site_label(url: str) -> str:
    """站点 → 给用户看的标签。只取 host，**不带协议**（见模块 docstring）。"""
    url = str(url or "").strip()
    if not url:
        return "该站点"
    parts = urlsplit(url if "://" in url else f"//{url}")
    return parts.hostname or url


def _reason(branch: Any) -> str:
    return CLOSING_DISPOSITION.get(str(branch or "")) or UNKNOWN_REASON


def _excluded_text(failed: Sequence[Mapping[str, Any]]) -> str:
    items: list[str] = []
    seen: set[tuple[str, str]] = set()
    for o in failed:
        label = _site_label(str(o.get("site_url") or o.get("url") or ""))
        reason = _reason(o.get("branch"))
        key = (label, reason)
        if key in seen:
            continue
        seen.add(key)
        items.append(f"{label}（{reason}）")
    return "、".join(items)


def _success_url(success: Mapping[str, Any]) -> str:
    return str(success.get("play_page_url") or success.get("url") or "").strip()


def closing_text(facts: ClosingFacts) -> str:
    """生成一条面向用户的收尾答复。**确定性、不编新事实**。

    句子结构（每条都以「。」结尾）：

    - 成功：``找到了《片名》的可播放网址：<url>。``（没记下网址时如实说）
    - 失败：``没有找到《片名》的可播放网址，已排除的站点：a.com（原因）、…。``
    - 有动作但没访问到站点：``没有找到《片名》的可访问站点。``
    - ``conclusion``（模型自己写的 reflection 结论）另起一句附后。

    ⚠️ **per-Run 之后没有「搜索单元」那种收尾**（2026-11-03 re-base）：
    搜索阶段的「接下来逐一核验」是**一个决策单元**的收尾，而决策单元已经
    不是样本的粒度了——那句话拿掉，否则整条消息流会以「接下来逐一核验」
    结束，而这条轨迹其实已经跑完了。
    """
    quote = _quote(facts.title)
    sentences: list[str] = []

    success = next(
        (o for o in facts.outcomes if isinstance(o, Mapping) and o.get("branch") is None),
        None,
    )
    failed = [
        o for o in facts.outcomes
        if isinstance(o, Mapping) and o.get("branch") is not None
    ]

    if success is not None:
        url = _success_url(success)
        if url:
            sentences.append(f"找到了{quote}的可播放网址：{url}")
        else:
            sentences.append(f"找到了{quote}的可播放资源，但没有记下网址")
        if failed:
            sentences.append("另外，已排除的站点：" + _excluded_text(failed))
    elif failed:
        sentences.append(
            f"没有找到{quote}的可播放网址，已排除的站点：{_excluded_text(failed)}"
            if quote else
            f"没有找到可播放的网址，已排除的站点：{_excluded_text(failed)}"
        )
    elif facts.no_sites:
        sentences.append(
            f"没有找到{quote}的可访问站点" if quote else "没有找到可访问的站点"
        )
    else:
        # 没有站点级结论（P1 ``outcome`` 缺失 / 全是路由记账）：结论是
        # 「没找到」，但**为什么没找到不知道**——照实说，不编原因。
        sentences.append(
            f"没有找到{quote}的可播放网址，原因未能确认"
            if quote else
            "没有找到可播放的网址，原因未能确认"
        )

    conclusion = str(facts.conclusion or "").strip().rstrip("。")
    if conclusion:
        sentences.append(conclusion)
    # 只去掉**句尾**那个句号再拼——结论内部可能本来就有句号，不能按
    # 最后一个句号截断（那会把「我先看了 A。然后看了 B」截成半句）。
    return "。".join(s[:-1] if s.endswith("。") else s for s in sentences) + "。"


def closing_for_unit(
    *,
    title: str,
    unit: str = "",
    outcome: Mapping[str, Any] | None = None,
    conclusion: str = "",
) -> str:
    """P2 的一个决策单元 → 收尾答复。

    ⚠️ **2026-11-03 · re-base 到 per-Run：这条入口已无调用方**（决策单元
    不再是样本的粒度，P3 与 T 的收尾都是**整条轨迹**的——见
    :func:`closing_for_run`）。保留它只为**读得懂** per-unit 时代的存档与
    脚本；新代码不许再调它——两个粒度的收尾混进同一批语料，模型会学到
    「有的轨迹收尾说『接下来核验』、有的说『没找到』」。

    ``unit == "search"`` ⇒ 搜索单元（没有站点结论，只交代下一步）。
    """
    facts = ClosingFacts(
        title=str(title or ""),
        conclusion=str(conclusion or ""),
        outcomes=(dict(outcome),) if isinstance(outcome, Mapping) else (),
    )
    if str(unit or "") == "search":
        return f"已找到{_quote(str(title or ''))}的候选站点，接下来逐一核验。"
    return closing_text(facts)


def run_closing_outcomes(
    *,
    decisive: Mapping[str, Any] | None,
    site_outcomes: Sequence[Mapping[str, Any] | None] = (),
) -> tuple[Mapping[str, Any], ...]:
    """收尾要用的 outcome 列表——**裁定 ②** 的落地。

    = **决定性结论**（``decisive``）+ 其余**失败**站点。

    两条规则各扣一句话：

    - **中途成功过不算成功**：裁定 ② 是「看最后一个结论」，所以收尾跟着
      **决定性结论**说话。站点列表里成功的那些**剔掉**——它们既不是
      「决定性结论」，也不是「被排除的站点」，混进去会让文本说「找到了」
      而任务级标签说「没找到」。
    - **失败站点要列出来**：那是「已经在哪些地方看过、都没成」——对用户
      有用，也让「没找到」不至于是一句空话。

    决定性结论本身是失败时会**出现两次**（一次是它自己，一次在站点列表
    里）——:func:`_excluded_text` 按 ``(host, 原因)`` 去重，渲染出来的
    文本里只有一条。**不额外判重**：判重要靠对象身份，而 P2 侧的决定性
    结论与站点结论是两个 ``OutcomeRef``（``succeeded`` 可能不同），按值
    比会误伤。

    ⚠️ **这个函数是 P2 / P3 / T 三侧共用的**——收尾文本三处都要一样，
    各写一遍就是三处会漂。
    """
    out: list[Mapping[str, Any]] = []
    if isinstance(decisive, Mapping):
        out.append(decisive)
    for o in site_outcomes:
        if not isinstance(o, Mapping):
            continue
        if o.get("branch") is None:
            continue
        out.append(o)
    return tuple(out)


def closing_for_run(
    *,
    title: str,
    outcomes: Sequence[Mapping[str, Any]] = (),
    conclusion: str = "",
    no_sites: bool = False,
) -> str:
    """一条**轨迹**（一个 Run）→ 收尾答复。

    ⚠️ **2026-11-03 · re-base 到 per-Run**：per-unit 时代 P3 的收尾是
    「一个单元的答复」，现在 P3 的收尾是**整条轨迹的答复**——所以 P3 与 T
    的输入事实**变得一样**（都是整条 run 的站点清单），两边读的是
    **同一句话**，不再是「同款模板、不同事实」。

    仍分两个入口，是因为**读者不同**：P3 渲染的是**这一条训练样本**的
    收尾（模型最后一句说什么），T 渲染的是**给人看**的任务级答复。
    事实一样 ⇒ 文本一样 ⇒ 两边不会各写一句再靠测试盯同步。

    ``outcomes``：收尾要用的 outcome 列表（**已按裁定 ② 滤过**，见
    :func:`run_closing_outcomes`）。每个元素是 outcome 形状
    （``url`` / ``site_url`` / ``play_page_url`` / ``branch``）；
    ``branch is None`` = 成功。

    ``no_sites``：有动作、但一个站点都没访问到（搜索阶段就被拦）。
    """
    facts = ClosingFacts(
        title=str(title or ""),
        conclusion=str(conclusion or ""),
        outcomes=tuple(dict(o) for o in outcomes if isinstance(o, Mapping)),
        no_sites=bool(no_sites),
    )
    return closing_text(facts)


#: 可交付网址：**带协议**的 http(s)。失败答复里的站点只写 host，
#: 所以那里不会命中——「含 URL」说的是「给了用户能点开的东西」。
_URL_RE = re.compile(r"https?://[^\s，。；、）)」』\"']+", re.IGNORECASE)


def count_urls(text: str) -> int:
    """收尾答复里有几个**可交付的网址**（``http(s)://``）。

    R2-3 判的是**最终回复文本**，不是 ``outcome`` 字段：字段里有网址
    ≠ 答复里说了。``output_contract.min_urls`` 要求几个就数几个。
    """
    return len(_URL_RE.findall(str(text or "")))


def has_url(text: str) -> bool:
    """收尾答复里**至少有一个**可交付网址（``count_urls(...) >= 1``）。"""
    return count_urls(text) > 0


__all__ = [
    "CLOSING_DISPOSITION",
    "UNKNOWN_REASON",
    "ClosingFacts",
    "closing_text",
    "closing_for_unit",
    "closing_for_run",
    "run_closing_outcomes",
    "count_urls",
    "has_url",
]