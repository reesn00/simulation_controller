"""**版本级出处台账 + 面感知可比性**的契约测试（方案 16 C-11 补齐）。

这一组盯的是 C-11 留下的那半个缺口：**存量冻结档 v1–v8 的行里没有
``scorer_version``**，而冻结档一字不许改 ⇒ 「这批 pred 是哪版评分器打的」
对存量数据本来答不出来。

补法不是重跑 ``overlay-scout``（那是改数），而是**档外**的版本级台账：
重算当前代码面、逐行比对存档里的读数，把结论记成
「复现得出来（可比）/ 复现不出来（**已知不可比**）/ 没得比（不可判定）」。

同时盯住**面**这件事：代码面（D-2/D-4/D-5/D-6）/ prompt 面（D-3/D-9）/
采集侧（D-10 的 ``code.branch``）是三件事。一律拿 prompt 面去比，就是
**问错了问题**——而问错问题的答案看起来完全正常。
"""

from __future__ import annotations

import argparse
import json
import pathlib

import pytest

from trajectory_pipeline.evaluation.calibration import disagreement, provenance
from trajectory_pipeline.evaluation.calibration import snapshot as snap_mod
from trajectory_pipeline.evaluation.golden import builder
from trajectory_pipeline.evaluation.scorers import rubric_version

#: 仓库根（测试文件在 ``trajectory_pipeline/tests/contract/`` 下）。
_REPO_ROOT = pathlib.Path(__file__).resolve().parents[3]
#: 黄金集数据根。
_GOLDEN = _REPO_ROOT / "trajectory_pipeline" / "evaluation" / "golden" / "data"


# ── 面：投影与「不猜」 ───────────────────────────────────────────


def test_面按维度投影():
    """面是维度的函数——**唯一真源**在 :data:`DIM_FACES`，不许各写一份。"""
    assert provenance.face_of_dimension("d2") == provenance.FACE_CODE
    assert provenance.face_of_dimension("d4") == provenance.FACE_CODE
    assert provenance.face_of_dimension("d5") == provenance.FACE_CODE
    assert provenance.face_of_dimension("d6") == provenance.FACE_CODE
    assert provenance.face_of_dimension("d3") == provenance.FACE_LLM
    assert provenance.face_of_dimension("d9") == provenance.FACE_LLM
    assert provenance.face_of_dimension("d10") == provenance.FACE_COLLECTION


def test_批次级维度不落任何一面():
    """``d7`` / ``d8`` 是**批次级**（B-2 / B-4），不出逐行分数。

    它们**不登记** ⇒ 落 ``unknown`` ⇒ 不可判定。**不许**猜它们属于哪一面。
    """
    for dim in ("d7", "d8"):
        assert provenance.face_of_dimension(dim) == provenance.FACE_UNKNOWN
    assert provenance.face_of_dimension("没登记的维") == provenance.FACE_UNKNOWN
    assert provenance.face_of_dimension("") == provenance.FACE_UNKNOWN


def test_指纹两段都能取出来():
    """``code:…|llm:…``——两面各取各的段；取不到写 ``unrecorded``。"""
    fp = rubric_version.scorer_fingerprint()
    assert rubric_version.code_part(fp) == rubric_version.code_scorer_version()
    assert rubric_version.llm_part(fp) == rubric_version.evaluator_version()
    assert rubric_version.code_part("unrecorded") == "unrecorded"
    assert rubric_version.code_part("") == "unrecorded"
    # 只有 llm 段的指纹（老格式）⇒ code 段取不到 ⇒ unrecorded，不返回空串。
    assert rubric_version.code_part("llm:abc") == "unrecorded"


def test_采集侧没有版本可取():
    """采集面**从来没有过版本**——判定侧版本从未被指纹化。"""
    fp = rubric_version.scorer_fingerprint()
    assert provenance.face_version_of(fp, provenance.FACE_COLLECTION) == "unrecorded"
    assert provenance.face_version_of(fp, provenance.FACE_CODE) == (
        rubric_version.code_part(fp)
    )
    assert provenance.face_version_of(fp, provenance.FACE_LLM) == (
        rubric_version.llm_part(fp)
    )


# ── 台账：读写与 fail-closed ─────────────────────────────────────


