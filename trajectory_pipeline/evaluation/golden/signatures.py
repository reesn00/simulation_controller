"""金标署名的分类：**谁写的这一行**（真源）。

为什么要分类
------------
金标的 ``golden.reviewed_by`` 记的是「谁定了这个标签」。v14 的 294 行里
**只有 82 行是人署的名**，其余 212 行是自动验证器 / AI 辅助写的。推上
Label Studio 时这个区分是**判读投入的开关**：

* 人署的名 ⇒ owner 看过这一行，复核时重点是「代码判得对不对」；
* 自动写的 ⇒ **没人看过一眼**，复核时重点可能是「金标本身对不对」。

⇒ 标注页必须把这两类**区别对待**，而分类规则不能各写一份。

分类规则（**就这一条**）
-----------------------
============================  ===========================================
``human``                    署名 ∈ :data:`HUMAN_SIGNATURES`
``automated``                署名非空、但不在上面那个集合里
``unknown``                  署名是空的
============================  ===========================================

⚠️ **「不在集合里 ⇒ automated」是 fail-closed 的方向**：
新冒出来的署名（``agent-*`` / 什么新工具名）先算**自动**，于是它会被
「需要人看一眼」的那批圈进去。倒过来（未知 ⇒ 算人核过的）会让新署名
**悄悄冒充人核过的**——那正是最贵的错：标着「owner 核过」的标签其实
没人看过。

⚠️ **AI 辅助算自动化，不算人核**。``ai-assisted-inc33`` 这类署名是
「模型起草、人没逐条看」——它与 ``manual-review`` / ``reesn``（人自己
定的）不是一回事。仓库里既有的口径见
``tests/unit/test_inc37_validator_pass.py``（人类身份封条那一组），
**本模块与那份集合必须逐字一致**（契约测试盯住）。
"""

from __future__ import annotations

#: **人署的名**（人自己定的标签）。
#:
#: ``manual-review`` = 人工复核批的署名；``reesn`` = owner 本人在金标
#: 重标定（方案 16/19）起用的署名。两者都是**人**写的。
#:
#: ⚠️ **只数 ``manual-review`` 会把一次合法的人工改签读成「人类行被机器
#: 吃掉」**（2026-10-25 有 11 条人核行改署名为 ``reesn``）。
HUMAN_SIGNATURES: frozenset[str] = frozenset({"manual-review", "reesn"})

#: 分类结果。
KIND_HUMAN = "human"
KIND_AUTOMATED = "automated"
KIND_UNKNOWN = "unknown"

#: 全部三种分类（报表按这个顺序输出）。
SIGNATURE_KINDS: tuple[str, ...] = (KIND_HUMAN, KIND_AUTOMATED, KIND_UNKNOWN)


def classify_signature(signature: str | None) -> str:
    """署名 → 分类（``human`` / ``automated`` / ``unknown``）。

    空串 / ``None`` ⇒ ``unknown``——**「没人署名」与「署了机器的名」是
    两件事**，合成一类就看不出「这一行压根没人碰过」。
    """
    text = (signature or "").strip()
    if not text:
        return KIND_UNKNOWN
    if text in HUMAN_SIGNATURES:
        return KIND_HUMAN
    return KIND_AUTOMATED


def is_human_signature(signature: str | None) -> bool:
    """这条是不是**人署的名**（用于「要人看一眼」的判定）。"""
    return classify_signature(signature) == KIND_HUMAN


__all__ = [
    "HUMAN_SIGNATURES",
    "KIND_AUTOMATED",
    "KIND_HUMAN",
    "KIND_UNKNOWN",
    "SIGNATURE_KINDS",
    "classify_signature",
    "is_human_signature",
]
