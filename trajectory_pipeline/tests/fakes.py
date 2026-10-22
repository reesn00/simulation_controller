"""测试替身——**假 LLM 后端**。

⚠️ 放这里而不是 ``tests/unit/`` 或 ``tests/contract/``：那两个子目录
**没有** ``__init__.py``（它们的测试文件按 basename 加载），跨目录绝对
导入会 ``ImportError``。``tests/`` 本身有，所以 ``trajectory_pipeline.tests.fakes``
是唯一能被两处引用的位置。

关于 fake 的定位（决定它「答得对」还是「乱答」）
----------------------------------------------
:class:`FakeLLM` **模拟一个业务上答得对的模型**：页面上有播放控件就
回一个真实 ref，没有就回 ``null``。契约测试（七不变式）验的是
:class:`~trajectory_pipeline.perception.llm_perceptor.LLMPerceptor`
的**代码层守卫**——采集预检、ref 白名单、fail-closed——而 LLM 的语义
能力是另一件事，只能靠真实验证测（见 ``docs/02-避坑指南.md``）。
若 fake 随机乱答，契约测试就变成测「LLM 版能不能处理乱答」，
与七不变式无关，且会掩盖真正要测的东西。

**真实的坏行为由独立开关模拟**，见各 ``mode`` 参数——它们不是「让 fake
变傻」，而是复现 ``trajectory_pipeline/perception/probe_llm.py`` 里实测到的后端行为：
套 ``<think>``、套 markdown fence、输出散文、编造 ref。
"""

from __future__ import annotations

import json
from typing import Any

from trajectory_pipeline.llm.client import ChatResult
from trajectory_pipeline.perception import llm_perceptor as lp
from trajectory_pipeline.perception.questions import CLASSIFY_CATEGORIES
from trajectory_pipeline.common.elements import (
    element_name,
    element_role,
    vocabulary_of_archive_elements,
)
from trajectory_pipeline.perception.rule_perceptor import (
    SEARCH_BOX_KEYWORDS,
    SEARCH_BOX_WORDS,
    classify_trailer,
    is_play_control,
)

#: 多轮形态下 :meth:`FakeLLM.chat_messages` 回的 token 计数（方案 12
#: A2a-1）。刻意**取固定的整数**而不是随机值：账本断言要能写出确定值，
#: 而一个会跳的数字只会逼着断言退化成「> 0」这种恒真式。
_FAKE_USAGE: dict[str, int] = {"prompt_tokens": 128, "completion_tokens": 32}