def test_台账读写往返(tmp_path):
    """写进去读出来一字不差（顺序按版本号升序 ⇒ 重写不产生 diff 噪声）。"""
    rows = [
        {"version": 2, "method": "recompute", "faces": {}},
        {"version": 1, "method": "recompute", "faces": {}},
    ]
    path = provenance.write_provenance(tmp_path / "p.jsonl", rows)
    back = provenance.read_provenance(path)
    assert sorted(back) == [1, 2]
    assert back[1]["method"] == "recompute"
    assert [r["version"] for r in
            provenance.read_provenance(path).values()] == [1, 2]


def test_台账不存在读成空字典(tmp_path):
    """没建过台账 = 没建过，**不是错误**（旁路账不该拦住测量）。"""
    assert provenance.read_provenance(tmp_path / "不存在的台账.jsonl") == {}


def test_台账行读坏就抛(tmp_path):
    """半份台账比没有台账更坏 ⇒ 读坏抛 ``ValueError``，不静默跳过那行。"""
    p = tmp_path / "坏台账.jsonl"
    p.write_text('{"version": 1}\n这不是 JSON\n', encoding="utf-8")
    with pytest.raises(ValueError):
        provenance.read_provenance(p)


def test_台账缺某一面时按未记录处理():
    """缺一条**不让链路断**，但也不许把「没记」读成「一致」。"""
    rec = provenance.face_record({"version": 8, "faces": {}}, provenance.FACE_CODE)
    assert rec["status"] == provenance.STATUS_UNRECORDED
    assert rec["comparable"] is None
    assert rec["version"] == "unrecorded"
    # 连 faces 都没有的行也一样。
    rec2 = provenance.face_record({"version": 8}, provenance.FACE_CODE)
    assert rec2["status"] == provenance.STATUS_UNRECORDED


# ── 快照：三栏（面 / 版本 / 怎么定的） ────────────────────────────


def _row(**over):
    row = {
        "golden_id": "E001__a__b",
        "golden": {"label": "success", "dimension_scores": {"d4": 5}},
        "code": {"branch": "success"},
        "dimension_scores": {"d4": 3},
    }
    row.update(over)
    return row


def test_快照条目带面三栏():
    snap = snap_mod.build_snapshot(
        [_row()], golden_version=8, success_label="success",
    )
    entry = next(e for e in snap["entries"] if e["dimension"] == "d4")
    assert entry["pred_face"] == provenance.FACE_CODE
    assert entry["pred_face_status"] == provenance.STATUS_UNRECORDED
    assert entry["pred_face_version"] == "unrecorded"


def test_硬分类pred出自采集侧():
    """D-10 的 pred 是**采集时**写下的 branch——不是评分器的产物。"""
    snap = snap_mod.build_snapshot(
        [_row(dimension_scores={})], golden_version=8, success_label="success",
    )
    entry = next(e for e in snap["entries"] if e["dimension"] == "d10")
    assert entry["pred_face"] == provenance.FACE_COLLECTION
    assert entry["pred_face_status"] == provenance.STATUS_NOT_APPLICABLE
    assert entry["pred_provider"] == "code:frozen_row@unrecorded"


def test_行里记了版本时面版本从指纹里取():
    snap = snap_mod.build_snapshot(
        [_row(scorer_version="code:abc|llm:def")],
        golden_version=8, success_label="success",
    )
    entry = next(e for e in snap["entries"] if e["dimension"] == "d4")
    assert entry["pred_face_version"] == "abc"
    assert entry["pred_face_status"] == "recorded"
    # 出处仍是**全指纹**（C-11 的形状不变）。
    assert entry["pred_provider"] == "code:row.dimension_scores@code:abc|llm:def"


def test_存量行靠台账补出代码面版本():
    """台账说「当前代码面逐行复现了本版读数」⇒ 补出版本，且**只补那一面**。"""
    prov = {
        8: {
            "version": 8,
            "faces": {
                provenance.FACE_CODE: {
                    "version": "codefp01",
                    "status": provenance.STATUS_REPRODUCED,
                    "comparable": True,
                },
            },
        }
    }
    snap = snap_mod.build_snapshot(
        [_row()], golden_version=8, success_label="success", provenance=prov,
    )
    entry = next(e for e in snap["entries"] if e["dimension"] == "d4")
    assert entry["pred_face_version"] == "codefp01"
    assert entry["pred_face_status"] == provenance.STATUS_REPRODUCED
    assert entry["pred_provider"] == "code:row.dimension_scores@code:codefp01"


