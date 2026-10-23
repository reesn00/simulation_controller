"""rubric 变更记录的守门测试（方案 16 C-12 + C-18）。

设计来源：``docs/设计方案/16-评估器与rubric双向校准回路方案.md`` §5.5 / §5.8 / §9.5。

这一份测两类东西：

1. **表格契约**——列、条款词表、变更类型封闭集、预期效果可证伪、责任区分声明
   不许只是借口。每条规矩都有一个「应当被驳回」的反例；
2. **条款级不变守卫**——两版快照里，本轮没列到的条款一处都不许动。

真实文档的校验在 ``tests/contract/test_rubric_traceability.py``；这里只喂假文档。
"""

from __future__ import annotations

import json

import pytest

from trajectory_pipeline.evaluation.scorers import rubric_changelog as cl


# ---------------------------------------------------------------------------
# 造文档 / 造行
# ---------------------------------------------------------------------------


def _doc(*rows: str, header: tuple[str, ...] | None = None) -> str:
    cols = header or cl.COLUMNS
    lines = [
        "# 假 rubric",
        "**变更记录**：",
        "| " + " | ".join(cols) + " |",
        "|" + "|".join(["---"] * len(cols)) + "|",
        *[f"| {r} |" for r in rows],
        "---",
    ]
    return "\n".join(lines)


FIRST = "v1.0 | ____ | * | 新增 | 初版发放 | — | — | — | rs"


def _row(
    version: str = "v1.3",
    date: str = "2026-10-24",
    clauses: str = "D-5",
    kinds: str = "修改表述",
    summary: str = "把付费页边界写清楚",
    effect: str = "D-5 分歧 12 → ≤4",
    cards: str = "v7:E006__2729220b__e9b9542b:d10",
    decl: str = "判不了的情形：站点明说需要登录但页面不含任何阻断词",
    author: str = "rs",
) -> str:
    return f"{version} | {date} | {clauses} | {kinds} | {summary} | {effect} | {cards} | {decl} | {author}"


def _entries(*rows: str) -> list[cl.Entry]:
    return cl.parse_entries(_doc(FIRST, *rows))


# ---------------------------------------------------------------------------
# 解析
# ---------------------------------------------------------------------------


def test_解析真实文档_三个版本四行():
    entries = cl.parse_entries()  # 读真文档
    assert cl.versions(entries) == ["1.0", "1.1", "1.2"]
    assert len(entries) == 4, "v1.2 有补记，同一版本两行"
    assert entries[-1].label == "补记，2026-10-17"
    assert entries[0].clauses == ("*",)


def test_多值单元格按中文标点拆分():
    entry = _entries(_row(clauses="D-3、D-5, B-1", kinds="新增；修改表述"))[-1]
    assert entry.clauses == ("D-3", "D-5", "B-1")
    assert entry.kinds == ("新增", "修改表述")


def test_空值记号折成空串():
    entry = _entries(_row(effect="—", cards="—", decl="—"))[-1]
    assert entry.expected_effect == "" and entry.disagreement_ids == ()
    assert entry.responsibility == ""


def test_单元格里出现竖线就抛():
    with pytest.raises(cl.ChangelogError, match="格"):
        cl.parse_entries(_doc(FIRST, _row(summary="改了 A | 又改了 B")))


def test_表头不对就抛():
    with pytest.raises(cl.ChangelogError, match="表头"):
        cl.parse_entries(_doc(FIRST, header=cl.COLUMNS[:-1]))


def test_没有标记就抛():
    with pytest.raises(cl.ChangelogError, match="标记"):
        cl.parse_entries("# 干净的文档，没有变更记录")


def test_版本格不认识就抛():
    with pytest.raises(cl.ChangelogError, match="版本格"):
        cl.parse_entries(_doc(FIRST, _row(version="2026-10-24")))


# ---------------------------------------------------------------------------
# 校验：结构
# ---------------------------------------------------------------------------


