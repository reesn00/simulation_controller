"""W3 LLM 版 Perceptor——**混合体，不是「纯 LLM」**。

「代码层做护栏、LLM层做探索」在这里有三条可检查的落法，缺一条就会
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
import re
import time
from types import MappingProxyType
from typing import Any, Mapping

from trajectory_pipeline.llm import schema_parse
from trajectory_pipeline.llm.client import LLMClient
from trajectory_pipeline.perception import questions
from trajectory_pipeline.common.elements import (
    element_json,
)
from trajectory_pipeline.perception.base import (
    Decision,
    Observation,
    Q,
    Question,
)
from trajectory_pipeline.perception.rule_perceptor import (
    classify_paywall,
    classify_trailer,
    infer_access_from_evidence,
    infer_paywall_type_from_evidence,
    paywall_rescue_evidence,
    play_control_candidates,
    search_box_candidates,
    trailer_only_candidates,
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
CONTRACTS: Mapping[str, str] = MappingProxyType(
    {
        Q.SELECT_PLAY_SITES: (
            '{"selected":[{"url":"候选链接的url","title":"站点标题","why":"选中理由"}],'
            '"rejected":[{"url":"候选链接的url","reason":"排除理由"}]}'
        ),
        # 2026-10-12：IS_REACHABLE 在 answer=false 时要求同时给出
        # ``access_control_type``，决策点 ② 的细分分支靠它选用
        # （login→login_required、region/captcha/anti_bot→blocked、
        # not_found→broken；null 兜底回落合并态）。
        # 增量十六 · P4a：另加 ``page_quotes`` 字段（array of str）——
        # **只装页面原文短语**，与 evidence 分离；code 用 page_quotes 做
        # fallback（详见 ``_is_reachable`` 解析段），避免 evidence 自述
        # 误触关键词集合（P3 v1 regression 的根因）。
        # 增量十七 · P4a 第二轮：``page_quotes`` 提到 ``answer`` 之后立即位
        # （**先填字段**），让 LLM 先看到它再写 evidence——避免 evidence 写满
        # 页面引文、page_quotes 空。evidence 说明文字也收紧：「不含页面原文
        # 引用」明示，禁止「」/"" 任何引号包裹。
        Q.IS_REACHABLE: (
            '{"answer":true|false|null,'
            '"page_quotes":["原文短语1","原文短语2"],'
            '"access_control_type":"login|region|captcha|anti_bot|not_found|null",'
            '"evidence":"一句话，不含页面原文引用（页面引文全部进 page_quotes）"}'
        ),
        Q.FIND_PLAY_CONTROL: (
            '{"ref":"控件ref或null","trailer_only":true|false,'
            '"trailer_suspect":["标签文本"],"why":"一句话，指出控件原文"}'
        ),
        # 2026-10-12：PLAYER_OK 在 answer=false 且页面**实际阻挡**播放时
        # 要求 paywall_type（preview→partial_free；trial 默认仍走订阅墙；
        # null 兜底）。
        # 增量十六 · P4a：另加 ``page_quotes`` 字段，code 用它做 fallback。
        # 增量十七 · P4a 第二轮：``page_quotes`` 提到 ``answer`` 之后立即位
        # （**先填字段**），让 LLM 先看到它再写 evidence——避免 evidence 写满
        # 页面引文、page_quotes 空。evidence 说明文字同步收紧。
        Q.PLAYER_OK: (
            '{"answer":true|false|null,'
            '"page_quotes":["原文短语1","原文短语2"],'
            '"paywall_type":"preview|trial|null",'
            '"evidence":"一句话，不含页面原文引用（页面引文全部进 page_quotes）"}'
        ),
        # ── 路径 B/C 发现链六题（2026-10-16）──────────────────────────
        # 四道 SELECT_* 与 ① 同构：编号回指（``i``）优先于 URL 记忆——
        # 「回长 URL 是记忆任务，回编号是复制任务」。其中 ⑥/⑥C 两题
        # （单页选链接）的「selected 空 + rejected 非空」是**真结论**
        # （本页没有要找的东西），①′ 两题（搜索页选题）零选中仍是
        # 「没结论」——口径差异在 handler 层实现，契约形状一致。
        Q.SELECT_AGGREGATOR_RESULTS: (
            '{"selected":[{"i":0,"title":"站点标题","why":"选中理由"}],'
            '"rejected":[{"i":1,"reason":"排除理由"}]}'
        ),
        Q.IS_AGGREGATOR: (
            '{"answer":true|false|null,"evidence":"一句话判断"}'
        ),
        Q.SELECT_SITE_ENTRIES: (
            '{"selected":[{"i":0,"why":"选中理由"}],'
            '"rejected":[{"i":1,"reason":"排除理由（介绍页/广告/站内分类页/dead link）"}]}'
        ),
        Q.SELECT_ARTICLE_RESULTS: (
            '{"selected":[{"i":0,"title":"文章标题","why":"选中理由"}],'
            '"rejected":[{"i":1,"reason":"排除理由"}]}'
        ),
        Q.SELECT_REFERRAL_LINKS: (
            '{"selected":[{"i":0,"why":"选中理由"}],'
            '"rejected":[{"i":1,"reason":"排除理由（站内导航/参考文献/广告/社交分享）"}]}'
        ),
        Q.FIND_SITE_SEARCH: (
            '{"ref":"输入框ref或null","why":"一句话，指出搜索框原文"}'
        ),
        # ── 方案 12 阶段 A1 · 分类任务（2026-10-16）────────────────────
        # 一道题统一三条发现路径的「选某类链接」语义：四个类别的数组
        # 键名与 :data:`perception.questions.CLASSIFY_CATEGORIES` 同源
        # （改名等于破坏性契约变更——存档 payload 与 P1 里都是这四个键）。
        # **四个数组全部为空 = 没作答**（fail-closed），漏分类的链接
        # 不计入任何结论（prompt 声明 + orchestrator 兜底）。
        Q.CLASSIFY_SEARCH_LINKS: (
            '{"video_site":[{"i":0,"title":"站点标题","why":"归类理由"}],'
            '"aggregator":[{"i":1,"title":"站点标题","why":"归类理由"}],'
            '"article_post":[{"i":2,"title":"文章标题","why":"归类理由"}],'
            '"noise":[{"i":3,"title":"链接标题","why":"归类理由"}]}'
        ),
    }
)

#: access_control_type 合法值集合——解析层只放行这五个，其余回 None。
#: 这里列必须与 :func:`~trajectory_pipeline.executor.branches.decision_branch`
#: 的细分逻辑同源：某个取值一旦出现却不在这里，回落合并态是配套的。
ACCESS_CONTROL_TYPES: frozenset[str] = frozenset(
    {"login", "region", "captcha", "anti_bot", "not_found"}
)

#: paywall_type 合法值集合（仅 preview/trial 走细分；null 兜底）。
PAY_WALL_TYPES: frozenset[str] = frozenset({"preview", "trial"})

#: 喂给模型的观察裁剪上限。实测：把正文从 1200 降到 600 字符后，
#: 之前直接返回 ``None`` 的大输入请求恢复正常。裁剪必须与
#: :class:`trajectory_pipeline.assembler.observation_view` 的
#: ``VIEW_BODY_CHARS`` 区分开——那是**训练数据**的裁剪，
#: 这里是**判定输入**的裁剪，作用是延迟与稳定性。
LLM_BODY_CHARS = 600
LLM_ELEMENTS = 25
LLM_LINKS = 20

#: 路径 B/C 的四道链接选择题（2026-10-16）+ 分类任务①（方案 12 阶段 A1）。
#: 与 ① 共用编号回指契约，但**无片名判据**——①B 的查询是通用聚合词、
#: ①C 与分类题判的是链接/页面形态，``_title_link_sufficiency`` 一概
#: 不适用，预检只查链接非空。
_LINK_SELECTION_QUESTIONS = frozenset(
    {
        Q.SELECT_AGGREGATOR_RESULTS,
        Q.SELECT_SITE_ENTRIES,
        Q.SELECT_ARTICLE_RESULTS,
        Q.SELECT_REFERRAL_LINKS,
        Q.CLASSIFY_SEARCH_LINKS,
    }
)

#: 渲染时**给链接编号**的选择题全集（① + 上面四题）。:func:`_render`
#: 按它加 ``i`` 字段，handler 按 ``i`` 回指——两处共用同一个集合，
#: 避免「渲染带编号、解析不认编号」的错位（渲染与解析曾经在窗口口径
#: 上错位过一次，后果见 :func:`_ordered_links` 的 docstring）。
_NUMBERED_LINK_QUESTIONS = _LINK_SELECTION_QUESTIONS | {Q.SELECT_PLAY_SITES}


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
                # 2026-10-15 INC43 · 哨兵路径付费墙救济：③ 的 0 元素哨兵
                # 拦下了这道题，但 body_text 里有**确定性付费墙证据**
                # （完整短语 / 强弱信号组合）——正文能采到付费墙文案，
                # 本身就证明页面渲染了，哨兵担心的「没渲染完」不成立。
                # 实测 m.ixigua ×2（elements=0、body=100 字符
                # 「以下内容需下载西瓜视频查看」）即此形态。
                #
                # ⚠️ ``obs.degraded`` 时**不救济**（§5.5：采集失败与
                # 「真没有」在存档里同形，必须 fail-closed）——降级观察的
                # 正文可能只是部分加载的导航栏。
                #
                # 2026-10-15 INC45 · 乱码页例外（簇 B，tv.sohu ×3）：混合
                # 编码页（GBK 主体 + UTF-8 字节混入）的 ``body_mojibake``
                # 与其他降级**本质不同**——正文**采到了**（实测 1588 字符
                # 里 156 个 U+FFFD，其余可读），可读部分含确定性付费墙
                # 证据（「新用户首月9.9元，开通会员 立即抢购」）。§5.5 守
                # 的是「**缺**证据别当业务结论」，这里是**有**证据——
                # 仅当降级集合只含 mojibake 标记（body/title）时放行救济，
                # 其余降级（body_text 缺失 / snapshot 失败等）照旧
                # fail-closed。强信号闸（paywall_rescue_evidence）与
                # INC43 同判据，nav-bar 弱 CTA 不放行。
                if question in (Q.IS_REACHABLE, Q.FIND_PLAY_CONTROL) and (
                    not obs.degraded
                    or set(obs.degraded) <= {"body_mojibake", "title_mojibake"}
                ):
                    rescue = paywall_rescue_evidence(obs.body_text or "")
                    if rescue:
                        mojibake_note = (
                            "；正文为混合编码（部分乱码），可读部分已含上述证据"
                            if obs.degraded
                            else ""
                        )
                        return self._answer(
                            spec,
                            False,
                            evidence=(
                                f"[哨兵路径付费墙救济] {guard}；但正文含{rescue}"
                                f"{mojibake_note}"
                                f"——页面已渲染出付费墙文案，「无控件」的真因是"
                                f"付费/客户端墙，按 2026-10-10 口径记订阅墙，"
                                f"不落哨兵 fail-closed"
                            ),
                            started=started,
                            payload={"paywall": True, "paywall_word": rescue},
                            fallback=True,
                        )
                return self._unresolved_answer(spec, guard, started)

            handler = {
                Q.SELECT_PLAY_SITES: self._select_play_sites,
                Q.IS_REACHABLE: self._is_reachable,
                Q.FIND_PLAY_CONTROL: self._find_play_control,
                Q.PLAYER_OK: self._player_ok,
                # 路径 B/C 发现链（2026-10-16）：①′ 两题（搜索页选题）
                # 与 ⑥′ 两题（单页选链接）各共享一个实现——差异只在
                # 「零选中是不是真结论」这一个布尔，见 _select_from_links。
                Q.SELECT_AGGREGATOR_RESULTS: self._select_search_results,
                Q.SELECT_ARTICLE_RESULTS: self._select_search_results,
                Q.SELECT_SITE_ENTRIES: self._select_page_links,
                Q.SELECT_REFERRAL_LINKS: self._select_page_links,
                Q.IS_AGGREGATOR: self._is_aggregator,
                Q.FIND_SITE_SEARCH: self._find_site_search,
                # 方案 12 阶段 A1（2026-10-16）：搜索页链接分类任务——
                # orchestrator 三条发现路径统一问这一道，按类分发。
                Q.CLASSIFY_SEARCH_LINKS: self._classify_search_links,
            }[question]

            # 确定性事实优先：video 标签存在性不进 LLM
            if question == Q.PLAYER_OK and obs.video_tag_count >= 1:
                return self._answer(
                    spec,
                    True,
                    evidence=(
                        f"存在 <video>/<audio> 标签 ×{obs.video_tag_count}"
                        f"（iframe={obs.iframe_count}）——代码层事实，不问 LLM"
                    ),
                    started=started,
                    payload={
                        "media_count": obs.video_tag_count,
                        "iframe_count": obs.iframe_count,
                    },
                    fallback=True,
                )
            decision = handler(spec, obs, started)
            # 2026-10-15 INC43 · ②③ 负分支付费墙救济：LLM 已判 False
            # （页面确实不可用），但 ②③ 的负分支（login_wall_or_blocked /
            # no_play_control）从不检查付费墙信号——订阅墙页面于是贴错
            # 标签。正文含强付费墙证据时标记 ``payload["paywall"]``，
            # ``decision_branch`` 会据此改判 ``subscription_wall``。
            #
            # **只在 answer=False 时救济**（不翻转保证）：True 是成功，
            # None 是「没能力判」——词表命中不构成推翻二者的证据
            # （④ 的既有口径：两边一致才落细分，任何一边不一致都按
            # 未定的那个结论记）。② 已给出 access_control_type 细分
            # （login/region/...）时也不救济——细分证据比关键词更具体，
            # ``decision_branch`` 本就让细分优先，救济只添噪声。
            #
            # ⚠️ 2026-10-15 INC44 证伪记录：曾把救济扩到 ④（PLAYER_OK），
            # e2e 75 对 A/B 实测**零收益**（修复 0 / 反转 0）——簇 A 的
            # 付费墙证据在 LLM evidence 里而非 body_text，词表够不着；
            # 唯一触发行 T011 v.qq.com/error.html 还是假阳性（错误页
            # 页脚「VIP 会员」强弱组合命中，broken 被误改判订阅墙）。
            # 已回滚，勿再对 ④ 做词表救济（见 05-现状与验收.md INC44）。
            if (
                question in (Q.IS_REACHABLE, Q.FIND_PLAY_CONTROL)
                and decision.answer is False
                and not decision.payload.get("paywall")
                and (
                    question == Q.FIND_PLAY_CONTROL
                    # ② 的「无细分」形态是空串/None 两种（payload 恒带
                    # ``access_control_type`` 键，缺细分时值为空）。
                    or not decision.payload.get("access_control_type")
                )
            ):
                rescue = paywall_rescue_evidence(obs.body_text or "")
                if rescue:
                    return _rescued_paywall_decision(decision, rescue)
            return decision
        except Exception as exc:  # 兜底：契约禁止抛出去
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

    def _select_play_sites(
        self, spec: Question, obs: Observation, started: float
    ) -> Decision:
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
        raw, why = self._ask(
            spec, _render(obs, target_title=self._target_title, question=spec.id)
        )
        if raw is None:
            return self._unresolved_answer(spec, why, started)
        data = schema_parse.extract_json(raw)
        if data is None:
            return self._unresolved_answer(spec, _why_unparsable(raw), started)

        known = {link.href for link in obs.links}
        window = _link_window(obs, spec.id, self._target_title)
        raw_selected = _rows(schema_parse.field(data, "selected", []))
        raw_rejected = _rows(schema_parse.field(data, "rejected", []))
        # 优先按编号回指（:func:`_render` 送给模型的每条都带 ``i``），
        # 回不到再退回 URL 匹配——旧后端按旧契约回 url 也不至于整批作废。
        selected = _resolve_rows(raw_selected, window, known)
        rejected = _resolve_rows(raw_rejected, window, known)

        if not selected and not rejected:
            # 「模型给了条目但全对不上」与「模型压根没作答」是**两件事**，
            # 合成一句会把复盘引向错误的方向——实测踩过：evidence 写成
            # 「全部不在观察的 107 个链接里」，实际是模型返回了**空的**
            # 两个数组。字面为真而语义在说别的事，排查因此绕了远路。
            if not raw_selected and not raw_rejected:
                return self._unresolved_answer(
                    spec,
                    f"模型未给出任何 selected/rejected（送进模型 "
                    f"{len(window)} 条链接）——它**没答这道题**，"
                    f"不是答「这页没有可看站点」；模型原文摘录：{_excerpt(raw)}",
                    started,
                )
            return self._unresolved_answer(
                spec,
                f"模型给出的 {len(raw_selected)} 个 selected / {len(raw_rejected)}"
                f" 个 rejected 全部不在观察的 {len(known)} 个链接里，无法采信",
                started,
            )
        if not selected:
            # ⚠️ **判 True 的语义是「我选出了站」**。只排除、零选中说明模型
            # 给不出选站结论——那不是「这页没有可看站点」（那是 False 的意思），
            # 而是**没能力答完这道题**。
            #
            # 放它过去的后果实测过（2026-10-10 T001）：控制流按 True 走
            # 过滤，``kept`` 为空 → 0 候选 → **一个站都没访问**，
            # 整批只留下一堆 ``unresolved``，什么素材都没采到。
            # 而 fail-closed 的定义正是「没能力判就返回 None」。
            return self._unresolved_answer(
                spec,
                f"模型排除了 {len(rejected)} 个链接但一个都没选中——"
                f"**这不是「这页没有可看站点」**（那是 False），"
                f"而是没给出选站结论，不判",
                started,
            )
        return self._answer(
            spec,
            True,
            evidence=(
                f"自 {len(known)} 个链接中选出 {len(selected)} 个可看站点"
                f"（已剔除不在观察中的 url），{len(rejected)} 个排除"
            ),
            started=started,
            payload={"selected": selected, "rejected": rejected},
        )

    def _is_reachable(
        self, spec: Question, obs: Observation, started: float
    ) -> Decision:
        """判断点 ②：页面是否正常打开内容。

        2026-10-12 阶段 1：answer=false 时**必须**带回
        ``access_control_type``，decision_branch 据此选用细分分支
        （login_required / blocked / broken）——payload 缺失则回落合并态
        ``login_wall_or_blocked``（保持旧路径的兼容，见
        ``branches.decision_branch`` 的「payload 缺失保持现状」注释）。

        解析故意 fail-soft：模型没给字段、字段值不在合法集、或
        answer=true 都没填，都按 None 走——不抛异常、不改答案。

        2026-10-12 增量十五：payload 缺失 + evidence 含访问阻断关键词时
        code fallback 反推 access——LLM 即便看到 70017001 也不一定每次都
        填字段，但 evidence 又写明。evidence 字符串后置 fallback 标记，
        让复核能识别。

        2026-10-12 增量十六 · P4a：fallback 的输入源**改为 ``page_quotes``**——
        LLM 单独回传的"页面原文短语"字段，而不是 LLM 自述 evidence。
        ``page_quotes`` 里是 LLM 看到的页面真实字符、不会包含 LLM 推断语言。
        这样避免 P3 v1 实证过的「LLM 自述 → 误触关键字」regression。
        """
        data, why = self._ask_json(spec, _render(obs))
        if data is None:
            return self._unresolved_answer(spec, why, started)
        answer, why_null = _bool_or_reason(data)
        if answer is None:
            return self._unresolved_answer(spec, why_null, started)
        raw_access = schema_parse.as_str(schema_parse.field(data, "access_control_type"))
        # P4a 增量十六：fallback 输入源**只**用 page_quotes——把 LLM 的
        # 页面原文与"自身推断"严格分开。如果 LLM 没给 page_quotes，fallback
        # 就拿不到证据，按现状落合态——宁可不命中也别拿 LLM 自述当原文。
        # 2026-10-12 增量二十一：``page_quotes`` 走 ``_quotes_with_fallback``
        # — LLM 没给时从 evidence 后处理抽业务引文段作 fallback；零 LLM 改动。
        quotes_text = " ".join(_quotes_with_fallback(data))
        access, fallback_note = _resolve_access(raw_access, quotes_text)
        tail = fallback_note
        if answer is False and access is None:
            tail = (
                "（模型未给出合法 access_control_type 也无 page_quotes 命中——"
                "决策点②细分未启用，落合并态 login_wall_or_blocked）"
            )
        evidence = _evidence(data, spec, answer) + tail
        return self._answer(
            spec,
            answer,
            evidence=evidence,
            started=started,
            payload={
                "title": obs.page_title,
                "access_control_type": access,
                # page_quotes 同步进 payload——单测、人工复核、报表都能
                # 看到 LLM 真正观察到的页面文本。
                "page_quotes": _quotes_with_fallback(data),
            },
        )

    def _find_play_control(
        self, spec: Question, obs: Observation, started: float
    ) -> Decision:
        """判断点 ③：找播放控件 + 判是否仅预告。

        ``trailer_only`` **以代码词表为准，不以模型为准**：预告片是用户
        明确的业务规则（「播放按钮或剧集按钮文本是预告片即无正片」），
        模型只能作为交叉印证。反过来做的话，一个把「预告·第1季正片」
        判成 only 的模型会把正片站点写进负样本池——**不可逆**。

        **ref 同理以词表为准——模型给不给都给代码核实，缺了就由词表取回。**
        判据有两道，缺一不可：

        - 模型给的 ref **必须在观察里**（白名单），编造的 ref 直接判
          ``None``——点了不存在的元素，报错会掩盖真实原因；
        - 模型**没**给 ref 时不是「判不了」，而是**由代码从词表取回第一个
          非预告控件**。实测 v.qq.com 就是这样丢掉的：模型看得到按钮却
          忘了回传字段，于是判 ``None`` → 不点击 → 没有 ``player_obs``
          → 判断点 ④ 无从判起 → **整批 D-2 全是 undetermined**。
          采集链在这里断掉，代价是**这一站整条轨迹作废**，而不点就等于
          把「模型忘了填字段」写成「这个站看不了」。

        这里**绝不判 False**：负样本不可逆，而「模型没回字段」与
        「页面没有播放控件」是两件事，前者没有任何证据支撑后者。
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
        suspects = [
            schema_parse.as_str(x)
            for x in _rows(schema_parse.field(data, "trailer_suspect"))
        ]
        suspects = [s for s in suspects if s]

        if ref:
            label = next(
                (e.name for e in obs.interactive_elements if e.ref == ref), ""
            )
            verdict = classify_trailer(label)
            if verdict == "only":
                return self._answer(
                    spec,
                    True,
                    evidence=f"ref={ref} 文本 {label!r} 命中预告片词表",
                    started=started,
                    payload={
                        "ref": "",
                        "trailer_only": True,
                        "trailer_suspect": [],
                        "why": "词表判定",
                    },
                )
            if verdict == "suspect" and not suspects:
                suspects = [label]
            return self._answer(
                spec,
                True,
                evidence=f"选定 ref={ref} 文本 {label!r}；{_evidence(data, spec, True)}"
                + _wordlist_note(ref, label, obs),
                started=started,
                payload={
                    "ref": ref,
                    "trailer_only": False,
                    "trailer_suspect": suspects,
                    "why": schema_parse.as_str(schema_parse.field(data, "evidence")),
                },
            )

        # ── 无 ref：代码把 ref 取回来，不让采集链在这里断掉 ──────
        # 词表是确定性事实层，「模型回传了哪个 ref」只是它的读数。
        picked = play_control_candidates(obs)
        if picked:
            first = picked[0]
            return self._answer(
                spec,
                True,
                evidence=(
                    f"模型未回传 ref；代码按词表从本页 {len(picked)} 个非预告"
                    f"播放控件中取 ref={first.ref}（文本 {first.name!r}）——"
                    f"**词表说了算**，不以模型是否回传字段为准"
                ),
                started=started,
                payload={
                    "ref": first.ref,
                    "trailer_only": False,
                    "trailer_suspect": suspects,
                    "why": schema_parse.as_str(schema_parse.field(data, "why"))
                    or "模型未回传 ref，代码按词表取回",
                },
            )
        # ── 词表里只剩预告控件：这是 trailer_only，不是「没有控件」────
        # 口径必须与 :meth:`RulePerceptor._find_play_control` 一致。原来
        # LLM 版没有这一支，直落 False，于是同一个页面 W1 记
        # ``trailer_only``、W3 记 ``no_play_control``——两条不同的负分支，
        # 而「该站只有预告片」与「该站压根没有播放控件」在训练里是两件事。
        trailers = trailer_only_candidates(obs)
        if trailers:
            trailer_suspects = [
                e.name for e in trailers if classify_trailer(e.name) == "suspect"
            ]
            if not trailer_suspects:
                trailer_suspects = suspects
            return self._answer(
                spec,
                True,
                evidence=(
                    f"{len(trailers)} 个候选控件全为预告："
                    f"{[e.name for e in trailers][:3]}"
                ),
                started=started,
                payload={
                    "ref": "",
                    "trailer_only": not trailer_suspects,
                    "trailer_suspect": trailer_suspects,
                    "why": "候选控件文本均命中预告片词表",
                },
            )
        if suspects:
            return self._answer(
                spec,
                True,
                evidence=f"候选疑似预告：{suspects[:3]}",
                started=started,
                payload={
                    "ref": "",
                    "trailer_only": False,
                    "trailer_suspect": suspects,
                    "why": "模型线索",
                },
            )
        return self._answer(
            spec,
            False,
            evidence=(
                f"{len(obs.interactive_elements)} 个交互元素中，"
                f"词表与模型均未找到非预告播放控件：{_evidence(data, spec, False)}"
            ),
            started=started,
            payload={
                "ref": "",
                "trailer_only": False,
                "trailer_suspect": [],
                "why": "词表与模型一致",
            },
        )

    def _player_ok(self, spec: Question, obs: Observation, started: float) -> Decision:
        """判断点 ④：播放页是否可用。``<video>`` 存在性已在主入口处理。

        **付费墙在代码层先判**（口径一「确定性事实优先」）：页面上写着
        「开通会员 / 订阅后观看」是**本项目的业务规则**，模型不知道也不该猜——
        它多半会答 True（页面确实有播放器）。而用户裁定「有播放页但要付费
        **不算成功**」，所以这一支必须落 :data:`questions.PAY_WALL` 而不是
        笼统的 ``component_unverified``。

        **只在模型也判 False 时才升级为付费墙**：模型判 True 说明它认为页面
        可用，此时词表命中多半是导航栏文案（实测百度页正文就有「免费观看」）。
        两边一致才落细分分支——**任何一边不一致都按未定的那个结论记**。

        2026-10-12 阶段 1：付费墙判定时**同时**读 LLM 给的 ``paywall_type``，
        仅 ``preview`` / ``trial`` 走 :data:`questions.PAY_WALL` 的细分——
        ``preview``（试看/前几集免费）落 :data:`partial_free`，
        其余形态仍落订阅墙。模型未给出合法 paywall_type 时，词表命中
        但 LLM 没细分，按现状保留订阅墙。

        2026-10-12 增量十五：``paywall=True`` + LLM 未给 ``paywall_type`` 时
        code 从 evidence 关键词反推（``infer_paywall_type_from_evidence``）：
        B 站"大会员抢先看" —— LLM 给 answer=false 但 paywall_type=null。
        仅在 ``paywall=True`` 时启用——partial_free 本就是付费墙的细分。

        2026-10-12 增量十六 · P4a：fallback 输入源改为 ``page_quotes``（页面
        原文短语字段），不是 evidence。避免 LLM 自述 vs 页面原文混淆。
        """
        data, why = self._ask_json(spec, _render(obs))
        if data is None:
            return self._unresolved_answer(spec, why, started)
        answer, why_null = _bool_or_reason(data)
        if answer is None:
            return self._unresolved_answer(spec, why_null, started)

        hit = classify_paywall(obs.body_text)
        ev_text = _evidence(data, spec, answer)
        # 解析 LLM 给的 paywall_type（fail-soft：不在合法集回 None）
        raw_ptype = schema_parse.as_str(schema_parse.field(data, "paywall_type"))
        # P4a 增量十六：fallback 输入源用 page_quotes，不再用 evidence。
        # 2026-10-12 增量二十一：fallback 走 ``_quotes_with_fallback``，LLM
        # 没给 page_quotes 时从 evidence 后处理抽业务引文段。
        quotes_text = " ".join(_quotes_with_fallback(data))
        ptype, ptype_fallback_note = _resolve_field(
            raw_ptype, PAY_WALL_TYPES, infer_paywall_type_from_evidence,
            quotes_text, label="paywall_type",
        )

        paywall = bool(answer is False and hit)
        evidence = ev_text
        if paywall:
            evidence += (
                f"；页面含付费墙信号「{hit}」——按 2026-10-10 口径，"
                f"**有播放页但需订阅/付费不算成功**"
            )
            if ptype is None:
                evidence += (
                    "（模型未给出合法 paywall_type，page_quotes 也无 paywall 关键词——"
                    "决策点④细分未启用，落 subscription_wall；"
                    "如实际是试看，请改判 partial_free）"
                )
            evidence += ptype_fallback_note
        payload: dict[str, Any] = {
            "media_count": obs.video_tag_count,
            "iframe_count": obs.iframe_count,
            "paywall": paywall,
            "paywall_type": ptype if paywall else None,
            "page_quotes": _quotes_with_fallback(data),  # 同步进 payload；增量二十一
            **({"paywall_word": hit} if paywall else {}),
        }
        return self._answer(
            spec, answer, evidence=evidence, started=started, payload=payload,
        )

    # ── 路径 B/C 发现链（2026-10-16）───────────────────────────────

    def _select_search_results(
        self, spec: Question, obs: Observation, started: float
    ) -> Decision:
        """①B / ①C：从搜索结果页选出聚合站 / 文章候选。

        ① 模式（与 :meth:`_select_play_sites` 同口径）：**零选中不是
        「这页没有聚合站/文章」**（那是 False 的意思），而是没给出选题
        结论——fail-closed。负样本由 rejected 逐候选点名产生（调用方
        按 ``_apply_selection`` 同款路径记 ``not_aggregator`` /
        ``not_article``），不在这一层整页判 False。

        与 ① 的差异只有一处：**无片名判据**——①B 的查询是通用聚合词
        （与片名无关）、①C 的判据是页面形态，链接文本里没有可以
        ``_title_link_sufficiency`` 匹配的东西，预检只查链接非空。
        """
        return self._select_from_links(
            spec, obs, started, empty_is_conclusion=False
        )

    def _select_page_links(
        self, spec: Question, obs: Observation, started: float
    ) -> Decision:
        """⑥ / ⑥C：从聚合站页 / 文章页选出正式入口 / 引荐外链。

        ⑥ 模式（与 ① 刻意不同口径）：题问的是「**这个页面**上的入口」，
        范围收敛到单页——**排除全部候选后零选中是对本页的真结论**
        （answer=False → ``aggregator_no_entries`` / ``article_no_links``），
        不是「没答」。搜索页噪声占多数（① 零选中多半是判不动），
        单页则每个链接都是待判定项，「全都排除了」本身携带信息。
        """
        return self._select_from_links(
            spec, obs, started, empty_is_conclusion=True
        )

    def _select_from_links(
        self,
        spec: Question,
        obs: Observation,
        started: float,
        *,
        empty_is_conclusion: bool,
    ) -> Decision:
        """四道 SELECT_* 新题的共享实现。**URL 必须来自观察，模型不能编。**

        与 :meth:`_select_play_sites` 的骨架一致（解析 → 编号回指 →
        白名单落位 → 按选中/排除分派），差别只在「零选中」一支的
        归宿由 ``empty_is_conclusion`` 决定——①′ 落 None（没结论），
        ⑥′ 落 False（真结论）。抽出来共享而不是复制四份：四份拷贝
        里改三份漏一份的漂移，比口径差异本身难查得多。
        """
        raw, why = self._ask(
            spec, _render(obs, target_title=self._target_title, question=spec.id)
        )
        if raw is None:
            return self._unresolved_answer(spec, why, started)
        data = schema_parse.extract_json(raw)
        if data is None:
            return self._unresolved_answer(spec, _why_unparsable(raw), started)

        known = {link.href for link in obs.links}
        window = _link_window(obs, spec.id, self._target_title)
        raw_selected = _rows(schema_parse.field(data, "selected", []))
        raw_rejected = _rows(schema_parse.field(data, "rejected", []))
        selected = _resolve_rows(raw_selected, window, known)
        rejected = _resolve_rows(raw_rejected, window, known)

        if not selected and not rejected:
            # 「模型给了条目但全对不上」与「模型压根没作答」是两件事
            # （① 的实测教训，理由见 _select_play_sites 同款分支）。
            if not raw_selected and not raw_rejected:
                return self._unresolved_answer(
                    spec,
                    f"模型未给出任何 selected/rejected（送进模型 "
                    f"{len(window)} 条链接）——它**没答这道题**，"
                    f"不是答「这页没有可选目标」；模型原文摘录：{_excerpt(raw)}",
                    started,
                )
            return self._unresolved_answer(
                spec,
                f"模型给出的 {len(raw_selected)} 个 selected / {len(raw_rejected)}"
                f" 个 rejected 全部不在观察的 {len(known)} 个链接里，无法采信",
                started,
            )
        if not selected:
            if empty_is_conclusion:
                # ⑥ 模式：单页上全排除 = 真结论。evidence 带上点名理由——
                # 这是负样本池里「为什么没有入口」的唯一解释。
                reasons = "；".join(
                    r.get("reason") or ""
                    for r in rejected
                    if isinstance(r.get("reason"), str)
                )[:160]
                return self._answer(
                    spec,
                    False,
                    evidence=(
                        f"本页 {len(known)} 个链接全部排除"
                        f"（{len(rejected)} 条点名：{reasons or '未给出理由'}）"
                        f"——单页零选中是真结论，不是没答"
                    ),
                    started=started,
                    payload={"selected": [], "rejected": rejected},
                )
            # ① 模式：零选中 = 没给出选题结论，fail-closed（① 同款理由：
            # 放它过去会让整批一个候选都不访问，只留一堆 unresolved）。
            return self._unresolved_answer(
                spec,
                f"模型排除了 {len(rejected)} 个链接但一个都没选中——"
                f"**这不是「这页没有可选目标」**（那是 False），"
                f"而是没给出选题结论，不判",
                started,
            )
        return self._answer(
            spec,
            True,
            evidence=(
                f"自 {len(known)} 个链接中选出 {len(selected)} 个"
                f"（已剔除不在观察中的 url），{len(rejected)} 个排除"
            ),
            started=started,
            payload={"selected": selected, "rejected": rejected},
        )

    def _classify_search_links(
        self, spec: Question, obs: Observation, started: float
    ) -> Decision:
        """方案 12 阶段 A1：候选链接四分类（一道题统一三条发现路径）。

        与 :meth:`_select_from_links` 的骨架一致（解析 → 编号回指 →
        白名单落位），差别在语义结构：SELECT_* 是「按路径预置的二元筛选」
        （选中/排除），本题是「类型判定」——每条链接归入
        ``video_site / aggregator / article_post / noise`` 之一，**按哪类
        保留、按哪类记负由 orchestrator 按发现路径分发**（direct 留
        video_site 记 not_play_site、B 留 aggregator 记 not_aggregator、
        C 留 article_post 记 not_article）。

        ⚠️ **answer 没有 False**：分类是逐条的类型判定，「这批里没有目标
        类型」是 orchestrator 从 payload 推导的分发结论（kept=0），不是
        本题的整页结论——把三种路径的 False 语义塞回一道题，等于把
        参数化刚拆开的东西又粘回去。True = 分类可用（至少一条对上了
        观察里的链接）；None = 没答 / 全对不上（与 SELECT_* 同款两分支，
        两种「零」是两件事，理由见 :meth:`_select_play_sites`）。

        「漏分类的链接不计入任何结论」由 prompt 声明、由 orchestrator
        兜底：未点名的候选按无证据处理（unresolved），与 rejected 未点名
        同一条纪律。
        """
        raw, why = self._ask(
            spec, _render(obs, target_title=self._target_title, question=spec.id)
        )
        if raw is None:
            return self._unresolved_answer(spec, why, started)
        data = schema_parse.extract_json(raw)
        if data is None:
            return self._unresolved_answer(spec, _why_unparsable(raw), started)

        known = {link.href for link in obs.links}
        window = _link_window(obs, spec.id, self._target_title)
        classes: dict[str, list[dict[str, Any]]] = {}
        raw_total = 0
        for cls in questions.CLASSIFY_CATEGORIES:
            raw_rows = _rows(schema_parse.field(data, cls, []))
            raw_total += len(raw_rows)
            # 编号回指 + URL 白名单（与 _resolve_rows 同款）：模型回 ``i``
            # 落到窗口链接上，回不到编号退 URL 匹配，都落空的整条丢弃
            # ——编出来的链接会让控制流去访问不存在的站点。
            classes[cls] = _resolve_rows(raw_rows, window, known)

        classified = sum(len(rows) for rows in classes.values())
        if not classified:
            if not raw_total:
                return self._unresolved_answer(
                    spec,
                    f"模型四个类别数组全空（送进模型 {len(window)} 条链接）"
                    f"——它**没答这道题**；模型原文摘录：{_excerpt(raw)}",
                    started,
                )
            return self._unresolved_answer(
                spec,
                f"模型给出的 {raw_total} 个条目全部不在观察的 "
                f"{len(known)} 个链接里，无法采信",
                started,
            )
        return self._answer(
            spec,
            True,
            evidence=(
                f"自 {len(known)} 个链接分类：video_site "
                f"{len(classes['video_site'])}、aggregator "
                f"{len(classes['aggregator'])}、article_post "
                f"{len(classes['article_post'])}、noise "
                f"{len(classes['noise'])}"
                f"（已剔除不在观察中的条目；未点名的 "
                f"{len(known) - classified} 条不计入结论）"
            ),
            started=started,
            payload=classes,
        )

    def _is_aggregator(
        self, spec: Question, obs: Observation, started: float
    ) -> Decision:
        """⑤：站点级确认——这个页面是不是影视聚合站 / 导航站。

        ② 模式（页面级确认）但更简单：没有 login/region 那样的既定
        细分法，answer 即结论。False → ``not_aggregator``（负样本：
        这个候选实际不是聚合站）；None → fail-closed，控制流继续走
        下一步（与 ①② 同口径，不中断采集）。
        """
        data, why = self._ask_json(spec, _render(obs))
        if data is None:
            return self._unresolved_answer(spec, why, started)
        answer, why_null = _bool_or_reason(data)
        if answer is None:
            return self._unresolved_answer(spec, why_null, started)
        return self._answer(
            spec,
            answer,
            evidence=_evidence(data, spec, answer),
            started=started,
            payload={"title": obs.page_title},
        )

    def _find_site_search(
        self, spec: Question, obs: Observation, started: float
    ) -> Decision:
        """⑦：站点首页上找站内搜索框。③ 模式同款——ref 白名单 + 词表兜底。

        - 模型回传 ref 且在观察里 → True（``payload["ref"]`` 即控制流
          要 ``fill`` 片名的那个框）；
        - 回传 ref 但不在观察里 → None（编造的 ref 会让代码填错框，
          与 ③ 的铁律 3 同一条）；
        - 未回传 ref → **词表取回**（:func:`search_box_candidates`）——
          「模型看得到框却忘了回传字段」与 ③ 同形态（v.qq.com 实测：
          漏字段 → 判 None → 整站轨迹作废），采集链不能在这里断；
        - 词表也取不到 → False：**真结论**（这个站没有站内搜索框），
          落 ``no_site_search`` 负样本。搜索框没有 ③ 那样的预告/
          suspect 中间形态——框要么在元素表里，要么不在。
        """
        raw, why = self._ask(spec, _render(obs))
        if raw is None:
            return self._unresolved_answer(spec, why, started)
        data = schema_parse.extract_json(raw)
        if data is None:
            return self._unresolved_answer(spec, _why_unparsable(raw), started)

        # ── ref 白名单校验（铁律 3，③ 同款）────────────────────────
        ref = schema_parse.as_str(schema_parse.field(data, "ref"))
        known = {e.ref for e in obs.interactive_elements}
        if ref and ref not in known:
            return self._unresolved_answer(
                spec,
                f"模型回传 ref={ref!r} 不在观察的 {len(known)} 个元素里"
                f"（{sorted(known)[:5]}）——编造的 ref 会让代码填错框",
                started,
            )
        why_str = schema_parse.as_str(schema_parse.field(data, "why"))
        if ref:
            label = next(
                (e.name for e in obs.interactive_elements if e.ref == ref), ""
            )
            return self._answer(
                spec,
                True,
                evidence=f"选定搜索框 ref={ref}（文本 {label!r}）；{why_str}",
                started=started,
                payload={"ref": ref, "why": why_str},
            )

        # ── 无 ref：代码把框取回来，不让采集链在这里断（③ 同款）────
        picked = search_box_candidates(obs)
        if picked:
            first = picked[0]
            return self._answer(
                spec,
                True,
                evidence=(
                    f"模型未回传 ref；代码按词表从 {len(picked)} 个搜索框"
                    f"候选中取 ref={first.ref}（文本 {first.name!r}，"
                    f"kind={first.role}）——**词表说了算**，"
                    f"不以模型是否回传字段为准"
                ),
                started=started,
                payload={
                    "ref": first.ref,
                    "why": why_str or "模型未回传 ref，代码按词表取回",
                },
            )
        return self._answer(
            spec,
            False,
            evidence=(
                f"{len(obs.interactive_elements)} 个交互元素中，"
                f"词表与模型均未找到站内搜索框——首页型候选无法"
                f"定位目标片名，按无搜索框记负样本"
            ),
            started=started,
            payload={"ref": "", "why": "词表与模型一致：无搜索框"},
        )

    # ── 内部 ────────────────────────────────────────────────────────

    def _ask(
        self, spec: Question, rendered: Mapping[str, Any]
    ) -> tuple[str | None, str]:
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
            + "输出格式必须严格是："
            + CONTRACTS[spec.id]
            + "\n证据不足时 answer 给 null，不要猜。"
        )
        user = json.dumps(
            {"question": spec.id, "observation": rendered}, ensure_ascii=False
        )
        got = self._client.chat(system, user)
        if got is None:
            return None, f"LLM 不可用（{self._client.last_error or '未知原因'}）"
        return got, ""

    def _ask_json(
        self, spec: Question, rendered: Mapping[str, Any]
    ) -> tuple[Mapping[str, Any] | None, str]:
        raw, why = self._ask(spec, rendered)
        if raw is None:
            self._unresolved.add(spec.id)
            return None, why
        data = schema_parse.extract_json(raw)
        if data is None:
            self._unresolved.add(spec.id)
            return None, _why_unparsable(raw)
        return data, ""

    def _unresolved_answer(
        self, spec: Question, reason: str, started: float
    ) -> Decision:
        """fail-closed 的唯一出口。**evidence 里必须带上原因。**

        原因不能省：它解释了为什么这一站没有落到负样本池，是人工复核
        时判断「该重跑还是该认了」的唯一线索。
        """
        self._unresolved.add(spec.id)
        return Decision(
            question=spec.id,
            answer=None,
            confidence=CONFIDENCE_NONE,
            evidence=reason or "未取得结论",
            source="llm",
            payload=MappingProxyType({}),
            latency_ms=int((time.monotonic() - started) * 1000),
            fallback_used=True,
        )

    def _answer(
        self,
        spec: Question,
        answer: bool | None,
        *,
        evidence: str,
        started: float,
        payload: Mapping[str, Any] | None = None,
        fallback: bool = False,
    ) -> Decision:
        return Decision(
            question=spec.id,
            answer=answer,
            confidence=CONFIDENCE_JUDGED,
            evidence=evidence,
            source="llm",
            payload=MappingProxyType(dict(payload))
            if payload
            else MappingProxyType({}),
            latency_ms=int((time.monotonic() - started) * 1000),
            fallback_used=fallback,
        )


