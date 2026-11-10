"""D-2「观察-动作一致性」——**代码筛 + LLM 判语义**（样本级）。

## rubric v1.4 改了什么

v1.3 及以前，D-2 判的是「动作引用的实体在观察存档里存不存在」：抽
``click`` 目标的 label，逐条在前置观察的元素清单里检索。v1.4 起判据改为
**前后动作顺序正常、连贯**——**不再核对点击目标是否在元素清单里**。

为什么换：旧判据只认 ``click``。一条只有 ``goto`` 的轨迹（搜索 → 进站 →
观察）没有任何引用可校验，只能判「不可判定（null）」——而它的动作顺序明明
是可判的（实测 ``E001__57bbf995``：4 步全是 goto，d2 恒 null）。新判据看的是
整条动作链的时序与衔接，纯导航轨迹同样能判。

## 两段判法（不是投票）

1. **代码筛**（:func:`screen_d2`）——机械提取时序疑点：
   - ``no_observation``：某步动作没有对应观察。**事实残缺 ⇒ 整条不可判定**，
     不记作轨迹缺陷（缺观察是采集问题，不是模型不连贯）。
   - ``goto_not_landed``：``goto`` 的落点与目标 URL 不一致（重定向、被拦、
     或动作没生效）——**交 LLM 判真伪**，因为正常站点也会 http→https 或
     跨主机重定向。
   - ``repeat_action``：同一页面连续重复同一动作——**交 LLM 判严重度**，
     因为重试有时是正常的。
2. **LLM 判语义**（:func:`score_d2`）——对每个疑点判「真断裂 / 误报」，
   并通读动作链补出代码筛漏掉的断裂；每处标 ``mild`` / ``material``。

**代码筛不产出分数，LLM 也不重新筛疑点**——两个阶段各自只有一个判据来源，
不是两份判断取交集（CLAUDE.md：禁止投票/集成）。

## 三态

- ``score=None``（``undetermined``）：没有模型动作 / 缺观察 / LLM 不可用 /
  LLM 输出不可解析。这四种都**不扣轨迹的分**：前两类是对象或采集事实缺失，
  后两类是评估能力缺失（fail-closed，绝不猜）。
- ``score=5..1``：按**真断裂**处数与严重度落档（见下）。

## 分数映射（rubric v1.4 D-2，取最重档）

=========================================  ======
情形                                        分数
=========================================  ======
0 处断裂                                     5
1 处轻微断裂                                 4
≥2 处轻微断裂                                3
1 处实质断裂                                 2
≥2 处实质断裂                                1
=========================================  ======

"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Literal, Mapping, Sequence
from urllib.parse import urlsplit

from trajectory_pipeline.assembler.schema import Sample
from trajectory_pipeline.evaluation.scorers import rubric_version
from trajectory_pipeline.llm.client import LLMClient
from trajectory_pipeline.llm.schema_parse import extract_json_reporting, strip_think

#: 疑点严重度。与 rubric 的「轻微 / 实质」一一对应，不另设刻度。
Severity = Literal["mild", "material"]

#: 代码筛的两种结局。
ScreenKind = Literal["screenable", "undetermined"]

#: 整条样本的判定结局。
VerdictKind = Literal["scored", "undetermined"]

#: 合法严重度取值——解析层据此拒收契约外字符串（不猜）。
SEVERITIES: frozenset[str] = frozenset({"mild", "material"})

#: 导航类动作——承载页面跳转。``new_tab`` / ``close_tab`` 由执行器发出
#: （``origin="infrastructure"``），不会进模型动作流，列在这里是为了让
#: 「哪些动作算导航」只有一处定义。
NAV_TOOLS: frozenset[str] = frozenset({"goto", "new_tab", "close_tab"})

#: 喂 LLM 时每步正文的截断长度。观察正文动辄数千字，整条轨迹塞进去会超时；
#: 判「顺序连贯」只需要知道这一步看到了什么，不需要全文。
BODY_PREVIEW_CHARS = 240

#: 解析失败时的短原因（与 d3 同款措辞，便于报表搜索）。
_PARSE_FAIL = "parse_fail"


# ═══════════════════════════════════════════════════════════════════════
# 结果结构
# ═══════════════════════════════════════════════════════════════════════


@dataclass(frozen=True, slots=True)
class ScreenPoint:
    """代码筛出的一处**时序疑点**（尚未判真伪）。

    ``reason`` 必须写清依据（哪一步、看到了什么），因为读报告的人要靠它
    判断该去改采集器还是改模型提示词。
    """

    step: int
    severity: Severity
    code: str
    reason: str

    def to_json(self) -> dict[str, Any]:
        return {"step": self.step, "severity": self.severity,
                "code": self.code, "reason": self.reason}


@dataclass(frozen=True, slots=True)
class StepDigest:
    """一步动作 + 它执行后的观察，供 LLM 判读（**只读**，不落训练数据）。

    ``step`` 是**样本 ``actions`` 里的全局下标**（0 起，含执行器动作留下的
    空档）——报告里的「第 N 步」与 P1/P2 的步号因此对得上。
    """

    step: int
    tool: str
    params: Mapping[str, Any]
    target: Mapping[str, str] | None
    origin: str
    url: str
    title: str
    body: str
    error: str


@dataclass(frozen=True, slots=True)
class BreakFinding:
    """LLM 判定的**真断裂**。``source`` 区分来源：``code`` = 代码筛疑点
    被判真，``llm`` = 代码筛没看到、LLM 补出的。"""

    step: int
    severity: Severity
    reason: str
    source: str

    def to_json(self) -> dict[str, Any]:
        return {"step": self.step, "severity": self.severity,
                "reason": self.reason, "source": self.source}


@dataclass(frozen=True, slots=True)
class D2Screen:
    """代码筛结果。``undetermined`` 时 ``reasons`` 说明缺什么事实。"""

    kind: ScreenKind
    task: str
    steps: tuple[StepDigest, ...] = ()
    points: tuple[ScreenPoint, ...] = ()
    reasons: tuple[str, ...] = ()

    def to_json(self) -> dict[str, Any]:
        return {"kind": self.kind, "task": self.task,
                "steps": [s.step for s in self.steps],
                "points": [p.to_json() for p in self.points],
                "reasons": list(self.reasons)}


@dataclass(frozen=True, slots=True)
class D2Verdict:
    """D-2 判定结果。``score`` 为 ``None`` 时 ``kind`` 必为 ``undetermined``。"""

    score: int | None
    kind: VerdictKind
    breaks: tuple[BreakFinding, ...] = ()
    points: tuple[ScreenPoint, ...] = ()
    reasons: tuple[str, ...] = ()

    def to_json(self) -> dict[str, Any]:
        return {"dimension": "D-2", "score": self.score, "kind": self.kind,
                "breaks": [b.to_json() for b in self.breaks],
                "points": [p.to_json() for p in self.points],
                "reasons": list(self.reasons)}


# ═══════════════════════════════════════════════════════════════════════
# 读样本
# ═══════════════════════════════════════════════════════════════════════


def _attr(action: Any, key: str, default: Any = None) -> Any:
    """兼容 ``ActionView``（dataclass）与存档字典。"""
    value = getattr(action, key, None)
    if value is None and isinstance(action, Mapping):
        value = action.get(key)
    return default if value is None else value


def _model_actions(sample: Any) -> list[tuple[int, Any]]:
    """样本里**模型可见**的动作，连同它在 ``sample.actions`` 里的原下标。

    只取 ``origin == "model"``：``new_tab`` / ``close_tab`` 是执行器的会话
    隔离动作，模型永不输出它们（见 ``executor/actions.py``）。判「模型的
    动作顺序」时把执行器的动作算进来，等于拿采集器的行为去扣模型的分。

    **返回原下标是必须的**：``observations`` 与 ``sample.actions`` 里的
    **全部**动作（含执行器动作）按位对齐。先过滤再按下标取观察，会让
    infra 步之后的每一次观察都错位一格——而错位后「这一步看到了什么」
    仍然是一份正常观察，报告上看不出异常。
    """
    out: list[tuple[int, Any]] = []
    for index, act in enumerate(getattr(sample, "actions", ()) or ()):
        if _attr(act, "tool", "") == "":
            continue
        if _attr(act, "origin", "model") != "model":
            continue
        out.append((index, act))
    return out


def _observations(sample: Any) -> list[Any]:
    """**按下标对齐**的全部观察，不在这里过滤。

    ``observations`` 与 ``actions`` 按位对齐；先过滤掉非对象项会让之后的
    每一步观察都错位一格，而错位后的观察仍是一份正常观察、报告上看不出
    异常。非对象项交由 :func:`_obs_unusable` 在原地判「这一步没有观察」。
    """
    return list(getattr(sample, "observations", ()) or ())


def _task_text(sample: Any) -> str:
    return str(getattr(sample, "user_prompt", "") or "")


def _obs_unusable(obs: Any) -> str:
    """这份观察**能不能支撑**「这一步看到了什么」。

    返回空串表示能用；非空是**不可用**的具体原因。

    只判「事实在不在」，不判「全不全」：正文/元素/链接任一非空就能用。
    降级 / 截断**不拦**——新判据不要求元素清单完整（那只影响旧口径的
    「目标存不存在」），但会在 LLM 输入里原样带上 ``degraded`` 标记。
    """
    if obs is None:
        return "这一步没有对应观察"
    if not isinstance(obs, Mapping):
        return "这一步的观察不是对象，读不出任何事实"
    body = str(obs.get("body_text") or "").strip()
    elements = list(obs.get("interactive_elements") or ())
    links = list(obs.get("links") or ())
    url = str(obs.get("url") or "").strip()
    if not body and not elements and not links and not url:
        return "观察三项全空（正文 / 元素 / 链接 / URL 都没有），采集可能整体失败"
    return ""


def _url_key(url: str) -> tuple[str, str]:
    """URL 的**落点身份**：``(host, path)``，忽略 scheme / query / fragment。

    忽略 scheme 是刻意的：http→https 是站点常态跳转，不是动作断裂。
    忽略 query 是刻意的：很多播放站靠 query 定位视频，但同一 host+path
    的 query 变化不构成「顺序」问题。
    """
    try:
        parts = urlsplit(url.strip())
        host = (parts.hostname or "").lower()
        path = parts.path.rstrip("/") or "/"
        return host, path
    except ValueError:
        return "", url.strip()


def _goto_landed(params: Mapping[str, Any], landed_url: str) -> bool:
    """``goto`` 的目标 URL 与动作后观察的 URL 是否落在同一位置。

    **只作为疑点，不直接判断裂**：跨主机重定向、登录跳转都会让落点不同，
    而它们是否「顺序不连贯」需要看上下文（LLM 判）。
    """
    target = str((params or {}).get("url") or "").strip()
    landed = str(landed_url or "").strip()
    if not target or not landed:
        return True  # 没目标/没落点 → 交给别的信号，不在这里凭空判
    if _url_key(target) == _url_key(landed):
        return True
    th, tp = _url_key(target)
    lh, lp = _url_key(landed)
    # 同 host 且落点路径是目标路径的前缀（或反之）→ 视为落到位（重定向到
    # 子路径/规范化 URL），不记疑点。
    if th and th == lh and (tp.startswith(lp) or lp.startswith(tp)):
        return True
    return False


def _action_signature(action: Any) -> str:
    """动作的**逐字签名**——判断「连续重复」用。"""
    payload = {
        "tool": _attr(action, "tool", ""),
        "params": _attr(action, "params", {}) or {},
        "target": _attr(action, "target", None),
    }
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str)


def _preview(text: Any, limit: int = BODY_PREVIEW_CHARS) -> str:
    s = str(text or "").strip()
    return s if len(s) <= limit else s[:limit] + "…[已截断]"


# ═══════════════════════════════════════════════════════════════════════
# 第一段：代码筛
# ═══════════════════════════════════════════════════════════════════════


def screen_d2(sample: Sample | Any) -> D2Screen:
    """**代码筛**：把动作链切成步摘要，并机械提取时序疑点。

    不产出分数、不判真伪、不调 LLM。``undetermined`` 只由**事实残缺**
    触发（没有模型动作 / 某步缺观察），且**不记作轨迹缺陷**。
    """
    pairs = _model_actions(sample)
    task = _task_text(sample)
    if not pairs:
        return D2Screen(
            "undetermined", task, (),
            (),
            ("本条样本没有模型可见的动作，没有动作链可判连贯性；"
             "「没有可判对象」不等于「全程连贯」",),
        )

    observations = _observations(sample)
    steps: list[StepDigest] = []
    for source_i, action in pairs:
        obs = observations[source_i] if source_i < len(observations) else None
        why = _obs_unusable(obs)
        if why:
            return D2Screen(
                "undetermined", task, tuple(steps), (),
                (f"第 {source_i + 1} 步（{_attr(action, 'tool', '')}）：{why}——"
                 "缺观察是采集事实残缺，不是轨迹不连贯；本条不出分",),
            )
        assert obs is not None  # _obs_unusable 已排除 None
        steps.append(StepDigest(
            step=source_i,
            tool=str(_attr(action, "tool", "") or ""),
            params=dict(_attr(action, "params", {}) or {}),
            target=dict(_attr(action, "target", {}) or {}) or None,
            origin=str(_attr(action, "origin", "model") or ""),
            url=str(obs.get("url") or ""),
            title=str(obs.get("page_title") or ""),
            body=_preview(obs.get("body_text")),
            error=str(_attr(action, "error", "") or ""),
        ))

    points: list[ScreenPoint] = []
    prev_sig = ""
    for chain_i, ((_, action), step) in enumerate(zip(pairs, steps)):
        tool = step.tool
        # ① goto 没落到目标页（重定向 / 被拦 / 动作未生效）
        if tool == "goto" and not _goto_landed(step.params, step.url):
            points.append(ScreenPoint(
                step.step, "material", "goto_not_landed",
                f"第 {step.step + 1} 步 goto 的目标是 {step.params.get('url')!r}，"
                f"但动作后的观察落在 {step.url!r}——落点与目标不一致，"
                "需判是否属于正常重定向",
            ))
        # ② 连续重复动作（同一动作、同一页面）
        sig = _action_signature(action)
        if chain_i > 0 and sig == prev_sig and steps[chain_i - 1].url == step.url:
            # G-4：连续相同调用按一次计——整段只在段首记一处，不逐个累加
            if not points or points[-1].code != "repeat_action" \
                    or points[-1].step != steps[chain_i - 1].step:
                points.append(ScreenPoint(
                    step.step, "mild", "repeat_action",
                    f"第 {step.step + 1} 步与第 {steps[chain_i - 1].step + 1} 步"
                    f"是同一动作、同一页面（{step.url!r}），"
                    "属于原地空转，需判是否正常重试",
                ))
        prev_sig = sig

    return D2Screen("screenable", task, tuple(steps), tuple(points), ())


# ═══════════════════════════════════════════════════════════════════════
# 第二段：LLM 判语义
# ═══════════════════════════════════════════════════════════════════════


#: 给 LLM 的系统提示。「评分锚」一栏**从锚点表渲染**（方案 16 §7 / C-10；
#: owner 2026-10-23 裁定 D-C7 = rubric 变更强制连带改 prompt）。
D2_SYSTEM_PROMPT: str = (
    "你是一名轨迹连贯性判读员。你的任务：判断一条 Agent 浏览器轨迹里"
    "**动作的先后顺序是否正常、连贯**。\n\n"
    "## 判什么\n"
    "- 每一步动作是否建立在上一步的观察之上（看到什么，才做什么）。\n"
    "- 相邻动作之间的推进是否正常：没有跳步、没有原地空转、"
    "没有与任务无关的来回。\n"
    "- **不要**核对点击目标的名称是否出现在观察的元素清单里——"
    "rubric v1.4 起那不是本维判据。\n\n"
    "## 两档严重度\n"
    "- ``mild``（轻微）：动作顺序可解释，但有冗余 / 重复 / 回溯，"
    "推进效率受损。\n"
    "- ``material``（实质）：动作缺少前序观察支撑，或页面 / 步骤衔接断裂"
    "（例如 goto 没落到目标页就继续推进、动作链跳段）。\n\n"
    "## 输出契约（严格 JSON，无前后文字、无 markdown 代码块）\n"
    "```\n"
    '{"points": [{"index": 0, "is_break": true,'
    ' "severity": "mild|material", "reason": "..."}],'
    ' "new_breaks": [{"step": 2, "severity": "mild", "reason": "..."}]}\n'
    "```\n"
    "1. ``points`` 必须与输入疑点**一一对应**：数量、``index`` 都不能少。\n"
    "2. ``new_breaks`` 只写代码筛漏掉的断裂；没有就写 ``[]``。"
    "``step`` 用动作链里「第 N 步」的 **N**（从 1 起）。\n"
    "3. ``reason`` 必须引用轨迹里的具体动作或页面，不写泛泛评价。\n"
    "4. ``severity`` 只能是 ``mild`` / ``material``。\n"
    + rubric_version.render_anchor_block("d2")
)


def _render_user(screen: D2Screen) -> str:
    """代码筛结果 → LLM 用户提示。**只给事实，不给结论**。"""
    lines = [f"任务：{screen.task or '(未知)'}", "", "## 动作链"]
    for d in screen.steps:
        params = json.dumps(dict(d.params), ensure_ascii=False, default=str)
        target = (json.dumps(dict(d.target), ensure_ascii=False, default=str)
                  if d.target else "无")
        lines.append(f"### 第 {d.step + 1} 步")
        lines.append(f"- 动作：{d.tool}  参数={params}  目标={target}")
        lines.append(f"- 动作后观察：url={d.url or '(空)'}  title={d.title or '(空)'}")
        if d.body:
            lines.append(f"- 正文摘录：{d.body}")
        if d.error:
            lines.append(f"- 执行错误：{d.error}")
    lines.append("")
    lines.append("## 代码筛疑点（逐条判真伪）")
    if not screen.points:
        lines.append("（代码筛未发现时序疑点）")
    for i, p in enumerate(screen.points):
        lines.append(
            f"- index={i}  step={p.step + 1}  候选严重度={p.severity}  "
            f"code={p.code}  依据={p.reason}"
        )
    return "\n".join(lines)


def _parse_response(raw: str | None) -> tuple[dict[str, Any] | None, str]:
    """从 LLM 原文抠 JSON。**入口已剥 think**（``schema_parse`` 内部）。"""
    if not raw:
        return None, "LLM 返回空"
    data, _repairs = extract_json_reporting(raw)
    if data is None:
        # 顶层是合法 JSON 但不是对象（数组 / 标量）时给明确原因：报表上
        # 「模型答错了形状」与「模型没答出 JSON」是两种要分别修的事。
        if _is_non_object_json(raw):
            return None, "LLM 返回的 JSON 不是对象"
        return None, _PARSE_FAIL
    return dict(data), ""


def _is_non_object_json(raw: str) -> bool:
    """原文能否解析成**合法 JSON 但顶层不是对象**（数组 / 标量）。

    提取器只认 ``{...}``，因此顶层数组会掉到 ``None``;这里只负责把
    「形状不对」与「根本不是 JSON」分开，不参与正路解析。
    """
    try:
        parsed = json.loads(strip_think(raw).strip())
    except (ValueError, TypeError):
        return False
    return not isinstance(parsed, Mapping)


def _as_int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float) and value.is_integer():
        return int(value)
    if isinstance(value, str) and value.strip().lstrip("-").isdigit():
        return int(value.strip())
    return None


def _parse_judgement(
    data: Mapping[str, Any], screen: D2Screen
) -> tuple[list[BreakFinding] | None, str]:
    """LLM 输出 → 真断裂列表。**不合契约就整条作废**（返回 None + 原因），
    不做部分采信——少判一条疑点会让分数偏高，那正是要防的方向。"""
    raw_points = data.get("points")
    if not isinstance(raw_points, list):
        return None, "缺少 points 列表"
    if len(raw_points) != len(screen.points):
        return None, (f"points 数量 {len(raw_points)} 与代码筛疑点数 "
                      f"{len(screen.points)} 不一致")

    seen: set[int] = set()
    breaks_by_index: dict[int, BreakFinding] = {}
    for item in raw_points:
        if not isinstance(item, Mapping):
            return None, "points 里有非对象项"
        index = _as_int(item.get("index"))
        if index is None or index < 0 or index >= len(screen.points):
            return None, f"points 的 index 越界：{item.get('index')!r}"
        if index in seen:
            return None, f"points 的 index 重复：{index}"
        seen.add(index)
        is_break = item.get("is_break")
        if not isinstance(is_break, bool):
            return None, f"index={index} 的 is_break 不是布尔值"
        severity = str(item.get("severity") or "").strip()
        reason = str(item.get("reason") or "").strip()
        if is_break:
            if severity not in SEVERITIES:
                return None, f"index={index} 的 severity 不在契约内：{severity!r}"
            if not reason:
                return None, f"index={index} 判为断裂却没写 reason"
            breaks_by_index[index] = BreakFinding(
                screen.points[index].step, severity, reason, "code")
    if len(seen) != len(screen.points):
        missing = [i for i in range(len(screen.points)) if i not in seen]
        return None, f"points 漏判了 index：{missing}"

    raw_new = data.get("new_breaks", [])
    if raw_new is None:
        raw_new = []
    if not isinstance(raw_new, list):
        return None, "new_breaks 不是列表"
    breaks = [breaks_by_index[i] for i in sorted(breaks_by_index)]
    for item in raw_new:
        if not isinstance(item, Mapping):
            return None, "new_breaks 里有非对象项"
        raw_step = _as_int(item.get("step"))
        # 契约里 ``step`` 是「第 N 步」的 N（1 起）；内部一律用 0 起下标。
        step = None if raw_step is None else raw_step - 1
        severity = str(item.get("severity") or "").strip()
        reason = str(item.get("reason") or "").strip()
        valid_steps = {d.step for d in screen.steps}
        if step is None or step not in valid_steps:
            legal = sorted(s + 1 for s in valid_steps)
            return None, (f"new_breaks 的 step 不是动作链里的步号："
                          f"{item.get('step')!r}（合法：{legal}，从 1 起）")
        if severity not in SEVERITIES:
            return None, f"new_breaks 的 severity 不在契约内：{severity!r}"
        if not reason:
            return None, "new_breaks 有断裂没写 reason"
        breaks.append(BreakFinding(step, severity, reason, "llm"))
    return breaks, ""


def _score_of(breaks: Sequence[BreakFinding]) -> int:
    """真断裂 → 1–5 档（rubric v1.4 D-2，**取最重档**）。

    「实质」优先于「轻微」：同一批里既有实质断裂又有两条轻微时，报 3 分
    会让那条实质断裂消失，而它才是需要人去查的。
    """
    material = sum(1 for b in breaks if b.severity == "material")
    mild = sum(1 for b in breaks if b.severity == "mild")
    if material >= 2:
        return 1
    if material == 1:
        return 2
    if mild >= 2:
        return 3
    if mild == 1:
        return 4
    return 5


def score_d2(
    sample: Sample | Any,
    *,
    llm_client: LLMClient | None = None,
    system_prompt: str | None = None,
) -> D2Verdict:
    """D-2 打分：代码筛 → LLM 判语义 → 按断裂处数落档。

    Args:
        sample: P2 样本（``Sample`` 对象或 Mapping 形态的动作）。
        llm_client: LLM 后端；``None`` → 本条不出分（fail-closed）。
        system_prompt: 换一版提示词（A/B 用；默认 :data:`D2_SYSTEM_PROMPT`）。

    **永不抛**：LLM 调不通 / JSON 抠不出 / 契约字段不对，全部走
    ``undetermined``——本维没有「判不出就扣分」的锚点，能力缺失就不出分。
    """
    screen = screen_d2(sample)
    if screen.kind == "undetermined":
        return D2Verdict(None, "undetermined", (), (), screen.reasons)

    if llm_client is None:
        return D2Verdict(
            None, "undetermined", (), screen.points,
            ("未提供 LLM client（TRAJECTORY_LLM_* 未配置，或被 --no-llm "
             "关掉）——D-2 的语义判读缺能力，本条不出分；这是 fail-closed，"
             "不是「判不了」（与 D-3 / D-9 的空跑纪律同款）",),
        )

    raw = llm_client.chat(system_prompt or D2_SYSTEM_PROMPT, _render_user(screen))
    data, why = _parse_response(raw)
    if data is None:
        return D2Verdict(
            None, "undetermined", (), screen.points,
            (f"LLM 未判（{why}）——不猜，本条不出分",),
        )

    breaks, why = _parse_judgement(data, screen)
    if breaks is None:
        return D2Verdict(
            None, "undetermined", (), screen.points,
            (f"LLM 输出不合契约（{why}）——不猜，本条不出分",),
        )

    score = _score_of(breaks)
    if breaks:
        reasons = tuple(
            f"第 {b.step + 1} 步（{b.source}）：{b.severity}——{b.reason}"
            for b in breaks
        )
    else:
        reasons = ("0 处断裂：动作链全程连贯——rubric §D-2 锚点 5",)
    return D2Verdict(score, "scored", tuple(breaks), screen.points, reasons)


__all__ = [
    "Severity", "ScreenKind", "VerdictKind", "SEVERITIES", "NAV_TOOLS",
    "BODY_PREVIEW_CHARS", "ScreenPoint", "StepDigest", "BreakFinding",
    "D2Screen", "D2Verdict", "D2_SYSTEM_PROMPT",
    "screen_d2", "score_d2",
]