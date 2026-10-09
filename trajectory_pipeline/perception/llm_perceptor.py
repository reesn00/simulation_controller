"""W3 LLM 版 Perceptor——**混合体，不是「纯 LLM」**。

「LLM 是传感器不是驾驶员」在这里有三条可检查的落法，缺一条就会
把采集故障写成业务结论：

1. **确定性事实优先，LLM 只做语义补位。**
   ``<video>`` 标签存在性是代码事实（``video_tag_count``），不问模型；
   预告片词表是**用户业务规则**，模型不知道也不该猜。LLM 接管的是
   「页面上有没有能播的东西」这类规则判不了的语义。
   纯 LLM 实现的实测反例：iqiyi 站点页 ``video=1`` 却被判「否」，
   理由是模型说「body 为空、元素为 0，说明未渲染」——
   它把**采集不全**读成**页面不行**，然后给了一个自信的错答案。

2. **采集充分性在代码层预检，在问模型之前。**
   :func:`_sufficiency`。这是上面那个反例的直接对策：模型不知道
   「正文 0 字符」是采集失败还是页面真没内容，它只会看见「空的」，
   然后**自信地判否**。规则版早就防住这条（见
   ``test_零个交互元素不判无播放控件``），LLM 版必须同样守住。

3. **ref 白名单校验。** 模型回传的 ref 必须**真的存在于**
   ``obs.interactive_elements``，否则 fail-closed。模型会编 ref（会话内
   句柄看着就像可生成的字符串），而代码拿它去 click——点错不可逆。
   校验在代码层，不靠提示词里那句「必须回传该控件的 ref」。

关于 confidence
--------------
**不采信模型自报。** ``llm/__init__.py`` 记着「自报 confidence 普遍虚高，
须经验校准后才可用于阈值判断」。本实现给的是**未校准先验**
（:data:`CONFIDENCE_JUDGED`），evidence 里写明它不是模型自报。
拿到真实标注集后按题校准；在那之前，阈值判断不应依赖这个数。
"""

from __future__ import annotations

import json
import time
from types import MappingProxyType
from typing import Any, Mapping

from trajectory_pipeline.llm import schema_parse
from trajectory_pipeline.llm.client import LLMClient
from trajectory_pipeline.perception import questions
from trajectory_pipeline.perception.base import Decision, Observation, Q, Question
from trajectory_pipeline.perception.rule_perceptor import (
    classify_trailer,
    is_play_control,
)

#: 本实现的置信度先验——**不是模型自报值**。取 0.8 是因为代码层已经
#: 过了采集预检与 ref 白名单两道闸，结论质量高于裸模型输出；但它**未经
#: 标注集校准**，evidence 里会写明这一点，避免下游误当实测值。
CONFIDENCE_JUDGED = 0.8

#: fail-closed 时的置信度。刻意为 0——它表示「**没有结论**」，
#: 而不是「很不确定地认为否」。
CONFIDENCE_NONE = 0.0

#: 逐题的输出契约。**必须显式写死键名**——后端不强制 json_schema，
#: 只给一句「输出 JSON」模型会回散文或 markdown 列表（已实测）。
#: 这里给的是 :mod:`~trajectory_pipeline.llm.schema_parse` 的容错层
#: 能对齐的形状；别名表是容错层的事，这里只保证「键名列全」。
CONTRACTS: Mapping[str, str] = MappingProxyType({
    Q.SELECT_PLAY_SITES: (
        '{"selected":[{"url":"候选链接的url","title":"站点标题","why":"选中理由"}],'
        '"rejected":[{"url":"候选链接的url","reason":"排除理由"}]}'
    ),
    Q.IS_REACHABLE: (
        '{"answer":true|false,"evidence":"一句话，指出页面上的具体证据"}'
    ),
    Q.FIND_PLAY_CONTROL: (
        '{"ref":"控件ref或null","trailer_only":true|false,'
        '"trailer_suspect":["标签文本"],"why":"一句话，指出控件原文"}'
    ),
    Q.PLAYER_OK: (
        '{"answer":true|false,"evidence":"一句话，指出页面上的具体证据"}'
    ),
})

