"""D-10 三态口径只读读数的单测。

这个模块回答的是一个很具体的问题：**现口径的一致率把「代码弃权」算成了
「代码判错」**。测试要钉住的就是这个区分，以及它不能带来别的副作用。

钉住的六件事：

1. **三态语义**：两侧都有结论且等价 → ``agree``；都有结论但不等价 →
   ``disagree``；任一侧没结论 → ``undetermined`` + 归因方向
   （``both`` / ``pred`` / ``gold`` / ``missing``）；
2. **弃权不进分母**：``unresolved`` 这类「不表达结论」的分支落 UNKNOWN
   等价类，在现口径下**算一致**（同类的 unresolved 对 unresolved）——读数
   必须把它拆成「未判定」，否则 39% 的弃权率会被读成 91% 的质量；
3. **等价类只有一份**：``blocked`` 与 ``login_required`` 判 ``agree``
   且带 ``detail_mismatch``——三态判定走的是
   ``golden/alignment.EQUIV_CLASS``，不是另抄的表；
4. **署名分层用现成分类**：``reesn`` / ``manual-review`` → ``human``，
   ``ai-assisted-inc33`` → ``automated``（模型起草、人没逐条看）；
5. **现口径列要复现现读数**：从原始行按 ``cmd_agreement`` 的公式重算，
   必须与快照派生的现口径列**逐字相等**——不等就是快照与 builder 已经
   对不上了；
6. **只读**：跑一次读数**不落任何文件**。
"""

from __future__ import annotations

import io
import json
import pathlib
from contextlib import redirect_stdout

import pytest

from trajectory_pipeline.evaluation.calibration import d10_readout
from trajectory_pipeline.evaluation.calibration.d10_readout import (
    SIDE_BOTH,
    SIDE_GOLD,
    SIDE_MISSING,
    SIDE_PRED,
    STATE_AGREE,
    STATE_DISAGREE,
    STATE_UNDETERMINED,
)


# ── 夹具 ──────────────────────────────────────────────────────


def _row(
    golden_id: str,
    *,
    gold_label: str | None = "success",
    branch: str | None = "success",
    reviewed_by: str | None = "manual-review",
    split: str = "calibration",
    source_kind: str = "archive_outcome",
) -> dict:
    """一条冻结档行（形状照 ``evaluation/golden/data/v*/`` 里的行）。"""
    return {
        "golden_id": golden_id,
        "golden_id_legacy": "",
        "source_kind": source_kind,
        "task_id": "T001",
        "run_id": "abc12345",
        "url": f"https://{golden_id}.test/",
        "code": {"branch": branch, "evidence": "证据"},
        "golden": {
            "label": gold_label,
            "note": "备注",
            "reviewed_by": reviewed_by,
        },
        "__split__": split,
    }


def _entry(
    golden_id: str,
    *,
    gold_value: str,
    pred_value: str,
    verdict: str = "agree",
    split: str = "calibration",
    surface_comparable: bool | None = None,
) -> dict:
    """一条快照条目（形状照 ``calibration/v*/snapshot.json`` 里的条目）。"""
    return {
        "golden_id": golden_id,
        "golden_id_legacy": "",
        "split": split,
        "dimension": "d10",
        "rubric_ref": "D-10",
        "gold_value": gold_value,
        "pred_value": pred_value,
        "pred_provider": "code:frozen_row@unrecorded",
        "verdict": verdict,
        "surface_comparable": surface_comparable,
    }


def _snapshot(entries: list[dict], *, version: int = 16) -> dict:
    return {
        "schema_version": "2",
        "meta": {
            "golden_version": version,
            "rubric_version": "1.2",
            "evaluator_version": "test",
            "code_version": "test",
            "tolerance": 1,
            "success_label": "success",
            "hard_dim": "d10",
        },
        "entries": entries,
    }


# ── 1. 三态语义 ───────────────────────────────────────────────


def test_两侧都有结论且等价_判一致():
    state = d10_readout.hard_agreement("success", "success")
    assert state.state == STATE_AGREE
    assert state.counts_in_denominator