class FakeLLM:
    """假后端。接口与 :class:`trajectory_pipeline.llm.client.LLMClient` 一致。"""

    def __init__(
        self,
        *,
        down: bool = False,
        think: bool = False,
        think_text: str = "让我看看页面上有什么",
        think_only: bool = False,
        fence: bool = False,
        prose: bool = False,
        lie_about_ref: str = "",
        invent_urls: bool = False,
        reject_all: bool = False,
        no_conclusion: bool = False,
        by_index: bool = False,
        forget_ref: bool = False,
        answers: dict[str, bool] | None = None,
        budget_exhausted: bool = False,
        answer_null: bool = False,
        answer_missing: bool = False,
        answer_garbage: Any = None,
        # 2026-10-12 增量十七 · P4a 第二轮：让端到端测试**控制** LLM 在
        # ``IS_REACHABLE`` / ``PLAYER_OK`` 时回传 ``page_quotes`` 字段
        # ——``page_quotes`` 是 P4a 改造后 fallback 的**唯一**输入源，必须能
        # 被端到端测试驱动才能验「LLM 给了 page_quotes → fallback 触发」。
        # 默认 None / 空串以**不破坏**现有 ``TestBadOutputs`` 等用默认 FakeLLM
        # 的测试。
        page_quotes: list[str] | None = None,
        # IS_REACHABLE 用的 ``access_control_type``——空串表示"我没给"。
        access: str = "",
        # bool 题（IS_REACHABLE / PLAYER_OK / IS_AGGREGATOR）的 **evidence**
        # ——空串 = 用默认那句「页面主体可见且可交互」。
        # 2026-10-21（H-16 修复①）新增：让位规则要能验「**模型已经诊断出
        # 更具体的访问形态**时不改判订阅墙」——模型的诊断只写在 evidence
        # 里（③ 没有结构化字段），而 evidence 以前是写死的一句，造不出
        # 「模型判出地区限制」这个形态。
        evidence: str = "",
        # PLAYER_OK 用的 ``paywall_type``——空串表示"我没给"。
        paywall_type: str = "",
        # 方案 12 阶段 A1 · 分类任务的显式类别表：``{href: 类别}``
        # 覆盖默认的关键词规则——端到端测试靠它驱动三条路径的分发
        # （谁保留、谁记负、谁 unresolved），不靠碰巧的 URL 措辞。
        classify: dict[str, str] | None = None,
        # 方案 12 A2a-1 · 多轮形态：``chat_messages`` 返回的 ``usage``。
        # 默认**给**（真 OpenAI 兼容后端会回 token 计数）；置 True 模拟
        # 「后端没给 usage」——``llm_calls[]`` 账本对这种情况必须记
        # ``None`` 而不是 0，两种事实在成本对比上是两回事。
        no_usage: bool = False,
    ) -> None:
        #: 后端整体不可用（连接失败/超时）
        self.down = down
        #: 输出套 ``<think>`` 包裹（**raw CoT 红线的实测形态**）
        self.think = think
        #: think 段的内容。**断言时要认得出它**——默认那句太通用，
        #: 「evidence 里没有『让我看看』」这种断言可能被别的路径误满足。
        self.think_text = think_text
        #: **只输出思考、没有正文**——推理模型的另一种实测形态
        self.think_only = think_only
        #: 输出套 ```json 代码块
        self.fence = fence
        #: 输出散文而非 JSON
        self.prose = prose
        #: 回传这个 ref（无论它是否真的存在）——验证白名单守卫
        self.lie_about_ref = lie_about_ref
        #: ① 里编造不在观察中的 url
        self.invent_urls = invent_urls
        self.reject_all = reject_all
        #: ① 的**实测形态**：selected 与 rejected **都留空**。
        #: 实测（2026-10-10 T035、2026-10-11 E001）两个判断点①连续两次
        #: 返回空数组，而**此前所有 fixture 都不产生这个形态**——
        #: 于是「没作答」这条路径从未被断言过，evidence 才会写成
        #: 「全部不在观察的 N 个链接里」这种字面为真、语义指错的句子。
        self.no_conclusion = no_conclusion
        #: ① 按新契约**回编号 i** 而不是回 URL（2026-10-11 起的主路径）
        self.by_index = by_index
        #: ③ **看得到控件却不回 ref**——实测 v.qq.com 就是这样丢掉整条轨迹的
        self.forget_ref = forget_ref
        #: 逐题强制答案（键为 question id）
        self.answers = answers or {}
        #: **推理模型的实测形态**：预算被思考吃光，正文为空。
        #: 报 ``budget:`` 而不是 ``shape:``——两者的下一步动作完全不同，
        #: 而混起来时症状只是「拿不到内容」。
        self.budget_exhausted = budget_exhausted
        #: ``answer`` 显式给 null——**这是提示词里写明的合规答法**
        #: （「证据不足时 answer 给 null」），不是输出坏了。两者都落
        #: ``unresolved``，但人工复核该采取的动作不同，故要能分别模拟。
        self.answer_null = answer_null
        #: ``answer`` 整个字段不出现
        self.answer_missing = answer_missing
        #: ``answer`` 给了个既非布尔也非 null 的东西
        self.answer_garbage = answer_garbage
        # 2026-10-12 增量十七：见上
        self.page_quotes = page_quotes
        self.access = access
        self.evidence = evidence
        self.paywall_type = paywall_type
        #: 分类任务的显式类别表（方案 12 阶段 A1，见构造参数说明）
        self.classify = classify
        #: 多轮形态下 ``usage`` 给不给（方案 12 A2a-1，见构造参数说明）
        self.no_usage = no_usage
        #: 记录每次请求，供断言「问了几次、system 里带了什么」
        self.calls: list[tuple[str, str]] = []
        #: 每次请求实际带的 ``max_tokens``（None = 走 config）
        self.budgets: list[int | None] = []
        #: 与真客户端同名字段：失败原因
        self.last_error = ""

    # ── 接口 ──────────────────────────────────────────────────────

    def chat(self, system: str, user: str, *, max_tokens: int | None = None) -> str | None:
        #: 记下**每次调用实际带的预算**。签名与真客户端一致（默认 None），
        #: 于是「生产调用点是否还在自带数字」这件事可以被断言——
        #: 曾经写死 600，而那个数字的错误症状是「正文为空」，
        #: 看着像模型判不了，谁也不会想到去改它。
        self.calls.append((system, user))
        self.budgets.append(max_tokens)
        content, error = self._respond(user)
        if error:
            self.last_error = error
        return content

    def chat_messages(
        self,
        messages: list[dict[str, Any]],
        *,
        max_tokens: int | None = None,
    ) -> ChatResult:
        """多轮形态的同签名方法（方案 12 A2a-1，agent 循环用）。

        取**首条 system + 末条 user** 组装记账，正文交给与 :meth:`chat`
        同一份 :meth:`_respond`——于是十一道判断题与分类任务的应答逻辑、
        全部坏行为开关一行不改就能在多轮形态下复用。

        ``latency_ms`` 恒为 ``0``：fake 不测真实耗时（那是
        ``time.perf_counter`` 的事），而一个会跳的数字只会让断言不可确定。
        """
        system = ""
        user = ""
        for m in messages or ():
            role = m.get("role")
            if role == "system" and not system:
                system = str(m.get("content", ""))
            elif role == "user":
                user = str(m.get("content", ""))
        self.calls.append((system, user))
        self.budgets.append(max_tokens)
        content, error = self._respond(user)
        if error:
            self.last_error = error
        return ChatResult(
            content=content,
            usage=None if (self.no_usage or content is None) else dict(_FAKE_USAGE),
            latency_ms=0,
            error=error,
        )

    def health(self) -> tuple[bool, str]:
        return (not self.down), "fake 后端"

    # ── 应答生成（chat / chat_messages 共用） ─────────────────────

    def _respond(self, user: str) -> tuple[str | None, str]:
        """按 ``user`` 正文生成应答，返回 ``(原文, 错误码)``。

        抽出来是为了 :meth:`chat` 与 :meth:`chat_messages` **共用一份**
        「题号 dispatch + 坏行为开关」逻辑：两份就会分叉，而分叉的后果是
        「某个开关只在单轮形态下生效」——它要等真实批次里那条路径第一次
        被走到才暴露，那时已经分不清是模型的问题还是 fake 的问题。
        """
        if self.down:
            return None, "transport:ConnectError"
        qid, obs = _request(user)
        if qid is None:
            return None, "shape:KeyError"
        if self.budget_exhausted:
            return None, (
                "budget:max_tokens 用尽（finish_reason=length，content 为空）；"
                "调大 TRAJECTORY_LLM_MAX_TOKENS"
            )
        payload = self._build(qid, obs)
        payload = self._shape_answer(payload)
        # ⚠️ **think 包装必须在 prose 之后统一套，不能在 prose 分支提前
        # return**。原来的写法让 ``think=True, prose=True`` 造不出
        # 「带思考且正文不是 JSON」的形态，于是 `_why_unparsable` 里那处
        # **raw CoT 落盘**的红线破口测试了几个月都没人发现——
        # 不是没有测试守着，是**测试造不出这个形态**。
        raw = _prose_for(qid) if self.prose else json.dumps(payload, ensure_ascii=False)
        if self.think:
            raw = f"<think>{self.think_text}</think>\n{raw}"
        if self.think_only:
            return f"<think>{self.think_text}</think>", ""
        if self.fence:
            raw = f"```json\n{raw}\n```"
        return raw, ""

    # ── 各题应答 ──────────────────────────────────────────────────

    def _shape_answer(self, payload: dict[str, Any]) -> dict[str, Any]:
        """按开关把 ``answer`` 改成 null / 删掉 / 换成非布尔值。

        只作用于 bool 型输出（``IS_REACHABLE`` / ``PLAYER_OK``）——
        ``SELECT_PLAY_SITES`` 与 ``FIND_PLAY_CONTROL`` 的契约里没有 answer。
        判不出来时补一句可辨认的自述，用来验证**模型的 rationale 没有
        在 fail-closed 路径上被丢掉**。
        """
        if self.answer_garbage is not None:
            payload["answer"] = self.answer_garbage
            payload["evidence"] = "页面主体为空"
            return payload
        if self.answer_null:
            payload["answer"] = None
            payload["evidence"] = "页面主体为空"
            return payload
        if self.answer_missing:
            payload.pop("answer", None)
            return payload
        return payload

    def _build(self, qid: str, obs: dict[str, Any]) -> dict[str, Any]:
        if qid == lp.Q.SELECT_PLAY_SITES:
            links = obs.get("links") or []
            if self.invent_urls:
                return {"selected": [{"url": "https://编造.test/x",
                                      "title": "编的", "why": "看着像"}],
                        "rejected": []}
            if self.no_conclusion:
                # 实测形态：两个数组都空。**排在 invent_urls 之前也要分清
                # 「没作答」与「作答了但地址全是编的」**——判据不同。
                return {"selected": [], "rejected": []}
            if self.reject_all:
                return {"selected": [],
                        "rejected": [{"url": l.get("href", ""),
                                      "reason": "不是这个作品的播放站"}
                                     for l in links[:3]]}
            if not links:
                return {"selected": [], "rejected": []}
            if self.by_index:
                # 新契约：回编号 i，不回 URL。字符串形态一并覆盖
                # （模型爱给整数加引号）。
                return {"selected": [{"i": str(0), "title": links[0].get("text", ""),
                                      "why": "标题含目标片名"}],
                        "rejected": [{"i": 1, "reason": "导航链接"}]}
            return {"selected": [{"url": links[0].get("href", ""),
                                  "title": links[0].get("text", ""),
                                  "why": "标题含目标片名"}],
                    "rejected": []}
        if qid == lp.Q.FIND_PLAY_CONTROL:
            if self.lie_about_ref:
                return {"ref": self.lie_about_ref, "trailer_only": False}
            if self.forget_ref:
                # 实测形态：页面上有控件，模型却回空 ref。**必须在
                # 「扫元素找控件」之前返回**，否则这个形态造不出来——
                # 而它正是 2026-10-11 丢掉 v.qq.com 整条轨迹的那个形态。
                return {"ref": None, "trailer_only": False,
                        "why": "页面上有播放按钮，但我没回传 ref"}
            for e in obs.get("interactive_elements") or []:
                if is_play_control(e.get("label", "")):
                    return {"ref": e.get("ref", ""), "trailer_only": False,
                            "why": f"控件文本 {e.get('label','')!r}"}
            return {"ref": None, "trailer_only": False, "why": "未见播放控件"}
        # 2026-10-16 路径 B/C · ⑦ 站内搜索框：与 ③ 同款——页面上有
        # 搜索框就回真实 ref，没有就回 null。匹配用词表同源常量
        # （SEARCH_BOX_*），不另写一份判定。
        if qid == lp.Q.FIND_SITE_SEARCH:
            if self.lie_about_ref:
                return {"ref": self.lie_about_ref, "why": "看着像搜索框"}
            if self.forget_ref:
                # 与 ③ 同款实测形态：看得到框却不回 ref——检验词表兜底
                return {"ref": None, "why": "页面上有搜索框，但我没回传 ref"}
            elements = obs.get("interactive_elements") or []
            words = SEARCH_BOX_WORDS[vocabulary_of_archive_elements(elements)]
            for e in elements:
                kind = element_role(e).lower()
                label = " ".join(element_name(e).split()).replace(" ", "").lower()
                if (
                    kind in words
                    and label
                    and any(kw in label for kw in SEARCH_BOX_KEYWORDS)
                ):
                    return {"ref": e.get("ref", ""),
                            "why": f"搜索框 {element_name(e)!r}"}
            return {"ref": None, "why": "未见站内搜索框"}
        # 2026-10-16 路径 B/C · 四道 SELECT_*：与 ① 同款——按编号回指，
        # 复用同一批坏行为开关（invent_urls / no_conclusion / reject_all）。
        # reject_all 在 ①′ 两题（搜索页选题）表现为「没结论」，在 ⑥′ 两题
        # （单页选链接）表现为「真结论：本页没有入口」——同一开关驱动
        # 两种口径，正好把 handler 的分叉两侧都测到。
        if qid in (
            lp.Q.SELECT_AGGREGATOR_RESULTS,
            lp.Q.SELECT_SITE_ENTRIES,
            lp.Q.SELECT_ARTICLE_RESULTS,
            lp.Q.SELECT_REFERRAL_LINKS,
        ):
            links = obs.get("links") or []
            if self.invent_urls:
                return {"selected": [{"url": "https://编造.test/x",
                                      "why": "看着像"}],
                        "rejected": []}
            if self.no_conclusion:
                return {"selected": [], "rejected": []}
            if self.reject_all:
                return {"selected": [],
                        "rejected": [{"i": n, "reason": "不是正式入口"}
                                     for n, _ in enumerate(links[:3])]}
            if not links:
                return {"selected": [], "rejected": []}
            selected = [{"i": 0, "title": links[0].get("text", ""),
                         "why": "看着像目标"}]
            rejected = (
                [{"i": 1, "reason": "导航链接"}] if len(links) > 1 else []
            )
            return {"selected": selected, "rejected": rejected}
        # 方案 12 阶段 A1 · 分类任务：模拟「业务上答得对」的模型——
        # 知乎/豆瓣/微博归 article_post、聚合/导航词归 aggregator、
        # 新闻/下载词归 noise、其余归 video_site；``classify`` 显式表
        # 可逐链覆盖。坏行为开关的映射：``no_conclusion`` = 四数组全空
        # （没作答）；``invent_urls`` = 条目全部编造（对不上观察）；
        # ``reject_all`` = 全部归 noise（有作答但都不是目标类——
        # 分类任务里「kept=0 且逐条点名」的形态）。
        if qid == lp.Q.CLASSIFY_SEARCH_LINKS:
            links = obs.get("links") or []
            if self.no_conclusion:
                return {cls: [] for cls in CLASSIFY_CATEGORIES}
            if self.invent_urls:
                return {
                    cls: (
                        [{"url": "https://编造.test/x", "title": "编的",
                          "why": "看着像"}]
                        if cls == "noise" else []
                    )
                    for cls in CLASSIFY_CATEGORIES
                }
            if self.reject_all:
                return {
                    cls: (
                        [{"i": n, "title": l.get("text", ""),
                          "why": "不是播放站"} for n, l in enumerate(links)]
                        if cls == "noise" else []
                    )
                    for cls in CLASSIFY_CATEGORIES
                }
            out: dict[str, list[dict[str, Any]]] = {
                cls: [] for cls in CLASSIFY_CATEGORIES
            }
            for n, link in enumerate(links):
                href = str(link.get("href", ""))
                text = str(link.get("text", ""))
                blob = f"{href} {text}".lower()
                cls = (self.classify or {}).get(href)
                if cls is None:
                    if any(k in blob for k in
                           ("zhihu", "douban", "weibo", "知乎", "豆瓣", "微博")):
                        cls = "article_post"
                    elif any(k in blob for k in ("聚合", "导航", "nav", "大全")):
                        cls = "aggregator"
                    elif any(k in blob for k in ("新闻", "news", "下载", "download")):
                        cls = "noise"
                    else:
                        cls = "video_site"
                out[cls].append({"i": n, "title": text,
                                 "why": f"形态判定：{cls}"})
            return out
        # IS_REACHABLE / PLAYER_OK / IS_AGGREGATOR（⑤ 与前两者同形：
        # bool + evidence，走同一条通用应答；answers 开关可逐题覆盖）
        # 2026-10-12 增量十七 · P4a 第二轮：让 FakeLLM 能在 IS_REACHABLE /
        # PLAYER_OK 时**真**回 page_quotes / access / paywall_type——
        # 这样端到端测试就能驱动「LLM 给了 page_quotes → fallback 触发」
        # 这条路径。已有 ``answer_*`` / ``answers`` 标志仍按惯例处理，
        # 下面只在默认行为（无 answer_* 干预）下加这些字段。
        payload: dict[str, Any] = {
            "answer": self.answers.get(qid, True),
            "evidence": self.evidence or "页面主体可见且可交互",
        }
        # page_quotes 仅当显式给了非 None list 时才进 payload——保持默认
        # FakeLLM 与 P4a 之前测试同形（payload 无 page_quotes 字段），
        # 同时让端到端测试可以"塞" quotes 触发 fallback。
        if self.page_quotes is not None:
            payload["page_quotes"] = list(self.page_quotes)
        # access_control_type / paywall_type：给空串视为"未给"；给非空就
        # 透传——端到端测试可分别验"LLM 给了 + access 齐" 与 "LLM 给了
        # page_quotes 但 access 字段空，让 fallback 反推"两种姿势。
        if self.access:
            payload["access_control_type"] = self.access
        if self.paywall_type:
            payload["paywall_type"] = self.paywall_type
        return payload

    def close(self) -> None:
        pass


