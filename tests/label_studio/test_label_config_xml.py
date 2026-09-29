"""label_studio label_config XML 静态校验 (P1, 2026-09-28).

不连 LS, 用 XML 解析 + 结构断言把 LS 加载期会炸的问题**提前**拦下。
最关键的一条: 所有 ``toName`` 必须有对应 ``name`` 的控件, 否则 LS 报
"toName references missing tag", **整个项目建不起来**（原模板就踩了这个坑）。
"""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

from label_studio.settings import LabelStudioSettings

ROOT = Path(__file__).resolve().parents[2]
XML_PATH = ROOT / "label_studio" / "label_configs" / "trajectory_review.xml"


@pytest.fixture(scope="module")
def tree() -> ET.Element:
    assert XML_PATH.is_file(), f"label_config 缺失: {XML_PATH}"
    return ET.parse(XML_PATH).getroot()


# ---------------------------------------------------------------------------
# 基础合法性
# ---------------------------------------------------------------------------


def test_xml_is_wellformed(tree):
    assert tree.tag == "View"


def test_default_path_resolves(settings: LabelStudioSettings):
    assert settings.label_config_file() == XML_PATH


# ---------------------------------------------------------------------------
# toName 锚点（关键）
# ---------------------------------------------------------------------------


def test_every_toname_has_a_matching_control(tree):
    """LS 加载期硬校验: toName 引用的控件必须存在。"""
    names = {el.get("name") for el in tree.iter() if el.get("name")}
    dangling = [
        (el.tag, el.get("toName"))
        for el in tree.iter()
        if el.get("toName") and el.get("toName") not in names
    ]
    assert not dangling, f"toName 指向不存在的控件: {dangling}"


def test_task_anchor_exists(tree):
    """``<TextArea name="task" .../>`` 锚点 —— 原模板缺失导致项目建不起来。"""
    anchors = [
        el for el in tree.iter()
        if el.get("name") == "task" and el.get("visible") == "false"
    ]
    assert len(anchors) == 1
    assert anchors[0].tag == "TextArea"


def test_criterion_per_item_anchor_exists(tree):
    """逐条核对页的 perItem 锚点 —— name/toName 同为 criterion。"""
    anchors = [
        el for el in tree.iter()
        if el.get("name") == "criterion" and el.get("toName") == "criterion"
        and el.get("perItem") == "true"
    ]
    assert len(anchors) == 1
    assert anchors[0].get("value") == "$criteria"


def test_overall_decision_is_required_single_choice(tree):
    control = next(el for el in tree.iter() if el.get("name") == "overall_decision")
    assert control.tag == "Choices"
    assert control.get("required") == "true"
    assert control.get("choice") == "single"
    values = {c.get("value") for c in control}
    assert values == {"accept", "revise", "reject"}


# ---------------------------------------------------------------------------
# 控件合法性
# ---------------------------------------------------------------------------


def test_textarea_controls_have_no_toname(tree):
    """TextArea 是直接输入控件, 用 toName 是错误用法 (原模板 revise_notes)。"""
    offenders = [
        el.get("name")
        for el in tree.iter()
        if el.tag == "TextArea" and el.get("toName")
        and el.get("perItem") != "true"      # perItem 的 TextArea 是展示件
    ]
    assert not offenders, f"TextArea 不该带 toName: {offenders}"


def test_revise_notes_is_plain_textarea(tree):
    control = next(el for el in tree.iter() if el.get("name") == "revise_notes")
    assert control.tag == "TextArea"
    assert control.get("toName") is None
    assert control.get("maxSubmissions") == "1"


def test_criterion_controls_are_per_item(tree):
    for name in ("criterion_verdict", "criterion_note"):
        control = next(el for el in tree.iter() if el.get("name") == name)
        assert control.get("perItem") == "true", name
        assert control.get("toName") == "criterion", name


def test_criterion_verdict_is_required(tree):
    control = next(el for el in tree.iter() if el.get("name") == "criterion_verdict")
    assert control.get("required") == "true"
    assert {c.get("value") for c in control} == {"agree", "disagree"}


def test_display_controls_are_not_editable(tree):
    """评分卡 / 轨迹 / 元数据都是**只读**展示 —— 标注员不该改自动分。"""
    for name in ("scorecard_view", "messages_view", "qf_text_view",
                 "metadata_view", "overall_derivation"):
        control = next(el for el in tree.iter() if el.get("name") == name)
        assert control.get("editable") == "false", name


# ---------------------------------------------------------------------------
# 与 task.data 字段对得上
# ---------------------------------------------------------------------------


def test_referenced_data_keys_exist_in_exporter(tmp_path: Path):
    """XML 里引的 ``$xxx`` 必须由 task_exporter 真的产出, 否则 UI 显示空白。"""
    from label_studio.task_exporter import build_task_data
    from c3_fixtures import QF_TEXT, RICH_META
    from conftest import write_c3

    meta_path = write_c3(tmp_path / "refine_data", meta=RICH_META, qf_text=QF_TEXT)
    data, _ = build_task_data(meta_path)

    xml = XML_PATH.read_text(encoding="utf-8")
    referenced = set(re.findall(r"\$([A-Za-z_][A-Za-z0-9_.]*)", xml))
    # 去掉 scorecard.overall.* 这类嵌套路径的父节点已包含在顶层键里
    top_level = {ref.split(".")[0] for ref in referenced}
    assert top_level <= set(data), f"XML 引用了 task.data 没有的键: {top_level - set(data)}"


def test_scorecard_tabs_present(tree):
    tabs = {t.get("value") for t in tree.iter("Tab")}
    assert {"评分卡", "指令核对", "轨迹", "ChatML"} <= tabs


def test_metadata_tab_added(tree):
    """``$metadata`` 原生 dict 直接展示 —— 省掉 R5 的字符串转义层。"""
    assert "元数据" in {t.get("value") for t in tree.iter("Tab")}
