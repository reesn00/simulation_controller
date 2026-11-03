"""训练侧两段投影的**一致性口径**——跨层地基，不属 ``assembler``。

为什么放在顶层
--------------
同一份轨迹要发两段：

* ``messages``——纯文本形态（``[动作] goto(…)``），留给 LS 给人看；
* ``openai.messages``——function-calling 形态，训练侧喂模型。

O-5 / 方案 23 R5-2 要求两段**逐条一致**：同一动作序列 / 同一观察内容 /
同一收尾，差异只允许形态。这条校验要被两个地方调用：

- :mod:`trajectory_pipeline.assembler.task_view` —— 渲染完、**落盘前**；
- :mod:`trajectory_pipeline.evaluation.ls_bridge.exporter` —— **导出前**，
  拦旧批次 / 人手改过的 T 视图。

而 ``ls_bridge`` 是 evaluation 层，契约测试
（``test_ls_bridge_d145_contract.py::TestImportBoundary``）**禁止它
import assembler**；T 也不该反向依赖 evaluation。两边都挂不了，于是同
:mod:`trajectory_pipeline.toolcatalog` 的处理：放到顶层，只放字面量与
纯函数，**不 import 任何功能模块**。

除了「两段都发」的验收，这个模块还兜住一个更隐蔽的失效：渲染器一旦
分叉，LS 上标注员看到的与训练侧喂进去的是两个故事，而报表上看不出
异常——两边都「成功导出」。
"""

from __future__ import annotations

from typing import Any, Mapping, Sequence

#: messages 侧动作行的前缀。渲染器（``views._render_action`` /
#: ``task_view._render_action``）按它写，本模块按它读——改前缀就是改
#: 两段的对齐口径。
ACTION_PREFIX = "[动作] "


def _plain_think(content: str) -> str:
    """messages 侧 assistant content → 思考段（动作行之前的部分）。"""
    return content.split(ACTION_PREFIX, 1)[0].strip()


def _plain_action_name(content: str) -> str:
    """messages 侧 assistant content → 动作行的工具名。"""
    index = content.find(ACTION_PREFIX)
    if index < 0:
        return ""
    rest = content[index + len(ACTION_PREFIX):].strip()
    return rest.split("(", 1)[0].strip()


def projection_problems(
    messages: Sequence[Mapping[str, Any]],
    openai_messages: Sequence[Mapping[str, Any]],
) -> list[str]:
    """逐条比对两段投影，返回不一致项（空列表 = 对齐）。

    比较项（对应方案 23 §5 A14）：

    - 动作数 / 工具名序列 / ``tool_call_id`` 是否按顺序稠密；
    - 每步思考文本（messages 的动作行前缀 vs openai 的 ``content``）；
    - 每步观察文本（messages 的 ``role=user`` vs openai 的 ``role=tool``）；
    - 收尾文本（两段末条无动作的 assistant）。
    """
    problems: list[str] = []
    plain_raw = list(messages or ())
    fc_raw = list(openai_messages or ())
    # 坏档里可能是标量 / 数组元素——不崩，按「这侧少了一条」报出来。
    # 导出侧对旧档 / 手改档跑这个函数，崩溃会把整批导出打断。
    plain = [m for m in plain_raw if isinstance(m, Mapping)]
    fc = [m for m in fc_raw if isinstance(m, Mapping)]
    if len(plain) != len(plain_raw):
        problems.append("messages 里有非对象元素")
    if len(fc) != len(fc_raw):
        problems.append("openai.messages 里有非对象元素")

    plain_actions = [
        i for i, m in enumerate(plain)
        if m.get("role") == "assistant"
        and ACTION_PREFIX in str(m.get("content") or "")
    ]
    fc_actions = [i for i, m in enumerate(fc) if m.get("tool_calls")]
    if len(plain_actions) != len(fc_actions):
        problems.append(
            f"动作数不一致：messages={len(plain_actions)} openai={len(fc_actions)}"
        )

    for seq, (pi, fi) in enumerate(zip(plain_actions, fc_actions)):
        pa = plain[pi]
        fa = fc[fi]
        pcontent = str(pa.get("content") or "")
        fcontent = str(fa.get("content") or "").strip()
        if _plain_think(pcontent) != fcontent:
            problems.append(f"第 {seq} 步思考文本不一致")

        pname = _plain_action_name(pcontent)
        calls = fa.get("tool_calls") or []
        # 一个动作步对应恰好一条 tool_call（契约）——多条时下面按第 0 条比
        # 名字会掩盖「一条平文本动作被拆成两条调用」。
        if len(calls) != 1:
            problems.append(f"第 {seq} 步 tool_calls 不是恰好一条：{len(calls)}")
        fname = str((calls[0].get("function") or {}).get("name") or "") if calls else ""
        if pname != fname:
            problems.append(f"第 {seq} 步工具名不一致：{pname!r} vs {fname!r}")
        if len(calls) == 1 and calls[0].get("id") != f"call_{seq}":
            problems.append(
                f"第 {seq} 步 tool_call_id 不是 call_{seq}：{calls[0].get('id')!r}"
            )

        pobs = (
            plain[pi + 1]
            if pi + 1 < len(plain) and plain[pi + 1].get("role") == "user"
            else None
        )
        fobs = (
            fc[fi + 1]
            if fi + 1 < len(fc) and fc[fi + 1].get("role") == "tool"
            else None
        )
        if (pobs is None) != (fobs is None):
            problems.append(f"第 {seq} 步观察一侧缺失")
        elif pobs is not None and fobs is not None:
            if str(pobs.get("content") or "") != str(fobs.get("content") or ""):
                problems.append(f"第 {seq} 步观察文本不一致")
            if fobs.get("tool_call_id") != f"call_{seq}":
                problems.append(
                    f"第 {seq} 步观察的 tool_call_id 不是 call_{seq}"
                )

    def _closing_of(items, *, action_key):
        close = None
        for m in items:
            if m.get("role") != "assistant":
                continue
            if action_key(m):
                continue
            close = str(m.get("content") or "")
        return close

    pclose = _closing_of(
        plain, action_key=lambda m: ACTION_PREFIX in str(m.get("content") or "")
    )
    fclose = _closing_of(fc, action_key=lambda m: bool(m.get("tool_calls")))
    if pclose != fclose:
        problems.append("收尾文本不一致")

    return problems


__all__ = ["ACTION_PREFIX", "projection_problems"]