def test_首版必须是新增():
    entries = cl.parse_entries(_doc("v1.0 | ____ | * | 修改表述 | 初版 | — | — | — | rs"))
    with pytest.raises(cl.ChangelogError, match="必须是"):
        cl.validate_entries(entries)


def test_首版必须写通配():
    entries = cl.parse_entries(_doc("v1.0 | ____ | D-5 | 新增 | 初版 | — | — | — | rs"))
    with pytest.raises(cl.ChangelogError, match="整份新增"):
        cl.validate_entries(entries)


def test_通配只许首版用():
    entries = _entries(_row(clauses="*"))
    with pytest.raises(cl.ChangelogError, match="禁止整份重写"):
        cl.validate_entries(entries)


def test_版本倒退驳回():
    entries = _entries(_row(version="v1.4"), _row(version="v1.3"))
    with pytest.raises(cl.ChangelogError, match="倒退"):
        cl.validate_entries(entries)


def test_变更内容与修订人不许空():
    for kwargs in ({"summary": " "}, {"author": " "}):
        entries = _entries(_row(**kwargs))
        with pytest.raises(cl.ChangelogError):
            cl.validate_entries(entries)


# ---------------------------------------------------------------------------
# 校验：条款与变更类型
# ---------------------------------------------------------------------------


def test_未知条款号驳回():
    entries = _entries(_row(clauses="D-11"))
    with pytest.raises(cl.ChangelogError, match="不在条款词表"):
        cl.validate_entries(entries)


def test_拼错的档位驳回():
    entries = _entries(_row(clauses="D-3.9"))
    with pytest.raises(cl.ChangelogError, match="不在条款词表"):
        cl.validate_entries(entries)


@pytest.mark.parametrize("clause", ["G-7", "D-3", "D-3.5", "B-1"])
def test_合法条款号都收(clause):
    cl.validate_entries(_entries(_row(clauses=clause)))


def test_条款词表覆盖所有维度():
    """``DIM_CLAUSES`` 里每个维度条款都必须能在变更记录里写出来。"""
    assert cl.clause_vocabulary_covers_dims() == []


def test_变更类型不在封闭集驳回():
    entries = _entries(_row(kinds="微调一下"))
    with pytest.raises(cl.ChangelogError, match="封闭集"):
        cl.validate_entries(entries)


def test_没有权重表时调权重驳回():
    """`G-7` 明写不可相加 ⇒ 「调权重」是空类，用它就是编一个不存在的改动。"""
    assert cl.WEIGHT_TABLE_EXISTS is False
    entries = _entries(_row(kinds="调权重"))
    with pytest.raises(cl.ChangelogError, match="空类"):
        cl.validate_entries(entries)


# ---------------------------------------------------------------------------
# 校验：动机链（C-12）
# ---------------------------------------------------------------------------


def test_追溯线之前允许留空():
    """v1.2 及之前本来就没有卡、也没有可证伪预期——留 `—`，不伪造。"""
    entries = _entries(
        _row(version="v1.1", effect="—", cards="—", decl="判不了的情形：v1.0 用比率锚定")
    )
    cl.validate_entries(entries)


@pytest.mark.parametrize("field", ["effect", "cards"])
def test_追溯线之后必填(field):
    entries = _entries(_row(**{field: "—"}))
    with pytest.raises(cl.ChangelogError, match="必填|至少指一张"):
        cl.validate_entries(entries)


@pytest.mark.parametrize(
    "effect",
    [
        "希望更贴合准则",          # 无分母无方向
        "让 D-5 更好一些",          # 有条款号无数字
        "12 条",                    # 有数字无方向
    ],
)
def test_预期效果不可证伪驳回(effect):
    entries = _entries(_row(effect=effect))
    with pytest.raises(cl.ChangelogError, match="不可证伪"):
        cl.validate_entries(entries)


@pytest.mark.parametrize(
    "effect",
    ["D-5 分歧 12 → ≤4", "误判 ≤3 条", "B-1 各条款分数逐位不变"],
)
def test_可证伪的三种写法都收(effect):
    cl.validate_entries(_entries(_row(effect=effect)))