#: 喂给模型的观察裁剪上限。实测：把正文从 1200 降到 600 字符后，
#: 之前直接返回 ``None`` 的大输入请求恢复正常。裁剪必须与
#: :class:`trajectory_pipeline.assembler.observation_view` 的
#: ``VIEW_BODY_CHARS`` 区分开——那是**训练数据**的裁剪，
#: 这里是**判定输入**的裁剪，作用是延迟与稳定性。
LLM_BODY_CHARS = 600
LLM_ELEMENTS = 25
LLM_LINKS = 20


class LLMPerceptor:
    """语义感知实现。W3 起与 :class:`RulePerceptor` 并列。"""

    name = "llm"

    def __init__(self, client: LLMClient, *, target_title: str = "") -> None:
        """
        ``target_title``：目标片名，**判断点 ① 必需**。

        它不在 ``Observation`` 里——片名是**任务的属性**，不是页面的属性，
        而 ``decide(question, obs)`` 的签名只有观察。把 ``task`` 塞进
        ``Observation`` 会污染事实层（P1 存档的观察字段会多出不属于
        页面的东西），所以走构造注入。

        **一个实例服务一个 task。** executor 已经是每 run 建一个
        perceptor（见 ``executor/cli.py``），所以这不引入新的生命周期
        约束。裸构造（契约测试用）拿不到片名，此时 ① 只能按「哪些像
        播放站」筛——那会选出通用视频站而非这个作品，故默认空串。
        """
        self._client = client
        self._target_title = target_title
        #: 本进程内因「能力不足」而 None 的题——覆盖率报表用。
        self._unresolved: set[str] = set()

    # ── 主入口 ──────────────────────────────────────────────────────

    def decide(self, question: str, obs: Observation) -> Decision:
        """判定 ``question``。**从不向调用方抛异常。**"""
        started = time.monotonic()
        spec = questions.get(question)

        try:
            guard = _sufficiency(question, obs, target_title=self._target_title)
            if guard:
                return self._unresolved_answer(spec, guard, started)

            handler = {
                Q.SELECT_PLAY_SITES: self._select_play_sites,
                Q.IS_REACHABLE: self._is_reachable,
                Q.FIND_PLAY_CONTROL: self._find_play_control,
                Q.PLAYER_OK: self._player_ok,
            }[question]

            # 确定性事实优先：video 标签存在性不进 LLM
            if question == Q.PLAYER_OK and obs.video_tag_count >= 1:
                return self._answer(
                    spec, True,
                    evidence=(
                        f"存在 <video>/<audio> 标签 ×{obs.video_tag_count}"
                        f"（iframe={obs.iframe_count}）——代码层事实，不问 LLM"
                    ),
                    started=started,
                    payload={"media_count": obs.video_tag_count,
                             "iframe_count": obs.iframe_count},
                    fallback=True,
                )
            return handler(spec, obs, started)
        except Exception as exc:                      # 兜底：契约禁止抛出去
            return self._unresolved_answer(
                spec, f"判定异常 {type(exc).__name__}: {exc}", started
            )

    # ── health ──────────────────────────────────────────────────────

    def health(self) -> tuple[bool, str]:
        ok, why = self._client.health()
        return ok, f"W3 LLM 版：语义题走 LLM，确定性事实仍归代码（{why}）"

    @property
    def unresolved_questions(self) -> frozenset[str]:
        return frozenset(self._unresolved)

    # ── 逐题实现 ────────────────────────────────────────────────────

    def _select_play_sites(self, spec: Question, obs: Observation,
                           started: float) -> Decision:
        """判断点 ①：选出提供在线观看的站点。

        **URL 必须来自观察，模型不能编。** 被选中的 url 逐个校验存在性，
        认不出的整条丢弃——编出来的 url 会让控制流去访问不存在的站点，
        而那次访问会以「不可达」入负样本池，污染出一个并不存在的失败原因。

        **问模型之前先问「这个输入支持这道题吗」**（见 :func:`_sufficiency`）。
        ① 的全部职责是「把 URL 与目标片名对上」，而 bing 结果的
        ``links[].text`` 只有面包屑（实测 ``qq.com https://v.qq.com › cover``），
        32 条链接没有一条写着片名——片名在 ``body_text`` 里，却与 URL
        无对应关系。这种输入下模型答「检索摘要未显示作品标题」是**对输入的
        准确描述**，随后按契约 fail-closed 成 ``False``，而 ``not_play_site``
        是负样本分支——错标签就进了池子。
        """
        raw, why = self._ask(spec, _render(obs, target_title=self._target_title))
        if raw is None:
            return self._unresolved_answer(spec, why, started)
        data = schema_parse.extract_json(raw)
        if data is None:
            return self._unresolved_answer(spec, _why_unparsable(raw), started)

        known = {link.href for link in obs.links}
        raw_selected = _rows(schema_parse.field(data, "selected", []))
        raw_rejected = _rows(schema_parse.field(data, "rejected", []))
        selected = [row for row in raw_selected
                    if schema_parse.as_str(row.get("url")) in known]
        rejected = [row for row in raw_rejected
                    if schema_parse.as_str(row.get("url")) in known]

        if not selected and not rejected:
            # 全被 URL 校验刷掉 = 模型说的与我们给的候选无关。
            # 判 False 等于说「这页一个可看站点都没有」，而输入可能只是没给全。
            return self._unresolved_answer(
                spec,
                f"模型给出的 {len(raw_selected)} 个 selected / {len(raw_rejected)}"
                f" 个 rejected 全部不在观察的 {len(known)} 个链接里，无法采信",
                started,
            )
        return self._answer(
            spec, True,
            evidence=(
                f"自 {len(known)} 个链接中选出 {len(selected)} 个可看站点"
                f"（已剔除不在观察中的 url），{len(rejected)} 个排除"
            ),
            started=started,
            payload={"selected": selected, "rejected": rejected},
        )

    def _is_reachable(self, spec: Question, obs: Observation,
                      started: float) -> Decision:
        """判断点 ②：页面是否正常打开内容。"""
        data, why = self._ask_json(spec, _render(obs))
        if data is None:
            return self._unresolved_answer(spec, why, started)
        answer, why_null = _bool_or_reason(data)
        if answer is None:
            return self._unresolved_answer(spec, why_null, started)
        return self._answer(
            spec, answer,
            evidence=_evidence(data, spec, answer),
            started=started,
            payload={"title": obs.page_title},
        )

    def _find_play_control(self, spec: Question, obs: Observation,
                           started: float) -> Decision:
        """判断点 ③：找播放控件 + 判是否仅预告。

        ``trailer_only`` **以代码词表为准，不以模型为准**：预告片是用户
        明确的业务规则（「播放按钮或剧集按钮文本是预告片即无正片」），
        模型只能作为交叉印证。反过来做的话，一个把「预告·第1季正片」
        判成 only 的模型会把正片站点写进负样本池——**不可逆**。
        """
        raw, why = self._ask(spec, _render(obs))
        if raw is None:
            return self._unresolved_answer(spec, why, started)
        data = schema_parse.extract_json(raw)
        if data is None:
            return self._unresolved_answer(spec, _why_unparsable(raw), started)

        # ── ref 白名单校验（铁律 3）────────────────────────────────
        ref = schema_parse.as_str(schema_parse.field(data, "ref"))
        known = {e.ref for e in obs.interactive_elements}
        if ref and ref not in known:
            return self._unresolved_answer(
                spec,
                f"模型回传 ref={ref!r} 不在观察的 {len(known)} 个元素里"
                f"（{sorted(known)[:5]}）——编造的 ref 会让代码点错",
                started,
            )

        # ── 预告判定：词表说了算，模型只提供 suspect 线索 ────────────
        suspects = [schema_parse.as_str(x)
                    for x in _rows(schema_parse.field(data, "trailer_suspect"))]
        suspects = [s for s in suspects if s]

        if ref:
            label = next((e.label for e in obs.interactive_elements
                          if e.ref == ref), "")
            verdict = classify_trailer(label)
            if verdict == "only":
                return self._answer(
                    spec, True,
                    evidence=f"ref={ref} 文本 {label!r} 命中预告片词表",
                    started=started,
                    payload={"ref": "", "trailer_only": True,
                             "trailer_suspect": [], "why": "词表判定"},
                )
            if verdict == "suspect" and not suspects:
                suspects = [label]
            return self._answer(
                spec, True,
                evidence=f"选定 ref={ref} 文本 {label!r}；{_evidence(data, spec, True)}",
                started=started,
                payload={"ref": ref, "trailer_only": False,
                         "trailer_suspect": suspects,
                         "why": schema_parse.as_str(
                             schema_parse.field(data, "evidence"))},
            )

        # 无 ref：若词表在页面上确实找不到任何非预告播放控件，判 False；
        # 否则是模型没选出来，fail-closed。
        if _has_play_control(obs):
            return self._unresolved_answer(
                spec, "模型未回传 ref，但词表在本页有非预告播放控件", started)
        if suspects:
            return self._answer(
                spec, True,
                evidence=f"候选疑似预告：{suspects[:3]}",
                started=started,
                payload={"ref": "", "trailer_only": False,
                         "trailer_suspect": suspects, "why": "模型线索"},
            )
        return self._answer(
            spec, False,
            evidence=(
                f"{len(obs.interactive_elements)} 个交互元素中，"
                f"词表与模型均未找到非预告播放控件：{_evidence(data, spec, False)}"
            ),
            started=started,
            payload={"ref": "", "trailer_only": False,
                     "trailer_suspect": [], "why": "词表与模型一致"},
        )

    def _player_ok(self, spec: Question, obs: Observation,
                   started: float) -> Decision:
        """判断点 ④：播放页是否可用。``<video>`` 存在性已在主入口处理。"""
        data, why = self._ask_json(spec, _render(obs))
        if data is None:
            return self._unresolved_answer(spec, why, started)
        answer, why_null = _bool_or_reason(data)
        if answer is None:
            return self._unresolved_answer(spec, why_null, started)
        return self._answer(
            spec, answer,
            evidence=_evidence(data, spec, answer),
            started=started,
            payload={"media_count": obs.video_tag_count,
                     "iframe_count": obs.iframe_count},
        )

    # ── 内部 ────────────────────────────────────────────────────────

    def _ask(self, spec: Question, rendered: Mapping[str, Any]) -> tuple[str | None, str]:
        """发一轮，返回 ``(原文, 失败原因)``。

        ``question`` **写进 user 正文**而不是只靠 system prompt 里的契约串
        区分。实测踩到的：``IS_REACHABLE`` 与 ``PLAYER_OK`` 的输出契约
        字面完全相同（同为 bool+evidence），任何靠契约串反查题目的下游
        都会认错——把一次 FIND_PLAY_CONTROL 当成 IS_REACHABLE 回答，
        ``ref`` 字段就无声地丢了。显式带题号是唯一不依赖文本比对的写法，
        且对模型也更清楚。

        失败原因**必须区分**「后端不可用」与「输出不像 JSON」——前者是
        基础设施问题（该重跑/该配 key），后者是模型格式问题（该改提示词）。
        两者混成一句话，evidence 就没法指导下一步动作。

        **这里不传 ``max_tokens``**。曾经写死 600，于是推理模型把预算全花在
        思考上、正文一个字没吐，而客户端按红线不读 ``reasoning_content``，
        整条链上每一道题都只表现为一句 ``shape:ValueError``——看不出要调
        预算。预算归 :class:`~trajectory_pipeline.llm.client.LLMConfig` 管，
        生产调用点不各写各的。
        """
        system = (
            spec.prompt
            + "\n\n只输出一个 JSON 对象，不要解释文字，不要 markdown 代码块。\n"
            + "输出格式必须严格是：" + CONTRACTS[spec.id]
            + "\n证据不足时 answer 给 null，不要猜。"
        )
        user = json.dumps({"question": spec.id, "observation": rendered},
                         ensure_ascii=False)
        got = self._client.chat(system, user)
        if got is None:
            return None, f"LLM 不可用（{self._client.last_error or '未知原因'}）"
        return got, ""

    def _ask_json(self, spec: Question,
                  rendered: Mapping[str, Any]) -> tuple[Mapping[str, Any] | None, str]:
        raw, why = self._ask(spec, rendered)
        if raw is None:
            self._unresolved.add(spec.id)
            return None, why
        data = schema_parse.extract_json(raw)
        if data is None:
            self._unresolved.add(spec.id)
            return None, _why_unparsable(raw)
        return data, ""

    def _unresolved_answer(self, spec: Question, reason: str,
                           started: float) -> Decision:
        """fail-closed 的唯一出口。**evidence 里必须带上原因。**

        原因不能省：它解释了为什么这一站没有落到负样本池，是人工复核
        时判断「该重跑还是该认了」的唯一线索。
        """
        self._unresolved.add(spec.id)
        return Decision(
            question=spec.id, answer=None, confidence=CONFIDENCE_NONE,
            evidence=reason or "未取得结论", source="llm",
            payload=MappingProxyType({}), latency_ms=int((time.monotonic() - started) * 1000),
            fallback_used=True,
        )

    def _answer(self, spec: Question, answer: bool | None, *, evidence: str,
                started: float, payload: Mapping[str, Any] | None = None,
                fallback: bool = False) -> Decision:
        return Decision(
            question=spec.id, answer=answer, confidence=CONFIDENCE_JUDGED,
            evidence=evidence, source="llm",
            payload=MappingProxyType(dict(payload)) if payload else MappingProxyType({}),
            latency_ms=int((time.monotonic() - started) * 1000),
            fallback_used=fallback,
        )


