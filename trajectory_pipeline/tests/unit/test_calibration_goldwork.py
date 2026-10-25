"""软维度金标**填表盘**（`calibration.goldwork`）的守门测试。

设计来源：``docs/设计方案/16-评估器与rubric双向校准回路方案.md`` §9.5。

守的是六件事：

1. **填的维度来自代码面**：`GOLD_DIMS` 从 ``provenance.DIM_FACES`` 投影，
   **不手抄**——新增一个代码维度时这里跟着长，抄一份就会漏；
2. **表里必须有可判的东西**：rubric 锚点原文（现读文档）+ 现场（P1 存档）
   + 当前评分器的 pred 与理由。缺了现场，人只能照抄 pred——那等于把尺子
   交给被量的人（C-06 踩过同一坑）；
3. **只出有样本的行**：`status != scored` 的行不进表，但**按维度单列计数**
   （`no_sample`）——混成「没人填」会让覆盖率永远上不去；
4. **内容键预填**（L-19）：已填过的格子重出表时带回来，人不用再填一遍；
5. **确定性**：同一输入两次出表逐行相同（表是派生物，不是新的真源）；
6. **表不写 `annotations.jsonl`**：写入只有一个入口
   （`annotate --dimensions-only`）。
"""

import json
import pathlib

import pytest

from trajectory_pipeline.evaluation.calibration import goldwork
from trajectory_pipeline.evaluation.calibration.provenance import DIM_FACES, FACE_CODE
from trajectory_pipeline.evaluation.calibration.worklist import WorklistError


def _row(
    gid: str = "g1",
    *,
    status: str = "scored",
    split: str = "calibration",
    source_kind: str = "archive",
    pred: dict | None = None,
    gold: dict | None = None,
    branch: str = "success",
    evidence: str = "词表命中播放控件",
) -> dict:
    return {
        "golden_id": gid,
        "source_kind": source_kind,
        "__split__": split,
        "status": status,
        "task_id": "T001",
        "run_id": "run00001",
        "url": "https://example.com/play",
        "site_url": "https://example.com",
        "play_page_url": "",
        "dimension_scores": dict(pred or {}),
        "golden": {"label": "success", "note": "", **(gold or {})},
        "code": {
            "branch": branch,
            "evidence": evidence,
            "reached_play_page": True,
            "fallback_used": False,
            "source": "llm",
        },
        "scores_detail": [
            {"dimension": dim, "value": value, "reasons": [f"{dim} 的理由"]}
            for dim, value in (pred or {}).items()
        ],
    }


_SCENE = {"found": True, "status": "located", "title": "片子标题", "steps": [], "body": "正文"}


# ── 1. 维度集 ────────────────────────────────────────────────


def test_填的维度就是代码面那几个():
    assert goldwork.GOLD_DIMS == ("d2", "d4", "d5", "d6")
    for dim in goldwork.GOLD_DIMS:
        assert DIM_FACES[dim] == FACE_CODE


def test_维度集从面表投影_不是手抄():
    # 面表里非 code 面的维度一个都不能混进来（d3/d9 是 llm 面、d10 是 collection）
    other = {dim for dim, face in DIM_FACES.items() if face != FACE_CODE}
    assert not (other & set(goldwork.GOLD_DIMS))


# ── 2. 一格该带什么 ──────────────────────────────────────────


def test_一格带锚点原文_predict_与现场():
    rows = [_row(pred={"d5": 4})]
    sheet, _ = goldwork.build_gold_worksheet(
        rows, scenes={"g1": _SCENE}, split="calibration"
    )
    cell = [r for r in sheet if r["dimension"] == "d5"][0]
    assert cell["rubric_ref"] == "D-5"
    # 锚点是 rubric 文档的原文，五档都在
    assert set(cell["anchors"]) == {"5", "4", "3", "2", "1"}
    assert all(isinstance(v, str) and v.strip() for v in cell["anchors"].values())
    # pred 侧是要判的对象，不是答案
    assert cell["pred_value"] == 4
    assert cell["pred_reasons"] == ["d5 的理由"]
    assert cell["pred_branch"] == "success"
    assert cell["scene"]["title"] == "片子标题"
    # 人填的三格
    assert cell["gold_score"] == ""
    assert cell["state"] == "to_fill"


