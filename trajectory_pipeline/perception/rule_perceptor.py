"""W1 规则版 Perceptor——**故意做得很弱**。

「弱」不是偷懒，是三层分工铁律的直接后果：事实层由代码管，那么
规则版**只配做确定性事实判定**，语义判断一律 ``answer=None``（I4）。
让规则版去「猜」语义，得到的不是弱信号而是**假信号**——它会污染负样本池，
让「这一站没播放控件」这个结论看起来有代码背书，实际是关键词匹配的幻觉。

W1 只有两件事是确定性的：

1. **``trailer_only``**——纯文本匹配，不需要理解页面（用户业务规则：
   播放控件文本是「预告片」即说明播放资源不存在）。
2. **``PLAYER_OK`` 的正向存在性**——**存在 ``<video>`` / ``<audio>`` 标签**
   时可判 True；**不存在时返回 None 而非 False**，因为 JS / canvas / iframe
   播放器都不产生这些标签，「没测到」≠「没有」。
   （iframe 数量**不作**判据：实测导航站 hao123 满屏 15 个 iframe，
   拿它当判据会把导航站判成播放页。）

其余两题（``SELECT_PLAY_SITES`` / ``IS_REACHABLE``）W1 全程 ``answer=None``。
这意味着 W1 批次**不会有正样本**——它是执行层的连通性验证批次，
素材价值主要在负样本池。正样本要等 W3 的 LLMPerceptor。
"""

from __future__ import annotations

import re
import time
from types import MappingProxyType
from typing import Mapping, Any

from trajectory_pipeline.perception.base import (
    Decision,
    InteractiveElement,
    Observation,
    Q,
)
from trajectory_pipeline.perception import questions

# ── 控件识别词表 ────────────────────────────────────────────────────────

#: 播放控件的**肯定**信号。命中即可进入候选池。
#: 注意这里只放「明确表示可以开始播放」的词，不放「与播放相关」的词——
#: 「播放历史」「观看记录」「播放设置」都相关但都不是播放入口。
PLAY_CONTROL_KEYWORDS: tuple[str, ...] = (
    "播放", "立即播放", "立即观看", "在线观看", "免费观看", "免费看",
    "观看", "看片", "看第", "正片", "开始观看", "观看全集", "看全集",
    "免费", "点击观看", "观看本期",
)

#: 否定信号，**优先级高于**肯定信号。先排除再匹配，否则
#: 「继续播放」会被当成播放控件、「观看记录」会挤进候选池。
PLAY_CONTROL_NEGATIVE: tuple[str, ...] = (
    "播放历史", "观看历史", "观看记录", "播放记录", "播放设置",
    "设置播放", "播放列表设置", "预告片设置", "倍速", "字幕设置",
    "播放方式", "自动播放", "暂停", "停止播放", "投屏",
)

#: 剧集项的**结构化**匹配——「有剧集即可」，用户业务规则明确不要求定位到第 N 集。
#:
#: 早期版本是 ``EPISODE_KEYWORDS = ("集", "话", "期", "章", "回")``，
#: 实测被击穿得很彻底：hao123 首页的「星**期**四」被判成剧集项，
#: 整条链跟着走进了一个导航站。所以剧集项必须**带结构**，不能只看单字——
#: 这五个字在中文日常用语里的密度高到毫无区分度（星期/期间/对话/机会）。
#: 要求「第 N 集」或 ``EP01`` / ``S01E02`` 这类编号形态即可。
EPISODE_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"第\s*\d+\s*[集话期章回]"),
    re.compile(r"\bEP?\s?\d{1,3}\b", re.IGNORECASE),
    re.compile(r"\bS\d{1,2}E\d{1,3}\b", re.IGNORECASE),
    # 量词之间一律允许空白：UI 文案里「全24集」和「全 24 集」都常见，
    # 写死 `\d+` 相邻会在带空格的那一半上直接漏掉。
    re.compile(r"正片|全\s*\d+\s*集|连载至"),
)

#: **预告片词表**——唯一不需要 LLM 的判断（确定性文本匹配）。
#:
#: 设计边界（这比词表本身重要）：
#:   命中 = 「这个控件通向的资源不是正片」。所以词表里的词都必须是
#:   **修饰播放资源种类**的词，而不是碰巧含「预告」二字的词。
#:   已知的误杀风险与处理见测试 ``test_预告片_误杀边界``。
TRAILER_KEYWORDS: tuple[str, ...] = (
    # 中文
    "预告片", "预告", "预告剧场", "抢先看", "先导片", "先导预告",
    "预告视频", "影视预告", "花絮", "片花", "片段", "试看片段",
    # 英文
    "trailer", "teaser", "preview", "sneak peek",
    "official trailer", "official teaser", "trailer only",
)

