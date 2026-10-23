"""C-08 内环验证器的守门测试。

设计来源：``docs/设计方案/16-评估器与rubric双向校准回路方案.md`` §5.1（四条判据）·
§9.4（C-08）· §10（验收信号）。

守的是五件事：

1. **H1-α 回放必须驳回**：修 4 / 伤 6 / 净 −2 那次若当时有这门，
   会当场被拦下——尤其第四条（回归集 ``regressed == 0``）与前两条
   （净改善、无类回归）各自独立生效；
2. **总数变好不能盖住类回归**：净 +5 但某一类 F1 掉了 → 照样驳回；
3. **判不了 ≠ 通过**：``not_evaluable`` 的必判项即驳回；同桶清零在
   没有分诊数据时**不参与判定**，但**不许标成 pass**；
4. **不可比的输入直接退 2**（键重复 / 换过档 / 回归集键集或金标变了 /
   schema 不同）——不能把它们算成"回归 0 条"这个绿色结论；
5. **同源**：快照默认根与 ``builder.PIPELINE_ROOT`` 同一处，
   verdict 三面归属覆盖 ``snapshot.VERDICTS`` 全集。
"""

import io
import json
import pathlib
from contextlib import redirect_stderr, redirect_stdout

import pytest

from trajectory_pipeline.evaluation.calibration import snapshot, verify
from trajectory_pipeline.evaluation.golden import builder


# ── 夹具 ──────────────────────────────────────────────────────

PAYWALL = "subscription_wall"
SUCCESS = "success"


def _row(
    gid: str,
    *,
    gold: str = SUCCESS,
    branch: str = SUCCESS,
    split: str = "calibration",
    gold_dims: dict | None = None,
    pred_dims: dict | None = None,
) -> dict:
    return {
        "golden_id": gid,
        "source_kind": "archive_outcome",
        "task_id": gid.split("__")[0],
        "run_id": "run00001",
        "url": f"https://{gid}.test/",
        "site_url": f"https://{gid}.test/",
        "__split__": split,
        "code": {"branch": branch, "evidence": f"{gid} 的证据"},
        "golden": {
            "label": gold,
            "note": "",
            "dimension_scores": dict(gold_dims or {}),
        },
        "dimension_scores": dict(pred_dims or {}),
        "scores_detail": [
            {"dimension": d, "score": s, "reasons": [f"{d} 理由"]}
            for d, s in (pred_dims or {}).items()
        ],
    }


def _snap(
    rows: list[dict],
    *,
    version: int = 7,
    rubric: str = "1.2",
    evaluator: str = "ev0000000001",
    tolerance: int = 1,
) -> dict:
    return snapshot.build_snapshot(
        rows,
        golden_version=version,
        success_label=SUCCESS,
        rubric_version=rubric,
        evaluator_version=evaluator,
        tolerance=tolerance,
    )


def _h1a_pools(
    *,
    fixed: int = 4,
    regressed: int = 6,
    n_paywall: int = 35,
    n_success: int = 36,
    paywall_tp: int = 7,
    n_regression: int = 6,
    n_holdout: int = 2,
    regress_split: str = "regression",
) -> tuple[list[dict], list[dict]]:
    """H1-α 形态的两轮行集合（``fixed`` 条 PAYWALL FN 被修好，``regressed`` 条 SUCCESS 被误伤）。

    数字取自 [11 号附录 A](../../docs/设计方案/11-外部方案对账.md)：
    35 条 PAYWALL 金标里 baseline 只判对 7 条（FN 28），36 条 SUCCESS 全对；
    H1-α 的宽松判据修 4 条 FN、误伤 6 条 SUCCESS。
    """
    base: list[dict] = []
    curr: list[dict] = []
    for i in range(n_paywall):
        gid = f"P{i:03d}__r1__h"
        ok = i < paywall_tp
        base.append(_row(gid, gold=PAYWALL, branch=PAYWALL if ok else SUCCESS))
        curr_ok = ok or (paywall_tp <= i < paywall_tp + fixed)
        curr.append(_row(gid, gold=PAYWALL, branch=PAYWALL if curr_ok else SUCCESS))
    for i in range(n_success):
        gid = f"S{i:03d}__r1__h"
        base.append(_row(gid, gold=SUCCESS, branch=SUCCESS))
        hurt = i >= n_success - regressed
        curr.append(_row(gid, gold=SUCCESS, branch=PAYWALL if hurt else SUCCESS))
    # 回归集：两轮完全相同（冻结纪律：不得改动）
    for i in range(n_regression):
        gid = f"R{i:03d}__r1__h"
        gold = PAYWALL if i % 3 == 0 else SUCCESS
        branch = gold
        base.append(_row(gid, gold=gold, branch=branch, split=regress_split))
        curr.append(_row(gid, gold=gold, branch=branch, split=regress_split))
    # 持有集：两轮相同，只报条数
    for i in range(n_holdout):
        gid = f"H{i:03d}__r1__h"
        base.append(_row(gid, gold=SUCCESS, branch=SUCCESS, split="holdout"))
        curr.append(_row(gid, gold=SUCCESS, branch=SUCCESS, split="holdout"))
    return base, curr


