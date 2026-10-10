"""rationale 生成（LLM）——模块 4 的**语言层**半边。

与 :mod:`~trajectory_pipeline.rationale.gates` 的分工
=====================================================

``gates`` 是**判据**（纯代码、零成本、100% 覆盖），本模块是**生成**。
先有判据再有生成——反过来第一步产出的就是无法评估的批次。

⚠️ 本模块**不 import executor、不 import assembler、不 import perception**。
它只认两种东西：一个 :class:`~trajectory_pipeline.llm.client.LLMClient`，
以及**按文件格式读进来的动作与观察字典**。理由与
:mod:`trajectory_pipeline.assembler.schema` 同款：交接面是格式。
executor 改一个 dataclass 字段，半年前那批存档就不该切不出 rationale。

七条口径
========

1. **不可用返回 ``None``，不返回空串。** ``WriteResult.status`` 三值：
   ``written`` / ``unavailable`` / ``empty``。**「LLM 挂了」与「模型什么都没说」
   是两回事**，混成一个空串就等于把采集故障记成数据事实（§3.1）。

2. **``<think>`` 剥完是空串 → 判不可用，不是「模型没说话」。**
   这是本模块最贵的一个坑：推理模型被 ``max_tokens`` 截断时会留下
   **未闭合**的 ``<think>``，后面全是思维链正文。
   :func:`~trajectory_pipeline.llm.schema_parse.strip_think` 会把这一整段
   剥掉，于是本模块拿到的是**空串**——而空串在下游与「模型这一轮没说话」
   完全同形。处置是**显式记原因并判不可用**：观测**到什么**推出空串，
   这本身就是一条要修的信号。

   ⚠️ **哪几个后端会返内联 ``<think>`` 是实测出来的，不能假定。**
   同一个服务端上（2026-10-10 同一提示词、同一 ``max_tokens``）：

   | 模型 | 内联 ``<think>`` | 正文长度 |
   |---|---|---|
   | ``MiniMax-M3`` / ``MiniMax-M2.7`` | ✅ **有** | 2286 / 2320 字符 |
   | ``MiniMax-M3.1-Flash-Preview``（当前 ``TRAJECTORY_LLM_MODEL``） | ❌ 无 | 209 |
   | ``deepseek-v4.1-flash`` / ``qwen3.8-flash`` / ``qwen3.8-27b`` / ``GLM-5.3-Flash`` | ❌ 无 | 213–490 |

   **同产品族里两种行为都有**，所以这条剥离是**防御性**的而不是多余的：
   换一次配置就可能换到一个会返的，而它返回与否**逐次都可能不同**。

3. **采集降级必须写进提示词，不能只靠闸门兜。** 闸门是**事后**判，
   而模型是在**当场**编故事。§5.5 那条最贵的实测就发生在这里：
   iqiyi 播放页 ``video=1``、正文与元素皆空，模型写下
   「正文为空、元素为空，说明未渲染」——一句**通顺的错结论**。
   所以本模块把「这份观察不完整：<原因>」直接写进 user 提示词，
   让模型自己知道它的输入有多薄。**闸门是第二道防线，不是第一道。**

4. **不把 ``ref`` 句柄喂给模型。** 它是 obscura 的会话内句柄，换个 session
   就失效，进训练数据等于让模型学一个随机数。与
   :class:`~trajectory_pipeline.assembler.schema.ActionView` 同款口径：
   点击目标只给 ``tag`` + ``label``。

5. **提示词不提 obscura / MCP / ref / 控制流。** 模型学到的是
   「遇到这样的用户与页面该怎么办」，不是「怎么驱动某个浏览器」——
   与 :data:`trajectory_pipeline.assembler.schema.SYSTEM_PROMPT` 同款口径。

6. **剥完仍残留思维链标记 → 拒写。** 红线是 raw CoT 不进任何持久化路径，
   而本模块是 thinking 变成落盘数据的**第一站**。
   :data:`_RESIDUAL_COT` 只列 :func:`~trajectory_pipeline.llm.schema_parse.strip_think`
   **漏掉的**那些形态（特殊 token、``<scratchpad>`` 一类）；
   ``<think>`` / ``<thinking>`` / ``<reasoning>`` 由 ``strip_think`` 负责，
   **不重复列**——列了就是一条看起来在守线、实际永远不触发的死守卫。

7. **两个字段用 JSON 承载。** 已实测本项目后端**不强制 ``json_schema``**，
   模型会漂字段名、套 ``<think>``、首轮跑偏；别名容错与重试是
   :mod:`~trajectory_pipeline.llm.schema_parse` 的共享资产。
   **解析失败判不可用，不静默只留散文**——悄悄少一个字段，
   下游看到的是「结论为空」而不是「这一条没写成」。

   ⚠️ **实测第三种漂移形态：字符串值里写未转义的 ASCII 双引号**
   （``页面标题只是笼统的"腾讯视频"，正文…``）。它的可怕之处是
   **大括号配平**——:func:`~trajectory_pipeline.llm.schema_parse._balanced`
   取出来的那段看起来完全正常，而 ``json.loads`` 报
   ``Expecting ',' delimiter``。**同一句话同一后端，有时犯有时不犯**
   （实测 4 次里坏 1 次），所以这不是「换个模型就好了」。
   共享解析层已做**报错驱动的**转义修复，本模块把修复记录写进
   :attr:`WriteResult.degraded`——**修可以，账要记**。

超长怎么办
==========

``MAX_TEXT_CHARS`` 之外的部分**截断并记账**，不静默丢：
:attr:`WriteResult.degraded` 会带 ``rationale_truncated``，
由 assembler 写进 ``degraded_from``。CoT SFT 吃的是完整轨迹，
半截 CoT 与没有 CoT 是两种不同的坏，而只有记账能让人分出来。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Literal, Mapping, Sequence

from trajectory_pipeline.llm import schema_parse
from trajectory_pipeline.llm.client import LLMClient
from trajectory_pipeline.rationale import gates

# ── 预算 ──────────────────────────────────────────────────────────

#: 喂给模型的正文字数上限。**与
#: :data:`~trajectory_pipeline.perception.llm_perceptor.LLM_BODY_CHARS`
#: 同值同款理由**：实测正文从 1200 降到 600 字符后，原本直接返回 ``None``
#: 的请求恢复正常（大输入会超时，而超时会把站点记成 ``unresolved``——
#: 采集故障写成业务结论）。
#:
#: **刻意不 import 那个常量**：感知层答的是判断题，本模块答的是「写一段
#: 思考」，两者预算分开调才对。共用一个常量会让改一个动两个。
BODY_CHARS = 600

#: 交互元素条数上限。同上，不共用。
ELEMENTS = 20

#: rationale 正文的硬上限（超出截断并记账，见模块 docstring）。
MAX_TEXT_CHARS = 1200

#: 结论字段的硬上限。结论是短句，超了就是模型跑偏而不是内容长。
MAX_CONCLUSION_CHARS = 200

#: 剥完 ``<think>`` 后**仍然**出现即拒写的残留标记。见口径 6。
#:
#: **这张表刻意与 :func:`~trajectory_pipeline.llm.schema_parse.strip_think`
#: 的覆盖范围不重叠。** 第一版把 ``<think>`` / ``<thinking>`` /
#: ``<reasoning>`` 也列进来，实测它们**根本到不了这里**——``strip_think``
#: 连未闭合的 ``<reasoning>`` 都会吃掉，于是残留守卫是**死代码**，
#: 而死守卫比没有更坏：它看起来在守着红线。
#: 由 ``test_writer.py::TestRawCoTRedLine`` 反向盯住这条不重叠。
#:
#: 剩下的是**特殊 token 形态**——Llama-3 / Qwen3 的 ``<|begin_of_thought|>``、
#: gpt-5 系推理档的 ``<|channel|>``，以及社区常见的 ``<scratchpad>`` /
#: ``<reflection>`` / ``<analysis>``。**这些目前在本机后端上没有观测到**，
#: 列在这里是**红线兜底**：raw CoT 不落盘是不可协商的，而这条路径
#: （模型输出第一次变成落盘数据）就是最后一道能拦的地方。
_RESIDUAL_COT = (
    "<|begin_of_thought|>", "<|end_of_thought|>",
    "<|channel|>", "<|message|>", "<|start|>", "<|end|>",
    "<scratchpad>", "</scratchpad>",
    "<reflection>", "</reflection>",
    "<analysis>", "</analysis>",
)

WriteStatus = Literal["written", "unavailable", "empty"]


# ── 值对象 ────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class Draft:
    """一段写好的 rationale。

    ``text`` 是 **refined CoT 的位点**，不是 raw CoT——raw CoT 受 CLAUDE.md
    红线约束不得落盘。落在这里的是模型面向用户的「当时的思考」，
    撰写前已按红线剥净思维链（见 :func:`write_one` 的口径 2、6）。
    """

    text: str
    conclusion: str = ""

    def to_json(self) -> dict[str, Any]:
        return {"text": self.text, "conclusion": self.conclusion}


@dataclass(frozen=True, slots=True)
class WriteResult:
    """一次生成的判定。**三值，且 ``draft`` 可为 ``None``。**

    ``status`` 与 ``draft`` 分开而不是靠 ``draft is None`` 推断
    「为什么没有」——同 :class:`~trajectory_pipeline.rationale.gates.GateResult`
    把 ``status`` 与 ``undeterminable`` 分列是同一个理由：合成一个字段就
    分不出「没跑」与「跑了但跑不出来」。
    """

    status: WriteStatus
    draft: Draft | None = None
    reason: str = ""
    #: 需要传下去给 assembler 的降级标记（如 ``rationale_truncated``）。
    degraded: tuple[str, ...] = ()

    @property
    def written(self) -> bool:
        return self.status == "written" and self.draft is not None

    def to_json(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "rationale": self.draft.to_json() if self.draft else None,
            "reason": self.reason,
            "degraded": list(self.degraded),
        }


# ── 提示词 ────────────────────────────────────────────────────────

#: 系统提示词。**只描述角色与硬约束**，不掺任何执行端实现——
#: 不提 obscura、不提 MCP、不提 ref、不提控制流。理由与
#: ``assembler.schema.SYSTEM_PROMPT`` 同款：模型要学的是判断，
#: 不是怎么驱动某个浏览器。
SYSTEM = (
    "你在帮一位用户找在线观看某部片子的资源。\n"
    "每一步你会拿到：用户想要什么、刚刚执行了什么动作、"
    "执行后页面返回的观察。\n"
    "写这段思考时遵守：\n"
    "1. 只根据观察里真实出现的内容来写。观察里没有的东西不要说。\n"
    "2. 如果观察不完整（页面还没渲染完、正文或元素被截断），"
    "就明说你无法确认，不要编一个听起来合理的解释。\n"
    "3. 写你**当时**的判断依据，不要用事后才知道的说法。\n"
    "4. 简明，200 字以内。\n"
    "输出 JSON：{\"thinking\": \"...\", \"conclusion\": \"...\"}，"
    "thinking 是上述思考，conclusion 是一句话结论。"
)


# ── 观察渲染 ──────────────────────────────────────────────────────


def render(observation: Mapping[str, Any] | None) -> dict[str, Any]:
    """观察字典 → 喂给模型的精简视图。**纯代码**，不吃预算以外的东西。

    刻意**不带 ``ref``**（见模块 docstring 口径 4），也不带 obscura 的
    原始返回体——那里面全是 CSS 与 DOM 碎片，实测会抢光 ``max_chars``
    预算（baidu 首页 6000 字符预算里正文字数为 0，CSS 吃掉 16675）。
    """
    obs = observation or {}
    body = str(obs.get("body_text") or "")
    elements = list(obs.get("interactive_elements") or ())
    out: dict[str, Any] = {
        "url": str(obs.get("url") or ""),
        "page_title": str(obs.get("page_title") or ""),
        "body_text": body[:BODY_CHARS],
        "body_total_chars": len(body),
        "body_truncated": len(body) > BODY_CHARS,
        "elements_total": len(elements),
        "interactive_elements": [
            {"tag": str(e.get("tag") or ""), "label": str(e.get("label") or "")}
            for e in elements[:ELEMENTS]
            if isinstance(e, Mapping)
        ],
        "video_tag_count": int(obs.get("video_tag_count") or 0),
        "iframe_count": int(obs.get("iframe_count") or 0),
    }
    if len(elements) > ELEMENTS:
        out["elements_kept"] = ELEMENTS
    links = list(obs.get("links") or ())
    if links:
        out["links"] = [
            {"text": str(l.get("text") or ""), "href": str(l.get("href") or "")}
            for l in links[:ELEMENTS] if isinstance(l, Mapping)
        ]
        out["links_total"] = len(links)
    return out


def shortfall(observation: Mapping[str, Any] | None) -> str:
    """这份观察**能不能支撑**「某实体存在/不存在」的结论。空串 = 能。

    **直接委托** :func:`trajectory_pipeline.rationale.gates.observation_shortfall`，
    不另写一份：两者一处是「事前告诉模型输入有多薄」、一处是「事后判能不能查」，
    而**「什么算不充分」必须只有一个定义**。分开实现的话，改了一处忘了另一处，
    后果是模型被告知「输入完整」而闸门随后判「查不了」——同一份观察两个口径，
    而两边都不会报错。
    """
    return gates.observation_shortfall(observation)


# ── 动作渲染 ──────────────────────────────────────────────────────


def render_action(action: Mapping[str, Any] | None) -> dict[str, Any]:
    """动作 → 喂给模型的视图。**整条动作里都不许有 ``ref``。**

    ``target`` 是显式挑 ``tag`` / ``label`` 两个字段建的，所以天然没有 ref；
    ``params`` 是**原样透传**的，所以要显式剔掉 ``ref`` 键——真实 P1 的
    ``click`` 把 ref 放在 ``target`` 而 ``params`` 是空的，于是
    「剔 params 里的 ref」这件事**在真实数据上从不触发**。留着是因为
    它让上面那句话为真，而那句话是**红线**，不是描述当前数据长什么样。
    """
    act = action or {}
    out: dict[str, Any] = {"tool": str(act.get("tool") or "")}
    params = {k: v for k, v in (act.get("params") or {}).items()
              if k != "ref"}
    if params:
        out["params"] = params
    target = act.get("target")
    if isinstance(target, Mapping):
        out["target"] = {"tag": str(target.get("tag") or ""),
                         "label": str(target.get("label") or "")}
    return out


def render_history(history: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """前置步的动作摘要。**只给动作，不给观察。**

    给动作是为了别让模型重复已经做过的事、别与上一步的结论自相矛盾；
    **不给观察**是为了控制输入量——前置观察全量进来会让每一步的
    prompt 线性增长，而超时会把整批站点拖成 ``unresolved``。
    """
    return [render_action(a) for a in history if isinstance(a, Mapping)]


# ── 主入口 ────────────────────────────────────────────────────────


def write_one(
    client: LLMClient,
    *,
    action: Mapping[str, Any] | None,
    observation: Mapping[str, Any] | None,
    user_prompt: str = "",
    target_title: str = "",
    history: Sequence[Mapping[str, Any]] = (),
) -> WriteResult:
    """为**一步**生成 rationale。失败一律是返回值，不是异常。

    Args:
        client: 已构造好的客户端。**不自己读环境变量**——调用方决定
            用哪个后端，而 writer 猜一个就是「配置错了但程序照跑」。
        action: 本步实际动作（P1 ``steps[i].action`` 的形状）。
        observation: 本步动作之后的观察。
        user_prompt: 用户诉求原文。
        target_title: 目标片名。**任务给的，不该由模型从页面上找**。
        history: 前置步的动作（只要动作，不要观察，见
            :func:`render_history`）。

    Returns:
        :class:`WriteResult`。``status="unavailable"`` 时 ``draft is None``。
    """
    obs = observation or {}
    gap = shortfall(obs)

    payload: dict[str, Any] = {
        "user_wants": user_prompt,
        "target_title": target_title,
        "previous_actions": render_history(history),
        "this_action": render_action(action),
        "observation": render(obs),
    }
    # 口径 3：**把采集降级明写进提示词**。这是第一道防线，
    # 闸门（gates）只能事后发现，拦不住模型当场编故事。
    payload["observation_completeness"] = (
        f"不完整：{gap}——不要据此断定页面上没有该内容"
        if gap else "完整"
    )

    import json

    raw = client.chat(SYSTEM, json.dumps(payload, ensure_ascii=False))
    if raw is None:
        return WriteResult(
            "unavailable", None,
            f"LLM 不可用（{client.last_error or '未配置或连不上'}）")

    # 口径 2：剥完是空 → 不可用，且**原因要写出来**。
    text = schema_parse.strip_think(raw)
    if not text:
        return WriteResult(
            "unavailable", None,
            "剥掉<think>后正文为空——推理预算被思考吃光或响应被截断，"
            "**不是**「模型没说话」")

    # 口径 6：残留思维链标签即拒写（红线：raw CoT 不进持久化）。
    low = text.lower()
    residual = [m for m in _RESIDUAL_COT if m in low]
    if residual:
        return WriteResult(
            "unavailable", None,
            f"剥净<think>后仍残留思维链标签 {residual}——"
            f"按红线 raw CoT 不落盘，拒写")

    data, repairs = schema_parse.extract_json_reporting(text)
    if data is None:
        return WriteResult(
            "unavailable", None,
            f"正文里抠不出 JSON（前 40 字：{text[:40]!r}）——"
            f"解析失败判不可用，不静默只留散文")

    thinking = schema_parse.as_str(
        schema_parse.field(data, "thinking") or schema_parse.field(data, "text")
    ).strip()
    conclusion = schema_parse.as_str(
        schema_parse.field(data, "conclusion") or schema_parse.field(data, "decision")
    ).strip()
    # 「键在但值是空串」与「键根本没有」必须分开（口径 1）：
    #   前者是**数据事实**——模型这一轮确实什么都没说（``empty``）；
    #   后者是**流水线形状漂移**，整批样本都会缺同一段（``unavailable``）。
    # 合成一个空串就等于把后者记成前者，而前者不进负样本池、
    # 后者会——统计口径直接错。
    if not thinking:
        if "thinking" not in data and "text" not in data:
            return WriteResult(
                "unavailable", None,
                f"JSON 里没有 thinking/text 字段（拿到了 {sorted(data)}）——"
                f"字段漂移，判不可用")
        return WriteResult(
            "empty", None,
            "模型返回了 thinking 字段但内容为空——这是数据事实"
            "（这一轮它什么都没说），不是后端不可用")

    degraded: list[str] = list(repairs)
    if len(thinking) > MAX_TEXT_CHARS:
        thinking = thinking[:MAX_TEXT_CHARS]
        degraded.append("rationale_truncated")
    if len(conclusion) > MAX_CONCLUSION_CHARS:
        conclusion = conclusion[:MAX_CONCLUSION_CHARS]
        degraded.append("conclusion_truncated")

    return WriteResult("written", Draft(thinking, conclusion), "", tuple(degraded))


__all__ = [
    "BODY_CHARS", "ELEMENTS", "MAX_TEXT_CHARS", "MAX_CONCLUSION_CHARS",
    "SYSTEM", "WriteStatus", "Draft", "WriteResult",
    "render", "render_action", "render_history", "shortfall", "write_one",
]
