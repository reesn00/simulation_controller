"""label_studio label_config XML 静态校验 (P1, 2026-09-28; 2026-09-29 按实测重写).

不连 LS, 用 XML 解析 + 结构断言把 LS 加载期会炸的问题**提前**拦下。

⚠️ **最重要的一条教训**: ``POST /api/projects/{id}/validate/`` 会为一份
**浏览器根本解析不了**的 label_config 返回 200, ``/import`` 也照样 201 ——
task 推上去了, 标注页打开是一片红。所以"服务端校验通过"**不是**证据,
唯一可信的静态信号是下面的标签白名单。

下面几条全部是**对 LS 1.23.0 实测 / 在标注页上看到报错**得来的, 不是从文档抄的:

1. ``<Tabs>`` / ``<Tab>`` / ``<TextEditor>`` **在本机 LS 里没注册**。标注页报
   ``Tag with name tabs is not registered`` / ``texteditor is not registered``。
   **``<TextEditor>`` 根本不是 LS 的合法标签名** —— 只读展示用
   ``<TextArea editable="false">``。
2. ``<Filter>`` 必须带 ``name``, 否则标注页报
   ``Attribute name is required for FilterModel``。
3. ``<TextArea>`` **恒需** ``toName``, 与 ``editable`` / ``visible`` 无关。
   缺了报 ``Validation failed on : 'toName' is a required property``,
   且 LS 不告诉你是哪一个, 只能逐个试。
4. 逐条 (perItem) 列表的锚点得是 ``<Text name="x" value="$list"/>`,
   控件 ``toName="x" perItem="true"``。**不能**写成自引用的
   ``<TextArea name="x" toName="x" perItem="true" value="$list"/>`` ——
   实测 import 阶段必然 ``data['x']=...`` 400。
5. 文本标签的 value 必须绑**字符串**（或 list[str]）, 绑 dict / list[dict]
   import 必 400。
"""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

from label_studio.settings import LabelStudioSettings

ROOT = Path(__file__).resolve().parents[2]
XML_PATH = ROOT / "label_studio" / "label_configs" / "trajectory_review.xml"

#: LS OSS 1.23 确定注册的标签 —— 本配置只许用这些。
#:
#: 白名单而不是黑名单: 已知坏标签 (``Tabs`` / ``Tab`` / ``TextEditor``) 只是
#: **已经被发现**的那几个, 白名单能挡住所有还没撞上的。黑名单挡不住。
_ALLOWED_TAGS = {
    "View",        # 根
    "Header",      # 静态文本分隔（代替 <Tabs>/<Tab> 分区）
    "Text",        # 只读展示 + perItem 锚点
    "TextArea",    # 输入控件 / editable="false" 的只读块
    "Choices",     # 单选/多选
    "Choice",      # 选项
    "Filter",      # perItem 过滤，必须带 name
}

#: 已知在本机 LS 1.23 **未注册**、会让标注页整页报错的标签。
_UNREGISTERED_TAGS = {
    "Tabs": "分页容器未注册 → 'Tag with name tabs is not registered'，改用 <Header>",
    "Tab": "分页未注册 → 'Tag with name tab is not registered'，改用 <Header>",
    "TextEditor": "压根不是 LS 标签名 → 'texteditor is not registered'，改用 <TextArea editable=\"false\">",
    "Table": "结构化表格，scorecard 各维度不齐时表现不稳，用不上",
}


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
    """``<Text name="task" value="$task_id" visible="false"/>`` 锚点。

    **标签类型必须是 ``<Text>`` 且带 value**（2026-09-29 修）。这里原来是个
    ``<TextArea name="task" toName="task_anchor" maxSubmissions="1"/>`` ——
    当初加它只是为了"让 ``Choices toName="task"`` 有东西可指、项目建得起来",
    但它自身没有 value, 是个纯控件而不是数据源。LS 照样让项目建起来、
    ``validate/`` 照样返回 200, 但**标注页点一下那个复选框整个崩成空白页**,
    且 annotations/drafts/results 全是 0(什么都没存下)。

    这就是本项目踩过的 "validate/ 会说谎" 的又一层: 服务端给的是"配得进去",
    不是"标注页能用"。见 test_choice_toname_points_at_real_data_source。
    """
    anchors = [
        el for el in tree.iter()
        if el.get("name") == "task" and el.get("visible") == "false"
    ]
    assert len(anchors) == 1
    assert anchors[0].tag == "Text"
    assert anchors[0].get("value") == "$task_id"


