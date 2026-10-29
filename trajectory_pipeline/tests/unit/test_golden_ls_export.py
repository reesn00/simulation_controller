"""``golden_ls.exporter`` / ``label_config`` / ``report``：上传前的纯函数。

守的是**「读数不许被读错」**这一类错——它们全都不报错，只是让标注员
（或读报告的人）拿到一个**看起来完整、其实偏了**的结论：

1. **读不到就说读不到**：现场读不到时写「⚠ 没读到现场」+ 定位说明 +
   候选，**不给空串充数**——空读数会被读成「页面里没有播放控件」
   （``calibration/evidence.py`` 模块 docstring 的原话）；
2. **缺字段与没测是两回事**：维度分数三种状态分开写（M-29 的病）；
3. **分支文本取行内的**：④ 用 ``code.branch_label``（判读当时的文本），
   ⑦ 的分支表是**现行**的 22 类封闭集，只作参照；
4. **署名分类只有一个出处**：``golden.signatures``；``ai-assisted-*``
   算**自动化**（没人逐条看过），不算人核；
5. **label_config 的分支选项现读真源**：抄第二份必然漂，而漂了不报错。

⚠️ 全部**不联网**。
"""

from __future__ import annotations

import json
import re

import pytest

from trajectory_pipeline.evaluation.calibration import evidence
from trajectory_pipeline.evaluation.golden import signatures
from trajectory_pipeline.evaluation.golden_ls import exporter, label_config, report


# ── 夹具 ────────────────────────────────────────────────────────


def _row(
    gid: str = "T001__r1__abc",
    *,
    label: str = "not_play_site",
    branch: str = "not_play_site",
    branch_label: str = "不是播放站点",
    signature: str = "manual-review",
    split: str = "calibration",
    dims_present: bool = True,
    dims: object = None,
    code: bool = True,
) -> dict:
    row: dict = {
        "golden_id": gid,
        "task_id": "T001",
        "run_id": "r1",
        "url": "https://x.test/a",
        "site_url": "https://x.test/",
        "play_page_url": "",
        "source_kind": "archive_outcome",
        "status": "ok",
        "golden": {
            "label": label,
            "label_label": "不是播放站点",
            "reviewed_by": signature,
            "reviewed_at": "2026-10-14T01:11:41+00:00",
        },
        "persona": {"维度1": "x"},
        "__split__": split,
    }
    if code:
        row["code"] = {
            "branch": branch,
            "branch_label": branch_label,
            "question": "select.play_sites",
            "source": "llm",
            "reached_play_page": False,
            "fallback_used": False,
            "evidence": "证据原文",
        }
    if dims_present:
        row["dimension_scores"] = dims
        row["scores_detail"] = None
    return row


def _meta(version: int = 14) -> dict:
    return {
        "version": version,
        "frozen_at": "2026-10-28T08:53:32+00:00",
        "label_schema_version": "3",
        "rubric_version": "1.2",
        "evaluator_version": "1046e67994aa",
        "counts": {"total": 294},
    }


def _clauses() -> dict:
    return {"D-10": "**D-10 结论正确性 ★** 结论与 ground truth 一致",
            "D-4": "**D-4 播放组件** 什么算播放组件",
            "D-5": "**D-5 分支语义** 决策树"}


def _data(row: dict, **kwargs) -> dict:
    return exporter.build_golden_task_data(
        row,
        version_meta=_meta(),
        clauses=_clauses(),
        scene={"found": False, "status": "not_found", "note": "P1 存档不存在"},
        trajectory=None,
        **kwargs,
    )


# ── 九个区块 ────────────────────────────────────────────────────


class TestBlocks:
    def test_九个块一个不少(self) -> None:
        data = _data(_row())
        missing = [c for c in label_config.BLOCK_CONTROLS if c not in data]
        assert not missing, f"task.data 缺展示块: {missing}"

    def test_sample_id等于golden_id(self) -> None:
        row = _row(gid="T001__r1__xyz")
        data = _data(row)
        assert data["sample_id"] == row["golden_id"] == data["golden_id"]

    def test_分支文本取行内的(self) -> None:
        """④ 用判读当时的 ``branch_label``，不是现行分支表。"""
        row = _row(branch="unreachable_hard", branch_label="（2026-10-27 前的旧说法）")
        data = _data(row)
        assert "（2026-10-27 前的旧说法）" in data["code_block"]
        assert "旧说法" not in data["rubric_block"]

    def test_机器署名要显眼(self) -> None:
        data = _data(_row(signature="auto-validator-l3l4-v1-no-text-signal"))
        assert "不是人署的名" in data["version_block"]

    def test_人署名不报警(self) -> None:
        data = _data(_row(signature="manual-review"))
        assert "不是人署的名" not in data["version_block"]


