"""scout 的三态分流纯函数。

关键边界:
- ``score=0`` 是「判了零分」 —— 走 ``negative``(等同其它 ≤ 2 的判定)。
- ``score=None`` 是「没判」 —— 走 ``quarantine``,不进负例。
- 任何一维 ``is_veto=True`` —— 走 ``negative`` 并打 ``veto`` tag。

``not_run`` 不在本函数处理范围 —— 是 scout 上游发现 fail-closed 后
直接置的;一旦进来就 :func:`classify` 只可能产出 ``quarantine``。
"""

from __future__ import annotations

from typing import Literal

from trajectory_pipeline.evaluation.scout.types import ScoutRecord

#: 负例分流的分数阈值。任一维 ≤ 该值 → ``negative``,并打
#: ``d{dimension[1:]}failed`` tag。**注意:不要与 ``0`` 混用** —— ``0`` 是
#: 判了零分,``None`` 是没判,二者语义不同。
NEGATIVE_SCORE_THRESHOLD = 2

ClassifyResult = Literal["positive", "negative", "quarantine", "skip"]


def classify(rec: ScoutRecord) -> ClassifyResult:
    """对一条 :class:`ScoutRecord` 给出三态分流。

    判定顺序(veto 先于 None 检查):
    1. ``status == "not_run"`` → ``skip``(不落盘,只进 ``--stats`` 计数)
    2. ``status == "undetermined"`` → ``quarantine``
    3. 任何一维 ``is_veto`` → ``negative``(打 ``veto``)
       即使该维 ``score is None``,否决信号也比「缺件」更重。
    4. 任何一维 ``score is None`` → ``quarantine``(兜底:正常路径下不会到这里)
    5. 任何一维 ``score <= 2`` → ``negative``(打 ``d*_failed``)
    6. 否则 → ``positive``
    """
    if rec["status"] == "not_run":
        return "skip"
    if rec["status"] == "undetermined":
        return "quarantine"

    # veto 先于 None 检查:veto 是「明确否决」,即便 score=None 也比「缺件」更重。
    # score=None + is_veto=False 才是上游 bug,走 quarantine。
    if any(s["is_veto"] for s in rec["scores"]):
        return "negative"

    # status == "scored" 的兜底:任何 None 视为上游 bug → quarantine
    if any(s["score"] is None for s in rec["scores"]):
        return "quarantine"

    if any(s["score"] <= NEGATIVE_SCORE_THRESHOLD for s in rec["scores"]):
        return "negative"

    return "positive"


def tags_for(rec: ScoutRecord) -> tuple[str, ...]:
    """为 :class:`ScoutRecord` 派生负向 tag。

    顺序:dim_failed 类先按 dim 升序,``veto`` 在最后(语义上它最重)。
    该函数只对 ``negative`` 结果有调用意义;``positive`` / ``quarantine``
    / ``skip`` 不会用到。
    """
    if not any(s["is_veto"] for s in rec["scores"]):
        veto_tag = ()
    else:
        veto_tag = ("veto",)

    dim_tags = tuple(
        f"{s['dimension']}_failed"
        for s in rec["scores"]
        if s["score"] is not None and s["score"] <= NEGATIVE_SCORE_THRESHOLD
    )
    return dim_tags + veto_tag


__all__ = ["NEGATIVE_SCORE_THRESHOLD", "ClassifyResult", "classify", "tags_for"]
