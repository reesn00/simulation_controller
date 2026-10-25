"""W1 规则版 Perceptor——**故意做得很弱**。

「弱」不是偷懒，是三层分工铁律的直接后果：代码层做护栏，那么
规则版**只配做确定性事实判定**，语义判断一律 ``answer=None``（I4）。
让规则版去「猜」语义，得到的不是弱信号而是**假信号**——它会污染负样本池，
让「这一站没播放控件」这个结论看起来有代码背书，实际是关键词匹配的幻觉。

W1 只做两件事，且都必须是**确定性的**：

1. **``trailer_only``**——纯文本匹配，不需要理解页面（用户业务规则：
   播放控件文本是「预告片」即说明播放资源不存在）。
2. **``PLAYER_OK`` 判不出来就返回 None**——**存在 ``<video>`` / ``<audio>``
   标签也不判 True**（2026-10-23 · M-26）：媒体计数是**必要条件**，
   不是充分条件——优酷「试看6分钟｜开通会员看完整视频」页 ``video=1``、
   网易号美食图文页 ``video=6``（点广告后注入），都曾被读成「能看正片」。
   ⇒ 规则版**没有 LLM 可让**，``False`` 更糟（会把「能看但判不出」写成
   **真负样本**进池），唯一不撒谎的取值是 ``None``（fail-closed）。
   它落 ``player_unverified``：**不进负样本池**、在人工复核队列里
   （这正是 W1 设计的正样本路径）。
   （iframe 数量**不作**判据：实测导航站 hao123 满屏 15 个 iframe，
   拿它当判据会把导航站判成播放页。）

其余两题（``SELECT_PLAY_SITES`` / ``IS_REACHABLE``）W1 全程 ``answer=None``；
④（``PLAYER_OK``）自 2026-10-23 · M-26 起**也不再产出 True**（见上）。
这意味着 W1 批次**不会有正样本**——它是执行层的连通性验证批次，
素材价值主要在负样本池。正样本要等 W3 的 LLMPerceptor，
或走人工复核队列（W1 的正样本来源，见 ``executor/review_queue.py``）。
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass
from types import MappingProxyType
from typing import Mapping, Any

from trajectory_pipeline.common.elements import (
    ELEMENT_VOCAB_ARIA_ROLE,
    ELEMENT_VOCAB_HTML_TAG,
)
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

#: 付费墙词表——**分两层**（2026-10-21 · 台账 H-16 修复①）。
#:
#: **业务规则，模型不该猜**：页面明确要求订阅/付费才能播放时，
#: 这条结论是「**有播放页但看不了**」——它**不是成功**（用户看不到正片），
#: 也不是「页面上没有播放控件」（控件是有的，卡在付费）。
#: 两种混在一起记，训练信号就失真了。
#:
#: 与 :data:`TRAILER_KEYWORDS` 同款定位：**确定性事实归代码层**，
#: 模型只在选中某个 ref 时用它复核（口径一「确定性事实优先」）。
#:
#: ⚠️ **「免费观看」「在线观看」不在表里**：它们出现在**导航栏**里的
#: 频率远高于出现在付费提示里（实测百度页正文就有），把它们当付费信号
#: 会把大批正常站点判成付费墙。
#:
#: ⚠️⚠️ **为什么分成两层**（2026-10-21，H-16 修复①）
#: ------------------------------------------------
#: 原来这里是一张**扁平**表，把两种性质完全不同的短语混在一起：
#:
#:   - **门禁命题**：「订阅后观看」「以下内容需下载」——页面明说
#:     「不给钱 / 不订阅 / 不换端就看不了正片」，短语本身就是完整命题；
#:   - **号召 CTA**：「开通会员」「续费」「打开app观看」——它们在
#:     **站点导航栏、角标、营销模块**里高频出现，单独出现分不清
#:     「真付费墙」与「入口页广告」。
#:
#: 扁平表当判据用的后果（2026-10-21 人工复核：池里 12 条
#: ``subscription_wall`` **没有一条站得住**）：v.qq.com 8 条的真因是
#: **地区限制**（70017001.1），「开通会员」只是句尾附加；youku 1 条是
#: **续费营销**；ixigua 1 条是「打开app观看」——**全是 CTA 裸命中**。
#:
#: 而 :mod:`trajectory_pipeline.executor.gatekeeper` **早就知道**这张表
#: 不能当判据：它的 ``is_paywall`` 刻意绕开 ``classify_paywall``，
#: 注释明写「PAYWALL_WORDS 第一条就是「开通会员」——它在站点导航栏广告
#: （nav-bar CTA）里高频出现，单凭它分不清真付费墙与入口页广告」。
#: 同一个仓库里两套口径，是这类错判能活这么久的原因。
#:
#: 所以现在分两层，判定走 :func:`paywall_evidence`：
#:   L1 :data:`PAYWALL_GATE_PHRASES` —— 门禁短语，命中即成立；
#:   L2 :data:`PAYWALL_CTA_PHRASES`  —— CTA 短语，必须**配同句观看语境**
#:      （:data:`PAYWALL_CONTEXT_WORDS`）才成立。
PAYWALL_GATE_PHRASES: tuple[str, ...] = (
    # 中文 · 门禁命题
    "付费观看", "付费解锁", "订阅后观看", "订阅后可看", "订阅即可观看",
    "购买单集", "单集购买", "按集购买",
    "开通会员观看", "开通vip观看", "开通VIP观看",
    # 2026-10-15 INC43 · APP/客户端硬墙：页面明示「需下载 App/客户端
    # 才能看正片」。实测三站命中而原词表全漏（金标 subscription_wall
    # 被判 no_play_control / login_wall_or_blocked）：
    #   - m.ixigua ×2：「以下内容需下载西瓜视频查看」
    #   - play.tudou：「下载优酷PC客户端继续观看」
    # 短语刻意收窄：「以下内容需下载」「客户端继续观看」都是**门禁语境**，
    # 站点页脚的「下载中心」「下载客户端」（实测 mgtv / luketv 均有）
    # 不构成子串命中，不会误伤。
    "以下内容需下载", "客户端继续观看", "下载客户端后观看",
    # 英文 · 门禁命题
    "subscribe to watch", "subscription required", "purchase to watch",
    "rent for", "buy this episode", "premium required",
    "members only", "vip required", "unlock with",
)

#: **条件式门禁短语**（2026-10-25 · 方案 19 A 类补漏）——播放器报错形态，
#: 单看是「错误」而非「付费」，必须与 :data:`PAYWALL_ERROR_CONTEXT_WORDS`
#: 里的会员电商信号**同页同现**才算付费墙门禁。
#:
#: 背景：bilibili 会员影片未登录/未开通时，播放器区显示「发生了错误，
#: 请稍后再试」+ 页面导航带「会员购」，正片标签标「会员」——这是
#: **付费墙**形态（不是 broken，也不是「没控件」）。但「发生了错误」是
#: 极泛的错误提示，服务器 5xx / 404 / 网络错误都会出现，单看它会把
#: broken 页误判成订阅墙。
#:
#: 所以这批短语**不能进 :data:`PAYWALL_GATE_PHRASES`**（那是无条件命中），
#: 必须单独一张表，配会员电商信号同现才成立——见
#: :func:`_find_paywall_hit` 的第三条判据。
#:
#: ⚠️ 会员电商信号刻意只收**会员专享入口**（「会员购」「大会员购」「会员专区」），
#: 不收泛化的「会员」二字——「会员」在普通影视详情页的标签/角标里也高频，
#: 收进来会让「发生了错误」+「会员」标签的普通页面误判成付费墙。
PAYWALL_ERROR_GATE_PHRASES: tuple[str, ...] = (
    "发生了错误", "请稍后再试", "播放出错", "加载失败", "视频加载失败",
    "播放器错误", "视频播放失败",
)

#: 条件式门禁的**会员电商信号**（必须与 :data:`PAYWALL_ERROR_GATE_PHRASES`
#: 同页同现）。只收会员专享入口，不收泛化「会员」二字。
PAYWALL_ERROR_CONTEXT_WORDS: tuple[str, ...] = (
    "会员购", "大会员购", "会员专区", "会员专享区",
)

#: L2 · **号召 CTA** 短语——必须配同句观看语境才成立（见模块头那段说明）。
#:
#: ⚠️ **「sign in to watch」不在这里**（2026-10-21）：它是**登录门**
#: 不是付费门——「登录了也要付钱」与「登录后免费」是两条不同的分支
#: （``login_required`` vs ``subscription_wall``），把它算进付费墙
#: 会把可绕过的登录墙写成绕不过去的订阅墙。它已并入
#: :data:`ACCESS_HINTS` 的 ``login``。
PAYWALL_CTA_PHRASES: tuple[str, ...] = (
    # 中文
    "开通会员", "开通vip", "开通VIP", "购买会员", "会员专享",
    "订阅即可", "续费", "充值", "立即购买", "开通连续包月",
    "打开app观看", "打开APP观看",
)

#: **观看语境词**——L2 的 CTA 短语必须与之一**同句**，且该语境词**不在**
#: CTA 短语自己的跨度内（否则「打开app观看」自己给自己作证）。
#:
#: 刻意**不含**「播放」「观看」两个高频词：站点导航栏的「立即播放」
#: 「在线观看」「免费观看」到处都是，与「开通会员」同句就足以让裸 CTA
#: 复活成付费墙（v.qq.com 那 8 条的正文里就有「当前设备不支持播放」）。
#: 收进来的都是**门禁特有**的形态：试看 / 解锁 / 正片 / 全集 / 完整版。
PAYWALL_CONTEXT_WORDS: tuple[str, ...] = (
    "试看", "解锁", "正片", "全集", "完整版", "完整内容",
    "观看完整", "看完", "本片",
)

#: **词表全集**（L1 门禁 + L2 CTA）——给 L3 粗分类层用（它要的是
#: 「页面里有没有付费相关字样」，不是「有没有门禁」）。
#:
#: ⚠️ **这张表不是判据**：裸命中分不出真付费墙与导航栏广告。
#: 贴分支一律走 :func:`paywall_evidence`。
PAYWALL_WORDS: tuple[str, ...] = PAYWALL_GATE_PHRASES + PAYWALL_CTA_PHRASES


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
    """正文里有没有**付费墙门禁证据**。返回命中的短语（没有则空串）。

    ⚠️ **口径 2026-10-21 改过**（台账 H-16 修复①）：原来这里是
    「扁平词表子串命中」，「开通会员」单独出现就算——那是**导航栏广告**
    与**真付费墙**分不清的根源（实测 12 条 ``subscription_wall`` 没有
    一条站得住）。现在走 :func:`paywall_evidence` 的**门禁语境**判据：
    「订阅后观看」成立，「开通会员」独行**不成立**。

    返回的仍是**短语**（不是描述），老调用方（``cli probe`` 打印、
    ④ 的 ``paywall_word``）的用法不变。
    """
    hit = _find_paywall_hit(text)
    return hit.phrase if hit else ""


@dataclass(frozen=True, slots=True)
class _PaywallHit:
    """一次付费墙证据命中：命中哪个短语、哪一层、有没有语境词。"""

    phrase: str
    kind: str          # "gate"（L1 门禁短语）| "cta"（L2 CTA + 语境）
    context: str = ""  # 语境词（仅 kind == "cta" 时非空）


#: 付费墙语境判定的**切句单位**：换行 + 句读（。！？；）。
#: 刻意**不按「，」切**——实测真阳性 sohu「您可以试看5分钟，点击开通会员」
#: 里「试看」与「开通会员」被一个逗号隔开，按逗号切就看不到「同句」了。
_PAYWALL_UNIT_SPLIT = re.compile(r"[\n\r。！？；;!?]+")


def _paywall_units(text: str) -> list[tuple[int, int, str]]:
    """把正文切成语境判定用的**句单元**，返回 ``(起, 止, 单元文本)``。"""
    units: list[tuple[int, int, str]] = []
    pos = 0
    for m in _PAYWALL_UNIT_SPLIT.finditer(text):
        if m.start() > pos:
            units.append((pos, m.start(), text[pos:m.start()]))
        pos = m.end()
    if pos < len(text):
        units.append((pos, len(text), text[pos:]))
    return units


def _context_outside(unit_low: str, cta_start: int, cta_end: int) -> str:
    """句单元里 CTA 短语**跨度之外**的观看语境词（没有则空串）。

    跨度之内不算：「打开app观看」这个 CTA 短语自己就含「观看」，
    让它给自己作证等于没有判据。
    """
    for word in PAYWALL_CONTEXT_WORDS:
        wlow = word.lower()
        pos = unit_low.find(wlow)
        while pos >= 0:
            if pos + len(wlow) <= cta_start or pos >= cta_end:
                return word
            pos = unit_low.find(wlow, pos + 1)
    return ""


def _find_paywall_hit(text: str) -> _PaywallHit | None:
    """付费墙**门禁证据**的判定实现（唯一真源）。没有则 ``None``。

    判据（任一即可）：

    1. **L1 门禁短语**命中（:data:`PAYWALL_GATE_PHRASES`）——短语本身
       就是完整命题（「订阅后观看」= 不订阅不给看），不需要再看上下文；
    2. **L2 CTA 短语**命中（:data:`PAYWALL_CTA_PHRASES`）**且同一句**
       里有**观看语境词**（:data:`PAYWALL_CONTEXT_WORDS`）——
       「开通会员」独行是导航栏广告，「开通会员 观看完整版」是门禁；
    3. **L1.5 条件式门禁**（2026-10-25 方案 19 A 类补漏）：播放器报错
       短语（:data:`PAYWALL_ERROR_GATE_PHRASES`）命中，**且页面**里
       有会员电商信号（:data:`PAYWALL_ERROR_CONTEXT_WORDS`）——
       bilibili「发生了错误」+「会员购」= 付费墙；「发生了错误」独行
       = broken/unreachable，不是付费墙。
    """
    if not text:
        return None
    low = text.lower()
    for phrase in PAYWALL_GATE_PHRASES:
        if phrase.lower() in low:
            return _PaywallHit(phrase=phrase, kind="gate")
    for _start, _end, unit in _paywall_units(text):
        ulow = unit.lower()
        for cta in PAYWALL_CTA_PHRASES:
            clow = cta.lower()
            pos = ulow.find(clow)
            while pos >= 0:
                ctx = _context_outside(ulow, pos, pos + len(clow))
                if ctx:
                    return _PaywallHit(phrase=cta, kind="cta", context=ctx)
                pos = ulow.find(clow, pos + 1)
    # L1.5 条件式门禁：播放器报错短语 + 会员电商信号同页同现。
    for phrase in PAYWALL_ERROR_GATE_PHRASES:
        if phrase.lower() in low:
            for ctx in PAYWALL_ERROR_CONTEXT_WORDS:
                if ctx.lower() in low:
                    return _PaywallHit(
                        phrase=phrase, kind="gate", context=ctx
                    )
            # 报错短语命中但无会员电商信号——不成立（就是 broken/错误页）。
            break
    return None


def paywall_evidence(text: str) -> str:
    """正文里的**付费墙门禁证据**描述，空串 = 没有证据。

    这是**判定付费墙的唯一入口**（2026-10-21，H-16 修复①）。
    判据见 :func:`_find_paywall_hit` 的说明。

    返回的是**给 evidence 用的描述**（命中哪个短语、哪一层、有没有
    语境词），不是布尔值——凡是靠它改写分支的地方，evidence 必须能
    指认判据，否则半年后没人说得清「凭什么算付费墙」。
    """
    hit = _find_paywall_hit(text)
    if hit is None:
        return ""
    if hit.kind == "gate":
        if hit.context:
            return f"付费墙门禁「{hit.phrase}」+ 会员电商信号「{hit.context}」"
        return f"付费墙门禁短语「{hit.phrase}」"
    return f"付费墙 CTA「{hit.phrase}」+ 同句观看语境「{hit.context}」"


# ── 付费墙强弱信号分级 ─────────────────────────────────────────────────
#
# 2026-10-13 增量三十二引入（原在 ``executor/branches.py``，仅用于同页守卫
# 旁路）；2026-10-15 INC43 移到感知层——②③ 负分支的付费墙救济需要同一套
# 判据，两处各留一份的后果是「守卫放行了、救济不认」或反过来，而两者
# 本就是同一句话的两个说法。``branches.py`` 反向导入（executor → perception
# 单向依赖，不破层）。
#
# ⚠️ **2026-10-21 口径变更（H-16 修复①）**：这张表**不再用于贴分支**。
# 原来 ②③ 的付费墙救济（``paywall_rescue_evidence``）走的是
# 「词表命中 **或** 强弱信号组合」——两个口径都会把**导航栏 CTA** 与
# **续费营销**判成付费墙（实测 12 条 ``subscription_wall`` 没有一条
# 站得住）。现在救济只认**门禁语境**（:func:`paywall_evidence`）。
#
# 强弱信号**仍在用**，但用途收窄成**不贴分支**的地方：
#   - :mod:`trajectory_pipeline.executor.gatekeeper` 的 ``is_paywall``
#     （要不要把页面拦下来）。
#
# ⚠️ **2026-10-21**：原先的第二处用途（``executor.branches`` 的同页守卫
# 旁路）随同页守卫窄化而**失去拦截对手**，那个适配函数
# ``_has_strong_paywall_signal`` 已删除——**今天强弱信号只管拦截**。
# 这里要的是「这页**像**付费墙吗」，不是「这页**是不是**付费墙」——
# 拦错页面与贴错分支，代价不是一个量级。
#
# 「开通」「会员」单独放**弱**组：站点导航栏广告高频短语（「开通会员 享
# 更多精彩」），单凭它们分不清真付费墙与 nav-bar CTA。要过强信号闸，
# 必须至少配一个强词或价格模式。

STRONG_PAYWALL_KEYWORDS: tuple[str, ...] = (
    # VIP 标记 / 订阅 / 续费 / 价格动词 / 营销词——这些在真付费墙页
    # 几乎必然出现，而在站点入口页导航栏里**不会**同时出现多个。
    "VIP", "订阅", "续费", "包月", "包年", "专享", "特惠",
)
WEAK_PAYWALL_KEYWORDS: tuple[str, ...] = (
    # 开通 / 会员——这两个词在站点导航栏广告里高频出现，单凭它们
    # 不能区分「真付费墙」与「站点入口页 nav-bar CTA」。
    "开通", "会员",
)
PRICE_PATTERN = re.compile(
    r"(?:[¥￥$€£]\s*\d|\d+\s*元|\d+\.\d+元\s*/\s*月|\d+月\d+元|首\s*\d+\s*月)"
)


def has_strong_paywall_signal(text: str) -> bool:
    """文本里的付费墙信号是否**足够强**（增量三十二同款三判据）。

    任一即可：
      - ≥1 强关键词 + ≥1 弱关键词（「VIP + 会员」、「订阅 + 开通」）；
      - ≥2 强关键词（「VIP + 续费」、「订阅 + 包年」）；
      - ≥1 强关键词 + 价格/优惠模式（「VIP + ¥99」）。

    空串按弱信号处理——不旁路、不救济。

    ⚠️ **不用于贴分支**（2026-10-21）：它答的是「这页**像**付费墙吗」，
    用来**拦页面**与**放行同页守卫**；贴分支要答「这页**是不是**付费墙」，
    那是 :func:`paywall_evidence` 的门禁语境判据。
    """
    ev = (text or "").strip()
    if not ev:
        return False
    strong = sum(1 for kw in STRONG_PAYWALL_KEYWORDS if kw in ev)
    weak = sum(1 for kw in WEAK_PAYWALL_KEYWORDS if kw in ev)
    has_price = bool(PRICE_PATTERN.search(ev))
    if strong >= 1 and weak >= 1:
        return True
    if strong >= 2:
        return True
    return strong >= 1 and has_price


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
#
#: 2026-10-13 增量三十二 · ``not_found`` 新增英文 + 5xx 短语：
#: D10 偏差修缮覆盖 kmslxsh/nginx 404、v.qq.com/error.html 那条视频不见了、
#: ixigua Internal Server Error 三类实测样例；不带"X 不存在"这类短语的口径
#: 不变（见下方注释）。
ACCESS_HINTS: tuple[tuple[str, tuple[str, ...]], ...] = (
    (
        "login", (
            "登录后观看", "登录后可", "登录后免费", "请登录",
            "请先登录", "需登录", "登录解锁", "登录会员",
            "登录后继续", "登录才能", "未登录",
            # 2026-10-21（H-16 修复①）：「sign in to watch」原来在付费墙
            # 词表里——但它是**登录门**不是付费门。「登录了也要付钱」
            # （subscription_wall）与「登录后免费」（login_required）是
            # 两条不同的分支，判错方向的代价是**把可绕过的登录墙写成
            # 绕不过去的订阅墙**。
            "sign in to watch", "sign in to view",
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
            # ⚠️ **2026-10-21 去掉「对不起」**（H-16 修复①）：它是**礼貌语**
            # 不是 404 证据。实测正文里出现「对不起，评论暂时关闭」
            # （正常播放页的评论区）——留着它，一次正文扫描就能把正常页面
            # 判成 ``not_found``，进而在让位规则里**收回**付费墙标签，
            # 把一条真负样本变成 ``broken``（错因）或 ``unresolved``
            # （少一条负样本）。LLM evidence 里的「对不起，该视频已被删除」
            # 本来就带着「已被删除」，去掉它不丢覆盖。
            "页面不存在", "已下架", "已被删除",
            "该视频已被删除", "404 错误", "404错误",
            "找不到该视频",
            # 2026-10-13 增量三十二 · D10 偏差修缮：404/5xx 形态落 broken 的设计
            # 意图（见 evaluation/golden/alignment.py）此前因为关键词只覆盖中文
            # 短句、漏英文/英文混合页面（kmslxsh.com/nginx 的「404 Not Found」、
            # ixigua 的「Internal Server Error」），fallback 返回空串、
            # branches.py:decision_branch 退化到 login_wall_or_blocked 合并态。
            # 补英文 + 通用 5xx 短语，覆盖三类实测样例：
            #   - nginx 默认 404 页（kmslxsh.com 实测）
            #   - 腾讯视频 error.html 那条视频不见了（v.qq.com 实测）
            #   - 西瓜视频 5xx 错误页（m.ixigua.com 实测）
            "404 Not Found", "404 not found", "Not Found",
            "Internal Server Error", "500 Internal", "502 Bad Gateway",
            "nginx", "那条视频不见了",
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


#: **环境阻断形态**——「不是不给钱，是这个环境不给看」。
#:
#: 刻意**不含 ``login``**：站点导航栏的「请登录」与「开通会员」一样高频
#: （实测百度页正文就有「免费观看」），拿登录形态给付费墙让位，会把
#: 整批订阅墙赶进 ``unresolved``——那是**假 fail-closed**，不是严谨。
#: 登录墙有自己的判断点（② 的 ``access_control_type`` 结构化字段）。
ACCESS_DEFECT_KINDS: tuple[str, ...] = ("region", "captcha", "anti_bot", "not_found")


def infer_access_defect(text: str) -> str:
    """文本里的**环境阻断形态**（地区 / 验证码 / 反爬 / 404），空串=无。

    这是**付费墙的让位判据**（2026-10-21，H-16 修复①）：页面原文里已经
    出现「当前设备不支持播放」「70017001」「请完成验证」这类形态时，
    真因是**环境阻断**，不是「要付钱」——两者的处置与训练信号都不同。
    实测 v.qq.com 8 条：主证据是「当前设备不支持播放」+ 70017001.1，
    「开通会员」只是句尾附加；被付费墙抢走就贴错了支。

    与 :func:`infer_access_from_evidence` 的差别是**不含 login**
    （理由见 :data:`ACCESS_DEFECT_KINDS`）。
    """
    if not text:
        return ""
    norm = _norm(text)
    for value, keywords in ACCESS_HINTS:
        if value not in ACCESS_DEFECT_KINDS:
            continue
        for kw in keywords:
            if _norm(kw) in norm:
                return value
    return ""


def access_evidence(text: str, *, kind: str = "") -> str:
    """文本里的**拦截形态证据描述**，空串 = 没有证据。

    ``kind`` 非空时只在该类里找（``"region"`` / ``"login"`` …）——
    调用方已经知道是哪一类时不必再让 ``ACCESS_HINTS`` 的顺序替它选
    （顺序里 ``login`` 在最前，一段同时出现登录与地区短语的文本会永远
    报登录，那样错误消息里引的原话就不是判据命中的那一句）。

    与 :func:`paywall_evidence` **对称**：那个回答「这页是不是要付钱」，
    这个回答「这页是不是被拦住了」，返回的都是**给 evidence 用的描述**
    （命中哪个短语、属于哪一类），不是布尔值——凡是靠它改写结论的地方，
    evidence 必须能指认判据，否则半年后没人说得清「凭什么算登录墙」。

    **它不是新的判据**：词表仍是 :data:`ACCESS_HINTS`，判定语义仍由
    :func:`infer_access_from_evidence` 定义，这里只把「命中了哪一句」
    摊开。加它是因为读报告的人要看原话——只给 ``region`` 三个字母，
    复核时还得自己回去 grep 一遍正文（D-5 的证据核就卡在这上面）。

    ⚠️ **含 ``login``**：这里回答的是「记录里有没有拦截形态」，
    不参与 H-16 的让位裁定（那用的是 :func:`infer_access_defect`，
    **不含** login）。两个问题不同，别混用。
    """
    if not text:
        return ""
    norm = _norm(text)
    for value, keywords in ACCESS_HINTS:
        if kind and value != kind:
            continue
        for kw in keywords:
            if _norm(kw) in norm:
                return f"「{kw}」（{value}）"
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
            if is_play_control(e.name) and classify_trailer(e.name) == "none"]


def trailer_only_candidates(obs: Observation) -> list[InteractiveElement]:
    """**预告**播放控件——与 :func:`play_control_candidates` 互补，两者并起来
    就是 :func:`is_play_control` 筛出的全部候选。

    「有播放控件但全是预告片」与「压根没有播放控件」是**两条不同的负分支**
    （``trailer_only`` vs ``no_play_control``），所以判据必须同源：W1 在这里
    落 ``trailer_only``、W3 落 ``no_play_control``，同一批素材会出两个结论，
    且两个都看着像对的——这正是词表复制的代价。
    """
    return [e for e in obs.interactive_elements
            if is_play_control(e.name) and classify_trailer(e.name) != "none"]


# ── 站内搜索框词表（2026-10-16 路径 B/C）─────────────────────────────────

#: 搜索框的肯定信号。命中即进入候选池。
#:
#: 只收**明确写着搜索语义**的词。输入框的 label 通常是 placeholder
#: 文案（「搜索片名」「输入影视剧名称」），命中「搜索/检索」即算；
#: 刻意不含「输入」「关键词」这类泛输入词——登录框、邮箱订阅框的
#: placeholder 也长这样（INC43 教训：词表必须核信号位置，泛词的
#: 误伤面控制不住）。
SEARCH_BOX_KEYWORDS: tuple[str, ...] = ("搜索", "检索", "search")

#: 可能承载搜索框的标签。``input`` 是主体、``textarea`` 少见但同构；
#: **刻意不含** ``button`` / ``a``——本词表服务的是
#: :func:`~trajectory_pipeline.perception.llm_perceptor.LLMPerceptor._find_site_search`
#: 的 ref 兜底，取回的 ref 要被 ``fill`` 填词，填进按钮会直接
#: DriverError。「搜索」按钮的点击路径是 site_search 的
#: ``SEARCH_BUTTON_HINTS``（那边是回车失败后的第二条提交路），两边
#: 词表各管各的动作，不共用。
#:
#: **一套词表一个方言，按观察自带的词汇表取**（P4 收窄；P3-α 的并集已退役）。
#:
#: 并集的问题是它**在两个方言下都会命中错的东西**：``role`` 槽里装的是
#: ``input`` 还是 ``textbox``，并集都能匹配，于是「换了执行层」这件事在
#: 这里完全没有痕迹——而两套词表本来就不该通用（``input`` 是标签名，
#: ``textbox`` 是 role，同一个控件在两面各写各的）。
#:
#: 词汇表认不出时**抛**，不回落任何一套：回落等于猜「这份观察大概是什么面」，
#: 猜错的症状是静默取回 0 个候选，与「这页确实没有搜索框」同形。
#:
#: **不收 ``combobox``**：playwright 给 ``<select>`` 的 role 就是 ``combobox``，
#: 而往 select 里 ``fill`` 会直接失败——那是「取回一个填不进去的 ref」，
#: 比取回 0 个更坏（0 个会走「没有搜索框」分支，错的很明显）。
SEARCH_BOX_WORDS: Mapping[str, frozenset[str]] = MappingProxyType({
    # HTML 标签名（obscura 面）
    ELEMENT_VOCAB_HTML_TAG: frozenset({"input", "textarea"}),
    # ARIA role（camoufox 面）
    ELEMENT_VOCAB_ARIA_ROLE: frozenset({"textbox", "searchbox"}),
})


def search_box_words(obs: Observation) -> frozenset[str]:
    """该观察的元素词表对应的「可填词控件」词集。认不出**抛**（不猜）。"""
    try:
        return SEARCH_BOX_WORDS[obs.element_vocabulary]
    except KeyError:
        raise ValueError(
            f"认不出的元素词汇表 {obs.element_vocabulary!r}——"
            f"搜索框词表无从确定。已知的：{sorted(SEARCH_BOX_WORDS)}"
        ) from None


def search_box_candidates(obs: Observation) -> list[InteractiveElement]:
    """页面上的站内搜索框候选，按观察原序。

    ``FIND_SITE_SEARCH`` 的代码侧兜底（模型不回传 ref 时由它取回，
    :func:`play_control_candidates` 在 ③ 的地位同款）与交叉印证。
    词表唯一所有者在这里，避免 W3 兜底与单测各写一份。

    匹配前把 label 归一并**去掉全部空格**（「搜 索」→「搜索」）——
    按钮文案带空格排版是常见形态，逐变体列词表不如归一后比对。
    """
    words = search_box_words(obs)
    out: list[InteractiveElement] = []
    for e in obs.interactive_elements:
        if (e.role or "").lower() not in words:
            continue
        text = _norm(e.name).replace(" ", "")
        if text and any(kw in text for kw in SEARCH_BOX_KEYWORDS):
            out.append(e)
    return out


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
        # 2026-10-16 路径 B/C：新增六题（SELECT_AGGREGATOR_RESULTS /
        # IS_AGGREGATOR / SELECT_SITE_ENTRIES / SELECT_ARTICLE_RESULTS /
        # SELECT_REFERRAL_LINKS / FIND_SITE_SEARCH）与方案 12 阶段 A1 的
        # CLASSIFY_SEARCH_LINKS **刻意不挂 handler**——
        # 全是语义题，W1 一律 fail-closed，与 ①② 现状一致。
        # ``search_box_candidates`` 词表只作为 W3 的 ref 兜底存在，
        # 不构成 W1 的判定能力（「词表命中」≠「语义确认」，同
        # :func:`play_control_candidates` 不让 W1 独立判 ③ 的理由）。
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
        return True, ("确定性实现：可判 trailer_only；"
                      "④ 播放页判不出来一律 None（2026-10-23 · M-26 起不凭媒体计数判成功）；"
                      "其余题 fail-closed")

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
        candidates = [e for e in obs.interactive_elements if is_play_control(e.name)]
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
            suspects = [e for e in candidates if classify_trailer(e.name) == "suspect"]
            return self._answer(
                spec, True,
                evidence=f"命中播放控件 ref={real[0].ref} name={real[0].name!r}",
                started=started,
                payload={"ref": real[0].ref, "trailer_only": False,
                         "trailer_suspect": [e.name for e in suspects],
                         "why": f"命中 {len(real)} 个非预告控件"},
            )
        # 有候选但全是预告片 —— 这是业务规则里的「播放资源不存在」
        suspects = [e.name for e in candidates
                    if classify_trailer(e.name) == "suspect"]
        return self._answer(
            spec, True,
            evidence=(
                f"{len(candidates)} 个候选控件全为预告："
                f"{[e.name for e in candidates][:3]}"
            ),
            started=started,
            payload={"ref": "", "trailer_only": not suspects,
                     "trailer_suspect": suspects,
                     "why": "候选控件文本均命中预告片词表"},
        )

    def _player_ok(self, spec: Any, obs: Observation, started: float) -> Decision:
        """判断点 ④：播放页是否有媒体组件。

        **``<video>`` / ``<audio>`` 的存在与 iframe 数量都不是判据**——
        两种形态都返回 ``None``（见下）。

        实测把 iframe 当判据会直接产出假阳性：hao123（纯导航站）首页有
        **15** 个 iframe，于是被判「播放页 OK」——而它连作品都不对应。
        iframe 可以是广告、埋点、地图、天气，**数量与「是不是播放器」无关**。
        要真判 iframe 里是不是播放器得看 src 与尺寸，那是启发式阈值，
        拿不准的东西不该当判据。

        **两种形态都返回 ``answer=None``**（2026-10-23 · M-26 改）：
        有标签时**原来判 True**（「存在 ⇒ 成功」），那是**护栏越权**——
        代码层可以**否**，不能**许**；媒体计数只能当**必要条件**，不能当
        **充分条件**（实测：优酷「试看6分钟｜开通会员看完整视频」页
        ``video_tag_count=1`` ⇒ 被读成「能看正片」⇒ **5 份存档 5 条假成功**，
        而 LLM 压根没被问过）。
        规则版**没有 LLM 可让**（LLM 版的对应修复是删掉短路、改问 LLM），
        而 ``False`` 更糟——那会把「能看但判不出」写成**真负样本**进池；
        ⇒ 唯一不撒谎的取值是 ``None``：判不出来，交人工
        （落 ``player_unverified``，不进负样本池，且在人工复核队列里）。
        没有标签时同样返回 ``None`` 而非 False——JS / canvas / iframe 播放器
        都不产生这些标签，「没测到」≠「没有」。优酷的播放页正是这种形态
        （video=0 iframe=1），W1 判不出来就该说判不出来：少一条正样本，
        比多一条假阳性划算（假阳性会污染成功率的基线）。
        """
        media = obs.video_tag_count
        frames = obs.iframe_count
        if media >= 1:
            # ⚠️ 2026-10-23（M-26）：**原来这里返回 ``True``**——
            # 「存在标签 ⇒ 判成功」。删掉的理由见上面 docstring与台账 §M-26：
            # 护栏可以否、不能许，媒体计数不是成功的充分条件。
            # 这里保留一条**独立分支**而不是直接落下面的通用 ``None``：
            # 两种「判不出来」的成因不同（**有组件但不知能不能用** vs
            # **连组件都没测到**），诊断信息要分开说。
            return self._answer(
                spec, None,
                evidence=(
                    f"存在 <video>/<audio> 标签 ×{media}（iframe={frames}）——"
                    "**有播放组件 ≠ 能正常播放**；代码层不许凭计数许诺成功"
                    "（2026-10-23 · M-26），判不出来交人工"
                ),
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
        # 2026-10-13 增量三十二 · D10 偏差修缮：英文 + 通用 5xx 短语落
        # not_found——覆盖三类实测样本：
        #   - nginx 默认 404 页（kmslxsh.com 实测）
        #   - 腾讯视频 error.html 那条视频不见了（v.qq.com 实测）
        #   - 西瓜视频 5xx 错误页（m.ixigua.com 实测）
        # 与 ACCESS_HINTS["not_found"] 同步增补。
        "404 Not Found", "Not Found",
        "Internal Server Error", "500 Internal", "502 Bad Gateway",
        "nginx", "那条视频不见了",
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
    # L3 是**粗分类层**（要的是「页面里有没有付费相关字样」），
    # 用的是**全集**（门禁 + CTA）——它不贴分支，贴分支的判据是
    # :func:`paywall_evidence` 的门禁语境。两者不是一回事，别混用。
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
