"""judge 逐行读数 → T-2 / T-3 配对输入（台账 **M-56**）的守门测试。

守的是**两半能不能接上**，以及三条 fail-closed：

1. **形状**：judge 产物有两种外形（``row["d3"]`` / ``row["scores"]``），
   包装键有两种（``samples`` / ``rows``）⇒ 都要认；
2. **配对键**：``sample_id`` **不是** ``golden_id`` ⇒ 必须按
   (task_id, run_id, url) 与冻结档对上；**对不上就报出来**，不许猜；
3. **不静默**：没有分的行（``score=None``）**不进配对文件**——进了会让
   ``triggers.t2_judge_human`` 在 ``float("None")`` 上抛，整维被静默跳过；
   代码 judge 的维度（d2/d4/d5/d6）**不收**（混进来 = 两个评分者混一份读数）。
"""

from __future__ import annotations

import json
import pathlib

import pytest

from trajectory_pipeline.evaluation.calibration import judge_pairs as jp
from trajectory_pipeline.evaluation.calibration import triggers
from trajectory_pipeline.evaluation.calibration.snapshot import build_snapshot


# ── 读产物 ──────────────────────────────────────────────────────


def test_认三种包装形状(tmp_path):
    p = tmp_path / "r.json"
    p.write_text(json.dumps({"summary": {}, "samples": [{"sample_id": "a"}]}),
                 encoding="utf-8")
    assert jp.read_report(p) == [{"sample_id": "a"}]

    p.write_text(json.dumps({"rows": [{"sample_id": "b"}]}), encoding="utf-8")
    assert jp.read_report(p) == [{"sample_id": "b"}]

    p.write_text(json.dumps([{"sample_id": "c"}]), encoding="utf-8")
    assert jp.read_report(p) == [{"sample_id": "c"}]


def test_认JSONL(tmp_path):
    p = tmp_path / "r.jsonl"
    p.write_text(
        "\n".join(json.dumps({"sample_id": s}) for s in ("a", "b")) + "\n",
        encoding="utf-8",
    )
    assert jp.read_report(p) == [{"sample_id": "a"}, {"sample_id": "b"}]


def test_文件不在就抛(tmp_path):
    with pytest.raises(jp.JudgePairError):
        jp.read_report(tmp_path / "nope.json")


def test_维度名归一():
    assert jp.dim_name("D-3") == "d3"
    assert jp.dim_name("d9") == "d9"
    assert jp.dim_name(3) == "d3"
    with pytest.raises(jp.JudgePairError):
        jp.dim_name("")
    with pytest.raises(jp.JudgePairError):
        jp.dim_name("三")


def test_verdicts认两种形状():
    flat = jp.verdicts_of({"d3": {"score": 4, "reasons": ["a"]}})
    assert flat == [("d3", {"score": 4, "reasons": ["a"]})]
    nested = jp.verdicts_of(
        {"scores": [{"dimension": "D-9", "score": 5, "reasons": []}]}
    )
    assert nested == [("d9", {"dimension": "D-9", "score": 5, "reasons": []})]


def test_配对键从sample_id拆():
    assert jp.row_key({"sample_id": "T014__acaa3ef1__site-2"}) == (
        "T014",
        "acaa3ef1",
        "",
    )
    assert jp.row_key({"task_id": "T1", "run_id": "r1", "url": "u"}) == ("T1", "r1", "u")


# ── 索引 ────────────────────────────────────────────────────────


def _frozen_row(**over):
    row = {
        "golden_id": "T014__acaa3ef1__aabbccdd",
        "task_id": "T014",
        "run_id": "acaa3ef1",
        "url": "https://example.com/a",
        "source_kind": "site",
        "golden": {"label": "success", "dimension_scores": {"d3": 4}},
    }
    row.update(over)
    return row


def test_索引不收轨迹级行():
    index, by_gid = jp.build_index(
        [
            _frozen_row(),
            _frozen_row(
                golden_id="T014__acaa3ef1__site-1",
                source_kind="p2_sample",
                url="https://example.com/b",
            ),
        ]
    )
    assert index == {("T014", "acaa3ef1", "https://example.com/a"):
                     "T014__acaa3ef1__aabbccdd"}
    assert "T014__acaa3ef1__site-1" not in by_gid


# ── 转换 ────────────────────────────────────────────────────────


def _report_row(**over):
    row = {
        "sample_id": "T014__acaa3ef1__site-1",
        "url": "https://example.com/a",
        "d3": {"score": 4, "kind": "scored", "reasons": ["看到了播放入口"]},
        "d9": {"score": 5, "kind": "scored", "reasons": []},
    }
    row.update(over)
    return row


def test_配上了就出配对行():
    index, by_gid = jp.build_index([_frozen_row()])
    result = jp.convert([_report_row()], index, by_gid)
    assert len(result.pairs) == 2
    assert {p["dimension"] for p in result.pairs} == {"d3", "d9"}
    assert all(p["golden_id"] == "T014__acaa3ef1__aabbccdd" for p in result.pairs)
    assert result.matched == 1
    assert result.unmatched == () and result.no_score == ()


def test_配不上就报出来不丢():
    index, by_gid = jp.build_index([_frozen_row()])
    result = jp.convert([_report_row(url="https://example.com/zzz")], index, by_gid)
    assert result.pairs == ()
    assert len(result.unmatched) == 1
    assert "配不上" in result.unmatched[0]["reason"]


