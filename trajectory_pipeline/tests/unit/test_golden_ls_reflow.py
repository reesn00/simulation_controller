"""**回流**：LS 上的人工裁定 → 金标（方案 22 §9 R5 / 台账 M-52）。

守的是七件事：

1. **两类裁定分两条路**：「人确认了金标」⇒ 只补观测面（不换尺子）；
   「人说金标该改」⇒ 改标签（换尺子 ⇒ 新基线）；
2. **等价类判定**：``blocked`` 与 ``login_wall_or_blocked`` 判一致——字面比
   会把「其实一样」误判成改判，而误判成改判的后果是**白改一次金标 +
   白耗一个版本号**；
3. **三栏必须由人填**：``access_surface`` / ``observed_at``（LS 表单里没有
   面控件 ⇒ 推不出来）、改判型的 ``reviewed_by``；缺一栏 ⇒ **退 2**，
   **不按默认面补**；
4. **署名不许静默退化**：改判型的 ``reviewed_by`` 必须是**人**的署名，
   否则这条「人核行」会悄悄变成「机器写的」；
5. **holdout 不回流**（它要保持没被人看过）；**回归集不得改动**（C-17）；
6. **两套署名分列**（裁定④）：确认型**不改**金标侧署名（标签没变，
   「谁当初定了这个标签」还是那个人）；LS 侧复核人只留在工作表里；
7. **写回走 ``builder annotate``**：不另写一套合并逻辑（那是唯一真源）。
"""

import io
import json
import pathlib
from contextlib import redirect_stderr, redirect_stdout

from trajectory_pipeline.evaluation.golden_ls import reflow as reflow_mod


# ── 工具 ────────────────────────────────────────────────────


def _run(argv: list[str]) -> tuple[int, str, str]:
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        from trajectory_pipeline.evaluation.golden_ls import cli

        try:
            rc = cli.main(argv)
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


def _record(gid: str, **over) -> dict:
    row = {
        "golden_id": gid,
        "ls_task_id": 1,
        "split": "calibration",
        "verdict": "agree",
        "human_branch": "（不可用）",
        "reason": "（不可用）",
        "note": "（不可用）",
        "annotator": " reesn@qq.com",
        "annotator_kind": "human",
        "annotated_at": "2026-10-29T06:03:22.123456Z",
        "annotation_count": 1,
    }
    row.update(over)
    return row


def _gold(gid: str, label: str = "success", split: str = "calibration") -> dict:
    return {
        "golden_id": gid,
        "__split__": split,
        "golden": {"label": label, "label_label": "成功", "reviewed_by": "manual-review"},
    }


def _ann(gid: str, label: str = "success", **over) -> dict:
    row = {
        "golden_id": gid,
        "round": 1,
        "label": label,
        "label_label": "成功",
        "reviewed_by": "manual-review",
        "reviewed_at": "2026-10-12T04:01:21+00:00",
        "note": "",
        "dimension_scores": {"d2": 5, "d4": 5, "d5": 1, "d6": 3},
    }
    row.update(over)
    return row


# ── 分类 ────────────────────────────────────────────────────


def test_agree算确认型():
    kind, _ = reflow_mod.classify(
        _record("g1", verdict="agree"),
        gold_label="success",
        split="calibration",
        annotation_label="success",
    )
    assert kind == reflow_mod.KIND_CONFIRM


def test_disagree但分支等价算确认型():
    """``blocked`` 与 ``login_wall_or_blocked`` 同等价类 ⇒ 不是改判。"""
    kind, _ = reflow_mod.classify(
        _record("g1", verdict="disagree", human_branch="login_wall_or_blocked"),
        gold_label="blocked",
        split="calibration",
        annotation_label="blocked",
    )
    assert kind == reflow_mod.KIND_CONFIRM


def test_disagree且分支不等价算改判型():
    kind, _ = reflow_mod.classify(
        _record("g1", verdict="disagree", human_branch="not_play_site"),
        gold_label="unresolved",
        split="calibration",
        annotation_label="unresolved",
    )
    assert kind == reflow_mod.KIND_RELABEL