def _run(argv: list[str]) -> tuple[int, str, str]:
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        rc = verify.main(argv)
    return rc, out.getvalue(), err.getvalue()


def _write(path: pathlib.Path, snap: dict) -> pathlib.Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(snap, ensure_ascii=False), encoding="utf-8")
    return path


# ── 1. H1-α 回放：修 4 / 伤 6 必须驳回 ────────────────────────


def test_H1α回放_修4伤6_必须驳回():
    base, curr = _h1a_pools(fixed=4, regressed=6)
    report = verify.analyze(_snap(base), _snap(curr))

    calib = report["calibration"]["d10"]
    assert (calib["fixed"], calib["regressed"], calib["net"]) == (4, 6, -2)
    assert report["gate"] == verify.GATE_REJECT
    # 两条独立理由：净改善 ≤ 0，且 SUCCESS 的 F1 掉了（总数与单类分开看）
    assert report["criteria"]["net_improvement"]["status"] == verify.CRITERION_FAIL
    assert report["criteria"]["class_regression"]["status"] == verify.CRITERION_FAIL
    assert report["calibration"]["per_class"]["SUCCESS"]["regressed"] is True
    # 回归集没被动过 → 第四条判据是过的（它是"修 4 伤 6"之外的另一道门）
    assert (
        report["criteria"]["regression_regressed_zero"]["status"]
        == verify.CRITERION_PASS
    )


def test_总数变好但单类F1掉了_照样驳回():
    # 小类才是这类判据的靶子：三类里 PAYWALL 净 +2，而 BROKEN_CONTENT
    # 只有 2 条支撑，坏掉 1 条就把 1.000 拖到 0.667——**总数在涨，单类在塌**
    # （§5.1 第 2 条："PAYWALL recall 掉了不算改善"）。
    base = [
        _row("P000__r1__h", gold=PAYWALL, branch=SUCCESS),
        _row("P001__r1__h", gold=PAYWALL, branch=SUCCESS),
        _row("B000__r1__h", gold="broken", branch="broken"),
        _row("B001__r1__h", gold="broken", branch="broken"),
        _row("R000__r1__h", gold=SUCCESS, branch=SUCCESS, split="regression"),
    ]
    curr = [
        _row("P000__r1__h", gold=PAYWALL, branch=PAYWALL),
        _row("P001__r1__h", gold=PAYWALL, branch=PAYWALL),
        _row("B000__r1__h", gold="broken", branch="broken"),
        _row("B001__r1__h", gold="broken", branch=SUCCESS),
        _row("R000__r1__h", gold=SUCCESS, branch=SUCCESS, split="regression"),
    ]
    report = verify.analyze(_snap(base), _snap(curr))
    assert report["calibration"]["d10"]["net"] == 1
    assert report["criteria"]["net_improvement"]["status"] == verify.CRITERION_PASS
    assert report["calibration"]["per_class"]["BROKEN_CONTENT"]["regressed"] is True
    assert report["criteria"]["class_regression"]["status"] == verify.CRITERION_FAIL
    assert report["gate"] == verify.GATE_REJECT