#: 付费墙词表（2026-10-10 口径裁定）。
#:
#: **业务规则，模型不该猜**：页面明确要求订阅/付费才能播放时，
#: 这条结论是「**有播放页但看不了**」——它**不是成功**（用户看不到正片），
#: 也不是「页面上没有播放控件」（控件是有的，卡在付费）。
#: 两种混在一起记，训练信号就失真了。
#:
#: 与 :data:`TRAILER_WORDS` 同款定位：**确定性事实归代码**，
#: 模型只在选中某个 ref 时用它复核（口径一「确定性事实优先」）。
#:
#: ⚠️ **「免费观看」「在线观看」不在表里**：它们出现在**导航栏**里的
#: 频率远高于出现在付费提示里（实测百度页正文就有），把它们当付费信号
#: 会把大批正常站点判成付费墙。
PAYWALL_WORDS: tuple[str, ...] = (
    # 中文
    "开通会员", "开通vip", "开通VIP", "购买会员", "会员专享",
    "付费观看", "付费解锁", "订阅后观看", "订阅即可", "续费",
    "充值", "购买单集", "单集购买", "立即购买", "开通连续包月",
    # 英文
    "subscribe to watch", "subscription required", "purchase to watch",
    "rent for", "buy this episode", "premium required", "sign in to watch",
    "members only", "vip required", "unlock with",
)


#: 「预告片」的反义旁证——出现这些词时，即使文本含「预告」也多半是正片
#: （如「预告 · 第 1 季正片」）。命中则**不**判 trailer_only。
TRAILER_DISQUALIFIERS: tuple[str, ...] = (
    "正片", "全集", "免费观看", "在线观看", "立即播放", "完结", "更新至",
)

#: 剥掉预告片词之后，**仍然算「整体就是预告片」**的结构性残留。
#: 「第 1 集 预告片」是「第 1 集的预告片」，判 suspect 是过度保守——
#: 站内这种按钮极常见，误判成 suspect 会把大批明显的预告控件推给人工。
#: 只收纯结构性字符：编号、剧集量词、连接符、标点。
_STRUCTURAL_RE = re.compile(
    r"^[\d\s第集话期章回上下中全本季部卷·|\-—_/,：:.。()（）\[\]【】]*$"
)


def _norm(label: str) -> str:
    return " ".join(label.split()).lower()


def classify_paywall(text: str) -> str:
    """正文里有没有付费墙信号。返回命中的词（没有则空串）。

    **判据是「页面明确要求付费」，不是「页面提到会员」**——所以只收
    祈使性/条件性的短语（开通会员、订阅后观看），不收「VIP」「会员」这类
    光杆名词：它们在正常播放页的导航与角标里也到处都是。

    :func:`PAYWALL_WORDS` 的每一条都写成**完整短语**而不是关键词，
    理由同上——「续费」「充值」单独出现可能是侧栏广告。
    """
    norm = _norm(text)
    for word in PAYWALL_WORDS:
        if _norm(word) in norm:
            return word
    return ""


#: 2026-10-12 增量十五 · 代码层 fallback：从 LLM 给的 evidence 文本反推
#: ``paywall_type``。LLM 即便看到付费墙也不是每次都给细分字段（实测
#: P1.5: B 站"大会员抢先看" —— LLM 给 answer=false 但 paywall_type=null），
#: 此时仅靠 ``PAYWALL_WORDS`` 命中会让 ``subscription_wall`` 大筐塞太多
#: 真 ``partial_free`` 样本。fallback 让形态更接近真实分布。
#:
#: **关键词必须是「试看/前几」类结构性短语，不是「大会员」类平台黑话**：
#: 后者扩词会污染 ``PAYWALL_WORDS`` 主表。fallback 的定位是「文章已落到
#: 付费词、且语义给得出 preview/trial」。
#:
#: 推断器是 best-effort：返 None 时调用方走原 fallback（``subscription_wall``）。
#: **不要**从 evidence 反向推广——证据无关键词就放弃细分。
PAYWALL_TYPE_HINTS: tuple[tuple[str, tuple[str, ...]], ...] = (
    # preview: 可看一段（试看 N 分钟 / 前几集免费 / 限时免费 / 抢先看之一部分）
    (
        "preview", (
            "试看", "首集免费", "限时免费", "免费试看",
            "前6分钟", "前 6 分钟", "前几分钟", "前几集",
            "抢先看", "部分试看", "限免",
        ),
    ),
    # trial: 试用 / 体验期 / 新用户免
    (
        "trial", (
            "试用", "体验期", "新用户免费", "首月免费",
            "7天免费", "免费体验",
        ),
    ),
)


