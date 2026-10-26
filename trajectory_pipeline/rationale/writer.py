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

1. **rationale 是「动作前动机」，不是「事后反思」。** INC46（2026-10-15）
   改口径：生成第 ``i`` 步时**只喂历史**（第 ``0..i-1`` 步的动作与观察）
   与本步动作，**不喂本步观察**。

   旧口径喂的是 ``action[i] + observation[i]``，而 P3 把 ``rationale[i]``
   渲染在 ``tool_call(action[i])`` **之前**——模型还没导航就先评价页面
   内容，训练在教**未卜先知**。实测 ``E005__e35b7455__site-4``：首步是
   ``goto``，rationale 却已经写下「当前页是…越剧特别版…虽列出 8 期链接」。

   **「动作尚未执行」顺带解释了为什么没有 ``error`` 参数**：动作没执行
   谈不上失败，而失败原因本就在 P1 ``steps[i].error`` 落盘，不必再喂一次。

   本步观察不再进提示词，**历史观察仍要进**（不然第 2 步之后的动机无从
   谈起）。控输入量的纪律改成：只最近
   :data:`HISTORY_OBS_STEPS` 步带观察，且用**瘦身形状**
   （:func:`render_history_observation`）——更早的步只留动作。

2. **不可用返回 ``None``，不返回空串。** ``WriteResult.status`` 三值：
   ``written`` / ``unavailable`` / ``empty``。**「LLM 挂了」与「模型什么都没说」
   是两回事**，混成一个空串就等于把采集故障记成数据事实（§3.1）。

3. **``<think>`` 剥完是空串 → 判不可用，不是「模型没说话」。**
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

4. **采集降级必须写进提示词，不能只靠闸门兜。** 闸门是**事后**判，
   而模型是在**当场**编故事。§5.5 那条最贵的实测就发生在这里：
   iqiyi 播放页 ``video=1``、正文与元素皆空，模型写下
   「正文为空、元素为空，说明未渲染」——一句**通顺的错结论**。
   所以本模块把「这份观察不完整：<原因>」直接写进 user 提示词，
   让模型自己知道它的输入有多薄。**闸门是第二道防线，不是第一道。**

   ⚠️ 改动作前动机口径后，**没有本步观察可判了**：这一条改成指
   **最后一条历史观察**的 shortfall（:func:`shortfall`），
   而**没有历史时该键不出现**——首步本来就没有可判的东西，
   写一个「完整」进去等于告诉模型它的输入是充分的，而它只有用户诉求。

5. **喂给模型的动作不带 ``ref``，提示词不提 obscura / MCP / 控制流。**
   ``ref`` 是 obscura 的会话内句柄，换个 session 就失效，进训练数据等于
   让模型学一个随机数（点击目标只给 ``tag`` + ``label``，与
   :class:`~trajectory_pipeline.assembler.schema.ActionView` 同款口径）；
   不提执行端则是让模型学「遇到这样的用户与页面该怎么办」，
   而不是「怎么驱动某个浏览器」——与
   :data:`trajectory_pipeline.assembler.schema.SYSTEM_PROMPT` 同款口径。

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

终局反思：:func:`write_reflection`
=================================

动作前动机把「事后口吻」从第 ``i`` 步赶了出去，但**单元收尾那一句总结
本来就该是事后的**——它要说的是「这一趟下来确认了什么、怎么处置」。
所以它是**另一个入口**，不是给 :func:`write_one` 加一个
``terminal=True`` 开关：两个入口的 SYSTEM 提示词、payload 形状、
语义都不同，合成一个函数会让「本步观察能不能喂」这个最关键的区别
退化成一个布尔参数的副作用——而那条区别正是 INC46 的全部内容。

产物落 **unit 级 ``reflection`` 键**（与 ``steps`` 平级），**不落在
outcome 里**：它是 LLM 产物，而 outcome 要保持采集事实层纯洁。
:class:`WriteResult` 形状不变，所以 ``reflection`` 与 ``steps[i].rationale``
在 P1 里同构，组装期用同一套代码读。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Literal, Mapping, Sequence

