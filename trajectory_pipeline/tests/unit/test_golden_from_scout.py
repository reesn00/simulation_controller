"""``from-scout → labeled → freeze → refresh-scores`` 的端到端链路（H-2）。

审查来源：``docs/10-黄金集评估器与反馈回路审查.md`` H-2。

这条链路以前**零覆盖**，四个断点各自静默：

1. scout 记录的 ``score=None`` 被写成 ``0`` ⇒ 负样本池凭空多一条；
2. 一条 run 的多条 scout 记录逐条 append ⇒ ``candidates.jsonl`` 里多个
   同一个 ``golden_id``，冻结按 id 建索引后先出现的静默消失；
3. 重写候选集前不核人核标注 ⇒ 标注悄悄掉线；
4. 站点行不带评分字段 ⇒ 冻结出来的站点行 D-10 软维度分母整个塌掉。

本文件跑的是**真实链路**（scout 打分 → from-scout 落候选 → annotate →
freeze → refresh-scores），不是对单个函数的替身测试。
"""

from __future__ import annotations

import io
import json
import pathlib
from contextlib import redirect_stderr, redirect_stdout

import pytest

from trajectory_pipeline.evaluation.golden import builder
from trajectory_pipeline.evaluation.scout import io as scout_io


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
    text = path.read_text(encoding="utf-8")
    return [json.loads(x) for x in text.splitlines() if x.strip()]


def _obs(url: str) -> dict:
    return {
        "url": url,
        "page_title": "T",
        "body_text": "正文",
        "truncated": False,
        "degraded": [],
        "video_tag_count": 0,
        "iframe_count": 0,
        "interactive_elements": [],
        "links": [],
    }


# ── 夹具：一条 run 三个访问点 ──────────────────────────────────────


