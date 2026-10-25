"""软维度金标的**写入链**（`annotate --dimensions-only` → `freeze --refresh-gold`）。

设计来源：``docs/设计方案/16-评估器与rubric双向校准回路方案.md`` §9.5 / C-11。

为什么这两条命令必须各有守门测试：它们是**填金标这件事唯一能落地的出口**。
缺了它们，`annotations.jsonl` 里填好的分数只活在派生数据里——
`freeze --from-version` 是「行内容不动」，今天填的分数**永远进不了新版本**。

守的是六件事：

1. **只写分数**：`label` / `label_label` / `note` / 标签署名人一字不改；
2. **没填不是判不了**：整张表空格 ⇒ 退 2，不写一堆 `null`；
3. **改分要 `--force`**：同键同值幂等（重跑不算改动），不同值必须显式覆盖；
4. **`--refresh-gold` 只能配 `--from-version`**：不继承的冻结本来就直读
   annotations，加了它没有「带过来的旧金标」可刷；
5. **只换 `golden.dimension_scores`**：`golden.label` / `code` / pred 侧不动
   ——这条链上**没有东西被重判**（Q-11 裁定：不重标）；
6. **金标变动 = 新基线**：manifest 落 `gold_refresh.baseline_reset=true`，
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
        except SystemExit as exc:  # pragma: no cover - argparse 只该在用法错时退
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
            "note": "标签理由",
            "dimension_scores": None,
        },
        "dimension_scores": {"d5": 2},
        "scores_detail": [],
    }
    row.update(over)
    return row


def _fixture(tmp_path: pathlib.Path, *, version: int = 7) -> pathlib.Path:
    """造一个已冻结的小版本 + annotations（分数全 null）。

    调参池 2 行 / 持有集 1 行是不卡分层门的最小形状（60%–70%）：行数再少一点
    冻结就会被「校准占比不在区间」拦下，测的就不是本文件这几条规矩了。
    """
    root = tmp_path / "golden"
    vdir = root / f"v{version}"
    _dump(vdir / "calibration.jsonl", [_frozen("g1"), _frozen("g3")])
    _dump(vdir / "holdout.jsonl", [_frozen("g2")])
    (vdir / "manifest.json").write_text(
        json.dumps(
            {
                "schema_version": "1",
                "label_schema_version": builder.LABEL_SCHEMA_VERSION,
                "version": version,
                "holdout_runs": 0,
                "seed": 42,
                "counts": {"calibration": 1, "holdout": 1},
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    _dump(root / builder.VERSIONS_INDEX, [{"version": version}])
    _dump(root / builder.ANNOTATIONS_FILE, [_ann("g1"), _ann("g2"), _ann("g3")])
    return root


def _sheet(root: pathlib.Path, rows: list[dict], name: str = "sheet.jsonl") -> pathlib.Path:
    path = root.parent / name
    _dump(path, rows)
    return path


# ── annotate --dimensions-only ──────────────────────────────


def test_只补分数_标签一字不碰(tmp_path):
    root = _fixture(tmp_path)
    sheet = _sheet(root, [{"golden_id": "g1", "dimension": "d5", "gold_score": 4, "reviewed_by": "甲"}])
    rc, out, _ = _run(
        ["--golden-root", str(root), "annotate", "--dimensions-only", "--input", str(sheet)]
    )
    assert rc == 0
    rows = {r["golden_id"]: r for r in _read(root / builder.ANNOTATIONS_FILE)}
    assert rows["g1"]["dimension_scores"] == {"d5": 4}
    assert rows["g1"]["dimension_reviewed_by"] == "甲"
    # 标签侧原样（**尤其**是标签署名人没被分数填写人盖掉）
    assert (rows["g1"]["label"], rows["g1"]["label_label"], rows["g1"]["note"]) == (
        "success", "成功", "标签理由"
    )
    assert rows["g1"]["reviewed_by"] == "标标签的人"
    assert "refresh-gold" in out


def test_整张表空格就退2_不写一堆null(tmp_path):
    root = _fixture(tmp_path)
    sheet = _sheet(root, [{"golden_id": "g1", "dimension": "d5", "gold_score": ""}])
    rc, _, err = _run(
        ["--golden-root", str(root), "annotate", "--dimensions-only", "--input", str(sheet)]
    )
    assert rc == 2
    assert "没填不是判不了" in err
    assert _read(root / builder.ANNOTATIONS_FILE)[0]["dimension_scores"] is None


def test_没人签就退2(tmp_path):
    root = _fixture(tmp_path)
    sheet = _sheet(root, [{"golden_id": "g1", "dimension": "d5", "gold_score": 4}])
    rc, _, err = _run(
        ["--golden-root", str(root), "annotate", "--dimensions-only", "--input", str(sheet)]
    )
    assert rc == 2
    assert "reviewed_by" in err


def test_改已有分数要force(tmp_path):
    root = _fixture(tmp_path)
    _dump(
        root / builder.ANNOTATIONS_FILE,
        [_ann("g1", dimension_scores={"d5": 2}), _ann("g2"), _ann("g3")],
    )
    sheet = _sheet(root, [{"golden_id": "g1", "dimension": "d5", "gold_score": 4, "reviewed_by": "甲"}])
    rc, _, err = _run(
        ["--golden-root", str(root), "annotate", "--dimensions-only", "--input", str(sheet)]
    )
    assert rc == 2 and "--force" in err
    rc, _, _ = _run(
        [
            "--golden-root", str(root),
            "annotate", "--dimensions-only", "--input", str(sheet), "--force",
        ]
    )
    assert rc == 0
    assert _read(root / builder.ANNOTATIONS_FILE)[0]["dimension_scores"] == {"d5": 4}


def test_重跑同一张表是幂等跳过(tmp_path):
    root = _fixture(tmp_path)
    sheet = _sheet(root, [{"golden_id": "g1", "dimension": "d5", "gold_score": 4, "reviewed_by": "甲"}])
    argv = ["--golden-root", str(root), "annotate", "--dimensions-only", "--input", str(sheet)]
    _run(argv)
    rc, out, _ = _run(argv)
    assert rc == 0
    assert "无变化" in out


def test_补丁形态也能导(tmp_path):
    root = _fixture(tmp_path)
    sheet = _sheet(root, [{"golden_id": "g2", "dimension_scores": {"d4": 3}, "reviewed_by": "乙"}])
    rc, _, _ = _run(
        ["--golden-root", str(root), "annotate", "--dimensions-only", "--input", str(sheet)]
    )
    assert rc == 0
    rows = {r["golden_id"]: r for r in _read(root / builder.ANNOTATIONS_FILE)}
    assert rows["g2"]["dimension_scores"] == {"d4": 3}
    assert rows["g1"]["dimension_scores"] is None  # 没提的行不动


# ── freeze --refresh-gold ───────────────────────────────────


def test_继承式冻结把填好的分数带进新版本(tmp_path):
    root = _fixture(tmp_path)
    _dump(
        root / builder.ANNOTATIONS_FILE,
        [
            _ann("g1", dimension_scores={"d5": 4, "d4": None}),
            _ann("g2"),
            _ann("g3", dimension_scores={"d5": 3}),
        ],
    )
    rc, out, _ = _run(
        ["--golden-root", str(root), "freeze", "--from-version", "7", "--refresh-gold"]
    )
    assert rc == 0
    v8 = root / "v8"
    rows = _read(v8 / "calibration.jsonl")
    assert rows[0]["golden"]["dimension_scores"] == {"d5": 4, "d4": None}
    # 标签金标与 pred 侧一个字都没动
    assert rows[0]["golden"]["label"] == "success"
    assert rows[0]["code"] == {"branch": "success", "evidence": "g1 的证据"}
    assert rows[0]["dimension_scores"] == {"d5": 2}
    manifest = json.loads((v8 / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["gold_refresh"]["enabled"] is True
    assert manifest["gold_refresh"]["baseline_reset"] is True
    assert manifest["gold_refresh"]["changed_rows"] == 2
    assert "新基线" in out


def test_refresh_gold必须配from_version():
    rc, _, err = _run(["freeze", "--refresh-gold"])
    assert rc == 2
    assert "--from-version" in err


def test_一条分数都没有时退2(tmp_path):
    root = _fixture(tmp_path)  # annotations 分数全是 null
    rc, _, err = _run(
        ["--golden-root", str(root), "freeze", "--from-version", "7", "--refresh-gold"]
    )
    assert rc == 2
    assert "一条软维度分数都没有" in err
    assert not (root / "v8").exists()  # 没白耗一个版本号


def test_重挂后仍然逐行可比(tmp_path):
    """`--refresh-gold` 不自作主张改行集合：条数与 golden_id 顺序不变。"""
    root = _fixture(tmp_path)
    _dump(
        root / builder.ANNOTATIONS_FILE,
        [
            _ann("g1", dimension_scores={"d5": 4}),
            _ann("g2"),
            _ann("g3", dimension_scores={"d5": 3}),
        ],
    )
    _run(["--golden-root", str(root), "freeze", "--from-version", "7", "--refresh-gold"])
    for split in ("calibration", "holdout"):
        old = _read(root / "v7" / f"{split}.jsonl")
        new = _read(root / "v8" / f"{split}.jsonl")
        assert [r["golden_id"] for r in old] == [r["golden_id"] for r in new]
        # 除了 golden.dimension_scores，整行逐字节相同
        for a, b in zip(old, new):
            a2 = json.loads(json.dumps(a))
            b2 = json.loads(json.dumps(b))
            a2["golden"].pop("dimension_scores")
            b2["golden"].pop("dimension_scores")
            assert a2 == b2


def test_没加refresh_gold时金标不跟着走(tmp_path):
    """继承式冻结是「行内容不动」——这条测试钉的是**现状**，不是理想。"""
    root = _fixture(tmp_path)
    _dump(
        root / builder.ANNOTATIONS_FILE,
        [_ann("g1", dimension_scores={"d5": 4}), _ann("g2"), _ann("g3")],
    )
    rc, _, err = _run(["--golden-root", str(root), "freeze", "--from-version", "7"])
    assert rc == 0
    rows = _read(root / "v8" / "calibration.jsonl")
    assert rows[0]["golden"]["dimension_scores"] is None  # 填好的分数没跟过来
    manifest = json.loads((root / "v8" / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["gold_refresh"] == {"enabled": False}
    assert "未刷新评分" in err  # 分数过期的既有告警还在