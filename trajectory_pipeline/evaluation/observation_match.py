"""证据锚定（evidence anchoring）——「依据陈述有没有引用观察里的具体东西」。

**唯一出处**：两处问的是**同一个问题**：

- D-3 的 5 档 / 4 档判据（「依据陈述准确」vs「泛泛而谈」）——
  见 :mod:`trajectory_pipeline.evaluation.scorers.d3_scorer`；
- B-4 ③「止损理由锚定观察」——
  见 :mod:`trajectory_pipeline.evaluation.batch.asserts`。

抄两份必然漂：一边放宽、另一边收紧 ⇒ 同一份依据在 D-3 与 B-4 判出不同
结果，而报告上看不出异常（两处都是「有依据 / 没依据」的措辞差异，
混在一起读成「这条的依据时有时无」）。

口径（与 D-3 v1.2 一致，**不因搬到这里而改**）
----------------------------------------------
三类信号，**任一**成立即视为「锚定」：

(a) 依据含某条观察 ``body_text`` 的 **≥4 字连续子串**；
(b) 依据含某条观察 ``interactive_elements`` 的**元素名**（≥2 字，
    方言两套键名都读——``name`` / ``label``）；
(c) 依据提到 ``video_tag_count`` / ``iframe_count`` 之一的具体数字
    （且该计数 > 0——计数为 0 时「0」不算锚）。

⚠️ 这是**字面层**匹配——真要编也能骗过（把观察里的一句抄进依据即可）。
它只承担「具体 / 泛泛」二选一，**不承担**「结论对不对」的判定
（D-10 因子 B 另有裁判，见 ``d10_scorer``）。

⚠️ **不读 raw CoT**：本模块只看依据陈述与观察存档，不碰模型的思考原文
（CLAUDE.md 红线）。
"""

from __future__ import annotations

import re
from typing import Any, Mapping, Sequence

from trajectory_pipeline.common.elements import element_name

#: 正文子串的**最短长度**。4 字是 D-3 v1.2 定下的口径（3 字以下误命中
#: 率太高——「播放器」这类词在两个页面上都能撞上）。
MIN_BODY_SUBSTRING = 4

#: 元素名的**最短长度**。2 字起（单字元素名撞车率过高）。
MIN_LABEL_CHARS = 2

#: 依据里的数字。
_NUMBER_RE = re.compile(r"\d+")


#: 命中后**向两侧延伸**的上限（字符）。延伸是为了给人读时看得懂
#: （「开通会员」比「开通」有用），但延伸不是判据的一部分——判据只有
#: 「≥4 字连续子串」这一条，延伸只是把它显示得更完整。
_MAX_ANCHOR_CHARS = 40


def _body_anchor(body: str, evidence: str) -> str:
    """正文里出现在依据中的 ≥4 字连续子串（没有则空串）。

    扫描 4 字窗口（O(n)）；命中后**向两侧延伸**取更完整的片段（上限
    :data:`_MAX_ANCHOR_CHARS` 字）——延伸只影响「报给人看的措辞」，
    不影响判据（4 字窗口已命中就说明锚定成立）。
    """
    return text_anchor(body, evidence)


def text_anchor(text: str, evidence: str) -> str:
    """任意文本里出现在依据中的 ≥4 字连续子串（没有则空串）。

    与 :func:`anchored_phrases` 里的正文锚**同一套判据**（≥4 字连续
    子串）——B-4 的「止损理由锚定观察」在没有观察可锚时要退到**步骤
    报错原文**上，两处必须同口径，否则同一份理由在 D-3 与 B-4 判出
    不同结果。
    """
    if not text or not evidence:
        return ""
    n = len(text)
    for start in range(0, n - MIN_BODY_SUBSTRING + 1):
        piece = text[start:start + MIN_BODY_SUBSTRING]
        if piece not in evidence:
            continue
        lo, hi = start, start + MIN_BODY_SUBSTRING
        while lo > 0 and hi - lo < _MAX_ANCHOR_CHARS and text[lo - 1:hi] in evidence:
            lo -= 1
        while hi < n and hi - lo < _MAX_ANCHOR_CHARS and text[lo:hi + 1] in evidence:
            hi += 1
        return text[lo:hi]
    return ""


def _label_anchors(obs: Mapping[str, Any], evidence: str) -> list[str]:
    """观察里的元素名，出现在依据中的那些。"""
    out: list[str] = []
    for elem in obs.get("interactive_elements") or ():
        if not isinstance(elem, Mapping):
            continue
        label = element_name(elem).strip()
        if len(label) >= MIN_LABEL_CHARS and label in evidence:
            out.append(label)
    return out


def anchored_phrases(
    evidence: str,
    observations: Sequence[Mapping[str, Any]] | None,
) -> tuple[str, ...]:
    """依据**锚在哪**——命中的短语 / 元素名 / 计数。

    空元组 = 没锚定（依据是「泛泛而谈」）。

    Returns:
        按 (a) 正文子串 → (b) 元素名 → (c) 计数的顺序列出命中的锚。
    """
    text = str(evidence or "")
    if not text or not observations:
        return ()
    found: list[str] = []
    seen: set[str] = set()

    def _add(item: str) -> None:
        if item and item not in seen:
            seen.add(item)
            found.append(item)

    for obs in observations:
        if not isinstance(obs, Mapping):
            continue
        _add(_body_anchor(str(obs.get("body_text") or ""), text))
        for label in _label_anchors(obs, text):
            _add(f"元素名「{label}」")
        media = int(obs.get("video_tag_count") or 0)
        iframe = int(obs.get("iframe_count") or 0)
        for count in (media, iframe):
            if count > 0 and str(count) in text:
                _add(f"计数 {count}")
    return tuple(found)


def evidence_anchored(
    evidence: str,
    observations: Sequence[Mapping[str, Any]] | None,
) -> bool:
    """依据陈述有没有锚定到观察里的具体内容。

    这是 :func:`anchored_phrases` 的布尔壳——**判据只有一处**。
    """
    return bool(anchored_phrases(evidence, observations))


__all__ = [
    "MIN_BODY_SUBSTRING",
    "MIN_LABEL_CHARS",
    "anchored_phrases",
    "evidence_anchored",
    "text_anchor",
]
