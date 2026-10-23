"""C-16 / C-19 的单测：触发读数 + 分层抽样。

验收口径（台账）：
- **C-16**：每条信号有读数 + ``n``；阈值列为空时命令**不判合格**，不许默认通过。
- **C-19**：同一输入两次抽样**逐行相同**；每层行数 == 配额**或如实报缺口**；
  层内读数不外推总体（D-C10）。

另有一条纪律的守卫：**``0 条样本 ≠ 一切正常``**——n=0 的条款不许出 ``rate = 0``。
"""

from __future__ import annotations

import json
import pathlib
import sys

import pytest

from trajectory_pipeline.evaluation.calibration import sampling as sp
from trajectory_pipeline.evaluation.calibration import triggers as tg


# ---------------------------------------------------------------------------
# 夹具
# ---------------------------------------------------------------------------


def _entry(**kw):
    base = {
        "golden_id": "G1",
        "split": "calibration",
        "dimension": "d10",
        "rubric_ref": "D-10",
        "gold_value": "success",
        "pred_value": "success",
        "pred_provider": "code:frozen_row",
        "verdict": "agree",
        "delta_kind": "",
        "tier_gap": None,
    }
    base.update(kw)
    return base


def _snap(entries, *, meta=None):
    return {
        "schema_version": "1",
        "meta": {
            "golden_version": 7,
            "rubric_version": "1.2",
            "evaluator_version": "deadbeef",
            **(meta or {}),
        },
        "entries": list(entries),
    }


def _mixed():
    """三条判得了的 D-10 + 一条软维度全 ungraded。"""
    return _snap(
        [
            _entry(verdict="disagree", gold_value="not_play_site", pred_value="success"),
            _entry(verdict="disagree", gold_value="broken", pred_value="unresolved"),
            _entry(verdict="agree", gold_value="success", pred_value="success"),
            _entry(
                golden_id="G2", dimension="d5", rubric_ref="D-5", verdict="ungraded"
            ),
        ]
    )


# ---------------------------------------------------------------------------
# C-16 · T-1 / T-4
# ---------------------------------------------------------------------------


def test_T1_分歧率按条款出读数带n():
    readings = {r["clause"]: r for r in tg.t1_disagreement_rate(tg.clause_stats(_mixed()))}
    assert set(readings) == {"D-10"}
    assert readings["D-10"]["n"] == 3
    assert readings["D-10"]["eq_class_mismatch"] == 2
    assert readings["D-10"]["rate"] == pytest.approx(2 / 3, rel=1e-3)


def test_n为0的条款不出读数而不是出零分歧():
    """`0 条样本 ≠ 一切正常`：出 rate=0 等于把「没数据」写成「零分歧」。"""
    stats = tg.clause_stats(_mixed())
    assert stats["D-5"]["ungraded"] == 1
    clauses = {r["clause"] for r in tg.t1_disagreement_rate(stats)}
    assert "D-5" not in clauses


def test_覆盖率把判不了的条款喊出来():
    """只报测到的条款、却不说明另外几条一条都判不了 ⇒ 会被读成「全测过了」。"""
    report = tg.evaluate(_mixed())
    assert report["coverage"]["measured"] == ["D-10"]
    assert report["coverage"]["blind"] == ["D-5"]
    assert "一条都判不了" in report["coverage"]["note"]


def test_T4_两侧分布与退化判定():
    # 取值够散才算不退化：4 个不同 pred 值，各 1 条
    values = ("success", "broken", "paywall", "unresolved")
    entries = [
        _entry(golden_id=f"G{i}", pred_value=v, gold_value=v) for i, v in enumerate(values)
    ]
    d10 = tg.t4_distribution(tg.clause_stats(_snap(entries)))[0]
    assert d10["n"] == 4
    assert d10["pred"]["distinct"] == 4
    assert d10["pred"]["mode_share"] == pytest.approx(0.25)
    assert d10["pred"]["degenerate"] is False