def infer_paywall_type_from_evidence(evidence: str) -> str:
    """从 LLM 写的 evidence 文本反推 ``paywall_type``。

    返回合法值之一（``preview``/``trial``）；空字符串 = 未命中，
    调用方按现状保留 ``subscription_wall``。
    """
    if not evidence:
        return ""
    norm = _norm(evidence)
    for value, keywords in PAYWALL_TYPE_HINTS:
        for kw in keywords:
            if _norm(kw) in norm:
                return value
    return ""


#: 2026-10-12 增量十五 · 代码层 fallback：从 evidence 反推
#: ``access_control_type``。LLM 看到登录墙 / 地区码 / 验证码 / Cloudflare 等
#: 不一定每次给细分字段（实测 P1: bilibili "大会员" LLM 给 access=null），
#: evidence 又写明这些字面；code 层兜底。
#:
#: **先于 keyword 顺序**：五个值的关键字相互之间没有歧义，但「登录」-「会员」
#: 的语义重叠让 LLM 在 B 站 page 上踌躇（详见增量十四 §5）。fallback 让
#: 「登录后」/「需登录」明确走 ``login``，避免把 paywall 形态的"大会员"
#: 误判为纯 login。剩余形态交回合并态。
ACCESS_HINTS: tuple[tuple[str, tuple[str, ...]], ...] = (
    (
        "login", (
            "登录后观看", "登录后可", "登录后免费", "请登录",
            "请先登录", "需登录", "登录解锁", "登录会员",
            "登录后继续", "登录才能", "未登录",
        ),
    ),
    (
        "region", (
            "地区不支持", "当前地区不支持", "本地区不支持",
            "地区限制", "您所在地区", "版权限制",
            "70017001", "地区无权",
            # 2026-10-12 增量二十一：腾讯视频地区码场景下 LLM 经常把 70017001
            # 改写成「当前设备不支持播放」之类页面文案；fallback 抽出后
            # 仅靠 70017001 数字会漏判，补两条文案短语。
            "当前设备不支持播放", "设备不支持播放",
        ),
    ),
    (
        "anti_bot", (
            "performing security verification", "just a moment...",
            "checking your browser", "cf-chl", "robot check",
            "安全验证", "人机验证", "robot challenge",
        ),
    ),
    (
        "captcha", (
            "请输入验证码", "请完成验证", "滑动验证", "图形验证",
            "短信验证", "captcha",
        ),
    ),
    (
        "not_found", (
            # **不带"X 不存在"这类短语**：LLM evidence 是「结论+证据」混合，
            # 「视频不存在/内容不存在」经常是模型自述结论而非页面文案
            # （实测 P3 重跑 v.qq.com/70017001 因此被误判 broken）。
            # 留着"已被删除 / 已下架 / 404 错误"——这些更接近页面真实文案。
            "页面不存在", "已下架", "已被删除",
            "该视频已被删除", "404 错误", "404错误", "对不起",
            "找不到该视频",
        ),
    ),
)


def infer_access_from_evidence(evidence: str) -> str:
    """从 evidence 文本反推 ``access_control_type``。

    返合法值或空串；调用方按空串落合并态。这是有意 fail-soft——空串
    绝不强行给一个 ``null``/``unknown``；反推不成立就承认不知道。

    ⚠️ **故意不做模糊匹配**：关键词是「完整短语」而不是「子串」——
    「登录」单独出现做 ``login`` 推断会让 B 站"登录账号"普通按钮页
    被误判。该函数行为比 :func:`classify_paywall` 更激进——
    宁可不出也不猜错。
    """
    if not evidence:
        return ""
    norm = _norm(evidence)
    for value, keywords in ACCESS_HINTS:
        for kw in keywords:
            if _norm(kw) in norm:
                return value
    return ""


