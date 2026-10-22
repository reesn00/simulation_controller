"""六桶归属：封闭集、前置事实、责任方（方案 16 §4 / C-05）。

为什么这张表必须短
------------------
桶名是**结论的标签**，而每个标签的定义里都带前提——与
[`executor/branches.py`](../../executor/branches.py) 的 ``BRANCH_PRECONDITION``
同款。分支表那条教训是：表一旦长到跟现象一样多，就没人维护，
最后每条都往里贴，分布失去意义。

所以（D-C2）：

- **封顶 6 桶**（模块导入即检查，``tests/contract/test_rubric_traceability.py``
  再独立盯一次）；
- **没有 `other`**：不认识的情形一律留空 → 卡停在 ``open``，不贴桶。
  ``other`` 一旦存在就是万能垃圾桶；
- ``accepted_exception`` 是**状态**不是桶（§3.3），别混进来。

谁能判（D-C3）
--------------
``rubric_*`` 与 ``gold_defect`` 是**尺子与真值的裁定**（改 rubric、改金标），
只能人填；agent 至多写建议值。闸门在 :mod:`triage`（写入即校验）。
``HUMAN_ONLY`` 由桶名前缀派生而不是手抄第二份名单——新增一个
``rubric_*`` 桶会自动落到「只能人填」那一侧，这是 fail-closed 的方向。
"""

from __future__ import annotations

from typing import Mapping

BUCKET_EVALUATOR_LOGIC = "evaluator_logic"
BUCKET_EVALUATOR_PROMPT = "evaluator_prompt"
BUCKET_RUBRIC_AMBIGUOUS = "rubric_ambiguous"
BUCKET_RUBRIC_MISSING = "rubric_missing"
BUCKET_GOLD_DEFECT = "gold_defect"
BUCKET_EVIDENCE_MISSING = "evidence_missing"

#: 六桶封闭集（顺序 = §4 表序）。**加一条先裁一条**（D-C2）。
BUCKETS: tuple[str, ...] = (
    BUCKET_EVALUATOR_LOGIC,
    BUCKET_EVALUATOR_PROMPT,
    BUCKET_RUBRIC_AMBIGUOUS,
    BUCKET_RUBRIC_MISSING,
    BUCKET_GOLD_DEFECT,
    BUCKET_EVIDENCE_MISSING,
)

#: 封顶（D-C2）。导入即检查：表长到第 7 项时**在 import 时炸**，
#: 而不是等到某次报表发现分布里多了一类。
MAX_BUCKETS = 6
if len(BUCKETS) > MAX_BUCKETS:  # pragma: no cover - 加桶时必然触发
    raise RuntimeError(
        f"桶表 {len(BUCKETS)} 项 > 封顶 {MAX_BUCKETS}（D-C2）："
        "加一个新桶之前先裁掉一个，或者先改 D-C2"
    )

#: 前置事实：**不成立就不能贴这个桶**（§4）。写清「什么不算」，
#: 因为踩过的坑都在「看起来像」那一侧。
BUCKET_PRECONDITION: Mapping[str, str] = {
    BUCKET_EVALUATOR_LOGIC: (
        "按 rubric **明写条款**应得另一个值，**且**金标与准则两边都无歧义。"
        "缺一条就不是它：准则读得出两种结论 → `rubric_ambiguous`；"
        "金标与准则打架 → `gold_defect`。「我觉得判错了」不算"
    ),
    BUCKET_EVALUATOR_PROMPT: (
        "LLM judge 的输出与锚点冲突（该给 1 档给了 5 档 / 引用观察之外的来源 / "
        "evidence 里找不到锚）。**只对 LLM 判的那几维成立**——代码判据的分歧"
        "贴这个桶就是把判据缺陷推给 prompt"
    ),
    BUCKET_RUBRIC_AMBIGUOUS: (
        "两条条款对同一情形给出不同结论，**或**条款没定义这种情形。"
        "与 `rubric_missing` 的分界：ambiguous 是「写了但打架/说不清」，"
        "missing 是「根本没写」"
    ),
    BUCKET_RUBRIC_MISSING: (
        "准则完全没覆盖这种形态（新分支 / 新页面类型）——**根本没写**。"
        "要求能指到「哪一档都不对应」；只是「条款写得糙/有歧义」那是 "
        "`rubric_ambiguous`"
    ),
    BUCKET_GOLD_DEFECT: (
        "人工标注与 rubric **明写条款**冲突（标注员读错尺子）。要求能指到"
        "具体条款；「我觉得这个标签不好」不算。处置要走修正窗口（金标改动 = "
        "新版本 + 人裁），不许就地改"
    ),
    BUCKET_EVIDENCE_MISSING: (
        "存档不足以判（采集降级 / 裁剪 / 无观察）。这是**采集层的账**，"
        "不是尺子或判据的问题——贴到这里意味着这条分歧不该进 rubric 讨论"
    ),
}