def test_台账说复现不出时状态是diverged():
    """**已知不可比**——不是「不可判定」。"""
    prov = {
        8: {
            "version": 8,
            "faces": {
                provenance.FACE_CODE: {
                    "version": "unrecorded",
                    "status": provenance.STATUS_DIVERGED,
                    "comparable": False,
                },
            },
        }
    }
    snap = snap_mod.build_snapshot(
        [_row()], golden_version=8, success_label="success", provenance=prov,
    )
    entry = next(e for e in snap["entries"] if e["dimension"] == "d4")
    assert entry["pred_face_status"] == provenance.STATUS_DIVERGED
    assert entry["pred_face_version"] == "unrecorded"


def test_没有台账时不拿当前指纹顶替():
    """**不猜**：台账没有 ⇒ ``unrecorded``，不写当前代码面的指纹。

    顶替会让「没量到」看起来像「量过且一致」——M-29 那类误读。
    """
    snap = snap_mod.build_snapshot(
        [_row()], golden_version=8, success_label="success",
    )
    entry = next(e for e in snap["entries"] if e["dimension"] == "d4")
    assert entry["pred_face_version"] == "unrecorded"
    assert entry["pred_provider"] == "code:row.dimension_scores@unrecorded"


# ── 可比性：按面判定 + 理由 ──────────────────────────────────────


def _card(face, version, status, current):
    return {
        "pred_face": face,
        "pred_face_version": version,
        "pred_face_status": status,
        "current_face_version": current,
    }


def test_代码面同版可比():
    card = _card(provenance.FACE_CODE, "abc", "recorded", "abc")
    assert disagreement.pred_comparability(card) is True


def test_代码面异版不可比():
    card = _card(provenance.FACE_CODE, "abc", "recorded", "xyz")
    assert disagreement.pred_comparability(card) is False


def test_复现不出判不可比而不是不可判定():
    """``diverged`` ⇒ **已知不可比**（False），不是「答不出来」（None）。"""
    card = _card(provenance.FACE_CODE, "unrecorded",
                 provenance.STATUS_DIVERGED, "xyz")
    assert disagreement.pred_comparability(card) is False


def test_版本没记到时判不出来():
    card = _card(provenance.FACE_CODE, "unrecorded",
                 provenance.STATUS_UNRECORDED, "xyz")
    assert disagreement.pred_comparability(card) is None


def test_采集侧pred比不了且理由是不适用():
    """D-10 的 pred 不是评分器的产物 ⇒ **不适用**（不是「不可判定」）。"""
    card = _card(provenance.FACE_COLLECTION, "unrecorded",
                 provenance.STATUS_NOT_APPLICABLE, "")
    assert disagreement.pred_comparability(card) is None
    reason = disagreement.pred_comparability_reason(card)
    assert "不适用" in reason
    assert "采集侧" in reason


def test_可比性理由总能说清():
    """三态答案**必须带理由**——没有理由的三态是半份答案。"""
    for card in (
        _card(provenance.FACE_CODE, "abc", "recorded", "abc"),
        _card(provenance.FACE_CODE, "abc", "recorded", "xyz"),
        _card(provenance.FACE_CODE, "unrecorded", provenance.STATUS_UNRECORDED, "x"),
        _card(provenance.FACE_CODE, "unrecorded", provenance.STATUS_DIVERGED, "x"),
        _card(provenance.FACE_COLLECTION, "unrecorded",
              provenance.STATUS_NOT_APPLICABLE, ""),
    ):
        reason = disagreement.pred_comparability_reason(card)
        assert reason.strip(), card
        assert disagreement.pred_comparability(card) in (True, False, None)


