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

3. **不 import assembler.schema 任何名字**。``tests/contract/test_t_contract.py::TestNoP2Import``
   用 AST 强制——T 是 P1 的渲染，连同 :data:`SYSTEM_PROMPT` / :data:`TOOL_SPECS`
   都**自己声明**（与 schema.py 字面字符串一致，由契约测试盯住）。这
   与 P3 不同（P3 从 P2 的 ``six.tools`` 拿工具声明；T 从 P1 拿不到，
   只能自己声明）。

4. **P1 observation 是取证态**（full 6000 字符 / 80 元素 / 50 链接），
   经 :func:`~trajectory_pipeline.assembler.observation_view.render`
   裁到训练态再经 :func:`~trajectory_pipeline.assembler.views.render_observation`
   排版——与 P3 同款裁剪口径（2000/40/30），只是 T 这边**自己跑一遍裁剪**，
   而 P3 那边是从 P2 已裁好的形态接过来。

5. **观察 / 动作 / 思考逐步对齐**（同 P3 第 6 条判断）。``infrastructure``
   步整步跳过——消息流里不存在的动作不该占号（与 P3 第 2 条判断同款）。

6. **任务级 summary 收尾**（同 P3 第 7 条判断）：来源 ``outcomes[]`` +
   ``ledger.by_branch``，**不读 unit 级 reflection**（T 层级高于 unit）。
   处置词表 ``_CLOSING_DISPOSITION`` 自行声明，**与
   ``executor.branches.BRANCH_LABELS + {"success"}`` 键集同步由契约测试盯住**
   ——不 import executor，交接面是格式不是 import。

T 落盘由 :func:`trajectory_pipeline.storage.task_views.write_task_view`
接管（与 ``write_p3.write_views`` 同款纪律：原子写、默认不覆盖）。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Mapping

from trajectory_pipeline.assembler import observation_view

#: 路径权威的 re-export：T 渲染端不许自己拼 ``output/pipeline/task_views``
#: 字面量。改存储布局只动 ``trajectory_pipeline.storage.task_views_dir``
#: 一处，这里照转发。由 ``tests/contract/test_t_contract.py::Test产物根``
#: 盯住两侧同一对象。
from trajectory_pipeline.storage import task_views_dir as storage_task_views_dir  # noqa: F401

__all__ = [
    "to_task_messages",
    "to_task_openai_messages",
    "to_task_meta",
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
    "你是一个会帮用户找在线观看资源的助手。\n"
    "你会拿到用户的诉求、可用工具，以及每一步操作后页面返回的观察。\n"
    "按下面的规则行动：\n"
    "1. 只根据观察里出现的东西判断，不要根据网址或片名猜。\n"
    "2. 找不到能看正片的资源时如实说找不到，不要编造链接。\n"
    "3. 每一步只做一个动作。\n"
)


#: T 视图的训练动作空间。**字面字符串**与
#: :data:`trajectory_pipeline.assembler.schema.TOOL_SPECS` 逐字相同——
#: 工具名 / 参数名元组 / instruction 字面一致，由契约测试盯住。
#: 字典形态（不是 ToolSpec dataclass），避免 T 视图依赖 assembler.schema。
#: 可见性按 ``visible_to_model`` 规则复现：``new_tab`` / ``close_tab``
#: 不进工具声明（模型永远不该选它们——会话管理动作）。
_TASK_TOOL_SPECS: tuple[dict[str, Any], ...] = (
    {"name": "goto", "params": ("url",), "instruction": "打开一个网址。"},
    {
        "name": "click",
        "params": (),
        "instruction": "点击页面上一个可交互元素（按元素类型与文字指定）。",
    },
    {"name": "new_tab", "params": ("url",), "instruction": "在新标签页打开网址。"},
    {"name": "fill", "params": ("text",), "instruction": "往输入框里填文本（如把片名填进站内搜索框）。"},
    {"name": "press", "params": ("key",), "instruction": "按一个键（如回车，提交站内搜索）。"},
    {"name": "close_tab", "params": (), "instruction": "关闭当前标签页。"},
)