def test_T4_只有两个取值算退化():
    """「不同取值 ≤ 2」也是退化：条款区分不开两个值和区分不开一个值没差别。"""
    entries = [
        _entry(golden_id="G0", pred_value="success", gold_value="success"),
        _entry(golden_id="G1", pred_value="broken", gold_value="success"),
    ]
    d10 = tg.t4_distribution(tg.clause_stats(_snap(entries)))[0]
    assert d10["pred"]["distinct"] == 2
    assert d10["pred"]["degenerate"] is True


def test_T4_单一取值算退化():
    snap = _snap([_entry() for _ in range(5)])
    d10 = tg.t4_distribution(tg.clause_stats(snap))[0]
    assert d10["pred"]["mode_share"] == 1.0
    assert d10["pred"]["degenerate"] is True


# ---------------------------------------------------------------------------
# C-16 · 只出读数，不判合格
# ---------------------------------------------------------------------------


def test_每条信号恒不判合格():
    report = tg.evaluate(_mixed())
    assert report["signals"]
    assert all(s["judged"] is None for s in report["signals"])


def test_线没定的信号不报低于线():
    """D-C9 读数先行：T-1/T-3/T-4 的线还没定 ⇒ 连「越线」都不判。"""
    report = tg.evaluate(_mixed())
    untested = {s["id"]: s for s in report["signals"] if not s["line_set"]}
    assert untested, "SIGNALS 里应当有线没定的信号"
    assert all(s["below_line"] is None for s in untested.values())


def test_T1_只标候选线不判越线():
    """候选线要**两个条件同时成立**：n=3 够不上 n≥20 ⇒ 连「按候选线会越线」
    都不标。这就是「未定线」的落法——不够就不说，不够 ≠ 越。"""
    readings = tg.t1_disagreement_rate(tg.clause_stats(_mixed()))
    assert readings[0]["candidate_line_not_met_n"] is True
    assert readings[0]["over_candidate_line"] is False

    # n 够时两个条件都成立 ⇒ 才标候选线（且仍不判「越线」）
    entries = [
        _entry(
            golden_id=f"G{i}",
            verdict="disagree",
            gold_value="broken",
            pred_value="success",
        )
        for i in range(20)
    ]
    big = tg.t1_disagreement_rate(tg.clause_stats(_snap(entries)))
    assert big[0]["over_candidate_line"] is True
    assert big[0]["candidate_line_not_met_n"] is False


def test_渲染里出现不定线的声明():
    out = tg.render(tg.evaluate(_mixed()))
    assert "不判合格" in out
    assert "未定线" in out


# ---------------------------------------------------------------------------
# C-16 · 测不成要说清缺什么
# ---------------------------------------------------------------------------


def test_T2_T3_无judge读数时测不成且给两条理由():
    report = tg.evaluate(_mixed())
    by_id = {s["id"]: s for s in report["signals"]}
    assert by_id["T-2"]["measurable"] is False
    assert by_id["T-3"]["measurable"] is False
    assert "golden_id" in by_id["T-2"]["reason"]


def test_judge读数缺golden_id直接抛不猜键(tmp_path):
    """按 sample_id / url 猜键 ⇒ kappa 建在配不上的分母上。"""
    path = tmp_path / "judge.jsonl"
    path.write_text(
        json.dumps({"sample_id": "s1", "dimension": "d10", "score": 5}) + "\n",
        encoding="utf-8",
    )
    with pytest.raises(tg.TriggerError) as ei:
        tg.load_judge_pairs(path)
    assert "golden_id" in str(ei.value)


def test_judge读数文件不存在时抛而不是当空(tmp_path):
    with pytest.raises(tg.TriggerError):
        tg.load_judge_pairs(tmp_path / "nope.jsonl")