def test_没有url不许猜():
    index, by_gid = jp.build_index([_frozen_row()])
    result = jp.convert([_report_row(url="")], index, by_gid)
    assert result.pairs == ()
    assert len(result.unmatched) == 1
    assert "url" in result.unmatched[0]["reason"]


def test_没有分的行不进配对文件():
    """`score=None` 落进配对文件 ⇒ ``float("None")`` 抛 ⇒ 整维被静默跳过。"""
    index, by_gid = jp.build_index([_frozen_row()])
    result = jp.convert(
        [_report_row(d3={"score": None, "kind": "undetermined", "reasons": []})],
        index,
        by_gid,
    )
    assert [p["dimension"] for p in result.pairs] == ["d9"]
    assert len(result.no_score) == 1
    assert result.no_score[0]["dimension"] == "d3"


def test_代码judge的维度不收():
    """d2/d4/d5/d6 是**代码** judge ⇒ 不收进 judge 配对文件。"""
    index, by_gid = jp.build_index([_frozen_row()])
    result = jp.convert(
        [
            {
                "sample_id": "T014__acaa3ef1__site-1",
                "url": "https://example.com/a",
                "d2": {"score": 3, "kind": "scored", "reasons": []},
            }
        ],
        index,
        by_gid,
    )
    assert result.pairs == ()
    assert result.skipped_dims == {"d2": 1}


def test_金标侧状态分三档():
    index, by_gid = jp.build_index(
        [
            _frozen_row(golden={"label": "success", "dimension_scores": {"d3": 4}}),
            _frozen_row(
                golden_id="T014__acaa3ef1__eeff0011",
                url="https://example.com/b",
                golden={"label": "success", "dimension_scores": {"d3": None}},
            ),
            _frozen_row(
                golden_id="T014__acaa3ef1__eeff0022",
                url="https://example.com/c",
                golden={"label": "success", "dimension_scores": {"d5": 3}},
            ),
        ]
    )
    result = jp.convert(
        [
            _report_row(),
            _report_row(url="https://example.com/b", d9={"score": 5, "reasons": []}),
            _report_row(url="https://example.com/c", d9={"score": 5, "reasons": []}),
        ],
        index,
        by_gid,
    )
    assert result.gold_side["d3"] == {"有分": 1, "判不了": 1, "缺这一维": 1}


# ── 两半接得上（本模块存在的理由）──────────────────────────────


def test_产出的配对文件能被triggers读(tmp_path):
    index, by_gid = jp.build_index([_frozen_row()])
    result = jp.convert([_report_row()], index, by_gid)
    p = tmp_path / "pairs.jsonl"
    p.write_text(
        "\n".join(json.dumps(x, ensure_ascii=False) for x in result.pairs) + "\n",
        encoding="utf-8",
    )
    pairs = triggers.load_judge_pairs(p)
    assert {(x.golden_id, x.dimension) for x in pairs} == {
        ("T014__acaa3ef1__aabbccdd", "d3"),
        ("T014__acaa3ef1__aabbccdd", "d9"),
    }


def test_配上之后T2能出读数():
    """**本模块存在的理由**：以前是「测不成」，现在 d3 那一维能算出 kappa。"""
    index, by_gid = jp.build_index(
        [
            _frozen_row(),
            _frozen_row(
                golden_id="T014__acaa3ef1__eeff0011",
                url="https://example.com/b",
                golden={"label": "success", "dimension_scores": {"d3": 5}},
            ),
        ]
    )
    result = jp.convert(
        [
            _report_row(),
            _report_row(
                sample_id="T014__acaa3ef1__site-2",
                url="https://example.com/b",
                d3={"score": 5, "kind": "scored", "reasons": ["锚定到了观察"]},
                d9={"score": None, "kind": "not_evaluated", "reasons": []},
            ),
        ],
        index,
        by_gid,
    )
    pairs = [
        triggers.JudgePair(
            golden_id=x["golden_id"], dimension=x["dimension"],
            score=x["score"], reasons=tuple(x["reasons"]),
        )
        for x in result.pairs
    ]
    snapshot = build_snapshot(
        [
            {
                "golden_id": "T014__acaa3ef1__aabbccdd",
                "source_kind": "site",
                "__split__": "calibration",
                "golden": {"label": "success", "note": "", "dimension_scores": {"d3": 4}},
                "dimension_scores": {},
            },
            {
                "golden_id": "T014__acaa3ef1__eeff0011",
                "source_kind": "site",
                "__split__": "calibration",
                "golden": {"label": "success", "note": "", "dimension_scores": {"d3": 5}},
                "dimension_scores": {},
            },
        ],
        golden_version=16,
        success_label="success",
    )
    report = triggers.evaluate(snapshot, judge_pairs=pairs, split="calibration")
    t2 = next(s for s in report["signals"] if s["id"] == "T-2")
    assert t2["measurable"] is True
    # d9 没有分 ⇒ 不进配对文件 ⇒ T-2 只出 d3（**不静默假装 d9 也测了**）
    assert {r["dimension"] for r in t2["readings"]} == {"d3"}
    assert t2["readings"][0]["n"] == 2
