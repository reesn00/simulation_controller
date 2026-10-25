"""D-10 pred 侧 ``code`` 的**重挂链**（candidates 的新分支 → `freeze --refresh-code`）。

背景：D-10 的 pred 唯一来源是冻结行的 ``code.branch``（``agreement`` 只读它、
不重算）。继承式冻结是「行内容不动」——重采/重判得到的新 ``code`` 走
``--from-version`` 永远进不了新版本。软维度金标有 ``--refresh-gold``、评分有
``--refresh-scores``、D-10 label 有 ``--refresh-label``，唯独 pred 侧的 ``code``
没有出口；``--refresh-code`` 补的就是这一截。

守的是七件事：

1. **只换 code**：``golden`` / ``dimension_scores`` / ``scores_detail`` /
   ``status`` 一字不动——金标归 ``--refresh-gold``/``--refresh-label``、评分归
   ``--refresh-scores``，各走各的出口；
2. **``--refresh-code`` 只能配 ``--from-version``**：不继承的冻结本来就直读
   candidates 的 ``code``，加了它没有「带过来的旧 code」可刷；
3. **缺 ``golden_id`` 退 2 且不落任何文件**，并列出全部缺失 id（fail-closed，
   绝不「找不到就留着旧 code」）；
4. **候选缺 ``code`` 而冻结行有 ``code`` ⇒ 退 2**：拿空覆盖只会把分支结论抹掉；
5. **code 变动 ≠ 新基线**：manifest 落 ``code_refresh.baseline_reset=false``，
   本版与上一版仍逐行可比（与 gold/label 刷新相反），``holdout_runs`` 不归零；
6. **回归集不重切**：成员与顺序不变（C-17）；内容未变则指纹不变；
7. **刷空仅告警不报错**：candidates 的 code 与冻结行完全一致 ⇒ 退 0 + stderr 告警。
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
        "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows),
        encoding="utf-8",
    )


def _read(path: pathlib.Path) -> list[dict]:
    return [
        json.loads(x)
        for x in path.read_text(encoding="utf-8").splitlines()
        if x.strip()
    ]


_FROZEN_CODE = {"branch": "success", "branch_label": "成功", "evidence": "旧证据"}


def _frozen(gid: str, **over) -> dict:
    row = {
        "golden_id": gid,
        "source_kind": "archive_outcome",
        "url": f"https://{gid}.test/",
        "site_url": f"https://{gid}.test/",
        "code": dict(_FROZEN_CODE, evidence=f"{gid} 旧证据"),
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
        "status": "scored",
    }
    row.update(over)
    return row


def _cand(gid: str, **over) -> dict:
    row = {
        "golden_id": gid,
        "code": dict(_FROZEN_CODE, evidence=f"{gid} 旧证据"),
        "dimension_scores": {"d2": 5, "d4": 5, "d5": 3, "d6": None},
        "scores_detail": [{"dimension": "d5", "score": 3, "reasons": []}],
        "status": "scored",
    }
    row.update(over)
    return row


def _fixture(
    tmp_path: pathlib.Path, *, version: int = 7, holdout_runs: int = 0
) -> pathlib.Path:
    """已冻结的小版本（calibration 3 / regression 1 / holdout 2）。

    pool_frac = (3+1)/6 = 66.7% 落在区间内，不卡分层门。回归集单独 1 行，
    用来钉「改 code 不重切回归集」这条。
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
                "holdout_runs": holdout_runs,
                "seed": 42,
                "counts": {
                    "candidates": 6,
                    "annotated": 6,
                    "calibration": 3,
                    "holdout": 2,
                    "regression": 1,
                },
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
        root / builder.CANDIDATES_FILE,
        [_cand("g1"), _cand("g2"), _cand("g3"), _cand("g4"), _cand("g5"), _cand("r1")],
    )
    return root


def _others(*gids: str) -> list[dict]:
    return [_cand(g) for g in gids]


# ── freeze --refresh-code ───────────────────────────────────


