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

import pytest

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


def test_工作表改判型写人给的分支_署名与时刻一起留空():
    """H-3 #1/#3：改判行的 ``label`` 是**人给的分支**，不是旧金标。

    以前写旧金标 ⇒ apply 把旧 label 写回去 ⇒ ``--refresh-label`` 判
    ``changed_rows==0`` 退 2，改判永远落不了地。署名与时刻是**同一次人判
    的两半**：换了署名就不能再带旧时刻。
    """
    records = [_record("g1", verdict="disagree", human_branch="not_play_site")]
    gold_rows = {"g1": _gold("g1", label="unresolved")}
    ann_rows = {"g1": _ann("g1", label="unresolved")}
    rows, summary = reflow_mod.build_worksheet(
        records, gold_rows=gold_rows, ann_rows=ann_rows
    )
    assert summary["relabel"] == 1
    row = rows[0]
    assert row["kind"] == reflow_mod.KIND_RELABEL
    assert row["label"] == "not_play_site"        # 人的分支，不是旧金标
    assert row["label_label"] == "不是播放站点"    # 文本也跟着新标签
    assert row["reviewed_by"] == ""  # 「谁重新定了标签」是人的事实
    assert row["reviewed_at"] == ""  # 不能新署名配旧时刻
    assert row["source_label"] == "unresolved"    # 出表时金标是什么，留痕
    assert row["confirm_label_change"] is False


def test_工作表记来源版本与出表时的金标():
    """H-3 #6：apply 要能回答「这张表是哪一版、按哪条金标裁的」。"""
    rows, _ = reflow_mod.build_worksheet(
        [_record("g1", verdict="agree")],
        gold_rows={"g1": _gold("g1")},
        ann_rows={"g1": _ann("g1")},
        source_version=14,
    )
    assert rows[0]["source_version"] == 14
    assert rows[0]["source_label"] == "success"


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
                label="not_play_site",
                ls_human_branch="not_play_site",
                reviewed_by="agent-vlm",
                confirm_label_change=True,
            )
        ],
        gold_rows={"g1": _gold("g1")},
    )
    assert any("reviewed_by" in e for e in errors)


def test_改判型没显式确认时不合格():
    errors = reflow_mod.validate(
        [
            _ws(
                kind=reflow_mod.KIND_RELABEL,
                label="not_play_site",
                ls_human_branch="not_play_site",
                reviewed_by="reesn",
            )
        ],
        gold_rows={"g1": _gold("g1")},
    )
    assert any("confirm_label_change" in e for e in errors)


# ── H-3：工作表绑定来源与结论 ──────────────────────────────


def test_改判行的label还是旧金标时不合格():
    """H-3 #2：回流写的是 ``label``——它若还是旧金标，改判就落不了地。"""
    errors = reflow_mod.validate(
        [
            _ws(
                kind=reflow_mod.KIND_RELABEL,
                label="success",                 # 旧金标，没改成人的分支
                ls_human_branch="not_play_site",  # 人判的是这个
                reviewed_by="reesn",
                confirm_label_change=True,
            )
        ],
        gold_rows={"g1": _gold("g1")},
    )
    assert any("必须把它改成人的分支" in e for e in errors)


def test_改判行缺人给的分支时不合格():
    """没有 ``ls_human_branch`` ⇒ 无从核对 label 是不是人判的那个（fail-closed）。"""
    errors = reflow_mod.validate(
        [
            _ws(
                kind=reflow_mod.KIND_RELABEL,
                label="not_play_site",
                reviewed_by="reesn",
                confirm_label_change=True,
            )
        ],
        gold_rows={"g1": _gold("g1")},
    )
    assert any("ls_human_branch" in e for e in errors)


def test_改判行与金标等价时不合格():
    """等价分支是确认型，不是改判（改判会白耗一个版本号）。"""
    errors = reflow_mod.validate(
        [
            _ws(
                kind=reflow_mod.KIND_RELABEL,
                label="login_wall_or_blocked",
                ls_human_branch="login_wall_or_blocked",
                reviewed_by="reesn",
                confirm_label_change=True,
                source_label="blocked",
            )
        ],
        gold_rows={"g1": _gold("g1", label="blocked")},
    )
    assert any("等价" in e for e in errors)


def test_工作表过期时不合格():
    """H-3 #6：出表之后冻结档的金标变了 ⇒ 旧判断不能拿来改新金标。"""
    errors = reflow_mod.validate(
        [_ws(source_label="blocked")],  # 出表时金标是 blocked
        gold_rows={"g1": _gold("g1")},  # 现在是 success
    )
    assert any("工作表已过期" in e for e in errors)


def test_来源版本对不上时不合格():
    errors = reflow_mod.validate(
        [_ws(source_version=6)],
        gold_rows={"g1": _gold("g1")},
        expected_version=7,
    )
    assert any("与本次 --version 7 不一致" in e for e in errors)


