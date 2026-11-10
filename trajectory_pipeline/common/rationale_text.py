"""rationale 文本的训练侧清洗（P3 与 T 共用）。

为什么有这条规则
----------------
2026-11-10 owner 指出：assistant 消息以「目前没有此前操作。」开头。
这句是模型对「上文没有更早动作」的状态陈述——对训练零信息，却占了
CoT 的第一个句子。它来自 P1 的 rationale 原文（模型自己写的），
**P1 存档一个字不动**；只在本层组装出的训练/复核视图里去掉。

⚠️ 只删**精确的句首**。不做「没有…操作」的模糊匹配：把
「目前没有找到可播放页面」这类有效事实误删，比留一句废话坏得多。
"""

from __future__ import annotations

#: 模型偶尔写在首步 rationale 开头的空状态陈述（精确句）。
EMPTY_HISTORY_LEAD_IN = "目前没有此前操作。"


def strip_empty_history_lead_in(text: str) -> str:
    """去掉 rationale 开头的「目前没有此前操作。」。

    只处理**句首**；正文里出现同样的字（引用、复述）不动。
    删完只剩空白时返回空串——调用方按「这一步没写思考」处理。
    不含该前缀时原样返回（不做 strip：调用方已经 strip 过，这里
    不替它改别的空白）。
    """
    stripped = text.lstrip()
    if not stripped.startswith(EMPTY_HISTORY_LEAD_IN):
        return text
    return stripped[len(EMPTY_HISTORY_LEAD_IN):].lstrip()


__all__ = ["EMPTY_HISTORY_LEAD_IN", "strip_empty_history_lead_in"]