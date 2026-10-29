"""**观测面**的写回链（`annotate` 补 ``access_surface`` → `freeze --refresh-surface`）。

设计来源：``docs/设计方案/20-反爬拦截与金标观测面.md`` §R2（**金标加
``access_surface`` / ``observed_at``，旧行 ``unrecorded``**）。

为什么这条出口必须有
--------------------
写入口（``builder annotate``）从 2026-10-26 起**强制**这两栏——「断言一个
标签就得说出在哪张面上、什么时候看到的」。但**继承式冻结是「行内容不动」**
⇒ 今天声明的面走 ``--from-version`` 永远进不了新版本。``--refresh-label``
虽能带上这两栏，却**只在 label 变了的时候**才跑（label 没变 ⇒ 退 2「刷空等于
什么都没做」）⇒ 于是「人复核了一遍、标签一个字没改、只补记了在哪张面上看的」
这件事在冻结链上**没有出口**。

⇒ v13 / v14 的 294 行**一个都没声明**（manifest 里 ``declared: 0``），
这个出口补上之前，「金标是在哪张面上看到的」永远是 ``unrecorded``。

守的是八件事：

1. **只换面与时刻**：``label`` / ``reviewed_by`` / ``note`` /
   ``dimension_scores`` / ``code`` / pred 侧一字不动；
2. **观测面变了 ≠ 金标变了** ⇒ manifest 落 ``surface_refresh.baseline_reset
   =false`` ⇒ 本版与上一版**仍逐行可比**（与 ``--refresh-gold`` /
   ``--refresh-label`` 相反）；
3. **没声明就不写**：标注没声明面 ⇒ 行内容与源版本**逐字节相同** ⇒ C-17 的
   回归集指纹不因「多了一个 unrecorded 键」而变红；
4. **只能配 ``--from-version``**：不继承的冻结本来就直读 annotations；
5. **与 ``--refresh-label`` 互斥**：改标签与补面各走各的出口；
6. **刷空退 2**：面/时刻与冻结行完全一致 ⇒ 退 2，不白耗版本号；
7. **label 冲突退 2**：那条要改标签 ⇒ 走 ``--refresh-label``（新基线）；
8. **回归集不得改动（C-17）**：回归集里的行要改面 ⇒ 退 2。
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
    """标注行。

    ⚠️ **默认不声明面**——而且是**缺键**形态：v13 / v14 的标注行里压根没有
    ``access_surface`` / ``observed_at`` 这两栏（旧行不回填，读入口落
    ``unrecorded``）。要声明就显式给 ``access_surface=`` + ``observed_at=``。
    """
    row = {
        "golden_id": gid,
        "round": 1,
        "label": "success",
        "label_label": "成功",
        "reviewed_by": "manual-review",
        "reviewed_at": "2026-10-01",
        "note": "标签理由",
        "dimension_scores": None,
    }
    row.update(over)
    return row


def _frozen(gid: str, **over) -> dict:
    """冻结行。

    ⚠️ **golden 里没有 access_surface / observed_at 两个键**——与 v13 / v14
    的实际存档同形（「旧行不回填」，读入口落 ``unrecorded``）。
    """
    row = {
        "golden_id": gid,
        "source_kind": "archive_outcome",
        "url": f"https://{gid}.test/",
        "site_url": f"https://{gid}.test/",
        "code": {"branch": "success", "evidence": f"{gid} 的证据"},
        "golden": {
            "label": "success",
            "label_label": "成功",
            "reviewed_by": "manual-review",
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
    """造一个已冻结的小版本 + annotations。

    调参池 3 行 / 回归集 1 行 / 持有集 2 行 = 6 行：pool_frac = (3+1)/6 = 66.7%
    落在 [60%, 70%] 区间内，不卡分层门。
    """
    root = tmp_path / "golden"
    vdir = root / f"v{version}"
    _dump(vdir / "calibration.jsonl", [_frozen("g1"), _frozen("g3"), _frozen("g5")])
    _dump(vdir / "holdout.jsonl", [_frozen("g2"), _frozen("g4")])
    _dump(vdir / "regression.jsonl", [_frozen("r1")])
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


def _with_surface(root: pathlib.Path, gid: str, **over) -> None:
    """给某条标注补上观测面（其余行不动）。"""
    rows = _read(root / builder.ANNOTATIONS_FILE)
    for row in rows:
        if row["golden_id"] == gid:
            row.update(
                {
                    "access_surface": over.get("access_surface", "human-browser"),
                    "observed_at": over.get("observed_at", "2026-10-29T14:03:00+08:00"),
                }
            )
    _dump(root / builder.ANNOTATIONS_FILE, rows)


# ── freeze --refresh-surface ────────────────────────────────


def test_补面后冻结行带上观测面与时刻(tmp_path):
    root = _fixture(tmp_path)
    _with_surface(root, "g1")
    rc, out, _ = _run(
        ["--golden-root", str(root), "freeze", "--from-version", "7", "--refresh-surface"]
    )
    assert rc == 0
    rows = {r["golden_id"]: r for r in _read(root / "v8" / "calibration.jsonl")}
    assert rows["g1"]["golden"]["access_surface"] == "human-browser"
    assert rows["g1"]["golden"]["observed_at"] == "2026-10-29T14:03:00+08:00"
    # 没声明的行：一个键都不加（与源版本逐字节相同）
    assert "access_surface" not in rows["g3"]["golden"]
    assert "observed_at" not in rows["g3"]["golden"]
    assert "观测面" in out


def test_只换面与时刻_其余一字不动(tmp_path):
    root = _fixture(tmp_path)
    _with_surface(root, "g1")
    _run(["--golden-root", str(root), "freeze", "--from-version", "7", "--refresh-surface"])
    old = {r["golden_id"]: r for r in _read(root / "v7" / "calibration.jsonl")}["g1"]
    new = {r["golden_id"]: r for r in _read(root / "v8" / "calibration.jsonl")}["g1"]
    assert new["golden"]["label"] == old["golden"]["label"]
    assert new["golden"]["label_label"] == old["golden"]["label_label"]
    assert new["golden"]["reviewed_by"] == old["golden"]["reviewed_by"]
    assert new["golden"]["reviewed_at"] == old["golden"]["reviewed_at"]
    assert new["golden"]["note"] == old["golden"]["note"]
    assert new["golden"]["dimension_scores"] == old["golden"]["dimension_scores"]
    assert new["code"] == old["code"]
    assert new["dimension_scores"] == old["dimension_scores"]
    assert new["scores_detail"] == old["scores_detail"]


def test_观测面变了不等于金标变了_仍与前版可比(tmp_path):
    root = _fixture(tmp_path)
    _with_surface(root, "g1")
    _run(["--golden-root", str(root), "freeze", "--from-version", "7", "--refresh-surface"])
    manifest = json.loads((root / "v8" / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["surface_refresh"]["enabled"] is True
    assert manifest["surface_refresh"]["baseline_reset"] is False
    assert manifest["surface_refresh"]["changed_rows"] == 1
    # gate 的可比性预检只看 gold_refresh / label_refresh ⇒ 这一版不该退 2
    from trajectory_pipeline.evaluation.calibration import gate

    gate.assert_gold_comparable(root, 8)


def test_没声明面时行内容逐字节不变_回归集指纹不变(tmp_path):
    root = _fixture(tmp_path)
    _with_surface(root, "g1")
    _run(["--golden-root", str(root), "freeze", "--from-version", "7", "--refresh-surface"])
    old_fp = json.loads(
        (root / "v7" / "manifest.json").read_text(encoding="utf-8")
    )["split"]["regression_fingerprint"]
    new_fp = json.loads(
        (root / "v8" / "manifest.json").read_text(encoding="utf-8")
    )["split"]["regression_fingerprint"]
    assert new_fp == old_fp
    # 没声明面的那几行逐字节相同
    old_rows = {r["golden_id"]: r for r in _read(root / "v7" / "calibration.jsonl")}
    new_rows = {r["golden_id"]: r for r in _read(root / "v8" / "calibration.jsonl")}
    assert new_rows["g3"] == old_rows["g3"]
    assert new_rows["g5"] == old_rows["g5"]


def test_refresh_surface必须配from_version():
    rc, _, err = _run(["freeze", "--refresh-surface"])
    assert rc == 2
    assert "--from-version" in err


def test_refresh_surface与refresh_label互斥(tmp_path):
    root = _fixture(tmp_path)
    _with_surface(root, "g1")
    rc, _, err = _run(
        [
            "--golden-root",
            str(root),
            "freeze",
            "--from-version",
            "7",
            "--refresh-surface",
            "--refresh-label",
        ]
    )
    assert rc == 2
    assert "互斥" in err
    assert not (root / "v8").exists()


def test_面没变时退2(tmp_path):
    root = _fixture(tmp_path)  # 标注全都没声明面 ⇒ 冻结行也没有面 ⇒ 无可重挂
    rc, _, err = _run(
        ["--golden-root", str(root), "freeze", "--from-version", "7", "--refresh-surface"]
    )
    assert rc == 2
    assert "没有需要重挂的观测面" in err
    assert not (root / "v8").exists()  # 没白耗一个版本号


def test_显式写unrecorded也当没声明(tmp_path):
    """``unrecorded`` 是「没记」不是一张面 ⇒ 不写进行（行内容逐字节不变）。"""
    root = _fixture(tmp_path)
    rows = _read(root / builder.ANNOTATIONS_FILE)
    for row in rows:
        if row["golden_id"] == "g1":
            row["access_surface"] = "unrecorded"
            row["observed_at"] = "2026-10-29T14:03:00+08:00"
    _dump(root / builder.ANNOTATIONS_FILE, rows)
    rc, _, err = _run(
        ["--golden-root", str(root), "freeze", "--from-version", "7", "--refresh-surface"]
    )
    assert rc == 2  # 无可重挂 ⇒ 刷空退 2
    assert "没有需要重挂的观测面" in err


def test_声明了面却没时刻退2(tmp_path):
    root = _fixture(tmp_path)
    rows = _read(root / builder.ANNOTATIONS_FILE)
    for row in rows:
        if row["golden_id"] == "g1":
            row["access_surface"] = "human-browser"
            row["observed_at"] = ""
    _dump(root / builder.ANNOTATIONS_FILE, rows)
    rc, _, err = _run(
        ["--golden-root", str(root), "freeze", "--from-version", "7", "--refresh-surface"]
    )
    assert rc == 2
    assert "observed_at" in err


def test_label与冻结行不一致时退2(tmp_path):
    root = _fixture(tmp_path)
    rows = _read(root / builder.ANNOTATIONS_FILE)
    for row in rows:
        if row["golden_id"] == "g1":
            row["label"] = "subscription_wall"
            row["access_surface"] = "human-browser"
            row["observed_at"] = "2026-10-29T14:03:00+08:00"
    _dump(root / builder.ANNOTATIONS_FILE, rows)
    rc, _, err = _run(
        ["--golden-root", str(root), "freeze", "--from-version", "7", "--refresh-surface"]
    )
    assert rc == 2
    assert "--refresh-label" in err
    assert not (root / "v8").exists()


def test_回归集里的行要改面时退2(tmp_path):
    """C-17：回归集一经冻结，其**内容**不得改动。"""
    root = _fixture(tmp_path)
    _with_surface(root, "r1")
    rc, _, err = _run(
        ["--golden-root", str(root), "freeze", "--from-version", "7", "--refresh-surface"]
    )
    assert rc == 2
    assert "C-17" in err
    assert not (root / "v8").exists()


def test_只有部分行有标注也能刷_其余行原样带过(tmp_path):
    """⚠️ **真实数据的形态**：``annotations.jsonl`` 只有 2 条，冻结档有 294 行。

    ⇒ 这条出口必须能只刷「声明了面的那几行」，其余行**原样带过**
    （行集合不变）。若照抄 ``--refresh-label`` 的「缺一行就退 2」，
    它在真实数据上永远跑不通。
    """
    root = _fixture(tmp_path)
    _dump(
        root / builder.ANNOTATIONS_FILE,
        [
            _ann(
                "g1",
                access_surface="human-browser",
                observed_at="2026-10-29T14:03:00+08:00",
            )
        ],
    )
    rc, _, err = _run(
        ["--golden-root", str(root), "freeze", "--from-version", "7", "--refresh-surface"]
    )
    assert rc == 0, err
    new_rows = {r["golden_id"]: r for r in _read(root / "v8" / "calibration.jsonl")}
    old_rows = {r["golden_id"]: r for r in _read(root / "v7" / "calibration.jsonl")}
    assert new_rows["g1"]["golden"]["access_surface"] == "human-browser"
    # 没标注的行：逐字节原样
    assert new_rows["g3"] == old_rows["g3"]
    assert new_rows["g5"] == old_rows["g5"]
    # 行集合不变
    assert set(new_rows) == set(old_rows)