def test_等价类相同但标签不同_判一致并记细分():
    """``blocked`` 与 ``login_required`` 属同一等价类（ACCESS_BLOCKED）。

    三态判定必须走 :data:`golden.alignment.EQUIV_CLASS`——**不另抄一份
    映射**。抄一份就必然漂：``click`` 那次教训就是这个形状。"""
    state = d10_readout.hard_agreement("login_required", "blocked")
    assert state.state == STATE_AGREE
    assert state.detail == d10_readout.DETAIL_LABEL_MISMATCH
    assert state.counts_in_denominator


def test_两侧都有结论但不等价_判冲突():
    state = d10_readout.hard_agreement("success", "paywall")
    assert state.state == STATE_DISAGREE
    assert state.counts_in_denominator


def test_代码弃权而金标有结论_判未判定且归因到代码():
    """``unresolved`` = 采集侧按 fail-closed「判不出就不判」。

    现口径把它算成 ``disagree``——**惩罚守纪律的那一方**。三态口径必须
    判成「未判定」，归因方向是 ``pred``（是 b 没给出结论）。"""
    state = d10_readout.hard_agreement("success", "unresolved")
    assert state.state == STATE_UNDETERMINED
    assert state.side == SIDE_PRED
    assert not state.counts_in_denominator


def test_金标没结论而代码有结论_判未判定且归因到金标():
    state = d10_readout.hard_agreement("player_unverified", "success")
    assert state.state == STATE_UNDETERMINED
    assert state.side == SIDE_GOLD
    assert not state.counts_in_denominator


def test_双方都没结论_判未判定且归因到两侧():
    """这是本模块存在的**主要理由**。

    现口径下 ``unresolved`` 对 ``unresolved`` 是**同一等价类**⇒ 算「一致」，
    于是「谁都没判出来」被记成「判对了」。读数必须把它拆成「未判定」。"""
    state = d10_readout.hard_agreement("unresolved", "unresolved")
    assert state.state == STATE_UNDETERMINED
    assert state.side == SIDE_BOTH
    assert not state.counts_in_denominator


def test_任一侧为空_判未判定且归因为缺失():
    assert d10_readout.hard_agreement(None, "success").side == SIDE_MISSING
    assert d10_readout.hard_agreement("success", "").side == SIDE_MISSING
    assert d10_readout.hard_agreement("success", None).state == STATE_UNDETERMINED


def test_路由记账不是结论():
    """``site_search_routed`` 是**分支记账**，不是结论——与 UNKNOWN 同归
    「不表达结论」那一类。把它算进分母就是拿记账冒充判定。"""
    assert not d10_readout.is_conclusive("site_search_routed")
    assert not d10_readout.is_conclusive("unresolved")
    assert not d10_readout.is_conclusive("player_unverified")
    assert d10_readout.is_conclusive("success")
    assert d10_readout.is_conclusive("paywall")


# ── 2. 逐行分类 ───────────────────────────────────────────────


def test_逐行分类_把三方拆开():
    rows = [
        _row("g1", gold_label="success", branch="success"),
        _row("g2", gold_label="success", branch="paywall"),
        _row("g3", gold_label="success", branch="unresolved"),
        _row("g4", gold_label="unresolved", branch="unresolved"),
    ]
    entries = [
        _entry("g1", gold_value="success", pred_value="success"),
        _entry("g2", gold_value="success", pred_value="paywall", verdict="disagree"),
        _entry("g3", gold_value="success", pred_value="unresolved", verdict="disagree"),
        _entry("g4", gold_value="unresolved", pred_value="unresolved"),
    ]
    views = {v["golden_id"]: v for v in d10_readout.classify_rows(rows, _snapshot(entries))}
    assert views["g1"]["state"] == STATE_AGREE
    assert views["g2"]["state"] == STATE_DISAGREE
    assert views["g3"]["state"] == STATE_UNDETERMINED
    assert views["g3"]["side"] == SIDE_PRED
    assert views["g4"]["state"] == STATE_UNDETERMINED
    assert views["g4"]["side"] == SIDE_BOTH


def test_代码没给结构化结论_单独记一栏():
    """``code.branch`` 为空时，快照会兜底成 ``success``（``_hard_entry`` 的
    ``or success_label``）。

    不把这栏单记出来，「b 根本没输出」就会被读成「b 判成功」——现口径
    里有 27 行正是这样被算成一致的。"""
    rows = [_row("g1", gold_label="success", branch=None)]
    entries = [_entry("g1", gold_value="success", pred_value="success")]
    views = d10_readout.classify_rows(rows, _snapshot(entries))
    assert views[0]["raw_pred_missing"] is True
    assert views[0]["raw_pred_branch"] == ""
    assert views[0]["pred_value"] == "success"