def test_choice_toname_points_at_real_data_source(tree):
    """**每个** ``<Choices>`` 的 toName 必须指向一个带 value 的数据标签。

    这是本文件最该存在的一条规则。``<Choices>`` 靠 toName 指向的标签生成
    region; 指向一个"没有 value 的纯控件标签"时, 项目**建得起来**、
    ``validate/`` **报绿**、``import`` **201**, 但标注页一点就崩 —— 服务端
    没有任何一个信号能提前告诉你, 只有这条静态规则挡得住。

    同一个 label_config 里就能看到正反两面:
      * ``criterion_verdict toName="criterion"`` → ``<Text value="$criteria">`` ✓
      * ``overall_decision / failure_mode toName="task"`` → 曾经是无 value 的
        TextArea ✗
    """
    data_tags = {
        el.get("name") for el in tree.iter()
        if el.get("value") and el.get("name")
    }
    bad = [
        (el.get("name"), el.get("toName"))
        for el in tree.iter()
        if el.tag in ("Choices", "Labels", "Rating")
        and el.get("toName") not in data_tags
    ]
    assert not bad, (
        f"控件的 toName 没指向带 value 的数据标签, 标注页会崩: {bad}"
    )


def test_task_anchor_target_exists_and_is_string(tree):
    """``toName`` 指向的锚点得是 ``<Text>`` 且 value 绑纯字符串 ——
    绑 ``$messages`` (结构化) 时 import 直接 400 ``data['messages']=...``。"""
    anchor = next(el for el in tree.iter() if el.get("name") == "task_anchor")
    assert anchor.tag == "Text"
    assert anchor.get("value") == "$task_id"


def test_criterion_per_item_anchor_is_a_text_tag(tree):
    """逐条列表的锚点是 ``<Text>`` 而不是自引用的 ``<TextArea>``。"""
    anchors = [el for el in tree.iter() if el.get("name") == "criterion"]
    assert len(anchors) == 1
    assert anchors[0].tag == "Text", "perItem 锚点必须是 <Text>, 自引用 TextArea 实测必 400"
    assert anchors[0].get("toName") is None
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


def test_textarea_controls_all_have_toname(tree):
    """规则 1: ``<TextArea>`` 恒需 toName —— 这是原模板最贵的一个坑。

    LS 对缺 toName 的 TextArea 只说 "Validation failed on : 'toName' is a
    required property", 不说是哪个控件, 于是建项目这一步反复失败却查不出
    哪里错了。
    """
    offenders = [
        el.get("name")
        for el in tree.iter()
        if el.tag == "TextArea" and not el.get("toName")
    ]
    assert not offenders, f"TextArea 缺 toName (LS 1.23 建项目会失败): {offenders}"


def test_textarea_anchor_controls_carry_no_value(tree):
    """挂在锚点上的 ``<TextArea>`` **可以**有 value —— 只读展示块就靠它取数据。

    真正的约束不是"不能写 value", 而是"绑的值必须是字符串", 那条由
    :func:`test_display_bindings_are_plain_strings` 守。
    """
    display = {
        "scorecard_view",
        "risk_hints",
        "messages_view",
        "qf_text_view",
        "metadata_view",
    }
    for name in display:
        control = next(el for el in tree.iter() if el.get("name") == name)
        assert control.tag == "TextArea", name
        assert control.get("toName") == "task_anchor", name
        assert control.get("editable") == "false", name
        assert (control.get("value") or "").startswith("$"), name


