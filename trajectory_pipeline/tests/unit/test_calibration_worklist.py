"""C-06 首轮分诊 worklist 的守门测试。

设计来源：``docs/设计方案/16-评估器与rubric双向校准回路方案.md`` §9.3 / C-06。

守的是五件事：

1. **建议不是裁定**：worklist 行里**没有 `bucket` 这个键**（不是留空），
   建议只落 `suggested_*`；`suggested_by` 是机器身份，搬进分诊档会被拒；
2. **能预填的桶只有两个**（§11.2）：注册表里出现 `rubric_*` / `gold_*` 时
   **导入即炸**，不等分诊那天才发现；
3. **规则要能说清凭哪句话**：命中时返回的是证据原话，不是 bool——人在确认时
   核的正是那句话；
4. **fail-closed**：没有规则命中 / 两条规则指向不同桶，都**不给建议**，
   原因写进 `needs_human_reason`；
5. **工作表整批校验**：半填的表整批拒（逐条列出缺什么），全填的才进分诊档，
   且**上下文不被抄进裁定档**（裁定档只留 `KEPT_FIELDS`）。

真实读数（v7，33 张卡）在最后一节：有建议 12 / 待人判 21，一条冲突都没有——
冲突路径由本文件用假规则覆盖（真数据里没有两条规则同时命中的卡）。
"""

import io
import json

import pytest

from trajectory_pipeline.evaluation.calibration import buckets, triage, worklist
from trajectory_pipeline.evaluation.calibration.snapshot import HARD_DIM


def _card(**over) -> dict:
    card = {
        "disagreement_id": "v7:P000__r1__h:d10",
        "golden_id": "P000__r1__h",
        "golden_version": 7,
        "split": "calibration",
        "dimension": HARD_DIM,
        "rubric_ref": "D-10",
        "rubric_version": "1.2",
        "gold_value": "subscription_wall",
        "pred_value": "unresolved",
        "pred_provider": "code:frozen_row",
        "pred_evidence": "采集降级（['body_text']），证据不完整，不判",
        "pred_reasons": [],
        "gold_note": "",
        "delta_kind": "eq_class_mismatch",
    }
    card.update(over)
    return card


def _row(**over) -> dict:
    row = {
        "golden_id": "P000__r1__h",
        "code": {
            "branch": "unresolved",
            "source": "llm",
            "fallback_used": False,
            "reached_play_page": False,
        },
        "golden": {"label": "subscription_wall", "reviewed_by": "manual-review"},
    }
    row.update(over)
    return row


def _facts(card=None, row=None) -> dict:
    return worklist.facts_of(card or _card(), row or _row())


# ── 1. 注册表守卫：只能预填那两个桶 ────────────────────────


def test_注册表里的桶都可预填且不碰human_only():
    for rule in worklist.PREFILL_RULES:
        assert rule.bucket in worklist.PREFILLABLE_BUCKETS
        assert not buckets.is_human_only(rule.bucket)
    assert worklist.PREFILLABLE_BUCKETS == frozenset(
        {buckets.BUCKET_EVALUATOR_LOGIC, buckets.BUCKET_EVIDENCE_MISSING}
    )


@pytest.mark.parametrize(
    "bucket_name",
    ["rubric_ambiguous", "rubric_missing", "gold_defect"],
)
def test_规则指向human_only桶时导入即炸(bucket_name):
    """D-C3 的结构版：不是"记得别那么干"，是注册表根本装不进去。"""
    bad = worklist.PrefillRule(
        rule_id="R-BAD",
        bucket=bucket_name,
        why="机器想替人裁尺子",
        detect=lambda card, facts: "（伪造命中）",
    )
    with pytest.raises(RuntimeError, match="不是可预填的桶"):
        worklist.check_rules((bad,))


def test_规则桶名拼错也炸():
    bad = worklist.PrefillRule(
        rule_id="R-TYPO", bucket="evidence_missng", why="", detect=lambda c, f: None
    )
    with pytest.raises(KeyError):
        worklist.check_rules((bad,))


def test_规则id查得回来_不认识就keyerror():
    assert worklist.rule_of("R-COLLECTOR-REFUSED").bucket == "evidence_missing"
    with pytest.raises(KeyError):
        worklist.rule_of("R-NOPE")