from trajectory_pipeline.llm import schema_parse
from trajectory_pipeline.llm.client import LLMClient
from trajectory_pipeline.common.elements import (
    element_keys,
    element_name,
    element_role,
    vocabulary_of_archive_elements,
)
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

#: 历史观察的正文预算。比 :data:`BODY_CHARS` **略小**：本步观察是「要据以
#: 写」的主证据，历史观察只是「别重复做过的事、别自相矛盾」的上下文。
HISTORY_BODY_CHARS = 500

#: **只有最近这么多步带历史观察**，更早的步只留动作。
#:
#: 控输入量的纪律没变（见 :func:`render_history`）：观察全量进来会让每一步
#: 的 prompt 线性增长，而超时会把整批站点拖成 ``unresolved``。放宽成
#: 「最近 5 步」是动作前动机口径的**必要代价**——不喂历史，第二步之后的
#: 动机就无从谈起（「点第一期确认」得先知道上一页列了 8 期）。
HISTORY_OBS_STEPS = 5

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
#:
#: INC46：口径从「事后反思」改成「**动作前动机**」。最关键的一句是
#: **这个动作还没执行**——所以第 2 条「不得声称已看到本步结果」不是
#: 风格要求，而是事实约束：喂进来的观察全是**此前**的，本步结果是
#: 空位，谁填谁就是幻觉。
SYSTEM = (
    "你在帮一位用户找在线观看某部片子的资源。\n"
    "每一步你会拿到：用户想要什么、此前各步的动作与页面返回、"
    "本步**即将执行**的动作。\n"
    "请写执行这个动作**之前**的动机与预期。遵守：\n"
    "1. 只根据**历史**里真实出现的内容与任务要求来写。"
    "历史里没有的东西不要说。\n"
    "2. 这个动作**还没有执行**，你不可能知道它返回什么。"
    "**不得**描述或评价本步动作的結果，不得声称已经看到了本步的页面。\n"
    "3. 最近的历史观察不完整时（页面没渲染完、正文或元素被截断），"
    "就明说你不确定，不要编一个听起来合理的解释。\n"
    "4. 说清楚**为什么要做这个动作、预期能看到什么**，不要用事后"
    "复盘的说法（「结果显示」「已经确认」「页面写着」一类）。\n"
    "5. 简明，200 字以内。\n"
    "输出 JSON：{\"thinking\": \"...\", \"conclusion\": \"...\"}，"
    "thinking 是上述思考，conclusion 是一句话结论"
    "（结论是**预期**，不是既成事实）。"
)