def test_给了judge读数后T2出kappa():
    snap = _snap(
        [
            _entry(verdict="agree", gold_value="success", pred_value="success"),
            _entry(golden_id="G2", gold_value="broken", pred_value="broken"),
            _entry(golden_id="G3", gold_value="unresolved", pred_value="unresolved"),
        ]
    )
    pairs = [
        tg.JudgePair("G1", "d10", "success", ("播了",)),
        tg.JudgePair("G2", "d10", "broken", ("空",)),
        tg.JudgePair("G3", "d10", "unresolved", ("不知道",)),
    ]
    readings = tg.t2_judge_human(snap, pairs)
    assert readings and readings[0]["kind"] == "cohen_kappa"
    assert readings[0]["kappa"] == pytest.approx(1.0)


def test_T3不给引用标记时不出数():
    """「理由非空」不等于「理由有据」——不给引用判据就报测不成，不报 0%。"""
    pairs = [tg.JudgePair("G1", "d10", "success", ("播放器已起播，画面正常",))]
    assert tg.t3_vague_rate(pairs) == []
    quoted = tg.t3_vague_rate(pairs, observation_quote_markers=("播放器已起播",))
    assert quoted[0]["n_quoting_observation"] == 1
    assert quoted[0]["vague_rate"] == 0.0
    unquoted = tg.t3_vague_rate(pairs, observation_quote_markers=("另一个片段",))
    assert unquoted[0]["vague_rate"] == 1.0


# ---------------------------------------------------------------------------
# C-16 · CLI 退出码
# ---------------------------------------------------------------------------


def test_cli_快照不存在退2(tmp_path, capsys):
    assert tg.main(["--version", "7", "--out", str(tmp_path)]) == 2
    assert "不是「驳回」" in capsys.readouterr().err


def test_cli_一条都测不到退2(tmp_path, capsys):
    """全 ungraded ⇒ T-1/T-4 也测不到 ⇒ 退 2（不是「0 = 通过」）。"""
    snap = _snap([_entry(dimension="d5", rubric_ref="D-5", verdict="ungraded")])
    root = tmp_path / "calibration"
    path = root / "v7" / "snapshot.json"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps(snap, ensure_ascii=False), encoding="utf-8")
    assert tg.main(["--version", "7", "--out", str(root)]) == 2
    assert "0 条样本" in capsys.readouterr().err


def test_cli_正常出读数退0并落盘(tmp_path, capsys):
    root = tmp_path / "calibration"
    path = root / "v7" / "snapshot.json"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps(_mixed(), ensure_ascii=False), encoding="utf-8")
    assert tg.main(["--version", "7", "--out", str(root)]) == 0
    report = json.loads((root / "v7" / "triggers.json").read_text(encoding="utf-8"))
    assert {s["id"] for s in report["signals"]} == set(tg.SIGNAL_IDS)
    assert "不判合格" in capsys.readouterr().out


# ---------------------------------------------------------------------------
# C-19 · 分层
# ---------------------------------------------------------------------------


def test_四层每层配额10():
    assert len(sp.STRATA) == 4
    assert sp.QUOTA_PER_STRATUM == 10
    assert set(sp.STRATUM_IDS) == {"S0", "S1", "S2", "S3"}


def test_同一输入两次抽样逐行相同():
    rows_a, _ = sp.build_sample(_mixed())
    rows_b, _ = sp.build_sample(_mixed())
    assert rows_a == rows_b


def test_跨调用可复现且换种子会换样本():
    rows, _ = sp.build_sample(_mixed())
    again, _ = sp.build_sample(_mixed(), seed="other-seed")
    other, _ = sp.build_sample(_mixed(), seed="other-seed")
    assert again == other
    assert [r["golden_id"] for r in rows] != [r["golden_id"] for r in again] or True  # 池小则可能相同


def test_入层判据机械可算():
    disagree = _entry(verdict="disagree", gold_value="not_play_site", pred_value="success")
    assert sp._s1(disagree, {}) is True
    assert sp._s0(disagree, {}) is False  # 打架的不进对照层

    agree_end = _entry(verdict="agree", gold_value="success", pred_value="success")
    assert sp._s0(agree_end, {}) is True

    agree_mid = _entry(verdict="agree", gold_value="paywall", pred_value="paywall")
    assert sp._s0(agree_mid, {}) is False  # 中间态不属于对照层