# ── 读不到 ≠ 没有 ──────────────────────────────────────────────


class TestMissingIsNotEmpty:
    def test_现场读不到要写明为什么(self) -> None:
        data = _data(_row())
        block = data["scene_block"]
        assert "没读到现场" in block
        assert "不是" in block  # 「**不是**「页面里没有播放控件」」
        assert "P1 存档不存在" in block

    def test_现场读不到不给空串(self) -> None:
        """空串会被读成「页面里没有 video 标签」。"""
        data = _data(_row())
        assert data["scene_block"].strip()

    def test_T视图读不到要写明(self) -> None:
        data = _data(_row())
        block = data["trajectory_block"]
        assert "没读到 T 视图" in block
        assert "不是" in block

    def test_维度分数三种状态分开(self) -> None:
        """M-29 的病：字段不存在 / 本行为空 / 本行弃权，长得几乎一样。"""
        absent = _data(_row(dims_present=False))["scores_block"]
        assert "没有顶层维度分数字段" in absent

        empty = _data(_row(dims={}))["scores_block"]
        assert "空（本行没测）" in empty

        null = _data(_row(dims=None))["scores_block"]
        assert "null（本行弃权）" in null

        filled = _data(_row(dims={"d3": 3}))["scores_block"]
        assert '"d3": 3' in filled


# ── label_config ────────────────────────────────────────────────


class TestLabelConfig:
    def test_分支选项现读真源(self) -> None:
        from trajectory_pipeline.executor.branches import BRANCH_LABELS

        xml = label_config.render_label_config()
        for branch in BRANCH_LABELS:
            assert f'value="{branch}"' in xml, f"下拉里没有分支 {branch}"

    def test_注入分支时用注入的(self) -> None:
        xml = label_config.render_label_config(branches={"x": "测试分支"})
        assert 'value="x"' in xml
        assert 'value="success"' not in xml

    def test_不用alias(self) -> None:
        """alias= 会把 LS 认的选项标识静默换成 alias（v1 实测）。"""
        assert "alias=" not in label_config.render_label_config()

    def test_隐藏锚点是容器display_none(self) -> None:
        """隐藏只能靠容器 ``display:none``；写在 ``<Text>`` 上的 ``visible=``
        是**无声无效**的（v1 实测）。

        ⚠️ 判之前先把注释剥掉——注释里引用这个属性名正是为了警告别用它。
        """
        xml = label_config.render_label_config()
        stripped = re.sub(r"<!--.*?-->", "", xml, flags=re.S)
        assert 'style="display:none"' in stripped
        assert "visible=" not in stripped

    def test_指纹稳定(self) -> None:
        a = label_config.render_label_config(branches={"x": "y"})
        b = label_config.render_label_config(branches={"x": "y"})
        assert label_config.label_config_sha(a) == label_config.label_config_sha(b)

    def test_与LS端存的那份比内容不比sha(self) -> None:
        """⚠️ **LS 实测（1.23.0）**：LS 存 XML 时**去掉了末尾的换行**。

        ⇒ 直接比 sha 必然不等，会把「配置没变」报成「配置漂了」——
        而那种误报会让人白白重推一遍。所以比**内容**（strip 之后）。
        """
        xml = label_config.render_label_config()
        stored = xml.rstrip("\n")  # LS 端存的那份
        assert label_config.label_config_matches(stored, xml)
        # 真的改了内容 ⇒ 判不一致（别把这条写成永远 True）
        assert not label_config.label_config_matches(stored, xml.replace("<View>", "<View2>"))


# ── 署名分类（真源只有一处） ────────────────────────────────────


