"""C-09 冻结时闸门的守门测试。

设计来源：``docs/设计方案/16-评估器与rubric双向校准回路方案.md`` §9.4（C-09）。

守的是六件事：

1. **闸门的触发点是 freeze，不是 push**：它比的是「这一版比上一版好」，
   所以缺省基线 = 本轮之前最近的一版；**没有基线 ⇒ 测不成（退 2）**，
   绝不是「首版通过」；
2. **退出码三态且 1 ≠ 2**：驳回（判据没过）与测不成（切分跨档 / 冻结档坏了 /
   没有基线）必须分开——把 2 写成 1，工具故障就被读成回退；
3. **同版本对同版本在预检就被拦**：那不是驳回也不是通过，是没比；
4. **每次重算快照**：盘上的旧 snapshot.json 不复用（同输入逐字节相同，
   重算不引入噪声，但复用会把上一轮的结论当这一轮的）；
5. **出快照与人工跑的是同一份实现**：闸门不抄一遍 ``agreement``；
6. **报告自带 ``gate_meta``**：哪两版比出来的写在文件里，不靠命令行历史。

⚠️ 夹具里**必须有回归集**：没有回归集的版本会让第四条判据 ``not_evaluable``
→ 驳回（那是 C-08 的口径：不能按"零回归"放行）。所以每个"应该通过"的
用例都要带上同一份冻结的 60 行里的几行。
"""

import io
import json
import pathlib
from contextlib import redirect_stderr, redirect_stdout

import pytest

from trajectory_pipeline.evaluation.calibration import gate, verify
from trajectory_pipeline.evaluation.golden import builder
from trajectory_pipeline.evaluation.golden.splits import SPLIT_FILES

PAYWALL = "subscription_wall"
SUCCESS = "success"


# ── 夹具：在临时目录里造「冻结档」 ─────────────────────────────


def _row(gid: str, *, gold: str = SUCCESS, branch: str = SUCCESS) -> dict:
    """与 ``test_calibration_verify._row`` 同形（不含 ``__split__``：档由文件定）。"""
    return {
        "golden_id": gid,
        "source_kind": "archive_outcome",
        "task_id": gid.split("__")[0],
        "run_id": "run00001",
        "url": f"https://{gid}.test/",
        "site_url": f"https://{gid}.test/",
        "code": {"branch": branch, "evidence": f"{gid} 的证据"},
        "golden": {"label": gold, "note": "", "dimension_scores": {}},
        "dimension_scores": {},
        "scores_detail": [],
    }


def _dump(path: pathlib.Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows),
        encoding="utf-8",
    )


def _version(root: pathlib.Path, n: int, **splits: list[dict]) -> pathlib.Path:
    """``root/v{N}/calibration.jsonl`` 等；键名即档名（calibration / regression / holdout）。"""
    vdir = root / f"v{n}"
    vdir.mkdir(parents=True, exist_ok=True)
    for split, rows in splits.items():
        _dump(vdir / SPLIT_FILES[split], rows)
    return vdir


def _pools(*, fixed: int = 2) -> tuple[dict, dict]:
    """两轮的行集合：基线有 ``fixed`` 条 PAYWALL 判错，本轮全判对。

    **回归集两轮完全相同**（它是冻结资产，跨轮只许一动不动）；
    另各配 1 条金标 SUCCESS 的对行——单靠 PAYWALL 会让 SUCCESS 类没有正样本，
    等价类 F1 的对比就少一类可看。
    """
    wrong = [
        _row(f"P{i:03d}__r1__h", gold=PAYWALL, branch=SUCCESS) for i in range(fixed)
    ]
    right = [_row(f"P{i:03d}__r1__h", gold=PAYWALL, branch=PAYWALL) for i in range(fixed)]
    ok = _row("S000__r1__h", gold=SUCCESS, branch=SUCCESS)
    reg = [_row("R000__r1__h", gold=SUCCESS, branch=SUCCESS)]
    return (
        {"calibration": wrong + [ok], "regression": reg},
        {"calibration": right + [ok], "regression": reg},
    )