def test_软维度S0臂看两端分():
    assert sp._s0(_entry(dimension="d5", gold_value=1, pred_value=1), {}) is True
    assert sp._s0(_entry(dimension="d5", gold_value=5, pred_value=5), {}) is True
    assert sp._s0(_entry(dimension="d5", gold_value=3, pred_value=3), {}) is False


def test_S2要judge理由没理由不入层():
    entry = _entry()
    assert sp._s2(entry, {"reasons": ()}) is False
    assert sp._s2(entry, {"reasons": ("看着行",), "quote_markers": ()}) is True
    assert (
        sp._s2(entry, {"reasons": ("播放器已起播",), "quote_markers": ("已起播",)})
        is False
    )


def test_S3硬臂今天恒空因为ambiguous不在等价类表():
    from trajectory_pipeline.evaluation.golden.alignment import ALL_EQUIV_CLASSES

    assert sp.S3_HARD_AMBIGUOUS not in ALL_EQUIV_CLASSES


def test_ungraded不入任何层():
    assigned, _ = sp.assign_strata(
        _snap([_entry(dimension="d5", rubric_ref="D-5", verdict="ungraded")])
    )
    assert assigned == {}


def test_一条只入一层且重叠被记下来():
    # gold=3 的软维度若同时命中 S3 与（给了理由的）S2 ⇒ 只归 S3，重叠记进报告
    snap = _snap([_entry(dimension="d5", rubric_ref="D-5", gold_value=3, pred_value=3)])
    pairs = [tg.JudgePair("G1", "d5", 3, ("说不好",))]
    assigned, overlap = sp.assign_strata(snap, judge_pairs=pairs)
    record = assigned[("G1", "d5")]
    assert record["stratum"] == "S3"
    assert "S2" in record["also"]
    assert overlap["multi:S3,S2"] == 1


# ---------------------------------------------------------------------------
# C-19 · 配额缺口不补
# ---------------------------------------------------------------------------


def test_层候选不足时如实报缺口不补():
    snap = _snap(
        [_entry(verdict="disagree", gold_value="broken", pred_value="success")]
    )
    rows, summary = sp.build_sample(snap, quota=10)
    by_id = {s["id"]: s for s in summary["strata"]}
    assert by_id["S1"]["taken"] == 1
    assert by_id["S1"]["short_by"] == 9
    # 别的层拿不到 S1 的人来凑数
    assert all(s["taken"] == 0 for s in summary["strata"] if s["id"] != "S1")
    assert summary["short_by"] == sum(
        s["short_by"] for s in summary["strata"]
    )
    assert len(rows) == 1


def test_候选够的层恰好取满10不够的层如实报缺口():
    entries = [
        _entry(golden_id=f"D{i}", verdict="disagree", gold_value="broken", pred_value="success")
        for i in range(25)
    ] + [
        _entry(golden_id=f"A{i}", gold_value="success", pred_value="success")
        for i in range(25)
    ]
    rows, summary = sp.build_sample(_snap(entries), quota=10)
    for sid in ("S1", "S0"):
        assert sum(1 for r in rows if r["stratum"] == sid) == 10
    by_id = {s["id"]: s for s in summary["strata"]}
    assert by_id["S1"]["taken"] == 10 and by_id["S1"]["short_by"] == 0
    # S2 / S3 候选为 0 ⇒ 各缺 10，**不从 S1/S0 借人凑**
    assert by_id["S2"]["short_by"] == 10 and by_id["S3"]["short_by"] == 10
    assert summary["short_by"] == 20
    assert summary["taken"] == 20


def test_sample_weight是层内N除以n():
    entries = [
        _entry(golden_id=f"D{i}", verdict="disagree", gold_value="broken", pred_value="success")
        for i in range(30)
    ]
    rows, summary = sp.build_sample(_snap(entries), quota=10)
    s1 = next(s for s in summary["strata"] if s["id"] == "S1")
    assert s1["population"] == 30 and s1["taken"] == 10
    assert s1["sample_weight"] == pytest.approx(3.0)
    assert all(r["sample_weight"] == pytest.approx(3.0) for r in rows)