def classify_trailer(label: str) -> str:
    """把控件文本分成三类：``"only"`` / ``"suspect"`` / ``"none"``。

    这条规则来自业务侧（W1 前确认）：**播放按钮或剧集按钮上的文本是
    「预告片」时，说明播放资源不存在**——即这个站点能看预告但没有正片。

    规则：
      1. 归一化后必须**包含**某个预告片词（不是相等——「抢先看」、
         「第 1 集预告片」都算）
      2. 若同时出现正片信号则判 ``"none"``——「预告 · 第 1 季正片」是季预告，
         点进去仍是正片资源
      3. **文本整体就是一个预告片词**才判 ``"only"``；含词但还有别的内容
         判 ``"suspect"``

    第 3 条是精度与召回的取舍，写在这里是因为它是**已知的能力上限**：
    「观看 trailer 解析」这类**解说页链接**在词表层无法与真正的预告控件区分
    （两者都是「含 trailer 二字的短文本」）。不堆排除词表——那只会把
    下一种误判换成另一种；判成 ``suspect`` 交给人工兜底
    （失败分支表的 ``trailer_suspect``），W3 由 LLM 判定语义。
    """
    text = _norm(label)
    if not text:
        return "none"
    if any(_norm(word) in text for word in TRAILER_DISQUALIFIERS):
        return "none"
    hits = [w for w in TRAILER_KEYWORDS if _norm(w) in text]
    if not hits:
        return "none"
    if text in {_norm(w) for w in hits}:
        return "only"
    # 剥掉命中词后若只剩编号/量词/标点，仍算「整体就是预告片」
    residue = text
    for word in hits:
        residue = residue.replace(_norm(word), "")
    if _STRUCTURAL_RE.match(residue):
        return "only"
    return "suspect"


def is_trailer_only(label: str) -> bool:
    """控件文本是否**确定**指向预告片资源（``classify_trailer`` 的高置信档）。"""
    return classify_trailer(label) == "only"


def is_trailer_suspect(label: str) -> bool:
    """含预告片词但不足以断定——留给人工兜底，不自动判负。"""
    return classify_trailer(label) == "suspect"


def is_play_control(label: str) -> bool:
    """控件是否像播放入口。

    顺序有讲究：**先看否定信号，再看是不是预告片控件，最后才走词表**。
    预告片必须单独放行——「预告片」不含任何肯定信号词，但它**恰恰是**
    业务规则要处理的那种播放控件（用户原话：「播放按钮或者剧集按钮上的
    文本是'预告片'」）。漏掉这一步，预告片按钮会先被判成
    ``no_play_control``，``trailer_only`` 这条负分支永远不会触发。

    **公开**是因为 :class:`~trajectory_pipeline.perception.llm_perceptor.LLMPerceptor`
    复用这张表做候选筛选。两份词表不同步的后果是「W3 判 False、
    W1 判 True」——同一批素材两个结论，且两个都看着像对的。
    """
    text = _norm(label)
    if not text:
        return False
    if any(_norm(word) in text for word in PLAY_CONTROL_NEGATIVE):
        return False
    if classify_trailer(text) in ("only", "suspect"):
        return True
    if any(_norm(word) in text for word in PLAY_CONTROL_KEYWORDS):
        return True
    return any(p.search(text) for p in EPISODE_PATTERNS)


def play_control_candidates(obs: Observation) -> list[InteractiveElement]:
    """页面上的**非预告**播放控件候选，按观察原序。

    词表与顺序规则的唯一所有者在这里，W1 自己判 ③ 与 W3 兜底取 ref
    都调它——两份实现不同步的后果是「同一批素材两个结论，且都像对的」。

    **预告控件被排除在外**：预告片是「有播放控件但资源不对」，
    与「没有播放控件」是两条不同的负分支，混在一起会同时毁掉两条。
    需要保留预告控件时用 :func:`is_play_control` 自己筛。
    """
    return [e for e in obs.interactive_elements
            if is_play_control(e.label) and classify_trailer(e.label) == "none"]


def trailer_only_candidates(obs: Observation) -> list[InteractiveElement]:
    """**预告**播放控件——与 :func:`play_control_candidates` 互补，两者并起来
    就是 :func:`is_play_control` 筛出的全部候选。

    「有播放控件但全是预告片」与「压根没有播放控件」是**两条不同的负分支**
    （``trailer_only`` vs ``no_play_control``），所以判据必须同源：W1 在这里
    落 ``trailer_only``、W3 落 ``no_play_control``，同一批素材会出两个结论，
    且两个都看着像对的——这正是词表复制的代价。
    """
    return [e for e in obs.interactive_elements
            if is_play_control(e.label) and classify_trailer(e.label) != "none"]