# ═══════════════════════════════════════════════════════════════════════
# 采集充分性预检（铁律 2）
# ═══════════════════════════════════════════════════════════════════════


def _rescued_paywall_decision(decision: Decision, rescue: str) -> Decision:
    """②③ 负分支付费墙救济（INC43）：给已判 False 的决策补付费墙标记。

    不改 ``answer`` / ``confidence`` / ``source``——LLM 的「页面不可用」
    结论保持原样，救济只做一件事：把「不可用的真因是付费墙」这个
    **确定性代码事实**（:func:`paywall_rescue_evidence`）补进 payload 与
    evidence，让 :func:`~trajectory_pipeline.executor.branches.decision_branch`
    把分支从 ``login_wall_or_blocked`` / ``no_play_control`` 改判为
    ``subscription_wall``。

    evidence 里必须能指认判据（命中的词 / 信号组合）——救济改写了分支，
    人工复核时这条 evidence 是「为什么不是登录墙」的唯一解释。
    """
    payload = dict(decision.payload or {})
    payload["paywall"] = True
    payload["paywall_word"] = rescue
    return Decision(
        question=decision.question,
        answer=decision.answer,
        confidence=decision.confidence,
        evidence=(
            f"{decision.evidence}\n[付费墙救济] 正文含{rescue}——"
            f"②③ 负分支的真因是付费墙，按 2026-10-10 口径记订阅墙"
        ),
        source=decision.source,
        payload=MappingProxyType(payload),
        latency_ms=decision.latency_ms,
        fallback_used=decision.fallback_used,
    )