def test_卡按维度投影面即使条目没带():
    """手搓的快照（条目没带 ``pred_face``）也能投影出面——投影不是猜。"""
    snap = {
        "meta": {"golden_version": 8, "rubric_version": "1.2",
                 "evaluator_version": "llmNEW", "code_version": "codeNEW"},
        "entries": [
            {
                "golden_id": "E001__a__b",
                "split": "calibration",
                "dimension": "d3",
                "rubric_ref": "D-3.5",
                "gold_value": 5,
                "pred_value": 3,
                "pred_provider": "code:row.dimension_scores@code:old|llm:llmOLD",
                "verdict": "disagree",
            }
        ],
    }
    card = disagreement.cards_from_snapshot(snap)[0]
    assert card["pred_face"] == provenance.FACE_LLM
    assert card["pred_face_version"] == "llmOLD"
    assert disagreement.pred_comparability(card) is False


def test_代码面的卡比的是代码面不是prompt面():
    """**面感知**：代码面的 pred 不能拿 prompt 面去比。

    问错问题的答案看起来完全正常——所以这条必须钉死。
    """
    snap = {
        "meta": {"golden_version": 8, "rubric_version": "1.2",
                 "evaluator_version": "llmNEW", "code_version": "codeNEW"},
        "entries": [
            {
                "golden_id": "E001__a__b",
                "split": "calibration",
                "dimension": "d5",
                "rubric_ref": "D-5.5",
                "gold_value": 5,
                "pred_value": 3,
                "pred_provider": "code:row.dimension_scores@code:codeOLD|llm:llmNEW",
                "verdict": "tier_gap",
            }
        ],
    }
    card = disagreement.cards_from_snapshot(snap)[0]
    assert card["pred_face"] == provenance.FACE_CODE
    assert card["pred_face_version"] == "codeOLD"
    assert card["current_face_version"] == "codeNEW"
    # prompt 面是**同一版**（llmNEW == llmNEW），但代码面不同 ⇒ 不可比。
    assert card["pred_prompt_version"] == "llmNEW"
    assert disagreement.pred_comparability(card) is False


def test_可比性结论写在卡上不是只在函数里():
    """**接线**：结论与理由必须**落在卡上**。

    只定义函数不写进产物 ⇒ 答案只活在「会调用它的地方」，而没有那种地方
    ⇒ 等于没答（这个坑本仓库踩过：一个没生效的检查和一个真红长得一样）。
    卡是**给人看的产物** ⇒ 读的人不必先知道有那两个函数。
    """
    snap = {
        "meta": {"golden_version": 7, "rubric_version": "1.2",
                 "evaluator_version": "llmNEW", "code_version": "codeNEW"},
        "entries": [
            {
                "golden_id": "E001__a__b",
                "split": "calibration",
                "dimension": "d5",
                "rubric_ref": "D-5.5",
                "gold_value": 5,
                "pred_value": 3,
                "pred_provider": "code:row.dimension_scores@code:codeOLD|llm:llmNEW",
                "verdict": "tier_gap",
            },
            {
                "golden_id": "E001__c__d",
                "split": "calibration",
                "dimension": "d10",
                "rubric_ref": "D-10.1",
                "gold_value": "success",
                "pred_value": "no_play_control",
                "pred_provider": "code:frozen_row@unrecorded",
                "verdict": "disagree",
            },
        ],
    }
    cards = disagreement.cards_from_snapshot(snap)
    by_dim = {c["dimension"]: c for c in cards}
    # 代码面异版 ⇒ 不可比；采集侧 ⇒ 不适用。**两态都写在卡上**。
    assert by_dim["d5"]["pred_comparability"] is False
    assert by_dim["d5"]["pred_comparability_reason"].strip()
    assert by_dim["d10"]["pred_comparability"] is None
    assert "不适用" in by_dim["d10"]["pred_comparability_reason"]
    # 卡上的结论与函数算出来的**同一个**（不许各算各的）。
    for c in cards:
        assert c["pred_comparability"] == disagreement.pred_comparability(c)
        assert c["pred_comparability_reason"] == \
            disagreement.pred_comparability_reason(c)