# ═══════════════════════════════════════════════════════════════════════
# 采集充分性预检（铁律 2）
# ═══════════════════════════════════════════════════════════════════════


def _sufficiency(question: str, obs: Observation, *,
                 target_title: str = "") -> str:
    """问模型**之前**先判这份观察够不够。返回空串 = 够。

    这是 LLM 版最要紧的一段。实测：iqiyi 的 PLAYER_OK 被判「否」，
    模型的原话是「body 为空、元素为 0，说明未渲染」——它把采集失败
    讲成了一条通顺的、错的结论。**模型看不到采集层发生了什么**，
    它只看到空的，于是按常识推理「空的 = 页面没内容」。
    而规则版早就知道哨兵返回分不清「没渲染完」与「真的没控件」
    （``obscura`` 的空元素返回哨兵文本，不是错误）。

    **两条守卫守的是两种不同的「能力不可用」**：

    1. 采集没拿到东西（``degraded`` / 正文与元素皆空）——原有那条。
    2. **采集拿到了，但这道题要的那种对应关系不在里面**（① 的片名↔URL）。
       实测 bing 结果的 ``links[].text`` 只有面包屑，32 条链接没有一条
       写着片名，片名在 ``body_text`` 里却与 URL 无对应——模型在这种输入下
       答「检索摘要未显示作品标题」是**对输入的准确描述**，但它随后按契约
       fail-closed 成 ``False``，而 ``not_play_site`` 是负样本分支：
       **错标签就进了池子**。``None`` 不入池而 ``False`` 入池，
       区别不在模型，在**有没有能力判**——而 fail-closed 的定义正是
       「没能力判就返回 ``None``」。

    第 2 条比第 1 条更隐蔽：第 1 条时模型看到的是明显的空，
    证据里写「采集降级」；第 2 条时页面内容丰富、看着一切正常，
    **没有任何东西提示判据缺失**，只有反查 URL 归属才能发现标签是错的。
    """
    if obs.degraded:
        return f"采集降级（{list(obs.degraded)}），证据不完整，不判"
    if question == Q.SELECT_PLAY_SITES:
        if not obs.links:
            return "搜索页未采到链接，无从选择站点，不判"
        return _title_link_sufficiency(obs, target_title)
    if question == Q.FIND_PLAY_CONTROL and not obs.interactive_elements:
        # 这道题**唯一的输入就是元素表**。表是空的，模型看到的是
        # 「这页一个控件都没有」——而正文里可能明明白白写着「立即播放」。
        #
        # 实测（2026-10-09 m.ixigua.com/video/6582085495839261192）：
        # 正文 100 字符、元素 0 个，模型判 False，理由「未提供任何可交互
        # 控件 ref」——于是记成 ``no_play_control`` **负样本**。
        # 而西瓜视频上有这条片子，页面还写着「立即播放」。
        #
        # ⚠️ **规则版早就防住了这条**（``rule_perceptor`` 里
        # ``not obs.interactive_elements`` → None），LLM 版漏了。
        # 两版口径必须一致，否则换 ``--perceptor`` 就换一批标签，
        # 而「规则版对、LLM 版错」在报表上完全看不出来。
        #
        # 关键在于**别拿「有正文」当「渲染完了」的证据**：
        # 正文与元素来自两个独立的 tool，前者成功不代表后者也成功。
        return (
            f"采到 0 个交互元素（正文 {len(obs.body_text)} 字符）——"
            f"哨兵返回无法区分「页面没渲染完」与「页面确实没控件」，"
            f"不判（正文里写着「立即播放」而元素 0 个，判 False 就是"
            f"把采集失败写成业务负样本）"
        )
    if not obs.body_text and not obs.interactive_elements:
        # 两者皆空 = 这不是「页面没内容」，是采集没拿到东西。
        # 实测三站（iqiyi / ixigua / sohu）都走到这里：正文 0 字符、
        # 元素 0 个，那是 JS 页面还没渲染完。
        return (
            f"正文 0 字符且交互元素 0 个——采集未拿到内容，"
            f"不判（哨兵返回分不清「未渲染」与「确实为空」）"
        )
    return ""