def test_净改善为0也算不过_阈值是严格大于():
    base, curr = _h1a_pools(fixed=2, regressed=2)
    report = verify.analyze(_snap(base), _snap(curr))
    assert report["calibration"]["d10"]["net"] == 0
    assert report["criteria"]["net_improvement"]["status"] == verify.CRITERION_FAIL
    assert report["gate"] == verify.GATE_REJECT


def test_干净改善_通过():
    base, curr = _h1a_pools(fixed=10, regressed=0)
    report = verify.analyze(_snap(base), _snap(curr))
    assert report["calibration"]["d10"]["net"] == 10
    assert report["criteria"]["class_regression"]["status"] == verify.CRITERION_PASS
    assert report["criteria"]["regression_regressed_zero"]["status"] == verify.CRITERION_PASS
    assert report["gate"] == verify.GATE_PASS
    assert report["reasons"] == []


# ── 2. 回归集（第四条判据） ───────────────────────────────────


def test_回归集反转即驳回_即使头条在改善(tmp_path):
    base, curr = _h1a_pools(fixed=10, regressed=0)
    # 让回归集里一条原本判对的行翻成判错
    target = next(r for r in curr if r["__split__"] == "regression" and r["golden"]["label"] == SUCCESS)
    target["code"]["branch"] = PAYWALL
    report = verify.analyze(_snap(base), _snap(curr))
    assert report["calibration"]["d10"]["net"] == 10
    assert report["regression"]["d10"]["regressed"] == 1
    assert (
        report["criteria"]["regression_regressed_zero"]["status"]
        == verify.CRITERION_FAIL
    )
    assert report["gate"] == verify.GATE_REJECT
    assert any("regression_regressed_zero" in r for r in report["reasons"])


def test_回归集键集不同_直接退2():
    base, curr = _h1a_pools(fixed=10, regressed=0)
    curr = [r for r in curr if r["golden_id"] != "R000__r1__h"]
    with pytest.raises(verify.VerifyError, match="回归集不得改动"):
        verify.analyze(_snap(base), _snap(curr))


def test_回归集金标变了_直接退2():
    base, curr = _h1a_pools(fixed=10, regressed=0)
    target = next(r for r in curr if r["golden_id"] == "R001__r1__h")
    target["golden"]["label"] = PAYWALL
    with pytest.raises(verify.VerifyError, match="金标在两轮之间变了"):
        verify.analyze(_snap(base), _snap(curr))


def test_换过档的行_直接退2():
    base, curr = _h1a_pools(fixed=10, regressed=0)
    moved = next(r for r in curr if r["golden_id"] == "S000__r1__h")
    moved["__split__"] = "regression"
    with pytest.raises(verify.VerifyError, match="换了档"):
        verify.analyze(_snap(base), _snap(curr))


def test_本轮没有回归集_回归门判不了():
    base, curr = _h1a_pools(fixed=10, regressed=0)
    base = [r for r in base if r["__split__"] != "regression"]
    curr = [r for r in curr if r["__split__"] != "regression"]
    report = verify.analyze(_snap(base), _snap(curr))
    crit = report["criteria"]["regression_regressed_zero"]
    assert crit["status"] == verify.CRITERION_NOT_EVALUABLE
    assert "还没切回归集" in crit["detail"]
    # 判不了 ≠ 通过
    assert report["gate"] == verify.GATE_REJECT


# ── 3. 不可比的输入 ──────────────────────────────────────────


def test_schema不同_直接退2():
    base, curr = _h1a_pools(fixed=10, regressed=0)
    b, c = _snap(base), _snap(curr)
    b["schema_version"] = "0"
    with pytest.raises(verify.VerifyError, match="schema 不同"):
        verify.analyze(b, c)


def test_键重复_直接退2():
    base, curr = _h1a_pools(fixed=10, regressed=0)
    b = _snap(base)
    b["entries"].append(dict(b["entries"][0]))
    with pytest.raises(verify.VerifyError, match="出现两条判定"):
        verify.analyze(b, _snap(curr))


def test_缺golden_id_直接退2():
    base, curr = _h1a_pools(fixed=10, regressed=0)
    b = _snap(base)
    b["entries"][0]["golden_id"] = ""
    with pytest.raises(verify.VerifyError, match="缺 golden_id"):
        verify.analyze(b, _snap(curr))


