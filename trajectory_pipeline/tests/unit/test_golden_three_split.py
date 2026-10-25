"""黄金集三档切分（调参池 / 回归集 / 持有集）的守门。

设计来源：``docs/设计方案/16-评估器与rubric双向校准回路方案.md``
C-03（基线对齐：以 v6 冻结档为准）、C-04（头条分母只算调参池）、
C-17（回归集切片与冻结守卫）。

这三条合并成一次"继承式重冻结"，所以测试也按一条链子读：

    1. ``档位词汇`` —— 档名/文件名只有一个真源，认不出的档名返回空串而不猜；
    2. ``回归切片`` —— 确定性、只收站点级、按标签分层、切不下就报错；
    3. ``继承式重冻结`` —— 行原样带过来（**v7 与 v6 逐行可比**）、
       ``holdout_runs`` 不许归零、回归集指纹落盘、已有回归集不许重切；
    4. ``agreement`` —— 头条只认调参池、旧口径同时输出、回归集被改动当场炸；
    5. ``快照与分歧卡`` —— 三档全收进快照，但回归集不出分歧卡（不作调参依据）。

为什么"行原样带过来"值得专门一条测试：C-03 定的是「以 v6 的 74.7% 为准」。
重冻结若重新洗牌，v7 与 v6 就不是同一批行，那个 74.7% 立刻失去对照意义。
"""

import io
import json
import pathlib
from contextlib import redirect_stderr, redirect_stdout

import pytest

from trajectory_pipeline.evaluation.calibration import disagreement, snapshot
from trajectory_pipeline.evaluation.golden import builder, splits


# ── 工具 ────────────────────────────────────────────────────────


def _row(
    gid: str,
    label: str = "success",
    *,
    source_kind: str = "archive",
    gold_label: str | None = None,
    dims: dict | None = None,
) -> dict:
    golden = {
        "label": gold_label if gold_label is not None else label,
        "note": "",
        "dimension_scores": dict(dims or {}),
    }
    return {
        "golden_id": gid,
        "source_kind": source_kind,
        "code": {"branch": label},
        "golden": golden,
    }