def test_卡id形状不对驳回():
    entries = _entries(_row(cards="E006:d10"))
    with pytest.raises(cl.ChangelogError, match="形状"):
        cl.validate_entries(entries)


def test_责任声明只写借口驳回():
    entries = _entries(_row(decl="评估器做不到"))
    with pytest.raises(cl.ChangelogError, match="借口"):
        cl.validate_entries(entries)


def test_责任声明说了情形就收():
    cl.validate_entries(_entries(_row(decl="判不了的情形：页面只有占位符，没有任何阻断特征")))
    assert cl.is_excuse_only("评估器做不到") is True
    assert cl.is_excuse_only("评估器做不到——页面没有可判证据，但与评估器能力无关") is False


def test_首版可以不写责任声明():
    """首版没有「被放宽」这回事。"""
    cl.validate_entries(cl.parse_entries(_doc(FIRST)))


def test_非首版不写责任声明驳回():
    entries = _entries(_row(decl="—"))
    with pytest.raises(cl.ChangelogError, match="责任区分声明必填"):
        cl.validate_entries(entries)


def test_当前版本必须在变更记录里():
    entries = _entries(_row(version="v1.4"))
    with pytest.raises(cl.ChangelogError, match="没有 v1.2"):
        cl.check_current(entries)


# ---------------------------------------------------------------------------
# 区间与条款匹配
# ---------------------------------------------------------------------------


def test_区间左开右闭():
    entries = cl.parse_entries()  # v1.0 / v1.1 / v1.2
    changed = cl.changed_clauses_between(entries, "1.0", "1.2")
    assert "D-5" in changed and "G-7" in changed
    # v1.0 自己的行不该被算进「1.0 → 1.2」这一段
    # v1.2 在文档里有两行（正文 + 补记），两行都落在这一段里
    assert cl.changed_clauses_between(entries, "1.1", "1.2") == frozenset({"D-5", "B-1"})


def test_版本没前进就抛():
    entries = cl.parse_entries()
    with pytest.raises(cl.ChangelogError, match="没有前进"):
        cl.changed_clauses_between(entries, "1.2", "1.2")


def test_区间内一条记录都没有就抛():
    entries = _entries(_row(version="v1.9"))
    with pytest.raises(cl.ChangelogError, match="一条改动都没有"):
        cl.changed_clauses_between(entries, "1.3", "1.5")


# ---------------------------------------------------------------------------
# 条款级不变守卫（C-18）
# ---------------------------------------------------------------------------


def _snap(*entries: tuple[str, str, str, object], rubric_version: str = "1.2") -> dict:
    """``(clause, golden_id, dimension, pred)`` → 快照。"""
    return {
        "schema_version": "1",
        "meta": {"golden_version": 7, "rubric_version": rubric_version},
        "entries": [
            {
                "golden_id": gid,
                "dimension": dim,
                "rubric_ref": clause,
                "gold_value": "success",
                "pred_value": pred,
                "verdict": "agree",
                "split": "calibration",
            }
            for clause, gid, dim, pred in entries
        ],
    }


def test_未列条款动了就驳回():
    before = _snap(("D-2", "A", "d2", 5), ("D-5", "A", "d5", 3))
    after = _snap(("D-2", "A", "d2", 4), ("D-5", "A", "d5", 5))
    report = cl.clause_invariance(before, after, changed={"D-5"})
    assert report["ok"] is False
    assert [v["clause"] for v in report["moved"]] == ["D-2"]
    assert report["moved"][0]["kind"] == "changed"


def test_列了的条款动了不算违规():
    before = _snap(("D-5", "A", "d5", 3))
    after = _snap(("D-5", "A", "d5", 5))
    report = cl.clause_invariance(before, after, changed={"D-5"})
    assert report["ok"] is True and report["moved"] == []


