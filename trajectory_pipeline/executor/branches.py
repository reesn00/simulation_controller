"""失败分支账本——**负样本池的唯一入池口**。

为什么单开一个模块：控制流里有十几条 ``rec(negative, "xxx")``，而「负样本池
少一支」是**静默失效**——报表上分部数字都在，就是有一支永远是 0，
没人知道是那支不产出还是那支没接。所以这里做两件事：

1. **入池时校验**：分支名不在注册表的 9 条之内 → 抛错，不落盘。
   新增分支忘了同步 ``perception/questions.py`` 会当场炸，而不是攒一批脏数据。
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
BRANCH_LABELS: Mapping[str, str] = {
    "not_play_site": "不是播放站点",
    "unreachable_hard": "导航失败（网络/DNS/超时）",
    "login_wall_or_blocked": "登录墙 / 验证码 / 地区限制",
    "no_play_control": "页面上没有播放控件",
    "trailer_only": "只有预告片，播放资源不存在",
    "trailer_suspect": "疑似预告（词表判不准，待人工）",
    "component_unverified": "点开后没有可验证的播放组件",
    "player_unverified": "有播放组件，但确认不了能不能用",
    "unresolved": "感知层未给出结论（fail-closed）",
}

#: 这些分支**不是**真负样本，是「没判出来」。单独统计，不进负样本池。
#:
#: 三支的成因**互不相同**，混在一支里报表就分不出该修什么：
#:   ``unresolved``         感知层没给出结论（环节①–④，连播放页都没到）
#:   ``player_unverified`` 环节⑦ 判不出来，**但控件点了、播放页就在眼前**
#:                         ——不确定的是「组件能不能用」，是页面证据不足
#:   ``trailer_suspect``    词表判不准，需要语义判断
NON_SAMPLE_BRANCHES = frozenset({
    "unresolved", "trailer_suspect", "player_unverified",
})


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
    reached: bool = False          # 是否走到了播放页
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
        if branch is not None and branch not in questions.all_branches():
            raise UnknownBranch(
                f"未知失败分支 {branch!r}；注册表 9 条为 "
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
        site_url: str = "",
        play_page_url: str = "",
    ) -> SiteOutcome:
        """按 Decision 记账：``answer is None`` → ``unresolved``，
        否则按该题绑定的负分支记。

        这条映射写在 :func:`decision_branch` 里而不是散在控制流，
        是为了让「判 false 归哪支」只有一处定义。

        ## ``obs`` 存在的理由：负分支的**前置事实**要核
        分支名是**结论的标签**，而每条负分支的定义里都带着一个前置事实
        ——``component_unverified`` 的定义是「**有 video 标签**但播放器未正常
        加载」。映射只看「模型说了 False」，于是前置事实不成立时照样贴标签。

        实测（2026-10-09 www.mgtvtv.com/tv/32080/）：点击前后两次观察的
        正文与元素数**完全相同**（390 字 / 37 元素 / video=0），说明点击
        根本没把页面带进播放页；模型判 False 的原话是「仍显示影片详情和
        『立即播放』按钮，并非实际播放页」。于是贴上 ``component_unverified``
        ——一条**货真价实的负样本**，说的是「这个站看不了《功夫》」。
        而它有、有播放按钮，只是我们没点进去。

        负样本池的全部价值是「这里真的看不了」。贴错标签不是噪声小一点，
        是**把可看的站写成看不了的**——而这类样本一旦进池，
        人工也未必再看得出它是错的（理由写得通顺、结论看着专业）。

        所以：**前置事实不成立就降级为 ``unresolved``**，交人工，
        而不是硬贴一条负分支。

        ⚠️ **环节⑦ 的 ``answer=None`` 不用本方法**，走
        :meth:`record_unresolved_reached`。那里的 ``None`` 说的是
        「组件能不能用」判不出来，与这里的「能不能判」不是一回事。
        """
        if decision.answer is None:
            branch = "unresolved"
        else:
            branch = questions.negative_branch_of(decision.question)
            reason = _precondition_failed(branch, obs)
            if reason:
                return self.record(
                    url, "unresolved",
                    f"{decision.evidence}\n"
                    f"[记账降级] 本该记 {branch}，但{reason}——"
                    f"该分支的前置事实不成立，不贴这条负标签",
                    decision=decision, reached=reached,
                    site_url=site_url, play_page_url=play_page_url,
                )
        return self.record(url, branch, decision.evidence, decision=decision,
                           reached=reached, site_url=site_url,
                           play_page_url=play_page_url)

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
        return self.record(url, "player_unverified", decision.evidence,
                           decision=decision, reached=True,
                           site_url=site_url, play_page_url=play_page_url)

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

        ``missing_branches`` 是给**人**看的：九条里有哪条这一批一条都没产出。
        它比「各分支数量」更能说明问题——数量为 0 可能只是样本少，
        而 missing 会直接指向「这条路径没接上」。

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


#: 负分支的**前置事实**：贴这条标签之前，页面上必须真的有这个东西。
#:
#: 「有 video 标签但播放器未正常加载」这句话在没有 video 标签的页面上
#: 是**语法通顺、语义空洞**的——而空洞的负标签比没有标签坏得多：
#: 它进了池，看着像一条正经的失败样本，人也未必再看得出它是空的。
#:
#: 值是「不成立时的说明」，用在 :meth:`RunLedger.record_from` 的降级路径里。
#: **没有前置事实的分支不进这张表**——表要短，
#: 长得跟分支表一样长就没人维护了。
BRANCH_PRECONDITION: Mapping[str, str] = {
    "component_unverified": (
        "「播放器未正常加载」说的是一个**存在但没起来**的播放器，"
        "而这一页连播放器都不存在"
    ),
}


def _precondition_failed(branch: str | None, obs: Observation | None) -> str:
    """这条分支的前置事实成立吗？不成立返回说明串，成立返回空串。

    ``obs`` 为 ``None``（调用方没传观察）一律放行——**这是有意的**：
    降级逻辑不能反过来把老调用方的记账打成 unresolved，
    而那种「忘了传 obs」的错误由 :meth:`RunLedger.record_from` 的
    ``obs`` 形参文档提醒，不在这里做第二次猜测。
    """
    if obs is None or branch is None:
        return ""
    if branch == "component_unverified" and obs.video_tag_count < 1:
        return f"页面上一个媒体标签都没有（video_tag_count={obs.video_tag_count}）——" \
               f"{BRANCH_PRECONDITION[branch]}"
    return ""


def decision_branch(decision: Decision) -> str:
    """Decision → 分支名。``None`` → ``unresolved``，否则取题绑定的负分支。

    ``answer=True`` 返回空串——成功不是失败分支。
    """
    if decision.answer is True:
        return ""
    if decision.answer is None:
        return "unresolved"
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