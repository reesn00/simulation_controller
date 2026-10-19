"""失败分支账本——**负样本池的唯一入池口**。

为什么单开一个模块：控制流里有十几条 ``rec(negative, "xxx")``，而「负样本池
少一支」是**静默失效**——报表上分部数字都在，就是有一支永远是 0，
没人知道是那支不产出还是那支没接。所以这里做两件事：

1. **入池时校验**：分支名不在注册表的 20 条之内 → 抛错，不落盘。
   新增分支忘了同步 ``perception/questions.py`` 会当场炸，而不是攒一批脏数据。
   已弃用分支（``questions.DEPRECATED_BRANCHES``）**单独放行**——历史路径
   仍可能产出合并态 ``login_wall_or_blocked``，不能把旧调用点当场炸掉。
2. **入池时必带证据**：``Decision.evidence`` 是负样本的**唯一**解释。
   一条没有 evidence 的负样本，在人工复核时无法与「代码 bug 导致的失败」区分，
   等于把排查成本转嫁给标注员。

另外记账四支「不进负样本池」的分支与真负样本**分开统计**（I4 的直接后果）：
``unresolved`` / ``player_unverified`` / ``trailer_suspect`` 都不是「这里看不了」，
混进负样本会污染负样本纯度——它会让负样本纯度这个指标失去意义，
而指标失效是察觉不到的。三支之间也不合并：成因不同，要修的东西就不同，
见 :data:`NON_SAMPLE_BRANCHES` 的说明。第四支 ``site_search_routed``
（2026-10-16 路径 B/C）是另一回事：路由**记账**不是结论，见该常量说明。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping

from trajectory_pipeline.perception import questions
from trajectory_pipeline.perception.base import Decision, Observation
# 2026-10-15 INC43：付费墙强弱信号表移到感知层（与 PAYWALL_WORDS 同源），
# 这里反向导入再导出——executor → perception 单向依赖不破，且旧引用
# （``branches.STRONG_PAYWALL_KEYWORDS`` 等）继续解析。
from trajectory_pipeline.perception.rule_perceptor import (
    PRICE_PATTERN,
    STRONG_PAYWALL_KEYWORDS,
    WEAK_PAYWALL_KEYWORDS,
    has_strong_paywall_signal,
)
# 2026-10-16 方案 12 阶段 G：``_same_page``（同页守卫的三特征比对）所有权
# 迁入 executor.gatekeeper——它与死循环检测同属「同页/同域机械事实」这一类
# 判定，统一拦截点收在那里。这里反向导入再导出（与上方 INC43 同款迁移
# 姿势）：本模块 ``_precondition_failed`` 与 site_search / test_branches 的
# 旧引用继续解析，与 d4_scorer 的同源对照测试不受影响（对照的是行为，
# 不是实现位置）。
from trajectory_pipeline.executor.gatekeeper import _same_page  # noqa: F401

#: 分支 → 人读标签。落进 P1 存档，Label Studio 与报表直接用，
#: 不在下游做二次翻译（翻译一次就多一次走样的机会）。
#:
#: 2026-10-12 阶段 1：登录墙合并态拆为 ``login_required`` / ``blocked``
#: （+ ``broken`` 承接 404）。原 ``login_wall_or_blocked`` **保留但弃用**
#: ——历史存档要展示，故不删；新代码路径不再主动产出它。全部合法值
#: （13 活跃 + 1 deprecated + success）= 15 类。
#:
#: 2026-10-16 路径 B/C：+7 支发现链分支（聚合站两支、文章链两支、
#: 站内搜索三支），活跃 13 → 20，全部合法值 = 22 类。
BRANCH_LABELS: Mapping[str, str] = {
    "not_play_site": "不是播放站点",
    "unreachable_hard": "导航失败（网络/DNS/超时）",
    "login_wall_or_blocked": "登录墙 / 验证码 / 地区限制（deprecated：已拆分为细分）",
    "no_play_control": "页面上没有播放控件",
    "trailer_only": "只有预告片，播放资源不存在",
    "trailer_suspect": "疑似预告（词表判不准，待人工）",
    "component_unverified": "点开后没有可验证的播放组件",
    "player_unverified": "有播放组件，但确认不了能不能用",
    "subscription_wall": "Subscription-based playback（有播放页，但需订阅/付费）",
    "unresolved": "感知层未给出结论（fail-closed）",
    "login_required": "登录后可免费观看（agent 无登录态）",
    "blocked": "地区限制 / 验证码 / 反爬环境阻断",
    "broken": "内容损坏（404 / 媒体不可解码 / duration=0）",
    "partial_free": "有免费部分（试看/预告），完整内容需付费",
    # ── 路径 B/C 发现链（2026-10-16）────────────────────────────
    # 前两支是聚合站链（①B 选站 + ⑤ 确认 / ⑥ 选入口），中间两支是
    # 文章链（①C 选文 / ⑥C 选外链），后三支是站内搜索链。
    # 见 .trae/documents/路径BC发现链实施计划.md。
    "not_aggregator": "候选不是影视聚合站 / 导航站",
    "aggregator_no_entries": "聚合站页面上没有正式影视站入口",
    "not_article": "候选不是文章 / 社区讨论页",
    "article_no_links": "文章页没有可引荐的影视站外链",
    "no_site_search": "首页型候选没有站内搜索框",
    "site_search_no_results": "站内搜索未产出任何子候选",
    "site_search_routed": "首页经站内搜索派发子候选（路由记账，非结论）",
}

#: 这些分支**不是**真负样本，是「没判出来」。单独统计，不进负样本池。
#:
#: 三支的成因**互不相同**，混在一支里报表就分不出该修什么：
#:   ``unresolved``         感知层没给出结论（环节①–④，连播放页都没到）
#:   ``player_unverified`` 环节⑦ 判不出来，**但控件点了、播放页就在眼前**
#:                         ——不确定的是「组件能不能用」，是页面证据不足
#:   ``trailer_suspect``    词表判不准，需要语义判断
#:
#: ``site_search_routed``（2026-10-16 路径 B/C）是第四支，成因与上面
#: 三支都不同：它**不是「没判出来」**，站内搜索成功派发了子候选，
#: 后续结论由子候选自己的 visit 记。它记录的是**路由去向**——
#: 「这个首页型候选的处理走了站内搜索这条路」。混进负样本会把
#: 「成功进入处理流程」记成「这里看不了」。
NON_SAMPLE_BRANCHES = frozenset(
    {
        "unresolved",
        "trailer_suspect",
        "player_unverified",
        "site_search_routed",
    }
)


class UnknownBranch(KeyError):
    """分支名不在注册表内。控制流写错就当场炸，不静默落盘。"""


@dataclass(frozen=True, slots=True)
class SiteOutcome:
    """一个站点的最终归宿。

    ``branch`` 只在负向时非空；正向成功记 ``branch=None``。

    **三个 URL 字段各管一层，不合并**——这是 rubric v1.1 D-6
    「站点 URL / 播放页 URL」两条必填能履约的前提：

        :attr:`site_url`      候选链接（搜索结果给的地址）。**恒有值**
        :attr:`url`           **结论形成处**的地址。
                              ⚠️ 它的值**取决于控制流走到哪一步**：
                              导航失败时是候选 URL、找控件失败时是
                              ``landed_url``、到播放页时是 ``player_obs.url``。
                              合并成一个字段时，读档的人必须回查控制流
                              才知道它指哪一层。
        :attr:`play_page_url` 播放页地址。**未走到播放页时为空**，
                              此时 D-6 的「播放页 URL」不适用、不扣分。
    """

    url: str
    branch: str | None
    evidence: str
    question: str = ""
    source: str = "rule"
    fallback_used: bool = False
    reached: bool = False  # 是否走到了播放页
    site_url: str = ""
    play_page_url: str = ""

    #: 2026-10-12 增量二十：LLM 在 ``page_quotes`` 字段单独回传的"页面
    #: 原文短语"列表（与 ``evidence`` 自述**严格分开**——后者是模型推断）。
    #:
    #: **来源**（按现状）：
    #:
    #: - LLM 版：``LLMPerceptor._is_reachable`` / ``_player_ok`` 把
    #:   ``_quote_list(data)`` 放进 ``Decision.payload["page_quotes"]``；
    #:   :meth:`RunLedger.record` / :meth:`RunLedger.record_from` 在
    #:   落 :class:`SiteOutcome` 时把它**提升**到独立字段，避免被
    #:   payload MappingProxyType 吞掉。
    #: - 规则版：W1 规则版无 LLM 原文短语，**永为空**——但保留字段以让
    #:   落盘 schema 不分 perceptor 类型，人工复核 / 报表 / 重跑都能
    #:   同一份 JSON 看。
    #:
    #: **JSON 形态**：落盘时是 list（与 LLM 的约定一致），反序列化时
    #: 仍为 list。空也写出 ``[]``——读档侧拿不到"未填"和"填了空数组"
    #: 的区别时，行为与现状一致（落空就当空）。
    #:
    #: **元组而非列表**：``SiteOutcome`` 是 ``frozen=True, slots=True``，
    #: 元组不可变，与 dataclass 不变式一致。JSON 序列化时 ``list(...)``。
    page_quotes: tuple[str, ...] = ()

    @property
    def is_negative_sample(self) -> bool:
        return self.branch is not None and self.branch not in NON_SAMPLE_BRANCHES

    def to_json(self) -> dict[str, Any]:
        return {
            "url": self.url,
            "site_url": self.site_url,
            "play_page_url": self.play_page_url,
            "branch": self.branch,
            "branch_label": BRANCH_LABELS.get(self.branch or "", "成功"),
            "evidence": self.evidence,
            # 2026-10-12 增量二十：``page_quotes`` 落盘。空时写 ``[]``
            # 而不是省略——读档侧拿不到"未填"和"填了空数组"的区别时，
            # 行为与现状一致；显式空数组让单测断言更直接（不需要
            # ``assert "page_quotes" in j or j["page_quotes"] == []``）。
            "page_quotes": list(self.page_quotes),
            "question": self.question,
            "source": self.source,
            "fallback_used": self.fallback_used,
            "reached_play_page": self.reached,
            "is_negative_sample": self.is_negative_sample,
        }


@dataclass
class RunLedger:
    """一次运行（一个 task）的分支记账。

    刻意**不做并发安全**：W1 控制流是串行的（每站点一个 tab，顺序遍历）。
    真要并行时再上锁——现在上锁是给不存在的问题写代码。
    """

    task_id: str
    outcomes: list[SiteOutcome] = field(default_factory=list)
    #: 未经感知层判定的站点数（候选来自代码层启发式，不是 LLM 选的）。
    #: 这个数字单独报出来，是为了不让「W1 跑出的候选」被误读成
    #: 「规则版能选出正确站点」——它不能。
    heuristic_candidates: int = 0

    # ── 记一笔 ──────────────────────────────────────────────────────

    def record(
        self,
        url: str,
        branch: str | None,
        evidence: str,
        *,
        decision: Decision | None = None,
        reached: bool = False,
        site_url: str = "",
        play_page_url: str = "",
        # 2026-10-12 增量二十：``page_quotes`` 显式传参时优先用显式值；
        # 否则从 ``decision.payload["page_quotes"]`` 提升（LLM 路径）。
        # 显式空元组 ``()`` 等价于"不要 page_quotes"——绝大多数调用点
        # 走默认 ``()`` 走 Decision 透传。
        page_quotes: tuple[str, ...] | None = None,
    ) -> SiteOutcome:
        """记一个站点结果并返回它。分支名非法时抛 :class:`UnknownBranch`。"""
        if (
            branch is not None
            and branch not in questions.all_branches()
            # 已弃用分支仍在历史路径产出（visit.py ② 不传 payload 时落
            # 合并态 login_wall_or_blocked），**放行**而不是抛错。
            and branch not in questions.DEPRECATED_BRANCHES
        ):
            raise UnknownBranch(
                f"未知失败分支 {branch!r}；注册表 "
                f"{len(questions.all_branches())} 条为 "
                f"{sorted(questions.all_branches())}"
            )
        # 2026-10-12 增量二十：``page_quotes`` 解析——见 :attr:`SiteOutcome.page_quotes`。
        # 三层兜底（显式 > decision.payload > 空），与其它字段（question /
        # source 等）同源路径保持一致。**归一为元组**以贴合 frozen dataclass。
        resolved_quotes = _resolve_page_quotes(page_quotes, decision)
        outcome = SiteOutcome(
            url=url,
            branch=branch,
            evidence=evidence,
            question=decision.question if decision else "",
            source=decision.source if decision else "rule",
            fallback_used=decision.fallback_used if decision else False,
            reached=reached,
            site_url=site_url,
            play_page_url=play_page_url,
            page_quotes=resolved_quotes,
        )
        self.outcomes.append(outcome)
        return outcome

    def record_from(
        self,
        url: str,
        decision: Decision,
        *,
        reached: bool = False,
        obs: Observation | None = None,
        pre_obs: Observation | None = None,
        site_url: str = "",
        play_page_url: str = "",
    ) -> SiteOutcome:
        """按 Decision 记账：``answer is None`` → ``unresolved``，
        否则按该题绑定的负分支记。

        这条映射写在 :func:`decision_branch` 里而不是散在控制流，
        是为了让「判 false 归哪支」只有一处定义。

        ## ``obs`` / ``pre_obs`` 存在的理由：负分支的**前置事实**要核
        分支名是**结论的标签**，而每条负分支的定义里都带着一个前置事实
        ——``component_unverified`` 的前提是「**到了播放页**，且播放页上
        没有可验证的播放组件」（2026-10-10 rubric 口径裁定：按决策树
        第四行「播放页无 video/组件 → 排除」理解，不按「有 video 标签
        但播放器未正常加载」）。``pre_obs`` 是点击**前**的站点页观察，
        ``obs`` 是点击后的观察——守卫核的是「点击到底有没有把页面
        带进播放页」。

        ⚠️ 判据刻意**不是** ``video_tag_count``。真实视频站普遍全是
        iframe/JS 播放器，真播放页上 ``video=0`` 是常态——拿它当
        「不在播放页」的信号，会把「到了播放页确实没组件」的合法排除
        一起杀掉。

        实测（2026-10-09 www.mgtvtv.com/tv/32080/）：点击前后两次观察的
        正文与元素数**完全相同**（390 字 / 37 元素 / video=0），说明点击
        根本没把页面带进播放页；模型判 False 的原话是「仍显示影片详情和
        『立即播放』按钮，并非实际播放页」。映射只看「模型说了 False」，
        于是贴上 ``component_unverified``——而它有片、有播放按钮，
        只是我们没点进去。

        负样本池的全部价值是「这里真的看不了」。贴错标签不是噪声小一点，
        是**把可看的站写成看不了的**——而这类样本一旦进池，
        人工也未必再看得出它是错的（理由写得通顺、结论看着专业）。

        所以：**前置事实不成立就降级为 ``unresolved``**，交人工，
        而不是硬贴一条负分支。``obs`` / ``pre_obs`` 为 ``None``
        （调用方没传）一律放行——守卫不反向把老调用方的记账打成
        unresolved，那种「忘了传」的错误由本 docstring 提醒，
        不在守卫里做第二次猜测。

        ⚠️ **环节⑦ 的 ``answer=None`` 不用本方法**，走
        :meth:`record_unresolved_reached`。那里的 ``None`` 说的是
        「组件能不能用」判不出来，与这里的「能不能判」不是一回事。
        """
        if decision.answer is None:
            branch = "unresolved"
        elif decision.answer is True:
            # **成功不是失败分支**（``decision_branch`` 的同一条口径）。
            # 原先这里落到 ``negative_branch_of(question)``，
            # 于是 ``answer=True`` 会把一条成功记成 ``component_unverified``——
            # 而没有测试断言 branch，那条错分支一直没被发现
            # （``test_决策来源透传`` 只断言 source/fallback_used）。
            branch = None
        else:
            # **走 decision_branch 而不是自己推导**——同一道题要能落不同分支
            # （④ 的「点了没组件」与「卡在付费」都由 PLAYER_OK 问出），
            # 两处各推一次的话，payload 里那个标记就只在一半路径上生效。
            # D-5 的映射表与注册表不同源已经吃过一次亏（避坑指南 §1.1）。
            branch = decision_branch(decision)
            reason = _precondition_failed(branch, obs, pre_obs, decision)
            if reason:
                return self.record(
                    url,
                    "unresolved",
                    f"{decision.evidence}\n"
                    f"[记账降级] 本该记 {branch}，但{reason}——"
                    f"该分支的前置事实不成立，不贴这条负标签",
                    decision=decision,
                    reached=reached,
                    site_url=site_url,
                    play_page_url=play_page_url,
                )
        return self.record(
            url,
            branch,
            decision.evidence,
            decision=decision,
            reached=reached,
            site_url=site_url,
            play_page_url=play_page_url,
        )

    def record_unresolved_reached(
        self,
        url: str,
        decision: Decision,
        *,
        site_url: str = "",
        play_page_url: str = "",
    ) -> SiteOutcome:
        """环节⑦：判不出来**且已站在播放页上**时记账，落 ``player_unverified``。

        为什么必须与 :meth:`record_from` 的 ``unresolved`` 分开：
        两者都不是负样本，但**责任方不同**——

            环节①–④ 的 None   连播放页都没到。不确定的是「能不能到」，
                              是代码能力不足（``unresolved``）
            环节⑦ 的 None     控件点了、快照拿到了、播放页就在眼前，
                              只是判不了能不能播。不确定的是「组件能不能用」，
                              是页面证据不足（``player_unverified``）

        混在一支里的代价是**复核队列失去分工**：队列只收这两支，
        而「请帮我看看这个页面能不能播」与「这个站就是这种情况」是两种待办，
        前者要人补判据，后者要人看页面。

        这条改动的**净影响很小**，别当成大重构：
        ``player_unverified`` 既不是负样本又已在 ``REVIEW_BRANCHES`` 的
        来源支内，所以 ``negative.jsonl`` 变、复核队列条数不变，
        只是标签从「感知层未给出结论」变成更准的描述。

        ⚠️ 本方法**只接受 ``answer=None``**。传了 True/False 进来会记错分支，
        所以当场抛——这是内部调用点，调用方拼错应该炸，
        而不是安静地产生一条错误标注。
        """
        if decision.answer is not None:
            raise ValueError(
                f"record_unresolved_reached 只接受 answer=None，收到 "
                f"{decision.answer!r}（question={decision.question}）"
            )
        return self.record(
            url,
            "player_unverified",
            decision.evidence,
            decision=decision,
            reached=True,
            site_url=site_url,
            play_page_url=play_page_url,
        )

    # ── 报表 ────────────────────────────────────────────────────────

    @property
    def succeeded(self) -> int:
        return sum(1 for o in self.outcomes if o.branch is None)

    def by_branch(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for o in self.outcomes:
            if o.branch:
                counts[o.branch] = counts.get(o.branch, 0) + 1
        return counts

    def negative_samples(self) -> list[SiteOutcome]:
        return [o for o in self.outcomes if o.is_negative_sample]

    def summary(self) -> dict[str, Any]:
        """覆盖率报表。

        ``missing_branches`` 是给**人**看的：二十条里有哪条这一批一条都没产出。
        它比「各分支数量」更能说明问题——数量为 0 可能只是样本少，
        而 missing 会直接指向「这条路径没接上」。
        已弃用分支（``questions.DEPRECATED_BRANCHES``）**不在** missing 里：
        它不再要求有产出，miss 会说成「没接上」，而那是设计好的。

        ⚠️ **跨批次比较分支分布前先看批次边界**：
        ``player_unverified`` 是 2026-10-09 新增的分支，历史存档里的
        等价情形仍记作 ``unresolved``（不回填，理由见 :mod:`executor.archive`
        「并池是写入时的动作，不是读取时的推导」）。于是边界之前
        ``unresolved`` 高、之后 ``player_unverified`` 高，
        不记批次就会读成「fail-closed 修好了」，而真相只是换了分类。
        """
        counts = self.by_branch()
        seen = {o.branch for o in self.outcomes if o.branch}
        return {
            "task_id": self.task_id,
            "total_sites": len(self.outcomes),
            "succeeded": self.succeeded,
            "negative_samples": len(self.negative_samples()),
            "by_branch": counts,
            "missing_branches": sorted(questions.all_branches() - seen),
            "heuristic_candidates": self.heuristic_candidates,
        }


#: 负分支的**前置事实**：贴这条标签之前，页面状态必须真的满足它的前提。
#:
#: ``component_unverified`` 的前提是「**到了播放页**，且页面上没有可验证的
#: 播放组件」（rubric 决策树第四行，2026-10-10 口径裁定）。点击前后
#: 同一个页面时，前提里「到了播放页」那半句不成立——这次点击没有产生
#: 新证据，结论是「语法通顺、语义空洞」的，而空洞的负标签比没有标签坏得多：
#: 它进了池，看着像一条正经的失败样本，人也未必再看得出它是空的。
#:
#: 值是「不成立时的说明」，用在 :meth:`RunLedger.record_from` 的降级路径里。
#: **没有前置事实的分支不进这张表**——表要短，
#: 长得跟分支表一样长就没人维护了。
BRANCH_PRECONDITION: Mapping[str, str] = {
    "component_unverified": (
        "「播放页上没有可验证的播放组件」说的是**到了播放页**之后没有组件，"
        "而这次点击没有把页面带进播放页，结论没有新证据支撑"
    ),
    "subscription_wall": (
        "「有播放页但需订阅/付费」同样要求**确实到了播放页**——"
        "站点页上的「开通会员」是导航栏文案，不是付费墙。"
        "这次点击没把页面带进播放页，就没有新证据支撑这条结论"
    ),
    # 2026-10-12 阶段 1：② 拆分的 login_required / blocked 与 ④ 拆分的
    # partial_free 同样要「确实到了目标页/播放页」，否则导航栏文案会被
    # 当成墙（与 subscription_wall 同款坑，风险 5 明写必须复用同页守卫）。
    "login_required": (
        "「登录后可免费观看」要求**确实到了目标页**——站点页导航栏里的"
        "「请登录」不是登录墙。这次点击没有产生新证据，"
        "就没有依据贴这条标签"
    ),
    "blocked": (
        "「地区限制/验证码/反爬」同样要求**确实到了目标页**——"
        "站点入口页的弹窗不是环境阻断。这次点击没有产生新证据，"
        "就没有依据贴这条标签"
    ),
    "partial_free": (
        "「有免费部分需付费看完整」同样要求**确实到了播放页**——"
        "站点页上的「会员抢先看」是宣传文案，不是试看付费墙。"
        "**纯续费营销 CTA / 会员到期提醒**是付费墙挽留营销，"
        "不是「试看 N 分钟 / 前 N 集免费」形态——那种走 subscription_wall。"
        "这次点击没把页面带进播放页，就没有新证据支撑这条结论"
    ),
    # ``broken`` 是**条件式**，不进同页守卫集合：404 形态无需「到了
    # 播放页」前置（URL 本身就是证据，页面即 404）；媒体损坏形态需要，
    # 但阶段 1 由 media_signal 暴露给人工复核，不经 decision_branch
    # 自动产出，这里只登记口径说明供复核参考。
    "broken": (
        "「内容损坏」分两种：404 形态无需播放页前置（URL 即证据）；"
        "媒体不可解码/duration=0 形态需要，由 media_signal 暴露、"
        "人工复核核验"
    ),
}


# 2026-10-13 增量三十二 · D10 偏差修缮：付费墙同页守卫的**强信号旁路**。
#
# 站点入口页导航栏里的「开通会员」广告是同页守卫要拦的「假付费墙」
# ——LLM 只看到「需付费」一个弱词就贴 ``subscription_wall``，守卫拦下、
# 降级 ``unresolved`` 是对的（见 ``test_付费墙前置事实不成立时降级``）。
#
# 但**真付费墙页面**点击前后就是同一个页面（页面本身就是付费墙终点站，
# 点击触发的是付费提示，不是导航）。LLM 在决策证据里写出**强付费墙**
# 关键词（VIP / 订阅 / 续费 / 包月 / 包年 / 专享 / 特惠）并至少配一个
# 弱关键词（开通 / 会员）或价格/优惠模式，就是「LLM 已经看穿了页面
# 内容」的强证据——这时同页守卫不该再把 ``subscription_wall`` 降级为
# ``unresolved``，否则 D10 偏差修缮白做。
#
# 强弱分组与三判据（≥1强+≥1弱 / ≥2强 / ≥1强+价格）的**唯一所有者**在
# :mod:`trajectory_pipeline.perception.rule_perceptor`
# （2026-10-15 INC43 移入，与 ``PAYWALL_WORDS`` 同源；本模块文件头反向
# 导入再导出，旧引用继续解析）。同页守卫旁路与 ②③ 负分支付费墙救济
# （``paywall_rescue_evidence``）是同一套信号的两个用途，两处各留一份
# 会漂移成「守卫放行了、救济不认」。
#
# 旁路**只对** ``subscription_wall`` 生效。``partial_free`` /
# ``login_required`` / ``blocked`` 同页守卫照旧——partial_free 在
# 阶段 1 没出现 D10 偏差，扩张旁路范围只会带来新的误判风险。
def _has_strong_paywall_signal(decision: Decision | None) -> bool:
    """LLM 决策证据里的付费墙信号是否**足够强**到让同页守卫放行。

    实现（含全部关键词表）在
    :func:`trajectory_pipeline.perception.rule_perceptor.has_strong_paywall_signal`
    ——这里只做 Decision → evidence 文本的适配。

    ``decision is None``（调用方没传）或 evidence 为空串都按弱信号
    处理——不旁路。
    """
    if decision is None:
        return False
    return has_strong_paywall_signal(decision.evidence or "")


# ``_same_page`` 已迁入 executor/gatekeeper.py（方案 12 阶段 G，
# 见文件头再导出说明）。以下为该迁移前的原 docstring 要点存档：
# rubric D-4 边界规则的可机械形态，与 evaluation.scorers.d4_scorer 的
# 同名判定同源同规则（url / 元素数 / 正文三特征）但不 import，
# 两处漂移由 test_branches 的对照测试钉住。


def _resolve_page_quotes(
    explicit: tuple[str, ...] | None,
    decision: Decision | None,
) -> tuple[str, ...]:
    """page_quotes 三层兜底解析（2026-10-12 增量二十）。
    优先级：显式参数 > decision.payload[page_quotes] > 空元组。
    不做 evidence 反推——那是独立增量，不要污染 P4a 边界。
    """
    if explicit is not None:
        return _coerce_quotes(explicit)
    if decision is not None:
        raw = decision.payload.get("page_quotes") if decision.payload else None
        if raw is not None:
            return _coerce_quotes(raw)
    return ()

def _coerce_quotes(raw: Any) -> tuple[str, ...]:
    """把 page_quotes 的任意可迭代输入归一成 tuple[str, ...]。
    边界：None / 非可迭代 -> ()；元素非字符串 -> 丢掉；空串 -> 丢掉。
    与 LLMPerceptor._quote_list 同款宁少勿错过滤。
    """
    if raw is None:
        return ()
    if isinstance(raw, (list, tuple)):
        return tuple(s for s in (str(x) for x in raw) if s)
    try:
        return tuple(s for s in (str(x) for x in raw) if s)
    except TypeError:
        return ()



def _precondition_failed(
    branch: str | None, obs: Observation | None, pre_obs: Observation | None = None,
    decision: Decision | None = None,
) -> str:
    """这条分支的前置事实成立吗？不成立返回说明串，成立返回空串。

    ``obs`` / ``pre_obs`` 为 ``None``（调用方没传）一律放行——**这是有意的**：
    降级逻辑不能反过来把老调用方的记账打成 unresolved，
    而那种「忘了传 obs」的错误由 :meth:`RunLedger.record_from` 的
    形参文档提醒，不在这里做第二次猜测。

    2026-10-13 增量三十二：D10 偏差修缮——``subscription_wall`` 在 LLM
    给出强付费墙信号时（见 :func:`_has_strong_paywall_signal`）旁路
    同页守卫。真付费墙页面点击前后就是同一个页面（页面本身就是终点站）。
    ``partial_free`` / ``login_required`` / ``blocked`` 同页守卫照旧。
    """
    if obs is None or branch is None:
        return ""
    if branch in (
        "component_unverified",
        "subscription_wall",
        "login_required",
        "blocked",
        "partial_free",
    ):
        if pre_obs is None:
            return ""
        if _same_page(pre_obs, obs):
            # 2026-10-13 增量三十二：subscription_wall 真付费墙信号旁路
            # 2026-10-15 INC44：旁路判据增加**确定性代码层信号**——
            # ``paywall_word``（``_player_ok`` 短语命中路径留下的词表
            # 标记）且 **body 过强弱信号组合**（:func:`has_strong_paywall_signal`，
            # 与 LLM evidence 旁路同款强度）。
            #
            # ⚠️ 强度闸不可省：短语命中路径对弱短语（「开通会员」）也设
            # ``paywall_word``——那是增量三十二要拦的 nav-bar CTA 形态
            # （mgtv 实测：详情页 + 开通会员广告 + LLM 判 False「仍在
            # 详情页」）。只认标记不认强度，会把 INC32 修掉的误判放回来。
            # 弱短语 + 同页 → 照旧降级 unresolved。
            #
            # （INC44 曾同时把 ②③ 词表救济扩到 ④ 让此通道常开——e2e
            # 零收益 + error.html 假阳性后 ④ 救济已回滚；本通道保留，
            # 只服务短语命中路径，纪律不变。）
            if branch == "subscription_wall" and (
                _has_strong_paywall_signal(decision)
                or (
                    decision is not None
                    and bool(decision.payload.get("paywall_word"))
                    and has_strong_paywall_signal(obs.body_text or "")
                )
            ):
                return ""
            return (
                f"点击播放控件前后是同一个页面"
                f"（url={obs.url}，元素数 / 正文也全同）——"
                f"{BRANCH_PRECONDITION[branch]}"
            )
    return ""


def decision_branch(decision: Decision) -> str:
    """Decision → 分支名。``None`` → ``unresolved``，否则取题绑定的负分支。

    ``answer=True`` 返回空串——成功不是失败分支。

    2026-10-12 阶段 1 起，**payload 细分优先于题绑定**（设计文档 §3.3）：

    - ② ``access_control_type``：``login`` → ``login_required``，
      ``region``/``captcha``/``anti_bot`` → ``blocked``，
      ``not_found`` → ``broken``；
    - ④ ``paywall_type``：``preview``/``trial`` → ``partial_free``，
      其余仍由 ``paywall`` 大类落在 ``subscription_wall``；
    - payload 均缺失时**保持题绑定的合并态**（``login_wall_or_blocked`` /
      ``component_unverified``）——visit.py ② 老路径不传 payload，
      该行为不能因为阶段 1 而改变（deprecated 分支由 ``record()`` 放行）。
    """
    if decision.answer is True:
        return ""
    if decision.answer is None:
        return "unresolved"
    # ② 的细分（见 questions._BRANCH_QUESTION 注释）：登录后可免费看、
    # 地区/验证码/反爬、404，都与「环境导航失败」（unreachable_hard）分开。
    access = decision.payload.get("access_control_type")
    if access == "login":
        return "login_required"
    if access in ("region", "captcha", "anti_bot"):
        return "blocked"
    if access == "not_found":
        return "broken"
    # ④ 的细分（2026-10-12 阶段 1）：paywall_type 标明「有免费部分」时
    # 走 partial_free——试看/预告能看一部分，与整段锁死的订阅墙
    # （subscription_wall）成因不同，训练目标也不同。
    if decision.payload.get("paywall_type") in ("preview", "trial"):
        return "partial_free"
    # 付费墙是 ④ 的细分结论（见 questions.PAY_WALL）。payload 优先于题绑定
    # ——同一个 question 下要能区分「点了没组件」与「卡在付费」，
    # 而两者都由 PLAYER_OK 问出。
    if decision.payload.get("paywall"):
        return questions.PAY_WALL
    return questions.negative_branch_of(decision.question)


def validate_observation_for_decision(obs: Observation, question: str) -> str:
    """判定前的观察可用性检查，返回告警原因（空串 = 无问题）。

    **不阻断**，只提示：阻断会把「采集降级」变成硬失败，而降级往往是
    单个 tool 超时，整站作废的代价远大于判个 None。

    多条问题**累积**返回而不是 early-return——降级和截断常常同时发生
    （比如 snapshot 预算被 CSS 吃光，既截断又可能连带影响后续采集），
    只报第一条等于让另半问题消失在存档里。
    """
    issues: list[str] = []
    if obs.degraded:
        issues.append(f"观察有降级项 {list(obs.degraded)}，{question} 可能被迫 None")
    if obs.truncated:
        issues.append(
            f"观察被截断（{obs.raw_len}/{obs.max_chars}），{question} 的判定置信度下降"
        )
    return "；".join(issues)
