"""继承式冻结的**轨迹交付层**（H-5）。

审查来源：``docs/10-黄金集评估器与反馈回路审查.md`` H-5。

五件事以前各自静默：

1. **来源读错**：继承式冻结的轨迹层仍读 live ``candidates.jsonl``/
   ``annotations.jsonl`` ⇒ v17 的交付层可能悄悄变成「今天的候选集」；
2. **manifest 不记账**：事后无法回答「这一版的轨迹层是从哪来的」；
3. **没有一致性门禁**：交付层与三档冻结行对不上也能写出去；
4. **``p2_sample`` 占位行混入站点**：没有 URL 的占位行被当成站点；
5. **缺候选不报**：``or {}`` 兜底出一条 ``code=None`` 的空壳轨迹。

本文件的守则是：**源版本有快照就原样继承**（live 文件一个字都不看）、
**没有来源就不写文件并记 manifest**、**对不上就退 2 不写**。
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


# ── 夹具：已冻结的 v7（站点行 + 轨迹快照）──────────────────────────


def _site(gid: str, *, task_id: str = "T001", run_id: str = "r1") -> dict:
    """一条站点级冻结行（形状与历史档一致：**不带** outcome_index）。"""
    return {
        "golden_id": gid,
        "source_kind": "archive_outcome",
        "task_id": task_id,
        "run_id": run_id,
        "url": f"https://{gid}.test/",
        "site_url": f"https://{gid}.test/",
        "play_page_url": "",
        "access_surface": "unrecorded",
        "code": {"branch": None, "evidence": "e"},
        "golden": {
            "label": "success",
            "label_label": "成功",
            "reviewed_by": "人核甲",
            "reviewed_at": "2026-11-01",
            "note": "",
            "dimension_scores": None,
            "access_surface": "unrecorded",
        },
        "dimension_scores": {"d2": 4},
        "scores_detail": [],
        "status": "scored",
        "persona": None,
        "retest": None,
    }


def _placeholder(gid: str) -> dict:
    """per-unit 时代的 ``p2_sample`` 占位行：没有 URL、没有现场。"""
    return {
        "golden_id": gid,
        "source_kind": "p2_sample",
        "url": "",
        "code": {"branch": "unresolved"},
        "golden": None,
        "dimension_scores": None,
        "scores_detail": [],
        "status": None,
    }


def _template(
    sites: list[dict],
    *,
    code_index: int,
    with_indices: bool,
    task_id: str = "T001",
    run_id: str = "r1",
) -> dict:
    entries = []
    for i, s in enumerate(sites):
        e = {
            "golden_id": s["golden_id"],
            "source_kind": s["source_kind"],
            "url": s["url"],
            "code": s["code"],
        }
        if with_indices:
            e["outcome_index"] = i
        entries.append(e)
    return {
        "golden_id": f"{task_id}__{run_id}",
        "source_kind": "trajectory",
        "task_id": task_id,
        "run_id": run_id,
        "trajectory_id": f"{task_id}__{run_id}",
        "access_surface": "unrecorded",
        # 模板结论：**这是「源版本快照」的判据**——live 候选写的是别的分支。
        "code": {"branch": None, "evidence": "模板的证据"},
        "code_index": code_index,
        "code_reason": "",
        "persona": None,
        "sample_id": f"{task_id}__{run_id}",
        "source_archive": f"{task_id}__{run_id}.json",
        "dimension_scores": None,
        "scores_detail": [],
        "status": "scored",
        "golden": None,
        "site_outcomes": entries,
        "splits": {"calibration": 2, "holdout": 1},
        "retest": None,
    }


def _live_traj(*, task_id: str = "T001", run_id: str = "r1", branch: str | None = "trailer_only") -> dict:
    """live 候选集里的一条轨迹行（与模板同 id、**结论不同**）。"""
    return {
        "golden_id": f"{task_id}__{run_id}",
        "source_kind": "scout_scored",
        "task_id": task_id,
        "run_id": run_id,
        "trajectory_id": f"{task_id}__{run_id}",
        "access_surface": "unrecorded",
        "code": {"branch": branch},
        "code_index": 0,
        "code_reason": "",
        "persona": None,
        "sample_id": f"{task_id}__{run_id}",
        "source_archive": f"{task_id}__{run_id}.json",
        "dimension_scores": None,
        "scores_detail": [],
        "status": "scored",
        "golden": None,
        "site_outcomes": [
            {
                "golden_id": f"{task_id}__{run_id}__live",
                "source_kind": "archive_outcome",
                "url": "https://live.test/",
                "code": {"branch": branch},
                "dimension_scores": {"d2": 2},
                "scores_detail": [],
                "status": "scored",
                "outcome_index": 0,
            }
        ],
        "retest": None,
    }


def _write_version(
    root: pathlib.Path,
    *,
    version: int,
    calib: list[dict],
    hold: list[dict],
    template: list[dict] | None = None,
    headline_unresolved: list[str] | None = None,
) -> None:
    vdir = root / f"v{version}"
    _dump(vdir / "calibration.jsonl", calib)
    _dump(vdir / "holdout.jsonl", hold)
    if template is not None:
        _dump(vdir / builder.TRAJECTORIES_FILE, template)
    manifest: dict = {
        "schema_version": "1",
        "label_schema_version": builder.LABEL_SCHEMA_VERSION,
        "version": version,
        "holdout_runs": 0,
        "seed": 42,
        "counts": {"calibration": len(calib), "holdout": len(hold)},
    }
    if headline_unresolved is not None:
        # 源版本 manifest 记的「头部未定」——继承式冻结靠它判断
        # 「快照里查不到 code_index」是合法未定还是坏数据。
        manifest["dataset"] = {
            "consistency": {"headline_unresolved": list(headline_unresolved)}
        }
    (vdir / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False),
        encoding="utf-8",
    )
    _dump(root / builder.VERSIONS_INDEX, [{"version": version}])


def _fixture(
    tmp_path: pathlib.Path,
    *,
    with_template: bool = True,
    with_indices: bool = True,
    code_index: int = 2,
    extra_calib: list[dict] | None = None,
    hold_count: int = 1,
) -> pathlib.Path:
    """v7：默认 3 个站点（2 调参 / 1 持有）+ 可选轨迹快照。"""
    root = tmp_path / "golden"
    sites = [_site("g1"), _site("g2"), _site("g3")]
    calib = [sites[0], sites[2]] + list(extra_calib or ())
    hold = [sites[1]] + [_site(f"h{i}") for i in range(2, 2 + hold_count - 1)]
    template = (
        [_template(sites, code_index=code_index, with_indices=with_indices)]
        if with_template
        else None
    )
    _write_version(root, version=7, calib=calib, hold=hold, template=template)
    return root


# ── 1. 源版本快照优先 ──────────────────────────────────────────────


def test_继承式冻结读源版本快照_不读live候选(tmp_path):
    root = _fixture(tmp_path)
    # live 候选写「同一个 id、不同结论」——源版本有快照时它一个字都不该被读
    _dump(root / builder.CANDIDATES_FILE, [_live_traj()])

    rc, out, err = _run(["--golden-root", str(root), "freeze", "--from-version", "7"])
    assert rc == 0, err

    v8 = root / "v8"
    traj = _read(v8 / builder.TRAJECTORIES_FILE)
    assert len(traj) == 1
    assert traj[0]["golden_id"] == "T001__r1"
    # 结论来自**模板**（branch=None），不是 live 候选的 trailer_only
    assert traj[0]["code"]["branch"] is None
    assert traj[0]["code"]["evidence"] == "模板的证据"

    man = json.loads((v8 / "manifest.json").read_text(encoding="utf-8"))
    assert man["dataset"]["enabled"] is True
    assert man["dataset"]["source"]["mode"] == "source_version"
    # manifest 记来源指纹（sha256 64 位）与来源类型计数
    assert len(man["dataset"]["source"]["source_trajectories_sha256"]) == 64
    assert man["dataset"]["source"]["live_candidates"] == ""
    assert man["dataset"]["build"]["sources"]["templates"] == 1
    assert man["dataset"]["build"]["trajectories"] == 1


# ── 2. 没有来源：不写文件、manifest 记账、站点冻结照常 ─────────────


def test_源版本没有轨迹层且live无轨迹行_不写空文件(tmp_path):
    root = _fixture(tmp_path, with_template=False)
    # live 候选一行都没有（历史版本只有站点行）
    rc, out, err = _run(["--golden-root", str(root), "freeze", "--from-version", "7"])
    assert rc == 0, err

    v8 = root / "v8"
    # **不写空文件**：文件存在与否就是「这一版有没有交付层」的判据
    assert not (v8 / builder.TRAJECTORIES_FILE).exists()
    assert (v8 / "calibration.jsonl").exists()  # 站点级冻结照常
    man = json.loads((v8 / "manifest.json").read_text(encoding="utf-8"))
    assert man["dataset"]["enabled"] is False
    assert "reason" in man["dataset"]["source"]
    assert "不生成" in err  # 不静默


# ── 3. 缺候选不许静默 ──────────────────────────────────────────────


def test_live候选集覆盖不全就退2_不写半份交付层(tmp_path):
    root = tmp_path / "golden"
    # v7 的站点行跨两条轨迹
    t1a, t1b = _site("g1"), _site("g2")
    t2 = _site("g3", task_id="T002", run_id="r2")
    _write_version(root, version=7, calib=[t1a, t2], hold=[t1b])
    # live 候选只覆盖 T001__r1
    _dump(root / builder.CANDIDATES_FILE, [_live_traj()])

    rc, _, err = _run(["--golden-root", str(root), "freeze", "--from-version", "7"])
    assert rc == 2
    assert "覆盖不全" in err
    assert "T002__r2" in err
    assert not (root / "v8").exists()


# ── 4. 一致性门禁 ──────────────────────────────────────────────────


def test_交付层与冻结行对不上就退2(tmp_path):
    """模板的 ``code_index`` 找不到对应站点 ⇒ 不写坏交付层。"""
    root = _fixture(tmp_path, with_indices=False, code_index=2)

    rc, _, err = _run(["--golden-root", str(root), "freeze", "--from-version", "7"])
    assert rc == 2
    assert "轨迹交付层与冻结行不一致" in err
    assert not (root / "v8").exists()


def test_头部站点没进冻结集不阻断_记未定(tmp_path):
    """``code_index`` 指向的站点在**来源**里存在、只是没被标注。

    与上一条的区别：上一条的**来源里根本没有这个下标**（明细与结论对不上，
    是坏数据）；这一条的来源里有、冻结集没有——那条站点没被人核过，
    没进三档文件。这是合法的「头部定不出」，由档闸 fail-closed 跳过，
    不该把整层冻结挡死，但必须记进 manifest。
    """
    root = _fixture(tmp_path, with_template=False, with_indices=False)
    live = _live_traj()
    live["code_index"] = 2
    # 来源三个 outcome（0/1/2）；code_index=2 指向第三条，而冻结行只有
    # 人核过的 g1/g2/g3（不带 outcome_index）。
    live["site_outcomes"] = [
        {
            "golden_id": f"T001__r1__s{i}",
            "source_kind": "archive_outcome",
            "url": f"https://s{i}.test/",
            "code": {"branch": None},
            "dimension_scores": None,
            "scores_detail": [],
            "status": None,
            "outcome_index": i,
        }
        for i in range(3)
    ]
    _dump(root / builder.CANDIDATES_FILE, [live])

    rc, _, err = _run(["--golden-root", str(root), "freeze", "--from-version", "7"])
    assert rc == 0, err

    v8 = root / "v8"
    traj = _read(v8 / builder.TRAJECTORIES_FILE)
    assert len(traj) == 1
    assert traj[0]["code_index"] == 2
    # 冻结行里没有 outcome_index=2 的站点 ⇒ 头部认不出（fail-closed 的入口）
    assert builder.rows_mod.headline_site(traj[0]) is None
    assert "定不出头部站点" in err
    man = json.loads((v8 / "manifest.json").read_text(encoding="utf-8"))
    assert "T001__r1" in man["dataset"]["consistency"]["headline_unresolved"]
    assert "T001__r1" in man["dataset"]["build"]["headline_outside_frozen"]


def test_继承式冻结_源版本已记头部未定_不阻断(tmp_path):
    """**快照的 ``site_outcomes[]`` 只装已冻结站点**（v17 实测形状）。

    ``code_index=2`` 指向的站点没进冻结集，而快照明细里只有已冻结的那些
    ⇒ 快照自己证明不了「下标 2 存在」。这时以**源版本 manifest** 的
    ``headline_unresolved`` 为准：源版本冻结时在**来源**上核过，继承结论。
    """
    root = tmp_path / "golden"
    sites = [_site(g) for g in ("g1", "g4", "g5", "g2", "g3")]
    template = _template(sites, code_index=2, with_indices=True)
    # 只留已冻结的那一条明细（真相在源版本冻结时已核过，快照不带全量）
    template["site_outcomes"] = template["site_outcomes"][:1]
    _write_version(
        root,
        version=7,
        calib=sites[:3],
        hold=sites[3:],
        template=[template],
        headline_unresolved=["T001__r1"],
    )

    rc, _, err = _run(["--golden-root", str(root), "freeze", "--from-version", "7"])
    assert rc == 0, err

    v8 = root / "v8"
    traj = _read(v8 / builder.TRAJECTORIES_FILE)
    assert len(traj) == 1
    assert traj[0]["code_index"] == 2
    # 快照里没有 outcome_index=2 的明细 ⇒ 头部认不出（fail-closed 的入口）
    assert builder.rows_mod.headline_site(traj[0]) is None
    man = json.loads((v8 / "manifest.json").read_text(encoding="utf-8"))
    assert "T001__r1" in man["dataset"]["build"]["headline_outside_frozen"]
    assert "T001__r1" in man["dataset"]["consistency"]["headline_unresolved"]


def test_继承式冻结_源版本没记头部未定_快照查不到仍退2(tmp_path):
    """反例：源版本 manifest **没有**这一条 ⇒ 说明源版本认为头部正常/坏数据
    ⇒ 快照里查不到就是真不一致，照旧退 2（不拿「可能是继承」兜底）。"""
    root = tmp_path / "golden"
    sites = [_site(g) for g in ("g1", "g4", "g5", "g2", "g3")]
    template = _template(sites, code_index=2, with_indices=True)
    template["site_outcomes"] = template["site_outcomes"][:1]
    _write_version(
        root,
        version=7,
        calib=sites[:3],
        hold=sites[3:],
        template=[template],
        headline_unresolved=[],
    )

    rc, _, err = _run(["--golden-root", str(root), "freeze", "--from-version", "7"])
    assert rc == 2
    assert "轨迹交付层与冻结行不一致" in err
    assert not (root / "v8").exists()


# ── 4b. 单行回填（--backfill-labeled-sites）────────────────────────


def _ann(gid: str, *, label: str = "success") -> dict:
    return {
        "golden_id": gid,
        "round": 1,
        "label": label,
        "label_label": "成功（可正常播放）",
        "reviewed_by": "人核甲",
        "reviewed_at": "2026-11-09",
        "note": "",
        "dimension_scores": None,
        "interval_days": None,
    }


def _live_with_new_site(*, code_index: int = 2, new_index: int = 2) -> dict:
    """live 候选：一条轨迹，既有已冻结的 g1，也有新补标注的 g9。"""
    base = _live_traj()
    base["code_index"] = code_index
    base["site_outcomes"] = [
        {
            "golden_id": "g1",
            "source_kind": "archive_outcome",
            "url": "https://g1.test/",
            "code": {"branch": None},
            "dimension_scores": None,
            "scores_detail": [],
            "status": None,
            "outcome_index": 0,
        },
        {
            "golden_id": "g9",
            "source_kind": "archive_outcome",
            "url": "https://g9.test/",
            "code": {"branch": None},
            "dimension_scores": None,
            "scores_detail": [],
            "status": None,
            "outcome_index": new_index,
        },
    ]
    return base


def _backfill_fixture(tmp_path: pathlib.Path, *, cross_split: bool) -> pathlib.Path:
    """v7：T001 的站点（g1/g2/g3）+ T900 的站点（h1/h2，凑比例用）。

    ``cross_split=False``：T001 三条全在 calibration（与 v17 的三条轨迹同形），
    T900 两条全在 holdout；
    ``cross_split=True``：T001 自己跨 calibration/holdout ⇒ 回填必须拒绝。
    两个模板都只装第一条站点的明细（快照形状）。
    """
    root = tmp_path / "golden"
    t1 = [_site(g) for g in ("g1", "g2", "g3")]
    t9 = [_site(g, task_id="T900", run_id="r900") for g in ("h1", "h2")]
    tpl1 = _template(t1, code_index=2, with_indices=True)
    tpl1["site_outcomes"] = tpl1["site_outcomes"][:1]  # 快照只装已冻结站点
    tpl9 = _template(t9, code_index=0, with_indices=True, task_id="T900", run_id="r900")
    if cross_split:
        calib = [t1[0], t1[1]]
        hold = [t1[2], *t9]
    else:
        calib = list(t1)
        hold = list(t9)
    _write_version(
        root,
        version=7,
        calib=calib,
        hold=hold,
        template=[tpl1, tpl9],
        headline_unresolved=["T001__r1"],
    )
    _dump(root / builder.CANDIDATES_FILE, [_live_with_new_site()])
    _dump(root / builder.ANNOTATIONS_FILE, [_ann("g9")])
    return root


def test_单行回填_新标注站点补进对应档并认出头站(tmp_path):
    """继承式冻结 + ``--backfill-labeled-sites``：新站点进档、头部认得出。"""
    root = _backfill_fixture(tmp_path, cross_split=False)

    rc, out, err = _run(
        [
            "--golden-root", str(root), "freeze",
            "--from-version", "7", "--backfill-labeled-sites",
        ]
    )
    assert rc == 0, err

    v8 = root / "v8"
    calib = _read(v8 / "calibration.jsonl")
    # 既有 3 行顺序不变，新行追加在末尾
    assert [r["golden_id"] for r in calib] == ["g1", "g2", "g3", "g9"]
    assert _read(v8 / "holdout.jsonl") == _read(root / "v7" / "holdout.jsonl")

    traj = [r for r in _read(v8 / builder.TRAJECTORIES_FILE) if r["golden_id"] == "T001__r1"]
    assert len(traj) == 1
    headline = builder.rows_mod.headline_site(traj[0])
    assert headline is not None  # 回填的 outcome_index 让它认得出头部
    assert headline["golden_id"] == "g9"
    assert headline["outcome_index"] == 2

    man = json.loads((v8 / "manifest.json").read_text(encoding="utf-8"))
    assert man["label_backfill"]["enabled"] is True
    assert man["label_backfill"]["added"] == {"calibration": ["g9"]}
    assert man["label_backfill"]["outcome_index_by_site"] == {"g9": 2}
    assert man["label_backfill"]["baseline_reset"] is False
    assert man["dataset"]["consistency"]["headline_unresolved"] == []
    # 回填不是「重挂」：既有行的金标没动
    assert man["label_refresh"]["enabled"] is False


def test_单行回填_轨迹跨档就拒绝(tmp_path):
    """同一轨迹的站点跨两档 ⇒ 没有唯一目标档 ⇒ 不猜、不冻结。"""
    root = _backfill_fixture(tmp_path, cross_split=True)

    rc, _, err = _run(
        [
            "--golden-root", str(root), "freeze",
            "--from-version", "7", "--backfill-labeled-sites",
        ]
    )
    assert rc == 2
    assert "跨着" in err
    assert not (root / "v8").exists()


def test_单行回填_不给flag就不加行(tmp_path):
    """默认契约不变：继承式冻结不读 live 标注，新站点不会自己冒出来。"""
    root = _backfill_fixture(tmp_path, cross_split=False)

    rc, _, err = _run(["--golden-root", str(root), "freeze", "--from-version", "7"])
    assert rc == 0, err

    v8 = root / "v8"
    calib = _read(v8 / "calibration.jsonl")
    assert [r["golden_id"] for r in calib] == ["g1", "g2", "g3"]
    man = json.loads((v8 / "manifest.json").read_text(encoding="utf-8"))
    assert man["label_backfill"]["enabled"] is False
    assert "T001__r1" in man["dataset"]["consistency"]["headline_unresolved"]


def test_单行回填_不配from_version直接报错(tmp_path):
    root = _backfill_fixture(tmp_path, cross_split=False)
    rc, _, err = _run(
        ["--golden-root", str(root), "freeze", "--backfill-labeled-sites"]
    )
    assert rc == 2
    assert "--from-version" in err


# ── 5. p2_sample 占位行不是站点 ────────────────────────────────────


def test_p2_sample占位行不进站点明细(tmp_path):
    """占位行没有 URL、没有现场，混进 ``site_outcomes[]`` 就是加了个空站点。

    调参池 3 / 持有 2（含占位行的 3/5 = 60% 不触发比例门）。
    """
    root = _fixture(
        tmp_path,
        extra_calib=[_placeholder("p2-old")],
        hold_count=2,
    )
    rc, out, err = _run(["--golden-root", str(root), "freeze", "--from-version", "7"])
    assert rc == 0, err

    v8 = root / "v8"
    traj = _read(v8 / builder.TRAJECTORIES_FILE)
    assert len(traj) == 1
    ids = [s["golden_id"] for s in traj[0]["site_outcomes"]]
    assert "p2-old" not in ids
    # 四个真站点（g1/g3 调参 + g2/h2 持有），不多不少
    assert sorted(ids) == ["g1", "g2", "g3", "h2"]

    man = json.loads((v8 / "manifest.json").read_text(encoding="utf-8"))
    assert man["dataset"]["build"]["skipped_placeholders"] == 1
    # 占位行仍留在三档文件里（继承「行内容不动」），只是不算站点
    assert "p2-old" in [r["golden_id"] for r in _read(v8 / "calibration.jsonl")]