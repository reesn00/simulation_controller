"""xlsx 人工复核工作单（23+254）与 annotations.jsonl 一致性守门。

设计来源：INC36「b 路径执行 · 77 条 manual-review 与 xlsx 1:1 一致审计」。

为什么需要这个测试：
    仓库根两份 xlsx 是人类填写的复核工作单，annotations.jsonl 里 77 条
    ``reviewed_by=manual-review`` 的源头就是它。后续如果有人改 xlsx
    （直接编辑 .xlsx 或重新导出），或者直接改 annotations.jsonl，漂移
    会让「manual-review = 真人工复核」这个事实破裂——D9 单人 test-retest
    的「单人稳不稳」测的就不是真人了。

守住的不变量：

    1. annotations 里所有 ``reviewed_by="manual-review"`` 的行，仓库根
       至少一份 xlsx 必须有对应 ``golden_id`` 的 ``review_label`` ；
    2. ``review_label`` 与 ``annotations.label`` 必须一致——
       除非 annotations 行的 ``note`` 显式说明是 INC32/INC33 追溯
       （这是设计使然，不是漂移）；
    3. 1 条已知追溯（TPIN02__1cf5deca__2d99fba8：xlsx unreachable_hard
       → annotations broken）由本测试**精确钉住**——哪天它变了，
       说明 INC32/INC33 追溯链又动过，需要走新一批增量。

不在本测试范围：

    - xlsx 里 199 条空 review_label——真待人工产能问题，**不**写硬测试
      （强行挂 fail 等于叫嚣「找人来填」，没意义）；
    - annotations 里 ``reviewed_by=ai-assisted-*`` 的 217 条——它们的
      来源是自动标注流水线，不是 xlsx；不与 xlsx 比对。
"""
from __future__ import annotations

import json
import pathlib

import openpyxl
import pytest

REPO_ROOT = pathlib.Path(__file__).resolve().parents[3]
XLSX_23 = REPO_ROOT / "黄金集人工复核工作单-23条.xlsx"
XLSX_254 = REPO_ROOT / "黄金集人工复核工作单-254条.xlsx"
ANNOTATIONS = pathlib.Path(__file__).resolve().parents[2] / "evaluation" / "golden" / "data" / "annotations.jsonl"


def _load_xlsx_by_id() -> dict[str, dict]:
    """合并两份 xlsx，按 golden_id 去重，保留 review_label 非空者。"""
    out: dict[str, dict] = {}
    for path in (XLSX_23, XLSX_254):
        if not path.exists():
            continue
        wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
        ws = wb.active
        rows = list(ws.iter_rows(values_only=True))
        if not rows:
            continue
        header = list(rows[0])
        cols = {name: i for i, name in enumerate(header)}
        for r in rows[1:]:
            rec = {k: r[v] for k, v in cols.items()}
            gid = rec.get("golden_id")
            if not gid or not rec.get("review_label"):
                continue
            if gid not in out or not out[gid].get("review_label"):
                out[gid] = rec
    return out


def _load_annotations() -> list[dict]:
    return [
        json.loads(l)
        for l in ANNOTATIONS.read_text(encoding="utf-8").splitlines()
        if l.strip()
    ]


@pytest.fixture(scope="module")
def xlsx_by_id() -> dict[str, dict]:
    if not (XLSX_23.exists() or XLSX_254.exists()):
        pytest.skip("xlsx 工作单不存在，b 路径无审计对象")
    return _load_xlsx_by_id()


@pytest.fixture(scope="module")
def annotations() -> list[dict]:
    return _load_annotations()


class TestManualReviewProvenance:
    def test_至少一份xlsx存在(self):
        assert XLSX_23.exists() or XLSX_254.exists(), (
            "23/254 份 xlsx 都不存在，b 路径无审计对象"
        )

    def test_manual_review_条数与xlsx_labeled_一致(self, xlsx_by_id, annotations):
        """77 manual-review ↔ 78 xlsx labeled（差 1 = 5 placeholders + 1 追溯），
        但 manual-review 子集必须 <= xlsx labeled（不能多）。"""
        mr = [a for a in annotations if a.get("reviewed_by") == "manual-review"]
        assert len(mr) <= len(xlsx_by_id), (
            f"manual-review {len(mr)} 条 > xlsx labeled {len(xlsx_by_id)} 条，"
            "annotations 里有 xlsx 没有的 manual-review 入口——谁是源？"
        )

    def test_每条manual_review在xlsx有对应行(self, xlsx_by_id, annotations):
        mr_ids = {a["golden_id"] for a in annotations if a.get("reviewed_by") == "manual-review"}
        missing = sorted(gid for gid in mr_ids if gid not in xlsx_by_id)
        assert not missing, (
            f"annotations manual-review {len(mr_ids)} 条里 "
            f"{len(missing)} 条在 xlsx 找不到对应 review_label：{missing[:10]}"
        )

    def test_manual_review_label与xlsx一致_除非INC追溯(self, xlsx_by_id, annotations):
        """xlsx 写了 A，annotations 标 B，必须是 INC32/INC33 追溯（note 解释）。"""
        mr = [a for a in annotations if a.get("reviewed_by") == "manual-review"]
        bad: list[tuple[str, str, str, str]] = []
        for a in mr:
            gid = a["golden_id"]
            xl = xlsx_by_id.get(gid, {}).get("review_label")
            if xl is None:
                continue  # 上一条已 cover
            if xl == a["label"]:
                continue
            note = a.get("note") or ""
            if "INC32_NOTE" in note or "INC33_NOTE" in note or "INC" in note and "追溯" in note:
                continue
            bad.append((gid, xl, a["label"], note[:50]))
        assert not bad, f"manual-review 漂移（非追溯）：{bad}"