def test_兜底碰巧同侧_单独计数():
    """兜底与金标同侧时**数对了，但数对的不是 b**。

    这两件事必须分开记：27 行里全是「兜底成 success 且金标也是 success」，
    看着像 b 判对了一堆；实际是 ``code.branch`` 字段缺失（证据还在），
    快照的 ``or success_label`` 兜底把它算成一致。机制是错的，只是这次
    碰巧没出错——金标若是 paywall，同一个兜底就会造出一个假分歧。"""
    rows = [
        _row("g1", gold_label="success", branch=None),
        _row("g2", gold_label="paywall", branch=None),
    ]
    entries = [
        _entry("g1", gold_value="success", pred_value="success"),
        _entry("g2", gold_value="paywall", pred_value="success", verdict="disagree"),
    ]
    views = d10_readout.classify_rows(rows, _snapshot(entries))
    bucket = d10_readout._tally(views)
    assert bucket["raw_pred_missing"] == 2
    assert bucket["raw_pred_missing_legacy_agreed"] == 1


def test_署名分层用现成分类():
    """分层走 ``golden/signatures.classify_signature``——不另划一套。

    ``ai-assisted-inc33`` 是「模型起草、人没逐条看」⇒ 算 ``automated``，
    这条口径在仓库里已有契约测试盯住，这里只验读数确实用了它。"""
    rows = [
        _row("g1", reviewed_by="manual-review"),
        _row("g2", reviewed_by="reesn"),
        _row("g3", reviewed_by="ai-assisted-inc33"),
        _row("g4", reviewed_by=""),
    ]
    entries = [
        _entry("g1", gold_value="success", pred_value="success"),
        _entry("g2", gold_value="success", pred_value="success"),
        _entry("g3", gold_value="success", pred_value="success"),
        _entry("g4", gold_value="success", pred_value="success"),
    ]
    views = d10_readout.classify_rows(rows, _snapshot(entries))
    kinds = {v["golden_id"]: v["signature_kind"] for v in views}
    assert kinds["g1"] == "human"
    assert kinds["g2"] == "human"
    assert kinds["g3"] == "automated"
    assert kinds["g4"] == "unknown"


def test_跨面不可判定_保持三值不折成否():
    """``surface_comparable = None`` ⇒ 不可判定。

    跨面不可比**不能**折成「不一致」——那是把「没记录面」当成「面不同」。
    本模块只把这一栏数出来，不重算。"""
    rows = [_row("g1")]
    entries = [
        _entry("g1", gold_value="success", pred_value="success", surface_comparable=None)
    ]
    views = d10_readout.classify_rows(rows, _snapshot(entries))
    assert views[0]["surface_comparable"] is None
    report = d10_readout.summarize(
        views, version=16, snapshot_path=pathlib.Path("x"), golden_root=pathlib.Path("y")
    )
    assert report["overall"]["surface_comparable_none"] == 1


# ── 3. 汇总口径 ───────────────────────────────────────────────


def test_分母只含双方都有结论的行():
    rows = [
        _row("g1", gold_label="success", branch="success"),
        _row("g2", gold_label="success", branch="paywall"),
        _row("g3", gold_label="success", branch="unresolved"),
        _row("g4", gold_label="unresolved", branch="unresolved"),
    ]
    entries = [
        _entry("g1", gold_value="success", pred_value="success"),
        _entry("g2", gold_value="success", pred_value="paywall", verdict="disagree"),
        _entry("g3", gold_value="success", pred_value="unresolved", verdict="disagree"),
        _entry("g4", gold_value="unresolved", pred_value="unresolved"),
    ]
    views = d10_readout.classify_rows(rows, _snapshot(entries))
    bucket = d10_readout._tally(views)
    assert bucket["n"] == 4
    assert bucket["states"][STATE_AGREE] == 1
    assert bucket["states"][STATE_DISAGREE] == 1
    assert bucket["states"][STATE_UNDETERMINED] == 2
    assert bucket["denominator"] == 2
    assert bucket["agreement_three_state"] == 0.5
    assert bucket["abstention_rate"] == 0.5
    # 现口径：双方都没结论那行算「一致」（同 UNKNOWN 等价类）
    assert bucket["legacy_agreed"] == 2
    assert bucket["legacy_agreement"] == 0.5