def _archive(p2_root: pathlib.Path) -> pathlib.Path:
    """``T001__r1.json``：a 站失败、b/c 站成功——三个站点各一条明细。"""
    p = p2_root / "T001__r1.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(
        json.dumps(
            {
                "task_id": "T001",
                "title": "功夫",
                "user_prompt": "我要看功夫",
                "search_url": "https://s.test",
                "search_observation": _obs("https://s.test"),
                "search_blocked": "",
                "steps": [],
                "provenance": {"genre": "喜剧"},
                "run_config": {
                    "engine": "baidu",
                    "max_candidates": 20,
                    "max_chars": None,
                    "stop_after_success": 0,
                    "per_site_timeout_s": 90.0,
                },
                "candidates": [],
                "visits": [
                    {
                        "url": f"https://{n}.test", "landed_url": f"https://{n}.test",
                        "steps": [], "site_obs": _obs(f"https://{n}.test"),
                        "player_obs": None, "success": n != "a",
                    }
                    for n in ("a", "b", "c")
                ],
                "outcomes": [
                    {
                        "url": "https://a.test", "branch": "no_play_control",
                        "question": "site.find_play_control", "evidence": "e",
                    },
                    {
                        "url": "https://b.test", "branch": None,
                        "question": "site.find_play_control", "evidence": "e",
                    },
                    {
                        "url": "https://c.test", "branch": None,
                        "question": "site.find_play_control", "evidence": "e",
                    },
                ],
                "ledger": None,
                "warnings": [],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    return p


def _p2_sample(p2_root: pathlib.Path) -> pathlib.Path:
    p = p2_root / "samples" / "T001__r1.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(
        json.dumps(
            {
                "sample_id": "T001__r1",
                "task_id": "T001",
                "run_id": "r1",
                "source_archive": "T001__r1.json",
                "title": "功夫",
                "user_prompt": "我要看功夫",
                "outcomes": [],
                "provenance": {"genre": "喜剧"},
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    return p


class _V:
    """最小 verdict 替身，只带 io.py 真正读的字段。"""

    def __init__(self, score: int | None):
        self.score = score
        self.is_veto = False
        self.reasons = ()


def _install_site_scorers(monkeypatch: pytest.MonkeyPatch) -> None:
    """a 站四维差（负例）、b/c 站都好（正例）。

    按**评分单元**的 ``sample_id`` 分流——一条 run 的三个站点各打各的。
    """
    table = {
        "T001__r1__site-1": {"d2": 1, "d4": 4, "d5": 4, "d6": 4},
        "T001__r1__site-2": {"d2": 5, "d4": 5, "d5": 5, "d6": 5},
        "T001__r1__site-3": {"d2": 5, "d4": 5, "d5": 5, "d6": 5},
    }

    def mux(dim: str):
        def _f(unit):
            return _V(table[unit.sample_id][dim])
        return _f

    for dim in ("d2", "d4", "d5", "d6"):
        monkeypatch.setitem(scout_io._SCORERS, dim, mux(dim))


def _ann(gid: str, label: str = "success") -> dict:
    return {
        "golden_id": gid,
        "round": 1,
        "label": label,
        "label_label": "成功" if label == "success" else "无播放控件",
        "reviewed_by": "人核甲",
        "reviewed_at": "2026-11-01",
        "note": "理由",
        "dimension_scores": None,
    }


def _scout_record(
    site_index: int, url: str, scores: dict[str, int | None]
) -> dict:
    return {
        "sample_id": "T001__r1",
        "unit_id": f"T001__r1__site-{site_index}",
        "site_index": site_index,
        "url": url,
        "task_id": "T001",
        "run_id": "r1",
        "status": "scored",
        "scores": [
            {"dimension": d, "score": scores[d], "is_veto": False, "reasons": []}
            for d in ("d2", "d4", "d5", "d6")
        ],
        "tags": [],
        "evidence_supplement": {},
    }


# ── 主链路 ──────────────────────────────────────────────────────────


def test_from_scout到freeze到refresh_scores的整条链路(tmp_path, monkeypatch):
    """三件套 → 候选（去重）→ 标注 → 冻结 → 重挂评分。

    守卫四件事：

    1. **一条 run 一行**：3 条 scout 记录（3 个站点）合成 1 条轨迹级候选；
    2. **分数挂在站点上**：``site_outcomes[]`` 各带自己的四维分数，
       轨迹行顶层不带分数；
    3. **冻结档站点行带分数**：``_labeled_rows`` 从站点明细抄进冻结行；
    4. **``--refresh-scores`` 重挂的是站点行**：新版本分数变、轨迹层
       从源版本快照继承（``mode=source_version``）。
    """
    p2_root = tmp_path / "pipeline"
    _archive(p2_root)
    _p2_sample(p2_root)
    scout_dir = tmp_path / "scout"
    _install_site_scorers(monkeypatch)
    stats = scout_io.run_scout(p2_root, scout_dir)
    assert (stats["positive"], stats["negative"]) == (2, 1)

    golden = tmp_path / "golden"
    rc, out, err = _run(
        [
            "--golden-root", str(golden),
            "from-scout",
            "--scout-dir", str(scout_dir),
            "--p2-root", str(p2_root),
        ]
    )
    assert rc == 0, err

    # 1. 去重：3 条 scout 记录 → 1 条轨迹级候选
    cands = _read(golden / builder.CANDIDATES_FILE)
    assert len(cands) == 1
    row = cands[0]
    assert row["golden_id"] == "T001__r1"
    assert row["trajectory_id"] == "T001__r1"
    assert len(row["site_outcomes"]) == 3
    # 2. 顶层不带分数（分数只属于站点）
    assert row["dimension_scores"] is None
    assert row["scores_detail"] == []
    sites = row["site_outcomes"]
    assert [s["url"] for s in sites] == [
        "https://a.test", "https://b.test", "https://c.test"
    ]
    assert [s["visit_index"] for s in sites] == [1, 2, 3]
    assert sites[0]["dimension_scores"] == {"d2": 1, "d4": 4, "d5": 4, "d6": 4}
    assert sites[1]["dimension_scores"] == {"d2": 5, "d4": 5, "d5": 5, "d6": 5}
    assert all(s["status"] == "scored" for s in sites)

    site_ids = [s["golden_id"] for s in sites]
    _dump(
        golden / builder.ANNOTATIONS_FILE,
        [
            _ann(site_ids[0], "no_play_control"),
            _ann(site_ids[1], "success"),
            _ann(site_ids[2], "success"),
        ],
    )

    rc, out, err = _run(["--golden-root", str(golden), "freeze", "--version", "1"])
    assert rc == 0, err
    v1 = golden / "v1"
    # 3. 冻结档站点行带分数（不是空 dict）
    frozen1 = {
        r["golden_id"]: r
        for split in ("calibration", "holdout")
        for r in _read(v1 / f"{split}.jsonl")
    }
    assert set(frozen1) == set(site_ids)
    assert frozen1[site_ids[0]]["dimension_scores"]["d2"] == 1
    assert frozen1[site_ids[1]]["dimension_scores"]["d2"] == 5
    # 轨迹交付层：一条轨迹，站点各带自己的档
    traj1 = _read(v1 / builder.TRAJECTORIES_FILE)
    assert len(traj1) == 1
    assert {s["split"] for s in traj1[0]["site_outcomes"]} <= {
        "calibration", "holdout"
    }
    man1 = json.loads((v1 / "manifest.json").read_text(encoding="utf-8"))
    assert man1["dataset"]["enabled"] is True
    assert man1["dataset"]["source"]["mode"] == "live_candidates"
    assert man1["dataset"]["build"]["trajectories"] == 1

    # 4. 改 b 站的分数，继承式重冻 + 重挂评分
    row["site_outcomes"][1]["dimension_scores"]["d2"] = 4
    _dump(golden / builder.CANDIDATES_FILE, [row])
    rc, out, err = _run(
        [
            "--golden-root", str(golden),
            "freeze",
            "--from-version", "1",
            "--refresh-scores",
            "--version", "2",
        ]
    )
    assert rc == 0, err
    v2 = golden / "v2"
    frozen2 = {
        r["golden_id"]: r
        for split in ("calibration", "holdout")
        for r in _read(v2 / f"{split}.jsonl")
    }
    assert frozen2[site_ids[1]]["dimension_scores"]["d2"] == 4
    assert frozen2[site_ids[0]]["dimension_scores"]["d2"] == 1  # 别的站不跟着变
    man2 = json.loads((v2 / "manifest.json").read_text(encoding="utf-8"))
    assert man2["score_refresh"]["enabled"] is True
    # 轨迹层：源版本有快照 ⇒ 从快照继承，且 manifest 记了来源指纹
    assert man2["dataset"]["enabled"] is True
    assert man2["dataset"]["source"]["mode"] == "source_version"
    assert len(man2["dataset"]["source"]["source_trajectories_sha256"]) == 64
    traj2 = _read(v2 / builder.TRAJECTORIES_FILE)
    assert traj2[0]["golden_id"] == "T001__r1"


# ── 断点 1：None 不转 0 ────────────────────────────────────────────


def test_scout分数None原样进候选_不转0(tmp_path):
    """``score=None`` 是「这一维没判出来」，写成 0 就是判了零分。

    这里直接喂手写 scout 记录（含 veto 语境的 ``None``）——scout 自己的
    分流会把 ``None`` 送 quarantine，而这条测的是 **from-scout 的转写**
    不替它猜。
    """
    p2_root = tmp_path / "pipeline"
    _archive(p2_root)
    scout_dir = tmp_path / "scout"
    _dump(
        scout_dir / "positive_candidates.jsonl",
        [_scout_record(2, "https://b.test", {"d2": 5, "d4": 5, "d5": 5, "d6": 5})],
    )
    _dump(
        scout_dir / "negative_candidates.jsonl",
        [_scout_record(1, "https://a.test", {"d2": 1, "d4": 4, "d5": None, "d6": 4})],
    )

    golden = tmp_path / "golden"
    rc, out, err = _run(
        [
            "--golden-root", str(golden),
            "from-scout",
            "--scout-dir", str(scout_dir),
            "--p2-root", str(p2_root),
        ]
    )
    assert rc == 0, err
    sites = _read(golden / builder.CANDIDATES_FILE)[0]["site_outcomes"]
    assert sites[0]["dimension_scores"]["d5"] is None   # 不转 0
    assert sites[0]["dimension_scores"]["d2"] == 1
    assert sites[0]["status"] == "scored"
    # 没有 scout 记录的站点：整块保持「没评分」，不平摊、不丢
    assert sites[2]["dimension_scores"] is None
    assert sites[2]["status"] is None


# ── 断点 3：孤儿门禁 ───────────────────────────────────────────────


def test_孤儿门禁_人核标注挂不上就不写(tmp_path):
    p2_root = tmp_path / "pipeline"
    _archive(p2_root)
    scout_dir = tmp_path / "scout"
    _dump(
        scout_dir / "positive_candidates.jsonl",
        [
            _scout_record(1, "https://a.test", {"d2": 1, "d4": 4, "d5": 4, "d6": 4}),
            _scout_record(2, "https://b.test", {"d2": 5, "d4": 5, "d5": 5, "d6": 5}),
        ],
    )
    golden = tmp_path / "golden"
    argv = [
        "--golden-root", str(golden),
        "from-scout",
        "--scout-dir", str(scout_dir),
        "--p2-root", str(p2_root),
    ]
    rc, _, err = _run(argv)
    assert rc == 0, err
    before = (golden / builder.CANDIDATES_FILE).read_text(encoding="utf-8")
    real_gid = _read(golden / builder.CANDIDATES_FILE)[0]["site_outcomes"][0][
        "golden_id"
    ]
    _dump(
        golden / builder.ANNOTATIONS_FILE,
        [
            _ann(real_gid),
            _ann("T001__r1__deadbeef"),  # 谁都认领不了的人核行
        ],
    )

    rc, _, err = _run(argv + ["--force"])
    assert rc == 2
    assert "人核标注挂不上" in err
    # **不写**：候选集一个字节都没动
    assert (golden / builder.CANDIDATES_FILE).read_text(encoding="utf-8") == before


# ── 断点 4：轨迹行顶层分数不许静默丢 ──────────────────────────────


def test_轨迹行顶层分数在冻结入口退2(tmp_path):
    """顶层分数不属于任何站点，冻结只认 ``site_outcomes[].dimension_scores``。"""
    golden = tmp_path / "golden"
    golden.mkdir()
    site = {
        "golden_id": "T001__r1__aaaaaaaa",
        "source_kind": "archive_outcome",
        "url": "https://a.test",
        "code": {"branch": None},
        "dimension_scores": None,
        "scores_detail": [],
        "status": None,
        "outcome_index": 0,
        "visit_index": 1,
    }
    cand = {
        "golden_id": "T001__r1",
        "source_kind": "scout_scored",
        "task_id": "T001",
        "run_id": "r1",
        "trajectory_id": "T001__r1",
        "code": {"branch": None},
        "code_index": 0,
        "dimension_scores": {"d2": 5},  # ← 不属于任何站点
        "scores_detail": [],
        "status": "scored",
        "site_outcomes": [site],
    }
    _dump(golden / builder.CANDIDATES_FILE, [cand])
    _dump(golden / builder.ANNOTATIONS_FILE, [_ann(site["golden_id"])])

    rc, _, err = _run(
        ["--golden-root", str(golden), "freeze", "--version", "1", "--force"]
    )
    assert rc == 2
    assert "顶层带 dimension_scores" in err
    assert not (golden / "v1").exists()