def test_判不了与没复核都不回流():
    for record, reason in (
        (_record("g1", verdict="unverifiable"), reflow_mod.SKIP_UNVERIFIABLE),
        (_record("g1", verdict="（不可用）"), reflow_mod.SKIP_NOT_REVIEWED),
    ):
        kind, why = reflow_mod.classify(
            record,
            gold_label="success",
            split="calibration",
            annotation_label="success",
        )
        assert kind == "skip"
        assert why == reason


def test_选了不一致但没给分支不回流():
    kind, why = reflow_mod.classify(
        _record("g1", verdict="disagree", human_branch="（不可用）"),
        gold_label="success",
        split="calibration",
        annotation_label="success",
    )
    assert kind == "skip"
    assert why == reflow_mod.SKIP_NO_BRANCH


def test_holdout不回流():
    kind, why = reflow_mod.classify(
        _record("g1", verdict="disagree", human_branch="not_play_site"),
        gold_label="success",
        split="holdout",
        annotation_label="success",
    )
    assert kind == "skip"
    assert why == reflow_mod.SKIP_HOLDOUT


def test_标注与冻结档标签不一致时不回流():
    kind, why = reflow_mod.classify(
        _record("g1", verdict="agree"),
        gold_label="success",
        split="calibration",
        annotation_label="blocked",
    )
    assert kind == "skip"
    assert why == reflow_mod.SKIP_LABEL_DRIFT


# ── 工作表 ──────────────────────────────────────────────────


def test_工作表三栏留空且确认型带出原署名():
    records = [_record("g1", verdict="agree")]
    gold_rows = {"g1": _gold("g1")}
    ann_rows = {"g1": _ann("g1")}
    rows, summary = reflow_mod.build_worksheet(
        records, gold_rows=gold_rows, ann_rows=ann_rows
    )
    assert summary["confirm"] == 1
    row = rows[0]
    assert row["kind"] == reflow_mod.KIND_CONFIRM
    # 必填三栏留空（推不出来）
    assert row["access_surface"] == ""
    assert row["observed_at"] == ""
    # 确认型：署名原样带出（标签没变，署名就不该变）
    assert row["reviewed_by"] == "manual-review"
    assert row["label"] == "success"
    assert row["dimension_scores"] == {"d2": 5, "d4": 5, "d5": 1, "d6": 3}
    # LS 侧复核人**单列**（裁定④）
    assert row["ls_annotator"] == " reesn@qq.com"


def test_工作表改判型把金标署名留空():
    records = [_record("g1", verdict="disagree", human_branch="not_play_site")]
    gold_rows = {"g1": _gold("g1", label="unresolved")}
    ann_rows = {"g1": _ann("g1", label="unresolved")}
    rows, summary = reflow_mod.build_worksheet(
        records, gold_rows=gold_rows, ann_rows=ann_rows
    )
    assert summary["relabel"] == 1
    row = rows[0]
    assert row["kind"] == reflow_mod.KIND_RELABEL
    assert row["label"] == "unresolved"
    assert row["reviewed_by"] == ""  # 「谁重新定了标签」是人的事实
    assert row["confirm_label_change"] is False


def test_跳过的行逐条记原因():
    records = [
        _record("g1", verdict="agree"),
        _record("g2", verdict="unverifiable"),
        _record("g3", verdict="agree"),
    ]
    gold_rows = {
        "g1": _gold("g1"),
        "g2": _gold("g2"),
        "g3": _gold("g3", split="holdout"),
    }
    ann_rows = {"g1": _ann("g1"), "g2": _ann("g2"), "g3": _ann("g3")}
    rows, summary = reflow_mod.build_worksheet(
        records, gold_rows=gold_rows, ann_rows=ann_rows
    )
    assert len(rows) == 1
    assert summary["skipped"] == 2
    reasons = {item["golden_id"]: item["reason"] for item in summary["skipped_detail"]}
    assert reasons["g2"] == reflow_mod.SKIP_UNVERIFIABLE
    assert reasons["g3"] == reflow_mod.SKIP_HOLDOUT


# ── 校验（fail-closed）─────────────────────────────────────