def test_弃权率与一致率分开记账():
    """把弃权行从分母里拿掉 ⇒ 一致率上升。

    只报一致率不报弃权率，就会把「排除项」读成「成绩」——这是本读数
    最容易被误用的地方，所以两个数**必须**同时在报表里。"""
    rows = [
        _row("g1", gold_label="success", branch="success"),
        _row("g2", gold_label="success", branch="unresolved"),
    ]
    entries = [
        _entry("g1", gold_value="success", pred_value="success"),
        _entry("g2", gold_value="success", pred_value="unresolved", verdict="disagree"),
    ]
    views = d10_readout.classify_rows(rows, _snapshot(entries))
    bucket = d10_readout._tally(views)
    assert bucket["agreement_three_state"] == 1.0
    assert bucket["abstention_rate"] == 0.5
    assert bucket["legacy_agreement"] == 0.5


# ── 4. 现口径列复现现读数 ─────────────────────────────────────


def test_现口径列_从原始行重算复现_cmd_agreement_公式():
    """现口径的公式（``golden/builder.py::cmd_agreement``）::

        is_equivalent(gold_label or success, code.branch or success)

    分母 = 站点级已标注行（``source_kind != "p2_sample"`` 且有 ``golden``）。
    读数的现口径列必须与它**逐字一致**——这一列是用来跟历史读数对齐的。"""
    rows = [
        _row("g1", gold_label="success", branch="success"),
        _row("g2", gold_label="success", branch="paywall"),
        _row("g3", gold_label="login_required", branch="blocked"),
        _row("g4", gold_label="success", branch=None),
        _row("g5", gold_label="success", branch="success", split="regression"),
        _row("g6", gold_label="success", branch="success", source_kind="p2_sample"),
    ]
    got = d10_readout.legacy_recompute(rows, success_label="success")
    # g1 一致；g2 不一致；g3 同等价类一致；g4 branch 空 ⇒ 兜底 success ⇒ 一致；
    # g5 属回归档；g6 是轨迹级（不进分母）。
    assert got["calibration"] == {"n": 4, "agreed": 3, "ratio": 0.75}
    assert got["regression"] == {"n": 1, "agreed": 1, "ratio": 1.0}
    assert got["__all__"]["n"] == 5
    assert got["__all__"]["agreed"] == 4


def test_自检_快照派生列与重算列一致():
    rows = [
        _row("g1", gold_label="success", branch="success"),
        _row("g2", gold_label="success", branch="paywall"),
    ]
    entries = [
        _entry("g1", gold_value="success", pred_value="success"),
        _entry("g2", gold_value="success", pred_value="paywall", verdict="disagree"),
    ]
    views = d10_readout.classify_rows(rows, _snapshot(entries))
    report = d10_readout.summarize(
        views,
        version=16,
        snapshot_path=pathlib.Path("x"),
        golden_root=pathlib.Path("y"),
        rows=rows,
        success_label="success",
    )
    assert report["self_check"]["all_match"] is True
    for check in report["self_check"]["checks"]:
        assert check["matches"] is True


def test_自检_对不上时必须报出来():
    """自检**不掩盖**不一致：快照与 builder 已经对不上时读数必须说。"""
    rows = [_row("g1", gold_label="success", branch="success")]
    entries = [_entry("g1", gold_value="success", pred_value="success")]
    views = d10_readout.classify_rows(rows, _snapshot(entries))
    # 造一个与行不一致的快照列（快照说 disagree，行重算是 agree）。
    views[0]["legacy_verdict"] = "disagree"
    report = d10_readout.summarize(
        views,
        version=16,
        snapshot_path=pathlib.Path("x"),
        golden_root=pathlib.Path("y"),
        rows=rows,
        success_label="success",
    )
    assert report["self_check"]["all_match"] is False


# ── 5. fail-closed ────────────────────────────────────────────


def test_快照版本与冻结档不一致_报错():
    rows = [_row("g1")]
    entries = [_entry("g1", gold_value="success", pred_value="success")]
    snap = _snapshot(entries, version=15)
    with pytest.raises(d10_readout.ReadoutError, match="不是同一版"):
        d10_readout.snapshot_facts(snap, version=16)


