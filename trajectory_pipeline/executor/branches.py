"""失败分支账本——**负样本池的唯一入池口**。

为什么单开一个模块：控制流里有十几条 ``rec(negative, "xxx")``，而「负样本池
少一支」是**静默失效**——报表上分部数字都在，就是有一支永远是 0，
没人知道是那支不产出还是那支没接。所以这里做两件事：

1. **入池时校验**：分支名不在注册表的 8 条之内 → 抛错，不落盘。
   新增分支忘了同步 ``perception/questions.py`` 会当场炸，而不是攒一批脏数据。
2. **入池时必带证据**：``Decision.evidence`` 是负样本的**唯一**解释。
   一条没有 evidence 的负样本，在人工复核时无法与「代码 bug 导致的失败」区分，
   等于把排查成本转嫁给标注员。

另外记账 ``unresolved`` 与真负样本**分开统计**（I4 的直接后果）：
``unresolved`` 是「没判出来」，混进负样本会污染负样本纯度——
它会让负样本纯度这个指标失去意义，而指标失效是察觉不到的。
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
    "unresolved": "感知层未给出结论（fail-closed）",
}

#: 这些分支**不是**真负样本，是「没判出来」。单独统计，不进负样本池。
NON_SAMPLE_BRANCHES = frozenset({"unresolved", "trailer_suspect"})


class UnknownBranch(KeyError):
    """分支名不在注册表内。控制流写错就当场炸，不静默落盘。"""


@dataclass(frozen=True, slots=True)
class SiteOutcome:
    """一个站点的最终归宿。

    ``branch`` 只在负向时非空；正向成功记 ``branch=None``。
    """

    url: str
    branch: str | None
    evidence: str
    question: str = ""
    source: str = "rule"
    fallback_used: bool = False
    reached: bool = False          # 是否走到了播放页

    @property
    def is_negative_sample(self) -> bool:
        return self.branch is not None and self.branch not in NON_SAMPLE_BRANCHES

    def to_json(self) -> dict[str, Any]:
        return {
            "url": self.url,
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
    ) -> SiteOutcome:
        """记一个站点结果并返回它。分支名非法时抛 :class:`UnknownBranch`。"""
        if branch is not None and branch not in questions.all_branches():
            raise UnknownBranch(
                f"未知失败分支 {branch!r}；注册表 8 条为 "
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
        )
        self.outcomes.append(outcome)
        return outcome

    def record_from(
        self,
        url: str,
        decision: Decision,
        *,
        reached: bool = False,
    ) -> SiteOutcome:
        """按 Decision 记账：``answer is None`` → ``unresolved``，
        否则按该题绑定的负分支记。

        这条映射写在 :func:`decision_branch` 里而不是散在控制流，
        是为了让「判 false 归哪支」只有一处定义。
        """
        if decision.answer is None:
            branch = "unresolved"
        else:
            branch = questions.negative_branch_of(decision.question)
        return self.record(url, branch, decision.evidence, decision=decision,
                           reached=reached)

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

        ``missing_branches`` 是给**人**看的：八条里有哪条这一批一条都没产出。
        它比「各分支数量」更能说明问题——数量为 0 可能只是样本少，
        而 missing 会直接指向「这条路径没接上」。
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