#: 终局反思的系统提示词（:func:`write_reflection`）。
#:
#: 与 :data:`SYSTEM` 唯一的区别是**时序立场反过来**：这里**允许且要求**
#: 事后口吻——它写的是「这一趟下来确认了什么」，而所有观察都已经是
#: 历史了。刻意复制一份而不是在 :data:`SYSTEM` 上打补丁：两份提示词
#: 会被独立调，而共用一个字符串 + 条件拼接会让「动作前」那版的第 2 条
#: 硬约束在某个分支下悄悄消失。
SYSTEM_REFLECTION = (
    "你在帮一位用户找在线观看某部片子的资源。\n"
    "这一轮访问已经结束。你会拿到：用户想要什么、各步的动作与页面返回、"
    "以及最终结论。\n"
    "写一段**事后总结**：这一趟下来确认了什么、为什么这么处置。遵守：\n"
    "1. 只根据历史里真实出现的内容与给出的最终结论来写，不要补新事实。\n"
    "2. 历史观察不完整时明说不确定，不要把「没采到」讲成「页面上没有」。\n"
    "3. 简明，200 字以内。\n"
    "输出 JSON：{\"thinking\": \"...\", \"conclusion\": \"...\"}，"
    "thinking 是总结，conclusion 是一句话结论。"
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
    # 键名跟着**存档的方言**走（P4），且**只搬键名不翻译值**：
    # 写死 ``tag``/``label`` 会让 camoufox 批次的元素在提示词里变成
    # 两列空串——模型看到「有元素、没名字」，于是把控件名判成幻觉。
    vocabulary = vocabulary_of_archive_elements(elements)
    kind_key, text_key = element_keys(vocabulary)
    out: dict[str, Any] = {
        "url": str(obs.get("url") or ""),
        "page_title": str(obs.get("page_title") or ""),
        "body_text": body[:BODY_CHARS],
        "body_total_chars": len(body),
        "body_truncated": len(body) > BODY_CHARS,
        "elements_total": len(elements),
        "interactive_elements": [
            {kind_key: element_role(e), text_key: element_name(e)}
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
        kind_key, text_key = element_keys(vocabulary_of_archive_elements([target]))
        out["target"] = {kind_key: element_role(target),
                         text_key: element_name(target)}
    return out


#: 一条历史步。``(动作, 观察)`` 二元组；观察可为 ``None``（那一步没取到）。
HistoryItem = tuple[Mapping[str, Any], Mapping[str, Any] | None]

#: 观察的**瘦身形状**——只留页面身份、正文摘要与元素计数。
#: 刻意在 :func:`render` 之外单独一份：:func:`render` 是「本步主证据」
#: 的形状（带 elements/links 明细、带 video/iframe 计数），历史只是上下文，
#: 带明细会让第 5 步之后的 prompt 膨胀到超时——而超时会把整批站点
#: 记成 ``unresolved``（采集故障写成业务结论）。
_SLIM_OBS_KEYS = ("url", "page_title", "body_text",
                  "body_truncated", "elements_total")


def render_history_observation(observation: Mapping[str, Any] | None) -> dict[str, Any]:
    """历史观察 → 瘦身视图。**不带 elements / links 明细**（见常量注）。"""
    obs = observation or {}
    body = str(obs.get("body_text") or "")
    elements = list(obs.get("interactive_elements") or ())
    total = int(obs.get("elements_total") or 0) or len(elements)
    return {
        "url": str(obs.get("url") or ""),
        "page_title": str(obs.get("page_title") or ""),
        "body_text": body[:HISTORY_BODY_CHARS],
        "body_truncated": len(body) > HISTORY_BODY_CHARS,
        "elements_total": total,
    }


def _split_history_item(item: Any) -> tuple[Mapping[str, Any] | None,
                                            Mapping[str, Any] | None]:
    """一条历史项 → ``(动作, 观察)``。**认两种形状**：

    - ``(action_json, obs_json)`` 二元组（orchestrator 现在给的形态）；
    - 裸 ``action_json``（老调用方 / 测试给的动作序列）。

    认裸映射不是「兼容历史包袱」：重生成 CLI 与单测都会直接喂动作列表，
    而让每个调用方自己包一层 ``(a, None)`` 只是把同一件事写三遍。
    """
    if isinstance(item, Mapping):
        return item, None
    if (
        isinstance(item, Sequence)
        and not isinstance(item, (str, bytes))
        and len(item) == 2
    ):
        action, obs = item[0], item[1]
        return (action if isinstance(action, Mapping) else None,
                obs if isinstance(obs, Mapping) else None)
    return None, None


def render_history(history: Sequence[Any]) -> list[dict[str, Any]]:
    """前置步摘要：**最近** :data:`HISTORY_OBS_STEPS` 步带瘦身观察，更早只留动作。

    INC46 之前是「只给动作，不给观察」——那是**事后反思**口径下的正确
    取舍（本步观察就在手边，历史只是防重复）。改成动作前动机之后，
    历史观察是**唯一的证据来源**：不喂它，「点第一期确认」这种动机
    就写不出来，模型只能对着任务复述一遍。

    代价是 prompt 会随步数增长，所以两头收紧：观察用瘦身形状
    （:func:`render_history_observation`），且**只有最近 5 步带**。
    更早的步只留动作——防重复做过的事不需要 10 步之前的正文。
    """
    items = list(history or ())
    # 观察窗口的起点：**末尾**往前数，不是从头数。动机依赖的是最近看到
    # 的东西，而截掉开头的旧观察不会让模型失去「现在在哪」的上下文。
    first_with_obs = max(0, len(items) - HISTORY_OBS_STEPS)
    out: list[dict[str, Any]] = []
    for i, item in enumerate(items):
        action, obs = _split_history_item(item)
        if action is None:
            continue
        entry = render_action(action)
        if obs is not None and i >= first_with_obs:
            entry["observation"] = render_history_observation(obs)
        out.append(entry)
    return out


def last_history_observation(
    history: Sequence[Any],
) -> Mapping[str, Any] | None:
    """最后一条**非空**的历史观察。没有则 ``None``。

    用途是 :func:`shortfall` 的输入（模块 docstring 口径 4）：把「最近这份
    观察不完整」写进提示词。取**最后一条非空**而非「最后一步的观察」
    是因为末步可能是 ``new_tab`` 那种观察为 null 的步——那时真正该提示的
    是再往前那条。
    """
    for item in reversed(list(history or ())):
        _, obs = _split_history_item(item)
        if obs:
            return obs
    return None


# ── 主入口 ────────────────────────────────────────────────────────


def write_one(
    client: LLMClient,
    *,
    action: Mapping[str, Any] | None,
    user_prompt: str = "",
    target_title: str = "",
    history: Sequence[Any] = (),
) -> WriteResult:
    """为**一步**生成「动作前动机」。失败一律是返回值，不是异常。

    INC46：**不喂本步观察**。喂了它，模型就会描述一个它此刻还没看到的
    页面，而 P3 把这段文本渲染在 ``tool_call`` 之前——那是在教未卜先知。

    Args:
        client: 已构造好的客户端。**不自己读环境变量**——调用方决定
            用哪个后端，而 writer 猜一个就是「配置错了但程序照跑」。
        action: 本步**即将执行**的动作（P1 ``steps[i].action`` 的形状）。
        user_prompt: 用户诉求原文。
        target_title: 目标片名。**任务给的，不该由模型从页面上找**。
        history: 前置步，``(动作, 观察)`` 序列，见 :func:`render_history`。
            观察**只最近** :data:`HISTORY_OBS_STEPS` 步带、且是瘦身形状。

    Returns:
        :class:`WriteResult`。``status="unavailable"`` 时 ``draft is None``。
    """
    payload: dict[str, Any] = {
        "user_wants": user_prompt,
        "target_title": target_title,
        "previous_actions": render_history(history),
        "this_action": render_action(action),
    }
    # 口径 4：**把采集降级明写进提示词**。这是第一道防线，
    # 闸门（gates）只能事后发现，拦不住模型当场编故事。
    # 改动作前动机后它指**最后一条历史观察**——没有历史时**不出现该键**：
    # 首步本来就没有可判的观察，写一个「完整」等于谎报输入充分。
    last_obs = last_history_observation(history)
    if last_obs is not None:
        gap = shortfall(last_obs)
        payload["observation_completeness"] = (
            f"不完整：{gap}——不要据此断定页面上没有该内容"
            if gap else "完整"
        )
    return _complete(client, SYSTEM, payload)


def write_reflection(
    client: LLMClient,
    *,
    history: Sequence[Any] = (),
    outcome: Mapping[str, Any] | None = None,
    user_prompt: str = "",
    target_title: str = "",
) -> WriteResult:
    """为一个**决策单元**写终局反思，落 unit 级 ``reflection`` 键。

    这是全链路**唯一**允许事后口吻的地方（见模块 docstring「终局反思」节）：
    动作前动机把后见之明从第 ``i`` 步赶了出去，而收尾那句总结本来就该
    是看完整个单元之后写的。

    Args:
        client: 同 :func:`write_one`。
        history: 本单元**全部**步的 ``(动作, 观察)`` 序列。观察窗口规则
            同 :func:`render_history`（最近 5 步带瘦身观察）——终局总结
            依赖的也是最近看到的东西。
        outcome: 本单元的最终判定（P1 ``outcomes[i]`` / 站点级分支）。
            **只取几个键**（:func:`render_outcome`），不整份透传：
            evidence 原文很长且是给评分器看的，喂进提示词只会挤掉正文预算。
        user_prompt: 用户诉求原文。
        target_title: 目标片名。

    Returns:
        同 :func:`write_one` 的 :class:`WriteResult`——形状**刻意一致**，
        于是 P1 里的 ``reflection`` 与 ``steps[i].rationale`` 同构，
        组装期用同一套代码读。
    """
    payload: dict[str, Any] = {
        "user_wants": user_prompt,
        "target_title": target_title,
        "previous_actions": render_history(history),
        "final_outcome": render_outcome(outcome),
    }
    return _complete(client, SYSTEM_REFLECTION, payload)


def render_outcome(outcome: Mapping[str, Any] | None) -> dict[str, Any]:
    """最终判定 → 喂给模型的视图。**只留结论骨架**，不带 evidence 原文。

    ``outcome`` 可能为 ``None``——**搜索单元没有 outcome**（它的「总结」
    就是「接下来逐一打开候选站点核验」）。此时返回空 dict，让提示词里
    的 ``final_outcome`` 是 ``{}`` 而不是缺键：缺键与「有判定但为空」
    在模型眼里是两回事，而这里要说清的是「这一单元没有站点级结论」。
    """
    o = outcome if isinstance(outcome, Mapping) else {}
    if not o:
        # 搜索单元没有站点级 outcome。返回**空 dict** 而不是「branch=None」：
        # ``branch=None`` 在 P1 里的含义是**成功**（ledger 只记失败分支），
        # 拿它表示「这一单元压根没有判定」会让模型把「还没判」读成「判了成功」。
        return {}
    out: dict[str, Any] = {}
    branch = o.get("branch")
    # branch=None 是**成功**（P1 的 ledger 只记失败分支），
    # 所以这里照实放 None 并显式补一句，别让模型把「无分支」读成「没结论」。
    out["branch"] = str(branch) if branch is not None else None
    out["branch_is_none_means"] = "无失败分支 = 判定为可播放站点" if branch is None else ""
    for key in ("url", "play_page_url", "succeeded", "reached_play_page"):
        if key in o:
            out[key] = o[key]
    return out


def _complete(client: LLMClient, system: str, payload: dict[str, Any]) -> WriteResult:
    """:func:`write_one` / :func:`write_reflection` 共用的收尾。

    抽出来不是为省几行：两个入口之后的**每一步处置**（剥 think、拒残留
    CoT、解析、三态记账）必须**完全一致**，否则「反思缺一个字段」与
    「动机缺一个字段」会在下游长出两套形状，而 reflection 是要被
    ``views._closing_text`` 直接读的。
    """
    import json

    raw = client.chat(system, json.dumps(payload, ensure_ascii=False))
    if raw is None:
        return WriteResult(
            "unavailable", None,
            f"LLM 不可用（{client.last_error or '未配置或连不上'}）")

    # 口径 3：剥完是空 → 不可用，且**原因要写出来**。
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
    # 「键在但值是空串」与「键根本没有」必须分开（口径 2）：
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
    "BODY_CHARS", "ELEMENTS", "HISTORY_BODY_CHARS", "HISTORY_OBS_STEPS",
    "MAX_TEXT_CHARS", "MAX_CONCLUSION_CHARS",
    "SYSTEM", "SYSTEM_REFLECTION", "WriteStatus", "Draft", "WriteResult",
    "render", "render_action", "render_history", "render_history_observation",
    "render_outcome", "last_history_observation", "shortfall",
    "write_one", "write_reflection",
]