def test_可比性一路走到人看的那张表():
    """答案要**走到人面前**：卡 → worklist 行 → 工作表，三处都在。

    停在卡上也没用——人判桶时看的是**工作表**。少了最后一跳，
    「不是同一版判的」这条现场就到不了判桶的人眼前。
    """
    from trajectory_pipeline.evaluation.calibration import worklist

    snap = {
        "meta": {"golden_version": 7, "rubric_version": "1.2",
                 "evaluator_version": "llmNEW", "code_version": "codeNEW"},
        "entries": [
            {
                "golden_id": "E001__a__b",
                "split": "calibration",
                "dimension": "d5",
                "rubric_ref": "D-5.5",
                "gold_value": 5,
                "pred_value": 3,
                "pred_provider": "code:row.dimension_scores@code:codeOLD|llm:llmNEW",
                "verdict": "tier_gap",
            }
        ],
    }
    cards = disagreement.cards_from_snapshot(snap)
    rows = [{
        "golden_id": "E001__a__b",
        "code": {"branch": "unresolved", "source": "llm"},
        "golden": {"label": "success"},
    }]
    wl = worklist.build_worklist(cards, rows)
    assert wl[0]["pred_comparability"] is False
    assert wl[0]["pred_comparability_reason"].strip()
    sheet = worklist.build_worksheet(wl)
    assert sheet[0]["pred_comparability"] is False
    assert sheet[0]["pred_comparability_reason"].strip()
    # 三处**同一个**结论——不许各算各的。
    assert sheet[0]["pred_comparability"] == cards[0]["pred_comparability"]


# ── 重算取证 ─────────────────────────────────────────────────────


def test_重算逐行比对能判出复现与分歧(monkeypatch):
    """重算：全部一致 ⇒ ``reproduced``；有一处不同 ⇒ ``diverged``。

    这里把「取样本」与「打分」都替掉，测的是**比对逻辑本身**。
    """
    import trajectory_pipeline.evaluation.scout.io as scout_io

    def _fake_resolve(row, p2_root):
        return object()

    def _fake_score(sample):
        # 第三个样本（golden_id 以 3 结尾）故意给不同的分数。
        return [
            {"dimension": "d2", "score": 5},
            {"dimension": "d4", "score": 4 if sample is None else 4},
            {"dimension": "d5", "score": 5},
            {"dimension": "d6", "score": None},
        ]

    monkeypatch.setattr(builder, "_resolve_sample_for_candidate", _fake_resolve)
    monkeypatch.setattr(scout_io, "_score_one", _fake_score)

    rows = [
        {"golden_id": "a1", "dimension_scores": {"d2": 5, "d4": 4, "d5": 5, "d6": None}},
    ]
    ev = builder._recompute_code_face(rows, p2_root=pathlib.Path("."))
    assert ev["rows_compared"] == 1
    assert ev["rows_matched"] == 1
    assert ev["rows_diverged"] == 0

    rows_bad = [
        {"golden_id": "a1", "dimension_scores": {"d2": 3, "d4": 4, "d5": 5, "d6": None}},
    ]
    ev2 = builder._recompute_code_face(rows_bad, p2_root=pathlib.Path("."))
    assert ev2["rows_matched"] == 0
    assert ev2["rows_diverged"] == 1
    assert ev2["diverged"][0]["golden_id"] == "a1"
    assert ev2["diverged"][0]["diffs"]["d2"] == {"stored": 3, "recomputed": 5}


def test_取不到样本的行不进分母(monkeypatch):
    """「比不了」的行**不算一致也不算不一致**——它是第三态，不是「一致」。"""
    import trajectory_pipeline.evaluation.scout.io as scout_io

    monkeypatch.setattr(builder, "_resolve_sample_for_candidate",
                        lambda row, p2_root: None)
    monkeypatch.setattr(scout_io, "_score_one", lambda s: [])
    rows = [{"golden_id": "a1", "dimension_scores": {"d2": 5}}]
    ev = builder._recompute_code_face(rows, p2_root=pathlib.Path("."))
    assert ev["rows_compared"] == 0
    assert ev["no_sample"] == 1


# ── 台账落库：存量冻结档确实都登记了 ─────────────────────────────


def test_存量冻结档的出处都已登记():
    """v1–v8 的行里**没有** ``scorer_version`` ⇒ 它们的出处**必须**在台账里。

    这条是「补齐」的判据：台账缺一版，那一版的 pred 出处就还是答不出来。
    """
    reg = provenance.read_provenance(provenance.provenance_path(_GOLDEN))
    for version in range(1, 9):
        assert version in reg, f"v{version} 的出处没登记"
        row = reg[version]
        assert set(row["faces"]) >= {
            provenance.FACE_CODE,
            provenance.FACE_LLM,
            provenance.FACE_COLLECTION,
        }
        for face, rec in row["faces"].items():
            assert rec["status"] in provenance.FACE_STATUSES, (version, face)