def test_继承式冻结把重采的新分支带进新版本(tmp_path):
    root = _fixture(tmp_path)
    _dump(
        root / builder.CANDIDATES_FILE,
        [
            _cand("g1", code={"branch": "subscription_wall", "evidence": "g1 重采"}),
            _cand("g2"),
            _cand("g3"),
            _cand("g4"),
            _cand("g5", code={"branch": "unresolved", "evidence": "g5 重采"}),
            _cand("r1"),
        ],
    )
    rc, out, _ = _run(
        ["--golden-root", str(root), "freeze", "--from-version", "7", "--refresh-code"]
    )
    assert rc == 0, out
    v8 = root / "v8"
    rows = {r["golden_id"]: r for r in _read(v8 / "calibration.jsonl")}
    # ① pred 侧 code 换了：整个子字典按 candidates 重挂
    assert rows["g1"]["code"] == {"branch": "subscription_wall", "evidence": "g1 重采"}
    assert rows["g5"]["code"] == {"branch": "unresolved", "evidence": "g5 重采"}
    # ② 金标 / 软维度 / 评分 / 状态一字没动
    assert rows["g1"]["golden"]["label"] == "success"
    assert rows["g1"]["golden"]["dimension_scores"] == {"d5": 4}
    assert rows["g1"]["dimension_scores"] == {"d5": 2}
    assert rows["g1"]["scores_detail"] == []
    assert rows["g1"]["status"] == "scored"
    # ③ manifest 如实记下刷了什么
    manifest = json.loads((v8 / "manifest.json").read_text(encoding="utf-8"))
    cr = manifest["code_refresh"]
    assert cr["enabled"] is True
    assert cr["source"] == builder.CANDIDATES_FILE
    assert cr["rows_matched"] == 6
    assert cr["rows_missing"] == 0
    assert cr["changed_rows"] == 2
    # ④ code 是 pred 不是金标 ⇒ 新基线开关是 False
    assert cr["baseline_reset"] is False
    # ⑤ branch 分布取站点级（本 fixture 全是 archive_outcome，即站点级）
    assert cr["branch_shift"]["before"] == {"success": 6}
    assert cr["branch_shift"]["after"] == {
        "success": 4,
        "subscription_wall": 1,
        "unresolved": 1,
    }
    assert "code 已按 candidates 重挂" in out
    assert "仍与前版逐行可比" in out


def test_改code不重切回归集_成员不变且内容未变则指纹不变(tmp_path):
    root = _fixture(tmp_path)
    # r1 的候选 code 与冻结行逐字一致 → 回归集内容未变
    _dump(
        root / builder.CANDIDATES_FILE,
        [_cand("g1", code={"branch": "blocked"})]
        + _others("g2", "g3", "g4", "g5", "r1"),
    )
    _run(
        ["--golden-root", str(root), "freeze", "--from-version", "7", "--refresh-code"]
    )
    v7reg = _read(root / "v7" / "regression.jsonl")
    v8reg = _read(root / "v8" / "regression.jsonl")
    # C-17：回归集成员与顺序不变（不重切）
    assert [r["golden_id"] for r in v8reg] == [r["golden_id"] for r in v7reg]
    assert v8reg == v7reg  # 内容未变
    old_fp = json.loads((root / "v7" / "manifest.json").read_text(encoding="utf-8"))[
        "split"
    ]["regression_fingerprint"]
    new_fp = json.loads((root / "v8" / "manifest.json").read_text(encoding="utf-8"))[
        "split"
    ]["regression_fingerprint"]
    assert new_fp == old_fp
    # 指纹与文件内容一致（指纹盖整行）
    assert new_fp == builder._rows_fingerprint(v8reg)


def test_回归集内容变则指纹跟着变(tmp_path):
    """指纹盖**整行**：``--refresh-code`` 改了回归行时指纹必须跟着变。"""
    root = _fixture(tmp_path)
    _dump(
        root / builder.CANDIDATES_FILE,
        [_cand("g1", code={"branch": "blocked"})]
        + _others("g2", "g3", "g4", "g5")
        + [_cand("r1", code={"branch": "subscription_wall"})],
    )
    rc, _, _ = _run(
        ["--golden-root", str(root), "freeze", "--from-version", "7", "--refresh-code"]
    )
    assert rc == 0
    v8reg = _read(root / "v8" / "regression.jsonl")
    old_fp = json.loads((root / "v7" / "manifest.json").read_text(encoding="utf-8"))[
        "split"
    ]["regression_fingerprint"]
    new_fp = json.loads((root / "v8" / "manifest.json").read_text(encoding="utf-8"))[
        "split"
    ]["regression_fingerprint"]
    assert new_fp != old_fp
    assert new_fp == builder._rows_fingerprint(v8reg)
    assert [r["golden_id"] for r in v8reg] == ["r1"]  # 成员没变


def test_refresh_code必须配from_version():
    rc, _, err = _run(["freeze", "--refresh-code"])
    assert rc == 2
    assert "--from-version" in err


def test_候选缺id则退2且不写任何文件(tmp_path):
    root = _fixture(tmp_path)
    # g3 从 candidates 里去掉
    _dump(
        root / builder.CANDIDATES_FILE,
        [_cand("g1"), _cand("g2"), _cand("g4"), _cand("g5"), _cand("r1")],
    )
    rc, _, err = _run(
        ["--golden-root", str(root), "freeze", "--from-version", "7", "--refresh-code"]
    )
    assert rc == 2
    assert "g3" in err
    assert "不刷新" in err
    assert not (root / "v8").exists()  # fail-closed：没白耗一个版本号


def test_候选缺code而冻结行有code则退2(tmp_path):
    root = _fixture(tmp_path)
    _dump(
        root / builder.CANDIDATES_FILE,
        [_cand("g1", code=None)] + _others("g2", "g3", "g4", "g5", "r1"),
    )
    rc, _, err = _run(
        ["--golden-root", str(root), "freeze", "--from-version", "7", "--refresh-code"]
    )
    assert rc == 2
    assert "g1" in err
    assert "没有 code" in err
    assert not (root / "v8").exists()