# ═══════════════════════════════════════════════════════════════════════
# agent 循环的假后端（方案 12 A2a-2 / 方案 13 §3.9）
# ═══════════════════════════════════════════════════════════════════════


class FakeAgentLLM:
    """agent_loop 的假后端。接口与 :meth:`LLMClient.chat_messages` 同签名。

    为什么与 :class:`FakeLLM` 分开而不是给它加开关：那边的 dispatch 是
    「题号 → 答案」，这边是「第几步 → 动作指令」。两种驱动逻辑塞进一个
    类，开关之间就会出现「哪个先生效」的隐式优先级。

    **「答得对」的基准是按脚本走。** 与 FakeLLM 同哲学——测试不靠碰巧的
    URL 措辞：导航路径由 :data:`script` 显式给定，断言写的就是「这条
    路径产出了什么轨迹」，而不是「模型今手感好不好」。

    坏行为开关复现的是**模型真的会犯的错**，不是让 fake 变傻：编 ref
    （I6 的攻击面）、吐散文（格式契约）、卡在原地不结束（死循环）、
    先错一步再修回来。最后一条尤其重要——D-A2 删除 L1 静默自愈之后，
    「被拦 → 修复 → 重试」这条链本身就是**要教给模型的训练信号**。
    """

    def __init__(
        self,
        script: list[dict[str, Any]] | None = None,
        *,
        bad_json: bool = False,
        invent_ref: bool = False,
        invent_url: bool = False,
        never_done: bool = False,
        flaky: int = 0,
        down: bool = False,
        no_usage: bool = False,
        model: str = "fake-agent",
        kind: str = "play",
    ) -> None:
        #: 逐步的导航脚本。每条是 wire format 的字段（``tool`` / ``ref`` /
        #: ``params``），**可含显式的终止条目** ``{"done": True, "report":
        #: {...}}``。走完就默认报「到了」——与 FakeLLM 默认答 True 同款：
        #: 默认态是「业务上走得通」，测别的东西时不用先排除导航失败。
        self.script = list(script or [])
        #: 坏行为开关，含义见类 docstring。
        self.bad_json = bad_json
        self.invent_ref = invent_ref
        #: 终局自报一条**从未观察过**的 URL。与 ``invent_ref`` 是同一条
        #: 纪律的两端（动作期 vs 终局期），攻击面分别是 I6 的 ref 白名单
        #: 与 :func:`_validate_selected` 的链接白名单。
        self.invent_url = invent_url
        self.never_done = never_done
        #: **前 N 次输出散文，之后按脚本走**（``flaky=1`` 就是方案 13 §3.9
        #: 命名的 ``wrong_then_recover`` 形态）。用整数而不是布尔，是因为
        #: 「连续错两步」是**另一种**真实现象——那正好是升配的触发条件，
        #: 而布尔开关造不出第二种。
        self.flaky = flaky
        self.down = down
        self.no_usage = no_usage
        #: 本 fake 的模型名。升配测试要能一眼看出「这次调用走了哪个
        #: 后端」，而账本里记的就是它。
        self.model = model
        #: 任务类型（task kind 三值之一）。它决定**默认终局报告**的形态：
        #: 链接类任务报「到了」时若不带 selected，走的是契约违反那条路，
        #: 于是「脚本走完就默认到了」这个便利对 ③④ 就失效了。
        self.kind = kind
        #: 每轮的 messages 快照（顺序即轮次）。
        self.calls: list[list[dict[str, Any]]] = []
        #: 已被消费的脚本下标——**只在产出有效指令时推进**，于是
        #: 「白问了一次」不会吃掉一个脚本步骤。
        self.script_i = 0
        self.last_error = ""

    @property
    def cfg(self) -> Any:
        """账本读的 ``cfg.model``。刻意只给 ``model`` 一个属性——
        agent_loop 从 cfg 上取的也只有它。"""
        return _Cfg(self.model)

    def chat_messages(
        self,
        messages: list[dict[str, Any]],
        *,
        max_tokens: int | None = None,
    ) -> ChatResult:
        self.calls.append([dict(m) for m in messages or ()])
        if self.down:
            self.last_error = "transport:ConnectError"
            return ChatResult(content=None, usage=None, latency_ms=0,
                              error="transport:ConnectError")
        refs = _refs_of(messages)
        payload = self._next_instruction(refs, _links_of(messages))
        if payload is None:
            # 散文形态：实测模型没被强制 JSON 时的默认输出。刻意**带书名号
            # 与序号**——那种形态最容易骗过「先找第一个 {」的解析器。
            raw = "我想先在页面上找找看有没有能点进去的地方。"
        else:
            raw = json.dumps(
                {
                    # 脚本条目可带 ``think`` 覆盖默认值——D-9 守卫测试要
                    # 造「这一步的动机引用了一个特定实体」的形态，常量
                    # think 造不出那种锚定关系。
                    "think": payload.get("think")
                    or "看到页面了，接着找正片入口",
                    "tool": payload.get("tool", ""),
                    "ref": payload.get("ref", ""),
                    "params": payload.get("params", {}),
                    "done": bool(payload.get("done")),
                    "report": payload.get("report"),
                },
                ensure_ascii=False,
            )
        return ChatResult(
            content=raw,
            usage=None if self.no_usage else dict(_FAKE_USAGE),
            latency_ms=0,
        )

    def _next_instruction(
        self, refs: tuple[str, ...], links: tuple[str, ...]
    ) -> dict[str, Any] | None:
        """产出这条指令。``None`` = 输出散文（格式契约违反）。

        ``invent_ref`` 与 ``bad_json`` 是**恒定**坏行为（每步都犯），
        ``wrong_then_recover`` 只在第一步犯——「错一步再修回来」正是
        它要造的形态，而修回来之后必须按脚本继续，否则测的就不是
        「纠正后能继续」而是「纠正后路径变了」。
        """
        if self.bad_json:
            return None
        if self.invent_ref:
            return {"tool": "click", "ref": "e99", "params": {}}
        if len(self.calls) <= self.flaky:
            # 前 N 轮吐散文。**不推进脚本下标**：那几步是白问的，纠错
            # 重试拿到的应当还是脚本里没走过的第一条。
            return None
        if self.never_done:
            # 卡在原地，永不结束——步数预算与死循环检测的靶子。
            # 页面没有可交互元素时退到 press：那也不需要 ref，于是
            # 「空转」这件事与「页面上恰好没有按钮」解耦。
            if refs:
                return {"tool": "click", "ref": refs[0], "params": {}}
            return {"tool": "press", "params": {"key": "End"}}
        if self.script_i >= len(self.script):
            return {"tool": "", "done": True,
                    "report": self._default_report(links)}
        item = dict(self.script[self.script_i])
        self.script_i += 1
        if item.get("done"):
            # 显式终止条目：report 原样透传，测试才能用它驱动
            # 「自报放弃」那种形态。
            report = item.get("report")
            if self.invent_url and isinstance(report, dict):
                report = dict(report)
                selected = list(report.get("selected") or ())
                selected.append(
                    {"url": "https://invented.test/nope",
                     "why": "我记得这里有"}
                )
                report["selected"] = selected
            return {"tool": "", "done": True, "report": report}
        return item

    def _default_report(self, links: tuple[str, ...]) -> dict[str, Any]:
        """脚本走完时的默认终局（与 FakeLLM 默认答 True 同款便利）。

        链接类任务报的是**它看见的那些链接**——一个守规矩的模型真的会
        这么报，所以默认态造出的是「业务上走得通」，测别的东西时不用
        先排除终局违约。一条链接都没看见时默认报**放弃**而不是「到了」：
        报「到了」却交不出链接是契约违反，那会把「这个页面没有链接」
        测成「fake 坏了」。
        """
        if self.invent_url:
            return {
                "reached": True,
                "why": "找到了",
                "selected": [{"url": "https://invented.test/nope",
                              "why": "我记得这里有"}],
            }
        if self.kind == "play":
            return {"reached": True, "why": "到了播放页"}
        if not links:
            return {"reached": False, "why": "这个页面上没有可引荐的链接"}
        return {
            "reached": True,
            "why": f"找到了 {len(links)} 条",
            "selected": [{"url": href, "why": "页面正文里的引荐"}
                         for href in links],
        }

    def close(self) -> None:
        pass


