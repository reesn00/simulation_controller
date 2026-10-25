"""D-10 标签金标的**修正链**（`annotate` 改 label → `freeze --refresh-label`）。

设计来源：``docs/设计方案/16-评估器与rubric双向校准回路方案.md`` §10 / 铁律 2
（金标不回改，``gold_defect`` 只能开新版本 + 人裁）。

为什么这条命令必须有守门测试：软维度有 ``--refresh-gold``、评分有
``--refresh-scores``，唯独 D-10 label 修正**在冻结链上没有出口**——继承式
冻结是「行内容不动」，今天改的 label 走 ``--from-version`` 永远进不了新版本。
不补这条路，Q-11「改标签 = 新版本 + 人裁」就断在最后一段。

守的是六件事：

1. **只换 label**：``golden.dimension_scores`` / ``code`` / pred 侧一字不动
   ——软维度归 ``--refresh-gold``、评分归 ``--refresh-scores``，各走各的出口；
2. **label 侧五个字段一起换**：``label`` / ``label_label`` / ``reviewed_by``
   / ``reviewed_at`` / ``note`` 是绑定的，重挂时一起更新；
3. **`--refresh-label` 只能配 `--from-version`**：不继承的冻结本来就直读
   annotations，加了它没有「带过来的旧 label」可刷；
4. **刷空退 2**：label 与冻结行完全一致 ⇒ 退 2，不白耗版本号；
5. **回归集原样冻结**：改 label 不重切回归集（C-17「一经冻结不得重切」），
   回归集指纹不变——这是本机制区别于「全新冻结」的核心价值；
6. **label 变动 = 新基线**：manifest 落 ``label_refresh.baseline_reset=true``，
   `gate` 读到就退 2（见 ``test_calibration_gate``）。
"""

import io
import json
import pathlib
from contextlib import redirect_stderr, redirect_stdout

from trajectory_pipeline.evaluation.golden import builder


# ── 工具 ────────────────────────────────────────────────────


def _run(argv: list[str]) -> tuple[int, str, str]:
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        try:
            rc = builder.main(argv)
        except SystemExit as exc:  # pragma: no cover
            rc = int(exc.code or 0)
    return rc, out.getvalue(), err.getvalue()


def _dump(path: pathlib.Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows), encoding="utf-8"
    )


def _read(path: pathlib.Path) -> list[dict]:
    return [json.loads(x) for x in path.read_text(encoding="utf-8").splitlines() if x.strip()]


def _ann(gid: str, **over) -> dict:
    row = {
        "golden_id": gid,
        "round": 1,
        "label": "success",
        "label_label": "成功",
        "reviewed_by": "标标签的人",
        "reviewed_at": "2026-10-01",
        "note": "标签理由",
        "dimension_scores": None,
    }
    row.update(over)
    return row


def _frozen(gid: str, **over) -> dict:
    row = {
        "golden_id": gid,
        "source_kind": "archive_outcome",
        "url": f"https://{gid}.test/",
        "site_url": f"https://{gid}.test/",
        "code": {"branch": "success", "evidence": f"{gid} 的证据"},
        "golden": {
            "label": "success",
            "label_label": "成功",
            "reviewed_by": "标标签的人",
            "reviewed_at": "2026-10-01",
            "note": "标签理由",
            "dimension_scores": {"d5": 4},
        },
        "dimension_scores": {"d5": 2},
        "scores_detail": [],
    }
    row.update(over)
    return row