# ── 2. 事实：机器能确认什么 ────────────────────────────────


def test_事实取自冻结行而不是卡():
    facts = _facts(
        _card(pred_evidence="站点遍历超时（>90.0s）"),
        _row(
            code={"source": "rule", "reached_play_page": False, "fallback_used": True},
            golden={"label": "login_wall_or_blocked", "reviewed_by": "manual-review"},
        ),
    )
    assert facts["pred_origin"] == "rule"
    assert facts["fallback_used"] is True
    assert facts["reached_play_page"] is False
    assert facts["gold_reviewed_by"] == "manual-review"
    assert any("站点遍历超时" in r for r in facts["collection_refusals"])


def test_事实字段缺了就空着不猜():
    facts = worklist.facts_of(
        _card(pred_evidence="存在 <video> 标签 ×6"), {"golden_id": "P000__r1__h"}
    )
    assert facts["pred_origin"] == ""
    assert facts["gold_reviewed_by"] == ""
    assert facts["collection_refusals"] == []


# ── 3. 建议：命中 / 不命中 / 冲突 ──────────────────────────


def test_采集层拒判命中_建议evidence_missing并给出证据原话():
    sug = worklist.suggest(_card(), _facts())
    assert sug["suggested_bucket"] == "evidence_missing"
    assert sug["suggested_by"] == worklist.SUGGESTED_BY
    assert sug["suggested_rules"] == ["R-COLLECTOR-REFUSED"]
    assert "采集降级" in sug["suggested_reason"]
    # 建议不等于裁定：仍要人签名
    assert sug["needs_human"] is True
    assert "建议不等于裁定" in sug["needs_human_reason"]


def test_预测侧不是unresolved就不建议_哪怕证据里提到降级():
    """「证据里提到超时」≠「这次判定的依据全是超时」。"""
    sug = worklist.suggest(
        _card(pred_value="login_wall_or_blocked"), _facts()
    )
    assert sug["suggested_bucket"] is None
    assert sug["suggested_rules"] == []


def test_软维度不建议_采集层规则只认硬维度():
    sug = worklist.suggest(
        _card(dimension="d3", pred_value="unresolved"), _facts()
    )
    assert sug["suggested_bucket"] is None


def test_没有规则命中时明说机器判不了():
    sug = worklist.suggest(
        _card(pred_value="success", pred_evidence="存在 <video> 标签 ×6"),
        _facts(),
    )
    assert sug["suggested_bucket"] is None
    assert sug["needs_human"] is True
    assert "机器判不了" in sug["needs_human_reason"]


def test_两条规则指向不同桶时不给建议(monkeypatch):
    """真数据里没有这种卡，但机制必须在：多桶命中一律不替人挑。"""
    fake = (
        worklist.PrefillRule(
            rule_id="R-A", bucket="evidence_missing", why="", detect=lambda c, f: "甲"
        ),
        worklist.PrefillRule(
            rule_id="R-B", bucket="evaluator_logic", why="", detect=lambda c, f: "乙"
        ),
    )
    monkeypatch.setattr(worklist, "PREFILL_RULES", fake)
    sug = worklist.suggest(_card(), _facts())
    assert sug["suggested_bucket"] is None
    assert sug["suggested_rules"] == ["R-A", "R-B"]
    assert "不同桶，机器不选" in sug["needs_human_reason"]
    assert "R-A→evidence_missing" in sug["suggested_reason"]


def test_两条规则同桶时合并成一条建议(monkeypatch):
    fake = (
        worklist.PrefillRule(
            rule_id="R-A", bucket="evidence_missing", why="", detect=lambda c, f: "甲"
        ),
        worklist.PrefillRule(
            rule_id="R-B", bucket="evidence_missing", why="", detect=lambda c, f: "乙"
        ),
    )
    monkeypatch.setattr(worklist, "PREFILL_RULES", fake)
    sug = worklist.suggest(_card(), _facts())
    assert sug["suggested_bucket"] == "evidence_missing"
    assert sug["suggested_rules"] == ["R-A", "R-B"]
    assert "甲" in sug["suggested_reason"] and "乙" in sug["suggested_reason"]


