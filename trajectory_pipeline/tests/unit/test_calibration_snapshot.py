"""C-15 全量快照 + C-02 分歧卡的守门测试。

设计来源：``docs/设计方案/16-评估器与rubric双向校准回路方案.md`` §3.1 / §5.5 /
§5.6 / §9.2（C-15）· §9.2（C-02）。

守的是四件事：

1. **同源**：快照算出的 D-10 ``n`` / ``agreed`` 必须与 ``cmd_agreement``
   打印的逐字相等——两处过滤判据一旦漂开，头条与快照就是两个分母；
2. **五态封闭集**：``agree`` / ``detail_mismatch`` / ``disagree`` /
   ``tier_gap`` / ``ungraded``，且 ``ungraded`` **不进分母**（``None ≠ 0``）；
3. **归属清楚**：D-10 的 pred 是冻结行里的 ``code.branch``（``code:frozen_row``），
   不是当前评分器重算的；
4. **卡只派生不裁定**：分诊 / 抽样 / 处置 / 动机链槽位一律为空，状态恒 ``open``。
"""

import io
import json
import pathlib
from contextlib import redirect_stderr, redirect_stdout

import pytest

from trajectory_pipeline.evaluation.calibration import disagreement, snapshot
from trajectory_pipeline.evaluation.golden import builder
from trajectory_pipeline.evaluation.scorers import rubric_version


# ── 夹具 ──────────────────────────────────────────────────────


def _row(
    golden_id: str,
    *,
    gold_label: str = "success",
    branch: str | None = None,
    gold_dims: dict | None = None,
    pred_dims: dict | None = None,
    source_kind: str = "archive_outcome",
    evidence: str | None = None,
) -> dict:
    return {
        "golden_id": golden_id,
        "source_kind": source_kind,
        "task_id": "T001",
        "run_id": "abc12345",
        "url": f"https://{golden_id}.test/",
        "site_url": f"https://{golden_id}.test/",
        "code": {"branch": branch, "evidence": evidence},
        "golden": {
            "label": gold_label,
            "note": "人工备注",
            "dimension_scores": dict(gold_dims or {}),
        },
        "dimension_scores": dict(pred_dims or {}),
        "scores_detail": [
            {"dimension": dim, "score": score, "reasons": [f"{dim} 的理由"]}
            for dim, score in (pred_dims or {}).items()
        ],
    }


