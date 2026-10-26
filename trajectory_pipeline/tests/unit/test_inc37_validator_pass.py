"""INC37 contract tests for 199 validator-pass upgrade（xlsx 工作单删除后的残存部分）。

背景变更（2026-10-21）：
    INC37 原来的待升级集合是「仓库根 254 份人工复核工作单里 199 条空
    ``review_label``」。该工作单已被人工删除且不可恢复（``.dumate/`` 只剩
    2026-10-12 早期快照，254 行全空，反推不出哪 199 条属于 pending 集合），
    所以按 pending 集合取数的 5 条测试已删除——它们此前每条都是永久 skip，
    留着只会在门禁里制造「这里还有测试封着」的错觉。本文件不再读取 xlsx，
    也不再 import openpyxl。

仍守住的不变量（纯 annotations 侧，不需要 xlsx）：

1. **人工行** / ai-assisted-inc33 的 reviewed_by 未被 INC37 覆写
   （人工合计 **82** 条 = ``manual-review`` 66 + ``reesn`` 16；inc33 18 条
   ——人类行与 inc33 行不许被自动流水线吃掉）。

   ⚠️ **口径变更（2026-10-26 回填）**：本条原先钉的是 ``manual-review``
   == 77。2026-10-25 两次金标重核（commit ``36ef71a`` / ``60cb30e``）后
   人工行是 **66 + 16 = 82**——**逐行核过**：7 行主人自己的人工行从
   ``manual-review`` 改成署名 ``reesn``（**换署名，不是被覆盖**），另
   5 行自动验证器行被人工判了（同样署 ``reesn``）⇒ **人工行只增不减
   （77 → 82）**，没有一条被自动流水线吃掉。
   ⇒ 本条守的不变量是「**人工行不许变少**」，所以钉的是**人工合计**，
   不是 ``manual-review`` 这个**标签**的数量（标签名换人不等于人没了）。
   详见 [编年史 §增量九一](../../docs/计划进展/90-增量编年史.md)。
2. round=1 总数 294、round=2（INC41 提前 retest）12；INC37 只动
   reviewed_by，不动总数。
3. AI validator verdict 分布稳定（2026-10-14 实测值，容差 10；
   heuristic 代码一改就红）。

不再守护（参照物已失）：

    - 那 199 条 pending 是否全部升级、label 是否未被改；
    - 30 条 flagged 行是否带 INC37_NOTE。

    这三件都要 pending 集合本身才判得了，集合随工作单一起消失。要恢复
    封条只能重导一份工作单或改用冻结基线（见 10-TODO台账 M-24 收口记录）。
"""
from __future__ import annotations

import json
import pathlib

import pytest

ANNOTATIONS = (
    pathlib.Path(__file__).resolve().parents[2]
    / "evaluation" / "golden" / "data" / "annotations.jsonl"
)

#: **人工署名**的取值——``manual-review``（历史批）与 ``reesn``（主人）。
#:
#: 这两个都是**人**写的行。``ai-assisted-inc33`` / ``agent-inc41`` /
#: ``auto-validator-*`` 都不是（分别是 AI 辅助、agent 跑批、自动验证器）。
HUMAN_LABELS = ("manual-review", "reesn")

#: 人工行的**合计**（2026-10-26 实测）：66 + 16 = 82。
#:
#: ⚠️ 这个数**不是**「77」——77 是 2026-10-15 时的数。2026-10-25 两次
#: 金标重核里，主人把 7 行自己的人工行改署名为 ``reesn``，并另把 5 行
#: 自动验证器行人工判了 ⇒ 人工行 77 → **82**（只增不减）。
#: **钉合计而不是钉标签数**：标签名换人不等于人没了，钉标签会把
#: 「主人改署名」误报成「人工行被自动流水线吃掉」。
HUMAN_TOTAL_EXPECTED = 82

# 2026-10-14 measured distribution; tolerance 10 because heuristic may
# shift slightly as ACCESS_HINTS / HINTS tuple evolve. Real drift > 10
# should trigger a new INC.
EXPECTED_DIST_2026_10_14 = {
    "auto-validator-l3l4-v1-confirmed": 2,
    "auto-validator-l3l4-v1-weak-confirmed": 9,
    "auto-validator-l3l4-v1-flagged-mismatch": 31,
    "auto-validator-l3l4-v1-no-text-signal": 157,
}


def _load_annotations():
    return [
        json.loads(line)
        for line in ANNOTATIONS.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


@pytest.fixture(scope="module")
def annotations():
    if not ANNOTATIONS.exists():
        pytest.skip("annotations.jsonl missing")
    return _load_annotations()


class TestInc37Invariants:
    def test_manual_review_not_overwritten(self, annotations):
        """人工行不许被自动流水线吃掉（守的是**合计**，不是某个标签名）。

        ⚠️ 2026-10-26 口径变更：原先钉 ``manual-review == 77``，现钉
        **人工合计 == 82**（``manual-review`` 66 + ``reesn`` 16）。来历
        见模块 docstring：主人改署名 + 5 行转人工 ⇒ 人工行只增不减。
        """
        rb = [a["reviewed_by"] for a in annotations]
        human = sum(rb.count(x) for x in HUMAN_LABELS)
        assert human == HUMAN_TOTAL_EXPECTED, (
            "人工行合计 " + str(human) + " != " + str(HUMAN_TOTAL_EXPECTED)
            + "（" + " + ".join(
                str(rb.count(x)) + " " + x for x in HUMAN_LABELS
            ) + "）"
            + "，did INC37 overwrite human rows?"
        )

    def test_total_count_unchanged(self, annotations):
        """round=1 维持 294（v3 freeze 总数；INC37 只改 reviewed_by）；
        round=2 是 INC41（2026-10-15 提前 retest）新增的 12 条，独立计数。"""
        r1 = [a for a in annotations if a.get("round", 1) == 1]
        r2 = [a for a in annotations if a.get("round", 1) == 2]
        assert len(r1) == 294, (
            "round=1 total changed to " + str(len(r1))
            + ", INC37 only moves reviewed_by not the total."
        )
        assert len(r2) == 12, (
            "round=2 retest rows changed to " + str(len(r2))
            + " (INC41 early retest wrote 12); re-run with --force intent."
        )


class TestInc37DistributionTolerance:
    """Hard-coded distribution. Heuristic changes red-light this."""

    def test_within_tolerance_of_2026_10_14(self):
        annotations = _load_annotations()
        from collections import Counter
        c = Counter(a["reviewed_by"] for a in annotations)
        for k, expected in EXPECTED_DIST_2026_10_14.items():
            actual = c.get(k, 0)
            assert abs(actual - expected) <= 10, (
                "INC37 " + k + ": actual " + str(actual)
                + ", expected " + str(expected) + " (tolerance 10)"
                " -- heuristic drifted or verdict logic wrong?"
            )

    def test_ai_assisted_inc33_reduced_by_pending_overlap(self, annotations):
        kept = sum(1 for a in annotations if a["reviewed_by"] == "ai-assisted-inc33")
        assert kept == 18, (
            "ai-assisted-inc33 kept " + str(kept) + " rows, expected 18 "
            "(orig 28 minus 10 overlapped with 199 pending)"
        )