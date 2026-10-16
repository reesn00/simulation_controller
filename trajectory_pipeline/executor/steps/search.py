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
import zlib
from dataclasses import dataclass
from typing import Any, Mapping
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

#: 候选里直接剔除的域名**族**——搜索引擎自身、附属域、海外社交。
#: 全部是**结构性事实**（链接指向的根本不是内容站），不是语义判断。
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
#:
#: 2026-10-16 方案 12 阶段 A1 拆分：zhihu / douban / weibo **移出**本表。
#: 它们是**语义信源**而不是结构噪声——知乎推荐贴 / 豆瓣微博讨论帖是
#: 「文章找视频站引荐链接」（路径 C）的正主，此前按「社交/UGC 不会变成
#: 播放站」整族剔除，等于把路径 C 的入口池系统性清零。移出后它们进
#: 候选池，由分类任务（``Q.CLASSIFY_SEARCH_LINKS``）判为
#: ``article_post`` 类走路径 C；在 direct 批次里被分类为非 ``video_site``
#: 而记 ``not_play_site``——那是有证据的真负样本，不是污染。
#: 海外社交（facebook / twitter / instagram）留下：中文引擎结果里它们
#: 不是任何发现路径的信源，剔除仍是纯结构事实。
EXCLUDED_HOST_ROOTS: tuple[str, ...] = (
    # 搜索引擎及其附属域
    "baidu.com",
    "bdstatic.com",
    "bcebos.com",
    # bing 的帮助中心走的是 **microsoft.com**，不是 bing.com
    "bing.com",
    "microsoft.com",
    "msn.com",
    "live.com",
    "windows.net",
    "duckduckgo.com",
    "duck.co",
    "google.com",
    "gstatic.com",
    "googleapis.com",
    "fonts.googleapis.com",
    "google-analytics.com",
    "w3.org",
    # 海外社交（中文信源之外的结构噪声，见上方拆分说明）
    "facebook.com",
    "twitter.com",
    "instagram.com",
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
_PATH_NOISE = re.compile(
    r"/(login|signup|register|help|feedback|about|"
    r"privacy|terms|agreement|contact|app|download)s?\b",
    re.IGNORECASE,
)


def quote(query: str) -> str:
    """URL 转义。用 ``quote_plus``——空格必须是 ``+`` 否则搜索引擎收到的是别的。"""
    from urllib.parse import quote_plus

    return quote_plus(query)


def search_url(query: str, engine: str = DEFAULT_ENGINE) -> str:
    """构造搜索页 URL。未知引擎抛错——不猜默认，避免静默走到错引擎。"""
    template = ENGINE_TEMPLATES.get(engine)
    if template is None:
        raise ValueError(f"未知搜索引擎 {engine!r}；可用：{sorted(ENGINE_TEMPLATES)}")
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


#: 路径 B（聚合站发现）的通用检索词表。**与片名无关**——聚合站的
#: 发现不靠片名（任何片名的聚合站入口池是同一个），靠的是「用聚合词
#: 能搜到哪些导航站」。按 task 轮换而不是固定一个，是让三条词的
#: 召回差都进批次——固定一条会把「这条词搜不到的聚合站」系统性排除，
#: 而那不是站点不存在，是我们没问。
AGGREGATOR_QUERIES: tuple[str, ...] = (
    "影视聚合站",
    "影视网站导航",
    "在线影视资源站大全",
)


def aggregator_query(seed: str) -> str:
    """按 seed（task_id）稳定轮换一条聚合检索词。

    用 ``zlib.crc32`` 而不是内置 ``hash``：后者每进程随机
    （PYTHONHASHSEED），同一 task 重跑会换词，而「这个 task 用了哪条
    词」必须可以从 task_id 重算出来——存档可归因的前提是**确定性**。
    """
    return AGGREGATOR_QUERIES[zlib.crc32(seed.encode("utf-8")) % len(AGGREGATOR_QUERIES)]


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

    ``origin`` / ``parent`` 是发现链溯源（2026-10-16 路径 B/C）：
    ``origin`` 记这个候选**从哪条发现路径**来——``"search"``（直搜，含
    站内搜索结果页）、``"aggregator"``（聚合站入口）、``"article"``
    （文章引荐外链）、``"site_search"``（站内搜索派发的子候选）；
    ``parent`` 记派发它的**中间页 URL**（聚合站页 / 文章页 / 站内搜索
    结果页），直搜候选为空串。批次出了问题要能从候选回溯到产地，
    没有这两个字段就只能按 URL 猜。默认值保证直搜路径（A）的
    全部既有构造点零改动。
    """

    url: str
    text: str
    rank: int
    source_href: str = ""
    opaque_redirect: bool = False
    origin: str = "search"
    parent: str = ""

    @property
    def host(self) -> str:
        # 密文中转包裹（百度 /link?url=<密文>）：目标 host 未知，
        # **不伪装成 baidu.com**（05 方案 §2.5）——空串 = 未知，
        # 存档/报表读到的是「未知」而不是「引擎自家域」。
        if self.opaque_redirect:
            return ""
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


#: 每个引擎的「结果中转路由」形态：path 前缀 + query 键（纯结构事实）。
#:
#: 百度自然结果 100% 是 ``//www.baidu.com/link?url=<密文>`` 包裹——真实结果的
#: href 不带目标站域名，而是百度服务端签名参数（**解不开**，不是 URL 编码）。
#: 05 方案 §1.1/§1.2 的证据：TPIN01 存档 ``candidate_filter.engine_self=91``，
#: 真实结果全部死于 :func:`exclusion_reason` 的 ``engine_self`` 判定——
#: ``host == www.baidu.com`` 在走到 ``redirect_wrapper`` 之前就被当
#: 「引擎自身页」剔掉，幸存候选只剩 text 无片名的聚合卡片裸链。
#:
#: 这里把「引擎中转结果」从「引擎自身功能页」拆出来：判定只看路由形态
#: （path 前缀 + query 键存在性），**不猜密文内容**。bing 的 ``/ck/a?…&u=``
#: 是明文 base64（本质不同），属后续独立一轮，刻意不进此表（05 方案 §6-1）。
RESULT_REDIRECT_ROUTES: dict[str, tuple[str, ...]] = {
    "baidu": ("/link",),  # http://www.baidu.com/link?url=<密文>
}


def _is_result_redirect(parsed: Any, engine: str) -> bool:
    """parsed 是否引擎的「结果中转路由」（纯结构判断，不解密）。"""
    route = RESULT_REDIRECT_ROUTES.get(engine)
    if route is None:
        return False
    path = parsed.path or ""
    return any(path == r or path.startswith(r + "/") for r in route) and (
        "url" in parse_qs(parsed.query or "")
    )


def _is_undecodable_redirect(url: str, engine: str) -> bool:
    """是否「密文中转」链接——中转路由上 url= 存在但值**不是**明文 http。

    判断依据是 routing shape（path + 参数键存在）+ url= 值的可解性，
    全是结构性事实。返回 True 意味着「目标 host 未知」——代码不给结论
    （不断言它/不是内容站），放行为候选由语义层与落地检测兜底，fail-closed
    管的是「结论」不是「采集」，见 CLAUDE.md I4。
    """
    try:
        parsed = urlparse(url)
    except ValueError:
        return False
    if not _is_result_redirect(parsed, engine):
        return False
    target = parse_qs(parsed.query or "").get("url")
    return bool(target) and not target[0].startswith(_BROWSABLE_SCHEMES)


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
    # 引擎「结果中转路由」在 host 类判定**之前**分流（05 方案 §2.2）：
    # 百度真实结果 host 仍是 www.baidu.com，先走 engine_self 会被误杀成
    # 「引擎自身页」。url= 值是明文 http → 维持 redirect_wrapper 原口径
    # （可解，后续由 extract_candidates 对解包后 URL 重新判定）；
    # 是密文（解不开）→ 目标 host 未知，放行——由感知层/落地检测兜底。
    if _is_result_redirect(parsed, engine):
        target = parse_qs(parsed.query or "").get("url")
        if target and target[0].startswith(_BROWSABLE_SCHEMES):
            return "redirect_wrapper"
        return ""
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


#: 每个引擎的「结果条目」选择器——用于把**结果标题**与 URL 对上。
#:
#: **为什么需要这一张表**：``links()`` 的 ``text`` 对搜索结果页往往不是标题。
#: 实测 bing 每条结果只给面包屑（``qq.com https://v.qq.com › cover``），
#: 32 条链接**没有一条**写着片名；而判断点 ① 的全部职责就是把 URL 与
#: 目标片名对上。没有这张表，① 拿到的输入根本不支持它那道题
#: （见 ``perception/llm_perceptor.py`` 的 ``_title_link_sufficiency``）。
#:
#: ``@href`` 是 obscura ``browser_extract`` 的属性语法，``[]`` 表示取全部
#: 匹配。两个字段**平行数组**，一一对应关系由本模块在长度相等时才认。
#:
#: ⚠️ **``[]`` 只在入参里，**返回的 JSON 键**不带它**——实测 obscura
#: ``0.2.4`` 对 ``{"titles[]": ..., "urls[]": ...}`` 回的是
#: ``{"titles": [...], "urls": [...]}``。按 ``titles[]`` 去读会永远读到
#: ``None``，于是 :func:`result_titles` 恒返回空字典，① 恒 fail-closed：
#: 症状是「选择器失效」，而选择器是好的。这条只能靠
#: ``executor/browser/probe_result_titles.py`` 取证。
#:
#: ⚠️ **选择器会随引擎改版失效**。失效时抽不到，返回空 → 退回
#: :func:`extract_candidates` 原有的面包屑路径 → ① 退回 fail-closed。
#: 换句话说，**这张表坏了只会让 ① 变哑，不会让它变错**——这正是要的。
RESULT_SELECTORS: dict[str, dict[str, str]] = {
    "bing": {"titles[]": "li.b_algo h2 a", "urls[]": "li.b_algo h2 a@href"},
    "baidu": {"titles[]": "div.result h3 a", "urls[]": "div.result h3 a@href"},
}

#: :func:`result_titles` 读的键名。**刻意不写 ``[]``**——见
#: :data:`RESULT_SELECTORS` 的注释：那不是排版，是上游的真实返回形状。
TITLE_FIELD = "titles"
URL_FIELD = "urls"


def result_titles(extracted: Mapping[str, Any]) -> dict[str, str]:
    """``{url: title}``。**只在两个数组等长时才认。**

    长度不等 = 上游只对其中一个字段有匹配（选择器漂移、引擎改版）。
    这时返回空字典而不是 ``zip``——``zip`` 会静默截短，
    配出来的「标题 ↔ URL」会是一条查无出处的假对应，
    而 ① 正准备拿它当**唯一判据**。

    同一个 URL 出现多次时保留**第一次**：同一 host 的多个入口在
    :func:`extract_candidates` 里本来也只留第一个，两边口径一致。
    """
    titles = extracted.get(TITLE_FIELD)
    urls = extracted.get(URL_FIELD)
    if not isinstance(titles, list) or not isinstance(urls, list):
        return {}
    if len(titles) != len(urls) or not urls:
        return {}
    out: dict[str, str] = {}
    for title, url in zip(titles, urls):
        t, u = str(title or "").strip(), str(url or "").strip()
        if t and u and u not in out:
            out[u] = t
    return out


def title_for(titles: Mapping[str, str] | None, href: str) -> str:
    """按 ``href`` 取结果标题，**取不到返回空串**（不猜）。

    ⚠️ **两个查找键必须一起试**：``titles`` 的键来自
    ``browser_extract`` 的 ``urls[]``，而百度结果页里它给的是**解包后**
    的目标 URL（``https://www.iqiyi.com/...``），而 ``links[].href`` 是
    **包裹形态**（``https://www.baidu.com/link?url=...``）。

    实测（2026-10-10 T001，baidu 抓到 8 条结果标题）：只按原始 href 查
    **一条都命不中**，于是 orchestrator 把标题写回 ``search_obs.links``
    静默失败——存档里 links 仍是导航栏，而判断点 ① **读的就是这份
    links**，于是它永远看不到片名、恒 fail-closed。症状是「选择器失效」，
    而选择器是好的：``result_titles`` 返回了 8 条，只是没人按对的键去取。

    这两处（写回 ``obs.links`` 与 :func:`extract_candidates` 取候选 text）
    **必须共用这一个函数**——口径一旦分叉，失败的是其中一处而另一处照常
    工作，没有任何报错。
    """
    if not titles:
        return ""
    return titles.get(href) or titles.get(_unwrap_redirect(href)) or ""


def extract_candidates(
    obs: Observation,
    *,
    engine: str = DEFAULT_ENGINE,
    limit: int = 20,
    stats: dict[str, int] | None = None,
    titles: Mapping[str, str] | None = None,
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
        # 密文中转包裹：url= 是密文、目标 host 未知（05 方案 §2.3）。
        # 若仍按 raw 的 host（www.baidu.com）去重，同批真实结果会
        # 互相撞车只剩 1 条——降级为**不去重**（按原始链接各自保留），
        # 由遍历期落地检测（判断点 ②）兜底。明文/裸链维持 host 去重。
        opaque = _is_undecodable_redirect(raw, engine)
        if not opaque and host in seen_hosts:
            bump("duplicate_host")
            continue
        if not opaque:
            seen_hosts.add(host)
        # 标题优先：结果标题才是判断点 ① 的判据，面包屑不是。
        # 取不到就留原样——**不猜**，让 ① 照旧 fail-closed。
        text = title_for(titles, link.href) or link.text
        out.append(
            Candidate(
                url=raw, text=text, rank=len(out) + 1,
                source_href=link.href, opaque_redirect=opaque,
            )
        )
        if len(out) >= limit:
            bump("over_limit")
            break
    return out


# 2026-10-16 方案 12 阶段 A1：``pin_candidates_first`` 与
# ``candidates_outside_window`` **整体删除**。
#
# 旧机制（05 方案的同源修复）：候选从全量 links（实测 111 条）提取后
# 散落在 index≥50，而感知层窗口只送前 ``LLM_LINKS``（20）条——模型看
# 不见候选，只能选窗口里的站内导航，又被 ``excluded_host:baidu.com``
# 剔出候选，于是「选中 ∩ 候选 = 空」。修复是把候选链接按 rank 钉到
# links 最前，再用 ``candidates_outside_window`` 重放感知层排序校验
# 「候选 ⊆ 窗口」。
#
# 消解（A1 分类任务）：分类的输入**就是候选列表**（orchestrator 构造
# 候选视图观察，按 ``PERCEPTION_LINK_WINDOW`` 分批），「模型看得到
# 全部候选」由构造保证——不再需要重排观察，也不再需要重放校验。
# 附带收益：``record.search_obs`` 保持引擎原序不动，存档的观察
# 忠实于页面而不是「钉过候选」的变形。