def _sufficiency(question: str, obs: Observation, *, target_title: str = "") -> str:
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
    if question == Q.CLASSIFY_SEARCH_LINKS:
        # 方案 12 阶段 A1：分类题读的是**候选视图**（orchestrator 构造的
        # 投影观察，links=候选），判据只有链接表非空一条。通用守卫
        # 「正文 0 字符且交互元素 0 个」守的是页面渲染完整性，对投影
        # 观察是假阳性——候选能从搜索页提取出来，「页面渲染过」这件事
        # 已由上游环节核过；视图的交互元素本来就刻意清空。
        if not obs.links:
            return "分类题收到 0 条链接（候选视图为空），无从分类，不判"
        return ""
    if question in _LINK_SELECTION_QUESTIONS and not obs.links:
        # 路径 B/C 的四道选择题（2026-10-16）。与 ① 的零链接守卫同一条
        # 纪律，但**无片名判据**（①B 通用查询 / ①C 判页面形态 / ⑥′ 判
        # 单页链接），_title_link_sufficiency 一概不适用——空链接表就是
        # 这道题唯一的「输入不支持」形态。
        return (
            f"采到 0 个链接（正文 {len(obs.body_text)} 字符）——"
            f"选择题的输入就是链接表，哨兵返回无法区分"
            f"「页面没渲染完」与「页面确实没有链接」，不判"
        )
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
    if question == Q.FIND_SITE_SEARCH and not obs.interactive_elements:
        # ⑦ 与 ③ 同一条纪律、同一个理由（2026-10-16 路径 B/C）：这道题
        # 唯一的输入就是元素表，空表分不清「首页没渲染完」与「首页
        # 确实没有搜索框」——后者是 ``no_site_search`` **负样本**，
        # 把前者写成后者，就是把采集失败伪装成「这个站没有站内搜索」。
        return (
            f"采到 0 个交互元素（正文 {len(obs.body_text)} 字符）——"
            f"哨兵返回无法区分「页面没渲染完」与「页面确实没有搜索框」，"
            f"不判"
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


def _ordered_links(obs: Observation, question: str, target_title: str) -> list:
    """① 题把「链接文本含目标片名」的链接提到最前，其余保持原序。

    **为什么必须有这一步**：预检（:func:`_title_link_sufficiency`）与渲染
    （:func:`_render`）原先看的是**两个不同的窗口**——预检扫全量
    ``obs.links``，渲染只送前 ``LLM_LINKS`` 条。

    实测（2026-10-10，baidu 搜「功夫 在线观看」，123 条链接）暴露了这个错位：
    百度结果页的链接顺序是**导航在前、结果在后**——前 12 条是
    「登录 / 抗击肺炎 / 新闻 / hao123 / 地图 / 视频 / 贴吧 / 学术 / 设置」，
    真正写着「《功夫》电影完整版-爱奇艺」的结果标题排在它们之后。
    于是预检数到 3 条命中就放行，而模型收到的前 20 条**全是导航**——
    它没有一次机会看到片名，于是判 True 且 selected/rejected 全空，
    5 个候选全部落进 ``not_play_site`` 负样本池，evidence 写着
    「模型未给出排除理由」。

    **这是「预检说有判别力」与「模型真的看得到判别力」两句话说的不是同一件事。**
    错位只在「链接顺序随引擎改版而变」时才显形，因此不能靠调阈值补——
    两处改成同源之后，它们是同一条判据在同一批链接上的两个说法。

    排序是**确定性**操作（子串匹配），不是语义判断——控制流只负责按答案分流，
    「哪些链接写着片名」是字符串事实，交给 LLM 反而不可靠。
    """
    links = list(obs.links)
    if question != Q.SELECT_PLAY_SITES or not target_title.strip():
        return links
    hit, miss = [], []
    for link in links:
        (hit if target_title in (link.text or "") else miss).append(link)
    return hit + miss


def _link_window(obs: Observation, question: str, target_title: str) -> list:
    """**送给模型的那批链接**——排序后再截断，全树唯一的定义。

    三处都要它，且必须三处同源：:func:`_render`（渲染）、:func:`_sufficiency`
    的 ① 判别力检查（预检）、:func:`_select_play_sites`（把模型的 ``i``
    映射回 href）。任何一处自己再拼一遍 ``_ordered_links(...)[:LLM_LINKS]``，
    就多一个能悄悄错位的点——预检与渲染曾经就是这么错位过一次，
    后果是模型收到的前 20 条全是导航栏（见 :func:`_ordered_links`）。
    """
    return _ordered_links(obs, question, target_title)[:LLM_LINKS]


def _title_link_sufficiency(obs: Observation, target_title: str) -> str:
    """① 的判别力检查：链接文本里有没有片名。

    判据**只用链接自身的文本**，不看 ``body_text``。body_text 里当然有
    片名（实测一份观察含「功夫」16 次），但它是一条无结构的文本流，
    里面的片名与 ``links[]`` 的 URL **没有任何对应关系**——模型无从把
    「这条标题」和「那个 URL」配上，对不上就是猜。猜出来的 ``not_play_site``
    是负样本，而实测已经出现过它与同一 URL 的**成功记录直接矛盾**。

    真正的修法是让采集层把标题带进 ``links[]``（见 ``steps/search.py``
    的 ``RESULT_SELECTORS``）；在那之前，这道题只能在输入有判别力时才问。

    ⚠️ **判据用的链接集合必须与 :func:`_render` 送给模型的完全相同**，
    即 :func:`_ordered_links` 的输出**再截到 ``LLM_LINKS``**。

    .. note::
       **这一层当前不改变行为，别为它写行为断言。**
       前置排序已经保证「只要全量里有命中，重排后命中项必在前
       ``LLM_LINKS`` 内」，所以扫窗口与扫全量结果恒等——变异测试证过：
       把这一行改回扫全量，**34 条测试照样全绿**（等价变异）。
       它留在这里是**结构性防漂移**：将来有人改 :func:`_render` 的排序
       或截断长度，两处仍在同一批链接上，判据不会悄悄退化成
       「全集 vs 前缀」。

       真正让 5 个候选落进 ``not_play_site`` 的是 :func:`_render`——
       拿掉那里的 :func:`_ordered_links`，3 条测试变红。
       **不要把这一层当防线来依赖，也不要给它配假断言**：
       「死守卫比没守卫更坏」（它看起来在限定范围，于是没人再去验证
       真正起作用的那道）。
    """
    title = (target_title or "").strip()
    if not title:
        # 没有目标片名就无从匹配。这不是「判为否」，是问错了题。
        return "未绑定目标片名，① 无法匹配，不判"
    window = _link_window(obs, Q.SELECT_PLAY_SITES, title)
    hits = sum(1 for link in window if title in (link.text or ""))
    if hits:
        return ""
    return (
        f"{len(obs.links)} 个候选链接里，送进模型的 {len(window)} 条中"
        f"没有一条含《{title}》"
        f"（实测 bing 只给面包屑、baidu 把导航栏排在结果之前，"
        f"片名在 body_text 里但与 URL 无对应）——输入不支持本题，不判"
    )


# ═══════════════════════════════════════════════════════════════════════
# 渲染与工具
# ═══════════════════════════════════════════════════════════════════════


def _render(
    obs: Observation, *, target_title: str = "", question: str = ""
) -> dict[str, Any]:
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
        # 键名跟着**方言**走（P4）：obscura 面写 ``tag``/``label``、camoufox 面
        # 写 ``role``/``name``。模型看到的就是这两个词——把 HTML 标签名
        # 叫 ``role`` 会让它在 camoufox 面的任务里按标签名去找 role。
        "interactive_elements": [
            element_json(e, obs.element_vocabulary, name_limit=60)
            for e in obs.interactive_elements[:LLM_ELEMENTS]
        ],
        "elements_total": len(obs.interactive_elements),
        "video_tag_count": obs.video_tag_count,
        "iframe_count": obs.iframe_count,
    }
    if obs.links:
        # 与 :func:`_title_link_sufficiency` 同源：同一批链接、同一个顺序、
        # 同一个截断长度（:func:`_link_window`）。不同源就是「预检放行了
        # 模型答不了的题」——那正是 2026-10-10 踩过的坑。
        window = _link_window(obs, question, target_title)
        # ① 题给每条链接编号，并要求模型回编号而不是回 URL：
        # **回长 URL 是记忆任务，回编号是复制任务**，出错率差一个量级——
        # 模型会顺手改写 scheme、吞掉 www、丢掉 query 尾巴，于是
        # 「选的 URL 压根不在观察里」整批作废。编号把它降级成纯复制，
        # 而回传后的 href 由代码从窗口里取，模型碰不到字符串。
        # 2026-10-16 路径 B/C：四道新 SELECT_* 同享编号契约
        # （_NUMBERED_LINK_QUESTIONS），与 handler 的 ``i`` 回指同源。
        numbered = question in _NUMBERED_LINK_QUESTIONS
        links = [{"text": l.text[:60], "href": l.href} for l in window]
        if numbered:
            links = [{"i": n, **row} for n, row in enumerate(links)]
        out["links"] = links
        out["links_total"] = len(obs.links)
    if target_title:
        out["target_title"] = target_title
    if obs.degraded:
        out["degraded"] = list(obs.degraded)
    return out