class RulePerceptor:
    """确定性 Perceptor。W1 的唯一实现。"""

    name = "rule"

    def __init__(self) -> None:
        self._unresolved: set[str] = set()

    # ── 主入口 ──────────────────────────────────────────────────────

    def decide(self, question: str, obs: Observation) -> Decision:
        """判定 ``question``。

        未知题 id 直接抛 ``KeyError``（注册表的既定行为）。
        ``obs`` 为空观察时**不猜**，返回 ``answer=None``。
        """
        started = time.monotonic()
        spec = questions.get(question)
        handler = {
            Q.FIND_PLAY_CONTROL: self._find_play_control,
            Q.PLAYER_OK: self._player_ok,
        }.get(question)

        if handler is None:
            # W1 不具备该题的语义能力 —— fail-closed，绝不猜（I4）
            self._unresolved.add(question)
            return self._answer(
                spec, None, evidence="W1 规则版不具备该题的语义判断能力",
                started=started, fallback=True,
            )
        return handler(spec, obs, started)

    # ── health ──────────────────────────────────────────────────────

    def health(self) -> tuple[bool, str]:
        """自述能力边界。``--readiness`` 类命令据此提示「正样本要等 W3」。"""
        return True, "确定性实现：可判 trailer_only 与播放组件存在性；其余题 fail-closed"

    @property
    def unresolved_questions(self) -> frozenset[str]:
        """本进程内被判定为「不具备能力」的题——用于覆盖率报表。"""
        return frozenset(self._unresolved)

    # ── 各题实现 ────────────────────────────────────────────────────

    def _find_play_control(
        self, spec: Any, obs: Observation, started: float
    ) -> Decision:
        """判断点 ③：找播放控件 + 判是否仅预告。

        判定顺序有讲究：**先全量筛出候选，再统一判 trailer**。
        反过来（找到第一个就返回）会把「预告片」当成结论，
        即使页面上同时存在「正片」按钮——那会误杀正样本。

        采集降级时（``links`` / ``interactive`` 拿不到）**返回 None 而非 False**：
        「没采到交互元素」和「页面上确实没有交互元素」在观察里长得一样，
        判成 False 就把基础设施故障写进了业务结论。
        """
        if obs.degraded:
            return self._answer(
                spec, None,
                evidence=f"采集降级（{list(obs.degraded)}），交互元素可能不完整，不能判无播放控件",
                started=started, fallback=True,
            )
        if not obs.interactive_elements:
            # obscura 对「页面上没有交互元素」返回的是**哨兵文本**
            # （``No interactive elements on this page.``）而不是错误，
            # 所以 ``degraded`` 抓不到它。但实测三站（iqiyi / ixigua / sohu）
            # 都走到这里：正文 0 字符、元素 0 个——那是 JS 页面还没渲染完，
            # 不是「这页真的一个控件都没有」。
            # 视频站的按钮几乎必然是 JS 渲染的，「0 个元素」当「没有播放控件」
            # 判 False，等于把采集失败写成业务负样本。
            return self._answer(
                spec, None,
                evidence=(
                    f"采到 0 个交互元素（正文 {len(obs.body_text)} 字符）；"
                    f"哨兵返回无法区分「页面没渲染完」与「页面确实没控件」，不判负"
                ),
                started=started, fallback=True,
            )
        candidates = [e for e in obs.interactive_elements if is_play_control(e.label)]
        if not candidates:
            if obs.video_tag_count >= 1:
                # 观察**自己**否定了「没有播放控件」这句话：页面上已经有 <video>，
                # 说明人已经站在播放页上，只是按钮文案没进词表。实测
                # tv.sohu.com（title=「功夫 - 搜狐视频」、video=1）就是这样被写成
                # no_play_control 的——一个**能看**的站点进了负样本池。
                # 负样本池的全部价值就是「这里真的看不了」，混进「能看但没认出来」
                # 会直接毁掉它。
                return self._answer(
                    spec, None,
                    evidence=(
                        f"{len(obs.interactive_elements)} 个交互元素无一命中词表，"
                        f"但页面已有 <video>×{obs.video_tag_count}——"
                        f"「没有播放控件」与观察自相矛盾，不判负"
                    ),
                    started=started, fallback=True,
                )
            return self._answer(
                spec, False,
                evidence=f"{len(obs.interactive_elements)} 个交互元素中无一命中播放控件词表",
                started=started,
            )
        real = play_control_candidates(obs)
        if real:
            suspects = [e for e in candidates if classify_trailer(e.label) == "suspect"]
            return self._answer(
                spec, True,
                evidence=f"命中播放控件 ref={real[0].ref} label={real[0].label!r}",
                started=started,
                payload={"ref": real[0].ref, "trailer_only": False,
                         "trailer_suspect": [e.label for e in suspects],
                         "why": f"命中 {len(real)} 个非预告控件"},
            )
        # 有候选但全是预告片 —— 这是业务规则里的「播放资源不存在」
        suspects = [e.label for e in candidates
                    if classify_trailer(e.label) == "suspect"]
        return self._answer(
            spec, True,
            evidence=(
                f"{len(candidates)} 个候选控件全为预告："
                f"{[e.label for e in candidates][:3]}"
            ),
            started=started,
            payload={"ref": "", "trailer_only": not suspects,
                     "trailer_suspect": suspects,
                     "why": "候选控件文本均命中预告片词表"},
        )

    def _player_ok(self, spec: Any, obs: Observation, started: float) -> Decision:
        """判断点 ④：播放页是否有媒体组件。

        **只认 ``<video>`` / ``<audio>``，iframe 数量不作为判据。**

        实测把 iframe 当判据会直接产出假阳性：hao123（纯导航站）首页有
        **15** 个 iframe，于是被判「播放页 OK」——而它连作品都不对应。
        iframe 可以是广告、埋点、地图、天气，**数量与「是不是播放器」无关**。
        要真判 iframe 里是不是播放器得看 src 与尺寸，那是启发式阈值，
        拿不准的东西不该当判据。

        存在标签时**只判正向**：存在性 ≠ 能正常播放，所以带 ``fallback=True``。
        没有标签时返回 ``answer=None`` 而非 False——JS / canvas / iframe 播放器
        都不产生这些标签，「没测到」≠「没有」。优酷的播放页正是这种形态
        （video=0 iframe=1），W1 判不出来就该说判不出来：少一条正样本，
        比多一条假阳性划算（假阳性会污染成功率的基线）。
        """
        media = obs.video_tag_count
        frames = obs.iframe_count
        if media >= 1:
            return self._answer(
                spec, True,
                evidence=f"存在 <video>/<audio> 标签 ×{media}（iframe={frames}）",
                started=started,
                payload={"media_count": media, "iframe_count": frames},
                # 存在性 ≠ 能正常播放。标出来，别让下游误以为这是语义判定
                fallback=True,
            )
        return self._answer(
            spec, None,
            evidence=(
                f"未见 <video>/<audio>（iframe={frames}，不作判据：导航站满屏 iframe，"
                f"实测 hao123 因 15 个 iframe 被误判为播放页）；"
                f"JS/canvas/iframe 播放器不能靠标签判定"
            ),
            started=started,
            payload={"media_count": media, "iframe_count": frames},
            fallback=True,
        )

    # ── 构造 ────────────────────────────────────────────────────────

    # ── L3 业务信号独立判定(2026-10-13 增量二十六)───────────────────

    def decide_business_signal(self, obs: Observation) -> Decision:
        """L3 业务信号独立判定——body_text 关键词命中产 7 类 access_kind。

        与 :meth:`decide` 的区别:不走 Question 注册表,
        产出 ``question="business.signal"``(不在 REGISTRY)用于 L5 仲裁层
        对账。``answer=True`` 表示已判出 access_kind(非 unresolved),
        实际分类在 ``payload["access_kind"]``。

        决策顺序(强拒访在前):
            region > anti_bot > captcha > not_found > login > paywall > unknown

        fail-closed 语义:
            - ``obs.body_text`` 为空 → access_kind="unknown" + confidence=L3_CONF_WEAK
            - 无任何关键词命中 → access_kind="unknown" + confidence=L3_CONF_WEAK
            - 关键词命中 → access_kind=对应类 + confidence=对应档位

        I1 evidence 写命中短语; I3 幂等(同一 obs 重复调用返相同 Decision);
        I5 无副作用(纯文本推理,不动 obs)。
        """
        started = time.monotonic()
        text = obs.body_text or ""

        if not text:
            return self._l3_answer(
                "unknown", evidence="body_text 为空,L3 无法判定",
                confidence=L3_CONF_WEAK, started=started,
                payload={"access_kind": "unknown", "hit_keyword": "",
                         "paywall_type": "none"},
            )

        for kind in L3_ACCESS_KIND_PRIORITY:
            hints = L3_ACCESS_HINTS.get(kind, ())
            hit = _l3_find_first_hit(text, hints)
            if hit is not None:
                confidence = _l3_access_kind_confidence(kind)
                payload: dict[str, Any] = {
                    "access_kind": kind,
                    "hit_keyword": hit,
                    "paywall_type": "none",
                }
                return self._l3_answer(
                    kind, evidence=f"L3 命中 {kind}: {hit!r}",
                    confidence=confidence, started=started, payload=payload,
                )

        return self._l3_answer(
            "unknown", evidence="L3 无业务信号命中",
            confidence=L3_CONF_WEAK, started=started,
            payload={"access_kind": "unknown", "hit_keyword": "",
                     "paywall_type": "none"},
        )

    def decide_paywall_type(self, obs: Observation) -> Decision:
        """L3 paywall_type 细分判定——仅当 ``access_kind="paywall"`` 时有意义。

        复用 :func:`infer_paywall_type_from_evidence`(增量十五已落地,
        P1.5 实测稳定区分 preview/trial)。无关键词时返 unknown。

        payload:
            - ``paywall_type``: "none" | "preview" | "trial" | "unknown"
            - ``hit_keyword``: 命中短语(unknown 时为空串)
            - ``access_kind``: "paywall"(固定,标识此决策由 L3 二次细分产出)
        """
        started = time.monotonic()
        text = obs.body_text or ""

        if not text:
            return self._l3_answer(
                "unknown", evidence="body_text 为空,L3 无法判定 paywall_type",
                confidence=L3_CONF_WEAK, started=started,
                payload={"paywall_type": "unknown", "hit_keyword": "",
                         "access_kind": "paywall"},
                question="paywall.type",
            )

        ptype = infer_paywall_type_from_evidence(text) or "unknown"
        if ptype == "unknown":
            return self._l3_answer(
                "unknown", evidence="L3 paywall_type 无关键词命中",
                confidence=L3_CONF_WEAK, started=started,
                payload={"paywall_type": "unknown", "hit_keyword": "",
                         "access_kind": "paywall"},
                question="paywall.type",
            )

        confidence = L3_CONF_STRONG if ptype in ("preview", "trial") else L3_CONF_MEDIUM
        hints_map: dict[str, tuple[str, ...]] = dict(PAYWALL_TYPE_HINTS)
        hit = _l3_find_first_hit(text, hints_map.get(ptype, ())) or ptype
        return self._l3_answer(
            ptype, evidence=f"L3 paywall_type 命中: {hit!r}",
            confidence=confidence, started=started,
            payload={"paywall_type": ptype, "hit_keyword": hit,
                     "access_kind": "paywall"},
            question="paywall.type",
        )

    def _l3_answer(
        self,
        answer: str,
        *,
        evidence: str,
        confidence: float,
        started: float,
        payload: Mapping[str, Any] | None = None,
        question: str = "business.signal",
    ) -> Decision:
        """L3 Decision 构造器——不走 Question 注册表,产 L5 仲裁对账用的 Decision。"""
        return Decision(
            question=question,
            answer=True,                # L3 总是返回 True 表示已判出(非 unresolved)
            confidence=confidence,
            evidence=evidence,
            source="rule",
            payload=MappingProxyType(payload) if payload else MappingProxyType({}),
            latency_ms=int((time.monotonic() - started) * 1000),
            fallback_used=False,
        )

    def _answer(
        self,
        spec: Any,
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
            confidence=1.0,        # 规则判定是确定性的，无不确定性可言
            evidence=evidence,     # I1：evidence 必须指向具体判据而非「我认为」
            source="rule",
            payload=MappingProxyType(payload) if payload else MappingProxyType({}),
            latency_ms=int((time.monotonic() - started) * 1000),
            fallback_used=fallback,
        )