def _write_jsonl(path: pathlib.Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        for row in rows:
            fh.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")


def _read_jsonl(path: pathlib.Path) -> list[dict]:
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def _run(argv: list[str]) -> tuple[int, str, str]:
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        rc = builder.main(argv)
    return rc, out.getvalue(), err.getvalue()


def _make_v6(
    root: pathlib.Path,
    *,
    n_success: int = 40,
    n_broken: int = 30,
    n_holdout: int = 32,
    n_traj: int = 2,
    holdout_runs: int = 1,
) -> pathlib.Path:
    """造一个 v6 冻结档（无回归集，与真实 v6 同形）。

    每 10 条里放 1 条金标不服的（``success`` 判成 ``not_play_site``），
    让分歧卡那条测试有东西可筛——全一致的数据集测不出"回归集不出卡"。
    """
    vdir = root / "v6"
    calib = [
        _row(
            f"c{i}", "success",
            gold_label="not_play_site" if i % 10 == 0 else None,
        )
        for i in range(n_success)
    ]
    calib += [_row(f"b{i}", "broken") for i in range(n_broken)]
    calib += [_row(f"t{i}", "success", source_kind="p2_sample") for i in range(n_traj)]
    hold = [_row(f"h{i}", "success") for i in range(n_holdout)]
    _write_jsonl(vdir / "calibration.jsonl", calib)
    _write_jsonl(vdir / "holdout.jsonl", hold)
    (vdir / "manifest.json").write_text(
        json.dumps(
            {
                "schema_version": "1",
                "label_schema_version": builder.LABEL_SCHEMA_VERSION,
                "version": 6,
                "holdout_runs": holdout_runs,
                "seed": 42,
                "counts": {
                    "candidates": 0,
                    "annotated": len(calib) + len(hold),
                    "calibration": len(calib),
                    "holdout": len(hold),
                },
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    _write_jsonl(root / builder.VERSIONS_INDEX, [{"version": 6}])
    return root


# ── 1. 档位词汇 ────────────────────────────────────────────────


def test_档位词汇只有一个真源():
    assert splits.SPLITS == ("calibration", "regression", "holdout")
    assert splits.HEADLINE_SPLIT == splits.SPLIT_CALIBRATION
    assert splits.SIDE_SPLITS == {"regression", "holdout"}
    assert splits.REGRESSION_SIZE == 60
    # 文件名与档名必须一一对应，且能反向解析回来
    for split, fname in splits.SPLIT_FILES.items():
        assert fname == f"{split}.jsonl"
        assert splits.split_of_file(fname) == split
        assert splits.split_of_file(split) == split


def test_未登记档名返回空串而不猜():
    """认不出就返回空串：把 ``holdback.jsonl`` 猜成 holdout 会让旁置档混进头条。"""
    assert splits.split_of_file("holdback.jsonl") == ""
    assert splits.split_of_file("") == ""
    # 大小写不同也算认不出——档名是契约，不做宽容匹配
    assert splits.split_of_file("Calibration.jsonl") == ""


# ── 2. 回归切片 ────────────────────────────────────────────────


def test_回归切片确定性():
    rows = [_row(f"c{i}", "success") for i in range(20)]
    rows += [_row(f"b{i}", "broken") for i in range(10)]
    a, _ = builder._regression_slice(rows, 9)
    b, _ = builder._regression_slice(rows, 9)
    assert [r["golden_id"] for r in a] == [r["golden_id"] for r in b]
    assert len(a) == 9


def test_回归切片只收站点级():
    rows = [_row(f"c{i}") for i in range(10)]
    rows += [_row(f"t{i}", source_kind="p2_sample") for i in range(5)]
    picked, rest = builder._regression_slice(rows, 4)
    assert all(r["source_kind"] != "p2_sample" for r in picked)
    # 轨迹级行不进回归集，也不从池子里消失
    assert sum(1 for r in rest if r["source_kind"] == "p2_sample") == 5
    assert len(picked) + len(rest) == len(rows)


def test_回归切片按标签分层():
    """稀有标签在 10 条里也要有：均匀抽样会让小分支落到 0，回归读不出来。"""
    rows = [_row(f"s{i}", "success") for i in range(90)]
    rows += [_row(f"w{i}", "subscription_wall") for i in range(10)]
    picked, _ = builder._regression_slice(rows, 10)
    labels = [r["golden"]["label"] for r in picked]
    assert labels.count("subscription_wall") >= 1
    assert labels.count("success") == 9


def test_回归切片切不下就报错():
    rows = [_row(f"c{i}") for i in range(5)]
    with pytest.raises(builder.GoldenError, match="调参池会剩不下"):
        builder._regression_slice(rows, 5)


def test_回归切片为0时不切():
    rows = [_row(f"c{i}") for i in range(3)]
    picked, rest = builder._regression_slice(rows, 0)
    assert picked == []
    assert len(rest) == 3


# ── 3. 继承式重冻结 ────────────────────────────────────────────


def test_继承式重冻结_行原样带过来(tmp_path):
    root = _make_v6(tmp_path / "golden")
    rc, out, _ = _run([
        "--golden-root", str(root), "freeze",
        "--version", "7", "--from-version", "6", "--regression", "20",
    ])
    assert rc == 0, out

    v6_calib = _read_jsonl(root / "v6" / "calibration.jsonl")
    v7_calib = _read_jsonl(root / "v7" / "calibration.jsonl")
    v7_reg = _read_jsonl(root / "v7" / "regression.jsonl")
    v7_hold = _read_jsonl(root / "v7" / "holdout.jsonl")

    # ① 三档并起来 == v6 的两档并起来（逐行、逐内容）
    def key(rows):
        return sorted(json.dumps(r, sort_keys=True, ensure_ascii=False) for r in rows)

    assert key(v7_calib + v7_reg + v7_hold) == key(
        v6_calib + _read_jsonl(root / "v6" / "holdout.jsonl")
    )
    # ② 回归集条数、站点级、与调参池不相交
    assert len(v7_reg) == 20
    assert all(r["source_kind"] != "p2_sample" for r in v7_reg)
    assert not ({r["golden_id"] for r in v7_reg} & {r["golden_id"] for r in v7_calib})
    # ③ 持有集一条不动
    assert v7_hold == _read_jsonl(root / "v6" / "holdout.jsonl")

    manifest = json.loads((root / "v7" / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["counts"]["regression"] == 20
    assert manifest["split"]["headline_split"] == "calibration"
    assert manifest["split"]["regression_size"] == 20
    assert len(manifest["split"]["regression_fingerprint"]) == 12
    assert manifest["derived_from"]["version"] == 6
    # ④ 封存计数不许归零（归零等于把"解封过一次"抹掉）
    assert manifest["holdout_runs"] == 1

    index = _read_jsonl(root / builder.VERSIONS_INDEX)
    assert index[-1]["version"] == 7
    assert index[-1]["regression"] == 20
    assert index[-1]["derived_from"] == 6


def test_继承式重冻结_回归集原样带过来(tmp_path):
    root = _make_v6(tmp_path / "golden")
    assert _run([
        "--golden-root", str(root), "freeze",
        "--version", "7", "--from-version", "6", "--regression", "20",
    ])[0] == 0
    # 再继承一次，不给 --regression：已有回归集原样带过来，不是"没切"
    rc, _, _ = _run([
        "--golden-root", str(root), "freeze", "--version", "8", "--from-version", "7",
    ])
    assert rc == 0
    assert _read_jsonl(root / "v8" / "regression.jsonl") == _read_jsonl(
        root / "v7" / "regression.jsonl"
    )
    manifest = json.loads((root / "v8" / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["counts"]["regression"] == 20


def test_回归集不得重切(tmp_path):
    """已冻结的回归集再切一次 = 把"原本判对的"换成"这次判对的"，必须拒绝。"""
    root = _make_v6(tmp_path / "golden")
    assert _run([
        "--golden-root", str(root), "freeze",
        "--version", "7", "--from-version", "6", "--regression", "20",
    ])[0] == 0
    rc, _, err = _run([
        "--golden-root", str(root), "freeze",
        "--version", "8", "--from-version", "7", "--regression", "10",
    ])
    assert rc == 2
    assert "不得重切" in err


def test_继承不存在的版本_报错(tmp_path):
    root = _make_v6(tmp_path / "golden")
    rc, _, err = _run([
        "--golden-root", str(root), "freeze",
        "--version", "9", "--from-version", "8",
    ])
    assert rc == 2
    assert "只能继承已冻结的版本" in err


def test_不切回归集时不写空文件(tmp_path):
    """未切就如实记 0 且不落文件——空文件会让"有没有回归门"多出一个判据。"""
    root = _make_v6(tmp_path / "golden")
    rc, _, _ = _run([
        "--golden-root", str(root), "freeze", "--version", "7", "--from-version", "6",
    ])
    assert rc == 0
    assert not (root / "v7" / "regression.jsonl").exists()
    manifest = json.loads((root / "v7" / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["counts"]["regression"] == 0
    assert manifest["split"]["regression_fingerprint"] == ""


# ── 4. agreement 头条口径 ──────────────────────────────────────


def _freeze_with_regression(tmp_path: pathlib.Path) -> pathlib.Path:
    root = _make_v6(tmp_path / "golden")
    assert _run([
        "--golden-root", str(root), "freeze",
        "--version", "7", "--from-version", "6", "--regression", "20",
    ])[0] == 0
    return root


def test_agreement头条不含回归集与持有集(tmp_path):
    root = _freeze_with_regression(tmp_path)
    out_json = tmp_path / "agr.json"
    rc, stdout, _ = _run([
        "--golden-root", str(root), "agreement", "--version", "7",
        "--json-out", str(out_json),
    ])
    assert rc == 0, stdout
    data = json.loads(out_json.read_text(encoding="utf-8"))

    calib_site = 70  # 40 success + 30 broken（2 条轨迹级不算）
    assert data["n"] == calib_site - 20
    assert data["d10_denominator"]["split"] == "calibration"
    assert data["d10_denominator"]["excluded_site_rows"] == {"regression": 20, "holdout": 32}
    # 旧口径仍在：三档合计
    assert data["legacy_denominator"]["n"] == calib_site + 32
    assert "旧口径，三档合计 102 条" in stdout
    assert "回归集 20 条不进头条" in stdout


def test_agreement_回归集被改过就当场报错(tmp_path):
    root = _freeze_with_regression(tmp_path)
    path = root / "v7" / "regression.jsonl"
    rows = _read_jsonl(path)
    # 最省力的作弊：把金标改成与 code.branch 一致 → 这一行立刻"判对"，
    # `regressed` 永远数不出来。指纹要挡的就是这个。
    idx = next(
        i for i, r in enumerate(rows)
        if r["golden"]["label"] != (r.get("code") or {}).get("branch")
    )
    rows[idx]["golden"]["label"] = rows[idx]["code"]["branch"]
    _write_jsonl(path, rows)

    rc, _, err = _run(["--golden-root", str(root), "agreement", "--version", "7"])
    assert rc == 2
    assert "回归集不得改动" in err


# ── 5. 快照与分歧卡 ────────────────────────────────────────────


def test_快照三档全收但回归集不出分歧卡(tmp_path):
    root = _freeze_with_regression(tmp_path)
    outdir = tmp_path / "cal"
    rc, _, _ = _run([
        "--golden-root", str(root), "agreement", "--version", "7",
        "--emit-disagreements", str(outdir),
    ])
    assert rc == 0

    snap = snapshot.load_snapshot(outdir / "v7" / "snapshot.json")
    stats = snapshot.summarize_snapshot(snap)
    # 快照收三档：回归判定要看"原本判对的现在判错"，只看调参池看不出来
    assert set(stats["d10_by_split"]) == {"calibration", "regression", "holdout"}
    assert stats["d10_by_split"]["regression"]["n"] == 20
    # 但头条 d10 只算调参池
    assert stats["d10"] == stats["d10_by_split"]["calibration"]

    cards = disagreement.read_cards(outdir / "v7" / "disagreements.jsonl")
    assert cards, "这批数据全一致，测试就白写了"
    # 分诊池 = 调参池：回归集（不能调参）与持有集（不能烧封存）都不出卡
    assert {c["split"] for c in cards} == {"calibration"}


# ── 6. 继承式冻结刷新评分（M-29 S2 / S3） ──────────────────────
#
# M-29 的病根：继承式冻结「行内容不动」，于是 v7 冻结时写进去的 pred 侧
# 分数**原样带到了 v8**——而那是 v6 时代的评分器留下的（D-5 还「恒真」，
# 清一色 5 分）。快照与评分器不再是同一个东西，且**一个字都不报**。
#
# `--refresh-scores` 补的就是这个能力：切分与金标照旧继承，只把 pred 侧
# 的三个字段按 golden_id 从 candidates 重挂。


def _make_v6_stale(root: pathlib.Path) -> pathlib.Path:
    """造一个 v6 冻结档：行带**过期**的 pred 侧评分（d5 全是 5）。

    「过期」正是 M-29 的病根——那批分数是当时评分器的输出，评分器换了之后
    它们就不代表当前评分器了。
    """
    vdir = root / "v6"
    calib = [_row(f"c{i}", "success") for i in range(10)]
    hold = [_row(f"h{i}", "success") for i in range(5)]
    for r in calib + hold:
        r["dimension_scores"] = {"d5": 5}
        r["scores_detail"] = [{"dimension": "d5", "score": 5, "reasons": []}]
        r["status"] = "scored"
    _write_jsonl(vdir / "calibration.jsonl", calib)
    _write_jsonl(vdir / "holdout.jsonl", hold)
    (vdir / "manifest.json").write_text(
        json.dumps(
            {
                "schema_version": "1",
                "label_schema_version": builder.LABEL_SCHEMA_VERSION,
                "version": 6,
                "holdout_runs": 0,
                "seed": 42,
                "counts": {
                    "candidates": 0,
                    "annotated": len(calib) + len(hold),
                    "calibration": len(calib),
                    "holdout": len(hold),
                },
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    _write_jsonl(root / builder.VERSIONS_INDEX, [{"version": 6}])
    # candidates：当前评分器的输出（d5 判 2 分、d6 弃权、d2/d4 五分）
    cands = []
    for r in calib + hold:
        cands.append({
            "golden_id": r["golden_id"],
            "dimension_scores": {"d2": 5, "d4": 5, "d5": 2, "d6": None},
            "scores_detail": [
                {"dimension": "d5", "score": 2, "reasons": ["环境阻断"]}
            ],
            "status": "scored",
        })
    _write_jsonl(root / builder.CANDIDATES_FILE, cands)
    return root


def test_刷新评分_只换pred侧不动金标与切分(tmp_path):
    root = _make_v6_stale(tmp_path / "golden")
    assert _run([
        "--golden-root", str(root), "freeze", "--version", "7",
        "--from-version", "6", "--regression", "3",
    ])[0] == 0
    v7_manifest = json.loads(
        (root / "v7" / "manifest.json").read_text(encoding="utf-8")
    )
    v7_rows = {
        r["golden_id"]: r
        for r in _read_jsonl(root / "v7" / "calibration.jsonl")
        + _read_jsonl(root / "v7" / "regression.jsonl")
        + _read_jsonl(root / "v7" / "holdout.jsonl")
    }

    rc, out, err = _run([
        "--golden-root", str(root), "freeze", "--version", "8",
        "--from-version", "7", "--refresh-scores",
    ])
    assert rc == 0, (out, err)

    v8_rows = {
        r["golden_id"]: r
        for r in _read_jsonl(root / "v8" / "calibration.jsonl")
        + _read_jsonl(root / "v8" / "regression.jsonl")
        + _read_jsonl(root / "v8" / "holdout.jsonl")
    }
    assert set(v8_rows) == set(v7_rows)

    for gid, row in v8_rows.items():
        old = v7_rows[gid]
        # ① pred 侧换了：d5 从「恒 5」变成当前评分器的 2 分，d6 是弃权
        assert row["dimension_scores"] == {
            "d2": 5, "d4": 5, "d5": 2, "d6": None
        }, gid
        assert row["status"] == "scored"
        # ② 金标与分支结论一个字段都不许动
        assert row["golden"] == old["golden"], gid
        assert row["code"] == old["code"], gid

    # ③ 切分不重洗：每档的行集合与 v7 一致
    for split, fname in (
        ("calibration", "calibration.jsonl"),
        ("regression", "regression.jsonl"),
        ("holdout", "holdout.jsonl"),
    ):
        assert (
            {r["golden_id"] for r in _read_jsonl(root / "v8" / fname)}
            == {r["golden_id"] for r in _read_jsonl(root / "v7" / fname)}
        ), split

    v8_manifest = json.loads(
        (root / "v8" / "manifest.json").read_text(encoding="utf-8")
    )
    # ④ 回归集指纹：内容变了指纹必须跟着变，且与文件内容一致
    assert v8_manifest["split"]["regression_fingerprint"] != (
        v7_manifest["split"]["regression_fingerprint"]
    )
    assert v8_manifest["split"]["regression_fingerprint"] == builder._rows_fingerprint(
        _read_jsonl(root / "v8" / "regression.jsonl")
    )
    # ⑤ manifest 如实记下刷了什么
    sr = v8_manifest["score_refresh"]
    assert sr["enabled"] is True
    assert sr["source"] == builder.CANDIDATES_FILE
    assert sr["rows_missing"] == 0
    assert sr["rows_matched"] == len(v8_rows)
    assert sr["per_dim"]["d5"]["before"] == {"5": len(v8_rows)}
    assert sr["per_dim"]["d5"]["after"] == {"2": len(v8_rows)}
    assert sr["tally"]["d5"] == {
        "scored": len(v8_rows), "abstain": 0, "no_sample": 0
    }


def test_刷新评分_候选缺id则退2且不写任何文件(tmp_path):
    """fail-closed：找不到 id 就退 2，**绝不「找不到就留着旧分数」**。

    留着旧分数看起来像刷过了，实际 pred 侧仍是过期的那批——比不刷更坏。
    """
    root = _make_v6_stale(tmp_path / "golden")
    assert _run([
        "--golden-root", str(root), "freeze", "--version", "7",
        "--from-version", "6",
    ])[0] == 0
    # candidates 里删掉一行
    cands = _read_jsonl(root / builder.CANDIDATES_FILE)
    dropped = cands.pop(0)
    _write_jsonl(root / builder.CANDIDATES_FILE, cands)

    versions_before = (root / builder.VERSIONS_INDEX).read_text(encoding="utf-8")
    rc, _, err = _run([
        "--golden-root", str(root), "freeze", "--version", "8",
        "--from-version", "7", "--refresh-scores",
    ])
    assert rc == 2
    assert dropped["golden_id"] in err
    # 一个文件都不许写
    assert not (root / "v8").exists()
    assert (root / builder.VERSIONS_INDEX).read_text(encoding="utf-8") == versions_before


def test_没刷新评分时要喊一声(tmp_path):
    """S3：带着上一版的旧分数走而不报，读数会被当成当前评分器的输出。"""
    root = _make_v6_stale(tmp_path / "golden")
    rc, out, err = _run([
        "--golden-root", str(root), "freeze", "--version", "7",
        "--from-version", "6",
    ])
    assert rc == 0, (out, err)
    assert "未刷新评分" in err
    assert "--refresh-scores" in err
    # 确实没刷新：pred 侧仍是源版本的分数
    row = _read_jsonl(root / "v7" / "calibration.jsonl")[0]
    assert row["dimension_scores"] == {"d5": 5}
    manifest = json.loads((root / "v7" / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["score_refresh"]["enabled"] is False


def test_刷新评分必须配继承(tmp_path):
    """不继承的冻结本来就直接读 candidates 的评分字段，没有「旧分数」这回事。"""
    root = _make_v6_stale(tmp_path / "golden")
    rc, _, err = _run([
        "--golden-root", str(root), "freeze", "--version", "7", "--refresh-scores",
    ])
    assert rc == 2
    assert "--from-version" in err
