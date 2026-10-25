"""**归因面**的契约测试（方案 16 C-11 / C-13 / C-14）。

这一组盯的是三件「答不上来就看不出异常」的事：

1. **C-11 —— pred 是哪版评分器打的**。分歧卡要能回答「旧 prompt 还是新
   prompt 判的」。答不出来的形态是**答不出来却看起来一样**：两版分数摆在
   面前，谁也说不清是不是同一把尺子量的。
2. **C-13 —— 等价类合并记账**。owner 2026-10-23 裁定 D-C4：合并等价类
   **不需 owner 签**（签字那一半被裁掉了），但「无人记账」没被裁掉——
   所以每个多标签类都必须有一张注记，且注记要与表**逐标签对齐**。
3. **C-14 —— 阈值唯一真源**。owner 2026-10-23 裁定 D-C6：阈值调整
   **不算 rubric 级变更**（不升版本、不进变更记录、不设签名门）。
   「不算」不等于「可以无声改」——所以阈值有自己的面，且各使用处不许再写
   字面量。
"""

from __future__ import annotations

import ast
import pathlib
import re

import pytest

from trajectory_pipeline.evaluation.calibration import disagreement, snapshot
from trajectory_pipeline.evaluation.golden import alignment, builder
from trajectory_pipeline.evaluation.scorers import rubric_version, thresholds

#: 仓库根（测试文件在 ``trajectory_pipeline/tests/contract/`` 下）。
_REPO_ROOT = pathlib.Path(__file__).resolve().parents[3]


# ── C-14：阈值唯一真源 ───────────────────────────────────────────


def test_阈值表每条都有定线状态与依据():
    """每条阈值都要说清「定没定线」「哪来的」——不许有裸阈值。"""
    for name, spec in thresholds.THRESHOLDS.items():
        assert spec["status"] in thresholds.STATUS_SET, name
        assert str(spec["basis"]).strip(), name
        assert spec["where"], name


def test_阈值表每条都被声明的使用处真的引用():
    """「表里那条确实被用到」——表不腐靠这条。

    查的是**真的调了** ``thresholds.value("<name>")``，不是「文件里出现过
    这个名字」：后者会被注释与文档骗过去。
    """
    for name in thresholds.THRESHOLDS:
        pattern = re.compile(
            r"thresholds\.value\(\s*['\"]" + re.escape(name) + r"['\"]\s*\)"
        )
        for rel in thresholds.where(name):
            src = (_REPO_ROOT / "trajectory_pipeline" / rel).read_text(encoding="utf-8")
            assert pattern.search(src), f"{rel} 没有引用阈值 {name!r}"


def test_未登记的阈值名直接报错():
    """fail-closed：取未登记的名字 → ``KeyError``，不返回默认值。

    「差一点点就把线调低」正是从一个默认 0.0 开始的。
    """
    with pytest.raises(KeyError):
        thresholds.value("没登记过的线")
    with pytest.raises(KeyError):
        thresholds.status("没登记过的线")
    with pytest.raises(KeyError):
        thresholds.basis("没登记过的线")
    with pytest.raises(KeyError):
        thresholds.where("没登记过的线")


def test_比率型阈值不在使用模块里留字面量():
    """各使用处**不许**再写一份数字——改线只改表一处。

    只查判定线（比率型）；切分比例（数据形状）与体量上限（成本）不在表里。
    """
    ratio_literals = {
        "evaluation/golden/builder.py": ("0.90",),
        "evaluation/calibration/triggers.py": ("0.70", "0.50", "0.10", "0.30", "0.80"),
    }
    for rel, literals in ratio_literals.items():
        src = (_REPO_ROOT / "trajectory_pipeline" / rel).read_text(encoding="utf-8")
        for lit in literals:
            assert lit not in src, (
                f"{rel} 里还留着阈值字面量 {lit}——它该只存在于 "
                "evaluation/scorers/thresholds.py"
            )


def test_阈值表与rubric版本互不依赖():
    """机械证明「阈值不是 rubric 级变更」。

    owner 裁定 D-C6：调阈值不升 rubric 版本、不进 rubric 变更记录。
    这条不变式对应到代码上就是：**两个模块不互相 import**——阈值动一下，
    rubric 版本那侧的依赖图纹丝不动。
    """
    for a, b in (
        ("evaluation/scorers/thresholds.py", "evaluation/scorers/rubric_version.py"),
        ("evaluation/scorers/rubric_version.py", "evaluation/scorers/thresholds.py"),
    ):
        src = (_REPO_ROOT / "trajectory_pipeline" / a).read_text(encoding="utf-8")
        other = pathlib.Path(b).stem
        tree = ast.parse(src)
        imported: list[str] = []
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                imported.append(node.module or "")
                # ``from X import Y`` 的 **Y** 也在依赖图上——只看 X 会漏掉
                # 「import 同包里的另一个模块」这种最常见的写法。
                imported.extend(alias.name for alias in node.names)
            elif isinstance(node, ast.Import):
                imported.extend(alias.name for alias in node.names)
        assert not any(other in name for name in imported), (
            f"{a} 导入了 {b}——阈值与 rubric 版本不该互相依赖"
        )