# --- L3 模块级常量 + helpers（增量二十六 · 阶段 4 L3 规则层真接入）---
# 设计纪律：L3 独立判定（不复用 L4 反推），词典独立但与 L4 同源对齐（不同 dict，同义词表）。
# 注入点：L4 之后、L5 之前；触发条件与 L4 同 fail-closed 跳过。
L3_ACCESS_KINDS: tuple[str, ...] = (
    "region", "anti_bot", "captcha", "not_found", "login", "paywall", "unknown",
)
L3_PAYWALL_TYPES: tuple[str, ...] = ("none", "preview", "trial", "unknown")
# 决策优先级：强拒访在前，paywall 兜底末位（unknown 作为兜兜底）
L3_ACCESS_KIND_PRIORITY: tuple[str, ...] = (
    "region", "anti_bot", "captcha", "not_found", "login", "paywall", "unknown",
)
# 置信度档位（与 I3 幂等：同一 obs 重复调用产出相同 confidence）
L3_CONF_STRONG: float = 0.85
L3_CONF_MEDIUM: float = 0.65
L3_CONF_WEAK: float = 0.30

# access_kind 关键词词典（与 L4 _BLOCKED_HINTS/_LOGIN_HINTS/_PAYWALL_HINTS 同源不同 dict；
# L3 走 body_text 全文，L4 走 page_quotes+body+snapshot combined_text）
L3_ACCESS_HINTS: MappingProxyType = MappingProxyType({
    "region": (
        "所在地区不支持", "所在地区暂不支持",
        "该地区不支持", "该地区暂不支持",
        "您所在地区", "您所在国家", "您所在的地区",
        "当前地区不支持", "当前地区暂不支持",
        "当前地区无法", "当前国家不支持",
        "该视频仅限", "仅限中国大陆",
        "暂不支持您所在", "不支持您所在的",
        "地区限制", "区域限制", "地理限制",
        "版权限制", "授权区域",
        "未在您的地区", "未取得该地区",
        "未获授权", "暂无版权",
        "7001-7001", "70017001",
        "this video is not available in your region/country",
        "not available in your country", "not available in your region",
        "geo-restricted", "geo blocked", "copyright restriction",
    ),
    "anti_bot": (
        "performing security verification", "just a moment...", "checking your browser",
        "verify you are human", "verifying you are human",
        "please wait while we verify",
        "cf-chl", "cf_chl", "cf-challenge", "cloudflare-challenge",
        "robot check", "robot verification", "automated traffic",
        "security check", "安全检查", "安全验证",
        "访问验证", "人机验证",
        "DDOS protection", "rate limit exceeded", "too many requests",
        "please enable javascript", "please enable cookies",
    ),
    "captcha": (
        "输入验证码", "请输入验证码",
        "请完成验证", "完成验证后",
        "图形验证码", "图片验证码",
        "短信验证码", "邮箱验证码",
        "滑动验证", "滑块验证",
        "拼图验证", "点选验证", "拖动验证",
        "captcha", "recaptcha", "hcaptcha", "turnstile",
        "verify code", "verification code",
    ),
    "not_found": (
        "页面不存在", "页面已删除",
        "视频不存在", "视频已删除",
        "视频不存在或已被删除",
        "视频已下架", "视频已过期", "视频已失效",
        "找不到该视频", "找不到您访问的页面",
        "找不到您访问的视频",
        "该视频不存在", "该视频已删除",
        "该视频已被删除", "该视频已下架",
        "该视频已过期", "该视频已失效",
        "视频不存在或",
        "抱歉，您访问的页面不存在",
        "抱歉，视频已",
        "404", "404 not found", "404 错误", "404错误", "404 页面",
    ),
    "login": (
        "请先登录", "请登录后", "登录后观看",
        "登录后查看", "登录后可",
        "登录观看", "登录查看", "登录后才能",
        "需登录",
        "未登录", "您还未登录", "您尚未登录",
        "请登录账号", "请登录账户",
        "请先登录账号", "请先登录账户",
        "登录账号后", "登录账户后",
        "扫码登录", "扫一扫登录",
        "sign in to watch", "sign in to view", "please sign in",
        "login required", "please log in",
    ),
    "paywall": PAYWALL_WORDS,
    "unknown": (),
})


def _l3_find_first_hit(text: str, hints) -> str | None:
    """L3 专用：从 text 中找到 hints 里第一个出现的关键词。

    与 L4 _find_first_hit 同源但独立；L3 只看 body_text（不走 snapshot）。
    返回原始关键词串（不是位置、不是裁切后片段）以便 evidence 与「命中」事实一致；len(hits)==0 时返回 None。
    """
    if not text or not hints:
        return None
    for hint in hints:
        if hint and hint in text:
            return hint
    return None


def _l3_access_kind_confidence(kind: str) -> float:
    """L3 专用：按 access_kind 返回置信度档位。

    - region / anti_bot / captcha / not_found → STRONG（文案代表明确拒服务）
    - login → MEDIUM（需要结合重定向）
    - paywall → STRONG（付费关键词较明确）
    - unknown → WEAK（兜底）
    """
    if kind in ("region", "anti_bot", "captcha", "not_found", "paywall"):
        return L3_CONF_STRONG
    if kind == "login":
        return L3_CONF_MEDIUM
    return L3_CONF_WEAK


# I3 幂等：决策函数同一 obs 调用两次返回同一 Decision（不依赖随机状态）。