def test_revise_notes_is_a_textarea_with_toname(tree):
    control = next(el for el in tree.iter() if el.get("name") == "revise_notes")
    assert control.tag == "TextArea"
    assert control.get("toName") == "task_anchor"
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
    """评分卡 / 风险提示 / 轨迹 / 元数据都是**只读**展示 —— 标注员不该改机器的判断。"""
    for name in ("scorecard_view", "risk_hints", "messages_view",
                 "qf_text_view", "metadata_view"):
        control = next(el for el in tree.iter() if el.get("name") == name)
        assert control.tag == "TextArea", name
        assert control.get("editable") == "false", name


def test_prediction_control_matches_label_config(tree):
    """预标注的 ``from_name`` / ``to_name`` 必须命中真实控件。

    LS 按 ``result[].from_name`` 匹配控件, **匹配不上就整条静默丢弃** ——
    ``import/predictions`` 照样回 ``201 {"created": 0}``, 不报错、不告警。
    所以推送侧 (``task_exporter.RISK_HINTS_CONTROL``) 和配置侧必须由这一条
    钉在一起, 两边各自测都测不出"名字对不上"。
    """
    from label_studio.task_exporter import (
        RISK_HINTS_CONTROL,
        TASK_ANCHOR_CONTROL,
    )

    names = {el.get("name") for el in tree.iter() if el.get("name")}
    assert RISK_HINTS_CONTROL in names, (
        f"预标注指向 {RISK_HINTS_CONTROL!r}, 但 label_config 里没有这个控件 —— "
        "LS 会静默丢弃整条预测"
    )
    assert TASK_ANCHOR_CONTROL in names, (
        f"预标注的 to_name={TASK_ANCHOR_CONTROL!r} 在 label_config 里不存在"
    )
    # 展示块还得真的绑到 risk_hints_text, 否则标注员看到的是空块
    risk = next(el for el in tree.iter() if el.get("name") == RISK_HINTS_CONTROL)
    assert risk.get("value") == "$risk_hints_text"


def test_audit_view_binds_audit_text(tree):
    """低分展示块绑 ``$audit_text``, 且该字段在推送侧**恒存在**。

    绑定名与 :func:`label_studio.task_exporter.build_task_data` 产出的键名
    必须逐字一致 —— 对不上时 LS 不报错, 只是把那块渲染成空白框, 标注员
    分不清是"样本没被拒收"还是"渲染坏了"。所以推送侧无条件写「（无）」,
    这里钉住的是"绑的键和写的键是同一个名字"。
    """
    view = next(
        (el for el in tree.iter() if el.get("name") == "audit_view"), None
    )
    assert view is not None, "label_config 缺 audit_view 展示块"
    assert view.get("value") == "$audit_text"
    assert view.get("toName") == "task_anchor"
    # 机器的判定不允许标注员改 —— 改它等于抹掉审计痕迹
    assert view.get("editable") == "false"


def test_audit_view_precedes_scorecard_view(tree):
    """低分标记排在评分卡**之前**。

    它解释了后面所有低分维度的成因; 排在后面等于让标注员先读一堆 0 分、
    带着"这数据有问题"的预设去读真正的拒收理由。
    """
    order = [el.get("name") for el in tree.iter() if el.get("name")]
    assert order.index("audit_view") < order.index("scorecard_view")


# ---------------------------------------------------------------------------
# 标签注册表（唯一能挡住"服务端说 OK、浏览器整页红"的静态信号）
# ---------------------------------------------------------------------------


def test_only_registered_tags_used(tree):
    """所有标签必须在白名单内。

    **这是本文件最重要的一条测试。** ``POST .../validate/`` 会为一份
    ``<Tabs>``/``<TextEditor>`` 全都未注册的 label_config 返回 200,
    ``/import`` 也照样 201 —— task 推上去了, 标注页打开是一屏
    "Tag with name X is not registered"。服务端给不了这个信号, 只有白名单能。
    """
    unknown = sorted({el.tag for el in tree.iter()} - _ALLOWED_TAGS)
    hints = {tag: _UNREGISTERED_TAGS.get(tag, "不在白名单里, 先在真实 LS 上确认能渲染再加")
             for tag in unknown}
    assert not unknown, f"label_config 用到未注册/未确认的标签: {hints}"