class Test已知的INC32追溯:
    """TPIN02__1cf5deca__2d99fba8：xlsx unreachable_hard → annotations broken。

    INC32 设计：unreachable_hard 改 broken 是 D10 偏差修缮的追溯（簇 2）。
    哪天这条测试红了，说明 INC32/INC33 追溯链又动过，需要新一批增量。"""

    def test_TPIN02_1cf5deca_2d99fba8_xlsx仍是unreachable_hard(self, xlsx_by_id):
        rec = xlsx_by_id.get("TPIN02__1cf5deca__2d99fba8")
        assert rec is not None, "xlsx 丢了 TPIN02__1cf5deca__2d99fba8 这条"
        assert rec["review_label"] == "unreachable_hard", (
            f"xlsx review_label 漂移为 {rec['review_label']}，"
            "INC32 追溯假设前提不成立"
        )

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


class Testxlsx_199_pending:
    """xlsx 254 份里 199 条 review_label 空——以前以为是「真待人工产能」。

    INC37 重新框定：人工过百后效率崩塌，验证器正是为压这个口子加的。
    199 现在已被 L3 + L4 heuristic 走一遍，30+ 全自动裁决（详见
    test_inc37_validator_pass.py）；人工复核只看 flagged 的 31 条。
    本测试退化为 xlsx 软事实门——若 199 大幅漂移，说明 xlsx 被
    重新导出过，需要重新算一遍 INC37。"""

    def test_199_pending_数字_仅记录(self, xlsx_by_id, annotations, capsys):
        if not (XLSX_23.exists() or XLSX_254.exists()):
            pytest.skip("xlsx 不存在")
        all_xlsx_rows: list[dict] = []
        for path in (XLSX_23, XLSX_254):
            if not path.exists():
                continue
            wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
            ws = wb.active
            rows = list(ws.iter_rows(values_only=True))
            if not rows:
                continue
            header = list(rows[0])
            cols = {name: i for i, name in enumerate(header)}
            for r in rows[1:]:
                rec = {k: r[v] for k, v in cols.items()}
                if rec.get("golden_id"):
                    all_xlsx_rows.append(rec)
        empty = [r for r in all_xlsx_rows if not r.get("review_label")]
        non_empty = [r for r in all_xlsx_rows if r.get("review_label")]
        # INC37 之后：annotations 里 ai-assisted-unconfirmed 应当归 0（pending
        # 集合中 189 条被升级 + 10 条 ai-assisted-inc33 重叠升为 flagged）。
        confirmed = sum(
            1 for a in annotations
            if (a.get("reviewed_by") or "").startswith("auto-validator-l3l4-v1-")
        )
        unconfirmed = sum(
            1 for a in annotations
            if a.get("reviewed_by") == "ai-assisted-unconfirmed"
        )
        out = (
            f"xlsx 总 {len(all_xlsx_rows)} 条："
            f"review_label 已填 {len(non_empty)} + 空 {len(empty)}。\n"
            f"annotations: auto-validator-* {confirmed} / "
            f"ai-assisted-unconfirmed {unconfirmed}。\n"
            "INC37 后 199 都已被验证器过一道；空 review_label 的 xlsx 是\n"
            "「人类复核副件」，不再压黄金集主路径。\n"
        )
        print(out, end="")
        # 软断言：pending 数应与上次审计一致（容许 ±5 的工单更新）
        assert abs(len(empty) - 199) <= 5, (
            f"xlsx 空 review_label 数 {len(empty)} 偏离 199 超过 5 条，"
            "可能 xlsx 被人增删——是否新一批？"
        )

        # INC37 后的额外断言：annotations 里 0 条 ai-assisted-unconfirmed。
        assert unconfirmed == 0, (
            f"annotations 里还有 {unconfirmed} 条 ai-assisted-unconfirmed，"
            "INC37 未全部覆盖——可能 xlsx pending 集合漂走或覆写脚本漏跑。"
        )