def test_快照条目在冻结档里找不到_报错():
    """对不上的行**不静默丢掉**——那是两个东西对不上，不是「这一行不算」。"""
    rows = [_row("g1")]
    entries = [
        _entry("g1", gold_value="success", pred_value="success"),
        _entry("gX", gold_value="success", pred_value="success"),
    ]
    with pytest.raises(d10_readout.ReadoutError, match="找不到对应行"):
        d10_readout.classify_rows(rows, _snapshot(entries))


def test_快照里golden_id重复_报错():
    rows = [_row("g1")]
    entries = [
        _entry("g1", gold_value="success", pred_value="success"),
        _entry("g1", gold_value="success", pred_value="paywall"),
    ]
    with pytest.raises(d10_readout.ReadoutError, match="重复"):
        d10_readout.classify_rows(rows, _snapshot(entries))


def test_快照文件不存在_报错而不是读成零():
    with pytest.raises(d10_readout.ReadoutError, match="不存在"):
        d10_readout.load_snapshot(pathlib.Path("不存在的路径.json"))


def test_版本台账读不出来_报错():
    with pytest.raises(d10_readout.ReadoutError):
        d10_readout.latest_version(pathlib.Path("不存在的目录"))


# ── 6. CLI：只读 ──────────────────────────────────────────────


def _write_golden(root: pathlib.Path, rows: list[dict], version: int = 16) -> None:
    vdir = root / f"v{version}"
    vdir.mkdir(parents=True)
    calib = [r for r in rows if r["__split__"] == "calibration"]
    (vdir / "calibration.jsonl").write_text(
        "\n".join(json.dumps(r, ensure_ascii=False) for r in calib) + "\n",
        encoding="utf-8",
    )
    (root / "versions.jsonl").write_text(
        json.dumps({"version": version}) + "\n", encoding="utf-8"
    )


def test_cli_跑起来且不落任何文件(tmp_path):
    rows = [
        _row("g1", gold_label="success", branch="success"),
        _row("g2", gold_label="success", branch="paywall"),
        _row("g3", gold_label="success", branch="unresolved"),
    ]
    golden_root = tmp_path / "golden"
    _write_golden(golden_root, rows)
    snapshot_path = tmp_path / "snapshot.json"
    snapshot_path.write_text(
        json.dumps(
            _snapshot(
                [
                    _entry("g1", gold_value="success", pred_value="success"),
                    _entry("g2", gold_value="success", pred_value="paywall", verdict="disagree"),
                    _entry("g3", gold_value="success", pred_value="unresolved", verdict="disagree"),
                ]
            ),
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    before = {p for p in tmp_path.rglob("*") if p.is_file()}
    buf = io.StringIO()
    with redirect_stdout(buf):
        rc = d10_readout.main(
            [
                "--version",
                "16",
                "--golden-root",
                str(golden_root),
                "--snapshot",
                str(snapshot_path),
            ]
        )
    assert rc == 0
    out = buf.getvalue()
    assert "三态口径" in out
    assert "自检" in out
    # **只读**：跑完不落任何文件
    after = {p for p in tmp_path.rglob("*") if p.is_file()}
    assert after == before


def test_cli_快照缺失_退2(tmp_path, capsys):
    golden_root = tmp_path / "golden"
    _write_golden(golden_root, [_row("g1")])
    rc = d10_readout.main(
        [
            "--version",
            "16",
            "--golden-root",
            str(golden_root),
            "--snapshot",
            str(tmp_path / "没有这个快照.json"),
        ]
    )
    assert rc == 2
    assert "不存在" in capsys.readouterr().err


def test_cli_默认版本取台账最新(tmp_path):
    rows = [_row("g1")]
    golden_root = tmp_path / "golden"
    _write_golden(golden_root, rows, version=16)
    snapshot_path = tmp_path / "snapshot.json"
    snapshot_path.write_text(
        json.dumps(
            _snapshot([_entry("g1", gold_value="success", pred_value="success")]),
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    buf = io.StringIO()
    with redirect_stdout(buf):
        rc = d10_readout.main(
            ["--golden-root", str(golden_root), "--snapshot", str(snapshot_path)]
        )
    assert rc == 0
    assert "v16" in buf.getvalue()