def _write_jsonl(path: pathlib.Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        for row in rows:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")


def _setup(
    tmp_path: pathlib.Path,
    *,
    calibration: list[dict],
    holdout: list[dict] | None = None,
    manifest: dict | None = None,
) -> pathlib.Path:
    root = tmp_path / "golden"
    vdir = root / "v6"
    _write_jsonl(vdir / "calibration.jsonl", calibration)
    _write_jsonl(vdir / "holdout.jsonl", holdout or [])
    meta = {"schema_version": "1", "version": 6}
    meta.update(manifest or {})
    (vdir / "manifest.json").write_text(
        json.dumps(meta, ensure_ascii=False), encoding="utf-8"
    )
    return root


def _run(argv: list[str]) -> tuple[int, str, str]:
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        rc = builder.main(argv)
    return rc, out.getvalue(), err.getvalue()


def _agreement(
    root: pathlib.Path, outdir: pathlib.Path, *extra: str
) -> tuple[int, str, str, dict]:
    json_out = outdir / "agreement.json"
    rc, stdout, stderr = _run([
        "--golden-root", str(root),
        "agreement", "--version", "6",
        "--emit-disagreements", str(outdir),
        "--json-out", str(json_out),
        *extra,
    ])
    data = json.loads(json_out.read_text(encoding="utf-8")) if json_out.exists() else {}
    return rc, stdout, stderr, data


def _load(outdir: pathlib.Path) -> tuple[dict, list[dict]]:
    snap = snapshot.load_snapshot(outdir / "v6" / "snapshot.json")
    cards = disagreement.read_cards(outdir / "v6" / "disagreements.jsonl")
    return snap, cards


# ── 1. 与 agreement 同源 ──────────────────────────────────────


def test_快照与agreement同源(tmp_path):
    root = _setup(
        tmp_path,
        calibration=[
            _row("g1"),                                          # 一致
            _row("g2", gold_label="not_play_site", branch="success"),   # 不一致
            _row("g3", gold_label="login_wall_or_blocked", branch="blocked"),  # 细分差
            _row("g4", gold_label="success", branch="success"),  # 一致
        ],
        holdout=[_row("h1", gold_label="not_play_site", branch="success")],
    )
    rc, _, _, data = _agreement(root, tmp_path / "out")
    assert rc == 0

    snap, _ = _load(tmp_path / "out")
    stats = snapshot.summarize_snapshot(snap)
    # 同源：快照的 ``d10`` 取头条档（C-04）——与命令行打印的 n/agreed 逐字相等
    assert stats["d10"]["n"] == data["n"] == 4
    assert stats["d10"]["agreed"] == data["agreed"] == 3
    # 旁置档单独成账：旧口径（三档合计）也留着，供跨版本对齐
    assert stats["d10_by_split"]["holdout"] == {"n": 1, "agreed": 0}
    assert stats["d10_all_splits"] == {"n": 5, "agreed": 3}
    assert data["legacy_denominator"]["n"] == 5


def test_轨迹级样本不进快照(tmp_path):
    root = _setup(
        tmp_path,
        calibration=[
            _row("g1"),
            _row("p2", source_kind="p2_sample"),
        ],
    )
    rc, _, _, _ = _agreement(root, tmp_path / "out")
    assert rc == 0
    snap, _ = _load(tmp_path / "out")
    assert {e["golden_id"] for e in snap["entries"]} == {"g1"}


# ── 2. 五态封闭集 ─────────────────────────────────────────────


def test_五态_细分差不算D10错误(tmp_path):
    root = _setup(
        tmp_path,
        calibration=[_row("g1", gold_label="login_wall_or_blocked", branch="blocked")],
    )
    rc, _, _, data = _agreement(root, tmp_path / "out")
    assert rc == 0
    snap, cards = _load(tmp_path / "out")
    entry = next(e for e in snap["entries"] if e["dimension"] == "d10")
    assert entry["verdict"] == "detail_mismatch"
    assert entry["delta_kind"] == "label_mismatch"
    # D-10 不算错：进 agreed，不进 disagree
    assert data["agreed"] == 1
    # 但细分差要出卡（细分对账需要它）
    assert [c["delta_kind"] for c in cards] == ["label_mismatch"]


def test_五态_等价类不同才算错(tmp_path):
    root = _setup(
        tmp_path,
        calibration=[_row("g1", gold_label="not_play_site", branch="success")],
    )
    rc, _, _, data = _agreement(root, tmp_path / "out")
    assert rc == 0
    snap, cards = _load(tmp_path / "out")
    entry = next(e for e in snap["entries"] if e["dimension"] == "d10")
    assert entry["verdict"] == "disagree"
    assert entry["delta_kind"] == "eq_class_mismatch"
    assert data["agreed"] == 0
    assert len(cards) == 1


def test_五态_一致与未判不出卡(tmp_path):
    root = _setup(
        tmp_path,
        calibration=[
            _row("g1", gold_label="success", branch="success"),
            _row("g2", pred_dims={"d4": 5}),  # 金标侧没有 d4 → ungraded
        ],
    )
    rc, _, _, _ = _agreement(root, tmp_path / "out")
    assert rc == 0
    snap, cards = _load(tmp_path / "out")
    verdicts = {e["verdict"] for e in snap["entries"]}
    assert "agree" in verdicts and "ungraded" in verdicts
    assert cards == []


# ── 3. 软维度 ────────────────────────────────────────────────


def test_软维度_档差超容忍记tier_gap并挂金标档条款号(tmp_path):
    root = _setup(
        tmp_path,
        calibration=[_row("g1", gold_dims={"d4": 5}, pred_dims={"d4": 2})],
    )
    rc, _, _, _ = _agreement(root, tmp_path / "out")
    assert rc == 0
    snap, cards = _load(tmp_path / "out")
    entry = next(e for e in snap["entries"] if e["dimension"] == "d4")
    assert entry["verdict"] == "tier_gap"
    assert entry["delta_kind"] == "tier_gap"
    assert entry["tier_gap"] == 3
    # 条款号按**金标档**取（金标 5 档 ⇒ D-4.5）
    assert entry["rubric_ref"] == "D-4.5"
    assert entry["pred_reasons"] == ["d4 的理由"]
    assert cards[0]["rubric_ref"] == "D-4.5"


def test_软维度_容忍内一致但仍记档差(tmp_path):
    root = _setup(
        tmp_path,
        calibration=[_row("g1", gold_dims={"d4": 4}, pred_dims={"d4": 5})],
    )
    rc, _, _, _ = _agreement(root, tmp_path / "out")
    assert rc == 0
    snap, _ = _load(tmp_path / "out")
    entry = next(e for e in snap["entries"] if e["dimension"] == "d4")
    assert entry["verdict"] == "agree"
    assert entry["delta_kind"] is None
    assert entry["tier_gap"] == 1  # 事实留下，判定不受影响


def test_软维度_单侧缺失记ungraded且不进分母(tmp_path):
    root = _setup(tmp_path, calibration=[_row("g1", pred_dims={"d4": 5})])
    rc, _, _, data = _agreement(root, tmp_path / "out")
    assert rc == 0
    snap, _ = _load(tmp_path / "out")
    entry = next(e for e in snap["entries"] if e["dimension"] == "d4")
    assert entry["verdict"] == "ungraded"
    assert data["n"] == 1  # 只有 d10 进分母


def test_未登记维度留空条款号且说明原因(tmp_path):
    root = _setup(tmp_path, calibration=[_row("g1", pred_dims={"d1": 3})])
    rc, _, _, _ = _agreement(root, tmp_path / "out")
    assert rc == 0
    snap, _ = _load(tmp_path / "out")
    entry = next(e for e in snap["entries"] if e["dimension"] == "d1")
    assert entry["verdict"] == "ungraded"
    assert entry["rubric_ref"] == ""
    assert "条款号不可用" in entry["note"]


# ── 4. fail-closed ───────────────────────────────────────────


def test_未登记标签不被吞掉():
    rows = [_row("g1", gold_label="bogus_label", branch="success")]
    with pytest.raises(KeyError):
        snapshot.build_snapshot(
            rows, golden_version=6, success_label="success",
        )


def test_emit需要版本(tmp_path):
    """没有 --version 时不许落快照：快照不绑版本，回归判定无从对齐。"""
    with pytest.raises(builder.GoldenError, match="--version"):
        builder._emit_calibration_artifacts(
            rows=[_row("g1")],
            version=0,
            rubric_version="",
            tolerance=1,
            target_dir=str(tmp_path / "out"),
        )
    assert not (tmp_path / "out" / "v0").exists()


# ── 5. 卡片契约与可复现 ───────────────────────────────────────


def test_卡片分诊槽位一律留空(tmp_path):
    root = _setup(
        tmp_path,
        calibration=[_row("g1", gold_label="not_play_site", branch="success")],
    )
    rc, _, _, _ = _agreement(root, tmp_path / "out")
    assert rc == 0
    _, cards = _load(tmp_path / "out")
    card = cards[0]
    assert card["disagreement_id"] == "v6:g1:d10"
    assert card["status"] == "open"
    for key in (
        "bucket", "responsibility", "triage_by", "triage_at", "triage_evidence",
        "stratum", "sample_weight", "action", "change_ref", "verify_run",
        "net_delta", "change_kind", "expected_effect",
    ):
        assert card[key] is None, key
    assert card["rubric_ref"] == "D-10"


def test_重算逐字节相同(tmp_path):
    root = _setup(
        tmp_path,
        calibration=[
            _row("g1", gold_label="not_play_site", branch="success"),
            _row("g2", gold_label="login_wall_or_blocked", branch="blocked"),
        ],
    )
    outdir = tmp_path / "out"
    _agreement(root, outdir)
    first = (outdir / "v6" / "snapshot.json").read_bytes(), (
        outdir / "v6" / "disagreements.jsonl").read_bytes()
    _agreement(root, outdir)
    second = (outdir / "v6" / "snapshot.json").read_bytes(), (
        outdir / "v6" / "disagreements.jsonl").read_bytes()
    assert first == second


def test_快照记录尺子的三个版本(tmp_path):
    root = _setup(
        tmp_path,
        calibration=[_row("g1")],
        manifest={
            "rubric_version": rubric_version.RUBRIC_VERSION,
            "evaluator_version": rubric_version.evaluator_version(),
        },
    )
    rc, _, _, _ = _agreement(root, tmp_path / "out")
    assert rc == 0
    snap, _ = _load(tmp_path / "out")
    assert snap["meta"]["rubric_version"] == rubric_version.RUBRIC_VERSION
    assert snap["meta"]["evaluator_version"] == rubric_version.evaluator_version()
    assert snap["meta"]["golden_version"] == 6


def test_老冻结档不拿当前值顶替(tmp_path):
    root = _setup(tmp_path, calibration=[_row("g1")], manifest={})
    rc, _, _, _ = _agreement(root, tmp_path / "out")
    assert rc == 0
    snap, _ = _load(tmp_path / "out")
    assert snap["meta"]["rubric_version"] == ""


# ── 6. 条款号投影 ─────────────────────────────────────────────


def test_条款号投影():
    assert rubric_version.clause_of("d3", 5) == "D-3.5"
    assert rubric_version.clause_of("d10") == "D-10"
    assert rubric_version.clause_of("d7") == "B-2"      # 批次级：不拼档位
    assert rubric_version.clause_of("d7", 5) == "B-2"
    with pytest.raises(KeyError):
        rubric_version.clause_of("d1")
    with pytest.raises(ValueError):
        rubric_version.clause_of("d3", 9)


def test_条款号覆盖全部软维度与硬维度():
    expected = set(rubric_version.DIM_CLAUSES)
    assert {"d2", "d3", "d4", "d5", "d6", "d9", "d10"} <= expected
    from trajectory_pipeline.evaluation.golden.alignment import SOFT_DIM_NAMES

    assert SOFT_DIM_NAMES <= expected