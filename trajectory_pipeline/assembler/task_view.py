"""T · 完整任务视图渲染——把 P1 渲染成 per-Run 的三视图合一 dict。

设计纪律（与 :mod:`~trajectory_pipeline.assembler.views` 同源，但**不互相
import**——交接面是格式不是对象）：

1. **渲染源是 P1**，不从 N 份 P2 反推（D14.3）。
   - P1 是 per-Run 的完整动作流（顶层 ``steps[]`` + ``visits[i].steps[]``），
     反推要重编全局 step_index / tool_call_id / 跨单位聚合 outcome，
     反而引入新拼接逻辑。
   - T 与 P3 对称：P3 是 P2 的另一份渲染，T 是 P1 的另一份渲染。

2. **不 import executor**。``tests/contract/test_t_contract.py::TestNoExecutorImport``
   用 AST 强制——同 :mod:`~trajectory_pipeline.assembler.schema` / 读取 /
   views 同款的纪律。重建一批 T 必须脱离 executor 当前版本独立完成。

3. **不 import assembler.schema 任何名字**。``tests/contract/test_t_contract.py::Test模块图``
   用 AST 强制——T 是 P1 的渲染。:data:`_TASK_SYSTEM_PROMPT` 仍**自己
   声明**（与 schema.py 字面一致，由契约测试盯住）。

   ⚠️ **工具声明不再自己声明**（2026-10-20 改）：以前 T 抄了一份
   ``_TASK_TOOL_SPECS``，靠「复现一次 + 契约测试盯同步」防漂移。工具从
   6 条扩到 13 条、每条多两个字段之后，手抄两份必漂。现在三方
   （P2 的 schema / P3 的 views / T 的 task_view）都从
   :mod:`trajectory_pipeline.toolcatalog` 取——**唯一真源**。
   这不违反「T 不 import assembler.schema」：catalog 是**跨层地基**
   （不 import 任何功能模块），不是组装端点的模块。

   ⚠️ **outcome 配对口径也不再自己写**（2026-10-30 · 方案 23）：以前 T
   **按位置**配（``visits[i]`` ↔ ``outcomes[i]``），而那两条序列**不是
   一一对应**的（孤儿记账、候选阶段记账、多轮访问）——实测在 109/127 份
   真实存档（86%）上与 P2 的 URL 配对给出**不同**的结论，其中一份把
   **成功报成了失败**。现在 T 与 P2 都从
   :mod:`trajectory_pipeline.outcomerules` 取——同一个**纯模块**
   （不 import 任何功能模块），理由与 catalog 同款。

4. **P1 observation 是取证态**（full 6000 字符 / 80 元素 / 200 链接），
   经 :func:`~trajectory_pipeline.assembler.observation_view.render`
   裁到训练态再经 :func:`~trajectory_pipeline.assembler.views.render_observation`
   排版——与 P3 同款裁剪口径（320/10/4），只是 T 这边**自己跑一遍裁剪**，
   而 P3 那边是从 P2 已裁好的形态接过来。

5. **观察 / 动作 / 思考逐步对齐**（同 P3 第 6 条判断）。``infrastructure``
   步整步跳过——消息流里不存在的动作不该占号（与 P3 第 2 条判断同款）。

6. **任务级 summary 收尾**（同 P3 第 7 条判断）：事实来自 ``outcomes[]``
   （裁定 ②）+ **末个写成单元**的 reflection 结论（取法真源在
   :func:`closing.last_written_reflection`，P2 / P3 / T 共用）。收尾文本
   与 P3 **共用同一个函数**
   :func:`~trajectory_pipeline.assembler.closing.closing_text`
   （方案 23 R2-2：两侧必须说同一句话，差异只放附加信息）。处置词表
   ``_CLOSING_DISPOSITION`` 是 :data:`closing.CLOSING_DISPOSITION` 的
   别名，**键集与 ``executor.branches.BRANCH_LABELS`` 的同步由契约测试
   盯住**——不 import executor，交接面是格式不是 import。

T 落盘由 :func:`trajectory_pipeline.storage.task_views.write_task_view`
接管（与 ``write_p3.write_views`` 同款纪律：原子写、默认不覆盖）。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence

from trajectory_pipeline import outcomerules, toolcatalog
from trajectory_pipeline.assembler import (
    closing,
    excluded,
    observation_view,
    pool,
    screen,
)
from trajectory_pipeline.sample_gate import sample_blocked_reason
from trajectory_pipeline.common import refine_sidecar
from trajectory_pipeline.common.rationale_text import strip_empty_history_lead_in
#: 两段投影一致性口径挂在顶层（方案 23 R5-2 / O-5）：ls_bridge 是
#: evaluation 层，契约禁止它 import assembler，而校验两边都要用。
#: 本模块 re-export，``task_view.projection_problems`` 仍可用。
from trajectory_pipeline.projection import (
    projection_problems,
    tool_declaration_problems,
)

#: 路径权威的 re-export：T 渲染端不许自己拼 ``output/pipeline/task_views``
#: 字面量。改存储布局只动 ``trajectory_pipeline.storage.task_views_dir``
#: 一处，这里照转发。由 ``tests/contract/test_t_contract.py::Test产物根``
#: 盯住两侧同一对象。
from trajectory_pipeline.storage import task_views_dir as storage_task_views_dir  # noqa: F401

__all__ = [
    "to_task_messages",
    "to_task_openai_messages",
    "to_task_meta",
    "projection_problems",
    "build_task_view",
    "build_all_task_views",
    "TaskViewRenderResult",
    "TaskViewBatchReport",
    "storage_task_views_dir",
]


# ── 工具清单与系统提示词（自己声明，不 import schema） ──────────────


#: T 视图的系统提示词。**字面字符串**与
#: :data:`trajectory_pipeline.assembler.schema.SYSTEM_PROMPT` 逐字相同，
#: 由 ``tests/contract/test_t_contract.py::TestSystemPromptConsistent`` 盯住。
#: 这条同步纪律很关键——T 视图与 P3 是**两种不同的渲染**，如果 ls_改
#: 了 P3 的版本而忘改 T，那两套 SFT 训练数据用的就是不同 prompt，
#: 模型看到的「系统」飘忽不定。
_TASK_SYSTEM_PROMPT: str = (
    "你是能帮用户找在线观看资源的助手。\n"
    "拿到用户的诉求、可用工具，以及操作后页面返回的观察。\n"
    "按下面的规则行动：\n"
    "1. 只根据观察里出现的东西判断，不要根据网址或片名猜。\n"
    "2. 找不到能看正片的资源时如实说找不到，不要编造链接。\n"
)


#: T 视图的训练动作空间。**从 :mod:`trajectory_pipeline.toolcatalog` 派生**——
#: 那份是唯一真源（P3 的 ``assembler.schema`` / ``assembler.views`` 也从它取）。
#:
#: 以前这里是**自己抄一份**的（"T 连 SYSTEM_PROMPT 与 TOOL_SPECS 都自己
#: 声明"），靠 ``test_t_contract.py::Test常量同步`` 盯两处逐字一致。
#: 抄一份的代价在这次扩容里现形了：工具从 6 条扩到 13 条、每条多两个
#: 字段（参数说明 / required），手抄两份必漂。
#:
#: 现在**不抄**了：catalog 是跨层地基（不 import 任何功能模块），T 从它取
#: **不违反**「T 不 import assembler.schema / assembler.views」——它俩是
#: 组装端点的模块，catalog 不是。
#:
#: 字典形态（不是 dataclass），保持与既有契约测试的形状一致。
_TASK_TOOL_SPECS: tuple[dict[str, Any], ...] = tuple(
    {
        "name": d.name,
        "params": tuple(p.name for p in d.params),
        "instruction": d.instruction,
    }
    for d in toolcatalog.CATALOG
)


# ── 工具声明 ───────────────────────────────────────────────────────


def task_tools(surface: str) -> list[dict[str, Any]]:
    """T 视图的工具声明（模型可见集，不进 ``new_tab`` / ``close_tab``）。

    与 :func:`~trajectory_pipeline.assembler.views._visible_tools` **同源**
    ——两边都从 :mod:`trajectory_pipeline.toolcatalog` 取，所以 T 与 P3 的
    工具声明**逐字一致**，不需要「复现一次 + 契约测试盯同步」那套。

    ⚠️ 参数级说明与 ``required`` 集合也来自 catalog：``click`` 的语义
    目标在 ``target={tag,label}`` 里，声明侧不写这两个参数，训出来的
    模型就会发空参数的点击（详见 ``views._openai_tool`` 的注释）。

    ⚠️ **按工具面取**（2026-10-20 方案 15 §7 R1）：T 读的是 P1 而不是
    P2，所以面从**源存档的 ``driver``** 折算（见 :func:`_surface_of`）。

    ⚠️ ``surface`` **没有默认值**（2026-11-03 · Q-A1）：默认面曾让
    ``build_task_view`` 漏传面却静默落盘（声明 obscura / 调用 camofox）。
    漏传现在直接 ``TypeError``，不再产出「报告上看不出异常」的错面声明。
    """
    return toolcatalog.openai_functions(surface=surface)


# ── P1 → units 拆条 ────────────────────────────────────────────────


def _excluded_unit_map(archive: Mapping[str, Any]) -> dict[str, str]:
    """``unit -> 被访问的排除平台``（方案 23 A10 / R4-2(b)）。

    与 P2 :func:`assembler.schema.split_archive_report` 共用
    :mod:`assembler.excluded` 的判定，**不各写一份**——同一条访问点一个剔
    一个留，而报告上只会显示「少了一条」。**整条 unit 剔**：只剔命中步会
    留下半条轨迹，前面的步还在为这个平台服务。
    """
    return excluded.excluded_unit_map(archive)


def _flatten_units(archive: Mapping[str, Any]) -> list[dict[str, Any]]:
    """把 P1 拆成 search + N 个 site 的 unit 列表（**已做访问级剔除**）。

    每个 unit 含 ``unit`` / ``actions`` / ``observations`` / ``rationales``
    / ``errors`` 四段，每段都是**等长**数组（与 P2 的 ``actions`` /
    ``observations`` / ``rationales`` 同款对齐）。``errors`` 是 P1 的
    ``steps[i].error``——R1-2 要拿它渲染「本步动作无效」。

    访问被排除平台的 unit 在这里就整条不出现：``unit`` 名保留原始
    ``site-i`` 编号，不重排（P2 的 sample_id 用的是同一套编号）。
    """
    out: list[dict[str, Any]] = []
    blocked = _excluded_unit_map(archive)

    # ── 单元 1：search ──
    search_steps = list(archive.get("steps") or ())
    if "search" not in blocked:
        out.append({
            "unit": "search",
            "actions": [s.get("action") or {} for s in search_steps],
            "observations": [s.get("observation") for s in search_steps],
            "rationales": [s.get("rationale") for s in search_steps],
            "errors": [str(s.get("error") or "") for s in search_steps],
        })

    # ── 单元 2..N：sites ──
    for i, visit in enumerate(archive.get("visits") or (), 1):
        if f"site-{i}" in blocked:
            continue
        site_steps = list(visit.get("steps") or ())
        out.append({
            "unit": f"site-{i}",
            "actions": [s.get("action") or {} for s in site_steps],
            "observations": [s.get("observation") for s in site_steps],
            "rationales": [s.get("rationale") for s in site_steps],
            "errors": [str(s.get("error") or "") for s in site_steps],
        })
    return out


# ── 观察渲染（P1 取证态 → 训练态文本） ──────────────────────────────


def _render_observation_text(obs: Mapping[str, Any]) -> str:
    """训练态 observation → 排版文本（与 :func:`views.render_observation` 同款）。

    **刻意不复用**——T 不 import :mod:`assembler.views`（P3 的产物），
    交接面是格式不是对象（D14.3 / D14.6）。契约测试
    ``test_t_contract.py::TestRenderObservationSync`` 盯住两份实现
    字面字符串一致。
    """
    if not obs:
        return "（本步没有取到页面内容）"

    if "observation_missing" in (obs.get("degraded") or ()):
        return "（本步没有取到页面内容）"

    parts: list[str] = []
    title = str(obs.get("page_title") or "")
    url = str(obs.get("url") or "")
    if title or url:
        parts.append(f"标题：{title}\n网址：{url}")

    # 读步只排版被读的那一块（方案 23 R4-1）。与
    # :func:`views.render_observation` **逐字同款**，由
    # ``test_t_contract.py::TestRenderObservationSync`` 盯住。
    read = str(obs.get("read") or "")
    if read in ("snapshot", "interactive", "links"):
        label = {
            "snapshot": "正文",
            "interactive": "可交互元素",
            "links": "链接",
        }[read]
        parts.append(f"[读取: {label}]")
        if read == "snapshot":
            body = str(obs.get("body_text") or "")
            parts.append(f"正文：\n{body}" if body else "正文：（空）")
        elif read == "interactive":
            elems = obs.get("interactive_elements") or []
            if elems:
                lines = [observation_view.element_line(e) for e in elems]
                parts.append("可交互元素：\n" + "\n".join(lines))
            else:
                parts.append("可交互元素：（空）")
        else:
            links = obs.get("links") or []
            if links:
                lines = [
                    f"  - {l.get('text', '')} → {l.get('href', '')}".rstrip()
                    for l in links
                ]
                parts.append("链接：\n" + "\n".join(lines))
            else:
                parts.append("链接：（空）")
    else:
        body = str(obs.get("body_text") or "")
        parts.append(f"正文：\n{body}" if body else "正文：（空）")

        elems = obs.get("interactive_elements") or []
        if elems:
            lines = [observation_view.element_line(e) for e in elems]
            parts.append("可交互元素：\n" + "\n".join(lines))
        else:
            parts.append("可交互元素：（空）")

        links = obs.get("links") or []
        if links:
            lines = [
                f"  - {l.get('text', '')} → {l.get('href', '')}".rstrip() for l in links
            ]
            parts.append("链接：\n" + "\n".join(lines))

        counts = (
            f"video 标签 {obs.get('video_tag_count', 0)} 个，"
            f"iframe {obs.get('iframe_count', 0)} 个"
        )
        parts.append(counts)

    # 「动作白做」与「信息没采到」分列（方案 23 R1-2），同 P3。
    error = str(obs.get("action_error") or "").strip()
    if error:
        if len(error) > 120:
            error = error[:119] + "…"
        parts.append(f"[本步动作无效: {error}]")

    degraded = list(obs.get("degraded") or ())
    if degraded:
        parts.append(f"[采集降级: {'、'.join(str(d) for d in degraded)}]")
    # **精炼留痕**（方案 25）：正文是模型选行裁出来的，不是「页面前 N 字」。
    # 不说破的话，模型会把精简过的正文当成「页面就这么多内容」——
    # 那是在训练侧教出来的幻觉。
    # ⚠️ 与 ``views.render_observation`` **逐字同款**（O-5：
    # 两段投影刻意不互相 import，字面一致靠契约测试盯着）。
    if obs.get("body_refined"):
        kept_lines = int(obs.get("body_refined_lines") or 0)
        parts.append(f"[内容已精简: 保留 {kept_lines} 行]")
    trunc = obs.get("truncated") or {}
    if trunc.get("source") or trunc.get("view"):
        which: list[str] = []
        if trunc.get("source"):
            which.append("上游截断")
        if trunc.get("view"):
            which.append("本次裁剪")
        parts.append(f"[内容不完整: {'、'.join(which)}]")
    return "\n\n".join(parts)


def _render_observation(
    obs: Mapping[str, Any] | None,
    *,
    focus: Sequence[str] = (),
    keep: Sequence[str] = (),
    action_error: str = "",
    refined: Mapping[str, Sequence[str]] | None = None,
) -> str:
    """P1 取证态 observation → 训练态文本。

    两段：① 经 :func:`observation_view.render` 把 P1 的 full body_text /
    80 元素 / 200 链接裁到 P3 同款训练态（320/10/4）；② 再经
    :func:`_render_observation_text` 排版成 ``标题：... 网址：...`` 形态。

    ``focus`` / ``keep`` / ``action_error`` 同 P2 的
    :func:`~trajectory_pipeline.assembler.builder._sample_to_json`：
    任务相关词、下一步动作目标、本步动作错误——T 读 P1，不经过 P2，
    所以这三样必须在这里再算一遍。

    ``refined`` 是**精炼旁挂索引**（方案 25）：有它就用模型选出的行
    重建正文（同一套保真口径，见
    :func:`trajectory_pipeline.assembler.observation_view.render`）。

    空 obs 走 :func:`observation_view.render` 的 None 路径，输出
    ``degraded=["observation_missing"]`` 的占位 dict，再由
    ``_render_observation_text`` 渲染成 ``（本步没有取到页面内容）``——
    与 P3 第 8 条判断同款。
    """
    train_view = observation_view.render(
        obs, focus=focus, keep=keep, action_error=action_error, refined=refined,
    )
    return _render_observation_text(train_view)


def _action_keep(action: Mapping[str, Any]) -> tuple[str, ...]:
    """一个动作的可读目标（点击名 / 导航网址）——同 P2 口径。"""
    target = action.get("target") or {}
    name = str(target.get("name") or target.get("label") or "").strip()
    url = str((action.get("params") or {}).get("url") or "").strip()
    return tuple(x for x in (name, url) if x)


def _next_keep(actions: Sequence[Mapping[str, Any]], index: int) -> tuple[str, ...]:
    """第 ``index`` 条观察之后**下一个可见动作**的目标。"""
    for action in actions[index + 1:]:
        if action.get("origin") == "infrastructure":
            continue
        keep = _action_keep(action)
        if keep:
            return keep
    return ()


# ── 训练步筛选（方案 23 R3-1 / R3-2）─────────────────────────────


@dataclass(frozen=True, slots=True)
class _TaskStep:
    """筛选后留下的一步。

    ``step_index`` 是**全局原始序号**（跨 unit 连续、infrastructure 步
    也占号），与 meta 的 ``rationale_steps.step_index`` 同一坐标系——
    复核时能直接对上「第几步」。
    """

    step_index: int
    action: Mapping[str, Any]
    observation: str
    rationale: Any


def _screened_task_steps(
    archive: Mapping[str, Any],
    *,
    refined: Mapping[str, Sequence[str]] | None = None,
) -> tuple[list[_TaskStep], screen.Screening]:
    """整条 run 的可见步 + 训练筛选（死循环剔出 / 重复标记）。

    **两段投影共用这一份结果**（``messages`` 与 ``openai.messages``）：
    筛选如果各做各的，两段就会在「哪些步还在」上分叉，而 O-5 要求
    两段逐条一致。观察文本在这里**只渲染一次**，比较对象与最终进语料
    的文本是同一份（与 P3 侧 :func:`views._screened_steps` 同款口径）。

    观察的 ``keep``（下一步动作目标）仍按**原始动作序列**算，不按筛选后
    的序列——否则筛一步会改写它前面那一步的观察文本，前后两次渲染
    （比较用 / 落盘用）就对不上了。

    ``refined`` 是**精炼旁挂索引**（方案 25）。它会改观察文本 ⇒ 也会改
    「相邻同动作 + 观察逐字相同」的判定 ⇒ **必须三处同款传进来**
    （``messages`` / ``openai.messages`` / ``meta``），否则 meta 报的
    「剔了哪几步」与消息里实际少掉的步对不上。
    """
    units = _flatten_units(archive)
    focus = (str(archive.get("title") or ""),) if archive.get("title") else ()
    staged: list[screen.Step] = []
    runtime: dict[int, _TaskStep] = {}
    global_index = 0
    for unit in units:
        actions = unit["actions"]
        observations = unit["observations"]
        errors = unit["errors"]
        rationales_by_index = {
            int(i): r for i, r in enumerate(unit["rationales"]) if r is not None
        }
        for i, action in enumerate(actions):
            gi = global_index
            global_index += 1
            if action.get("origin") == "infrastructure":
                # infrastructure 步整步跳过（同 P3 第 2 条判断）；但仍占
                # 原始步号，保证 step_index 与 rationale_steps 对得上。
                continue
            obs = observations[i] if i < len(observations) else None
            text = _render_observation(
                obs or {},
                focus=focus,
                keep=_next_keep(actions, i),
                action_error=errors[i] if i < len(errors) else "",
                refined=refined,
            )
            staged.append(screen.Step(
                step_index=gi,
                unit=unit["unit"],
                action=action,
                observation=text,
            ))
            runtime[gi] = _TaskStep(
                step_index=gi,
                action=action,
                observation=text,
                rationale=rationales_by_index.get(i),
            )

    result = screen.screen(staged)
    return [runtime[s.step_index] for s in result.steps], result


# ── 渲染辅助函数（与 views.py 同款，刻意不复用 ——交接面是格式） ──


def _rationale_text(rationale: Any) -> str:
    """一步的思考+结论，渲染成 assistant 消息的**前缀**。

    与 :func:`~trajectory_pipeline.assembler.views._rationale_text` 同款
    行为（缺一条不补空串，``status != "written"`` 返回空串——理由同 P3
    第 6 条判断，编一段占位思考会直接污染 CoT SFT）。

    ⚠️ **2026-10-19 增量六十一发现**：本函数历史上读 ``rationale["text"]``
    扁平字段，而生产 P1 实际写的是嵌套 ``rationale["rationale"]["text"]``
    （:func:`trajectory_pipeline.executor.agent_loop._native_think_payload`
    与 thought_refactor 写出形态）。结果是**所有 P1 不论 scripted / agent**
    思考文本都被丢掉,assistant 消息只剩 tool_call。本函数现在先读嵌套、
    后读扁平,两条路径都支持。
    """
    if not isinstance(rationale, Mapping) or rationale.get("status") != "written":
        return ""
    # 嵌套形态：{"rationale": {"text": "...", "conclusion": "..."}}
    inner = rationale.get("rationale")
    if isinstance(inner, Mapping):
        text = str(inner.get("text") or "").strip()
        conclusion = str(inner.get("conclusion") or "").strip()
    else:
        # 扁平形态（兜底）：{"text": "...", "conclusion": "..."} 顶层
        text = str(rationale.get("text") or "").strip()
        conclusion = str(rationale.get("conclusion") or "").strip()
    # 训练侧清洗：去掉「目前没有此前操作。」这类空状态陈述（2026-11-10
    # owner 指出）。P1 原文不动，只清洗 T/P3 组装出来的视图；两段必须
    # 用同一个函数，否则复核视图与训练视图会对同一句显示不同文本。
    text = strip_empty_history_lead_in(text)
    parts = [p for p in (text, conclusion) if p]
    return ("\n".join(parts) + "\n") if parts else ""


def _action_line(act: Mapping[str, Any]) -> str:
    """一条动作渲染成一行可读文本（与 :func:`views._action_line` 同款）。"""
    tool = str(act.get("tool") or "")
    params = dict(act.get("params") or {})
    target = act.get("target")
    if isinstance(target, Mapping):
        params.update({k: v for k, v in target.items() if v})
    if not params:
        return f"[动作] {tool}"
    return f"[动作] {tool}(" + ", ".join(f"{k}={v}" for k, v in params.items()) + ")"


def _arguments(act: Mapping[str, Any]) -> dict[str, Any]:
    """动作 → function-calling 的 ``arguments``（平铺后的 dict，与 :func:`views._arguments` 同款）。

    **落盘形态不是这个 dict**，见 :func:`_arguments_json`。
    """
    args: dict[str, Any] = dict(act.get("params") or {})
    target = act.get("target")
    if isinstance(target, Mapping):
        for k, v in target.items():
            if v not in (None, ""):
                args[str(k)] = v
    return args


def _arguments_json(act: Mapping[str, Any]) -> str:
    """动作 → 落盘的 ``arguments`` **JSON 字符串**（OpenAI Chat Completions 契约）。

    ``function.arguments`` 在协议里是**字符串**，模型侧发的也是字符串。
    落成 dict 会让按协议解析 ``json.loads`` 的下游崩，或者更糟——静默按
    Python 对象用掉，训出来的模型学会发 dict。改前全量 4902/4902 是 dict
    （2026-11-02 审计，方案 23 R3-4）。``ensure_ascii=False`` 保留中文原样。
    """
    return json.dumps(_arguments(act), ensure_ascii=False)


# ── 任务级 summary 处置词表（自行声明，不 import executor） ──────────


#: 分支 → 面向用户的排除原因。字面量的**真源**在
#: :data:`trajectory_pipeline.assembler.closing.CLOSING_DISPOSITION`
#: （P3 与 T 共用，方案 23 R2-2）；这里保留同名别名只是为了既有调用方
#: 与测试的读法。与 ``executor.branches.BRANCH_LABELS + {"success"}`` 的
#: 键集同步仍由契约测试盯住——不 import executor，交接面是格式不是 import。
_CLOSING_DISPOSITION: dict[str, str] = closing.CLOSING_DISPOSITION


#: 必须存在的分支全集。T 视图处置词表与 :data:`_CLOSING_DISPOSITION` 的
#: 键集对照——少一个就报 ``KeyError``。**字面字符串形式**与 executor
#: ``BRANCH_LABELS`` 一一对应，由契约测试盯住。**不含** ``success``——
#: T 视图的成功处置词是手写字符串，不在词表里。
_REQUIRED_DISPOSITION_KEYS: tuple[str, ...] = (
    "not_play_site",
    "unreachable_hard",
    "no_play_control",
    "trailer_only",
    "trailer_suspect",
    "component_unverified",
    "player_unverified",
    "subscription_wall",
    "unresolved",
    "login_required",
    "blocked",
    "broken",
    "partial_free",
    "not_aggregator",
    "aggregator_no_entries",
    "not_article",
    "article_no_links",
    "no_site_search",
    "site_search_no_results",
    "site_search_routed",
    "login_wall_or_blocked",  # deprecated
)


def _run_reflection(
    archive: Mapping[str, Any], blocked: Mapping[str, str]
) -> Mapping[str, Any] | None:
    """轨迹级反思：**末个写成单元**的 ``reflection``（剔后）。

    P1 里没有轨迹级 reflection——搜索阶段那一份在存档顶层，站点那几份在
    ``visits[i]``。取法真源在
    :func:`~trajectory_pipeline.assembler.closing.last_written_reflection`：
    P2 的 ``reflection`` 字段与 T 的收尾结论走**同一份判断**——各写一份，
    同一份存档在两侧会收尾成不同的话。
    """
    bodies: list[Mapping[str, Any]] = []
    if "search" not in blocked:
        bodies.append(archive)
    for i, visit in enumerate(archive.get("visits") or (), 1):
        if f"site-{i}" in blocked:
            continue
        if isinstance(visit, Mapping):
            bodies.append(visit)
    return closing.last_written_reflection(bodies)


def _closing_text(archive: Mapping[str, Any]) -> str | None:
    """T 视图的任务级收尾——**面向用户的答复**（方案 23 R2-1 / R2-2）。

    与 :func:`~trajectory_pipeline.assembler.views._closing_text` 调**同一个**
    :func:`~trajectory_pipeline.assembler.closing.closing_text`：同一套模板
    与同一份原因词表。⚠️ **2026-11-03 · re-base 到 per-Run**：per-unit 时代
    P3 只带**本单元**的 outcome、T 带整条 run；per-Run 之后**两边的事实
    一样**（整条 run 的站点清单 + 同一个决定性结论）⇒ 两边说的是
    **同一句话**。

    - **空 trajectory**（零可见模型动作）不编总结；
    - **决定性结论**（裁定 ②：任务级标签，剔后取的末条结论）说成功就说
      找到了，说失败就说没找到——**中途成功过不算成功**；
    - **轨迹级结论**来自**末个写成单元**的 reflection
      （``rationale.conclusion``，剔后取）——与 P2 的 ``reflection`` 字段、
      P3 的收尾**同一条判断**（真源
      :func:`~trajectory_pipeline.assembler.closing.last_written_reflection`），
      所以两侧对同一份存档说的是**逐字相同**的话；
    - **分支枚举名与分支计数不进文本**——那是并池口径，不是助手该对
      用户说的话（审计 G3）；
    - 一个站点都没访问到 ⇒ 如实说「没有找到可访问站点」，不输出
      ``total_sites=N`` 这类内部计数。

    ⚠️ **2026-11-04 · T 也带上轨迹级结论**：此前 T 不读 reflection，同一
    条 run 的 P3 收尾带模型结论、LS 卡面（T）却只到「已排除的站点」——
    人核读到的与会训到的不是同一句话。P1 没有轨迹级 reflection，取法
    是「末个写成单元」的那份（与 P2 同一条），不是把各单元结论拼起来。

    ⚠️ **2026-10-30 · 方案 23**：处置词从「任一成功 ⇒ 成功」改成
    **最后一个结论**（裁定 ②）——与 T 的 ``overall_branch``、与 P2 的
    任务级标签**同一条**。旧口径会把「中途成功过、后来又失败」的轨迹
    报成成功，而那正是训练侧要学的东西。
    """
    units = _flatten_units(archive)
    has_visible_action = any(
        a.get("origin") != "infrastructure"
        for u in units
        for a in u["actions"]
    )
    if not has_visible_action:
        return None

    blocked = _excluded_unit_map(archive)
    # 站点级结论按 **URL 配对**（:mod:`trajectory_pipeline.outcomerules`），
    # 不按位置——位置口径实测与 P2 在 **86%** 的存档上给出不同结论。
    kept = [
        c for c in outcomerules.site_conclusions(archive)
        if f"site-{c.index}" not in blocked
    ]
    site_outcomes = [c.outcome for c in kept if isinstance(c.outcome, Mapping)]
    # 决定性结论 = 任务级标签那条（裁定 ②，**剔后**取）。
    conclusion = outcomerules.task_conclusion(
        archive, exclude=excluded.excluded_outcome_indices(archive)
    )
    decisive = (
        conclusion.outcome
        if isinstance(conclusion.outcome, Mapping)
        else None
    )
    return closing.closing_for_run(
        title=str(archive.get("title") or ""),
        outcomes=closing.run_closing_outcomes(
            decisive=decisive,
            site_outcomes=site_outcomes,
        ),
        conclusion=closing.reflection_conclusion(
            _run_reflection(archive, blocked)
        ),
        # 一个站点都没访问到（有动作，但全在搜索阶段）——不说「没找到
        # 可播放网址」，说「没找到可访问站点」：前者是看过了没有，后者
        # 是压根没看。
        no_sites=not kept,
    )


# ── messages 视图（通用形态） ──────────────────────────────────────


def to_task_messages(
    archive: Mapping[str, Any],
    *,
    refined: Mapping[str, Sequence[str]] | None = None,
) -> list[dict[str, Any]]:
    """通用 ``{role, content}`` 序列（per-Run 完整对话流）。

    按时间顺序拼：search 单元的 steps[] → 全部 site-N 单元的 steps[] →
    任务级 summary 收尾。

    与 :func:`~trajectory_pipeline.assembler.views.to_messages` 同款
    行为但**输入是 P1**（不是 P2）：动作流从 P1 顶层 ``steps[]`` +
    ``visits[i].steps[]`` 取；观察按 ``step_index`` 全局重编（在拼装过程
    中**自动实现**——拼接时按出现顺序编）。

    **训练筛选**：死循环步（相邻同动作 + 观察逐字相同）整步剔出，
    重复动作在 meta 里标记（方案 23 R3-1 / R3-2）；规则见
    :func:`_screened_task_steps`，两段投影共用同一份结果。

    ``refined`` 是**精炼旁挂索引**（方案 25）——有它时观察正文按模型
    选出的行重建。
    """
    msgs: list[dict[str, Any]] = []

    system = _TASK_SYSTEM_PROMPT
    if system:
        msgs.append({"role": "system", "content": system})

    user = str(archive.get("user_prompt") or "")
    if user:
        msgs.append({"role": "user", "content": user})
    # 空 user_prompt 不发空消息（同 P3 第 4 条判断）。

    steps, _screening = _screened_task_steps(archive, refined=refined)
    for step in steps:
        msgs.append({
            "role": "assistant",
            "content": _rationale_text(step.rationale) + _action_line(step.action),
        })
        # 缺观察也显式发占位回执，不留没有回音的动作（同 P3 第 8 条）。
        msgs.append({"role": "user", "content": step.observation})

    closing_text = _closing_text(archive)
    if closing_text is not None and steps:
        msgs.append({"role": "assistant", "content": closing_text})
    return msgs


# ── openai 视图（function-calling 形态） ─────────────────────────────


def _surface_of(archive: Mapping[str, Any]) -> str:
    """这条 P1 的**工具面**（2026-10-20 方案 15 §7 R1）。

    T 视图读的是 P1 而不是 P2，所以面从**源存档的 ``driver``** 折算。
    没记驱动（历史批次）→ 默认面，那正是它们真实跑出来的面。
    """
    return toolcatalog.surface_of_driver(archive.get("driver"))


def to_task_openai_messages(
    archive: Mapping[str, Any],
    *,
    refined: Mapping[str, Sequence[str]] | None = None,
) -> list[dict[str, Any]]:
    """OpenAI function-calling 形态（per-Run）。

    ``tool_call_id`` 由 **可见步序号** 派生（``call_0`` / ``call_1``…），
    不取自 P1 的任何字段——P1 里没有可用的调用 id，而 seq 是**确定**的
    不是猜的。``infrastructure`` 步整步跳过后序号仍**稠密**——
    消息流里不存在的动作不该占号。训练筛选同
    :func:`to_task_messages`（同一份 :func:`_screened_task_steps`）。

    本函数**只生成 messages 段**，不返回 tools；顶层 ``tools`` 由
    :func:`build_task_view` 按同一工具面单独取（2026-11-03 · Q-A1）。

    ``refined`` 是**精炼旁挂索引**（方案 25）——与
    :func:`to_task_messages` **同款传入**，两段投影才逐条一致（O-5）。
    """
    out: list[dict[str, Any]] = []
    system = _TASK_SYSTEM_PROMPT
    if system:
        out.append({"role": "system", "content": system})

    user = str(archive.get("user_prompt") or "")
    if user:
        out.append({"role": "user", "content": user})

    steps, _screening = _screened_task_steps(archive, refined=refined)
    for seq, step in enumerate(steps):
        text = _rationale_text(step.rationale).strip() or None
        out.append({
            "role": "assistant",
            "content": text,
            "tool_calls": [{
                "id": f"call_{seq}",
                "type": "function",
                "function": {
                    "name": str(step.action.get("tool") or ""),
                    # 落盘是 JSON 字符串，不是 dict（方案 23 R3-4）
                    "arguments": _arguments_json(step.action),
                },
            }],
        })
        # 缺观察也显式发占位回执（同 P3 第 8 条）。
        out.append({
            "role": "tool",
            "tool_call_id": f"call_{seq}",
            "content": step.observation,
        })

    closing_text = _closing_text(archive)
    if closing_text is not None and steps:
        out.append({"role": "assistant", "content": closing_text})
    # **不返回 tools**——``to_task_openai_messages`` 只生成 messages 段，
    # 顶层 ``{"trajectory_id", "messages", "tools"}`` 由 :func:`to_task_meta`
    # 同款的包装在落盘处组装。
    return out


# ── meta 视图 ──────────────────────────────────────────────────────


def _aggregate_unit_outcomes(
    archive: Mapping[str, Any],
) -> list[dict[str, Any]]:
    """按 visits 顺序把 outcomes 配到 site-N unit 上（per-Run 聚合）。

    ⚠️ **2026-10-30 · 方案 23：配对规则换了**。

    旧口径**按位置**取（``visit[i]`` → ``outcomes[i]``），理由是
    「执行器的 to_json 已经按顺序对齐了」。那个前提**不成立**：
    ``outcomes[]`` 里混着**孤儿记账**（``_drop_site_homepages`` 造的
    ``not_play_site``）、**候选阶段记账**（每个候选一条 ``unresolved``），
    与 ``visits[]`` **不是一一对应**。实测 T 的按位置配对与 P2 的 URL
    配对在 **109/127 份真实存档（86%）** 上给出**不同**的结论，
    其中一份把**成功报成了失败**。

    现在统一到 :mod:`trajectory_pipeline.outcomerules`：**候选 → 落地 →
    播放页**逐档试，每档匹 ``site_url`` **或** ``url``，**同档取末条**
    （末条才是这个站点的最终结论）。口径在**单一真源**里，
    T 与 P2 与评估侧读的是同一份。

    没 outcome 时记 ``outcome=None``（缺件标记）——**不是**成功：
    成功是 ``branch is None``，缺件是压根没配到。
    """
    out: list[dict[str, Any]] = []
    outcomes = list(archive.get("outcomes") or ())
    blocked = _excluded_unit_map(archive)
    for i, visit in enumerate(archive.get("visits") or (), 1):
        if f"site-{i}" in blocked:
            # 访问级剔除（R4-2）：这条 unit 不在训练集里，outcome 也不能
            # 参与任务级聚合——否则分类结论仍然指向被排除平台。
            continue
        player_obs = visit.get("player_obs")
        player_url = (
            str(player_obs.get("url") or "")
            if isinstance(player_obs, Mapping)
            else ""
        )
        outcome = outcomerules.resolve(
            outcomes,
            str(visit.get("url") or ""),
            str(visit.get("landed_url") or ""),
            player_url,
        )
        if isinstance(outcome, Mapping):
            out.append({
                "unit": f"site-{i}",
                "matched": True,
                "branch": outcome.get("branch"),
                "branch_label": outcome.get("branch_label"),
                "evidence": str(outcome.get("evidence") or ""),
                "site_url": str(outcome.get("site_url") or ""),
                "play_page_url": str(outcome.get("play_page_url") or ""),
            })
        else:
            out.append({
                "unit": f"site-{i}",
                # **没配到 outcome**（缺件）——不是成功。``branch=None``
                # 在「配到了」的前提下才是成功；这里明确标出来，
                # 下游（``gate_breakdown``）才不会把缺件读成「过了」。
                "matched": False,
                "branch": None,
                "branch_label": None,
                "evidence": "",
                "site_url": str(visit.get("url") or ""),
                "play_page_url": "",
            })
    return out


def _task_sample_ids(archive: Mapping[str, Any]) -> list[str]:
    """对应 P3 ``sample_id`` 列表（便于从 T 反查 P3）。

    ⚠️ **2026-10-30 · 方案 23**：以前是「每个决策单元一个 id」
    （``<task_id>__<run_id>__search`` / ``__site-N``），现在**一个任务
    一条轨迹**，P3 的 ``sample_id`` 就是 ``<task_id>__<run_id>``——
    与 P1 存档名、与本视图的 ``trajectory_id`` **逐字相同**。

    仍返回**列表**而不是字符串：键名 ``unit_sample_ids`` 已被契约文档
    与下游读档脚本引用，改键名要同步一串地方；而「列表里只有一项」
    与「这项就是这条轨迹」是同一件事。
    """
    task_id = str(archive.get("task_id") or "")
    run_id = str(archive.get("run_id") or "")
    if not task_id or not run_id:
        return []
    return [f"{task_id}__{run_id}"]


#: T 的 ``degraded_from`` 词表。**与 P2 :func:`assembler.schema.split_archive`
#: 的 per-unit 词表同款**（契约文档 T 第 261 行），只是 T 取整条任务的并集。
#: T 不 import schema（纪律），词表在这里自己声明；同步由契约测试盯住。
_TASK_DEGRADED_VOCAB: tuple[str, ...] = (
    "user_prompt",
    "run_config",
    "steps",
    "no_body",
    "body_preview_only",
    "outcome_missing",
    "success_branch_mismatch",
)


def _body_missing_of(obs: Any) -> str | None:
    """这条观察缺了什么正文；不缺 / 没有这条观察返回 ``None``。

    与 P2 :func:`assembler.schema._obs_missing` 同款：``body_text`` 在就不缺；
    只有 400 字符的 ``body_preview`` 记 ``body_preview_only``（**不是**
    ``no_body``——「正文被截短」与「没有正文」对下游影响不同）；都没有
    记 ``no_body``。``obs`` 为空（没采到这条快照）**不记**：那是
    ``missing_observation_count`` 的账，不是「有观察但正文缺」。
    """
    if not obs or not isinstance(obs, Mapping):
        return None
    body = obs.get("body_text")
    if isinstance(body, str) and body:
        return None
    legacy = obs.get("body_preview")
    if isinstance(legacy, str) and legacy:
        return "body_preview_only"
    return "no_body"


def _outcome_for_visit(
    visit: Mapping[str, Any], outcomes: list[Any]
) -> Mapping[str, Any] | None:
    """按 **候选 url → 落地 url → 播放页 url** 找 visit 对应的 outcome。

    与 P2 :func:`assembler.schema._outcome_for` 的同一条匹配顺序
    （INC59：成功换址访问的 outcome 记在播放页 url 上）。T 不 import
    schema，这里重写一遍；顺序错了会静默把成功访问判成 ``outcome_missing``。
    """
    player_obs = visit.get("player_obs")
    player_url = (
        str(player_obs.get("url") or "") if isinstance(player_obs, Mapping) else ""
    )
    wanted = (
        str(visit.get("url") or ""),
        str(visit.get("landed_url") or ""),
        player_url,
    )
    for want in wanted:
        if not want:
            continue
        for outcome in outcomes:
            if isinstance(outcome, Mapping) and outcome.get("url") == want:
                return outcome
    return None


def _task_degraded_from(archive: Mapping[str, Any]) -> list[str]:
    """整条任务**缺了什么**——P2 per-unit 缺件标记的并集（方案 23 R2-4）。

    T 视图的 ``degraded_from`` 曾经硬编码为空列表：一份缺 ``user_prompt``
    或缺正文的 P1 渲染出来与全须全尾的一模一样。现在从 P1 事实现算，
    词表见 :data:`_TASK_DEGRADED_VOCAB`，顺序按词表固定（同 P2 的
    ``dict.fromkeys`` 去重口径）。

    **不在这里判的**：``observation_missing``（那条账在
    ``missing_observation_count`` / ``observation_complete`` 上）、
    ``rationale_missing``（独立字段）、采集期 ``degraded``（P1 里
    ``degraded`` 非空是**信任度**问题，不是缺件——P2 也只在闸门消费它）。
    """
    found: set[str] = set()
    if not archive.get("user_prompt"):
        found.add("user_prompt")
    if not archive.get("run_config"):
        found.add("run_config")

    if not list(archive.get("steps") or ()):
        found.add("steps")

    miss = _body_missing_of(archive.get("search_observation"))
    if miss:
        found.add(miss)

    outcomes = list(archive.get("outcomes") or ())
    blocked = _excluded_unit_map(archive)
    for i, visit in enumerate(archive.get("visits") or (), 1):
        if f"site-{i}" in blocked:
            # 被访问级剔除的 unit 不参与缺件账：它的缺陷不会进训练集，
            # 记进来只会让「这条轨迹还缺什么」指向一段不存在的语料。
            continue
        if not isinstance(visit, Mapping):
            continue
        if not list(visit.get("steps") or ()):
            found.add("steps")
        for key in ("site_obs", "player_obs"):
            miss = _body_missing_of(visit.get(key))
            if miss:
                found.add(miss)
        outcome = _outcome_for_visit(visit, outcomes)
        if outcome is None:
            found.add("outcome_missing")
        elif bool(visit.get("success")) != (outcome.get("branch") is None):
            # 「成功」只有一个来源说了算；两个来源对不上时标记，不挑一个
            # （挑一个就是在猜，而这条样本进不进训练集全看它——P2 同款）。
            found.add("success_branch_mismatch")

    return [k for k in _TASK_DEGRADED_VOCAB if k in found]


def to_task_meta(
    archive: Mapping[str, Any],
    *,
    source_archive: str = "",
    refined: Mapping[str, Sequence[str]] | None = None,
) -> dict[str, Any]:
    """元数据（per-Run）。训练代码不看它，人工复核与批次筛选看它。

    四组：来源 / 单元级汇总 / 可用性 / 可回放性自查。详见契约文档
    :mod:`trajectory_pipeline.docs.contracts.T-task-view`。

    Args:
        archive: P1 JSON dict。
        source_archive: 据以生成的 P1 文件名（不含目录）。**由调用方注入**
            ——archive dict 里没有自己的文件名，猜不出来；批量路径传
            ``path.name``，单文件路径传 ``p.name``，直接调本函数时为 ``""``
            （契约允许空串，「不知道」不冒充一个文件名）。
        refined: **精炼旁挂索引**（方案 25）。必须与消息两段**同款传入**
            ——它会改观察文本，因而改「相邻同动作 + 观察逐字相同」的判定；
            不传同一份的话，meta 报的「剔了哪几步」与消息里实际少掉的步
            对不上（报表与语料各说各话）。
    """
    task_id = str(archive.get("task_id") or "")
    run_id = str(archive.get("run_id") or "")
    trajectory_id = f"{task_id}__{run_id}" if task_id and run_id else ""
    title = str(archive.get("title") or "")

    units = _flatten_units(archive)
    visits = list(archive.get("visits") or ())

    # 全局动作 / 观察 / rationale / 缺失观察 / infrastructure
    all_actions: list[Mapping[str, Any]] = []
    all_observations: list[Any] = []
    all_rationales: list[Mapping[str, Any]] = []
    infra_tools: set[str] = set()
    missing_obs_count = 0
    for unit in units:
        for a in unit["actions"]:
            all_actions.append(a)
            if a.get("origin") == "infrastructure":
                infra_tools.add(str(a.get("tool") or ""))
        all_observations.extend(unit["observations"])
        all_rationales.extend(r for r in unit["rationales"] if r is not None)

    # 缺失观察占位（``observation_view.render`` 把 obs=None 渲染成
    # ``degraded=["observation_missing"]`` 的占位 dict；这里从 obs 形态
    # 直接判——与 P3 `to_meta` 同款口径）。
    for o in all_observations:
        if isinstance(o, Mapping) and "observation_missing" in (o.get("degraded") or ()):
            missing_obs_count += 1

    # rationale_missing = 整条任务一条 rationale 都没写成
    rationale_missing = len(all_rationales) == 0

    # unit_outcomes / ledger / gate_breakdown
    unit_outcomes = _aggregate_unit_outcomes(archive)
    ledger = dict(archive.get("ledger") or {})

    # ── 任务级结论（裁定 ②：看最后一个结论）────────────────────────
    # 口径在 :mod:`trajectory_pipeline.outcomerules`（单一真源）。
    #
    # ⚠️ **2026-10-30 · 方案 23**：旧口径是「任一 unit 成功 ⇒ 任务成功，
    # 否则取**首个**失败分支」。现在改成**最后一个结论**——
    # 与 P2 的任务级标签、与训练侧消费的那个标签**同一条**。
    # 旧口径会把「中途成功过、后来又失败」的轨迹报成成功。
    #
    # ⚠️ **2026-11-03 · re-base 到 per-Run**：结论取**剔后**的集合
    # （R4-2）——被排除平台的站点不进这条轨迹，它的结论也不该替这条
    # 轨迹定标签。剔除账与 P2 侧**同一份**（:mod:`assembler.excluded`）。
    conclusion = outcomerules.task_conclusion(
        archive, exclude=excluded.excluded_outcome_indices(archive)
    )
    if not conclusion.has_conclusion:
        # 一个站点结论都没有（被反爬拦 / 搜索失败 / 全是路由记账）
        # ——判不出来，不算通过。
        task_gate_status = "not_run"
        overall_branch = outcomerules.UNRESOLVED
    elif conclusion.branch is None:
        task_gate_status = "passed"
        overall_branch = "success"
    else:
        task_gate_status = "failed"
        overall_branch = str(conclusion.branch)

    blocked = _excluded_unit_map(archive)
    # 从**剔后**的 unit_outcomes 出账：``site-i`` 保留原始编号，
    # 被排除平台的 unit 不出现（R4-2）。
    gate_breakdown = [
        {
            "unit": str(o.get("unit") or ""),
            # **没配到 outcome = 缺件**，不是成功——读成「过了」会把
            # 一条没结论的访问点记成通过了。
            "gate": ("not_run" if not o.get("matched")
                     else ("passed" if o.get("branch") is None
                           else "failed")),
            "rationale_missing": rationale_missing,
        }
        for o in unit_outcomes
    ]
    # search 单元单独记一条（gate_breakdown 第一条 = search unit）
    if visits and "search" not in blocked:
        gate_breakdown.insert(0, {
            "unit": "search",
            "gate": "passed" if not rationale_missing else "not_run",
            "rationale_missing": rationale_missing,
        })

    # 「模型被告知但这条轨迹没用过」的工具名。
    #
    # ⚠️ T 视图的 tools 来自 catalog（T 不读 P2），所以这里报的是
    # **catalog 当前**的可见集。信息不显眼时，读 T 的人会把**能力声明**
    # 当成**已验证的动作**——看到 11 个工具就以为这批数据验证过 11 种
    # 能力，而实际只有动作流里出现过的那几个被真实运行验证过。
    used_tools = {str(a.get("tool") or "") for a in all_actions}
    visible_tools = [t["function"]["name"] for t in task_tools(_surface_of(archive))]
    tools_unused = sorted(set(visible_tools) - used_tools)

    # rationale_steps（逐条列出来，step_index 取**全局**重编）
    # 全局重编 = search unit 的 actions 全步先编（按 unit 内 i），然后
    # site-1 的 actions 续编，依此类推；infrastructure 步**占号**——
    # 行为与 P3 那边一致（messages 里 infrastructure 步整步跳，
    # tool_call_id 序号仍稠密：跳的是消息流不是 step 编号）。
    rationale_steps: list[dict[str, Any]] = []
    seq_index = 0
    for unit in units:
        for r in unit["rationales"]:
            if r is None:
                seq_index += 1
                continue
            if isinstance(r, Mapping):
                rationale_steps.append({
                    "step_index": seq_index,
                    "status": str(r.get("status") or ""),
                })
            seq_index += 1

    # 训练筛选的账（方案 23 R3-1 / R3-2）：与两段投影共用同一份筛选
    # 结果（:func:`_screened_task_steps`），所以 meta 报的「剔了哪几步」
    # 就是两段消息里实际少掉的步。
    _, screening = _screened_task_steps(archive, refined=refined)

    return {
        "trajectory_id": trajectory_id,
        "task_id": task_id,
        "run_id": run_id,
        "title": title,
        "source_archive": source_archive,
        "schema_version": "task-view.v1",
        # 工具面（2026-10-20 方案 15 §7 R1）：这份轨迹的训练动作空间属于
        # 哪个方言。同名工具在两个面里参数不同，面错了就是声明与调用对不上。
        "tool_surface": _surface_of(archive),
        # 单元级汇总（T 独有）
        "unit_count": len(units),  # search + 未被剔除的 sites
        "unit_sample_ids": _task_sample_ids(archive),
        # 访问级剔除明细（方案 23 A10 / R4-2）：被剔的 unit 不在
        # ``messages`` / ``unit_sample_ids`` 里，但必须留下账——
        # 「样本为什么少了一条」要能从 T 自己看出来。
        "excluded_units": [
            {"unit": unit, "platform": platform}
            for unit, platform in blocked.items()
        ],
        "unit_outcomes": unit_outcomes,
        "ledger_summary": ledger,
        "task_gate": {"status": task_gate_status, "reasons": []},
        "gate_breakdown": gate_breakdown,
        # 可用性 / 结论
        "gate": {"status": task_gate_status, "reasons": []},
        "rationale_missing": rationale_missing,
        # 整条任务缺件 = 各 unit 缺件的并集（与 P2 词表同款；方案 23 R2-4）
        "degraded_from": _task_degraded_from(archive),
        "overall_branch": overall_branch,
        # 可回放性自查
        "step_count": len(all_actions),
        "observation_count": len(all_observations),
        "missing_observation_count": missing_obs_count,
        "infrastructure_actions": sorted(infra_tools),
        # 能力声明 vs 已验证动作：这条轨迹没用到的模型可见工具
        "tools_declared": visible_tools,
        "tools_unused": tools_unused,
        "observation_complete": len(all_observations) >= len(all_actions),
        "closing_summary": _closing_text(archive) is not None
        and any(a.get("origin") != "infrastructure" for a in all_actions),
        # 训练筛选（方案 23 R3-1 / R3-2）。``step_index`` 与
        # ``rationale_steps`` 同一坐标系（全局原始序号，infrastructure
        # 占号）；``repeat_actions`` 只标留在训练集里的步。
        "deduped_step_count": len(screening.deduped),
        "deduped_steps": list(screening.deduped),
        "repeat_step_count": len(screening.repeats),
        "repeat_actions": [dict(r) for r in screening.repeats],
        "rationale_steps": rationale_steps,
    }


# ── 落盘包装 ────────────────────────────────────────────────────────


class TaskViewRenderResult:
    """一次 :func:`build_task_view` 的结果。"""

    __slots__ = ("trajectory_id", "written", "skipped", "legacy_skipped",
                 "not_produced")

    def __init__(self, trajectory_id: str, written: int, skipped: tuple[str, ...],
                 legacy_skipped: int = 0, not_produced: bool = False):
        self.trajectory_id = trajectory_id
        self.written = written
        self.skipped = skipped
        #: **旧面默认不重渲染**（owner 裁定 2026-11-10）而没落盘的条数。
        #: 单份存档 ⇒ 0 或 1。单独计数是为了让调用方能把「旧面没渲染」
        #: 与「已存在／写失败」分开处置——两者的补救命令不同。
        self.legacy_skipped = legacy_skipped
        #: **本轮不产出**（被口径挡住：判不出 / 旧面）⇒ **不在本轮名单里**
        #: （H-33 清点交付目录的依据）。
        #:
        #: ⚠️ **只有「已知判它不产出」才置 True**——读档失败、投影不一致、
        #: 写盘失败都**不算**：那是「不知道」，而「不知道」不许当成「坏」
        #: （与判分不可信用「显式 False」同一口径）。
        self.not_produced = not_produced

    @property
    def ok(self) -> bool:
        return self.skipped == ()

    def __repr__(self) -> str:  # pragma: no cover - 调试用
        return (
            f"TaskViewRenderResult(trajectory_id={self.trajectory_id!r}, "
            f"written={self.written}, skipped={self.skipped!r})"
        )


def build_task_view(
    archive: Mapping[str, Any],
    *,
    root: str | Path | None = None,
    overwrite: bool = False,
    source_archive: str = "",
    include_legacy: bool = False,
    refined: Mapping[str, Sequence[str]] | None = None,
) -> TaskViewRenderResult:
    """把一份 P1 JSON dict 渲染成 T 视图 dict + 落盘到 storage。

    落盘逻辑收拢在 :func:`trajectory_pipeline.storage.task_views.write_task_view`——
    本函数只做渲染。``overwrite=False`` 是默认：重建批不该把别人刚标好的
    视图抹掉；批量重建时显式传 True（CLI ``--overwrite``）。

    Args:
        archive: P1 JSON dict（由 ``json.loads(path.read_text())`` 直接读
            出来——**不走 ``assembler.schema.read_archive``**，那是 P2 切条
            用的，不属于 T 渲染语义）。
        root: 落盘根；``None`` 时取 :func:`storage.task_views_dir` 的默认根
            （``trajectory_pipeline/output/pipeline/task_views/``）。批量整批
            重建时传 ``tmp_path`` / 别的根,与 P3 :func:`assembler.views.build_views`
            同款参数约定。
        overwrite: 已存在时是否覆盖。
        source_archive: 据以生成的 P1 文件名（不含目录），写进
            ``meta.source_archive``。批量/单文件路径传 ``path.name``；
            默认空串（「不知道」不冒充文件名）。
        include_legacy: **旧面也渲染**（默认 False）。旧面是只读存量
            （owner 裁定 2026-11-10）：默认**不重渲染**——否则清过的
            legacy 池会自己长回来。按需重建旧批次时显式传 True。
        refined: **精炼旁挂索引**（方案 25）——``{正文内容哈希: 保留的行}``。
            有它时观察正文按模型选出的行重建（保真口径与规则裁剪同款）。
            **三处同款传**（messages / openai.messages / meta），否则两段
            投影与 meta 的训练筛选账会分叉。

    Returns:
        :class:`TaskViewRenderResult`——``written`` 1 表示落盘成功，
        ``skipped`` 元组带原因（空 trajectory_id / 缺 payload 键 / 写盘失败）。
    """
    # 延迟 import storage ——任务视图模块不应该走 import 边
    from trajectory_pipeline.storage.task_views import write_task_view

    task_id = str(archive.get("task_id") or "")
    run_id = str(archive.get("run_id") or "")
    if not task_id or not run_id:
        return TaskViewRenderResult(
            trajectory_id="",
            written=0,
            skipped=("<no-trajectory_id>: task_id / run_id 空",),
        )
    # ⚠️ **判不出 ⇒ 不出训练视图**（2026-11-11，H-31 第 4 层 / H-33 §3.4）。
    #
    # 判据走 :func:`assembler.schema.sample_blocked_reason`——**与 P2 同一份**
    # （两边都要挡：P2 出 P2 样本、T 出训练视图，漏一边就有一边进池）。
    # 判据用「显式 False」：历史存档没有这个键 ⇒ 缺键 = 不知道 ≠ 不可信
    # （「并池是写入时的动作」，不回填历史存档）。
    #
    # ⚠️ **T 侧原先只挡了「判分不可信」那一档**，漏了「搜索阶段没进决策」
    # ⇒ 实测 T 多渲染 **53 份**退化空壳（P2 侧不出它们、T 侧出了）——
    # 判据分叉的代价是**交付目录里多出训练集里没有的东西**，
    # 而报告上零处异常（2026-11-11 全量合成实测）。
    blocked = sample_blocked_reason(archive)
    if blocked:
        return TaskViewRenderResult(
            trajectory_id=f"{task_id}__{run_id}",
            written=0,
            skipped=(f"{task_id}__{run_id} ({blocked})",),
            not_produced=True,
        )

    trajectory_id = f"{task_id}__{run_id}"
    # 分池（方案 23 R3-3）：交付面落 ``task_views/``，旧面落
    # ``legacy/<surface>/task_views/``。面从源存档的 ``driver`` 折算
    # （:func:`_surface_of`）——认不出的 driver 直接抛，不回落。
    surface = _surface_of(archive)
    # 旧面默认**不重渲染**（owner 裁定 2026-11-10）：旧面是只读存量，
    # 不自己长回来。要重建旧批次显式传 ``include_legacy=True``。
    if not pool.should_render(surface, include_legacy=include_legacy):
        return TaskViewRenderResult(
            trajectory_id=trajectory_id,
            written=0,
            skipped=(f"{trajectory_id} (旧面 {surface}：{pool.LEGACY_SKIP_REASON})",),
            legacy_skipped=1,
            not_produced=True,
        )
    target_root = pool.pool_root(storage_task_views_dir(root).parent, surface)
    messages = to_task_messages(archive, refined=refined)
    openai_msgs = to_task_openai_messages(archive, refined=refined)
    # 工具声明与两段 messages 必须来自**同一个面**（Q-A1）：声明按面取，
    # 与 messages 里的真实调用参数逐条核对；面错配不许落盘。
    tools = task_tools(surface)
    # 两段投影一致才落盘（方案 23 R5-2 / O-5）：不一致的视图不该进交付集
    # ——训练侧与 LS 侧会看到两个故事，而报表上看不出异常。fail-closed：
    # 不写半份，skipped 带原因（CLI 会打印）。
    problems = projection_problems(messages, openai_msgs)
    problems += tool_declaration_problems(tools, openai_msgs)
    if problems:
        return TaskViewRenderResult(
            trajectory_id=trajectory_id,
            written=0,
            skipped=("投影不一致: " + "；".join(problems[:3]),),
        )
    meta = to_task_meta(archive, source_archive=source_archive, refined=refined)

    payload = {
        "trajectory_id": trajectory_id,
        "task_id": task_id,
        "run_id": run_id,
        "title": str(archive.get("title") or ""),
        "schema_version": "task-view.v1",
        "messages": messages,
        "openai": {
            "trajectory_id": trajectory_id,
            "messages": openai_msgs,
            "tools": tools,
        },
        "meta": meta,
    }
    rep = write_task_view(
        payload, trajectory_id, root=target_root, overwrite=overwrite
    )
    return TaskViewRenderResult(
        trajectory_id=rep.trajectory_id,
        written=rep.written,
        skipped=rep.skipped,
    )


# ── 批量门面（CLI 整批用） ─────────────────────────────────────────


class TaskViewBatchReport:
    """一次 :func:`build_all_task_views` 的整批结果。

    对称 :class:`trajectory_pipeline.assembler.views.ViewBuildReport`——
    一次返回整批 ``TaskViewRenderResult``，外加 ``total_written`` /
    ``total_skipped`` 计数与 ``target_dir`` 字符串（给 CLI 打印落点用）。
    """

    __slots__ = ("results", "total_written", "total_skipped", "target_dir",
                 "total_legacy_skipped", "sweep")

    def __init__(
        self,
        results: tuple[TaskViewRenderResult, ...],
        total_written: int,
        total_skipped: int,
        target_dir: str,
        total_legacy_skipped: int = 0,
        sweep: pool.StaleSweep | None = None,
    ):
        self.results = results
        self.total_written = total_written
        self.total_skipped = total_skipped
        self.target_dir = target_dir
        #: 旧面默认不重渲染（owner 裁定 2026-11-10）而没落盘的**总条数**
        #: ——报表上的独立一行，不与「已存在／写失败」混算。
        self.total_legacy_skipped = total_legacy_skipped
        #: **交付目录清点结果**（H-33）。``None`` = 没跑清点；
        #: 非 ``None`` 时 ``reason`` 说的是「没清点」的原因。
        self.sweep = sweep

    def __repr__(self) -> str:  # pragma: no cover - 调试用
        return (
            f"TaskViewBatchReport(results={len(self.results)}, "
            f"total_written={self.total_written}, "
            f"total_skipped={self.total_skipped}, "
            f"total_legacy_skipped={self.total_legacy_skipped}, "
            f"sweep={'None' if self.sweep is None else len(self.sweep.moved)}, "
            f"target_dir={self.target_dir!r})"
        )


def build_all_task_views(
    source_root: str | Path,
    *,
    target_root: str | Path | None = None,
    archive_filter: tuple[str, ...] | None = None,
    overwrite: bool = False,
    backfill_from_filename: bool = True,
    include_legacy: bool = False,
    prune_stale: bool = False,
    refined_dir: str | Path | None = None,
) -> TaskViewBatchReport:
    """读一整个 P1 目录，逐份渲染 T 视图并落盘。

    与 :func:`assembler.builder.build_all` / :func:`assembler.views.build_views_from_dir`
    同款纪律：

    - **坏档不打断整批**——单份 P1 读不出 / 缺 ``task_id`` / ``run_id``
      只进该份的 ``skipped``，其余继续；
    - **默认不覆盖**——已存在的 T 视图不重写（与 ``cli build --overwrite``
      同款），批量重建时显式传 True；
    - **路径不在这里拼字面量**——读侧取 ``source_root`` 直接，``None`` 时
      调方负责注入；写侧取 :func:`storage.task_views_dir` 的权威
      ``target_root``（``None`` 时取默认根）。

    Args:
        source_root: 读 P1 存档的目录（**不**是单文件——单文件请用
            :func:`build_task_view`）。目录下应平铺 ``<task_id>__<run_id>.json``。
        target_root: 写 T 视图的根；``None`` 时取 :func:`storage.task_views_dir`
            的默认根（``trajectory_pipeline/output/pipeline/task_views/``）。
        archive_filter: 只处理这些 archive 文件名（不含目录）。``None`` =
            全部（按文件名排序）。**探测取证**（不含 ``__``）与
            ``.reviewed.json`` 不自动跳——由调用方在 ``archive_filter`` 里
            显式收敛。
        overwrite: 目标 T 视图已存在时是否覆盖。
        backfill_from_filename: **过渡期回填**——``archive.write`` 历史上
            不向 P1 JSON 顶层写 ``run_id`` / ``batch_id``，文件名有但 JSON
            没有。2026-10-19 增量六十一起 ``archive.write`` 已修，新 P1
            自带字段；存量 P1 仍缺。本参数默认 True：从 ``<task_id>__<run>.json``
            文件名回填 ``run_id`` 到 archive 内存 dict（**不写回盘**——回填
            只在 T 渲染期生效），记 WARNING 级「过渡期缓兵之计」。明确要
            走纪律路径（缺字段就 skipped）时显式传 False。
            新 P1 自带后这条回填无操作（不覆盖现成值）。
        include_legacy: **旧面也渲染**（默认 False）。同
            :func:`build_task_view`——旧面是只读存量，默认不重渲染
            （owner 裁定 2026-11-10）。**迁移**（把交付目录里已存在的
            旧面文件挪进 legacy）照旧做。
        prune_stale: **按本轮名单清点交付目录**（H-33）：``task_views/``
            里**本轮没产出**的旧视图移进 ``quarantine/task_views/``。
            ⚠️ **默认关**：``archive_filter`` 收窄时「本轮名单」只是子集，
            拿它清点会把**别的批次**的视图当残留移走⇒ 只有**整批重建**
            （名单 = 全部存档）才该开。
        refined_dir: **精炼旁挂目录**（方案 25）。``None`` = 不做精炼
            （默认，行为与今天逐字一致）；给了就按存档名逐份读旁挂，
            读不到 = 那份没精炼过 ⇒ 那份走规则裁剪。

    Returns:
        :class:`TaskViewBatchReport`——一次返回整批 ``TaskViewRenderResult``。
    """
    import logging

    src = Path(source_root)
    out_dir = storage_task_views_dir(target_root)
    out_dir.mkdir(parents=True, exist_ok=True)
    # 分池（方案 23 R3-3）：先把**已存在**的旧面任务视图挪出交付目录。
    # 放在构建之前，``overwrite=False`` 的存在性检查才看对目录。
    migration = pool.migrate_task_views(out_dir)

    allowed = set(archive_filter) if archive_filter is not None else None
    log = logging.getLogger(__name__)

    results: list[TaskViewRenderResult] = []
    total_written = 0
    total_skipped = 0
    total_legacy_skipped = 0

    for path in sorted(src.glob("*.json")):
        if "__" not in path.stem:
            # 探测取证文件 / 杂项不带 ``__`` 的——跳过，不写日志噪声。
            continue
        if allowed is not None and path.name not in allowed:
            continue
        try:
            archive = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            results.append(
                TaskViewRenderResult(
                    trajectory_id=path.stem,
                    written=0,
                    skipped=(f"{path.name}: 读档失败 {type(exc).__name__}: {exc}",),
                )
            )
            total_skipped += 1
            continue
        # ── 过渡期回填：从文件名派生 run_id（不写盘）──
        if backfill_from_filename and isinstance(archive, dict):
            existing_run_id = str(archive.get("run_id") or "")
            if not existing_run_id:
                # 文件名形如 ``T001__b3a3548a`` → run_id = ``b3a3548a``
                stem = path.stem
                # ⚠️ ``.reviewed`` 是**文件身份**不是 run 身份：人工复核档
                # 叫 ``T001__b3a3548a.reviewed.json``，派生出的 run_id 必须
                # 是 ``b3a3548a``。不剥的话同一 run 会得到两个 trajectory_id，
                # 而 ``<run>.reviewed`` 那个对不上任何 ``trajectory_id``
                # （scout / LS 上传按 ``<task_id>__<run_id>.json`` 查）——
                # 死文件，且真正被查到的那份可能来自**未复核**的原档。
                # 与 :func:`assembler.schema.run_id_of` 同一条口径。
                if stem.endswith(".reviewed"):
                    stem = stem[: -len(".reviewed")]
                if "__" in stem:
                    derived_run_id = stem.split("__", 1)[1].strip()
                    if derived_run_id:
                        archive["run_id"] = derived_run_id
                        log.warning(
                            "build_all_task_views 过渡期回填: %s run_id 从文件名派生=%r "
                            "(archive.write 增量六十一修复后新 P1 不再缺;记录留痕)",
                            path.name, derived_run_id,
                        )
        res = build_task_view(
            archive,
            root=target_root,
            overwrite=overwrite,
            source_archive=path.name,
            include_legacy=include_legacy,
            refined=refine_sidecar.load_index(refined_dir, path.name),
        )
        results.append(res)
        total_written += res.written
        total_skipped += len(res.skipped)
        total_legacy_skipped += res.legacy_skipped
    # ── 按本轮名单清点交付目录（H-33）────────────────────────────────
    sweep: pool.StaleSweep | None = None
    if prune_stale:
        sweep = _sweep_task_views(out_dir, src, results, migration.unclassified)

    pool.record_pool(
        out_dir,
        moved=migration.moved,
        unclassified=migration.unclassified,
        legacy_skipped=total_legacy_skipped,
        quarantined=sweep.moved if sweep is not None else (),
    )
    return TaskViewBatchReport(
        tuple(results), total_written, total_skipped, str(out_dir),
        total_legacy_skipped, sweep,
    )


def _sweep_task_views(
    out_dir: Path,
    src: Path,
    results: Sequence[TaskViewRenderResult],
    unclassified: Sequence[str] = (),
) -> pool.StaleSweep:
    """清点 T 交付目录（H-33）。

    ⚠️ **名单 = 本轮该产出的 trajectory_id ∪ P2 目录里的 id**：

    - **前半 = T 自己的口径**（本轮渲染出来、且没被口径挡住的）；
    - **后半 = 下限**——P2 说在交付集里的，不许被 T 清掉
      （P2 是交付集的源头，T 是它的读侧：LS 上传 / 人工复核都按 T 找）。

    ⚠️ **取「并集」而不是「P2 目录」当名单**：P2 还没重建时（P2 是上一批的、
    T 是本批的），拿 P2 切名单会把**本轮刚渲出来的合法 T 视图**
    当残留搬走 ⇒ LS 上传会缺轨迹（候选来自 P2，视图却不齐）。

    ⚠️ **源目录不存在 ⇒ 不清点**：那是**路径指错了**，不是「本轮一条都没有」。
    两者必须分得开——把整目录搬开来「回应」一个不存在的目录 = 毁数据。
    """
    if not src.is_dir():
        return pool.StaleSweep(
            kind="task_views",
            reason=f"P1 源目录不存在（{src}）⇒ 路径指错，不清点",
        )
    roster = {
        r.trajectory_id for r in results
        if r.trajectory_id and not r.not_produced
    }
    # 下限：P2 目录里的 id（P2 是交付集的源头，它说在的就不许被清掉）
    delivery_samples = out_dir.parent / "samples"
    if delivery_samples.is_dir():
        roster |= {p.stem for p in delivery_samples.glob("*.json")}
    return pool.quarantine_offroster_task_views(
        out_dir, roster=roster, skip_names=unclassified,
    )