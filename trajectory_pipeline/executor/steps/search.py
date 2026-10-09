"""检索步骤——**取回候选站点**，不判断「哪个能看」。

分工边界（本文件存在的全部理由）：

    代码取回候选            → 本文件，事实层
    判断哪些是对应播放站    → ``Q.SELECT_PLAY_SITES``，语言层

「这个链接是不是能看《功夫》的网站」**必然是语义判断**——URL 里的 `iqiyi.com`
只说明它是爱奇艺，页面是否对得上这部作品要看了才知道。所以代码**不做**这个判断。

但 W1 的 ``RulePerceptor`` 对 ``SELECT_PLAY_SITES`` 只会返回 ``answer=None``
（fail-closed），若就此停下，W1 一条素材都跑不出来，负样本池永远空。
解法是让代码提供**候选**而不是**结论**——

    代码说：这 20 个链接是可以访问的外部站点，按引擎排定的相关度排序
    感知层说：其中哪些对应目标作品、哪些能在线观看（W3 的 LLMPerceptor 才做）

所以 W1 产出的一切站点级结论都带 ``fallback_used=True``，并在存档里标明
候选来源是启发式。**这批数据不能被读成「规则版能选出正确站点」——它不能。**
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from urllib.parse import parse_qs, unquote, urlparse

from trajectory_pipeline.perception.base import Observation

#: 搜索引擎。**默认值是实测可达的那个**（``probe_observe`` 跑过 baidu）。
#: bing/duckduckgo 在国内网络下时通时不通，不能当默认。
DEFAULT_ENGINE = "baidu"

#: 引擎的搜索 URL 模板。``{q}`` 会被 :func:`quote` 转义。
ENGINE_TEMPLATES: dict[str, str] = {
    "baidu": "https://www.baidu.com/s?wd={q}",
    "bing": "https://www.bing.com/search?q={q}",
    "duckduckgo": "https://duckduckgo.com/?q={q}",
}

#: 候选里直接剔除的域名**族**——搜索引擎自身、社交、字体、统计。
#: 全部是**结构性事实**（链接指向的根本不是内容站），不是语义判断：
#: 社交站点不会变成播放站，判它错没有收益。
#:
#: ⚠️ **按「注册域族」声明，不用裸 ``endswith``**。
#: 早期版本用 ``host.endswith(engine_host)`` 剔搜索引擎自身域，
#: 而 ``engine_host`` 是 ``www.bing.com``——于是 bing 结果里的
#: ``help.bing.microsoft.com`` 漏网（它不是 ``www.bing.com`` 的后缀），
#: 被当成候选站点遍历，最后判成 ``no_play_control`` **进了负样本池**。
#: 一个帮助中心页成为「这个站没有播放控件」的证据，是纯粹的污染。
#:
#: 匹配一律带点边界（``host == root or host.endswith("." + root)``），
#: 否则 ``notbing.com`` 会被 ``bing.com`` 误杀。
#:
#: 刻意**不放** ``cdn.`` / ``static.`` 这类子域前缀：它们在搜索结果页
#: 几乎不出现（结果页给的是页面链接），而前缀匹配会误杀
#: ``static.example.com`` / ``cdn.mysite.com`` 这种本身就是内容站的域名。
EXCLUDED_HOST_ROOTS: tuple[str, ...] = (
    # 搜索引擎及其附属域
    "baidu.com", "bdstatic.com", "bcebos.com",
    # bing 的帮助中心走的是 **microsoft.com**，不是 bing.com
    "bing.com", "microsoft.com", "msn.com", "live.com", "windows.net",
    "duckduckgo.com", "duck.co",
    "google.com", "gstatic.com", "googleapis.com", "fonts.googleapis.com",
    "google-analytics.com", "w3.org",
    # 社交 / UGC（不会变成播放站）
    "zhihu.com", "weibo.com", "douban.com",
    "facebook.com", "twitter.com", "instagram.com",
)

#: **不可能**是影视资源站的注册域族。
#:
#: 实测 bing 结果里混进了 ``beian.miit.gov.cn``（ICP 备案查询）、
#: ``dxzhgl.miit.gov.cn``（企业信息公示）、``beian.mps.gov.cn``——
#: 全是政府站的查询页，零播放控件，被判成 ``no_play_control``
#: **直接进负样本池**，把负样本纯度这个指标的底子直接腐蚀掉。
#:
#: 只放 ``gov.cn``。**不连带放 ``edu.cn``**：部分高校图书馆确有
#: 影视资料页，误杀候选的代价（少一条可看的站）虽然低于误判的代价，
#: 但它是**可避免**的；而 ``gov.cn`` 不可能有影视资源，无误杀可能。
#:
#: 这里判断的是"结构性不可能"，**不是**「页面上没有播放控件」——
#: 后者仍归感知层。
IMPOSSIBLE_CONTENT_ROOTS: tuple[str, ...] = ("gov.cn",)

#: 非 http(s) 的 scheme 一律不是站点链接。
_BROWSABLE_SCHEMES = ("http://", "https://")

#: URL 里出现这些片段的多半是功能页而不是内容站（用户注册/反馈/协议）。
_PATH_NOISE = re.compile(r"/(login|signup|register|help|feedback|about|"
                         r"privacy|terms|agreement|contact|app|download)s?\b",
                         re.IGNORECASE)


def quote(query: str) -> str:
    """URL 转义。用 ``quote_plus``——空格必须是 ``+`` 否则搜索引擎收到的是别的。"""
    from urllib.parse import quote_plus

    return quote_plus(query)


def search_url(query: str, engine: str = DEFAULT_ENGINE) -> str:
    """构造搜索页 URL。未知引擎抛错——不猜默认，避免静默走到错引擎。"""
    template = ENGINE_TEMPLATES.get(engine)
    if template is None:
        raise ValueError(
            f"未知搜索引擎 {engine!r}；可用：{sorted(ENGINE_TEMPLATES)}"
        )
    return template.format(q=quote(query))


def build_query(
    title: str,
    *,
    intent: str = "watch",
    persona: object | None = None,
) -> str:
    """构造搜索词。

    ``persona`` 为 ``None`` 时走默认词表——W1 的 persona 库尚未接入，
    但**接口先留出来**：persona 管输入分布，若 search step 不知道 persona，
    整个输入分布就无法从 persona 传导到采集侧，模块 1 就成了摆设。

    persona 接入后应按 ``genre`` / ``urgency`` 改写措辞（如急迫型 persona
    用「在线看」而不是「怎么在线看」），因为不同措辞召回的站点池不同——
    这正是 persona 维度要覆盖的东西。
    """
    base = title.strip()
    if intent == "watch":
        return f"{base} 在线观看"
    if intent == "play":
        return f"{base} 在线播放"
    if intent == "raw":
        return base
    raise ValueError(f"未知 intent: {intent!r}")


@dataclass(frozen=True, slots=True)
class Candidate:
    """一个候选站点。

    ``rank`` 是**引擎排定的顺序**，不是本项目的相关性判断——
    留这个字段是为了落盘时可复核「我们是不是把第 1 名跳过了」。

    ``source_href`` 是它在**搜索页观察里**的原始 href，与 :attr:`url`
    可能不同（百度结果页大量用 ``/link?url=`` 包裹，见 :func:`_unwrap_redirect`）。

    为什么两个都要：感知层读的是**观察**（:attr:`source_href`），而遍历
    用的是解包后的 :attr:`url`。判断点 ① 回传的 url 是前者，控制器要
    拿它对上后者才能过滤候选——少记这个字段就得靠字符串猜测，
    而百度链接一旦解码失败就是「整个站点集丢失」。
    """

    url: str
    text: str
    rank: int
    source_href: str = ""

    @property
    def host(self) -> str:
        try:
            return (urlparse(self.url).hostname or "").lower()
        except ValueError:
            return ""


def _host_in(host: str, roots: tuple[str, ...]) -> str:
    """host 是否落在任一注册域族内。返回命中的 root 或空串。

    点边界匹配——``bing.com`` 匹配 ``www.bing.com`` 与 ``bing.com``，
    但**不**匹配 ``notbing.com``。无边界的后缀匹配会误杀真实站点，
    而误杀候选的代价（少一条可看的站）高于漏掉一条帮助页。
    """
    for root in roots:
        if host == root or host.endswith("." + root):
            return root
    return ""


def exclusion_reason(url: str, engine: str) -> str:
    """这个链接为什么被剔除。返回原因串，**空串 = 保留**。

    返回原因而不只是 bool，是为了给 :func:`extract_candidates` 记账：
    「引擎给了 50 个链接、我们只跑了 11 个」中间那 39 个去哪了，
    必须能从存档里读到，不能静默。
    """
    if not url.startswith(_BROWSABLE_SCHEMES):
        return "non_http_scheme"
    try:
        parsed = urlparse(url)
    except ValueError:
        return "unparseable"
    host = (parsed.hostname or "").lower()
    if not host:
        return "no_host"
    engine_host = urlparse(ENGINE_TEMPLATES[engine].split("{q}")[0]).hostname or ""
    if engine_host and host == engine_host:
        return "engine_self"
    hit = _host_in(host, EXCLUDED_HOST_ROOTS)
    if hit:
        return f"excluded_host:{hit}"
    hit = _host_in(host, IMPOSSIBLE_CONTENT_ROOTS)
    if hit:
        return f"impossible_content:{hit}"
    if _PATH_NOISE.search(parsed.path or ""):
        return "path_noise"
    # 搜索引擎的跳转中转链接：/link?url=<真实地址> 或 /ck/a?...&url=
    target = parse_qs(parsed.query or "").get("url")
    if target and target[0].startswith(_BROWSABLE_SCHEMES):
        return "redirect_wrapper"
    return ""


def _is_excluded(url: str, engine: str) -> bool:
    """结构性剔除。**全部可用 URL 本身判断，不看页面内容**。"""
    return bool(exclusion_reason(url, engine))


def _unwrap_redirect(url: str) -> str:
    """取出搜索引擎中转链接里的真实地址；不是中转则原样返回。

    百度结果页大量使用 ``/link?url=`` 包裹。**不剥就完全拿不到候选**，
    而「剥」是纯字符串事实（query 里有没有 ``url=``），不涉及语义。
    """
    try:
        parsed = urlparse(url)
    except ValueError:
        return url
    target = parse_qs(parsed.query or "").get("url")
    if target and target[0].startswith(_BROWSABLE_SCHEMES):
        return unquote(target[0])
    return url


def extract_candidates(
    obs: Observation, *,
    engine: str = DEFAULT_ENGINE,
    limit: int = 20,
    stats: dict[str, int] | None = None,
) -> list[Candidate]:
    """从搜索结果页提取候选站点。

    **保留页面上的原始顺序**——搜索结果本就按相关性排过，
    重新排序等于用我们自己的启发式覆盖引擎的判断，那是没有依据的。

    去重按 host：同一站点的多个入口（首页 + 剧集页）只取第一个，
    否则遍历阶段会在同一个站上反复卡住。

    ``stats`` 是**可选出参**，传进来就被就地填充
    ``{剔除原因: 条数}``。剔除是必然发生的（引擎结果页里一半是自家
    功能链接），而**静默剔除会让损失不可见**——存档上写着 11 个候选，
    没人知道引擎原本给了 50 个链接、其中多少被规则扔掉、为什么扔。
    """
    def bump(reason: str) -> None:
        if stats is not None:
            stats[reason] = stats.get(reason, 0) + 1

    if obs.degraded:
        # 采集降级 → 返回空并让调用方记账。不在这里 raise：
        # 单次采集失败不该炸掉整批运行
        bump("degraded")
        return []

    out: list[Candidate] = []
    seen_hosts: set[str] = set()
    for link in obs.links:
        raw = _unwrap_redirect(link.href)
        reason = exclusion_reason(raw, engine)
        if reason:
            bump(reason)
            continue
        host = (urlparse(raw).hostname or "").lower()
        if not host:
            bump("no_host")
            continue
        if host in seen_hosts:
            bump("duplicate_host")
            continue
        seen_hosts.add(host)
        out.append(Candidate(url=raw, text=link.text, rank=len(out) + 1,
                            source_href=link.href))
        if len(out) >= limit:
            bump("over_limit")
            break
    return out