@pytest.mark.parametrize("changed", [{"D-3"}, {"D-3.5"}])
def test_基条款与档位条款互相覆盖(changed):
    """列 `D-3` 覆盖 `D-3.5`，列 `D-3.5` 也覆盖同卡的其他档位。"""
    before = _snap(("D-3.5", "A", "d3", 3), ("D-3.4", "B", "d3", 3))
    after = _snap(("D-3.5", "A", "d3", 5), ("D-3.4", "B", "d3", 5))
    report = cl.clause_invariance(before, after, changed=changed)
    assert report["moved"] == []


def test_通配时不判():
    before = _snap(("D-5", "A", "d5", 3))
    after = _snap(("D-5", "A", "d5", 1))
    report = cl.clause_invariance(before, after, changed={"*"})
    assert report["ok"] is True and report["wildcard"] is True


def test_只在一版出现的条目不算动但单列():
    before = _snap(("D-2", "A", "d2", 5), ("D-2", "B", "d2", 5))
    after = _snap(("D-2", "A", "d2", 5), ("D-2", "C", "d2", 5))
    report = cl.clause_invariance(before, after, changed={"D-5"})
    assert report["ok"] is True
    assert report["unpaired"]["only_before"] == ["B:d2"]
    assert report["unpaired"]["only_after"] == ["C:d2"]


def test_空值到值算appeared():
    before = _snap(("D-9", "A", "d9", None))
    after = _snap(("D-9", "A", "d9", 4))
    report = cl.clause_invariance(before, after, changed={"D-5"})
    assert report["moved"][0]["kind"] == "appeared"


def test_金标动了不算违规但单列():
    before = _snap(("D-2", "A", "d2", 5))
    after = _snap(("D-2", "A", "d2", 5))
    after["entries"][0]["gold_value"] = "failed"
    report = cl.clause_invariance(before, after, changed={"D-5"})
    assert report["ok"] is True, "重标走 gold_defect 窗口，与 rubric 改版是两条路"
    assert len(report["gold_moved"]) == 1


def test_一处都没动就标空跑():
    """继承式重冻结会把 pred 原样搬过来——空跑不等于通过。"""
    snap = _snap(("D-2", "A", "d2", 5), ("D-5", "A", "d5", 3))
    report = cl.clause_invariance(snap, snap, changed={"D-5"})
    assert report["ok"] is True and report["vacuous"] is True


def test_改了条款也一处处都没动仍算空跑():
    snap = _snap(("D-5", "A", "d5", 3))
    report = cl.clause_invariance(snap, snap, changed={"D-5"})
    assert report["vacuous"] is True


def test_判不了条款号的条目一样受检():
    """``rubric_ref`` 空着不是豁免——归到 ``""`` 组，动了照样驳回。"""
    before = _snap(("", "A", "dX", 5))
    after = _snap(("", "A", "dX", 1))
    report = cl.clause_invariance(before, after, changed={"D-5"})
    assert report["ok"] is False


# ---------------------------------------------------------------------------
# 引用的卡必须真的存在
# ---------------------------------------------------------------------------