def test_一格带存档路径_免得人自己拼(tmp_path):
    rows = [_row()]
    sheet, _ = goldwork.build_gold_worksheet(
        rows, split="calibration", pipeline_root=tmp_path
    )
    rec = sheet[0]["record"]
    # 存档名与 evidence.locate_sample 同一条口径：{task_id}__{run_id}.json
    assert rec["task_id"] == "T001"
    assert rec["run_id"] == "run00001"
    assert pathlib.Path(rec["archive"]).name == "T001__run00001.json"
    assert pathlib.Path(rec["archive"]).parent.name == tmp_path.name
    # 不在就明说不在——人能立刻分清「存档没了」和「我看不懂」
    assert rec["archive_exists"] is False
    # D-6 要核「记录字段完整性」，被核的字段本身得在表里
    assert rec["unit_url"] == "https://example.com/play"
    assert rec["site_url"] == "https://example.com"
    assert rec["reached_play_page"] is True


def test_存档在就标存在(tmp_path):
    (tmp_path / "T001__run00001.json").write_text("{}", encoding="utf-8")
    sheet, _ = goldwork.build_gold_worksheet(
        [_row()], split="calibration", pipeline_root=tmp_path
    )
    assert sheet[0]["record"]["archive_exists"] is True


def test_没有现场也照样有scene键_并明说读不到():
    rows = [_row()]
    sheet, _ = goldwork.build_gold_worksheet(rows, split="calibration")
    assert all("scene" in r for r in sheet)
    assert sheet[0]["scene"]["found"] is False


def test_锚点读不到就不编_给空dict():
    assert goldwork.anchors_of("d99") == {}


# ── 3. 只出有样本的行 ────────────────────────────────────────


def test_没样本的行不进表_但单列计数():
    rows = [
        _row("g1", status="scored"),
        _row("g2", status="missing_sample"),
        _row("g3", status="negative_pool_no_sample"),
    ]
    sheet, stats = goldwork.build_gold_worksheet(rows, split="calibration")
    assert {r["golden_id"] for r in sheet} == {"g1"}
    assert stats["no_sample"] == {"d2": 2, "d4": 2, "d5": 2, "d6": 2}


def test_轨迹级行不进表():
    rows = [_row("g1"), _row("t1", source_kind="p2_sample")]
    sheet, _ = goldwork.build_gold_worksheet(rows, split="calibration")
    assert {r["golden_id"] for r in sheet} == {"g1"}


def test_按档筛选():
    rows = [_row("g1", split="calibration"), _row("g2", split="holdout")]
    sheet, stats = goldwork.build_gold_worksheet(rows, split="calibration")
    assert {r["golden_id"] for r in sheet} == {"g1"}
    assert stats["skipped_other_split"] == 1


def test_空档位报错_不产空表():
    sheet, stats = goldwork.build_gold_worksheet(
        [_row("g1", status="missing_sample")], split="calibration"
    )
    assert sheet == []
    assert stats["rows"] == 0


# ── 4. 内容键预填（L-19）────────────────────────────────────