def _title_link_sufficiency(obs: Observation, target_title: str) -> str:
    """① 的判别力检查：链接文本里有没有片名。

    判据**只用链接自身的文本**，不看 ``body_text``。body_text 里当然有
    片名（实测一份观察含「功夫」16 次），但它是一条无结构的文本流，
    里面的片名与 ``links[]`` 的 URL **没有任何对应关系**——模型无从把
    「这条标题」和「那个 URL」配上，对不上就是猜。猜出来的 ``not_play_site``
    是负样本，而实测已经出现过它与同一 URL 的**成功记录直接矛盾**。

    真正的修法是让采集层把标题带进 ``links[]``（见 ``steps/search.py``
    的 ``RESULT_SELECTORS``）；在那之前，这道题只能在输入有判别力时才问。
    """
    title = (target_title or "").strip()
    if not title:
        # 没有目标片名就无从匹配。这不是「判为否」，是问错了题。
        return "未绑定目标片名，① 无法匹配，不判"
    hits = sum(1 for link in obs.links if title in (link.text or ""))
    if hits:
        return ""
    return (
        f"{len(obs.links)} 个候选链接的文本里没有一个含《{title}》"
        f"（实测 bing 只给面包屑，片名在 body_text 里但与 URL 无对应）"
        f"——输入不支持本题，不判"
    )