def _tmp(tmp_path: pathlib.Path) -> tuple[pathlib.Path, pathlib.Path]:
    return tmp_path / "golden", tmp_path / "cal"


def _run_main(argv: list[str]) -> tuple[int, str, str]:
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        rc = gate.main(argv)
    return rc, out.getvalue(), err.getvalue()


# ── 1. 正常路径 ─────────────────────────────────────────────


def test_冻结新版本_自动出两版快照并通过(tmp_path):
    golden_root, cal = _tmp(tmp_path)
    base, curr = _pools(fixed=2)
    _version(golden_root, 7, **base)
    _version(golden_root, 8, **curr)

    report, code = gate.run(golden_root=golden_root, calibration_root=cal)

    assert code == gate.EXIT_PASS
    assert report["gate"] == verify.GATE_PASS
    assert report["calibration"]["d10"]["fixed"] == 2
    meta = report["gate_meta"]
    assert (meta["baseline_version"], meta["current_version"]) == (7, 8)
    # 两版快照都由闸门自己派生出来了
    assert pathlib.Path(meta["baseline_snapshot"]).exists()
    assert pathlib.Path(meta["current_snapshot"]).exists()
    assert meta["emitted"] is True
    assert meta["exit_codes"] == {"pass": 0, "reject": 1, "unable": 2}


def test_缺省基线是本轮之前最近的一版(tmp_path):
    golden_root, cal = _tmp(tmp_path)
    base, curr = _pools(fixed=1)
    for n in (5, 6, 7):
        _version(golden_root, n, **base)
    _version(golden_root, 8, **curr)

    report, _ = gate.run(golden_root=golden_root, calibration_root=cal)
    assert report["gate_meta"]["current_version"] == 8
    assert report["gate_meta"]["baseline_version"] == 7  # 不是 5，也不是 6


def test_闸门出快照与人工跑agreement是同一份实现(tmp_path):
    """闸门若自己拼一份快照，就多了一处会漂的地方。"""
    golden_root, cal = _tmp(tmp_path)
    base, curr = _pools(fixed=2)
    _version(golden_root, 7, **base)
    _version(golden_root, 8, **curr)

    gate.run(golden_root=golden_root, calibration_root=cal)
    ours = (cal / "v8" / "snapshot.json").read_text(encoding="utf-8")

    from trajectory_pipeline.evaluation.calibration.snapshot import (
        build_snapshot,
        write_snapshot,
    )

    rows = []
    for split, split_rows in curr.items():
        for row in json.loads(json.dumps(split_rows)):
            row["__split__"] = split
            rows.append(row)
    manual = write_snapshot(
        tmp_path / "manual" / "snapshot.json",
        build_snapshot(
            rows,
            golden_version=8,
            success_label=SUCCESS,
            rubric_version="",
            evaluator_version=builder.evaluator_version(),
            tolerance=1,
        ),
    )
    assert ours == manual.read_text(encoding="utf-8")


# ── 2. 测不成：没有基线 / 同一版 / 反着比 ──────────────────────


def test_首版没有基线_退2而不是通过(tmp_path):
    golden_root, cal = _tmp(tmp_path)
    base, _ = _pools()
    _version(golden_root, 7, **base)

    with pytest.raises(gate.GateError, match="首版"):
        gate.run(golden_root=golden_root, calibration_root=cal)


def test_基线与本轮同一版_预检就拦(tmp_path):
    golden_root, cal = _tmp(tmp_path)
    base, _ = _pools()
    _version(golden_root, 7, **base)
    _version(golden_root, 8, **base)

    with pytest.raises(gate.GateError, match="同一版本"):
        gate.run(
            golden_root=golden_root, calibration_root=cal,
            current_version=8, baseline_version=8,
        )


def test_基线晚于本轮_退2(tmp_path):
    golden_root, cal = _tmp(tmp_path)
    base, curr = _pools(fixed=1)
    _version(golden_root, 7, **base)
    _version(golden_root, 8, **curr)

    with pytest.raises(gate.GateError, match="晚于本轮"):
        gate.run(
            golden_root=golden_root, calibration_root=cal,
            current_version=7, baseline_version=8,
        )