def test_工作表缺来源版本时不合格():
    """旧格式/手写的表没有来源版本 ⇒ 拒绝（fail-closed）。"""
    errors = reflow_mod.validate(
        [_ws()], gold_rows={"g1": _gold("g1")}, expected_version=7
    )
    assert any("没有来源版本" in e for e in errors)


def test_确认型改标签时不合格():
    errors = reflow_mod.validate(
        [_ws(label="blocked")], gold_rows={"g1": _gold("g1")}
    )
    assert any("确认型不换标签" in e for e in errors)


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


def test_写回改判行真的改了label(tmp_path):
    """H-3 端到端：worksheet 的 label = 人的分支 ⇒ 写进 annotations 的也是它。

    修前：预填旧金标 ⇒ 写回后 ``--refresh-label`` 判 ``changed_rows==0``
    退 2，改判永远落不了地。
    """
    root = _golden_fixture(tmp_path)
    row = _ws(
        kind=reflow_mod.KIND_RELABEL,
        label="not_play_site",
        label_label="不是播放站点",
        ls_human_branch="not_play_site",
        reviewed_by="reesn",
        reviewed_at="",  # 改判不继承旧时刻，由 annotate 写回时打点
        confirm_label_change=True,
    )
    reflow_mod.write_annotations([row], golden_root=str(root))
    ann = {r["golden_id"]: r for r in _read(root / "annotations.jsonl")}
    assert ann["g1"]["label"] == "not_play_site"
    assert ann["g1"]["label_label"] == "不是播放站点"
    assert ann["g1"]["reviewed_by"] == "reesn"
    assert ann["g1"]["reviewed_at"]          # 写回时打了新时刻
    assert ann["g1"]["reviewed_at"] != "2026-10-12T04:01:21+00:00"
    assert ann["g1"]["dimension_scores"] == {"d2": 5, "d4": 5, "d5": 1, "d6": 3}


def test_逐行force_一行改判不替整批开路(tmp_path):
    """H-3 #4：批级 ``force`` 会让确认行里的标签漂移被静默覆盖。

    这一批里 g1 是合法的改判（要改 label），g2 是**确认行**但 label 被
    手工改歪了 ⇒ 只能放行 g1，g2 必须被 ``annotate`` 拒掉。
    """
    root = _golden_fixture(tmp_path)
    rows = [
        _ws(
            kind=reflow_mod.KIND_RELABEL,
            label="not_play_site",
            ls_human_branch="not_play_site",
            reviewed_by="reesn",
            reviewed_at="",
            confirm_label_change=True,
        ),
        _ws(golden_id="g2", label="blocked"),  # 确认行不该换标签
    ]
    with pytest.raises(reflow_mod.ReflowError, match="被拒绝"):
        reflow_mod.write_annotations(rows, golden_root=str(root))
    # 拒绝是**整批不写**（annotate 在写盘前就抛）
    ann = {r["golden_id"]: r for r in _read(root / "annotations.jsonl")}
    assert ann["g1"]["label"] == "success"
    assert ann["g2"]["label"] == "success"


# ── H-3：CLI 的保护（不静默覆盖人工工作表 / 绑定版本）─────────


def _patch_worksheet_sources(monkeypatch, tmp_path):
    """把 worksheet 命令的三处外部读取接到内存/临时目录。"""
    from trajectory_pipeline.evaluation.golden_ls import cli

    monkeypatch.setattr(
        cli, "_version_dir", lambda version, root=None: tmp_path / "calib" / f"v{version}"
    )
    monkeypatch.setattr(
        cli,
        "_load_trajectories",
        lambda version, golden_root=None: [_gold("T001__r1")],
    )
    monkeypatch.setattr(cli, "_load_annotations", lambda golden_root=None: {"T001__r1": _ann("T001__r1")})
    monkeypatch.setattr(
        cli, "_read_ls_verdicts", lambda path: [_record("T001__r1", verdict="agree")]
    )
    return cli


def test_工作表已存在时不覆盖_要force(tmp_path, monkeypatch):
    """H-3 #5：重跑 worksheet 会冲掉人填好的三栏 ⇒ 缺省拒绝。"""
    _patch_worksheet_sources(monkeypatch, tmp_path)
    argv = [
        "--golden-root", str(tmp_path / "golden"),
        "reflow", "worksheet", "--version", "7",
    ]
    rc, _, err = _run(argv)
    assert rc == 0, err
    ws = tmp_path / "calib" / "v7" / "reflow_worksheet.traj.jsonl"
    assert ws.exists()

    ws.write_text('{"golden_id": "T001__r1", "手工": "填好的三栏"}\n', encoding="utf-8")
    rc, _, err = _run(argv)
    assert rc == 2
    assert "已存在" in err
    assert "手工" in ws.read_text(encoding="utf-8")  # 人工内容一个字没动

    rc, _, err = _run(argv + ["--force"])
    assert rc == 0, err
    assert "手工" not in ws.read_text(encoding="utf-8")  # --force 才覆盖
