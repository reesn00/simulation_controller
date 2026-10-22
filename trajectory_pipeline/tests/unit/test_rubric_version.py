"""`evaluation.scorers.rubric_version` + freeze/agreement 尺子字段的守门。

设计来源：``docs/设计方案/16-评估器与rubric双向校准回路方案.md`` §9.2 C-01。

C-01 的验收只有一句话：**每个冻结版本都能回答「这批数字是哪版尺子量的」**。
拆成五条机械检查：

    1. ``test_常量与rubric文档版本行一致`` —— 文档 ``**版本**: vX`` 与
       ``RUBRIC_VERSION`` 必须相等（改文档不改常量 → 红）；
    2. ``test_版本行解析失败_fail_closed`` —— 解析不出就抛，不返回空串；
    3. ``test_评估器指纹_稳定且是短哈希`` —— 同一代码两次调用同值，12 位 hex；
    4. ``test_freeze_写入尺子三字段`` —— manifest 有
       ``rubric_version`` / ``evaluator_version`` / ``holdout_runs``，
       ``versions.jsonl`` 索引也有一份 ``rubric_version``；
    5. ``test_agreement_老冻结档不拿当前值顶替`` —— v6 及更早的 manifest
       没有这两个字段，输出必须给空串并声明"未记录"，不许拿当前值顶上。
"""

import io
import json
import pathlib
from contextlib import redirect_stderr, redirect_stdout

import pytest

from trajectory_pipeline.evaluation.golden import builder
from trajectory_pipeline.evaluation.scorers import rubric_version


# ── 1. 常量 ↔ 文档 ──────────────────────────────────────────────


def test_常量与rubric文档版本行一致():
    assert rubric_version.documented_rubric_version() == rubric_version.RUBRIC_VERSION


def test_版本行解析_容忍全角冒号与空格():
    text = "# Rubric\n\n**版本**: v9.9 | **生效日期**: ____ |\n"
    assert rubric_version.documented_rubric_version(text) == "9.9"
    text_fullwidth = "**版本**：  v1.3\n"
    assert rubric_version.documented_rubric_version(text_fullwidth) == "1.3"


def test_版本行解析失败_fail_closed():
    with pytest.raises(ValueError, match="找不到版本行"):
        rubric_version.documented_rubric_version("# 没有版本行的文档\n")


# ── 2. 评估器指纹 ──────────────────────────────────────────────


def test_评估器指纹_稳定且是短哈希():
    a = rubric_version.evaluator_version()
    b = rubric_version.evaluator_version()
    assert a == b
    assert len(a) == 12
    int(a, 16)  # 是 hex


def test_评估器指纹_不把rubric版本算漏():
    """改 rubric 版本 → 指纹必须变（否则"换了尺子"在指纹上看不出来）。"""
    real = rubric_version.RUBRIC_VERSION
    try:
        before = rubric_version.evaluator_version()
        rubric_version.RUBRIC_VERSION = real + ".99"
        after = rubric_version.evaluator_version()
    finally:
        rubric_version.RUBRIC_VERSION = real
    assert before != after


# ── 3. freeze 落尺子字段 ───────────────────────────────────────


def _row(golden_id: str, label: str, *, golden_label: str | None = None) -> dict:
    """冻结候选行；``golden_label`` 非空时补上人工标签（agreement 只吃这类行）。"""
    return {
        "golden_id": golden_id,
        "source_kind": "archive_outcome",
        "task_id": "T001",
        "run_id": "abc12345",
        "url": f"https://{golden_id}.test/",
        "site_url": f"https://{golden_id}.test/",
        "play_page_url": "",
        "code": {"branch": None, "branch_label": "success"},
        "persona": None,
        "golden": {"label": golden_label} if golden_label else None,
        "retest": None,
    }


