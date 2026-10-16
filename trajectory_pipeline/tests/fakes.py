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

from trajectory_pipeline.perception import llm_perceptor as lp
from trajectory_pipeline.perception.rule_perceptor import (
    SEARCH_BOX_KEYWORDS,
    SEARCH_BOX_TAGS,
    classify_trailer,
    is_play_control,
)


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
        # PLAYER_OK 用的 ``paywall_type``——空串表示"我没给"。
        paywall_type: str = "",
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
        self.paywall_type = paywall_type
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
        if self.down:
            self.last_error = "transport:ConnectError"
            return None
        qid, obs = _request(user)
        if qid is None:
            self.last_error = "shape:KeyError"
            return None
        if self.budget_exhausted:
            self.last_error = (
                "budget:max_tokens 用尽（finish_reason=length，content 为空）；"
                "调大 TRAJECTORY_LLM_MAX_TOKENS"
            )
            return None
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
            return f"<think>{self.think_text}</think>"
        if self.fence:
            raw = f"```json\n{raw}\n```"
        return raw

    def health(self) -> tuple[bool, str]:
        return (not self.down), "fake 后端"

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
            for e in obs.get("interactive_elements") or []:
                tag = str(e.get("tag", "")).lower()
                label = " ".join(
                    str(e.get("label", "")).split()
                ).replace(" ", "").lower()
                if (
                    tag in SEARCH_BOX_TAGS
                    and label
                    and any(kw in label for kw in SEARCH_BOX_KEYWORDS)
                ):
                    return {"ref": e.get("ref", ""),
                            "why": f"搜索框 {e.get('label','')!r}"}
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
        # IS_REACHABLE / PLAYER_OK / IS_AGGREGATOR（⑤ 与前两者同形：
        # bool + evidence，走同一条通用应答；answers 开关可逐题覆盖）
        # 2026-10-12 增量十七 · P4a 第二轮：让 FakeLLM 能在 IS_REACHABLE /
        # PLAYER_OK 时**真**回 page_quotes / access / paywall_type——
        # 这样端到端测试就能驱动「LLM 给了 page_quotes → fallback 触发」
        # 这条路径。已有 ``answer_*`` / ``answers`` 标志仍按惯例处理，
        # 下面只在默认行为（无 answer_* 干预）下加这些字段。
        payload: dict[str, Any] = {
            "answer": self.answers.get(qid, True),
            "evidence": "页面主体可见且可交互",
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