def test_code没变时仅告警不报错(tmp_path):
    root = _fixture(tmp_path)  # candidates 的 code 与冻结行完全一致
    rc, _, err = _run(
        ["--golden-root", str(root), "freeze", "--from-version", "7", "--refresh-code"]
    )
    assert rc == 0
    assert "没重挂到任何变化" in err
    manifest = json.loads((root / "v8" / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["code_refresh"]["changed_rows"] == 0


def test_重挂后仍然逐行可比(tmp_path):
    """``--refresh-code`` 不自作主张改行集合：条数与 golden_id 顺序不变。"""
    root = _fixture(tmp_path)
    _dump(
        root / builder.CANDIDATES_FILE,
        [_cand("g1", code={"branch": "blocked"})]
        + _others("g2", "g3", "g4", "g5", "r1"),
    )
    _run(
        ["--golden-root", str(root), "freeze", "--from-version", "7", "--refresh-code"]
    )
    for split in ("calibration", "holdout", "regression"):
        old = _read(root / "v7" / f"{split}.jsonl")
        new = _read(root / "v8" / f"{split}.jsonl")
        assert [r["golden_id"] for r in old] == [r["golden_id"] for r in new]


def test_refresh_code不改尺子_holdout_runs不归零(tmp_path):
    root = _fixture(tmp_path, holdout_runs=3)
    _dump(
        root / builder.CANDIDATES_FILE,
        [_cand("g1", code={"branch": "blocked"})]
        + _others("g2", "g3", "g4", "g5", "r1"),
    )
    rc, _, _ = _run(
        ["--golden-root", str(root), "freeze", "--from-version", "7", "--refresh-code"]
    )
    assert rc == 0
    manifest = json.loads((root / "v8" / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["holdout_runs"] == 3  # 继承式重冻结不许把封存计数归零
    assert manifest["code_refresh"]["baseline_reset"] is False
    assert manifest["derived_from"]["version"] == 7


def test_轨迹级code为null不报错(tmp_path):
    """p2_sample 轨迹级行的 ``code`` 本来就是 ``null``：null → null 不算「拿空覆盖」。"""
    root = _fixture(tmp_path)
    _dump(
        root / "v7" / "holdout.jsonl",
        [
            _frozen("g2"),
            _frozen("g4"),
            _frozen("p2", source_kind="p2_sample", code=None),
        ],
    )
    _dump(
        root / builder.CANDIDATES_FILE,
        [_cand("g1", code={"branch": "blocked"})]
        + _others("g2", "g3", "g4", "g5", "r1")
        + [_cand("p2", code=None)],
    )
    rc, out, err = _run(
        [
            "--golden-root",
            str(root),
            "freeze",
            "--from-version",
            "7",
            "--refresh-code",
            "--force",
        ]
    )
    assert rc == 0, (out, err)
    rows = {r["golden_id"]: r for r in _read(root / "v8" / "holdout.jsonl")}
    assert rows["p2"]["code"] is None
    manifest = json.loads((root / "v8" / "manifest.json").read_text(encoding="utf-8"))
    # 站点级 branch 分布只数 6 条（calibration 3 + holdout 站点级 2 + regression 1），
    # 不含 p2 轨迹级
    after = manifest["code_refresh"]["branch_shift"]["after"]
    assert sum(after.values()) == 6
    assert "" not in after  # 没有被当成空分支塞进分布


def test_没加refresh_code时code不跟着走(tmp_path):
    """继承式冻结是「行内容不动」——这条测试钉的是**现状**。"""
    root = _fixture(tmp_path)
    _dump(
        root / builder.CANDIDATES_FILE,
        [_cand("g1", code={"branch": "blocked"})]
        + _others("g2", "g3", "g4", "g5", "r1"),
    )
    rc, _, _ = _run(["--golden-root", str(root), "freeze", "--from-version", "7"])
    assert rc == 0
    rows = _read(root / "v8" / "calibration.jsonl")
    assert rows[0]["code"]["branch"] == "success"  # 重采的新分支没跟过来
    manifest = json.loads((root / "v8" / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["code_refresh"] == {"enabled": False}


def test_与refresh_scores组合各刷各的(tmp_path):
    """``--refresh-code`` 与 ``--refresh-scores`` 同开：code 与 pred 侧评分各刷各的，互不覆盖。"""
    root = _fixture(tmp_path)
    _dump(
        root / builder.CANDIDATES_FILE,
        [_cand("g1", code={"branch": "blocked"})]
        + _others("g2", "g3", "g4", "g5", "r1"),
    )
    rc, out, err = _run(
        [
            "--golden-root",
            str(root),
            "freeze",
            "--from-version",
            "7",
            "--refresh-code",
            "--refresh-scores",
        ]
    )
    assert rc == 0, (out, err)
    row = _read(root / "v8" / "calibration.jsonl")[0]
    assert row["code"]["branch"] == "blocked"
    assert row["dimension_scores"] == {"d2": 5, "d4": 5, "d5": 3, "d6": None}
    manifest = json.loads((root / "v8" / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["code_refresh"]["enabled"] is True
    assert manifest["score_refresh"]["enabled"] is True