def _excerpt(raw: str, limit: int = 120) -> str:
    """模型原文的诊断摘录——**先剥思考再截**（与 :func:`_why_unparsable`
    同一条红线：``evidence`` 是 :class:`SiteOutcome` 的持久字段，raw CoT
    不进任何持久化路径，见 CLAUDE.md）。

    空回传分支（selected/rejected 全空）用它把「模型到底回了什么」留在
    evidence 里，才能判断根因是「它认为没有够格的站 / 它偷懒 / 它输出
    最省事的合规解」——三者的处置完全不同。剥完为空单独说：那是「只输
    出思考没有正文」，与预算耗尽的症状同源但不同因。
    """
    clean = schema_parse.strip_think(raw).strip()
    if not clean:
        return "（模型输出剥掉思考后为空——只输出了 reasoning，没有正文）"
    return clean[:limit].replace("\n", " ")


def _why_unparsable(raw: str) -> str:
    """解析失败的原因串。**带上模型实际说了什么——但剥掉思考过程。**

    只写「无法解析为 JSON」的话，模型输出格式漂移这件事没法从存档里
    看见——而它恰恰是最该按批次统计的东西（某个后端版本开始套 markdown
    fence，是从存档里一眼看出来的，不是从报错里）。

    ⚠️ **必须先** :func:`~trajectory_pipeline.llm.schema_parse.strip_think`
    再截前 120 字，否则 :meth:`LLMPerceptor.decide` 会把 **raw CoT**
    写进 ``evidence``——那是 CLAUDE.md 红线「不保存 raw CoT」的一条真实
    破口：``evidence`` 是 :class:`SiteOutcome` 的持久字段，直接落 P1。

    实测形态确凿（2026-10-11 核对时构造）::

        <think>模型内部推理：链接里有 5 条像结果…</think>根据页面观察，我无法确定。

    前 120 字**全是思考**。而这不是罕见分支——解析失败本身在真实批次里
    是常态（散文、格式漂移、markdown fence），推理模型又恰好人人带 think，
    两件事撞上就漏。

    剥完为空是另一件事，单独说：那是「模型只吐了思考、没有正文」，
    症状与 :class:`~trajectory_pipeline.llm.client.LLMClient` 报的预算耗尽
    同源但不同因（那个压根没返回正文），所以要能从 evidence 里认出来。
    """
    clean = schema_parse.strip_think(raw).strip()
    if not clean:
        return (
            "模型输出剥掉思考过程后为空——只输出了 reasoning，"
            "没有正文（推理模型形态；查 max_tokens 预算或后端行为）"
        )
    head = clean[:120].replace("\n", " ")
    return f"LLM 有输出但无法解析为 JSON（前 120 字）：{head}"