def test_建议签名是机器身份_搬进分诊档会被拒():
    """`suggested_by` 用与 triage 同一套身份词汇，不是自造一个字符串。"""
    assert triage.actor_kind(worklist.SUGGESTED_BY) == "agent"
    with pytest.raises(triage.TriageError, match="只能写建议值"):
        triage.validate_row(
            {
                "golden_id": "P000__r1__h",
                "dimension": HARD_DIM,
                "bucket": "evidence_missing",
                "triage_by": worklist.SUGGESTED_BY,
                "triage_evidence": "机器把自己的建议当成了裁定",
            }
        )


# ── 4. 组装：worklist 行里没有 bucket ─────────────────────


def test_worklist行里根本没有bucket这个键():
    """不是"留空"——压根不给这个字段，免得有人顺手填上。"""
    rows = worklist.build_worklist([_card()], [_row()])
    assert len(rows) == 1
    assert "bucket" not in rows[0]
    for field in ("suggested_bucket", "suggested_by", "suggested_reason",
                  "needs_human", "needs_human_reason", "facts"):
        assert field in rows[0]


def test_卡与冻结档对不上就报错_不猜():
    with pytest.raises(worklist.WorklistError, match="找不到"):
        worklist.build_worklist([_card()], [_row(golden_id="别的行")])


def test_重算逐字节相同():
    cards = [_card(), _card(golden_id="P001__r1__h")]
    rows = [_row(), _row(golden_id="P001__r1__h")]
    first = json.dumps(worklist.build_worklist(cards, rows), ensure_ascii=False)
    second = json.dumps(
        worklist.build_worklist(list(cards), list(rows)), ensure_ascii=False
    )
    assert first == second


def test_顺序跟随卡序():
    cards = [_card(golden_id="B"), _card(golden_id="A")]
    rows = [_row(golden_id="A"), _row(golden_id="B")]
    assert [r["golden_id"] for r in worklist.build_worklist(cards, rows)] == ["B", "A"]


# ── 5. 工作表与导入 ──────────────────────────────────────


def test_工作表三栏留空_身份两栏由卡带出():
    sheet = worklist.build_worksheet(worklist.build_worklist([_card()], [_row()]))
    assert sheet[0]["bucket"] == ""
    assert sheet[0]["triage_by"] == ""
    assert sheet[0]["triage_evidence"] == ""
    assert sheet[0]["golden_id"] == "P000__r1__h"
    assert sheet[0]["dimension"] == HARD_DIM
    # 只读上下文在，人不用另开 worklist
    assert sheet[0]["suggested_bucket"] == "evidence_missing"
    assert sheet[0]["facts"]["gold_reviewed_by"] == "manual-review"


def _write(path, rows) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows),
        encoding="utf-8",
    )


def _sheet(**over) -> dict:
    row = {
        "golden_id": "P000__r1__h",
        "dimension": HARD_DIM,
        "bucket": "evidence_missing",
        "triage_by": "owner",
        "triage_evidence": "采集器自己声明采集降级、按 fail-closed 不判，故不是判据问题",
        "suggested_bucket": "evidence_missing",
        "pred_evidence": "很长一段上下文",
        "facts": {"pred_origin": "llm"},
    }
    row.update(over)
    return row


def test_半填的工作表整批拒_并逐条列出缺什么(tmp_path):
    sheet = tmp_path / "worksheet.jsonl"
    _write(sheet, [_sheet(), _sheet(golden_id="P001__r1__h", bucket="", triage_by="")])
    with pytest.raises(triage.TriageError) as ei:
        triage.import_worksheet(sheet, target=tmp_path / "triage.jsonl")
    msg = str(ei.value)
    assert "还有 1 行没填完" in msg
    assert "P001__r1__h" in msg and "bucket" in msg
    assert not (tmp_path / "triage.jsonl").exists()


def test_空工作表不是全部填完(tmp_path):
    sheet = tmp_path / "worksheet.jsonl"
    _write(sheet, [])
    with pytest.raises(triage.TriageError, match="是空的"):
        triage.import_worksheet(sheet, target=tmp_path / "triage.jsonl")


