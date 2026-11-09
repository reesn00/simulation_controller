"""继承式冻结的**轨迹级重挂**与**逐行分数守卫**（H-7 / H-8）。

审查来源：``docs/10-黄金集评估器与反馈回路审查.md`` H-7 / H-8。

H-7 —— ``--refresh-label`` 只刷三档文件（站点行）。per-Run 之后人核主要
落在**轨迹级**标注上 ⇒ 轨迹级改判**永远不生效**；更糟的是站点行没变时
``changed_rows == 0`` 会先退 2，把「轨迹其实变了」盖成「什么都没做」。
另有一个读数缺陷：``rows_matched`` 记的是 ``len(after_by_label)``（≈ label
种数），读起来像「只重挂了十来行」，实际是几百行。

H-8 —— ``--refresh-gold`` 的 ``filled`` 是**全局**守卫：只要有一条标注
给了分数就放行，随后把**没给分数的行**的 ``golden.dimension_scores``
写成 ``None``。人只给 3 个站点填分、其余 291 行 label-only，软维度金标
就被静默清空，还宣告「新基线」。

本文件钉住四件事：

1. 轨迹级 ``golden=None`` 的行，``--refresh-label`` 后改判生效；
2. 站点行没变、轨迹行变了 ⇒ **不再退 2**；``rows_matched`` 是**行数**；
3. 部分行有分、其余 label-only ⇒ 其余行的分数原样保留；
4. 分数与冻结行完全一致 ⇒ 退 2 不耗版本号；``baseline_reset`` 只在真变了时为真。
"""

from __future__ import annotations

import io
import json
import pathlib
from contextlib import redirect_stderr, redirect_stdout

from trajectory_pipeline.evaluation.golden import builder


# ── 工具 ────────────────────────────────────────────────────────────


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
        "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows),
        encoding="utf-8",
    )


def _read(path: pathlib.Path) -> list[dict]:
    return [
        json.loads(x)
        for x in path.read_text(encoding="utf-8").splitlines()
        if x.strip()
    ]


# ── 夹具：v7 = 3 个站点行 + 1 条轨迹快照 ───────────────────────────


def _site(
    gid: str, *, label: str = "success", dims: dict | None = None
) -> dict:
    return {
        "golden_id": gid,
        "source_kind": "archive_outcome",
        "task_id": "T001",
        "run_id": "r1",
        "url": f"https://{gid}.test/",
        "site_url": f"https://{gid}.test/",
        "play_page_url": "",
        "access_surface": "unrecorded",
        "code": {"branch": None, "evidence": "e"},
        "golden": {
            "label": label,
            "label_label": "成功",
            "reviewed_by": "人核甲",
            "reviewed_at": "2026-11-01",
            "note": "",
            "dimension_scores": dims,
            "access_surface": "unrecorded",
        },
        "dimension_scores": {"d2": 4},
        "scores_detail": [],
        "status": "scored",
        "persona": None,
        "retest": None,
    }


def _template(sites: list[dict]) -> dict:
    return {
        "golden_id": "T001__r1",
        "source_kind": "trajectory",
        "task_id": "T001",
        "run_id": "r1",
        "trajectory_id": "T001__r1",
        "access_surface": "unrecorded",
        "code": {"branch": None, "evidence": "模板的证据"},
        "code_index": 0,
        "code_reason": "",
        "persona": None,
        "sample_id": "T001__r1",
        "source_archive": "T001__r1.json",
        "dimension_scores": None,
        "scores_detail": [],
        "status": "scored",
        # 人还没核过整条轨迹——轨迹级 golden 是空的（H-7 的主场景）
        "golden": None,
        "site_outcomes": [
            {
                "golden_id": s["golden_id"],
                "source_kind": s["source_kind"],
                "url": s["url"],
                "code": s["code"],
                "outcome_index": i,
            }
            for i, s in enumerate(sites)
        ],
        "splits": {"calibration": 2, "holdout": 1},
        "retest": None,
    }