def test_存量冻结档的行确实没记评分器版本():
    """台账存在的**理由**——前提没了，这条测试就该跟着改。"""
    for version in (6, 7, 8):
        vdir = _GOLDEN / f"v{version}"
        rows = []
        for fname in ("calibration.jsonl", "regression.jsonl", "holdout.jsonl"):
            p = vdir / fname
            if not p.exists():
                continue
            rows += [
                json.loads(line)
                for line in p.read_text(encoding="utf-8").splitlines()
                if line.strip()
            ]
        assert rows, f"v{version} 读不出行"
        assert not any(r.get("scorer_version") for r in rows), (
            f"v{version} 的行里已经有 scorer_version 了——台账的补齐前提变了，"
            "该改用「行里直接读」那条路"
        )


def test_台账里复现得出来的版本与复现不出来的版本都如实记着():
    """**不许**因为「结果不好看」就把 diverged 写成 reproduced。

    存量档里既有复现得出来的（v8）也有复现不出来的（v6/v7）——
    台账要把两者**分开**记，混成一句「大致一致」就没用了。
    """
    reg = provenance.read_provenance(provenance.provenance_path(_GOLDEN))
    statuses = {
        v: reg[v]["faces"][provenance.FACE_CODE]["status"] for v in reg
    }
    assert statuses[8] == provenance.STATUS_REPRODUCED
    assert statuses[6] == provenance.STATUS_DIVERGED
    assert statuses[7] == provenance.STATUS_DIVERGED
    # v1–v5 没有软维度读数可比 ⇒ 不可判定（**不猜**）。
    for v in (1, 2, 3, 4, 5):
        assert statuses[v] == provenance.STATUS_UNRECORDED


# ── ``provenance`` 子命令：登记本身也要经得起看 ───────────────────


def _fake_version(root: pathlib.Path, version: int, rows: list[dict]) -> pathlib.Path:
    """造一个只够 ``cmd_provenance`` 读的冻结档（行 + manifest）。"""
    vdir = root / f"v{version}"
    vdir.mkdir(parents=True, exist_ok=True)
    with open(vdir / "calibration.jsonl", "w", encoding="utf-8") as fh:
        for r in rows:
            fh.write(json.dumps(r, ensure_ascii=False) + "\n")
    (vdir / "manifest.json").write_text(
        json.dumps({"evaluator_version": "llmOLD"}, ensure_ascii=False),
        encoding="utf-8",
    )
    return vdir


def test_登记台账时行数与指纹数分开记(tmp_path, capsys):
    """**行数 ≠ 指纹数**——v9 起行里真会记版本，两个字段填同一个数，
    「rows_with_scorer_version」会把「500 行记了同一版」读成「只有 1 行记了」。"""
    fp = "code:aaa|llm:bbb"
    rows = [
        {"golden_id": f"g{i}", "scorer_version": fp} for i in range(5)
    ]
    _fake_version(tmp_path, 1, rows)
    args = argparse.Namespace(
        golden_root=str(tmp_path), version=1, p2_root=str(tmp_path),
        dry_run=False,
    )
    assert builder.cmd_provenance(args) == 0
    reg = provenance.read_provenance(provenance.provenance_path(tmp_path))
    ev = reg[1]["faces"][provenance.FACE_CODE]["evidence"]
    assert ev["rows_with_scorer_version"] == 5
    assert ev["distinct_fingerprints"] == 1


def test_同一版混着两版评分器就停下(tmp_path, capsys):
    """**混面 ⇒ 退 2**，不许登记——出处登记不出来就别硬写。"""
    rows = [
        {"golden_id": "g1", "scorer_version": "code:aaa|llm:bbb"},
        {"golden_id": "g2", "scorer_version": "code:ccc|llm:bbb"},
    ]
    _fake_version(tmp_path, 1, rows)
    args = argparse.Namespace(
        golden_root=str(tmp_path), version=1, p2_root=str(tmp_path),
        dry_run=False,
    )
    assert builder.cmd_provenance(args) == 2
    assert "混着两版评分器" in capsys.readouterr().err