def test_阈值表被改动不会牵动rubric版本():
    """改阈值 ⇒ :func:`evaluator_version` 不变（阈值不是尺子面的一部分）。"""
    before = rubric_version.evaluator_version()
    original = thresholds.THRESHOLDS["t2_hard_kappa"]["value"]
    try:
        thresholds.THRESHOLDS["t2_hard_kappa"]["value"] = 0.71
        assert rubric_version.evaluator_version() == before
    finally:
        thresholds.THRESHOLDS["t2_hard_kappa"]["value"] = original


# ── C-13：等价类合并记账 ─────────────────────────────────────────


def test_等价类合并记账与表逐标签对齐():
    """每个**多标签**类都要有注记，且注记的标签集合与表**恰好相等**。

    这条是「偷偷合并」的护栏：加了标签不补注记 ⇒ 这里红。
    """
    assert alignment.validate_merge_notes() == []
    for cls in alignment.ALL_EQUIV_CLASSES:
        labels = alignment.class_labels(cls)
        if len(labels) > 1:
            assert cls in alignment.EQUIV_CLASS_NOTES, f"{cls} 有 {len(labels)} 个标签却没注记"


def test_合并后不可见细分清单覆盖每个多标签类():
    """清单是 C-13 的产出面——「合并的代价」必须能列出来。"""
    lines = alignment.invisible_details()
    assert lines
    multi = {
        cls for cls in alignment.ALL_EQUIV_CLASSES
        if len(alignment.class_labels(cls)) > 1
    }
    listed = {line.split("：", 1)[0] for line in lines}
    assert listed == multi
    for line in lines:
        assert line.split("：", 1)[1].strip(), line


def test_偷偷合并会被记账检查抓住():
    """往一个类里塞标签而不补注记 ⇒ 检查报出问题（**不静默**）。"""
    original = dict(alignment.EQUIV_CLASS)
    try:
        alignment.EQUIV_CLASS["unresolved"] = "UNKNOWN"  # 换个标签进同一类
        alignment.EQUIV_CLASS["_偷偷加的_"] = "UNKNOWN"
        problems = alignment.validate_merge_notes()
        assert problems, "偷偷往等价类里加标签竟然没被抓住"
        assert any("UNKNOWN" in p for p in problems)
    finally:
        alignment.EQUIV_CLASS.clear()
        alignment.EQUIV_CLASS.update(original)
    assert alignment.validate_merge_notes() == []


def test_注记里的标签确实指向那个类():
    """注记声明的标签在表里必须真的指向这个类（抄错一处就红）。"""
    for cls, note in alignment.EQUIV_CLASS_NOTES.items():
        for label in (note["canonical"],) + tuple(note["merged"]):
            assert alignment.EQUIV_CLASS.get(label) == cls, (cls, label)


# ── C-11：pred 是哪版评分器打的 ───────────────────────────────────


def test_pred_provider带版本():
    """``<kind>@<version>``；版本没记到时写 ``unrecorded``，不省略 @ 段。"""
    assert snapshot.pred_provider_string("code:x", "abc") == "code:x@abc"
    assert snapshot.pred_provider_string("code:x", None) == "code:x@unrecorded"
    assert snapshot.pred_provider_string("code:x", "") == "code:x@unrecorded"
    assert snapshot.provider_version("code:x@abc") == "abc"
    assert snapshot.provider_version("code:x") == "unrecorded"


def test_行里没记评分器版本时不用当前指纹顶替():
    """拿当前评分器顶替 = 把「没量到」说成「量过且一致」——M-29 那类误读。"""
    assert snapshot.row_scorer_version({}) == "unrecorded"
    assert snapshot.row_scorer_version({"scorer_version": None}) == "unrecorded"
    assert snapshot.row_scorer_version({"scorer_version": "v1"}) == "v1"


def test_评分面指纹两段都在():
    """``code:…|llm:…``——代码面与 prompt 面都要在，缺一段就没法归因。"""
    fp = rubric_version.scorer_fingerprint()
    assert fp.startswith("code:")
    assert "|llm:" in fp
    assert rubric_version.llm_part(fp) == rubric_version.evaluator_version()
    # 不认识的指纹 → unrecorded，不返回空串
    assert rubric_version.llm_part("unrecorded") == "unrecorded"
    assert rubric_version.llm_part("") == "unrecorded"