def test_导入只保留裁定的字段_上下文不抄进分诊档(tmp_path):
    sheet = tmp_path / "worksheet.jsonl"
    _write(sheet, [_sheet()])
    target = tmp_path / "triage.jsonl"
    triage.import_worksheet(sheet, target=target, apply=True)
    saved = json.loads(target.read_text(encoding="utf-8").splitlines()[0])
    assert set(saved) <= set(triage.KEPT_FIELDS) | {"triage_at", "responsibility"}
    assert "pred_evidence" not in saved and "facts" not in saved
    assert saved["suggested_bucket"] == "evidence_missing"
    assert saved["responsibility"] == "collector"


def test_建议值脏了就拒(tmp_path):
    sheet = tmp_path / "worksheet.jsonl"
    _write(sheet, [_sheet(suggested_bucket="我瞎写的桶")])
    with pytest.raises(triage.TriageError, match="不是六桶之一"):
        triage.import_worksheet(sheet, target=tmp_path / "triage.jsonl")


def test_导入不覆盖已有桶_除非显式replace(tmp_path):
    sheet = tmp_path / "worksheet.jsonl"
    target = tmp_path / "triage.jsonl"
    _write(sheet, [_sheet()])
    triage.import_worksheet(sheet, target=target, apply=True)
    _write(sheet, [_sheet(bucket="evaluator_logic")])
    with pytest.raises(triage.TriageError, match="默认\\*\\*不覆盖\\*\\*"):
        triage.import_worksheet(sheet, target=target, apply=True)
    out = triage.import_worksheet(sheet, target=target, apply=True, replace=True)
    assert out["replaced"] == 1


# ── 5b. 工作表里的现场（scene 栏）─────────────────────────
#
# 为什么这一节存在：工作表原本给的是「结论 + 一句话证据」，**不是现场**。
# 而 D-10 的判据原文是「对照黄金集**预存档**的站点核验结论」——预存档不在表里。
# 人打开表看到「这个页面是不是播放站」这句话却**没有页面可看**，于是「该归
# 哪个桶」答不了。那不是「人不肯判」，是**表里没有可判的东西**。


def test_工作表每行都带scene栏_读不到也要明说():
    """没有 scene 键会被读成「这一行没有现场」；有键但 found=False 才是
    「读了，没读到」。"""
    sheet = worklist.build_worksheet(worklist.build_worklist([_card()], [_row()]))
    assert "scene" in sheet[0]
    assert sheet[0]["scene"]["status"] == "not_read"
    assert "没生成现场读数" in sheet[0]["scene"]["note"]


def test_工作表带现场时人不用另开文件():
    scenes = {"P000__r1__h": {"found": True, "status": "located", "steps": ["1. goto → video=0"]}}
    sheet = worklist.build_worksheet(
        worklist.build_worklist([_card()], [_row()]), scenes
    )
    assert sheet[0]["scene"]["found"] is True
    assert sheet[0]["scene"]["steps"] == ["1. goto → video=0"]
    # 现场是**只读上下文**：三栏仍然空着，桶仍然由人填
    assert sheet[0]["bucket"] == ""
    assert sheet[0]["triage_by"] == ""
    assert sheet[0]["triage_evidence"] == ""


def test_现场不抄进分诊档(tmp_path):
    """``scene`` 是判这一条的依据，不是裁定的一部分——重算会变，抄进裁定档
    就等于让今天的裁定引用着下一版的观察。"""
    sheet = tmp_path / "worksheet.jsonl"
    row = _sheet()
    row["scene"] = {"found": True, "steps": ["1. goto → video=0"], "body": "正文"}
    _write(sheet, [row])
    target = tmp_path / "triage.jsonl"
    triage.import_worksheet(sheet, target=target, apply=True)
    saved = json.loads(target.read_text(encoding="utf-8").splitlines()[0])
    assert "scene" not in saved
    assert set(saved) <= set(triage.KEPT_FIELDS) | {"triage_at", "responsibility"}


def test_带scene的工作表照样能导入(tmp_path):
    """多一栏只读上下文不该把导入挡了。"""
    sheet = tmp_path / "worksheet.jsonl"
    row = _sheet()
    row["scene"] = {"found": False, "status": "not_found"}
    _write(sheet, [row])
    target = tmp_path / "triage.jsonl"
    out = triage.import_worksheet(sheet, target=target, apply=True)
    assert out["added"] == 1