def _resolve_rows(
    rows: list[dict[str, Any]], window: list, known: set[str]
) -> list[dict[str, Any]]:
    """把模型回的行落到**观察里真实存在**的链接上。

    两条路径，先编号后 URL：编号 ``i`` 是渲染时送给模型的那批的序号
    （见 :func:`_render`），回指它等于把「记忆一个长 URL」降级成
    「复制一个整数」；回不到编号再退回 URL 匹配，好让按旧契约回
    ``url`` 的后端不至于整批作废。

    命中的行补上 ``url`` 字段：下游（:func:`_apply_selection` 与记账）
    只认 ``url``，而它要的是**观察里的原始 href**，也就是
    ``Candidate.source_href`` 那一侧。

    对不上的行整条丢掉——留着会让控制流去访问模型编的地址，
    而那次访问会以「不可达」入负样本池，污染出一个并不存在的失败原因。
    """
    out: list[dict[str, Any]] = []
    for row in rows:
        idx = _as_index(row.get("i"))
        if idx is not None and 0 <= idx < len(window):
            href = window[idx].href
        else:
            href = schema_parse.as_str(row.get("url"))
        if href in known:
            out.append({**row, "url": href})
    return out


def _as_index(value: Any) -> int | None:
    """把模型回的编号读成 :class:`int`，读不出返回 ``None``。

    JSON 里的编号可能是 int 也可能是 str（模型爱给整数加引号）；
    ``bool`` 是 ``int`` 的子类，不排除的话 ``true`` 会被当成 1 号链接。
    """
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.strip().isdigit():
        return int(value.strip())
    return None