class _Cfg:
    """账本读的 ``cfg`` 替身。只带 ``model``——把假后端的接口面缩到
    被真用到的那个属性上，多给一个属性就是多一处要维护的假象。"""

    __slots__ = ("model",)

    def __init__(self, model: str) -> None:
        self.model = model


def _obs_of(messages: list[dict[str, Any]]) -> dict[str, Any]:
    """从最后一条 user 消息里取出**对话里出现过**的观察投影。

    与 :func:`_request` 同款纪律——**从消息正文里读，不从 system prompt
    的契约串反查**。fake 因此只能引用模型**真的看见过**的东西，于是
    ``invent_ref`` / ``invent_url`` 造出的形态才正好是代码要挡的那一种。

    逐行试解析：观察 JSON 后面可能还跟着引导文案（同一条消息里），
    整段 ``json.loads`` 会失败。
    """
    for msg in reversed(messages or ()):
        if msg.get("role") != "user":
            continue
        for line in str(msg.get("content", "")).splitlines():
            try:
                body = json.loads(line)
            except ValueError:
                continue
            if isinstance(body, dict) and "interactive" in body:
                return body
    return {}


def _refs_of(messages: list[dict[str, Any]]) -> tuple[str, ...]:
    """对话里出现过的 ref（I6 白名单的同源镜像）。"""
    return tuple(
        str(e.get("ref", ""))
        for e in _obs_of(messages).get("interactive") or ()
    )