def test_代码评分面指纹按源码内容算():
    """**内容**指纹，不是 git commit——commit 不含工作树未提交的改动。

    这条钉住实现的定义：改了源码内容 ⇒ 指纹变。
    """
    import hashlib

    parts = []
    here = pathlib.Path(rubric_version.__file__).resolve().parent
    for name in rubric_version.CODE_SCORER_MODULES:
        parts.append(f"{name}:{(here / name).read_text(encoding='utf-8')}")
    expect = hashlib.sha256("\n".join(parts).encode("utf-8")).hexdigest()[:12]
    assert rubric_version.code_scorer_version() == expect


def test_分歧卡带着尺子面指纹与pred版本():
    """卡要能回答「旧 prompt 还是新 prompt 判的」——三样都得在卡上。"""
    snap = {
        "meta": {
            "golden_version": 7,
            "rubric_version": "1.2",
            "evaluator_version": "llmNEW",
        },
        "entries": [
            {
                "golden_id": "E001__a__b",
                "split": "calibration",
                "dimension": "d3",
                "rubric_ref": "D-3.5",
                "gold_value": "playable",
                "pred_value": "not_play_site",
                "pred_provider": "code:row.dimension_scores@code:old|llm:llmOLD",
                "verdict": "disagree",
                "delta_kind": "eq_class_mismatch",
            }
        ],
    }
    cards = disagreement.cards_from_snapshot(snap)
    assert len(cards) == 1
    card = cards[0]
    assert card["evaluator_version"] == "llmNEW"
    assert card["pred_provider_version"] == "code:old|llm:llmOLD"
    assert card["pred_prompt_version"] == "llmOLD"
    # 旧 prompt ⇒ 不可比（False），不是「答不出来」
    assert disagreement.pred_comparability(card) is False


def test_版本没记到时可比性判不出来():
    """**不猜**：pred 侧没记版本 ⇒ ``None``（不可判定），不是 True 也不是 False。"""
    snap = {
        "meta": {"golden_version": 7, "rubric_version": "1.2", "evaluator_version": "llmNEW"},
        "entries": [
            {
                "golden_id": "E002__a__b",
                "split": "calibration",
                "dimension": "d3",
                "pred_provider": "code:row.dimension_scores@unrecorded",
                "verdict": "disagree",
            }
        ],
    }
    card = disagreement.cards_from_snapshot(snap)[0]
    assert card["pred_prompt_version"] == "unrecorded"
    assert disagreement.pred_comparability(card) is None


def test_同一版prompt判的是可比的():
    snap = {
        "meta": {"golden_version": 7, "rubric_version": "1.2", "evaluator_version": "llmNEW"},
        "entries": [
            {
                "golden_id": "E003__a__b",
                "split": "calibration",
                "dimension": "d3",
                "pred_provider": "code:row.dimension_scores@code:x|llm:llmNEW",
                "verdict": "disagree",
            }
        ],
    }
    card = disagreement.cards_from_snapshot(snap)[0]
    assert disagreement.pred_comparability(card) is True


def test_硬分类pred的出处不冒用评分器版本():
    """D-10 的 pred 是**采集时**写下的 code.branch，采集侧版本没进冻结行。

    拿行里的 ``scorer_version`` 顶替会是撒谎——那栏说的是「分数谁打的」。
    """
    snap = snapshot.build_snapshot(
        [
            {
                "golden_id": "E004__a__b",
                "golden": {"label": "success"},
                "code": {"branch": "not_play_site"},
                "scorer_version": "code:x|llm:y",
                "dimension_scores": {},
            }
        ],
        golden_version=7,
        success_label="success",
    )
    entry = next(e for e in snap["entries"] if e["dimension"] == "d10")
    assert entry["pred_provider"] == "code:frozen_row@unrecorded"


def test_软维度pred的出处带行里记的版本():
    snap = snapshot.build_snapshot(
        [
            {
                "golden_id": "E005__a__b",
                "golden": {"label": "success", "dimension_scores": {"d4": 5}},
                "code": {"branch": "success"},
                "scorer_version": "code:x|llm:y",
                "dimension_scores": {"d4": 3},
            }
        ],
        golden_version=7,
        success_label="success",
    )
    entry = next(e for e in snap["entries"] if e["dimension"] == "d4")
    assert entry["pred_provider"] == "code:row.dimension_scores@code:x|llm:y"


def test_overlay回填时把评分面指纹写进行里():
    """C-11 的产出端：谁打的分，行里就得记着谁打的。

    没有这一栏，「旧 prompt 还是新 prompt 判的」永远答不出来。
    """
    src = pathlib.Path(builder.__file__).read_text(encoding="utf-8")
    assert 'row["scorer_version"] = scorer_fingerprint()' in src
    # 没样本的行必须**显式写空**——不能让上一轮的版本留在行里。
    assert 'row["scorer_version"] = None' in src