def _rows(value: Any) -> list[dict[str, Any]]:
    """把可能是 list/dict/None 的字段规整成 dict 列表。"""
    if isinstance(value, list):
        return [v for v in value if isinstance(v, dict)]
    if isinstance(value, dict):
        return [value]
    return []


def _wordlist_note(ref: str, label: str, obs: Observation) -> str:
    """模型给的 ref 未命中播放控件词表时的**标注**——标注，不是否决。

    图标按钮（纯 SVG / 文本挂在 ``aria-label`` 上 / ``<span>`` 套 ``<i>``）
    常过不了词表，而模型看得懂它的视觉语义，所以这里只留痕不改结论。
    但**必须留痕**：模型与词表不一致时，人得知道该信哪边——而禁止投票
    意味着分歧必须暴露给人看，不能由代码悄悄择一。

    命中词表时返回空串，不给 evidence 添噪音。
    """
    if any(e.ref == ref for e in play_control_candidates(obs)):
        return ""
    return (
        f"；⚠️ 该 ref 未命中播放控件词表（模型与词表不一致，"
        f"可能是图标按钮）——留痕待人工复核"
    )


def _evidence(data: Mapping[str, Any], spec: Question, answer: bool) -> str:
    """拼 evidence。**带上「置信度不是模型自报」这句。**"""
    why = schema_parse.as_str(schema_parse.field(data, "evidence")) or spec.prompt[:40]
    tail = f"（置信度 {CONFIDENCE_JUDGED} 为代码层先验，非模型自报）"
    return f"LLM 判 {answer}：{why}{tail}"