def test_一个冻结版本都没有_退2(tmp_path):
    golden_root, cal = _tmp(tmp_path)
    golden_root.mkdir()
    with pytest.raises(gate.GateError, match="找不到任何冻结版本"):
        gate.run(golden_root=golden_root, calibration_root=cal)


def test_指定了不存在的版本_退2(tmp_path):
    golden_root, cal = _tmp(tmp_path)
    base, _ = _pools()
    _version(golden_root, 7, **base)
    with pytest.raises(gate.GateError, match="不是冻结版本"):
        gate.run(golden_root=golden_root, calibration_root=cal, current_version=9)


def test_冻结档出不了快照就是测不成(tmp_path):
    """v7 只有持有集 ⇒ agreement 的头条分母为空 ⇒ 抛 GoldenError ⇒ 退 2。"""
    golden_root, cal = _tmp(tmp_path)
    base, curr = _pools()
    _version(golden_root, 7, holdout=base["calibration"])
    _version(golden_root, 8, **curr)

    with pytest.raises(gate.GateError, match="出不了快照"):
        gate.run(golden_root=golden_root, calibration_root=cal)


def test_跨切档_退2且提示下一对可比的是后继(tmp_path):
    golden_root, cal = _tmp(tmp_path)
    base, curr = _pools(fixed=2)
    _version(golden_root, 7, **base)
    # v8 把两条从调参池切进了回归集 —— C-17 切档，逐档不可比
    _version(
        golden_root, 8,
        calibration=curr["calibration"][:1],
        regression=curr["regression"] + curr["calibration"][1:],
    )

    with pytest.raises(gate.GateError) as ei:
        gate.run(golden_root=golden_root, calibration_root=cal)
    msg = str(ei.value)
    assert "换了档" in msg
    assert "后继" in msg  # 解法不是修门，是等下一版


# ── 3. 驳回：判据没过 ────────────────────────────────────────


def test_净改善为0_退1而不是退2(tmp_path):
    golden_root, cal = _tmp(tmp_path)
    base, _ = _pools()
    _version(golden_root, 7, **base)
    _version(golden_root, 8, **base)

    report, code = gate.run(golden_root=golden_root, calibration_root=cal)
    assert report["gate"] == verify.GATE_REJECT
    assert code == gate.EXIT_REJECT
    assert code != gate.EXIT_UNABLE


def test_回归集反转_退1(tmp_path):
    golden_root, cal = _tmp(tmp_path)
    base, curr = _pools(fixed=2)
    reg_ok = _row("R000__r1__h", gold=SUCCESS, branch=SUCCESS)
    reg_bad = _row("R000__r1__h", gold=SUCCESS, branch=PAYWALL)  # 原本对，现在错
    _version(golden_root, 7, calibration=base["calibration"], regression=[reg_ok])
    _version(golden_root, 8, calibration=curr["calibration"], regression=[reg_bad])

    report, code = gate.run(golden_root=golden_root, calibration_root=cal)
    assert report["regression"]["d10"]["regressed"] == 1
    assert (
        report["criteria"]["regression_regressed_zero"]["status"]
        == verify.CRITERION_FAIL
    )
    assert code == gate.EXIT_REJECT


def test_没有回归集时不许按零回归放行(tmp_path):
    """C-08 第四条：``not_evaluable`` 即驳回——回归集缺席不是"零回归"。"""
    golden_root, cal = _tmp(tmp_path)
    base, curr = _pools(fixed=2)
    _version(golden_root, 7, calibration=base["calibration"])
    _version(golden_root, 8, calibration=curr["calibration"])

    report, code = gate.run(golden_root=golden_root, calibration_root=cal)
    assert (
        report["criteria"]["regression_regressed_zero"]["status"]
        == verify.CRITERION_NOT_EVALUABLE
    )
    assert code == gate.EXIT_REJECT


# ── 4. 快照每次重算 ─────────────────────────────────────────