# ── 工具声明 ───────────────────────────────────────────────────────


def _task_tool(spec: Mapping[str, Any]) -> dict[str, Any]:
    """训练动作空间的一条 → function-calling 的 ``tools[]`` 元素。

    与 :func:`~trajectory_pipeline.assembler.views._openai_tool` 同款形状
    （P3 第 3 条判断同款），刻意**重新实现**而不 import——交接面是格式
    不是对象。ToolSpec 的可见性 ``visible_to_model`` 在调用方在
    :func:`task_tools` 里按名字过滤。
    """
    params = spec.get("params") or ()
    props = {str(p): {"type": "string"} for p in params}
    return {
        "type": "function",
        "function": {
            "name": str(spec.get("name") or ""),
            "description": str(spec.get("instruction") or ""),
            "parameters": {
                "type": "object",
                "properties": props,
                "required": list(params),
            },
        },
    }


def task_tools() -> list[dict[str, Any]]:
    """T 视图的工具声明（不进 ``new_tab`` / ``close_tab``）。

    与 :func:`~trajectory_pipeline.assembler.views._visible_tools` 同款纪律：
    可见性按名字判（``visible_to_model`` 是 ToolSpec 方法，T 这里按规则
    复现——契约测试盯住两边同步）。
    """
    out: list[dict[str, Any]] = []
    for spec in _TASK_TOOL_SPECS:
        if str(spec.get("name") or "") in ("new_tab", "close_tab"):
            continue
        out.append(_task_tool(spec))
    return out


# ── P1 → units 拆条 ────────────────────────────────────────────────