def _resolve_field(
    raw_value: str,
    valid_set: frozenset[str],
    infer_fn,
    evidence: str,
    *,
    label: str,
) -> tuple[str, str]:
    """LLM 给的细分字段 → 在 ``valid_set`` 即返回；否则 code fallback。

    返回 ``(final_value, fallback_note)``：
    - ``final_value`` 是合法值或空串（空串表示 fallback 也没命中）。
    - ``fallback_note`` 仅在启用 fallback 时非空，**非空串直接追加到
      evidence 末尾**——下游 ``record_from`` 写到 SiteOutcome 让复核看见
      「这是 code 推断的，不是 LLM 自报」。

    2026-10-12 增量十五。失败的姿势是「猜错」——空串绝不强给 unknown，
    让 ``decision_branch`` 按现状回落合并态。这是 fail-soft 的本意。
    """
    if raw_value and raw_value in valid_set:
        return raw_value, ""
    reason = "LLM 未给出" if not raw_value else f"LLM 给出非合法值 {raw_value!r}"
    inferred = infer_fn(evidence)
    if inferred:
        return inferred, (
            f"\n[code_fallback] {label}={inferred}（{reason}；"
            f"code 从 evidence 关键词反推）"
        )
    return "", ""


def _resolve_access(raw_value: str, evidence: str) -> tuple[str, str]:
    """``access_control_type`` 的 field resolver（`_resolve_field` 的
    简化包装）——单入口让 ``_is_reachable`` 看起来更对称。
    """
    return _resolve_field(
        raw_value, ACCESS_CONTROL_TYPES, infer_access_from_evidence, evidence,
        label="access_control_type",
    )


