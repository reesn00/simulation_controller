"""golden_ls.label_config: **渲染**黄金集复核用的 label_config（方案 22 §3.3）。

为什么是渲染而不是一个静态 XML
------------------------------
scout 轨迹那一路用的是静态文件 ``label_configs/trajectory_review_v2.xml``。
黄金集这一路多一样东西：**22 类分支的下拉选项**。

那 22 个选项的真源是 :data:`trajectory_pipeline.executor.branches.BRANCH_LABELS`。
抄进 XML 就是**抄第二份**——分支表改一处，两处就会漂，而漂了**不报错**：
标注员在下拉里选一个已经改名的分支，提交上来是一份没人认得的结论。

⇒ **由代码渲染**：分支选项现读 ``BRANCH_LABELS``，每次渲染都跟得上真源。
项目里存的那份是**建项目那一刻的快照**——这正好是对的：老项目对应老版本，
老版本就该配老口径（分支文本取行内的 ``code.branch_label``，见 exporter）。

LS 1.23 的三条硬约束（照抄 v1 实测教训，缺一条就整页崩）
----------------------------------------------------------
1. ``<TextArea>`` 的 ``toName`` 必须指向**真实存在**的 tag ⇒ 全部挂
   ``task_anchor``；缺了直接 ``Validation failed on : 'toName' is a
   required property``。
2. ``<Choices>`` 的 ``toName`` 必须指向**带 value 的** ``<Text>`` 数据标签
   （``task``），否则点一下标注区崩成空白页。
3. 隐藏只能靠容器 ``<View style="display:none">``；写在 ``<Text>`` 上的
   ``visible=`` 是**无声无效**的。

另：**不用 ``alias=``**——它会把 LS 认的选项标识从 ``value`` 静默换成
``alias``（v1 实测）。
"""

from __future__ import annotations

import hashlib
from typing import Any, Mapping

from trajectory_pipeline.evaluation.ls_bridge.exporter import TASK_ANCHOR_CONTROL

#: 九个只读展示块。**与 task.data 的键逐字一致**（契约测试盯住两处）。
#:
#: 顺序 = 人读的顺序：结论在前，现场在后，长正文最后（与
#: ``evidence.scene_summary`` 的口径同源）。
BLOCK_CONTROLS: tuple[str, ...] = (
    "version_block",   # ① 版本与署名
    "site_block",      # ② 站点与来源
    "gold_block",      # ③ 金标
    "code_block",      # ④ 代码判定
    "scores_block",    # ⑤ 维度分数
    "scene_block",     # ⑥ 现场读数
    "rubric_block",    # ⑦ 尺子
    "trajectory_block",  # ⑧ 轨迹（T 视图）
    "persona_block",   # ⑨ persona
)

#: 裁决控件。
VERDICT_CONTROL = "verdict"
HUMAN_BRANCH_CONTROL = "human_branch"
REASON_CONTROL = "reason"
NOTE_CONTROL = "note"

#: ``<Choices>`` 用的数据标签（必须是带 value 的 ``<Text>``）。
CHOICE_TARGET = "task"

#: 裁决三态。⚠️ **不预填**（不推 predictions）——预填等于给答案。
VERDICT_CHOICES: tuple[tuple[str, str], ...] = (
    ("agree", "代码判定与金标一致（不用改）"),
    ("disagree", "不一致（代码判定错了，要给分支）"),
    ("unverifiable", "判不了（现场读不到 / 证据不足）"),
)

#: 「人给的分支」里的「不适用」——代码判定没错时不必硬选一个分支。
NOT_APPLICABLE = "not_applicable"