def test_已填的格子重出表时带回来():
    rows = [_row("g1")]
    annotations = [
        {
            "golden_id": "g1",
            "round": 1,
            "dimension_scores": {"d5": 3, "d4": None},
            "dimension_reviewed_by": "甲",
            "dimension_note": "录制链路看得见",
        }
    ]
    sheet, stats = goldwork.build_gold_worksheet(
        rows, annotations=annotations, scenes={"g1": _SCENE}, split="calibration"
    )
    by_dim = {r["dimension"]: r for r in sheet}
    assert by_dim["d5"]["gold_score"] == 3
    assert by_dim["d5"]["state"] == "filled"
    assert by_dim["d5"]["reviewed_by"] == "甲"
    assert by_dim["d5"]["gold_note"] == "录制链路看得见"
    # null 是「判不了」，不是「没填」
    assert by_dim["d4"]["state"] == "judged_na"
    assert by_dim["d2"]["state"] == "to_fill"
    assert stats["totals"] == {"to_fill": 2, "filled": 1, "judged_na": 1}


def test_冻结行里的金标也算已有():
    """历史路径：金标直接落在冻结行的 `golden.dimension_scores` 里。"""
    rows = [_row("g1", gold={"dimension_scores": {"d6": 5}})]
    sheet, _ = goldwork.build_gold_worksheet(rows, split="calibration")
    cell = [r for r in sheet if r["dimension"] == "d6"][0]
    assert cell["state"] == "filled" and cell["gold_score"] == 5


def test_多轮标注只认round1():
    rows = [_row("g1")]
    annotations = [
        {"golden_id": "g1", "round": 1, "dimension_scores": {"d5": 3}},
        {"golden_id": "g1", "round": 2, "dimension_scores": {"d5": 1}},
    ]
    sheet, _ = goldwork.build_gold_worksheet(rows, annotations=annotations, split="calibration")
    assert [r for r in sheet if r["dimension"] == "d5"][0]["gold_score"] == 3


# ── 5. 确定性 ────────────────────────────────────────────────


def test_同一输入两次出表逐行相同():
    rows = [_row("g2"), _row("g1", pred={"d5": 2})]
    a, _ = goldwork.build_gold_worksheet(rows, split="calibration")
    b, _ = goldwork.build_gold_worksheet(list(reversed(rows)), split="calibration")
    assert a == b


def test_一行一维_格数等于行数乘维度数():
    rows = [_row("g1"), _row("g2"), _row("g3")]
    sheet, stats = goldwork.build_gold_worksheet(rows, split="calibration")
    assert len(sheet) == 3 * len(goldwork.GOLD_DIMS)
    assert stats["rows"] == len(sheet)


def test_维度名不在面表就报错():
    with pytest.raises(WorklistError):
        goldwork.build_gold_worksheet([_row()], dims=["d99"], split="calibration")


# ── 6. 只读 ─────────────────────────────────────────────────


def test_出表不碰annotations(tmp_path, monkeypatch):
    """工作表是派生物：`worksheet` 只写自己那份文件。"""
    ann = tmp_path / "annotations.jsonl"
    ann.write_text('{"golden_id": "g1", "round": 1, "dimension_scores": null}\n', encoding="utf-8")
    before = ann.read_text(encoding="utf-8")

    rows = [_row("g1")]
    sheet, _ = goldwork.build_gold_worksheet(
        rows, annotations=goldwork.load_annotations(tmp_path), split="calibration"
    )
    assert sheet and ann.read_text(encoding="utf-8") == before


# ── CLI ─────────────────────────────────────────────────────


def _main(argv):
    import contextlib
    import io

    out = io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
        rc = goldwork.main(argv)
    return rc, out.getvalue()


def _fixture(tmp_path) -> dict:
    golden = tmp_path / "golden"
    vdir = golden / "v7"
    vdir.mkdir(parents=True)
    rows = [_row("g1"), _row("g2", status="missing_sample")]
    with (vdir / "calibration.jsonl").open("w", encoding="utf-8") as fh:
        for row in rows:
            fh.write(__import__("json").dumps(row, ensure_ascii=False) + "\n")
        (golden / "annotations.jsonl").write_text(
            '{"golden_id": "g1", "round": 1, "dimension_scores": null}\n', encoding="utf-8"
        )
    out = tmp_path / "calib"
    out.mkdir()
    return {"golden": str(golden), "out": str(out)}


