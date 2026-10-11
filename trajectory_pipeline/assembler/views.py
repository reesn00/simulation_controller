"""P3 训练视图——把 P2 单条样本渲染成训练框架可读的三份形态。

三份视图各管一件事
------------------

``messages.json``
    通用 ``{"role", "content"}`` 序列。任何 chat 框架都能吃，
    **不做工具调用的强绑定**——它是保底形态，训练框架不认识
    ``tool_calls`` 时退回读它。
``openai.json``
    OpenAI function-calling 形态：assistant 带 ``tool_calls``，
    观察是独立的 ``role: "tool"`` 消息，靠 ``tool_call_id`` 关联。
``meta.json``
    元数据：来源存档、闸门状态、降级标记、outcome、可回放性自查。
    **训练代码不看它，但人工复核与批次筛选必须看它**。

输入是 **P2 的 JSON dict**，不是 :class:`~trajectory_pipeline.assembler.schema.Sample`
——与 ``schema.py`` 读 P1 同款纪律：**交接面是格式，不是对象**。
理由：P2 便宜、P3 更便宜，重建一批 P3 必须能脱离 ``executor`` 与
``builder`` 的当前版本独立完成。

六条判断，都是「看起来可以随便选、选错就静默毁训练数据」的那种
--------------------------------------------------------------------

1. **观察是 ``role: "tool"``，不是 ``role: "user"``。**
   ``tool_call_id`` 让模型学到「这条观察是对**那个**动作的回应」。
   退成 user 消息就把关联抹平了，而关联正是 agent 轨迹的训练信号。

2. **``new_tab`` 保留在轨迹里，但工具声明里不出现。**
   ``origin == "infrastructure"`` 的动作是执行器自己的会话隔离，
   模型永远不该输出它——但它是真实发生过的动作，删掉就无法回放
   （与 ``executor/actions.py`` 的 ``StepLog`` 同款理由）。
   **可见性由 :meth:`ToolSpec.visible_to_model` 唯一决定**，本模块
   不重新判断：``tools`` 声明里按它过滤，动作流里按原样保留。

3. **``click`` 的语义目标并进 ``arguments``。**
   ``ActionView`` 是 ``{tool, params, target, origin}`` 四字段，而
   function-calling 的 ``arguments`` 是**一个 dict**。所以
   ``target`` 的 ``{tag, label}`` 平铺进 ``arguments``，
   而不是塞进一个嵌套字段——后者会让训练出来的模型学会
   往 ``arguments.target.label`` 里填，而真实 API 要的是平铺。

4. **空 ``user_prompt`` **不发** user 消息，并在 meta 记缺件。**
   这与 :mod:`~trajectory_pipeline.assembler.observation_view` 的
   「空的就是空的，空本身是信号」**方向相反，理由必须写下来**：
   观察为空是**合法事实**（页面上确实什么都没有），而用户一句话都不说
   **不是合法事实**——用户开口就是一句诉求。手工路径下它为空是
   **没采到**，发一条空 user 消息会教模型「用户可以什么都不说」。

5. **降级样本照常渲染，但 ``meta`` 必须显眼。**
   ``degraded_from`` 非空的样本是真实数据，只是缺件；因为降级就
   拒绝渲染等于把一半数据扔了，因为降级就静默渲染等于让缺件不可见。

6. **``rationales`` 与动作逐步对齐，缺失就是缺失。**
   第 ``i`` 条只配第 ``i`` 个动作（2026-10-10 改，原为「整条一条、
   只挂第一步」）。某一步没写成时**该步没有思考段**，而整条一条都没写成时
   meta 记 ``rationale_missing``——「流水线没长出来」与「模型这轮没说话」
   的区分靠这个标记（与 :class:`GateMark` 的 ``not_run`` 同一思路）。
   **绝不补占位文字**：编一段思考会直接污染 CoT SFT。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from trajectory_pipeline.storage import (
    DEFAULT_VIEWS_DIRNAME,
    VIEW_NAMES,
    views_dir as stored_views_dir,
)
from trajectory_pipeline.storage.write_p3 import write_views as storage_write_views


def views_dir(root: str | Path | None = None) -> Path:
    """``views/`` 目录的绝对路径。**转发到 storage**——存储布局的权威
    在 :func:`trajectory_pipeline.storage.views_dir`，渲染侧不另定义路径
    字面量（改了布局只动 storage 一处）。

    ⚠️ ``parents[1]`` 不是 ``parents[2]`` 的坑及 D8 产物隔离，见 storage
    侧那条 docstring；``tests/unit/test_views.py::Test产物根`` 盯住的是
    ``views.views_dir`` 这个名字能通向正确的目录。
    """
    return stored_views_dir(root)


# ── 观察渲染成文本 ────────────────────────────────────────────────


def render_observation(obs: Mapping[str, Any]) -> str:
    """把一条**训练态观察**渲染成喂给模型的文本。

    :func:`~trajectory_pipeline.assembler.observation_view.render` 的产物
    （P2 里已经是训练态了），这里只做**排版**不再做裁剪。

    空观察渲染成 ``（本步没有取到页面内容）``——**不是**空字符串。
    理由：一条 ``content: ""`` 的 tool 消息在训练时会被当作「页面真的
    什么都没有」，而观察为空有三种成因（没采到 / 被拦 / 真的空），
    它们在 :attr:`Observation.degraded` 里有别，**空字符串会把它们抹平**。

    末尾的 ``[采集降级: …]`` 与 ``[已裁剪: …]`` 是**如实报账**：模型要学会
    看到「这块信息不完整」时说判不了，而不是当成页面上真没有
    （这正是 fail-closed 在训练侧的对应物）。
    """
    if not obs:
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

    counts = f"video 标签 {obs.get('video_tag_count', 0)} 个，iframe {obs.get('iframe_count', 0)} 个"
    parts.append(counts)

    # 如实报账，不静默省略——见模块 docstring 第 5 条与第 2 条。
    degraded = list(obs.get("degraded") or ())
    if degraded:
        parts.append(f"[采集降级: {'、'.join(str(d) for d in degraded)}]")
    trunc = obs.get("truncated") or {}
    if trunc.get("source") or trunc.get("view"):
        which = []
        if trunc.get("source"):
            which.append("上游截断")
        if trunc.get("view"):
            which.append("本次裁剪")
        parts.append(f"[内容不完整: {'、'.join(which)}]")
    return "\n\n".join(parts)


# ── 工具声明 ──────────────────────────────────────────────────────


def _openai_tool(spec: Mapping[str, Any]) -> dict[str, Any]:
    """一条 :class:`ToolSpec` → OpenAI function 定义。

    ``params`` 是**参数名元组**（``schema.TOOL_SPECS`` 的形状），转成
    OpenAI 的 ``parameters`` 时给 ``{"type": "object", "properties": {…}}``。
    刻意**不编造每步的参数类型**——P2 里存的就是名字，凭空补
    ``"string"`` 会让训练出来的模型以为参数校验是训练出来的，
    而真实执行层的校验在别处。
    """
    props = {name: {"type": "string"} for name in spec.get("params") or ()}
    return {
        "type": "function",
        "function": {
            "name": str(spec.get("name") or ""),
            "description": str(spec.get("instruction") or ""),
            "parameters": {
                "type": "object",
                "properties": props,
                "required": list(spec.get("params") or ()),
            },
        },
    }


def _visible_tools(sample: Mapping[str, Any]) -> list[dict[str, Any]]:
    """模型看得见的工具声明。

    **可见性的唯一权威是 P2 的 ``tools[]`` 里每条自己带的位置**——
    而 :class:`~trajectory_pipeline.assembler.schema.ToolSpec.visible_to_model`
    是按名字判的（``name != "new_tab"``）。本模块从 P2 的 JSON 里读不到
    方法，所以按同一条规则复现一次，并在契约测试里盯住两边同步。
    """
    out: list[dict[str, Any]] = []
    for spec in sample.get("six", {}).get("tools") or ():
        if str(spec.get("name") or "") == "new_tab":
            continue
        out.append(_openai_tool(spec))
    return out


# ── messages 视图（通用形态）──────────────────────────────────────


def to_messages(sample: Mapping[str, Any]) -> list[dict[str, Any]]:
    """通用 ``{role, content}`` 序列。

    动作与观察交替：``assistant`` 发动作、``user`` 回观察。
    工具调用在这里**降级成纯文本**（``[动作] goto(url=…)``）——
    这份视图的定位是「训练框架不认识 tool_calls 时的保底」，
    要精确的函数调用形态读 ``openai`` 视图。
    """
    six = sample.get("six") or {}
    msgs: list[dict[str, Any]] = []

    system = str(six.get("system_prompt") or "")
    if system:
        msgs.append({"role": "system", "content": system})

    user = str(six.get("user_prompt") or "")
    if user:
        msgs.append({"role": "user", "content": user})
    # 空 user_prompt **不发**空消息——见模块 docstring 第 4 条。

    actions = list(six.get("actions") or ())
    observations = list(six.get("observations") or [])
    rationales = list(six.get("rationales") or ())
    # 动作与观察一一配对；观察可能比动作少（末步失败没取到观察）
    for i, act in enumerate(actions):
        content = _rationale_text(rationales, i)
        msgs.append(
            {
                "role": "assistant",
                "content": content + _action_line(act),
            }
        )
        if i < len(observations):
            msgs.append(
                {"role": "user", "content": render_observation(observations[i])}
            )
    # 观察多于动作（理论上不该发生）时，多出来的照实补上——
    # 丢掉它们等于让模型看不到「动作发完了但观察还没回来」。
    for obs in observations[len(actions) :]:
        msgs.append({"role": "user", "content": render_observation(obs)})
    return msgs


def _rationale_text(rationales: Sequence[Any], index: int) -> str:
    """第 ``index`` 步的思考+结论，渲染成 assistant 消息的**前缀**。

    2026-10-10 起逐步对齐（原实现只给第 0 步取那条单值）：
    执行期是**逐���**生成的，只渲染第一步等于把后面的思考全丢掉。

    **缺一条不补空串**：``status != "written"`` 时返回空串，
    于是该步的 assistant 消息只有动作——「模型这轮没说话」与
    「压根没生成」在训练文本里同形是**刻意的**：后者用
    ``meta`` 视图的 ``rationale_missing`` 单独可见，
    而在这里编一段占位思考会直接污染 CoT SFT。
    """
    if index >= len(rationales):
        return ""
    r = rationales[index]
    if not isinstance(r, Mapping) or r.get("status") != "written":
        return ""
    text = str(r.get("text") or "").strip()
    conclusion = str(r.get("conclusion") or "").strip()
    parts = [p for p in (text, conclusion) if p]
    return ("\n".join(parts) + "\n") if parts else ""


def _action_line(act: Mapping[str, Any]) -> str:
    """一条动作渲染成一行可读文本。"""
    tool = str(act.get("tool") or "")
    params = dict(act.get("params") or {})
    target = act.get("target")
    if isinstance(target, Mapping):
        params.update({k: v for k, v in target.items() if v})
    if not params:
        return f"[动作] {tool}"
    return f"[动作] {tool}(" + ", ".join(f"{k}={v}" for k, v in params.items()) + ")"


# ── openai 视图（function-calling 形态）───────────────────────────


def to_openai_messages(sample: Mapping[str, Any]) -> list[dict[str, Any]]:
    """OpenAI function-calling 形态。

    ``tool_call_id`` 由 **step 序号** 派生（``call_0`` / ``call_1``…），
    不取自 P1 的任何字段——P1 里没有可用的调用 id，而 seq 是**确定**的，
    不是猜的（与 :mod:`~trajectory_pipeline.assembler.builder` 用
    ``sample_id`` 反查源存档同款：能从数据算出来的不另找来源）。
    """
    six = sample.get("six") or {}
    msgs: list[dict[str, Any]] = []

    system = str(six.get("system_prompt") or "")
    if system:
        msgs.append({"role": "system", "content": system})
    user = str(six.get("user_prompt") or "")
    if user:
        msgs.append({"role": "user", "content": user})

    actions = list(six.get("actions") or ())
    observations = list(six.get("observations") or [])
    rationales = list(six.get("rationales") or ())

    for i, act in enumerate(actions):
        # 思考段只挂在**第一步**上：它解释的是「为什么开始找」，
        # 而后续每步的思考在模块 4 落地后会逐步生成。
        # 思考段**逐步对齐**（2026-10-10 改，原实现只挂第一步）。
        # 少一步就想清一步——只挂第一步等于把后面的思考全丢掉，而模型
        # 要学的恰恰是「上一步看到什么，才决定这一步做什么」。
        text = _rationale_text(rationales, i).strip() or None
        msgs.append(
            {
                "role": "assistant",
                "content": text,
                "tool_calls": [
                    {
                        "id": f"call_{i}",
                        "type": "function",
                        "function": {
                            "name": str(act.get("tool") or ""),
                            # 平铺 target，见模块 docstring 第 3 条
                            "arguments": _arguments(act),
                        },
                    }
                ],
            }
        )
        if i < len(observations):
            msgs.append(
                {
                    "role": "tool",
                    "tool_call_id": f"call_{i}",
                    "content": render_observation(observations[i]),
                }
            )
    for j, obs in enumerate(observations[len(actions) :], start=len(actions)):
        msgs.append(
            {
                "role": "tool",
                "tool_call_id": f"call_{j}",
                "content": render_observation(obs),
            }
        )
    return msgs


def _arguments(act: Mapping[str, Any]) -> dict[str, Any]:
    """动作 → function-calling 的 ``arguments``。

    ``params`` 与 ``target`` **平铺合并**。``click`` 没有 params，
    它的语义目标就在 target 里；反过来 ``goto`` 的 url 在 params 里。
    两者键空间不冲突，合并即可。
    """
    args: dict[str, Any] = dict(act.get("params") or {})
    target = act.get("target")
    if isinstance(target, Mapping):
        for k, v in target.items():
            if v not in (None, ""):
                args[str(k)] = v
    return args


# ── meta 视图 ─────────────────────────────────────────────────────


def to_meta(sample: Mapping[str, Any]) -> dict[str, Any]:
    """元数据。**训练代码不看它，人工复核与批次筛选看它。**

    四组：

    - **来源**：``sample_id`` / ``task_id`` / ``unit`` / ``source_archive``——
      从 id 就能反查 P1，不必开文件。
    - **可用性**：``gate`` / ``rationale_missing`` / ``degraded_from``——
      决定这条能不能进训练集。
    - **结论**：``outcome``（含 ``branch``）——批次级断言的原料。
    - **可回放性自查**：``step_count`` / ``observation_count`` /
      ``infrastructure_actions``——见下。
    """
    six = sample.get("six") or {}
    actions = list(six.get("actions") or ())
    observations = list(six.get("observations") or ())
    infra = [
        str(a.get("tool") or "") for a in actions if a.get("origin") == "infrastructure"
    ]

    return {
        "sample_id": sample.get("sample_id", ""),
        "task_id": sample.get("task_id", ""),
        "unit": sample.get("unit", ""),
        "title": sample.get("title", ""),
        "source_archive": sample.get("source_archive", ""),
        "gate": dict(sample.get("gate") or {}),
        "rationale_missing": bool(sample.get("rationale_missing", True)),
        "degraded_from": list(sample.get("degraded_from") or ()),
        "outcome": dict(sample["outcome"]) if sample.get("outcome") else None,
        "provenance": dict(six.get("provenance") or {}),
        # 可回放性：动作与观察必须成对（末步失败允许少一个观察）
        "step_count": len(actions),
        "observation_count": len(observations),
        "infrastructure_actions": infra,
        # 成对性自查：动作多于观察说明末步没取到观察，训练侧会看到
        # 一个「发出去但没回音」的动作——那是真实轨迹的一部分，不修。
        "observation_complete": len(observations) >= len(actions),
        # 思考的覆盖：``rationale_missing`` 只说「一条都没有」，
        # 而「写了 2/3 步」在批次筛选时同样要看得见——**逐条列出来**，
        # 别合成一个计数：合成之后就分不出「缺中间那步」与「缺最后那步」，
        # 而两者的处置不同（前者会让后面的时序核查失去上下文）。
        "rationale_steps": [
            {"step_index": i, "status": str(r.get("status") or "")}
            for i, r in enumerate(six.get("rationales") or ())
            if isinstance(r, Mapping)
        ],
    }


# ── 汇总 ──────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class ViewResult:
    """一条样本的 P3 渲染结果。"""

    sample_id: str
    written: int = 0
    skipped: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class ViewBuildReport:
    samples: tuple[ViewResult, ...] = ()
    total_written: int = 0
    total_skipped: int = 0
    views_dir: str = ""

    @property
    def ok(self) -> bool:
        return self.total_skipped == 0


def build_views(
    sample: Mapping[str, Any],
    *,
    root: str | Path | None = None,
    overwrite: bool = False,
) -> ViewResult:
    """把一条 **P2 JSON** 渲染成三份 P3 并落盘。

    渲染在 :mod:`assembler.views`、**落盘在 :mod:`trajectory_pipeline.storage`
    （:func:`trajectory_pipeline.storage.write_p3.write_views`）**——原子写、
    默认不覆盖、失败进 ``skipped`` 不抛出的语义都在 storage 一侧，这里只
    把渲染好的 payload 交给它。改存储布局不动渲染，反之亦然。
    """
    sid = str(sample.get("sample_id") or "")
    if not sid:
        return ViewResult(sample_id="", written=0, skipped=("<no-sample_id>",))

    payloads = {
        "messages": to_messages(sample),
        "openai": {
            "sample_id": sid,
            "messages": to_openai_messages(sample),
            "tools": _visible_tools(sample),
        },
        "meta": to_meta(sample),
    }
    rep = storage_write_views(payloads, sid, root=root, overwrite=overwrite)
    return ViewResult(sample_id=rep.sample_id, written=rep.written, skipped=rep.skipped)


def build_views_from_dir(
    samples_root: str | Path | None = None,
    *,
    root: str | Path | None = None,
    overwrite: bool = False,
) -> ViewBuildReport:
    """读一整个 P2 目录，逐条渲染 P3。

    **坏样本不打断整批**——同 :func:`trajectory_pipeline.assembler.builder.build_all`：
    一条样本序列化失败只进 ``skipped``，其余继续。
    """
    base = (
        Path(samples_root)
        if samples_root is not None
        else views_dir(root).parent / "samples"
    )
    results: list[ViewResult] = []
    total_written = 0
    total_skipped = 0
    for path in sorted(base.glob("*.json")):
        try:
            sample = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            results.append(
                ViewResult(
                    sample_id=path.stem, written=0, skipped=(f"{path.name} ({exc})",)
                )
            )
            total_skipped += 1
            continue
        res = build_views(sample, root=root, overwrite=overwrite)
        results.append(res)
        total_written += res.written
        total_skipped += len(res.skipped)
    return ViewBuildReport(
        tuple(results), total_written, total_skipped, str(views_dir(root))
    )


__all__ = [
    "VIEW_NAMES",
    "DEFAULT_VIEWS_DIRNAME",
    "views_dir",
    "render_observation",
    "ViewResult",
    "ViewBuildReport",
    "to_messages",
    "to_openai_messages",
    "to_meta",
    "build_views",
    "build_views_from_dir",
]