# ── 4. 判不了的三面归属 ──────────────────────────────────────


def test_判不了的条目不计入修好或弄坏():
    """判不了只出现在**软维度**：d10 的 pred 恒有兜底标签。

    ``snapshot._hard_entry`` 的 pred 是 ``code.branch or success_label``——
    分支缺失会被写成 ``success`` 而不是"判不了"（这是快照的既有口径）。
    因此 d10 上不存在 ``ungraded``，而软维度（一侧缺档）会。
    """
    base = [
        # 这一对：基线 d4 有金标没预测（判不了），本轮有预测且一致
        {**_row("A__r1__h", gold_dims={"d4": 4}), "dimension_scores": {}},
        # 这一对：两轮都有，档差超容差（tier_gap → 算错）
        {
            **_row("B__r1__h", gold_dims={"d4": 4}),
            "dimension_scores": {"d4": 4},
        },
        _row("R000__r1__h", gold=SUCCESS, branch=SUCCESS, split="regression"),
        # d10 上凑一条真修好，让净改善为正
        _row("P000__r1__h", gold=PAYWALL, branch=SUCCESS),
    ]
    curr = [
        {**_row("A__r1__h", gold_dims={"d4": 4}), "dimension_scores": {"d4": 4}},
        {
            **_row("B__r1__h", gold_dims={"d4": 4}),
            "dimension_scores": {"d4": 1},
        },
        _row("R000__r1__h", gold=SUCCESS, branch=SUCCESS, split="regression"),
        _row("P000__r1__h", gold=PAYWALL, branch=PAYWALL),
    ]
    report = verify.analyze(_snap(base), _snap(curr))
    soft = report["calibration"]["other_dimensions"]["d4"]
    assert soft["paired"] == 2
    assert soft["unverifiable"] == 1        # A：基线判不了，本轮对 → 不是"修好"
    assert soft["fixed"] == 0
    assert soft["regressed"] == 1           # B：档差 3 > 容差 1 → 本轮判错
    # d10 侧没有"判不了"
    assert report["calibration"]["d10"]["unverifiable"] == 0


def test_verdict三面覆盖全集():
    assert set(verify.VERDICT_SIDES) == set(snapshot.VERDICTS)
    assert verify.VERDICT_SIDES[snapshot.VERDICT_DETAIL_MISMATCH] == verify.SIDE_OK
    assert verify.VERDICT_SIDES[snapshot.VERDICT_TIER_GAP] == verify.SIDE_BAD
    assert verify.VERDICT_SIDES[snapshot.VERDICT_UNGRADED] == verify.SIDE_UNVERIFIABLE


def test_软维度只出读数不进判据():
    base, curr = _h1a_pools(fixed=10, regressed=0)
    for r in base:
        r["golden"]["dimension_scores"] = {"d4": 4}
        r["dimension_scores"] = {"d4": 4}
    for r in curr:
        r["golden"]["dimension_scores"] = {"d4": 4}
        r["dimension_scores"] = {"d4": 2}
    report = verify.analyze(_snap(base), _snap(curr))
    soft = report["calibration"]["other_dimensions"]
    assert "d4" in soft
    assert soft["d4"]["paired"] > 0
    assert soft["d4"]["regressed"] == soft["d4"]["paired"]
    # 判据只有四条，软维度不参与（D-C9：新线先出读数）
    assert list(report["criteria"]) == [
        "net_improvement",
        "class_regression",
        "bucket_clear",
        "regression_regressed_zero",
    ]
    assert report["gate"] == verify.GATE_PASS


def test_未切档快照按头条计():
    base, curr = _h1a_pools(fixed=10, regressed=0)
    for r in base + curr:
        r.pop("__split__", None)
    report = verify.analyze(_snap(base), _snap(curr))
    assert report["default_headline"] is True
    # 整份没切档 ⇒ 全部按头条：调参 71 + 回归 6 + 持有 2
    assert report["calibration"]["d10"]["paired"] == 35 + 36 + 6 + 2
    assert report["regression"]["d10"]["paired"] == 0