# ═══════════════════════════════════════════════════════════════════════
# 渲染与工具
# ═══════════════════════════════════════════════════════════════════════


def _render(obs: Observation, *, target_title: str = "") -> dict[str, Any]:
    """把观察转成喂给模型的 dict。**纯裁剪，不加工。**

    刻意**不做**摘要、不改写、不截 link 到域名——那些是语义加工，
    属于 LLM 的活；这里只控制体积。真实站点一次观察能到几万字符，
    全量塞进去会让请求慢到超时，而超时 = fail-closed = 少一条样本。
    """
    out: dict[str, Any] = {
        "url": obs.url,
        "page_title": obs.page_title,
        "body_text": obs.body_text[:LLM_BODY_CHARS],
        "body_total_chars": len(obs.body_text),
        "body_truncated": len(obs.body_text) > LLM_BODY_CHARS,
        "interactive_elements": [
            {"ref": e.ref, "tag": e.tag, "label": e.label[:60]}
            for e in obs.interactive_elements[:LLM_ELEMENTS]
        ],
        "elements_total": len(obs.interactive_elements),
        "video_tag_count": obs.video_tag_count,
        "iframe_count": obs.iframe_count,
    }
    if obs.links:
        out["links"] = [
            {"text": l.text[:60], "href": l.href}
            for l in obs.links[:LLM_LINKS]
        ]
        out["links_total"] = len(obs.links)
    if target_title:
        out["target_title"] = target_title
    if obs.degraded:
        out["degraded"] = list(obs.degraded)
    return out


