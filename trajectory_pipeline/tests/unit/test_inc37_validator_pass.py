"""INC37 contract tests for 199 validator-pass upgrade（xlsx 工作单删除后的残存部分）。

背景变更（2026-10-21）：
    INC37 原来的待升级集合是「仓库根 254 份人工复核工作单里 199 条空
    ``review_label``」。该工作单已被人工删除且不可恢复（``.dumate/`` 只剩
    2026-10-12 早期快照，254 行全空，反推不出哪 199 条属于 pending 集合），
    所以按 pending 集合取数的 5 条测试已删除——它们此前每条都是永久 skip，
    留着只会在门禁里制造「这里还有测试封着」的错觉。本文件不再读取 xlsx，
    也不再 import openpyxl。

仍守住的不变量（纯 annotations 侧，不需要 xlsx）：

1. **77 条人类行一条都不许丢人类署名**，ai-assisted-inc33 仍 18 条。
   署名取值见 ``HUMAN_SIGNATURES``：``manual-review`` 之外还有 owner
   本人用的 ``reesn``——2026-10-24 起方案 16/19 的金标重标定把 11 条
   ``manual-review`` 改签成 ``reesn``（都带 ``gold_defect`` note），
   所以「manual-review 仍 77 条」这个计数已经不成立了（现 66 条）。
   断言改成对身份封条 ``SEALED_HUMAN_ROWS_2026_10_15`` 的**超集**：
   改签是人的动作，签名没丢；机器覆盖会让封条行**从集合里消失**，
   并点名是哪几条。
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

#: 人类署名。``manual-review`` = INC37 之前人工复核批的署名；
#: ``reesn`` = owner 本人在方案 16/19 金标重标定（2026-10-24 起）改签用的
#: 署名。两者都是**人**写的，只数 ``manual-review`` 会把一次合法的人工
#: 改签读成「人类行被机器吃掉」。自动化侧署名（``auto-validator-*`` /
#: ``ai-assisted-*`` / ``agent-*``）一律不进这个集合。
HUMAN_SIGNATURES = frozenset({"manual-review", "reesn"})

#: 77 条人类行的**身份封条**：2026-10-15 v3 冻结时的 ``(golden_id, round)``，
#: 抄自 7c14115（冻结之后、下一次改签之前的提交）。
#: 断言是**超集**，不是相等——改签（manual-review → reesn）不动身份，
#: 人类新接手机器行（方案 16 的 5 条 ``auto-validator-* → reesn``）会让
#: 集合变大，两者都不是回退。只有当某行**丢了**人类署名时才会失败。
#: 要加行：先确认那行是人复核过的，再把键补进本表并同步
#: ``docs/计划进展/09-门禁与纪律域.md`` §xlsx 测试清退 的读数。
SEALED_HUMAN_ROWS_2026_10_15 = frozenset({
    ("E001__57bbf995__6a28d60d", 1),
    ("E001__57bbf995__6e8d5d31", 1),
    ("E001__57bbf995__910a8b86", 1),
    ("E001__57bbf995__search", 1),
    ("E001__57bbf995__site-1", 1),
    ("E001__57bbf995__site-2", 1),
    ("E001__57bbf995__site-3", 1),
    ("E005__e35b7455__3bd50dfb", 1),
    ("E005__e35b7455__49e41ed3", 1),
    ("E005__e35b7455__a4844373", 1),
    ("E005__e35b7455__aa3626fb", 1),
    ("E005__e35b7455__b8513a71", 1),
    ("E005__e35b7455__de84d997", 1),
    ("E005__e35b7455__feb5257a", 1),
    ("E006__2729220b__0db2fb36", 1),
    ("E006__2729220b__10352791", 1),
    ("E006__2729220b__55573b12", 1),
    ("E006__2729220b__5aaa565e", 1),
    ("E006__2729220b__60e5c64f", 1),
    ("E006__2729220b__8e1a6470", 1),
    ("E006__2729220b__e9b9542b", 1),
    ("E014__f50809fb__30062587", 1),
    ("E014__f50809fb__5f00a90d", 1),
    ("E014__f50809fb__7493f4e0", 1),
    ("E014__f50809fb__75b59063", 1),
    ("E014__f50809fb__a8a5df5d", 1),
    ("E014__f50809fb__cf884eaa", 1),
    ("E014__f50809fb__dec0b09f", 1),
    ("E017__a2878b23__1e5490ea", 1),
    ("E017__a2878b23__7fd1a252", 1),
    ("E017__a2878b23__95b7b892", 1),
    ("E017__a2878b23__95f0a368", 1),
    ("E017__a2878b23__9a9dc9bf", 1),
    ("E017__a2878b23__b771c9f0", 1),
    ("E021__94a1392f__08347b3f", 1),
    ("E021__94a1392f__13837008", 1),
    ("E021__94a1392f__2ab12498", 1),
    ("E021__94a1392f__2b9c7618", 1),
    ("E021__94a1392f__3f85df8a", 1),
    ("E021__94a1392f__728b7785", 1),
    ("E021__94a1392f__d41fe4ac", 1),
    ("E023__4d175424__33a0fe40", 1),
    ("E023__4d175424__7754b3d2", 1),
    ("E023__4d175424__88efde54", 1),
    ("E023__4d175424__9d49aca1", 1),
    ("E023__4d175424__a7f0eaaf", 1),
    ("T001__67a05faa__11e1faf9", 1),
    ("T001__67a05faa__1327452f", 1),
    ("T001__67a05faa__1a0943a9", 1),
    ("T001__67a05faa__1edd45d0", 1),
    ("T001__d063ddf4__1a473949", 1),
    ("T011__443c5ec0__f9895792", 1),
    ("T012__8c050356__22e0fb36", 1),
    ("T012__8c050356__5c2c73cf", 1),
    ("T018__ad4a6065__22e0fb36", 1),
    ("T025__0d6d185e__417199e7", 1),
    ("T033__53ce4b49__22e0fb36", 1),
    ("T035__3ab75598__search", 1),
    ("T036__10313caf__22e0fb36", 1),
    ("TPIN02__1a5e526d__ad5971d1", 1),
    ("TPIN02__1a5e526d__bf4b6ae1", 1),
    ("TPIN02__1a5e526d__db4899cd", 1),
    ("TPIN02__1a5e526d__f73ce362", 1),
    ("TPIN02__1a5e526d__fab8371e", 1),
    ("TPIN02__1a5e526d__ff7185a5", 1),
    ("TPIN02__1cf5deca__1efaa919", 1),
    ("TPIN02__1cf5deca__3dcdbc14", 1),
    ("TPIN02__1cf5deca__417199e7", 1),
    ("TPIN02__1cf5deca__42ce4312", 1),
    ("TPIN02__1cf5deca__87597866", 1),
    ("TPIN02__1cf5deca__910a8b86", 1),
    ("TPIN02__1cf5deca__aa537eb5", 1),
    ("TPIN02__1cf5deca__ad5971d1", 1),
    ("TPIN02__1cf5deca__bab5e7fc", 1),
    ("TPIN02__1cf5deca__bf4b6ae1", 1),
    ("TPIN02__1cf5deca__fb7b373d", 1),
    ("TPIN02__1cf5deca__ff7185a5", 1),
})

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
    def test_human_rows_not_overwritten(self, annotations):
        """封条里的身份都得**还带**人类署名——超集断言，不是固定计数。

        计数为什么不能钉：2026-10-24/25 方案 16/19 的金标重标定把 11 条
        人类行从 ``manual-review`` 改签成 ``reesn``（16 条 ``reesn`` 里
        另 5 条原先是 ``auto-validator-*``），``manual-review`` 计数因此
        77 → 68 → 66。那是人做的，签名没丢，钉死 77 只会训练人忽略红灯。
        """
        human = {
            (a["golden_id"], a.get("round", 1))
            for a in annotations
            if a["reviewed_by"] in HUMAN_SIGNATURES
        }
        # 封条只许变长不许变短——删一行就等于悄悄放宽这条测试。
        assert len(SEALED_HUMAN_ROWS_2026_10_15) >= 77, (
            "封条被删到 " + str(len(SEALED_HUMAN_ROWS_2026_10_15))
            + " 条（2026-10-15 冻结时 77）；删封条行要先在 "
            "09-门禁与纪律域 记一笔"
        )
        lost = sorted(SEALED_HUMAN_ROWS_2026_10_15 - human)
        assert not lost, (
            str(len(lost)) + " sealed human rows no longer carry a human "
            "signature (human-signed rows now: " + str(len(human)) + "): "
            + str(lost) + " —— 自动流水线覆写了 reviewed_by？"
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