class TestSignatureClassification:
    def test_ai_assisted算自动化不算人核(self) -> None:
        """仓库既有口径：``ai-assisted-*`` 不进人类集合。"""
        assert signatures.classify_signature("ai-assisted-inc33") == (
            signatures.KIND_AUTOMATED
        )

    def test_agent署名算自动化(self) -> None:
        assert signatures.classify_signature("agent-x") == signatures.KIND_AUTOMATED

    def test_两个人类署名算人核(self) -> None:
        for name in ("manual-review", "reesn"):
            assert signatures.classify_signature(name) == signatures.KIND_HUMAN

    def test_空署名是unknown不是human(self) -> None:
        assert signatures.classify_signature("") == signatures.KIND_UNKNOWN
        assert signatures.classify_signature(None) == signatures.KIND_UNKNOWN

    def test_未知署名默认算自动化(self) -> None:
        """fail-closed：新冒出来的署名先算「没人看过」。"""
        assert signatures.classify_signature("某个新工具") == (
            signatures.KIND_AUTOMATED
        )


# ── 导出器（含凭据扫描） ───────────────────────────────────────


class TestExport:
    def test_任务数对得上(self) -> None:
        rows = [_row(f"g{i}") for i in range(3)]
        result = exporter.export_golden_rows(
            rows, version_meta=_meta(), clauses=_clauses()
        )
        assert result.count == 3
        assert len(result.rows) == 3

    def test_凭据命中拒推(self) -> None:
        row = _row()
        row["golden"]["note"] = "password = hunter2secret"
        result = exporter.export_golden_rows(
            [row], version_meta=_meta(), clauses=_clauses()
        )
        assert result.count == 0
        assert len(result.plan.rejected) == 1
        assert result.rows[0]["rejected"] is True

    def test_逐行读数带现场与轨迹(self) -> None:
        result = exporter.export_golden_rows(
            [_row()], version_meta=_meta(), clauses=_clauses()
        )
        stat = result.rows[0]
        assert stat["golden_id"] == "T001__r1__abc"
        assert stat["signature_kind"] == signatures.KIND_HUMAN
        assert stat["scene_found"] is False  # 没给 p2_root ⇒ 读不到
        assert stat["trajectory_found"] is False


# ── 报表口径 ────────────────────────────────────────────────────


class TestReport:
    def test_严格一致是全量逐字相等(self) -> None:
        rows = [_row("a", label="x", branch="x"), _row("b", label="x", branch="y")]
        strict = report.strict_agreement(rows)
        assert strict["total"] == 2
        assert strict["agree"] == 1
        assert strict["disagree"] == 1

    def test_口径C用等价类(self) -> None:
        """``blocked`` 与 ``login_wall_or_blocked`` 语义等价 ⇒ 算一致。"""
        rows = [
            _row("a", label="blocked", branch="login_wall_or_blocked"),
        ]
        d10 = report.d10_agreement(rows)
        assert d10["agree"] == 1

    def test_轨迹级样本不进口径C分母(self) -> None:
        row = _row("a")
        row["source_kind"] = "p2_sample"
        rows = [row]
        assert report.d10_agreement(rows)["total"] == 0
        assert report.strict_agreement(rows)["total"] == 1

    def test_未登记标签单列不吞掉(self) -> None:
        rows = [_row("a", label="某个没登记的标签", branch="success")]
        d10 = report.d10_agreement(rows)
        assert d10["unregistered"] == 1
        assert d10["agree"] == 0
        assert d10["total"] == 1  # 留在分母里

    def test_分档计数按固定顺序(self) -> None:
        rows = [_row("a", split="calibration"), _row("b", split="holdout")]
        counts = report.split_counts(rows)
        assert list(counts) == ["calibration", "regression", "holdout"]
        assert counts["calibration"] == 1
        assert counts["regression"] == 0

    def test_署名分布是原始事实(self) -> None:
        stats = [
            {"signature": "manual-review"},
            {"signature": "manual-review"},
            {"signature": "auto-validator-x"},
        ]
        assert report.signature_distribution(stats) == {
            "manual-review": 2,
            "auto-validator-x": 1,
        }


# ── 口径代 ──────────────────────────────────────────────────────


class TestEraNote:
    def test_代按金标署名时刻定位(self) -> None:
        note = exporter.era_note_for(_row())
        assert "口径代" in note
        assert "金标署名时刻" in note

    def test_没有署名时刻也不瞎猜(self) -> None:
        row = _row()
        row["golden"]["reviewed_at"] = ""
        note = exporter.era_note_for(row)
        assert "未知" in note


# ── 现场摘要的复用 ──────────────────────────────────────────────


def test_现场摘要用的是同一份配对逻辑() -> None:
    """``row_digest`` 与 ``card_digest`` 必须共用一份配对逻辑
    （否则两处的「第几步」会打架）。"""
    assert "row_digest" in evidence.__all__
    assert hasattr(evidence, "_archive_and_steps")