def _why_unparsable(raw: str) -> str:
    """解析失败的原因串。**带上模型实际说了什么**。

    只写「无法解析为 JSON」的话，模型输出格式漂移这件事没法从存档里
    看见——而它恰恰是最该按批次统计的东西（某个后端版本开始套 markdown
    fence，是从存档里一眼看出来的，不是从报错里）。
    """
    head = raw[:120].replace("\n", " ")
    return f"LLM 有输出但无法解析为 JSON（前 120 字）：{head}"


def _rows(value: Any) -> list[dict[str, Any]]:
    """把可能是 list/dict/None 的字段规整成 dict 列表。"""
    if isinstance(value, list):
        return [v for v in value if isinstance(v, dict)]
    if isinstance(value, dict):
        return [value]
    return []


def _has_play_control(obs: Observation) -> bool:
    """页面上是否**确有**非预告播放控件（词表判据，复用 W1 的表）。

    import 而不是复制：两份词表迟早不同步，而不同步的后果是
    「W3 判 False、W1 判 True」——同一批素材两个结论，且都像对的。
    """
    return any(is_play_control(e.label)
               for e in obs.interactive_elements
               if classify_trailer(e.label) == "none")


def _evidence(data: Mapping[str, Any], spec: Question, answer: bool) -> str:
    """拼 evidence。**带上「置信度不是模型自报」这句。**"""
    why = schema_parse.as_str(schema_parse.field(data, "evidence")) or spec.prompt[:40]
    tail = f"（置信度 {CONFIDENCE_JUDGED} 为代码层先验，非模型自报）"
    return f"LLM 判 {answer}：{why}{tail}"