#: 责任方（机器值）：报表按它分组回答「下一手该改哪一侧」（C-07）。
BUCKET_OWNER: Mapping[str, str] = {
    BUCKET_EVALUATOR_LOGIC: "code",
    BUCKET_EVALUATOR_PROMPT: "prompt",
    BUCKET_RUBRIC_AMBIGUOUS: "rubric",
    BUCKET_RUBRIC_MISSING: "rubric",
    BUCKET_GOLD_DEFECT: "gold",
    BUCKET_EVIDENCE_MISSING: "collector",
}

#: 回路归属（§5.1 内环 / §5.2 外环）：`evaluator` 走内环验证器，
#: `rubric` / `gold` 走人推的外环，`collection` 不属于调参回路。
BUCKET_LOOP: Mapping[str, str] = {
    BUCKET_EVALUATOR_LOGIC: "evaluator",
    BUCKET_EVALUATOR_PROMPT: "evaluator",
    BUCKET_RUBRIC_AMBIGUOUS: "rubric",
    BUCKET_RUBRIC_MISSING: "rubric",
    BUCKET_GOLD_DEFECT: "gold",
    BUCKET_EVIDENCE_MISSING: "collection",
}

#: 「只能人填」的桶名前缀（D-C3）。派生而不是手抄：新增 `rubric_*`
#: 自动落在人这一侧。
HUMAN_ONLY_PREFIXES: tuple[str, ...] = ("rubric_", "gold_")

#: 由前缀派生的只读集合（供报表与测试枚举）。
HUMAN_ONLY: frozenset[str] = frozenset(
    b for b in BUCKETS if b.startswith(HUMAN_ONLY_PREFIXES)
)

LOOPS: tuple[str, ...] = ("evaluator", "rubric", "gold", "collection")


def require_bucket(name: str) -> str:
    """封闭集校验。**未登记桶抛 ``KeyError``**（不猜、不回落、不建桶）。"""
    if name not in BUCKET_PRECONDITION:
        raise KeyError(
            f"{name!r} 不是六桶封闭集里的桶（D-C2）；合法的只有："
            f"{', '.join(BUCKETS)}"
        )
    return name


def is_human_only(bucket: str) -> bool:
    """这个桶是不是只能人填（D-C3）。未登记桶同样 ``KeyError``。"""
    return require_bucket(bucket) in HUMAN_ONLY


def owner_of(bucket: str) -> str:
    return BUCKET_OWNER[require_bucket(bucket)]


def loop_of(bucket: str) -> str:
    return BUCKET_LOOP[require_bucket(bucket)]


def buckets_in_loop(loop: str) -> tuple[str, ...]:
    if loop not in LOOPS:
        raise KeyError(f"{loop!r} 不是回路名（{', '.join(LOOPS)}）")
    return tuple(b for b in BUCKETS if BUCKET_LOOP[b] == loop)


__all__ = [
    "BUCKETS",
    "BUCKET_EVALUATOR_LOGIC",
    "BUCKET_EVALUATOR_PROMPT",
    "BUCKET_EVIDENCE_MISSING",
    "BUCKET_GOLD_DEFECT",
    "BUCKET_LOOP",
    "BUCKET_OWNER",
    "BUCKET_PRECONDITION",
    "BUCKET_RUBRIC_AMBIGUOUS",
    "BUCKET_RUBRIC_MISSING",
    "HUMAN_ONLY",
    "HUMAN_ONLY_PREFIXES",
    "LOOPS",
    "MAX_BUCKETS",
    "buckets_in_loop",
    "is_human_only",
    "loop_of",
    "owner_of",
    "require_bucket",
]