def test_CLI_worksheet产出表_并报无样本数(tmp_path):
    paths = _fixture(tmp_path)
    rc, out = _main(
        ["worksheet", "--version", "7", "--golden-root", paths["golden"], "--out", paths["out"]]
    )
    assert rc == 0
    sheet = pathlib.Path(paths["out"]) / "v7" / goldwork.GOLD_WORKSHEET_FILE
    assert sheet.exists()
    rows = [json.loads(x) for x in sheet.read_text(encoding="utf-8").splitlines() if x.strip()]
    assert len(rows) == len(goldwork.GOLD_DIMS)
    assert "无样本(不进表) 1" in out


def test_CLI_worksheet已存在不覆盖(tmp_path):
    paths = _fixture(tmp_path)
    argv = ["worksheet", "--version", "7", "--golden-root", paths["golden"], "--out", paths["out"]]
    _main(argv)
    sheet = pathlib.Path(paths["out"]) / "v7" / goldwork.GOLD_WORKSHEET_FILE
    sheet.write_text('{"人工":"填到一半"}\n', encoding="utf-8")
    rc, err = _main(argv)
    assert rc == 2
    assert "填到一半" in sheet.read_text(encoding="utf-8")


def test_CLI_worksheet空档退2(tmp_path):
    paths = _fixture(tmp_path)
    rc, _ = _main(
        [
            "worksheet", "--version", "7", "--golden-root", paths["golden"],
            "--out", paths["out"], "--split", "holdout",
        ]
    )
    assert rc == 2


def test_CLI_status读表报进度(tmp_path):
    paths = _fixture(tmp_path)
    _main(
        ["worksheet", "--version", "7", "--golden-root", paths["golden"], "--out", paths["out"]]
    )
    rc, out = _main(["status", "--version", "7", "--out", paths["out"]])
    assert rc == 0
    assert f"待填 {len(goldwork.GOLD_DIMS)}" in out


def test_CLI_status表不存在退2(tmp_path):
    rc, _ = _main(["status", "--version", "7", "--out", str(tmp_path)])
    assert rc == 2


# ── 7. 署名分桶（提交前最后一眼）──────────────────────────────


def test_已判格按署名人分桶():
    sheet = [
        {"dimension": "d5", "state": "filled", "reviewed_by": "甲"},
        {"dimension": "d4", "state": "judged_na", "reviewed_by": "甲"},
        {"dimension": "d6", "state": "filled", "reviewed_by": "乙"},
        {"dimension": "d6", "state": "filled", "reviewed_by": ""},
        # 待填格**不算判过**——它没被人看过，不该出现在署名里
        {"dimension": "d2", "state": "to_fill", "reviewed_by": "丙"},
    ]
    stats = goldwork.summarize(sheet)
    assert stats["by_reviewer"] == {"甲": 2, "乙": 1, "(未署名)": 1}


def test_没人署名的已判格归到未署名():
    stats = goldwork.summarize(
        [{"dimension": "d5", "state": "filled", "reviewed_by": "  "}]
    )
    assert stats["by_reviewer"] == {"(未署名)": 1}


def test_CLI_status打印署名分桶(tmp_path):
    path = tmp_path / "gold_worksheet.jsonl"
    rows = [
        {"dimension": "d5", "state": "filled", "reviewed_by": "甲"},
        {"dimension": "d2", "state": "to_fill", "reviewed_by": ""},
    ]
    path.write_text(
        "\n".join(json.dumps(r, ensure_ascii=False) for r in rows) + "\n",
        encoding="utf-8",
    )
    rc, out = _main(["status", "--version", "7", "--path", str(path)])
    assert rc == 0
    assert "署名（已判格）" in out
    assert "甲: 1 格" in out
    # 待填格不在署名里（没被看过的人不该出现在「谁判的」里）
    assert "丙" not in out