def test_known_bad_tags_absent(tree):
    """点名挡这三个: 曾经让标注页整页报错的标签。"""
    used = {el.tag for el in tree.iter()}
    for tag, why in _UNREGISTERED_TAGS.items():
        assert tag not in used, f"<{tag}> 在本机 LS 1.23 未注册 —— {why}"


def test_no_tabs_in_config(tree):
    """分区用 <Header> 而不是 <Tabs>/<Tab> —— 见 XML 里的说明。"""
    assert "Tabs" not in {el.tag for el in tree.iter()}
    assert "Tab" not in {el.tag for el in tree.iter()}


def test_filter_tags_have_name(tree):
    """``<Filter>`` 缺 name → 标注页报 "Attribute name is required for FilterModel"。"""
    filters = list(tree.iter("Filter"))
    assert filters, "perItem 页应当有 <Filter>"
    for control in filters:
        assert control.get("name"), f"<Filter toName={control.get('toName')!r}> 缺 name"


# ---------------------------------------------------------------------------
# 与 task.data 字段对得上
# ---------------------------------------------------------------------------


def test_referenced_data_keys_exist_in_exporter(tmp_path: Path):
    """XML 里引的 ``$xxx`` 必须由 task_exporter 真的产出, 否则 UI 显示空白。"""
    from c3_fixtures import QF_TEXT, RICH_META
    from conftest import write_c3
    from label_studio.task_exporter import build_task_data

    meta_path = write_c3(tmp_path / "refine_data", meta=RICH_META, qf_text=QF_TEXT)
    data, _ = build_task_data(meta_path)

    xml = XML_PATH.read_text(encoding="utf-8")
    referenced = set(re.findall(r"\$([A-Za-z_][A-Za-z0-9_.]*)", xml))
    # 去掉 scorecard.overall.* 这类嵌套路径的父节点已包含在顶层键里
    top_level = {ref.split(".")[0] for ref in referenced}
    assert top_level <= set(data), f"XML 引用了 task.data 没有的键: {top_level - set(data)}"


def test_display_bindings_are_plain_strings(tree, tmp_path: Path):
    """Text / TextArea 的 value 必须绑到**字符串或字符串列表**。

    LS 1.23 遇到 dict 直接 import 400 ``data['xxx']=...``; list 可以, 但
    元素必须逐个是字符串 —— list[dict] 同样 400。静态只看 XML 看不出来, 所以
    拿 exporter 的真实产物类型来断言。
    """
    from c3_fixtures import QF_TEXT, RICH_META
    from conftest import write_c3
    from label_studio.task_exporter import build_task_data

    meta_path = write_c3(tmp_path / "refine_data", meta=RICH_META, qf_text=QF_TEXT)
    data, _ = build_task_data(meta_path)

    offenders = []
    for el in tree.iter():
        if el.tag not in {"Text", "TextArea"}:
            continue
        value = el.get("value")
        if not value or not value.startswith("$"):
            continue
        bound = data.get(value[1:])
        if bound is None or isinstance(bound, str):
            continue
        if isinstance(bound, list) and all(isinstance(x, str) for x in bound):
            continue
        offenders.append((el.get("name"), value, type(bound).__name__))
    assert not offenders, f"这些文本标签绑到了非字符串值, import 必 400: {offenders}"


def test_all_four_sections_present(tree):
    """四个展示区都在 —— 原先是 <Tab value=...>, 现在是 <Header value=...>。

    一律平铺是因为 <Tabs>/<Tab> 在本机 LS 未注册, 平铺反而是能渲染的形态。
    """
    headers = " ".join(
        (el.get("value") or "") for el in tree.iter("Header")
    )
    for section in ("评分卡", "风险提示", "指令核对", "轨迹", "ChatML", "元数据"):
        assert section in headers, f"缺展示区: {section}"