def test_持有集只报条数():
    base, curr = _h1a_pools(fixed=10, regressed=0)
    report = verify.analyze(_snap(base), _snap(curr))
    assert report["holdout"]["paired"] == 2
    assert report["holdout"]["note"] == "只报条数，不判——封存纪律（D-C5）"
    # Q-12：切档按行 ⇒ 持有集不是 N 条独立证据。附注必须带，且**不重算
    # 泄漏条数**（快照里没有 URL，数字的真源在 agreement 的 URL 级读数）。
    assert "不是 N 条独立证据" in report["holdout"]["independence_note"]
    assert "agreement --version" in report["holdout"]["independence_note"]


# ── 5. 同桶清零 ─────────────────────────────────────────────


def _triage(rows: list[dict], bucket: str = "evaluator_logic") -> list[dict]:
    """分诊行必须过 C-05 的校验（六桶 / 理由必填 / bucket 只能人填）。"""
    return [
        {
            "golden_id": r["golden_id"],
            "dimension": "d10",
            "bucket": bucket,
            "triage_by": "owner",
            "triage_evidence": "按明写条款应得另一个值，金标与准则无歧义",
        }
        for r in rows
    ]


def test_同桶清零_没有分诊数据时记n_a且不参与判定():
    base, curr = _h1a_pools(fixed=10, regressed=0)
    report = verify.analyze(_snap(base), _snap(curr))
    crit = report["criteria"]["bucket_clear"]
    assert crit["status"] == verify.CRITERION_NOT_EVALUABLE
    assert crit["enforced"] is False
    assert "不算通过" in crit["detail"]
    assert report["gate"] == verify.GATE_PASS


def test_同桶清零_目标桶仍有残留即驳回():
    base, curr = _h1a_pools(fixed=4, regressed=6)
    # 本轮仍分歧的行：24 条 PAYWALL FN + 6 条误伤
    conflicted = [
        r for r in curr
        if r["__split__"] == "calibration"
        and r["golden"]["label"] != r["code"]["branch"]
    ]
    triage = _triage(conflicted)
    report = verify.analyze(
        _snap(base), _snap(curr), triage=triage, buckets=["evaluator_logic"]
    )
    crit = report["criteria"]["bucket_clear"]
    assert crit["status"] == verify.CRITERION_FAIL
    assert crit["enforced"] is True
    assert "残留 30" in crit["detail"]


def test_同桶清零_分诊覆盖全且目标桶无残留则过():
    base, curr = _h1a_pools(fixed=10, regressed=0)
    # 本轮仍有 18 条 PAYWALL FN：全部分到 gold_label 桶，目标桶 evaluator_logic 里为空
    conflicted = [
        r for r in curr
        if r["__split__"] == "calibration"
        and r["golden"]["label"] != r["code"]["branch"]
    ]
    triage = _triage(conflicted, bucket="gold_defect")
    report = verify.analyze(
        _snap(base), _snap(curr), triage=triage, buckets=["evaluator_logic"]
    )
    crit = report["criteria"]["bucket_clear"]
    assert crit["status"] == verify.CRITERION_PASS
    assert "残留 0" in crit["detail"]
    assert report["gate"] == verify.GATE_PASS


def test_给了分诊表却没给桶_退2():
    base, curr = _h1a_pools(fixed=10, regressed=0)
    with pytest.raises(verify.VerifyError, match="必须给 --bucket"):
        verify.analyze(_snap(base), _snap(curr), triage=[], buckets=[])


def test_分诊表没覆盖全部分歧_目标桶清零也不可信():
    base, curr = _h1a_pools(fixed=4, regressed=6)
    conflicted = [
        r for r in curr
        if r["__split__"] == "calibration"
        and r["golden"]["label"] != r["code"]["branch"]
    ]
    assert len(conflicted) == 30
    # 只分诊 10 条，剩 20 条没桶：目标桶残留 0 也可能只是"没分到它头上"
    triage = _triage(conflicted[:10], bucket="gold_defect")
    report = verify.analyze(
        _snap(base), _snap(curr), triage=triage, buckets=["evaluator_logic"]
    )
    crit = report["criteria"]["bucket_clear"]
    assert crit["status"] == verify.CRITERION_NOT_EVALUABLE
    assert "缺 20 条" in crit["detail"]