def test_卡文件里查得到才算指回(tmp_path):
    (tmp_path / "v7").mkdir()
    (tmp_path / "v7" / "disagreements.jsonl").write_text(
        json.dumps({"disagreement_id": "v7:E006__2729220b__e9b9542b:d10"},
                   ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    entries = _entries(_row(cards="v7:E006__2729220b__e9b9542b:d10"))
    assert cl.verify_card_ids(entries, root=tmp_path) == []

    ghost = _entries(_row(cards="v7:NOPE__00000000__00000000:d10"))
    problems = cl.verify_card_ids(ghost, root=tmp_path)
    assert len(problems) == 1 and "没有这一条" in problems[0][1]


def test_卡文件缺了要说核不了而不是通过(tmp_path):
    entries = _entries(_row(cards="v9:E006__2729220b__e9b9542b:d10"))
    problems = cl.verify_card_ids(entries, root=tmp_path)
    assert len(problems) == 1 and "核不了" in problems[0][1]


def test_一条卡号都没引时不印对号(capsys):
    """真实文档今天 4 行全是 `—`（追溯线之前）——核了 0 条 ≠ 全对得上。"""
    assert cl.main(["check", "--verify-cards"]) == 0
    out = capsys.readouterr().out
    assert "没有任何卡号可核" in out
    assert "✓" not in out


# ---------------------------------------------------------------------------
# CLI：0 通过 / 1 驳回 / 2 测不成
# ---------------------------------------------------------------------------


def _write_snap(root, version: int, rubric_version: str, *entries) -> None:
    d = root / f"v{version}"
    d.mkdir(parents=True, exist_ok=True)
    (d / "snapshot.json").write_text(
        json.dumps(_snap(*entries, rubric_version=rubric_version), ensure_ascii=False),
        encoding="utf-8",
    )


def test_cli_check通过(capsys):
    assert cl.main(["check"]) == 0
    assert "RUBRIC_VERSION = 1.2" in capsys.readouterr().out


def test_cli_audit未列条款不动就通过(tmp_path, capsys):
    # D-9 不在「1.0 → 1.2」这一段列过的条款里（列过的是 D-1..D-8 / D-10 / G-7）
    _write_snap(tmp_path, 7, "1.0", ("D-9", "A", "d9", 5))
    _write_snap(tmp_path, 8, "1.2", ("D-9", "A", "d9", 5), ("D-5", "A", "d5", 5))
    assert cl.main(["audit", "--before", "7", "--after", "8",
                    "--calibration-root", str(tmp_path)]) == 0
    out = capsys.readouterr().out
    assert "逐位不变" in out


def test_cli_audit未列条款动了就驳回(tmp_path, capsys):
    _write_snap(tmp_path, 7, "1.0", ("D-9", "A", "d9", 5))
    _write_snap(tmp_path, 8, "1.2", ("D-9", "A", "d9", 4))
    assert cl.main(["audit", "--before", "7", "--after", "8",
                    "--calibration-root", str(tmp_path)]) == 1
    assert "分数动了" in capsys.readouterr().out


def test_受检条款一条都没配上就不算通过():
    """0 条样本 ≠ 一切正常：快照里只有本轮改过的条款时，这条判据没测到东西。"""
    before = _snap(("D-5", "A", "d5", 3))
    after = _snap(("D-5", "A", "d5", 5))
    report = cl.clause_invariance(before, after, changed={"D-5"})
    assert report["ok"] is True, "没有违规条目"
    assert report["measurable"] is False, "但它也没测到任何东西"


def test_cli_受检条款一条都没配上就退2(tmp_path, capsys):
    _write_snap(tmp_path, 7, "1.1", ("D-5", "A", "d5", 3))
    _write_snap(tmp_path, 8, "1.2", ("D-5", "A", "d5", 5))
    assert cl.main(["audit", "--before", "7", "--after", "8",
                    "--calibration-root", str(tmp_path)]) == 2
    assert "测不成" in capsys.readouterr().err


def test_cli_两版rubric相同退2(tmp_path, capsys):
    _write_snap(tmp_path, 7, "1.2", ("D-2", "A", "d2", 5))
    _write_snap(tmp_path, 8, "1.2", ("D-2", "A", "d2", 5))
    assert cl.main(["audit", "--before", "7", "--after", "8",
                    "--calibration-root", str(tmp_path)]) == 2
    assert "没有尺子改动" in capsys.readouterr().err


def test_cli_缺快照退2(tmp_path, capsys):
    assert cl.main(["audit", "--before", "7", "--after", "8",
                    "--calibration-root", str(tmp_path)]) == 2
    assert "快照不存在" in capsys.readouterr().err


def test_cli_快照没rubric版本退2(tmp_path, capsys):
    for version in (7, 8):
        d = tmp_path / f"v{version}"
        d.mkdir(parents=True)
        (d / "snapshot.json").write_text(
            json.dumps({"schema_version": "1", "meta": {}, "entries": []}),
            encoding="utf-8",
        )
    assert cl.main(["audit", "--before", "7", "--after", "8",
                    "--calibration-root", str(tmp_path)]) == 2
    assert "rubric_version" in capsys.readouterr().err