def test_每次重算快照_不复用盘上的旧文件(tmp_path):
    golden_root, cal = _tmp(tmp_path)
    base, curr = _pools(fixed=2)
    _version(golden_root, 7, **base)
    _version(golden_root, 8, **curr)

    _, first = gate.run(golden_root=golden_root, calibration_root=cal)
    assert first == gate.EXIT_PASS

    # 本轮冻结档被改回"没改善"：盘上还留着上一轮那张"通过"的快照
    _version(golden_root, 8, **base)
    report, second = gate.run(golden_root=golden_root, calibration_root=cal)
    assert second == gate.EXIT_REJECT  # 复用了旧快照就会还是 0
    assert report["calibration"]["d10"]["net"] == 0


def test_no_emit缺快照就是测不成(tmp_path):
    golden_root, cal = _tmp(tmp_path)
    base, curr = _pools()
    _version(golden_root, 7, **base)
    _version(golden_root, 8, **curr)

    with pytest.raises(gate.GateError, match="--no-emit"):
        gate.run(golden_root=golden_root, calibration_root=cal, emit=False)


def test_no_emit可用盘上已有的快照(tmp_path):
    golden_root, cal = _tmp(tmp_path)
    base, curr = _pools(fixed=2)
    _version(golden_root, 7, **base)
    _version(golden_root, 8, **curr)
    gate.run(golden_root=golden_root, calibration_root=cal)

    report, code = gate.run(
        golden_root=golden_root, calibration_root=cal, emit=False
    )
    assert code == gate.EXIT_PASS
    assert report["gate_meta"]["emitted"] is False


# ── 5. CLI：1 与 2 分开报 ───────────────────────────────────


def test_CLI驳回报1并说清不是修门(tmp_path):
    golden_root, cal = _tmp(tmp_path)
    base, _ = _pools()
    _version(golden_root, 7, **base)
    _version(golden_root, 8, **base)

    rc, out, _ = _run_main([
        "--golden-root", str(golden_root),
        "--calibration-root", str(cal),
    ])
    assert rc == gate.EXIT_REJECT
    assert "驳回" in out
    assert "修改动" in out


def test_CLI测不成报2且不叫驳回(tmp_path):
    golden_root, cal = _tmp(tmp_path)
    base, _ = _pools()
    _version(golden_root, 7, **base)

    rc, _, err = _run_main([
        "--golden-root", str(golden_root),
        "--calibration-root", str(cal),
    ])
    assert rc == gate.EXIT_UNABLE
    assert "测不成" in err
    assert "不是驳回" in err


def test_CLI落报告带gate_meta(tmp_path):
    golden_root, cal = _tmp(tmp_path)
    base, curr = _pools(fixed=2)
    _version(golden_root, 7, **base)
    _version(golden_root, 8, **curr)
    out_json = tmp_path / "report.json"

    rc, _, _ = _run_main([
        "--golden-root", str(golden_root),
        "--calibration-root", str(cal),
        "--json-out", str(out_json),
    ])
    assert rc == gate.EXIT_PASS
    report = json.loads(out_json.read_text(encoding="utf-8"))
    assert report["gate_meta"]["baseline_version"] == 7
    assert report["gate_meta"]["current_version"] == 8
    assert report["gate_meta"]["emitted"] is True


def test_分诊与目标桶透传(tmp_path):
    """闸门自己不算分诊——它把表交给 verify，桶没清零照样驳回。"""
    golden_root, cal = _tmp(tmp_path)
    base, curr = _pools(fixed=1)
    _version(golden_root, 7, **base)
    # 本轮还有一条没修（P001 仍判错），分诊表把它归到 eq_class_mismatch
    extra = _row("P001__r1__h", gold=PAYWALL, branch=SUCCESS)
    _version(
        golden_root, 8,
        calibration=curr["calibration"] + [extra],
        regression=curr["regression"],
    )
    triage = [{
        "golden_id": "P001__r1__h",
        "dimension": "d10",
        "bucket": "evaluator_logic",
        "triage_by": "owner",
        "triage_evidence": "按明写条款应得另一个值（金标无歧义）",
    }]

    report, code = gate.run(
        golden_root=golden_root, calibration_root=cal,
        triage=triage, buckets=["evaluator_logic"],
    )
    assert report["criteria"]["bucket_clear"]["status"] == verify.CRITERION_FAIL
    assert code == gate.EXIT_REJECT