def _links_of(messages: list[dict[str, Any]]) -> tuple[str, ...]:
    """对话里出现过的链接 href（终局自报白名单的同源镜像）。

    与 :func:`_refs_of` 分开是因为两者管的东西不同：ref 是最近一页的
    可交互元素，链接是整条轨迹累积的——fake 只取最后一页的链接，正好
    是「模型看着这一页报出去」那种真实形态。
    """
    return tuple(
        str(x.get("href", ""))
        for x in _obs_of(messages).get("links") or ()
        if x.get("href")
    )


def _request(user: str) -> tuple[str, dict[str, Any]]:
    """从请求正文里取 ``(题目, 观察)``。

    **从正文里的 ``question`` 字段读题号，不靠 system prompt 的契约串
    反查。** 实测踩到的坑：``IS_REACHABLE`` 与 ``PLAYER_OK`` 的输出契约
    字面完全相同（同为 ``bool + evidence``），任何字符串反查都会认错，
    而认错的后果是**无声**的——一次 FIND_PLAY_CONTROL 被当成
    IS_REACHABLE 回答，``ref`` 就没了，判定从 True 掉成 None。
    """
    try:
        body = json.loads(user)
    except ValueError:
        return "", {}
    return str(body.get("question") or ""), body.get("observation") or {}


def _prose_for(qid: str) -> str:
    """散文形态——实测模型在没被强制 JSON 时的默认输出。"""
    return (
        "根据页面观察分析：\n\n"
        "1. 页面标题与目标作品一致\n"
        "2. 页面主体内容可见\n"
        "3. 未发现明显的错误页特征\n\n"
        "**结论：这是一个正常可访问的页面。**"
    )


def llm_perceptor(*, title: str = "功夫", **kwargs: Any) -> lp.LLMPerceptor:
    """构造一个接假后端的 :class:`LLMPerceptor`（契约测试用）。

    ``title`` 默认绑「功夫」——判断点 ① 的充分性预检要拿目标片名去匹配
    链接文本，没绑片名时 ① 恒为 None，于是**测 ① 的其他分支的测试会先撞上
    预检而不是自己要测的东西**。需要覆盖「没绑片名」那条的测试显式传 ``""``。
    """
    return lp.LLMPerceptor(FakeLLM(**kwargs), target_title=title)