# ── 6. 汇总与 CLI 三态 ──────────────────────────────────


def test_覆盖率与接受改判分开记():
    rows = worklist.build_worklist(
        [_card(), _card(golden_id="P001__r1__h", pred_value="success",
                        pred_evidence="存在 <video> 标签")],
        [_row(), _row(golden_id="P001__r1__h")],
    )
    triaged = [
        {"golden_id": "P000__r1__h", "dimension": HARD_DIM, "bucket": "evidence_missing"},
        {"golden_id": "P001__r1__h", "dimension": HARD_DIM, "bucket": "evaluator_logic"},
    ]
    stats = worklist.summarize_worklist(rows, triaged)
    assert stats["cards"] == 2
    assert stats["triaged"] == 2
    assert stats["suggested"] == 1
    assert stats["accepted"] == 1  # 建议 = evidence_missing，人照写
    assert stats["overridden"] == 0
    assert stats["by_bucket"] == {"evaluator_logic": 1, "evidence_missing": 1}


def test_改判也算分诊完成_但单独计数():
    rows = worklist.build_worklist([_card()], [_row()])
    stats = worklist.summarize_worklist(
        rows, [{"golden_id": "P000__r1__h", "dimension": HARD_DIM,
                "bucket": "rubric_ambiguous"}]
    )
    assert stats["triaged"] == 1
    assert stats["overridden"] == 1
    assert stats["accepted"] == 0


def test_对不上卡的分诊行单独报_不参与覆盖率():
    rows = worklist.build_worklist([_card()], [_row()])
    stats = worklist.summarize_worklist(
        rows, [{"golden_id": "旧版本行", "dimension": HARD_DIM, "bucket": "gold_defect"}]
    )
    assert stats["triaged"] == 0
    assert stats["unmatched_triage"] == ["旧版本行:d10"]


def _main(argv) -> tuple[int, str]:
    out = io.StringIO()
    import contextlib

    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
        rc = worklist.main(argv)
    return rc, out.getvalue()


def _fixture(tmp_path, cards=None) -> dict:
    """一份最小冻结档 + 分歧卡，摆成 agreement 会摆的样子。"""
    golden = tmp_path / "golden"
    vdir = golden / "v7"
    vdir.mkdir(parents=True)
    row = _row()
    row["__split__"] = "calibration"
    _write(vdir / "calibration.jsonl", [row])
    out = tmp_path / "calib"
    out.mkdir()
    _write(out / "v7" / "disagreements.jsonl", cards if cards is not None else [_card()])
    return {"golden": str(golden), "out": str(out)}


def test_CLI_build产出两份文件_且工作表不覆盖(tmp_path):
    paths = _fixture(tmp_path)
    rc, out = _main(
        ["build", "--version", "7", "--out", paths["out"], "--golden-root", paths["golden"]]
    )
    assert rc == 0
    work = worklist.worklist_path(paths["out"], 7)
    sheet = worklist.worksheet_path(paths["out"], 7)
    assert work.exists() and sheet.exists()
    sheet.write_text('{"人工":"填到一半"}\n', encoding="utf-8")

    rc, out = _main(
        ["build", "--version", "7", "--out", paths["out"], "--golden-root", paths["golden"]]
    )
    assert rc == 0
    assert "不覆盖" in out
    assert "填到一半" in sheet.read_text(encoding="utf-8")


def test_CLI_build缺卡就退2(tmp_path):
    paths = _fixture(tmp_path)
    (worklist.cards_path(paths["out"], 7)).unlink()
    rc, _ = _main(
        ["build", "--version", "7", "--out", paths["out"], "--golden-root", paths["golden"]]
    )
    assert rc == 2


