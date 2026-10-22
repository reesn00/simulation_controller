"""INC37 contract tests for 199 validator-pass upgrade（xlsx 工作单删除后的残存部分）。

背景变更（2026-10-21）：
    INC37 原来的待升级集合是「仓库根 254 份人工复核工作单里 199 条空
    ``review_label``」。该工作单已被人工删除且不可恢复（``.dumate/`` 只剩
    2026-10-12 早期快照，254 行全空，反推不出哪 199 条属于 pending 集合），
    所以按 pending 集合取数的 5 条测试已删除——它们此前每条都是永久 skip，
    留着只会在门禁里制造「这里还有测试封着」的错觉。本文件不再读取 xlsx，
    也不再 import openpyxl。

仍守住的不变量（纯 annotations 侧，不需要 xlsx）：

1. manual-review / ai-assisted-inc33 的 reviewed_by 未被 INC37 覆写
   （77 / 18 条——人类行与 inc33 行不许被自动流水线吃掉）。
2. round=1 总数 294、round=2（INC41 提前 retest）12；INC37 只动
   reviewed_by，不动总数。
3. AI validator verdict 分布稳定（2026-10-14 实测值，容差 10；
   heuristic 代码一改就红）。

不再守护（参照物已失）：

    - 那 199 条 pending 是否全部升级、label 是否未被改；
    - 31 条 flagged 行是否带 INC37_NOTE。

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
        rb = [a["reviewed_by"] for a in annotations]
        assert rb.count("manual-review") == 77, (
            "manual-review count " + str(rb.count("manual-review"))
            + " != 77, did INC37 overwrite human rows?"
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