#: 2026-10-12 增量十六 · P4a：把 LLM 回传的 ``page_quotes`` 字段规整成
#: ``_resolve_field`` 能用的 query 文本。
#: - ``_quote_text``：list[str] 用空格拼成 query 文本——``infer_*`` 是
#:   字符串关键词匹配，**只**接 ``str`` 输入。
#: - ``_quote_list``：把 list 规整成 ``list[str]``，丢掉非字符串元素，
#:   落进 :attr:`Decision.payload` 供下游复核台看到。
#:
#: 防御性：LLM 给 ``page_quotes: ""`` 或 ``null`` 也按空处理（不抛）——
#: 这条契约不复杂：模型漏字段 = fallback 拿不到证据 = 落合态，本就是
#: fail-soft 设计的目标行为。
def _quote_text(data: Mapping[str, Any]) -> str:
    raw = schema_parse.field(data, "page_quotes")
    if not isinstance(raw, list):
        return ""
    parts: list[str] = []
    for q in raw:
        s = schema_parse.as_str(q)
        if s:
            parts.append(s)
    return " ".join(parts)


def _quote_list(data: Mapping[str, Any]) -> list[str]:
    raw = schema_parse.field(data, "page_quotes")
    if not isinstance(raw, list):
        return []
    return [s for s in (schema_parse.as_str(q) for q in raw) if s]


_BUSINESS_QUOTE_HINTS: tuple[str, ...] = (
    "VIP", "vip",
    "会员", "大会员",
    "登录", "注册",
    "播放", "播放视频", "立即播放", "立即",
    "设备", "地区", "版权",
    "错误", "出错",
    "不支持", "暂不支持", "不可播放",
    "限制", "受限",
    "验证", "验证码",
    "开通", "续费", "购买", "付费", "收费", "充值", "购买单集",
    "免费", "完整版", "试看", "抢先", "会员抢先",
    "已下架", "不存在",
    "404",
    "新客", "特惠", "优惠", "促销",
    "超清", "高清", "蓝光",
    "重试", "刷新", "抱歉", "请稍后",
)
_NEGATIVE_QUOTE_HINTS: tuple[str, ...] = (
    "没", "可验证", "找错站", "去了发现",
    "采集", "哨兵", "不可逆", "前置事实",
)


def _extract_business_quotes(text: str, *, max_phrases: int = 5) -> list[str]:
    if not text:
        return []
    # 开放类 = 「 + 三种直/弯双引号；闭类 = 」 + 三种直/弯双引号；
    # 否定类 = 上面 4 个全排（含 」），避免 capture 跨过右侧书名号。
    # 实测覆盖三种真实输入：
    #   "当前设备不支持播放"     (弯引号)
    #   「开通会员」            (书名号)
    #   "开通 VIP"            (ASCII 直引号 - 测试源用的是这种)
    pattern = re.compile(r'[「“”"]([^「“”」"\n]{2,50})[」“”"]')
    seen: set[str] = set()
    out: list[str] = []
    for m in pattern.finditer(text):
        s = m.group(1).strip()
        if not s or s in seen:
            continue
        if len(s) > 13:
            continue
        if not any(kw in s for kw in _BUSINESS_QUOTE_HINTS):
            continue
        if any(kw in s for kw in _NEGATIVE_QUOTE_HINTS):
            continue
        seen.add(s)
        out.append(s)
        if len(out) >= max_phrases:
            break
    return out


def _quotes_with_fallback(data: Mapping[str, Any]) -> list[str]:
    """2026-10-12 增量二十一：``page_quotes`` 三层兜底解析。

    优先级：LLM 回的 ``page_quotes`` 非空 -> 用它；否则从
    ``data["evidence"]`` 后处理抽出业务引文段（启发式过滤页面原文与
    LLM 评论）；都没有 -> ``[]``。

    这是 :func:`_quote_list` 的**增强版**——下游 ``_is_reachable`` /
    ``_player_ok`` 都改用它。当 P4a 第二轮 prompt 修改让 page_quotes
    留 0/7 时，这条 fallback 是兜底（兜的是旧存档 + 模型退化）。
    """
    primary = _quote_list(data)
    if primary:
        return primary
    ev = schema_parse.as_str(schema_parse.field(data, "evidence"))
    return _extract_business_quotes(ev)

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
