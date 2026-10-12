"""失败分支账本——**负样本池的唯一入池口**。

为什么单开一个模块：控制流里有十几条 ``rec(negative, "xxx")``，而「负样本池
少一支」是**静默失效**——报表上分部数字都在，就是有一支永远是 0，
没人知道是那支不产出还是那支没接。所以这里做两件事：

1. **入池时校验**：分支名不在注册表的 13 条之内 → 抛错，不落盘。
   新增分支忘了同步 ``perception/questions.py`` 会当场炸，而不是攒一批脏数据。
   已弃用分支（``questions.DEPRECATED_BRANCHES``）**单独放行**——历史路径
   仍可能产出合并态 ``login_wall_or_blocked``，不能把旧调用点当场炸掉。
2. **入池时必带证据**：``Decision.evidence`` 是负样本的**唯一**解释。
   一条没有 evidence 的负样本，在人工复核时无法与「代码 bug 导致的失败」区分，
   等于把排查成本转嫁给标注员。

另外记账三支「没判出来」的分支与真负样本**分开统计**（I4 的直接后果）：
``unresolved`` / ``player_unverified`` / ``trailer_suspect`` 都不是「这里看不了」，
混进负样本会污染负样本纯度——它会让负样本纯度这个指标失去意义，
而指标失效是察觉不到的。三支之间也不合并：成因不同，要修的东西就不同，
见 :data:`NON_SAMPLE_BRANCHES` 的说明。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping

from trajectory_pipeline.perception import questions
from trajectory_pipeline.perception.base import Decision, Observation

#: 分支 → 人读标签。落进 P1 存档，Label Studio 与报表直接用，
#: 不在下游做二次翻译（翻译一次就多一次走样的机会）。
#:
#: 2026-10-12 阶段 1：登录墙合并态拆为 ``login_required`` / ``blocked``
#: （+ ``broken`` 承接 404）。原 ``login_wall_or_blocked`` **保留但弃用**
#: ——历史存档要展示，故不删；新代码路径不再主动产出它。全部合法值
#: （13 活跃 + 1 deprecated + success）= 15 类。
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
}

#: 这些分支**不是**真负样本，是「没判出来」。单独统计，不进负样本池。
#:
#: 三支的成因**互不相同**，混在一支里报表就分不出该修什么：
#:   ``unresolved``         感知层没给出结论（环节①–④，连播放页都没到）
#:   ``player_unverified`` 环节⑦ 判不出来，**但控件点了、播放页就在眼前**
#:                         ——不确定的是「组件能不能用」，是页面证据不足
#:   ``trailer_suspect``    词表判不准，需要语义判断
NON_SAMPLE_BRANCHES = frozenset(
    {
        "unresolved",
        "trailer_suspect",
        "player_unverified",
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
                f"未知失败分支 {branch!r}；注册表 13 条为 "
                f"{sorted(questions.all_branches())}"
            )
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
            reason = _precondition_failed(branch, obs, pre_obs)
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

        ``missing_branches`` 是给**人**看的：十三条里有哪条这一批一条都没产出。
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


def _same_page(a: Observation | None, b: Observation | None) -> bool:
    """这两份观察是不是**同一个页面**——rubric D-4 边界规则的可机械形态。

    与 :mod:`evaluation.scorers.d4_scorer` 的同名判定**同源同规则**
    （url / 元素数 / 正文三个粗粒度特征），但**不 import**——executor
    不依赖 evaluation，两处漂移由 test_branches 的对照测试钉住。

    只比三个粗粒度特征而不是逐字段：网址会从 http 跳 https、
    正文会因懒加载微调，逐字段比对会把「同一页的两次快照」判成不同页。
    """
    if a is None or b is None:
        return False
    if a.url != b.url:
        return False
    if len(a.interactive_elements) != len(b.interactive_elements):
        return False
    return a.body_text == b.body_text


def _precondition_failed(
    branch: str | None, obs: Observation | None, pre_obs: Observation | None = None
) -> str:
    """这条分支的前置事实成立吗？不成立返回说明串，成立返回空串。

    ``obs`` / ``pre_obs`` 为 ``None``（调用方没传）一律放行——**这是有意的**：
    降级逻辑不能反过来把老调用方的记账打成 unresolved，
    而那种「忘了传 obs」的错误由 :meth:`RunLedger.record_from` 的
    形参文档提醒，不在这里做第二次猜测。
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