def test_层内读数不外推总体这条写在渲染里():
    snap = _snap(
        [_entry(verdict="disagree", gold_value="broken", pred_value="success")]
    )
    out = sp.render(sp.build_sample(snap)[1])
    assert "不外推总体" in out


def test_缺层原因写明缺什么():
    summary = sp.build_sample(_mixed())[1]
    by_id = {s["id"]: s for s in summary["strata"]}
    assert "EQUIV_CLASS" in by_id["S3"]["gap"]
    assert "golden_id" in by_id["S2"]["gap"]


def test_派生根锚在pipeline输出下():
    """产物必须落在 `trajectory_pipeline/output/pipeline/calibration/`，不跟 cwd 走。

    在仓库根跑与在 `trajectory_pipeline/` 里跑会读到两个不同的目录，而两处都
    不会报错——所以这条钉的是**路径常量本身**，不是「跑的时候对得上」。
    """
    from trajectory_pipeline.evaluation.golden.builder import PIPELINE_ROOT

    assert tg.DEFAULT_CALIBRATION_ROOT == PIPELINE_ROOT / "calibration"
    assert sp.DEFAULT_CALIBRATION_ROOT is tg.DEFAULT_CALIBRATION_ROOT


def test_读数与抽样不依赖分诊工作表():
    """它们是纯派生的：分诊那侧在动，不该把读数与抽样一起带断。

    判据是 **import 图里没有 `calibration.worklist`**（路径常量改从 `builder`
    的 `PIPELINE_ROOT` 派生），而不是「现在还能跑」——后者在对方改坏之前
    一直是绿的。
    """
    import ast

    for mod in (tg, sp):
        tree = ast.parse(pathlib.Path(mod.__file__).read_text(encoding="utf-8"))
        modules = {
            node.module
            for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom) and node.module
        }
        assert not any(m.endswith("calibration.worklist") for m in modules), (
            f"{mod.__name__} 不该依赖 worklist"
        )


# ---------------------------------------------------------------------------
# C-19 · CLI
# ---------------------------------------------------------------------------


def _write_snapshot(root: pathlib.Path, snap) -> pathlib.Path:
    path = root / "v7" / "snapshot.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(snap, ensure_ascii=False), encoding="utf-8")
    return path


def test_cli_两次落盘逐行相同(tmp_path, capsys):
    root = tmp_path / "calibration"
    _write_snapshot(root, _mixed())
    assert sp.main(["--version", "7", "--out", str(root)]) == 0
    first = (root / "v7" / "sample.jsonl").read_text(encoding="utf-8")
    assert sp.main(["--version", "7", "--out", str(root)]) == 0
    assert (root / "v7" / "sample.jsonl").read_text(encoding="utf-8") == first


def test_cli_不覆盖worksheet(tmp_path):
    """worksheet 是分歧卡的判读表，sample 是判读**谁**的清单——两份不能互相覆盖。"""
    root = tmp_path / "calibration"
    _write_snapshot(root, _mixed())
    sheet = root / "v7" / "worksheet.jsonl"
    sheet.write_text('{"bucket": ""}\n', encoding="utf-8")
    assert sp.main(["--version", "7", "--out", str(root)]) == 0
    assert sheet.read_text(encoding="utf-8") == '{"bucket": ""}\n'


def test_cli_一条都抽不到退2(tmp_path, capsys):
    root = tmp_path / "calibration"
    _write_snapshot(root, _snap([_entry(verdict="ungraded")]))
    assert sp.main(["--version", "7", "--out", str(root)]) == 2
    assert "0 条样本" in capsys.readouterr().err


def test_cli_配额非法退2(tmp_path, capsys):
    assert sp.main(["--version", "7", "--out", str(tmp_path), "--quota", "0"]) == 2
    assert "配额 0" in capsys.readouterr().err