def _esc(text: Any) -> str:
    """XML 属性值转义。"""
    return (
        str(text)
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


def render_label_config(*, branches: Mapping[str, str] | None = None) -> str:
    """渲染 label_config 全文。

    Args:
        branches: 分支 → 中文标签。**默认现读**
            :data:`executor.branches.BRANCH_LABELS`（真源）；测试可注入
            固定映射以免依赖被执行层状态。
    """
    if branches is None:
        from trajectory_pipeline.executor.branches import BRANCH_LABELS

        branches = BRANCH_LABELS

    lines: list[str] = []
    add = lines.append

    add("<View>")
    add('  <Header value="══ 版本 $version_label · 档 $split · 行 $golden_id ══"/>')
    add('  <Header value="══ 站点 ══ task $task_id / run $run_id"/>')
    add("")
    add("  <!-- 隐藏锚点（LS 1.23 实测硬约束，理由见模块 docstring）：")
    add("       TextArea 的 toName 必须指向真实存在的 tag；")
    add("       Choices 的 toName 必须指向带 value 的 Text 数据标签；")
    add("       隐藏只能靠容器 display:none，写在 Text 上的 visible= 无声无效。 -->")
    add('  <View style="display:none">')
    add(f'    <Text name="{TASK_ANCHOR_CONTROL}" value="$golden_id"/>')
    add(f'    <Text name="{CHOICE_TARGET}" value="$golden_id"/>')
    add("  </View>")
    add("")

    headers = {
        "version_block": "══ ① 版本与金标署名 ══ 机器署名的金标要特别当心",
        "site_block": "══ ② 站点与来源 ══",
        "gold_block": "══ ③ 金标（人工真值）══",
        "code_block": "══ ④ 代码判定与证据 ══",
        "scores_block": "══ ⑤ 维度分数 ══",
        "scene_block": "══ ⑥ 现场读数 ══ 页面标题 / 每步观察 / 正文预览",
        "rubric_block": "══ ⑦ 尺子 ══ 条款原文 / 分支表 / 分支前置事实",
        "trajectory_block": "══ ⑧ 轨迹 ══ T 视图（per-Run 完整任务视图）",
        "persona_block": "══ ⑨ persona ══",
    }
    rows = {
        "version_block": 8, "site_block": 6, "gold_block": 6, "code_block": 8,
        "scores_block": 10, "scene_block": 16, "rubric_block": 20,
        "trajectory_block": 16, "persona_block": 8,
    }
    for control in BLOCK_CONTROLS:
        add(f'  <Header value="{_esc(headers[control])}"/>')
        add(
            f'  <TextArea name="{control}" toName="{TASK_ANCHOR_CONTROL}" '
            f'editable="false" rows="{rows[control]}" value="${control}"/>'
        )
        add("")

    add(f'  <Header value="══ 裁决（必选）══"/>')
    add(
        f'  <Choices name="{VERDICT_CONTROL}" toName="{CHOICE_TARGET}" '
        f'choice="single" required="true">'
    )
    for value, html in VERDICT_CHOICES:
        add(f'    <Choice value="{_esc(value)}" html="{_esc(html)}"/>')
    add("  </Choices>")
    add("")

    add(f'  <Header value="══ 人给的分支（裁决选了「不一致」时必填）══  22 类封闭集，取值见 ⑦ 尺子里的分支表"/>')
    add(
        f'  <Choices name="{HUMAN_BRANCH_CONTROL}" toName="{CHOICE_TARGET}" '
        f'choice="single" required="false">'
    )
    for branch, label in branches.items():
        add(f'    <Choice value="{_esc(branch)}" html="{_esc(label)}"/>')
    add(f'    <Choice value="{NOT_APPLICABLE}" html="不适用（代码判定没错）"/>')
    add("  </Choices>")
    add("")

    add(
        f'  <TextArea name="{REASON_CONTROL}" toName="{TASK_ANCHOR_CONTROL}" '
        f'maxSubmissions="1" '
        f'placeholder="裁决理由（选了「不一致」时必填）：凭什么说代码判错了"/>'
    )
    add(
        f'  <TextArea name="{NOTE_CONTROL}" toName="{TASK_ANCHOR_CONTROL}" '
        f'maxSubmissions="1" placeholder="备注（选填）"/>'
    )
    add("</View>")
    return "\n".join(lines) + "\n"


def label_config_sha(xml: str) -> str:
    """渲染结果的指纹（落台账用）。

    ⚠️ **口径：对「渲染出来的那份字符串」原样算**（含末尾换行）——
    台账里的 sha 因此与**留底文件的字节**一一对应（留底按字节写，见
    ``cli._write_text_bytes``）⇒ 事后拿留底文件复算 sha 一定对得上。
    """
    return hashlib.sha256(xml.encode("utf-8")).hexdigest()[:12]


def label_config_matches(stored: str, rendered: str) -> bool:
    """LS 端存的那份 label_config **是不是**渲染出来的那份。

    ⚠️ **实测（LS 1.23.0，2026-10-29）**：LS 把 XML 存下来时**去掉了末尾的
    换行**（内容一字不差，就差那一个 ``\\n``）⇒ 直接比 sha 会「不一致」，
    而那**不是**配置不同，是**尾随空白**。

    ⇒ 这里比的是 **strip 之后的内容**。**别改成比 sha**：sha 记的是
    「留底文件的字节」，用它去比服务端那份（少一个换行）必然不等，
    会把「配置没变」报成「配置漂了」——而那种误报会让人白白重推一遍。
    """
    return stored.strip() == rendered.strip()


__all__ = [
    "BLOCK_CONTROLS",
    "CHOICE_TARGET",
    "HUMAN_BRANCH_CONTROL",
    "NOT_APPLICABLE",
    "NOTE_CONTROL",
    "REASON_CONTROL",
    "VERDICT_CHOICES",
    "VERDICT_CONTROL",
    "label_config_matches",
    "label_config_sha",
    "render_label_config",
]
