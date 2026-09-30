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
2. ``<List>`` 官方文档说它能把对象数组拆成逐项 region（指令核对正需要这个）,
   但**本机 1.23 上什么都不渲染** —— 不报错也不出列表, 比报错更隐蔽。
3. ``<TextArea>`` **恒需** ``toName``, 与 ``editable`` / ``visible`` 无关。
   缺了报 ``Validation failed on : 'toName' is a required property``,
   且 LS 不告诉你是哪一个, 只能逐个试。
4. **逐项 (perItem / perRegion) 控件在本机拿不到逐条结果**, 指令核对已改
   ``<TextArea>`` 清单 + 整块三选一 + 自由文本点名。三轮探针定性: 属性名不是
   原因 (``perRegion`` / ``perItem`` 都是 1 组), ``<Filter>`` 洗清; 根因是
   ``perRegion`` 的语义是"当前选中的那个 region", 而 ``<Text value="$列表">``
   只产生**一个** region（6 条被 "," 连成一整段）, ``<Chat>`` 的 import 消息
   不可选, ``<List>`` 不渲染。
5. ``<Choices>`` 的 ``toName`` 必须指向**带 value 的数据标签**。指向"没有
   value 的纯控件标签"时 validate 报绿、import 201, 但标注页一点就崩。
6. 文本标签的 value 必须绑**字符串**, 绑 dict / list import 必 400。
7. ``<Choice>`` 的**显示标签走 html=**, 不写就显示机器键。**``alias=`` 是
   陷阱** —— 它会把 LS 认的选项标识从 value 换成 alias, 前端提交上去的就是
   alias, 等于静默改掉契约键。
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
    "Text",        # 隐藏锚点（task_anchor / task）
    "TextArea",    # 输入控件 / editable="false" 的展示块
    "Choices",     # 单选/多选
    "Choice",      # 选项
}

