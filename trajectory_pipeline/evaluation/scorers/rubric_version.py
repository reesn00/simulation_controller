"""rubric 版本与评估器指纹——D10 / 软维度数字的「尺子编号」。

为什么单独一个模块
------------------
`builder`（写 manifest / 出 agreement）与 `scorers`（判分）都要用它，而两边
都不该成为另一方的 import 根：`builder → scorers` 会拖进 LLM 客户端，
`scorers → builder` 则是验收方反过来依赖报表。放这里，两边都只依赖本模块。

它回答的问题是现在答不出来的那句话：
**「v6 这批 74.7% 是哪版尺子量出来的？」**

两件事分开记
------------
- :data:`RUBRIC_VERSION` —— **人写的**版本号，唯一真源是 rubric 文档的
  ``**版本**: v1.2`` 行；本常量抄一份并**由 contract test 比对**，
  改文档不改常量（或反之）当场红。
- :func:`evaluator_version` —— **机器算的**指纹（sha256 前 12 位），覆盖
  D-3 / D-9 的 system prompt、契约枚举与 few-shot 池。prompt 改一个字，
  指纹就变，分歧卡由此能回答「这条是旧 prompt 还是新 prompt 判的」。

范围声明（不夸大）
------------------
指纹**只覆盖 LLM 判断题（D-3 / D-9）**。代码评分器（D-2 / D-4 / D-5 / D-6）
的行为版本由 git commit 表征，不在这里造一个假版本号——**造一个永远不变的
`code-v1` 比没有更坏**，它会让「没变过」看起来像被验证过。

fail-closed
-----------
rubric 文档读不到、或版本行解析不出来 → 抛 ``ValueError``，不返回空串。
数字旁边挂一个空版本号，比数字本身更误导。
"""

from __future__ import annotations

import hashlib
import json
import pathlib
import re

#: rubric 文档（与代码同版本控制）。解析它的 ``**版本**: vN`` 行。
RUBRIC_DOC = (
    pathlib.Path(__file__).resolve().parents[2]
    / "docs"
    / "设计方案"
    / "在线视频场景rubric.md"
)

#: 当前 rubric 版本。**改 rubric 必须同时改这里**（contract test 盯着）。
RUBRIC_VERSION = "1.2"

#: 文档版本行格式：``**版本**: v1.2 | **生效日期**: ...``
_VERSION_RE = re.compile(r"^\*\*版本\*\*[:：]\s*v([0-9]+(?:\.[0-9]+)*)", re.MULTILINE)


def documented_rubric_version(text: str | None = None) -> str:
    """从 rubric 文档正文解析版本号；解析不出抛 ``ValueError``（fail-closed）。

    ``text`` 显式提供时只解析该文本（测试用），不读盘。
    """
    if text is None:
        if not RUBRIC_DOC.exists():
            raise ValueError(f"rubric 文档不存在：{RUBRIC_DOC}")
        text = RUBRIC_DOC.read_text(encoding="utf-8")
    m = _VERSION_RE.search(text)
    if not m:
        raise ValueError(
            f"rubric 文档里找不到版本行（期望形如 '**版本**: v1.2'）：{RUBRIC_DOC}"
        )
    return m.group(1)


def evaluator_version() -> str:
    """D-3 / D-9 判分面的指纹（sha256 前 12 位）。

    纳入：rubric 版本、两段 system prompt、契约枚举（类别 / 子类 / verdict）、
    few-shot 池（按内容排序后序列化，顺序变化也算变化）。

    惰性 import scorers：本函数被 CLI 调用时才加载 LLM 侧常量，
    避免 `builder` 的其它子命令被拖慢。
    """
    from trajectory_pipeline.evaluation.scorers import d3_scorer
    from trajectory_pipeline.evaluation.scorers import d9_scorer

    parts = {
        "rubric_version": RUBRIC_VERSION,
        "d3_prompt": d3_scorer.D3_SYSTEM_PROMPT,
        "d3_categories": sorted(d3_scorer.D3_CATEGORIES),
        "d3_subtypes": sorted(d3_scorer.D3_SUBTYPES),
        "d9_prompt": d9_scorer.D9_SYSTEM_PROMPT,
        "d9_verdicts": sorted(d9_scorer.D9_VERDICTS),
        "d9_few_shot": json.loads(
            json.dumps(d9_scorer.D9_FEW_SHOT_EXAMPLES, ensure_ascii=False, sort_keys=True,
                       default=str)
        ),
    }
    blob = json.dumps(parts, ensure_ascii=False, sort_keys=True).encode("utf-8")
    return hashlib.sha256(blob).hexdigest()[:12]


# ---------------------------------------------------------------------------
# 维度 → rubric 条款号（方案 16 §5.5 / C-02）
# ---------------------------------------------------------------------------
#
#: 维度名 → rubric 条款号。**唯一真源**：分歧卡的 ``rubric_ref``、prompt 锚点表
#: （方案 16 §7）都从这里投影，不许各写一份。
#:
#: 条款号本来就存在于 rubric 文档里（``G-1..G-7`` / ``D-2..D-10`` / ``B-1..B-4``），
#: 这里做的是**让代码能引用它**。`d7` / `d8` 是批次级维度，条款号是 ``B-2`` /
#: ``B-4``——它们不进样本级条数统计，但映射必须齐，否则 dim 一出现就查不到。
DIM_CLAUSES: dict[str, str] = {
    "d2": "D-2",
    "d3": "D-3",
    "d4": "D-4",
    "d5": "D-5",
    "d6": "D-6",
    "d7": "B-2",   # 检索覆盖完整性（批次级）
    "d8": "B-4",   # 异常处置合规率（批次级）
    "d9": "D-9",
    "d10": "D-10",
}

#: 有 1–5 分档的条款——档位号直接拼在条款号后（``d3`` + 5 → ``D-3.5``）。
#: ``d7`` / ``d8`` 的 B 条款**不出分数**（B-1..B-4 是断言不是评分卡），
#: 所以它们不在本集合里；拼上去会造出一个 rubric 里不存在的条款号。
TIERED_DIMS: frozenset[str] = frozenset(
    {"d2", "d3", "d4", "d5", "d6", "d9", "d10"}
)


def clause_of(dimension: str, tier: int | float | None = None) -> str:
    """维度（+ 可选档位）→ rubric 条款号。

    ``clause_of("d3", 5) == "D-3.5"``；``clause_of("d10") == "D-10"``。

    未登记维度 → ``KeyError``（fail-closed，与本模块其它解析同款）：
    返回空串或原样透传会让拼错的 dim 带着一个假条款号进分歧卡。
    档位越界（不在 1–5）同样抛 ``ValueError``——``D-3.9`` 这种条款不存在。
    """
    try:
        base = DIM_CLAUSES[dimension]
    except KeyError:
        raise KeyError(
            f"维度 {dimension!r} 没有登记 rubric 条款号；先补 DIM_CLAUSES"
        ) from None
    if tier is None or dimension not in TIERED_DIMS:
        return base
    try:
        t = int(tier)
    except (TypeError, ValueError):
        raise ValueError(f"档位 {tier!r} 不是整数") from None
    if not 1 <= t <= 5:
        raise ValueError(f"档位 {t} 不在 1–5 内（{base}.{t} 不是 rubric 条款）")
    return f"{base}.{t}"


__all__ = [
    "DIM_CLAUSES",
    "RUBRIC_DOC",
    "RUBRIC_VERSION",
    "TIERED_DIMS",
    "clause_of",
    "documented_rubric_version",
    "evaluator_version",
]