def _write_version(
    root: pathlib.Path,
    *,
    version: int,
    calib: list[dict],
    hold: list[dict],
    template: list[dict] | None,
) -> None:
    vdir = root / f"v{version}"
    _dump(vdir / "calibration.jsonl", calib)
    _dump(vdir / "holdout.jsonl", hold)
    if template is not None:
        _dump(vdir / builder.TRAJECTORIES_FILE, template)
    (vdir / "manifest.json").write_text(
        json.dumps(
            {
                "schema_version": "1",
                "label_schema_version": builder.LABEL_SCHEMA_VERSION,
                "version": version,
                "holdout_runs": 0,
                "seed": 42,
                "counts": {"calibration": len(calib), "holdout": len(hold)},
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    _dump(root / builder.VERSIONS_INDEX, [{"version": version}])


def _fixture(
    tmp_path: pathlib.Path,
    *,
    site_dims: dict[str, dict | None] | None = None,
    with_template: bool = True,
) -> tuple[pathlib.Path, list[dict]]:
    """v7：g1/g2 调参 + g3 持有（2/3 = 66.7% 过比例门）。

    ``site_dims`` 指定各站点 ``golden.dimension_scores`` 的初值；
    不给就是「软维度金标还没填」。
    """
    root = tmp_path / "golden"
    dims = site_dims or {}
    sites = [
        _site(g, dims=dims.get(g)) for g in ("g1", "g2", "g3")
    ]
    _write_version(
        root,
        version=7,
        calib=[sites[0], sites[1]],
        hold=[sites[2]],
        template=[_template(sites)] if with_template else None,
    )
    return root, sites


def _ann(
    gid: str,
    *,
    label: str = "success",
    dims: dict | None = None,
    reviewed_by: str = "人核甲",
    reviewed_at: str = "2026-11-01",
) -> dict:
    return {
        "golden_id": gid,
        "round": 1,
        "label": label,
        "label_label": "成功",
        "reviewed_by": reviewed_by,
        "reviewed_at": reviewed_at,
        "note": f"{gid} 的理由",
        "dimension_scores": dims,
    }


def _site_anns(**overrides: dict) -> list[dict]:
    """三条站点标注（默认与冻结行一致）。"""
    return [
        _ann(g, **overrides.get(g, {})) for g in ("g1", "g2", "g3")
    ]


def _manifest(root: pathlib.Path, version: int = 8) -> dict:
    return json.loads(
        (root / f"v{version}" / "manifest.json").read_text(encoding="utf-8")
    )


# ── H-7：轨迹级 --refresh-label ────────────────────────────────────


def test_轨迹级改判生效_站点行没变也不退2(tmp_path):
    """站点行一个字没动，但**轨迹行**的 label 变了 ⇒ 刷成功、进新版本。

    修前：``_refresh_label`` 只刷站点行 ⇒ ``changed_rows == 0`` ⇒ 退 2
    「没有需要重挂的 label」，轨迹级改判永远进不了冻结链。
    """
    root, _ = _fixture(tmp_path)
    _dump(
        root / builder.ANNOTATIONS_FILE,
        _site_anns()
        + [
            _ann(
                "T001__r1",
                label="no_play_control",
                reviewed_by="人核乙",
                reviewed_at="2026-11-09",
            )
        ],
    )

    rc, out, err = _run(
        [
            "--golden-root", str(root),
            "freeze", "--from-version", "7", "--refresh-label",
        ]
    )
    assert rc == 0, err

    traj = _read(root / "v8" / builder.TRAJECTORIES_FILE)
    assert len(traj) == 1
    assert traj[0]["golden"]["label"] == "no_play_control"
    assert traj[0]["golden"]["reviewed_by"] == "人核乙"
    assert traj[0]["golden"]["reviewed_at"] == "2026-11-09"

    # 站点行原样（label 没变）
    for split in ("calibration", "holdout"):
        for row in _read(root / "v8" / f"{split}.jsonl"):
            assert row["golden"]["label"] == "success"

    info = _manifest(root)["label_refresh"]
    assert info["enabled"] is True
    assert info["changed_rows"] == 1
    # ⚠ **行数**，不是 label 种数：3 条站点行 + 1 条轨迹行
    assert info["rows_matched"] == 4
    assert info["trajectory_rows_matched"] == 1
    assert info["trajectory_changed_rows"] == 1
    assert info["baseline_reset"] is True


def test_轨迹行没有标注就原样跳过(tmp_path):
    """不是每一整条轨迹都被人核过——没有 round=1 标注的轨迹行不报错、不动。"""
    root, _ = _fixture(tmp_path)
    _dump(
        root / builder.ANNOTATIONS_FILE,
        _site_anns(g1={"label": "blocked", "reviewed_by": "人核乙"}),
    )

    rc, _, err = _run(
        [
            "--golden-root", str(root),
            "freeze", "--from-version", "7", "--refresh-label",
        ]
    )
    assert rc == 0, err
    traj = _read(root / "v8" / builder.TRAJECTORIES_FILE)
    assert traj[0]["golden"] is None  # 没人核过，不凭空写 golden

    info = _manifest(root)["label_refresh"]
    assert info["trajectory_rows_matched"] == 0
    assert info["trajectory_changed_rows"] == 0
    assert info["changed_rows"] == 1  # 只有 g1 站点行变了
    assert info["rows_matched"] == 3


# ── H-8：逐行守卫，部分填分不清空其余行 ───────────────────────────


def test_部分站点有分_其余行分数原样保留(tmp_path):
    """人只给 g1 填了分：g2/g3 的金标分数**不被写成 None**。

    修前：全局 ``filled`` 通过（g1 有分），随后 g2/g3 的
    ``golden.dimension_scores`` 被无条件写成 ``None`` —— 291 行级的静默清空。
    """
    root, _ = _fixture(
        tmp_path,
        site_dims={"g1": {"d5": 2}, "g2": {"d5": 4}, "g3": {"d5": 4}},
    )
    _dump(
        root / builder.ANNOTATIONS_FILE,
        _site_anns(g1={"dims": {"d5": 5}}),  # 只有 g1 给了新分数
    )

    rc, _, err = _run(
        [
            "--golden-root", str(root),
            "freeze", "--from-version", "7", "--refresh-gold",
        ]
    )
    assert rc == 0, err

    rows = {
        r["golden_id"]: r
        for split in ("calibration", "holdout")
        for r in _read(root / "v8" / f"{split}.jsonl")
    }
    assert rows["g1"]["golden"]["dimension_scores"] == {"d5": 5}   # 只改了这行
    assert rows["g2"]["golden"]["dimension_scores"] == {"d5": 4}   # 不清空
    assert rows["g3"]["golden"]["dimension_scores"] == {"d5": 4}

    info = _manifest(root)["gold_refresh"]
    assert info["changed_rows"] == 1
    assert info["rows_matched"] == 3
    assert info["baseline_reset"] is True


def test_只有轨迹级分数_不清空站点行(tmp_path):
    """分数只填在**轨迹级**标注上：站点行的金标分数原样保留。

    修前：``filled`` 把轨迹级标注也算进来 ⇒ 守卫通过、站点行被清成 ``None``，
    而循环又只改站点行 ⇒ 轨迹级分数根本没写进去（两头落空）。
    """
    root, _ = _fixture(
        tmp_path,
        site_dims={"g1": {"d5": 2}, "g2": {"d5": 4}, "g3": {"d5": 4}},
    )
    _dump(
        root / builder.ANNOTATIONS_FILE,
        _site_anns()
        + [_ann("T001__r1", dims={"d5": 5})],
    )

    rc, _, err = _run(
        [
            "--golden-root", str(root),
            "freeze", "--from-version", "7", "--refresh-gold",
        ]
    )
    assert rc == 0, err

    rows = {
        r["golden_id"]: r
        for split in ("calibration", "holdout")
        for r in _read(root / "v8" / f"{split}.jsonl")
    }
    for gid in ("g1", "g2", "g3"):
        assert rows[gid]["golden"]["dimension_scores"] == {"d5": 4 if gid != "g1" else 2}

    traj = _read(root / "v8" / builder.TRAJECTORIES_FILE)
    assert traj[0]["golden"]["dimension_scores"] == {"d5": 5}

    info = _manifest(root)["gold_refresh"]
    assert info["trajectory_rows_matched"] == 1
    assert info["trajectory_changed_rows"] == 1
    assert info["changed_rows"] == 1  # 合计：站点 0 + 轨迹 1


def test_分数与冻结行一致就退2_不耗版本号(tmp_path):
    """所有给了分的行都与冻结行相同 ⇒ 刷空，退 2，不写 v8。

    以前 ``changed_rows == 0`` 没有任何守卫——白白用掉一个版本号、
    还落一个 ``baseline_reset: true`` 的假新基线。
    """
    root, _ = _fixture(
        tmp_path,
        site_dims={"g1": {"d5": 2}, "g2": {"d5": 4}, "g3": {"d5": 4}},
    )
    _dump(
        root / builder.ANNOTATIONS_FILE,
        _site_anns(
            g1={"dims": {"d5": 2}},
            g2={"dims": {"d5": 4}},
            g3={"dims": {"d5": 4}},
        ),
    )

    rc, _, err = _run(
        [
            "--golden-root", str(root),
            "freeze", "--from-version", "7", "--refresh-gold",
        ]
    )
    assert rc == 2
    assert "没有需要重挂的金标" in err
    assert not (root / "v8").exists()


def test_没给分数的行连行内容都不动(tmp_path):
    """逐行守卫的强形式：label-only 标注不让该行**逐字节**变化。

    C-17 的回归集指纹拿「内容没变」当证据——写一个空 dict / None
    进去都会让指纹变红。
    """
    root, _ = _fixture(
        tmp_path,
        site_dims={"g1": {"d5": 2}, "g2": {"d5": 4}, "g3": {"d5": 4}},
    )
    _dump(
        root / builder.ANNOTATIONS_FILE,
        _site_anns(g1={"dims": {"d5": 5}}),
    )
    before = {g: dict(r) for g, r in {
        r["golden_id"]: r
        for split in ("calibration", "holdout")
        for r in _read(root / "v7" / f"{split}.jsonl")
    }.items()}

    rc, _, err = _run(
        [
            "--golden-root", str(root),
            "freeze", "--from-version", "7", "--refresh-gold",
        ]
    )
    assert rc == 0, err

    after = {
        r["golden_id"]: r
        for split in ("calibration", "holdout")
        for r in _read(root / "v8" / f"{split}.jsonl")
    }
    assert after["g2"] == before["g2"]  # 逐字节（含 golden 子字典）一致
    assert after["g3"] == before["g3"]