def _flatten_units(archive: Mapping[str, Any]) -> list[dict[str, Any]]:
    """把 P1 拆成 search + N 个 site 的 unit 列表。

    每个 unit 含 ``unit`` / ``actions`` / ``observations`` / ``rationales``
    三段，每段都是**等长**数组（与 P2 的 ``actions`` / ``observations`` /
    ``rationales`` 同款对齐）。
    """
    out: list[dict[str, Any]] = []

    # ── 单元 1：search ──
    search_steps = list(archive.get("steps") or ())
    out.append({
        "unit": "search",
        "actions": [s.get("action") or {} for s in search_steps],
        "observations": [s.get("observation") for s in search_steps],
        "rationales": [s.get("rationale") for s in search_steps],
    })

    # ── 单元 2..N：sites ──
    for i, visit in enumerate(archive.get("visits") or (), 1):
        site_steps = list(visit.get("steps") or ())
        out.append({
            "unit": f"site-{i}",
            "actions": [s.get("action") or {} for s in site_steps],
            "observations": [s.get("observation") for s in site_steps],
            "rationales": [s.get("rationale") for s in site_steps],
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

    body = str(obs.get("body_text") or "")
    parts.append(f"正文：\n{body}" if body else "正文：（空）")

    elems = obs.get("interactive_elements") or []
    if elems:
        lines = [
            f"  - <{e.get('tag', '')}> {e.get('label', '')}".rstrip() for e in elems
        ]
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

    degraded = list(obs.get("degraded") or ())
    if degraded:
        parts.append(f"[采集降级: {'、'.join(str(d) for d in degraded)}]")
    trunc = obs.get("truncated") or {}
    if trunc.get("source") or trunc.get("view"):
        which: list[str] = []
        if trunc.get("source"):
            which.append("上游截断")
        if trunc.get("view"):
            which.append("本次裁剪")
        parts.append(f"[内容不完整: {'、'.join(which)}]")
    return "\n\n".join(parts)


def _render_observation(obs: Mapping[str, Any] | None) -> str:
    """P1 取证态 observation → 训练态文本。

    两段：① 经 :func:`observation_view.render` 把 P1 的 full body_text /
    80 元素 / 50 链接裁到 P3 同款训练态（2000/40/30）；② 再经
    :func:`_render_observation_text` 排版成 ``标题：... 网址：...`` 形态。

    空 obs 走 :func:`observation_view.render` 的 None 路径，输出
    ``degraded=["observation_missing"]`` 的占位 dict，再由
    ``_render_observation_text`` 渲染成 ``（本步没有取到页面内容）``——
    与 P3 第 8 条判断同款。
    """
    train_view = observation_view.render(obs)
    return _render_observation_text(train_view)


# ── 渲染辅助函数（与 views.py 同款，刻意不复用 ——交接面是格式） ──


def _rationale_text(rationale: Any) -> str:
    """一步的思考+结论，渲染成 assistant 消息的**前缀**。

    与 :func:`~trajectory_pipeline.assembler.views._rationale_text` 同款
    行为（缺一条不补空串，``status != "written"`` 返回空串——理由同 P3
    第 6 条判断，编一段占位思考会直接污染 CoT SFT）。
    """
    if not isinstance(rationale, Mapping) or rationale.get("status") != "written":
        return ""
    text = str(rationale.get("text") or "").strip()
    conclusion = str(rationale.get("conclusion") or "").strip()
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
    """动作 → function-calling 的 ``arguments``（与 :func:`views._arguments` 同款）。"""
    args: dict[str, Any] = dict(act.get("params") or {})
    target = act.get("target")
    if isinstance(target, Mapping):
        for k, v in target.items():
            if v not in (None, ""):
                args[str(k)] = v
    return args


# ── 任务级 summary 处置词表（自行声明，不 import executor） ──────────


#: 分支 → 收尾处置话术。**本模块自己声明**，与
#: ``executor.branches.BRANCH_LABELS + {"success"}`` 的键集同步由契约测试
#: 盯住——不 import executor，交接面是格式不是 import（同 P3 同款纪律）。
#: 字面字符串与 :data:`trajectory_pipeline.assembler.views._CLOSING_DISPOSITION`
#: **逐字一致**（由 ``tests/contract/test_t_contract.py::TestClosingDispositionSync` 盯住）
_CLOSING_DISPOSITION: dict[str, str] = {
    "not_play_site": "不是播放站点，排除",
    "unreachable_hard": "导航失败，排除",
    "no_play_control": "页面上没有播放控件，排除",
    "trailer_only": "只有预告片，没有正片资源，排除",
    "trailer_suspect": "疑似预告，判不准，不予记录",
    "component_unverified": "点开后没有可验证的播放组件，排除",
    "player_unverified": "有播放组件但确认不了能不能用，标记未验证，不入正式记录",
    "subscription_wall": "有播放页但需订阅/付费，排除",
    "unresolved": "无法确认能否播放，不予记录",
    "login_required": "登录后可免费观看（无登录态），排除",
    "blocked": "地区限制/验证码/反爬阻断，排除",
    "broken": "内容损坏，排除",
    "partial_free": "完整内容需付费，排除",
    "not_aggregator": "候选不是影视聚合站，排除",
    "aggregator_no_entries": "聚合站页面上没有正式影视站入口，排除",
    "not_article": "候选不是文章/社区讨论页，排除",
    "article_no_links": "文章里没有可引荐的影视站外链，排除",
    "no_site_search": "首页上没有站内搜索框，无法按片名定位，排除",
    "site_search_no_results": "站内搜索没有找到该片的资源，排除",
    "site_search_routed": "经站内搜索找到候选，转入结果页继续核验",
    "login_wall_or_blocked": "登录墙/验证码/地区限制，排除",
}


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


def _closing_text(archive: Mapping[str, Any]) -> str | None:
    """T 视图的任务级 summary。**只派生，不编新事实**。

    与 :func:`~trajectory_pipeline.assembler.views._closing_text` 同款
    形态但**来源不同**——T 从 ``outcomes[]`` + ``ledger.by_branch`` 派生，
    P3 从 unit outcome + unit reflection 派生。

    - **空 trajectory**（零可见模型动作）不编总结；
    - 任一 ``outcomes[i].branch is None`` ⇒ success 处置词；
    - ``outcomes[i].branch is not None`` ⇒ 失败处置词（首个失败的分支名
      进词表，未知分支照实写出分支名——不能静默消失）；
    - 全部 outcomes 为空 ⇒ "无可访问站点" 处置词。

    ⚠️ INC46（2026-10-15）后 T 也**不读 unit 级 reflection**——T 是任务级
    视图，unit 级 reflection 是事后反思，写 unit 级 conclusion 当任务级
    结论会把跨单元的整体判断抹成单点（与 P3 同款教训）。
    """
    units = _flatten_units(archive)
    has_visible_action = any(
        a.get("origin") != "infrastructure"
        for u in units
        for a in u["actions"]
    )
    if not has_visible_action:
        return None

    outcomes = list(archive.get("outcomes") or ())
    total_sites = int((archive.get("ledger") or {}).get("total_sites") or 0)
    if not outcomes and total_sites == 0:
        # 真没访问过任何站点——closing 也不发（search 单元有动作但
        # 检索阶段就被反爬拦了 / 全无候选）。closing 在「有结果要总结」
        # 时才有意义。
        return None
    ledger = archive.get("ledger") or {}

    if not outcomes:
        # 全部 outcomes 为空：要么没访问任何站点，要么都是「正在处理中」
        # （路径 B/C 路由记账）。处置词取自 by_branch 的判断。
        total_sites = ledger.get("total_sites", 0)
        disposition = f"无可访问站点（total_sites={total_sites}）"
    else:
        # 任一 success ⇒ success 处置词
        success = next((o for o in outcomes if o.get("branch") is None), None)
        if success is not None:
            url = str(success.get("play_page_url") or success.get("site_url") or "")
            disposition = (
                f"记录该网址为可播放资源：{url}" if url else "记录为可播放资源"
            )
        else:
            # 取首个失败的分支
            first_failure = next(
                (o for o in outcomes if o.get("branch") is not None), None
            )
            if first_failure is not None:
                branch = str(first_failure.get("branch") or "")
                disposition = _CLOSING_DISPOSITION.get(branch) or (
                    f"不予记录（分支：{branch}）"
                )
            else:
                disposition = "任务未能完成"

    # by_branch 摘要
    by_branch = ledger.get("by_branch") or {}
    if by_branch:
        branch_summary = " · ".join(
            f"{k}={v}" for k, v in sorted(by_branch.items())
        )
        return f"任务总结：处置：{disposition}。分支分布：{branch_summary}。"
    return f"任务总结：处置：{disposition}。"


# ── messages 视图（通用形态） ──────────────────────────────────────


def to_task_messages(archive: Mapping[str, Any]) -> list[dict[str, Any]]:
    """通用 ``{role, content}`` 序列（per-Run 完整对话流）。

    按时间顺序拼：search 单元的 steps[] → 全部 site-N 单元的 steps[] →
    任务级 summary 收尾。

    与 :func:`~trajectory_pipeline.assembler.views.to_messages` 同款
    行为但**输入是 P1**（不是 P2）：动作流从 P1 顶层 ``steps[]`` +
    ``visits[i].steps[]`` 取；观察按 ``step_index`` 全局重编（在拼装过程
    中**自动实现**——拼接时按出现顺序编）。
    """
    msgs: list[dict[str, Any]] = []

    system = _TASK_SYSTEM_PROMPT
    if system:
        msgs.append({"role": "system", "content": system})

    user = str(archive.get("user_prompt") or "")
    if user:
        msgs.append({"role": "user", "content": user})
    # 空 user_prompt 不发空消息（同 P3 第 4 条判断）。

    units = _flatten_units(archive)
    has_visible_action = False
    for unit in units:
        actions = unit["actions"]
        observations = unit["observations"]
        rationales_by_index = {
            int(i): r for i, r in enumerate(unit["rationales"]) if r is not None
        }
        for i, action in enumerate(actions):
            if action.get("origin") == "infrastructure":
                # infrastructure 步整步跳过（同 P3 第 2 条判断）。
                continue
            has_visible_action = True
            obs = observations[i] if i < len(observations) else None
            rationale = rationales_by_index.get(i)
            msgs.append({
                "role": "assistant",
                "content": _rationale_text(rationale) + _action_line(action),
            })
            # 缺观察也显式发占位回执，不留没有回音的动作（同 P3 第 8 条）。
            msgs.append({"role": "user", "content": _render_observation(obs or {})})

    closing = _closing_text(archive)
    if closing is not None and has_visible_action:
        msgs.append({"role": "assistant", "content": closing})
    return msgs


# ── openai 视图（function-calling 形态） ─────────────────────────────


def to_task_openai_messages(
    archive: Mapping[str, Any],
    tools: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """OpenAI function-calling 形态（per-Run）。

    ``tool_call_id`` 由 **可见步序号** 派生（``call_0`` / ``call_1``…），
    不取自 P1 的任何字段——P1 里没有可用的调用 id，而 seq 是**确定**的
    不是猜的。``infrastructure`` 步整步跳过后序号仍**稠密**——
    消息流里不存在的动作不该占号。

    Args:
        archive: P1 JSON dict。
        tools: 工具声明列表；``None`` 时取 :func:`task_tools`（默认按
            ``TOOL_SPECS`` 渲染，进 ``new_tab`` / ``close_tab`` 滤掉）。
    """
    if tools is None:
        tools = task_tools()

    out: list[dict[str, Any]] = []
    system = _TASK_SYSTEM_PROMPT
    if system:
        out.append({"role": "system", "content": system})

    user = str(archive.get("user_prompt") or "")
    if user:
        out.append({"role": "user", "content": user})

    units = _flatten_units(archive)
    seq = 0
    has_visible_action = False
    for unit in units:
        actions = unit["actions"]
        observations = unit["observations"]
        rationales_by_index = {
            int(i): r for i, r in enumerate(unit["rationales"]) if r is not None
        }
        for i, action in enumerate(actions):
            if action.get("origin") == "infrastructure":
                continue
            has_visible_action = True
            obs = observations[i] if i < len(observations) else None
            rationale = rationales_by_index.get(i)
            text = _rationale_text(rationale).strip() or None
            out.append({
                "role": "assistant",
                "content": text,
                "tool_calls": [{
                    "id": f"call_{seq}",
                    "type": "function",
                    "function": {
                        "name": str(action.get("tool") or ""),
                        "arguments": _arguments(action),
                    },
                }],
            })
            # 缺观察也显式发占位回执（同 P3 第 8 条）。
            out.append({
                "role": "tool",
                "tool_call_id": f"call_{seq}",
                "content": _render_observation(obs or {}),
            })
            seq += 1

    closing = _closing_text(archive)
    if closing is not None and has_visible_action:
        out.append({"role": "assistant", "content": closing})
    # **不返回 tools**——``to_task_openai_messages`` 只生成 messages 段，
    # 顶层 ``{"trajectory_id", "messages", "tools"}`` 由 :func:`to_task_meta`
    # 同款的包装在落盘处组装。
    return out


# ── meta 视图 ──────────────────────────────────────────────────────


def _aggregate_unit_outcomes(
    archive: Mapping[str, Any],
) -> list[dict[str, Any]]:
    """按 visits 顺序把 outcomes 配到 site-N unit 上（per-Run 聚合）。

    visits[i] ↔ outcomes[i] 的对齐由 ``_outcome_for`` 决定（站点 URL 三
    字段配对）——这里**只按位置**取（取``visit[i]`` → ``outcomes[i]``），
    因为执行器 (``VisitResult`` + ``RunRecord.ledger``) 的 to_json 已经按
    顺序对齐了；重写匹配会引入新拼接风险。

    没 outcome 时记 ``outcome=None``（缺件标记）。
    """
    out: list[dict[str, Any]] = []
    outcomes = list(archive.get("outcomes") or ())
    for i, visit in enumerate(archive.get("visits") or (), 1):
        outcome = outcomes[i - 1] if i - 1 < len(outcomes) else None
        if isinstance(outcome, Mapping):
            out.append({
                "unit": f"site-{i}",
                "branch": outcome.get("branch"),
                "branch_label": outcome.get("branch_label"),
                "evidence": str(outcome.get("evidence") or ""),
                "site_url": str(outcome.get("site_url") or ""),
                "play_page_url": str(outcome.get("play_page_url") or ""),
            })
        else:
            out.append({
                "unit": f"site-{i}",
                "branch": None,
                "branch_label": None,
                "evidence": "",
                "site_url": str(visit.get("url") or ""),
                "play_page_url": "",
            })
    return out


def _task_sample_ids(archive: Mapping[str, Any]) -> list[str]:
    """按 units 顺序生成对应 P3 ``sample_id`` 列表（便于从 T 反查 P3）。

    不读 P2（纪律）——只根据 P1 顶层切片轴拼出对应的 P3 sample_id 形式
    ``<task_id>__<run_id>__<unit>``，便于人查；P3 文件本身不在 T 时扫。
    """
    task_id = str(archive.get("task_id") or "")
    run_id = str(archive.get("run_id") or "")
    if not task_id or not run_id:
        return []
    return [f"{task_id}__{run_id}__search"] + [
        f"{task_id}__{run_id}__site-{i}"
        for i in range(1, len(list(archive.get("visits") or ())) + 1)
    ]


def to_task_meta(archive: Mapping[str, Any]) -> dict[str, Any]:
    """元数据（per-Run）。训练代码不看它，人工复核与批次筛选看它。

    四组：来源 / 单元级汇总 / 可用性 / 可回放性自查。详见契约文档
    :mod:`trajectory_pipeline.docs.contracts.T-task-view`。
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

    # task_gate: 全部 unit ``gate=passed``（整条任务级）才算 passed
    # 简化为——没访问成功的 unit 也算 failed（与 P3 同款 returner）
    any_branch = next(
        (o.get("branch") for o in unit_outcomes if isinstance(o, Mapping)),
        None,
    )
    if any_branch is None and not unit_outcomes:
        # outcomes 为空（被反爬拦 / 搜索失败）——task_gate 不算通过
        task_gate_status = "not_run"
    elif any(o.get("branch") is None for o in unit_outcomes):
        task_gate_status = "passed"
    else:
        task_gate_status = "failed"

    gate_breakdown = [
        {
            "unit": f"site-{i}",
            "gate": ("passed" if (i - 1 < len(unit_outcomes)
                                   and unit_outcomes[i - 1].get("branch") is None)
                     else "failed"),
            "rationale_missing": rationale_missing,
        }
        for i in range(1, len(visits) + 1)
    ]
    # search 单元单独记一条（gate_breakdown 第一条 = search unit）
    if visits:
        gate_breakdown.insert(0, {
            "unit": "search",
            "gate": "passed" if not rationale_missing else "not_run",
            "rationale_missing": rationale_missing,
        })

    # overall_branch（任务级聚合）
    success = next(
        (o for o in unit_outcomes if isinstance(o, Mapping) and o.get("branch") is None),
        None,
    )
    if success is not None:
        overall_branch = "success"
    else:
        first_failure = next(
            (o for o in unit_outcomes
             if isinstance(o, Mapping) and o.get("branch") is not None),
            None,
        )
        overall_branch = (
            str(first_failure.get("branch")) if first_failure else "unknown"
        )

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

    return {
        "trajectory_id": trajectory_id,
        "task_id": task_id,
        "run_id": run_id,
        "title": title,
        "source_archive": "",  # 由 build_task_view 注入
        "schema_version": "task-view.v1",
        # 单元级汇总（T 独有）
        "unit_count": 1 + len(visits),  # search + N sites
        "unit_sample_ids": _task_sample_ids(archive),
        "unit_outcomes": unit_outcomes,
        "ledger_summary": ledger,
        "task_gate": {"status": task_gate_status, "reasons": []},
        "gate_breakdown": gate_breakdown,
        # 可用性 / 结论
        "gate": {"status": task_gate_status, "reasons": []},
        "rationale_missing": rationale_missing,
        "degraded_from": [],  # T 视图无 per-unit degraded（P3 的事），整条任务级标记
        "overall_branch": overall_branch,
        # 可回放性自查
        "step_count": len(all_actions),
        "observation_count": len(all_observations),
        "missing_observation_count": missing_obs_count,
        "infrastructure_actions": sorted(infra_tools),
        "observation_complete": len(all_observations) >= len(all_actions),
        "closing_summary": _closing_text(archive) is not None
        and any(a.get("origin") != "infrastructure" for a in all_actions),
        "rationale_steps": rationale_steps,
    }


# ── 落盘包装 ────────────────────────────────────────────────────────


class TaskViewRenderResult:
    """一次 :func:`build_task_view` 的结果。"""

    __slots__ = ("trajectory_id", "written", "skipped")

    def __init__(self, trajectory_id: str, written: int, skipped: tuple[str, ...]):
        self.trajectory_id = trajectory_id
        self.written = written
        self.skipped = skipped

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

    trajectory_id = f"{task_id}__{run_id}"
    messages = to_task_messages(archive)
    openai_msgs = to_task_openai_messages(archive)
    meta = to_task_meta(archive)
    # meta 顶部 source_archive 字段填 P1 文件名（若有）
    meta["source_archive"] = ""  # 由调用方在批量场景下注入文件名

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
            "tools": task_tools(),
        },
        "meta": meta,
    }
    rep = write_task_view(payload, trajectory_id, root=root, overwrite=overwrite)
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

    __slots__ = ("results", "total_written", "total_skipped", "target_dir")

    def __init__(
        self,
        results: tuple[TaskViewRenderResult, ...],
        total_written: int,
        total_skipped: int,
        target_dir: str,
    ):
        self.results = results
        self.total_written = total_written
        self.total_skipped = total_skipped
        self.target_dir = target_dir

    def __repr__(self) -> str:  # pragma: no cover - 调试用
        return (
            f"TaskViewBatchReport(results={len(self.results)}, "
            f"total_written={self.total_written}, "
            f"total_skipped={self.total_skipped}, "
            f"target_dir={self.target_dir!r})"
        )


def build_all_task_views(
    source_root: str | Path,
    *,
    target_root: str | Path | None = None,
    archive_filter: tuple[str, ...] | None = None,
    overwrite: bool = False,
    backfill_from_filename: bool = True,
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

    Returns:
        :class:`TaskViewBatchReport`——一次返回整批 ``TaskViewRenderResult``。
    """
    import logging

    src = Path(source_root)
    out_dir = storage_task_views_dir(target_root)
    out_dir.mkdir(parents=True, exist_ok=True)

    allowed = set(archive_filter) if archive_filter is not None else None
    log = logging.getLogger(__name__)

    results: list[TaskViewRenderResult] = []
    total_written = 0
    total_skipped = 0

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
                if "__" in stem:
                    derived_run_id = stem.split("__", 1)[1].strip()
                    if derived_run_id:
                        archive["run_id"] = derived_run_id
                        log.warning(
                            "build_all_task_views 过渡期回填: %s run_id 从文件名派生=%r "
                            "(archive.write 增量六十一修复后新 P1 不再缺;记录留痕)",
                            path.name, derived_run_id,
                        )
        res = build_task_view(archive, root=target_root, overwrite=overwrite)
        results.append(res)
        total_written += res.written
        total_skipped += len(res.skipped)
    return TaskViewBatchReport(
        tuple(results), total_written, total_skipped, str(out_dir),
    )