def test_CLI_build产出尺子_且工作表每行带scene(tmp_path):
    """人填的那张表里必须有现场；尺子（D-4 / D-5）另外落一份。"""
    paths = _fixture(tmp_path)
    rc, out = _main(
        ["build", "--version", "7", "--out", paths["out"], "--golden-root", paths["golden"]]
    )
    assert rc == 0
    clauses = worklist.clauses_path(paths["out"], 7)
    assert clauses.exists()
    text = clauses.read_text(encoding="utf-8")
    assert "## D-4" in text and "## D-5" in text and "## D-10" in text
    sheet = worklist.worksheet_path(paths["out"], 7)
    rows = [json.loads(x) for x in sheet.read_text(encoding="utf-8").splitlines() if x.strip()]
    assert rows and "scene" in rows[0]
    # 存档不在（夹具没造 P1）⇒ 明说读不到，**不是**给空读数
    assert rows[0]["scene"]["found"] is False
    assert rows[0]["scene"]["status"] == "not_found"
    assert rows[0]["scene"]["note"]


def test_CLI_status_未满退1_全覆盖退0(tmp_path):
    paths = _fixture(tmp_path)
    _main(["build", "--version", "7", "--out", paths["out"],
           "--golden-root", paths["golden"]])
    triage_file = tmp_path / "triage.jsonl"
    _write(triage_file, [])
    rc, out = _main(["status", "--version", "7", "--out", paths["out"],
                     "--triage", str(triage_file)])
    assert rc == 1 and "0/1 = 0%" in out

    _write(triage_file, [{"golden_id": "P000__r1__h", "dimension": HARD_DIM,
                          "bucket": "evidence_missing", "triage_by": "owner",
                          "triage_evidence": "采集层自述降级"}])
    rc, out = _main(["status", "--version", "7", "--out", paths["out"],
                     "--triage", str(triage_file)])
    assert rc == 0 and "1/1 = 100%" in out


def test_CLI_status显式给的分诊档不存在就退2(tmp_path):
    paths = _fixture(tmp_path)
    _main(["build", "--version", "7", "--out", paths["out"],
           "--golden-root", paths["golden"]])
    rc, _ = _main(["status", "--version", "7", "--out", paths["out"],
                   "--triage", str(tmp_path / "没有这份.jsonl")])
    assert rc == 2


def test_CLI_status缺省分诊档不存在按0覆盖算_但仍然退1(tmp_path, monkeypatch):
    paths = _fixture(tmp_path)
    _main(["build", "--version", "7", "--out", paths["out"],
           "--golden-root", paths["golden"]])
    monkeypatch.setattr(triage, "DEFAULT_TRIAGE_FILE", tmp_path / "还不存在.jsonl")
    rc, out = _main(["status", "--version", "7", "--out", paths["out"]])
    assert rc == 1
    assert "覆盖率按 0 算" in out


# ── 7. 真数据读数（v7 冻结档在就核对，不在就跳过）──────────


def test_真数据_v7的33张卡_建议12条且无冲突():
    """当初"机器能给几条建议"的实测读数：写死在这里，规则改了会红。

    v7 有 33 张卡（全部 `d10` 跨等价类）。采集层拒判 12 条（采集降级 6 /
    未选中站点 4 / 遍历超时 1 / 哨兵无法区分 1），其余 21 条没有规则命中
    ——`evaluator_logic` 的预填**故意留空**（见 `_detect_rule_decided`），
    所以这 21 条要靠人读条款。
    """
    from trajectory_pipeline.evaluation.golden.builder import GOLDEN_ROOT

    if not (GOLDEN_ROOT / "v7").exists():
        pytest.skip("v7 冻结档不在")
    rows = worklist.load_frozen_rows(GOLDEN_ROOT, 7)
    from trajectory_pipeline.evaluation.calibration.disagreement import (
        cards_from_snapshot,
    )
    from trajectory_pipeline.evaluation.calibration.snapshot import build_snapshot

    cards = cards_from_snapshot(
        build_snapshot(rows, golden_version=7, success_label="success")
    )
    work = worklist.build_worklist(cards, rows)
    stats = worklist.summarize_worklist(work)
    if stats["cards"] != 33:  # 冻结档换版后这条读数不再成立，交给新的实测
        pytest.skip(f"v7 卡数已变（{stats['cards']}），读数待重记")
    assert stats["suggested"] == 12
    assert stats["by_rule"] == {"R-COLLECTOR-REFUSED": 12}
    # 一条建议都不许落在 human-only 桶上
    for row in work:
        if row["suggested_bucket"]:
            assert not buckets.is_human_only(row["suggested_bucket"])