def _ws(**over) -> dict:
    row = {
        "kind": reflow_mod.KIND_CONFIRM,
        "golden_id": "g1",
        "split": "calibration",
        "round": 1,
        "label": "success",
        "label_label": "成功",
        "reviewed_by": "manual-review",
        "reviewed_at": "2026-10-12T04:01:21+00:00",
        "note": "",
        "access_surface": "human-browser",
        "observed_at": "2026-10-29T14:03:00+08:00",
        "dimension_scores": {"d2": 5, "d4": 5, "d5": 1, "d6": 3},
    }
    row.update(over)
    return row


def test_面空时不合格():
    errors = reflow_mod.validate([_ws(access_surface="")], gold_rows={"g1": _gold("g1")})
    assert any("access_surface" in e for e in errors)


def test_面认不出时不合格():
    errors = reflow_mod.validate(
        [_ws(access_surface="人面")], gold_rows={"g1": _gold("g1")}
    )
    assert errors


def test_时刻不是ISO时不合格():
    errors = reflow_mod.validate(
        [_ws(observed_at="上周三")], gold_rows={"g1": _gold("g1")}
    )
    assert any("ISO" in e for e in errors)


def test_改判型署名不是人的时不合格():
    errors = reflow_mod.validate(
        [
            _ws(
                kind=reflow_mod.KIND_RELABEL,
                reviewed_by="agent-vlm",
                confirm_label_change=True,
            )
        ],
        gold_rows={"g1": _gold("g1")},
    )
    assert any("reviewed_by" in e for e in errors)


def test_改判型没显式确认时不合格():
    errors = reflow_mod.validate(
        [_ws(kind=reflow_mod.KIND_RELABEL, reviewed_by="reesn")],
        gold_rows={"g1": _gold("g1")},
    )
    assert any("confirm_label_change" in e for e in errors)


def test_holdout与回归集不许回流():
    errors = reflow_mod.validate(
        [_ws(split="holdout")], gold_rows={"g1": _gold("g1", split="holdout")}
    )
    assert any("holdout" in e for e in errors)
    errors = reflow_mod.validate(
        [_ws(split="regression")], gold_rows={"g1": _gold("g1", split="regression")}
    )
    assert any("C-17" in e for e in errors)


def test_合格的工作表没有错误():
    errors = reflow_mod.validate([_ws()], gold_rows={"g1": _gold("g1")})
    assert errors == []


def test_软维度分被清空时不合格():
    """整行模式的「整行」：不带它就等于把它清掉（清掉不报错）。"""
    errors = reflow_mod.validate(
        [_ws(dimension_scores=None)],
        gold_rows={"g1": _gold("g1")},
        ann_rows={"g1": _ann("g1")},
    )
    assert any("软维度分" in e for e in errors)


# ── 写回（走 builder annotate）──────────────────────────────


def _golden_fixture(tmp_path: pathlib.Path) -> pathlib.Path:
    """造一个能跑 ``builder annotate`` 的黄金集根（candidates + annotations）。"""
    root = tmp_path / "golden"
    _dump(root / "candidates.jsonl", [{"golden_id": "g1"}, {"golden_id": "g2"}])
    _dump(root / "annotations.jsonl", [_ann("g1"), _ann("g2")])
    return root


def test_写回后标签与署名不变_面与时刻被带上(tmp_path):
    root = _golden_fixture(tmp_path)
    rows = [_ws()]
    result = reflow_mod.write_annotations(rows, golden_root=str(root))
    assert result["accepted"] == 1
    ann = {r["golden_id"]: r for r in _read(root / "annotations.jsonl")}
    assert ann["g1"]["label"] == "success"  # 标签没变
    assert ann["g1"]["reviewed_by"] == "manual-review"  # 署名没变
    assert ann["g1"]["access_surface"] == "human-browser"  # 面被带上
    assert ann["g1"]["observed_at"] == "2026-10-29T14:03:00+08:00"
    assert ann["g1"]["dimension_scores"] == {"d2": 5, "d4": 5, "d5": 1, "d6": 3}


def test_写回后软维度分不被清空(tmp_path):
    """⚠️ 整行模式的「整行」：不带分数就等于把它清掉。"""
    root = _golden_fixture(tmp_path)
    reflow_mod.write_annotations([_ws()], golden_root=str(root))
    ann = {r["golden_id"]: r for r in _read(root / "annotations.jsonl")}
    assert ann["g1"]["dimension_scores"] == {"d2": 5, "d4": 5, "d5": 1, "d6": 3}