#: 已知在本机 LS 1.23 **不能依赖**的标签。
#: 前三个会让标注页整页报错, ``List`` / ``Table`` 是更隐蔽的一种: 不报错,
#: 也不渲染 —— 只有真去看标注页才发现, 所以更要静态挡住。
_UNREGISTERED_TAGS = {
    "Tabs": "分页容器未注册 → 'Tag with name tabs is not registered'，改用 <Header>",
    "Tab": "分页未注册 → 'Tag with name tab is not registered'，改用 <Header>",
    "TextEditor": "压根不是 LS 标签名 → 'texteditor is not registered'，改用 <TextArea editable=\"false\">",
    "List": "官方文档说它能把对象数组拆成逐项 region，但本机 1.23 什么都不渲染（探针 34 实测）——指令核对已改整块判定",
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
      * ``overall_decision / failure_mode / criterion_verdict toName="task"``
        → ``<Text name="task" value="$task_id">`` ✓
      * 曾经这里是 ``<TextArea name="task" toName="task_anchor" .../>`` ✗
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


def test_criteria_are_shown_as_one_newline_separated_block(tree):
    """指令核对清单是**换行分隔的单串**, 不是列表。

    曾经的形态是 ``<Text name="criterion" value="$criteria"/>`` 配 perItem 控件,
    实测在 LS 1.23 上只出 1 组单选、且 6 条 criterion 被 "," 连成一整段文本
    (三轮探针定性: 属性名不是原因, ``<Filter>`` 洗清, ``perRegion`` 的语义是
    "当前选中的 region", 前提是锚点产生 N 个 region, 而 ``<Text>`` 只产生一个;
    ``<Chat>`` 不可选; ``<List>`` 在本机不渲染)。详见 XML 里的说明段。
    """
    control = next(el for el in tree.iter() if el.get("name") == "criteria_view")
    assert control.tag == "TextArea"
    assert control.get("toName") == "task_anchor"
    assert control.get("editable") == "true"
    assert control.get("value") == "$criteria_text"
    # rows > 1 → Add 按钮可见 → 这块可提交(展示块可改, 见 observability §3.5)
    assert int(control.get("rows") or 1) > 1

    # 逐项控件已下线, 别有人再把 perItem 加回来
    assert not list(tree.iter("Filter")), "perItem 已放弃, <Filter> 不该再出现"
    for name in ("criterion_verdict", "criterion_note"):
        el = next(x for x in tree.iter() if x.get("name") == name)
        assert el.get("perItem") is None, name
        assert el.get("perRegion") is None, name


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
    """挂在锚点上的 ``<TextArea>`` **可以**有 value —— 展示块就靠它取数据。

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
        assert control.get("editable") == "true", name
        assert (control.get("value") or "").startswith("$"), name


def test_revise_notes_is_a_textarea_with_toname(tree):
    control = next(el for el in tree.iter() if el.get("name") == "revise_notes")
    assert control.tag == "TextArea"
    assert control.get("toName") == "task_anchor"
    assert control.get("maxSubmissions") == "1"


def test_criterion_controls_anchor_on_task(tree):
    """判定控件挂 ``<Text name="task">`` —— 带 value 的数据标签。

    ``criterion_note`` 挂 ``task_anchor``: 它是 TextArea(控件), 不是 Choices,
    不需要 region source。
    """
    verdict = next(el for el in tree.iter() if el.get("name") == "criterion_verdict")
    assert verdict.get("toName") == "task"
    note = next(el for el in tree.iter() if el.get("name") == "criterion_note")
    assert note.tag == "TextArea"
    assert note.get("toName") == "task_anchor"
    # 防止点第二次 Add 多存一条重复 submission (风险提示区实测踩过)
    assert note.get("maxSubmissions") == "1"


def test_criterion_verdict_is_required(tree):
    """三选一的**整块**判定, 不是一个无归属的 agree/disagree 标量。

    ``some_disagree`` 这一档是整套改动的关键: 逐项控件在 LS 1.23 上拿不到
    逐条结果, 归属只能由标注员在 ``criterion_note`` 里点名, 所以判定本身
    必须能把"有哪几条不认同"这个状态显式记下来。
    """
    control = next(el for el in tree.iter() if el.get("name") == "criterion_verdict")
    assert control.get("required") == "true"
    assert control.get("choice") == "single"
    assert {c.get("value") for c in control} == {
        "all_agree",
        "some_disagree",
        "none_agree",
    }


def test_criterion_note_placeholder_names_the_ids_to_call_out(tree):
    """点名框的 placeholder 必须给出写法示例 —— 判据校准全靠它。"""
    note = next(el for el in tree.iter() if el.get("name") == "criterion_note")
    placeholder = note.get("placeholder") or ""
    assert "criterion_id" in placeholder
    assert "：" in placeholder, "示例应当是 criterion_id: 理由 的形式"


def test_display_controls_are_textareas_on_the_anchor(tree):
    """展示块是**可改的审查工作区**, 不是只读回显 —— 这里钉的是「仍是挂在隐藏
    锚点上的 TextArea、且 ``editable="true"``」, 不是「不可编辑」。

    提交路径与 ``editable`` **无关**: 能不能提交取决于 **Add 按钮**, 而 Add 按钮
    默认在 ``rows="1"`` 时隐藏、``rows > 1`` 时可见 —— 下面这些块 rows 全部 > 1。
    实测 (2026-09-29, probe 项目 task 6): 标注员删掉 ``messages_view`` 里 168 个
    字符后提交, 那段删除原样进了 annotation, 与 ``task.data`` 不再一致。**那时
    这些块写的还是 ``editable="false"``** —— 它并不阻止提交, 只管"加完之后能不能
    再改"。

    2026-09-30 起改写 ``editable="true"``(探针 project 37 实测): 提交之后**可以
    就地改**, 不用为了修一个错字再点一次 Add 多存一条重复 submission。这同时让
    ``rows > 1`` 变成"刻意保留"的约束 —— 下面显式钉住。

    展示块可改是**设计决定** (审查本就要纠错, 只读反而是错的), 代价是
    annotation 里存的是**修正稿而非训练稿** —— 那条由文档承担:
    ``docs/observability-label-studio.md`` §3.5。想真正做成结构上只读得换
    ``<Text>`` 标签 (非控件, 挂不上 submission) + ``<Style>`` 保缩进, 配方已实测
    见 label_config 注释; 目前不做, 因为可改本身就是决定。
    """
    for name in ("scorecard_view", "risk_hints", "messages_view",
                 "qf_text_view", "metadata_view"):
        control = next(el for el in tree.iter() if el.get("name") == name)
        assert control.tag == "TextArea", name
        assert control.get("toName") == "task_anchor", name
        # rows>1 ⇒ Add 按钮默认可见 ⇒ 这正是"可提交"的那条路径。改小 rows 会
        # 改变行为 (按钮消失), 所以显式钉住, 不让它被无声改掉。
        assert int(control.get("rows") or 1) > 1, (
            f"{name} 的 rows 改成 1 会让 Add 按钮消失 (行为变更), 要改先更新"
            " label_config 里「展示块可改」那段说明"
        )
        assert control.get("editable") == "true", name


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
    # 提交入口由 Add 按钮(rows>1)决定, 不是 editable —— 这块照样可提交。
    # editable="true" 只保证"提交后能就地改", 别把它读成"鼓励改"。
    # 机器判定的原值以 output/refine_data/ 下的 C3 meta 为准, 见 observability §3.5
    assert view.get("editable") == "true"


def test_audit_view_precedes_scorecard_view(tree):
    """低分标记排在评分卡**之前**。

    它解释了后面所有低分维度的成因; 排在后面等于让标注员先读一堆 0 分、
    带着"这数据有问题"的预设去读真正的拒收理由。
    """
    order = [el.get("name") for el in tree.iter() if el.get("name")]
    assert order.index("audit_view") < order.index("scorecard_view")


def test_content_revision_is_required_single_choice(tree):
    """``content_revision`` —— 展示块可改, 就必须显式记下"改没改过"。

    缺的正是这个信息: 展示块可改之后, annotation 里的 ``messages_view`` 是**标注员
    的修正稿**, 而它和机器原值在结构上**完全一样** —— 都是 ``value.text`` 里的
    一个字符串, 没有任何字段能区分"原样"和"改过"。下游拿到一条 ``accept`` 的样本
    无从判断该用哪一份。

    （``JSON_MIN`` 导出下 ``*_text`` 与 ``*_view`` 是两列并存, 可以比对; 但比对
    13 万字符不现实, 而且"提交了但没改"与"没提交"长得一样。这个开关是显式信号。）

    required: 下游按"选训练集"口径消费时, 缺这个值的样本无法归类, 不如提交时就不
    让漏。
    """
    control = next(
        (el for el in tree.iter() if el.get("name") == "content_revision"), None
    )
    assert control is not None, "label_config 缺 content_revision 控件"
    assert control.tag == "Choices"
    assert control.get("required") == "true"
    assert control.get("choice") == "single"
    assert {c.get("value") for c in control} == {"unchanged", "corrected", "unusable"}


def test_every_choice_has_a_human_readable_html_label(tree):
    """每个 ``<Choice>`` 都要有 ``html=`` —— 否则标注页显示的是机器键。

    不写 ``html`` 时 LS 直接拿 ``value`` 当标签, 页面上就是 ``accept[2]`` /
    ``all_agree[1]`` 这种东西(角标是 ``hint`` 渲染成的脚注编号)。对
    ``overall_decision`` 这种词汇量小的还能忍, ``criterion_verdict`` 的
    ``some_disagree`` 标注员根本读不出是什么意思。

    ``html`` 只影响显示, 提交进标注的仍是 ``value``(探针 project 36 实测:
    服务端 ``parsed_label_config`` 里 ``labels`` 仍是 ``KEY_B``, ``html``
    只进 ``labels_attrs``; 写回标注读出来也是 ``KEY_B``)。
    """
    missing = [
        c.get("value")
        for c in tree.iter("Choice")
        if not (c.get("html") or "").strip()
    ]
    assert not missing, f"这些 Choice 没有 html 标签, 标注页会显示机器键: {missing}"


def test_choice_never_uses_alias(tree):
    """挡 ``alias=`` —— 它会**顶掉** ``value`` 成为提交上去的那个值。

    探针 project 36 实测, 服务端自己解析出来的 ``parsed_label_config``:

        <Choice value="KEY_C" alias="显示C"/>
          → labels:        ['显示C']        ← 选项标识变成了中文
          → control_weights: {'显示C': 1.0}

    也就是说前端点一下, 提交进标注的就是 ``显示C``, 而下游按 ``KEY_C`` 读。
    这个错误**没有任何信号**: validate 报绿、import 201、标注能提交, 只有等到
    读导出数据时才发现对不上。``html=`` 才是纯显示的那一个。
    """
    offenders = [
        (c.get("value"), c.get("alias"))
        for c in tree.iter("Choice")
        if c.get("alias")
    ]
    assert not offenders, f"alias 会顶掉 value 成为导出键, 改用 html=: {offenders}"


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


def test_list_tag_is_not_used(tree):
    """挡 ``<List>``。

    ctx7 的官方文档说它正好对路(对象数组 → 逐项 region), 但**在本机 LS 1.23
    上什么都不渲染**: 不报 "Tag with name list is not registered", 也不出列表
    (探针 project 34/ 2026-09-30 实测)。所以它进 ``_UNREGISTERED_TAGS`` ——
    这里的 "未注册" 指"不能依赖", 与"报错"是两回事。
    """
    assert "List" not in {el.tag for el in tree.iter()}


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
    # **先剥注释**: 说明段里会引用已下线的写法(例如 "<Text value="$criteria"/>"),
    # 那不是真的数据绑定, 不该要求 task.data 里有这个键。
    xml = re.sub(r"<!--.*?-->", "", xml, flags=re.DOTALL)
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