# ── 6. 类回归容差 ───────────────────────────────────────────


def test_类回归容差放宽后该类不再判回归():
    base, curr = _h1a_pools(fixed=4, regressed=6)
    strict = verify.analyze(_snap(base), _snap(curr))
    loose = verify.analyze(_snap(base), _snap(curr), class_tolerance=0.1)
    assert strict["criteria"]["class_regression"]["status"] == verify.CRITERION_FAIL
    assert loose["criteria"]["class_regression"]["status"] == verify.CRITERION_PASS
    # 但净改善 −2 还在 → 门仍然驳回（放宽一条不会让整门变绿）
    assert loose["gate"] == verify.GATE_REJECT


# ── 7. 归因（金标侧动了会被喊出来） ──────────────────────────


def test_金标变了会记进归因而不冒充修好():
    base, curr = _h1a_pools(fixed=0, regressed=0)
    target = next(r for r in curr if r["golden_id"] == "P010__r1__h")
    target["golden"]["label"] = SUCCESS   # 人把这条的金标改了 → 与 pred 一致
    report = verify.analyze(_snap(base), _snap(curr))
    calib = report["calibration"]["d10"]
    assert calib["attribution"]["gold_changed"] == 1
    assert calib["fixed"] == 1
    assert "不可归因" in verify.render(report)


# ── 8. 同源守卫 ─────────────────────────────────────────────


def test_快照默认根与builder同源():
    assert verify.DEFAULT_SNAPSHOT_ROOT == builder.PIPELINE_ROOT / "calibration"


# ── 9. CLI ──────────────────────────────────────────────────


def test_CLI_通过返回0_驳回返回1(tmp_path):
    base, curr = _h1a_pools(fixed=10, regressed=0)
    bp = _write(tmp_path / "base.json", _snap(base, version=7))
    cp = _write(tmp_path / "curr.json", _snap(curr, version=8))
    out_json = tmp_path / "report.json"

    rc, stdout, _ = _run(["--baseline", str(bp), "--current", str(cp),
                          "--json-out", str(out_json)])
    assert rc == 0
    assert "✅ 通过" in stdout
    assert json.loads(out_json.read_text(encoding="utf-8"))["gate"] == "pass"

    base2, curr2 = _h1a_pools(fixed=4, regressed=6)
    bp2 = _write(tmp_path / "base2.json", _snap(base2, version=7))
    cp2 = _write(tmp_path / "curr2.json", _snap(curr2, version=8))
    rc, stdout, _ = _run(["--baseline", str(bp2), "--current", str(cp2)])
    assert rc == 1
    assert "❌ 驳回" in stdout


def test_CLI_按版本号取快照_缺文件退2(tmp_path):
    rc, _, err = _run([
        "--calibration-root", str(tmp_path / "cal"),
        "--baseline-version", "7", "--current-version", "8",
    ])
    assert rc == 2
    assert "快照不存在" in err


def test_CLI_两个来源都给_退2(tmp_path):
    base, _ = _h1a_pools()
    bp = _write(tmp_path / "b.json", _snap(base))
    rc, _, err = _run(["--baseline", str(bp), "--baseline-version", "7",
                       "--current", str(bp)])
    assert rc == 2
    assert "只能给一个" in err


def test_CLI_分诊表缺字段_退2(tmp_path):
    base, curr = _h1a_pools(fixed=10, regressed=0)
    bp = _write(tmp_path / "b.json", _snap(base))
    cp = _write(tmp_path / "c.json", _snap(curr))
    triage = tmp_path / "triage.jsonl"
    # 签名（`triage_by`）先过——缺的是 `triage_evidence`，走的是"缺字段"那一条
    triage.write_text(
        json.dumps({"golden_id": "P000__r1__h", "dimension": "d10",
                    "triage_by": "zhang"}) + "\n",
        encoding="utf-8",
    )
    rc, _, err = _run(["--baseline", str(bp), "--current", str(cp),
                       "--triage", str(triage), "--bucket", "logic"])
    assert rc == 2
    assert "分诊行缺字段" in err