def _fixture(tmp_path: pathlib.Path, *, version: int = 7) -> pathlib.Path:
    """造一个已冻结的小版本 + annotations（label 可覆盖）。

    调参池 3 行 / 回归集 1 行 / 持有集 2 行 = 6 行：pool_frac = (3+1)/6 = 66.7%
    落在 [60%, 70%] 区间内，不卡分层门。回归集单独 1 行，用来钉「改 label
    不重切回归集」这条。
    """
    root = tmp_path / "golden"
    vdir = root / f"v{version}"
    _dump(vdir / "calibration.jsonl", [_frozen("g1"), _frozen("g3"), _frozen("g5")])
    _dump(vdir / "holdout.jsonl", [_frozen("g2"), _frozen("g4")])
    _dump(vdir / "regression.jsonl", [_frozen("r1")])
    # 回归集指纹用真实 _rows_fingerprint 算，钉死 C-17 守卫
    reg_fp = builder._rows_fingerprint(_read(vdir / "regression.jsonl"))
    (vdir / "manifest.json").write_text(
        json.dumps(
            {
                "schema_version": "1",
                "label_schema_version": builder.LABEL_SCHEMA_VERSION,
                "version": version,
                "holdout_runs": 0,
                "seed": 42,
                "counts": {"calibration": 3, "holdout": 2, "regression": 1},
                "split": {
                    "regression_fingerprint": reg_fp,
                    "regression_seed": 20261022,
                },
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    _dump(root / builder.VERSIONS_INDEX, [{"version": version}])
    _dump(
        root / builder.ANNOTATIONS_FILE,
        [_ann("g1"), _ann("g2"), _ann("g3"), _ann("g4"), _ann("g5"), _ann("r1")],
    )
    return root


# ── freeze --refresh-label ──────────────────────────────────


def test_继承式冻结把改好的label带进新版本(tmp_path):
    root = _fixture(tmp_path)
    _dump(
        root / builder.ANNOTATIONS_FILE,
        [
            _ann("g1", label="subscription_wall", label_label="付费墙", reviewed_by="reesn"),
            _ann("g2"),
            _ann("g3", label="blocked", label_label="地区限制"),
            _ann("g4"),
            _ann("g5"),
            _ann("r1"),
        ],
    )
    rc, out, _ = _run(
        ["--golden-root", str(root), "freeze", "--from-version", "7", "--refresh-label"]
    )
    assert rc == 0
    v8 = root / "v8"
    rows = {r["golden_id"]: r for r in _read(v8 / "calibration.jsonl")}
    # label 侧五个字段一起换
    assert rows["g1"]["golden"]["label"] == "subscription_wall"
    assert rows["g1"]["golden"]["label_label"] == "付费墙"
    assert rows["g1"]["golden"]["reviewed_by"] == "reesn"
    assert rows["g3"]["golden"]["label"] == "blocked"
    # 软维度金标 / code / pred 侧一字没动
    assert rows["g1"]["golden"]["dimension_scores"] == {"d5": 4}
    assert rows["g1"]["code"] == {"branch": "success", "evidence": "g1 的证据"}
    assert rows["g1"]["dimension_scores"] == {"d5": 2}
    manifest = json.loads((v8 / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["label_refresh"]["enabled"] is True
    assert manifest["label_refresh"]["baseline_reset"] is True
    assert manifest["label_refresh"]["changed_rows"] == 2
    assert "新基线" in out


def test_改label不重切回归集_指纹不变(tmp_path):
    root = _fixture(tmp_path)
    _dump(
        root / builder.ANNOTATIONS_FILE,
        [_ann("g1", label="subscription_wall"), _ann("g2"), _ann("g3"), _ann("g4"), _ann("g5"), _ann("r1")],
    )
    _run(["--golden-root", str(root), "freeze", "--from-version", "7", "--refresh-label"])
    old_fp = json.loads((root / "v7" / "manifest.json").read_text(encoding="utf-8"))["split"]["regression_fingerprint"]
    new_fp = json.loads((root / "v8" / "manifest.json").read_text(encoding="utf-8"))["split"]["regression_fingerprint"]
    assert new_fp == old_fp  # C-17：回归集一经冻结不得重切
    # 回归集内容逐字节不变
    assert _read(root / "v8" / "regression.jsonl") == _read(root / "v7" / "regression.jsonl")


def test_refresh_label必须配from_version():
    rc, _, err = _run(["freeze", "--refresh-label"])
    assert rc == 2
    assert "--from-version" in err


def test_label没变时退2(tmp_path):
    root = _fixture(tmp_path)  # annotations 的 label 与冻结行完全一致（都是 success）
    rc, _, err = _run(
        ["--golden-root", str(root), "freeze", "--from-version", "7", "--refresh-label"]
    )
    assert rc == 2
    assert "没有需要重挂的 label" in err
    assert not (root / "v8").exists()  # 没白耗一个版本号


def test_重挂后仍然逐行可比(tmp_path):
    """`--refresh-label` 不自作主张改行集合：条数与 golden_id 顺序不变。"""
    root = _fixture(tmp_path)
    _dump(
        root / builder.ANNOTATIONS_FILE,
        [_ann("g1", label="subscription_wall"), _ann("g2"), _ann("g3"), _ann("g4"), _ann("g5"), _ann("r1")],
    )
    _run(["--golden-root", str(root), "freeze", "--from-version", "7", "--refresh-label"])
    for split in ("calibration", "holdout", "regression"):
        old = _read(root / "v7" / f"{split}.jsonl")
        new = _read(root / "v8" / f"{split}.jsonl")
        assert [r["golden_id"] for r in old] == [r["golden_id"] for r in new]


def test_没加refresh_label时label不跟着走(tmp_path):
    """继承式冻结是「行内容不动」——这条测试钉的是**现状**，不是理想。"""
    root = _fixture(tmp_path)
    _dump(
        root / builder.ANNOTATIONS_FILE,
        [_ann("g1", label="subscription_wall"), _ann("g2"), _ann("g3"), _ann("g4"), _ann("g5"), _ann("r1")],
    )
    rc, _, err = _run(["--golden-root", str(root), "freeze", "--from-version", "7"])
    assert rc == 0
    rows = _read(root / "v8" / "calibration.jsonl")
    assert rows[0]["golden"]["label"] == "success"  # 改好的 label 没跟过来
    manifest = json.loads((root / "v8" / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["label_refresh"] == {"enabled": False}