def _write_jsonl(path: pathlib.Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        for r in rows:
            fh.write(json.dumps(r, ensure_ascii=False) + "\n")


def _prepare_golden_root(tmp_path: pathlib.Path) -> pathlib.Path:
    """造 6 条已标注候选（两类标签，供分层切分）。"""
    labels = ["success", "success", "success", "broken", "broken", "broken"]
    candidates = [_row(f"g{i}", lab) for i, lab in enumerate(labels)]
    annotations = [
        {
            "golden_id": f"g{i}",
            "round": 1,
            "label": lab,
            "label_label": lab,
            "reviewed_by": "manual-review",
            "reviewed_at": "2026-10-22T00:00:00+00:00",
            "note": "",
        }
        for i, lab in enumerate(labels)
    ]
    root = tmp_path / "golden"
    _write_jsonl(root / builder.CANDIDATES_FILE, candidates)
    _write_jsonl(root / builder.ANNOTATIONS_FILE, annotations)
    return root


def _run(argv: list[str]) -> tuple[int, str, str]:
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        rc = builder.main(argv)
    return rc, out.getvalue(), err.getvalue()


def test_freeze_写入尺子三字段(tmp_path):
    root = _prepare_golden_root(tmp_path)
    rc, _, _ = _run([
        "--golden-root", str(root), "freeze", "--version", "7", "--force",
    ])
    assert rc == 0

    manifest = json.loads((root / "v7" / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["rubric_version"] == rubric_version.RUBRIC_VERSION
    assert manifest["evaluator_version"] == rubric_version.evaluator_version()
    assert manifest["holdout_runs"] == 0
    # label_schema_version 不破（C-01 只加字段）
    assert manifest["label_schema_version"] == builder.LABEL_SCHEMA_VERSION

    index = [
        json.loads(line)
        for line in (root / builder.VERSIONS_INDEX).read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    assert index[-1]["rubric_version"] == rubric_version.RUBRIC_VERSION


# ── 4. agreement 输出尺子字段 ─────────────────────────────────


def _setup_vdir(tmp_path: pathlib.Path, *, with_ruler: bool) -> pathlib.Path:
    root = tmp_path / "golden"
    vdir = root / "v6"
    rows = [_row("g1", "success", golden_label="success")]
    _write_jsonl(vdir / "calibration.jsonl", rows)
    _write_jsonl(vdir / "holdout.jsonl", [_row("g2", "success", golden_label="success")])
    manifest = {
        "schema_version": "1",
        "label_schema_version": "2",
        "version": 6,
        "counts": {"calibration": 1, "holdout": 1},
    }
    if with_ruler:
        manifest.update({
            "rubric_version": rubric_version.RUBRIC_VERSION,
            "evaluator_version": rubric_version.evaluator_version(),
            "holdout_runs": 1,
        })
    (vdir / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False), encoding="utf-8",
    )
    return root


def test_agreement_输出含尺子字段(tmp_path):
    root = _setup_vdir(tmp_path, with_ruler=True)
    out_json = tmp_path / "agr.json"
    rc, stdout, _ = _run([
        "--golden-root", str(root), "agreement", "--version", "6",
        "--json-out", str(out_json),
    ])
    assert rc == 0
    data = json.loads(out_json.read_text(encoding="utf-8"))
    assert data["rubric_version"] == rubric_version.RUBRIC_VERSION
    assert data["evaluator_version"] == rubric_version.evaluator_version()
    assert data["evaluator_version_at_freeze"] == rubric_version.evaluator_version()
    assert data["holdout_runs"] == 1
    assert data["rows_by_split"] == {"calibration": 1, "regression": 0, "holdout": 1}
    # C-04：头条分母只算调参池；持有集旁置（旧口径读数另存 legacy_denominator）。
    assert data["d10_denominator"] == {
        "n": 1,
        "split": "calibration",
        "excluded_site_rows": {"holdout": 1},
    }
    assert data["legacy_denominator"] == {
        "n": 2,
        "agreed": 2,
        "agreement_ratio": 1.0,
        "criterion": "all_splits",
    }
    assert data["code_branch_provenance"] == "frozen_row"
    assert "rubric v" in stdout


def test_agreement_老冻结档不拿当前值顶替(tmp_path):
    """v6 及更早的 manifest 没有尺子字段 → 输出空串 + 明说「未记录」。"""
    root = _setup_vdir(tmp_path, with_ruler=False)
    out_json = tmp_path / "agr.json"
    rc, stdout, _ = _run([
        "--golden-root", str(root), "agreement", "--version", "6",
        "--json-out", str(out_json),
    ])
    assert rc == 0
    data = json.loads(out_json.read_text(encoding="utf-8"))
    assert data["rubric_version"] == ""
    assert data["evaluator_version_at_freeze"] == ""
    assert data["holdout_runs"] == 0
    assert "rubric 版本未记录" in stdout


def test_agreement_持有集旁置且旧口径同时输出(tmp_path):
    """C-04：头条换分母之后，①持有集只报条数 ②旧口径读数必须同时出现。

    只换分母不给旧口径，跨版本对比会把 289→188 读成"质量暴跌"；
    只给旧口径不换分母，封存纪律（D-C5）就还是纸面的。
    """
    root = _setup_vdir(tmp_path, with_ruler=True)
    _, stdout, _ = _run(["--golden-root", str(root), "agreement", "--version", "6"])
    assert "头条，分母 = calibration 1 条" in stdout
    assert "旧口径，三档合计 2 条" in stdout
    assert "旁置（不进头条）：regression 0 条 · holdout 1 条" in stdout
    assert "持有集 1 条只报条数、不报一致率" in stdout
    # 旧版那句「分母含 holdout」是 C-04 落地前的状态描述，必须已经不存在
    assert "本次 D10 分母含 holdout" not in stdout