def _bool_or_reason(data: Mapping[str, Any]) -> tuple[bool | None, str]:
    """读 ``answer``，并**说清为什么读不出来**。

    提示词里明写着「证据不足时 answer 给 null」——所以 ``null``（或缺字段）
    是**模型照契约作答**，不是它坏了。两种情况都落到 ``answer=None``、
    都记 ``unresolved`` 分支，**采集结果完全一样**，但证据必须分开说：

    - 契约式 null = 模型承认「这条我判不了」→ 该补采集 / 换模型 / 调提示词；
    - 格式坏      = 模型想答但没按格式答 → 该调解析容错。

    混成一句「无法解析为布尔」时，人工复核读到的是「模型坏了」，
    于是所有人都去调解析层，而真正该做的是让模型判得出来。
    """
    raw = schema_parse.field(data, "answer")
    answer = schema_parse.as_bool(raw)
    if answer is not None:
        return answer, ""
    # 两条 fail-closed 路径**都要**保留模型自述：模型说「我判不了」时
    # 通常仍说得出**为什么**，那是人工复核唯一能用的线索。丢掉它等于
    # 让这一站彻底没有依据——而这恰恰是本该交给人的那些站。
    why = schema_parse.as_str(schema_parse.field(data, "evidence"))
    tail = f"；模型自述：{why}" if why else ""
    # 合法的「我判不了」：显式 null / 字段缺失 / 空串
    if raw is None or (isinstance(raw, str) and not raw.strip()):
        return None, f"模型按契约给出 null（证据不足），不是输出格式问题{tail}"
    return None, f"answer={raw!r} 既不是布尔也不是 null（格式偏离）{tail}"