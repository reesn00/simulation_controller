"""manual-review 人工复核端的一致性守门（xlsx 工作单删除后的残存部分）。

设计来源：
    INC36「b 路径执行 · 77 条 manual-review 与 xlsx 1:1 一致审计」，
    以及 INC32/INC33 追溯链（unreachable_hard → broken）。

历史与现状（2026-10-21）：
    本文件原本以仓库根两份人工复核工作单
    ``黄金集人工复核工作单-23条.xlsx`` / ``-254条.xlsx`` 为参照物，
    守的不变量是「annotations 里 77 条 manual-review 的源头就是那两份表」。
    这两份 xlsx 已被人工删除且**不可恢复**——``.dumate/`` 里只剩
    2026-10-12 的早期快照（254 行 ``review_label`` 全空、23 行只填 5 条），
    重建不出终版。所有「拿 xlsx 当参照物」的断言因此一并删除：它们此前
    已是 5 条永久 skip + 1 条永久 fail（``test_至少一份xlsx存在``）。
    文件名沿用 ``xlsx_backfill`` 只为保住既有文档锚点，本文件不再读取
    任何 xlsx，也不再 import openpyxl。

仍守住的不变量：

    1. ``TPIN02__1cf5deca__2d99fba8`` 的 INC32/INC33 追溯链。工作单里记的
       是 unreachable_hard，回填成 broken 是 D10 偏差修缮（簇 2）的追溯
       结果，不是漂移；这条钉在 annotations 侧——哪天 label / reviewed_by /
       note 变了，说明追溯链又被动过，需要走新一批增量。

不再守护（参照物已失，记录在此以免被误读为「还有测试封着」）：

    - 「77 条 manual-review 每条都能在人工工作单里找到对应行」——表没了；
    - 「xlsx 写 A / annotations 标 B 必须有 note 解释」——同上；
    - 「xlsx 254 份里 199 条空 review_label」——同上，且该集合无法从
      annotations 反推（annotations 侧这些 id 现为
      ``auto-validator-l3l4-v1-*``）。
"""
from __future__ import annotations

import json
import pathlib

import pytest

ANNOTATIONS = (
    pathlib.Path(__file__).resolve().parents[2]
    / "evaluation" / "golden" / "data" / "annotations.jsonl"
)


def _load_annotations() -> list[dict]:
    return [
        json.loads(l)
        for l in ANNOTATIONS.read_text(encoding="utf-8").splitlines()
        if l.strip()
    ]


@pytest.fixture(scope="module")
def annotations() -> list[dict]:
    return _load_annotations()


class Test已知的INC32追溯:
    """TPIN02__1cf5deca__2d99fba8：工作单 unreachable_hard → annotations broken。

    INC32 设计：unreachable_hard 改 broken 是 D10 偏差修缮的追溯（簇 2）。
    哪天这条测试红了，说明 INC32/INC33 追溯链又动过，需要新一批增量。"""

    def test_TPIN02_1cf5deca_2d99fba8_annotations是broken_ai_assisted(self, annotations):
        """INC32 把 unreachable_hard 改 broken；INC33 标 ai-assisted-inc33。"""
        a = next(
            (x for x in annotations if x["golden_id"] == "TPIN02__1cf5deca__2d99fba8"),
            None,
        )
        assert a is not None
        assert a["label"] == "broken", f"label 漂移为 {a['label']}"
        assert a["reviewed_by"] == "ai-assisted-inc33", (
            f"reviewed_by 漂移为 {a['reviewed_by']}"
        )
        assert "INC33" in (a.get("note") or ""), (
            f"note 丢了 INC33 追溯链：{a.get('note')}"
        )