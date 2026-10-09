"""DOM 预处理——**必需项，不是优化项**。

实测依据（``output/pipeline/probe_observe.json`` + baidu 现场取数）：

    ``browser_snapshot(max_chars=6000)`` 返回 6220 字符，其中**没有一个字是正文**。

拆开看：页面里第一个 ``<style>`` 块就有 **16675 字符**，
而 ``document.body.innerText``——也就是渲染后的真实正文——**只有 474 字符**。
obscura 的 ``max_chars`` 是在**截断前**算的，CSS 与正文抢同一个预算，
于是 6000 字符全被样式表吃掉，正文**根本没进得来**（返回值末尾的
``...(truncated, 256798 more chars)`` 就是证据）。

关键的一条实测反直觉：**换原语救不了**。``browser_markdown(max_chars=1200)``
同样返回 CSS（已验，存档 ``obscura_returns_baidu.json``）——markdown 只是
换了序列化格式，没有绕过 DOM 里的 ``<style>`` 节点。

所以正文走**两条路**，优先级不可颠倒：

1. **正解**——``browser_evaluate('document.body.innerText')``，浏览器已渲染的
   文本，不含任何 CSS。这是 :func:`parse_body_text` 的输入。
2. **兜底**——``browser_snapshot`` 的正文，经 :func:`clean_body` 剥离标签。
   内核对齐，是防御而不是主路径：LLM 真正读到的是 innerText。

本模块只做**与浏览器无关的纯文本处理**：不碰 obscura 的 tool 名与返回结构
（那是 ``obscura_driver.py`` 的事），因而可以脱离网络单测。

产出三样东西，缺一不可：

1. **URL / Title 分离**——``browser_snapshot`` 返回 ``URL:`` / ``Title:`` 前缀，
   而 ``URL:`` 行是代码读回「点击后真实落到哪」的唯一依据。
2. **噪声剥离**——``<style>`` / ``<script>`` 块 + 残余标签 + 空白压缩。
3. **截断与污染的显式记录**——正文被 ``max_chars`` 截断时，rationale 引用的实体
   可能落在截断外；闸门据此区分「编造」与「被截掉」。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Final

# ═══════════════════════════════════════════════════════════════════════
# 常量
# ═══════════════════════════════════════════════════════════════════════

#: 传给 browser_snapshot 的字符预算。设 6000：实测 CSS 可占页面文本的
#: 30~70%，预算太小会让正文被样式表挤光（内容判断失效），
#: 太大则白白吃掉 LLM 上下文。这是个待调参数，不是定论。
DEFAULT_MAX_CHARS = 6000

#: 长度达到预算的这个比例即判定「已截断」。obscura 不给截断标记，
#: 只能按长度推断——故取保守值，宁可误判为截断（闸门放宽）也不误判为完整
#: （闸门把被截掉的实体判成幻觉）。
TRUNCATION_RATIO = 0.95

_URL_RE = re.compile(r"^URL:[ \t]*(.+?)[ \t]*$", re.MULTILINE)
_TITLE_RE = re.compile(r"^Title:[ \t]*(.+?)[ \t]*$", re.MULTILINE)
#: ``browser_snapshot`` 在 ``Title:`` 之后、正文之前还有一行 ``Snapshot:`` 标记。
#: 早期版本只剥 URL / Title 两行，于是这行标记成了正文的第一行——
#: 正文空的时候 ``body_preview`` 就是 ``"Snapshot:"`` 这种纯噪声。
_SNAPSHOT_MARKER_RE = re.compile(r"^Snapshot:[ \t]*$", re.MULTILINE)
_TAG_RE = re.compile(r"<[^>]{0,4000}>")
_HSPACE_RE = re.compile(r"[ \t　]+")
_MULTINL_RE = re.compile(r"\n{3,}")
# 常见的控制字符（保留 \n，保留中文与 emoji）。
# 含 U+E000–U+F8FF 私有区：DOM 里的 PUA 字符**几乎全是图标字体**
# （实测 baidu 首页 label 里的  / ），对 LLM 是纯噪声，
# 而 JSON 转义后的 "\u{e6dc}" 字样还会误导它去找这个实体。
_CTRL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f\ue000-\uf8ff]")

#: 噪声块标签。**正则刻意不做成 ``<style>…</style>``**——
#: 实测 obscura 的 max_chars 在 CSS 块中间硬截断，闭合标签根本没回来
#: （baidu 首页：``</style>`` 计数为 0，16675 字符的样式块被砍在 6000 处），
#: 配对式正则必然失配，CSS 于是全裸进正文。见 :func:`_strip_raw_block`。
_RAW_BLOCK_OPEN_RE = re.compile(r"<(style|script)\b[^>]*>", re.IGNORECASE)
#: 配对齐全时的正常形态。单独一条是为了让 ``_strip_raw_block`` 先走快路径。
_PAIRED_BLOCK_RE = re.compile(
    r"<(style|script)\b[^>]*>.*?</\1\s*>", re.IGNORECASE | re.DOTALL
)


@dataclass(frozen=True, slots=True)
class CleanedSnapshot:
    """预处理后的快照。

    ``body_source`` 记录正文**实际来自哪里**——这是可审计字段而非实现细节：
    语义判断的结论（"这站有没有剧集"）完全建立在 body 上，而
    ``"snapshot"`` 意味着正文是被 CSS 抢过预算后的残骸，判定可信度更低。
    落进 P1 存档后，人工复核能一眼看出哪些结论建立在弱证据上。
    """

    url: str
    title: str
    body: str
    raw_len: int                # 预处理前总长度
    stripped_ratio: float       # 清洗后长度 / 原始长度；<0.5 说明页面很脏
    truncated: bool
    max_chars: int | None
    body_source: str = "snapshot"    # "inner_text" | "snapshot"


@dataclass(frozen=True, slots=True)
class CleanedLink:
    """``browser_links`` 的一个条目。"""

    text: str
    href: str


def parse_body_text(text: str, *, max_chars: int | None = None) -> str:
    """解析 ``browser_evaluate('document.body.innerText')`` 的返回。

    实测 obscura 返回的是**JSON 字符串**（不是裸文本），故先尝试
    ``json.loads`` 还原；失败则按裸文本处理——两种形态都真实出现过，
    取决于表达式是否命中字符串。

    **只做空白规范化，不剥标签**。这是刻意的：innerText 是浏览器渲染后的
    可见文本，里面本就没有 ``<style>`` / ``<div>``，再套一遍标签剥离只会
    误伤真实内容——实测 ``a < b && c > d`` 会被 ``_TAG_RE`` 吃成 ``a d``。
    「LLM 看到的就等于用户看到的」这条契约，比多洗一遍更值钱。
    """
    import json

    raw = text.strip()
    if raw.startswith('"'):
        try:
            decoded = json.loads(raw)
            if isinstance(decoded, str):
                raw = decoded
        except ValueError:
            raw = raw.strip('"')
    body = _normalize_whitespace(raw)
    if max_chars is not None and len(body) > max_chars:
        return body[:max_chars]
    return body


def parse_snapshot(
    text: str,
    *,
    max_chars: int | None = None,
    body: str | None = None,
) -> CleanedSnapshot:
    """解析并清洗 ``browser_snapshot`` 的返回。

    实测返回形如::

        URL: https://www.baidu.com/
        Title: 百度一下，你就知道

        <body text …>

    Args:
        body: **已从 innerText 取得的正文**，给了就优先用它。
            snapshot 的正文在 CSS 严重的页面上等于空（见模块 docstring），
            所以 driver 正常情况下总会传这个参数。

    找不到 ``URL:`` / ``Title:`` 行时返回空串——**不猜**。
    """
    raw_len = len(text)
    url_m = _URL_RE.search(text)
    title_m = _TITLE_RE.search(text)

    if body is not None:
        # innerText 路径：raw_len 记内文长度，截断按 innerText 自己的预算判断
        cleaned = body
        stripped_ratio = 1.0
        truncated = max_chars is not None and len(cleaned) >= max_chars
        source = "inner_text"
    else:
        raw_body = text
        for pattern in (_URL_RE, _TITLE_RE, _SNAPSHOT_MARKER_RE):
            m = pattern.search(raw_body)
            if m:
                raw_body = raw_body[: m.start()] + raw_body[m.end() :]
        cleaned = clean_body(raw_body)
        stripped_ratio = (len(cleaned) / raw_len) if raw_len else 1.0
        truncated = max_chars is not None and raw_len >= max_chars * TRUNCATION_RATIO
        source = "snapshot"

    return CleanedSnapshot(
        url=url_m.group(1) if url_m else "",
        title=title_m.group(1) if title_m else "",
        body=cleaned,
        raw_len=raw_len,
        stripped_ratio=stripped_ratio,
        truncated=truncated,
        max_chars=max_chars,
        body_source=source,
    )


def _strip_raw_block(text: str) -> str:
    """剥离 ``<style>`` / ``<script>`` 块——**含未闭合的情形**。

    先按配对标签剥一遍；剩下的开标签（说明闭合标签被 ``max_chars`` 砍掉了）
    再剥一遍，边界取**下一个 ``<``**——CSS 与 JS 的正文里几乎不会有裸 ``<``，
    而真实 HTML 结构处处都是，所以这个边界判据在实测样本上很稳。

    找不到下一个 ``<``（即噪声块一路顶到快照末尾）时，**整段到末尾全部剥掉**：
    宁可丢掉一点正文，也不能让 CSS 顶进来——CSS 顶进来是**静默失效**
    （LLM 看着满屏 ``font-size`` 照样能编出一份像模像样的 rationale），
    丢正文则会在 ``stripped_ratio`` 上留下可见痕迹。
    """
    out = _PAIRED_BLOCK_RE.sub(" ", text)
    while True:
        m = _RAW_BLOCK_OPEN_RE.search(out)
        if not m:
            return out
        nxt = out.find("<", m.end())
        out = out[: m.start()] + " " + (out[nxt:] if nxt != -1 else "")


def _normalize_whitespace(text: str) -> str:
    """只压空白。``clean_body`` 与 ``parse_body_text`` 共用的最后一步。"""
    text = _CTRL_RE.sub("", text)
    text = _HSPACE_RE.sub(" ", text)
    text = "\n".join(line.strip() for line in text.splitlines())
    return _MULTINL_RE.sub("\n\n", text).strip()


def clean_body(body: str) -> str:
    """剥离 style / script / 标签，压缩空白。

    纯函数，无副作用——离线单测的主要对象。
    仅用于 snapshot 的 HTML 兜底路径；正文主来源走 :func:`parse_body_text`。
    """
    text = _strip_raw_block(body)
    return _normalize_whitespace(_TAG_RE.sub(" ", text))


def parse_links(text: str) -> tuple[CleanedLink, ...]:
    """解析 ``browser_links`` 的 NDJSON。

    实测：每行一个 ``{"text":…,"href":…}``；空时返回哨兵文本 ``No links found.``。

    逐行解析而非整体解析——NDJSON 整体 ``json.loads`` 必然失败，
    且末行可能被 MCP 传输截断成半截 JSON（那种行**丢弃**，不猜）。
    """
    import json

    sentinel = text.strip()
    if not sentinel or sentinel.startswith("No links found"):
        return ()

    out: list[CleanedLink] = []
    for line in sentinel.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            item = json.loads(line)
        except ValueError:
            continue          # 残行：丢弃，不补全
        if not isinstance(item, dict):
            continue
        href = str(item.get("href") or "").strip()
        if not href:
            continue
        out.append(CleanedLink(text=str(item.get("text") or "").strip(), href=href))
    return tuple(out)


def _unquote_label(raw: str) -> str:
    """从 ``"登录" name="tj_login"`` 里取出 ``登录``。

    obscura 的 label 列是「JSON 风格字符串 + 可能跟着若干 HTML 属性」
    （实测 baidu 首页 **20/75** 的行带属性）。早期实现直接 ``strip('"')``，
    于是 ``"登录" name="tj_login"`` 变成 ``登录" name="tj_login``——
    LLM 拿到的标签里混着 DOM 属性，判断点 ③ 会被带偏。

    闭引号按 JSON 规则找（跳过 ``\\"`` 转义），取到的部分再做一次
    ``json.loads`` 还原转义；失败则原样返回，绝不猜。
    """
    import json

    text = raw.strip()
    if not text.startswith('"'):
        return text
    for i in range(1, len(text)):
        ch = text[i]
        if ch == '"' and text[i - 1] != "\\":
            try:
                decoded = json.loads(text[: i + 1])
            except ValueError:
                return text[1:i]
            return decoded if isinstance(decoded, str) else text[1:i]
    # 没有闭引号：传输截断，原样保留（宁可脏，不丢内容）
    return text.strip('"')


def parse_interactive_elements(text: str) -> tuple[tuple[str, str, str], ...]:
    """解析 ``browser_interactive_elements`` 的列式文本。

    实测每行形如（**列间是多个空格**，text 本身可能含空格与转义引号）::

        ref=e1    a                      "Learn more"
        ref=e4    a                      "设置" name="tj_settingicon"
        ref=e2    textarea               "<style data-for=\\"result\\" …>"

    返回 ``(ref, tag, label)`` 三元组。

    两处实测修正：
      - label 外层引号按配对提取，后面的 HTML 属性**不要**（27% 的行带属性）
      - label 里会混进 ``<style>`` 的 CSS（textarea 的 value 就是样式表，
        实测 baidu 首页 6/75）——走 :func:`_strip_raw_block` 剥掉，
        但**不剥普通标签**：``"第 <3 集>"`` 里的 ``<`` 是真实内容

    空页面的哨兵 ``No interactive elements on this page.`` 返回空元组。
    行格式不符时**丢弃该行**——宁缺勿滥：错误的 ref 会让代码点错元素。
    """
    stripped = text.strip()
    if not stripped or stripped.startswith("No interactive elements"):
        return ()

    out: list[tuple[str, str, str]] = []
    for line in stripped.splitlines():
        line = line.strip()
        if not line.startswith("ref="):
            continue
        parts = line.split(None, 2)      # ['ref=e1', 'a', '"Learn more"']
        if len(parts) < 2:
            continue
        ref_field = parts[0]
        ref = ref_field[len("ref=") :].strip()
        tag = parts[1].strip()
        if not ref or not tag:
            continue
        label = _unquote_label(parts[2]) if len(parts) > 2 else ""
        out.append((ref, tag, _strip_raw_block(str(label)).strip()))
    return tuple(out)


def parse_count(text: str) -> int:
    """解析 ``browser_count``。

    实测返回**JSON 数字**（``70``），不是文本。解析失败返回 0——
    计数只用于存在性探测，0 与「探测失败」在此语义下不冲突
    （真要去区分，须看该站的 ``pollute_ratio`` 与调用日志）。
    """
    raw = text.strip()
    if not raw:
        return 0
    try:
        return max(0, int(raw))
    except ValueError:
        pass
    try:
        value = float(raw)
    except ValueError:
        return 0
    return max(0, int(value))


def looks_like_error(text: str, is_error: bool) -> bool:
    """判断 tool 返回是否为错误。

    实测 obscura 的错误有两种形态：
      - ``isError=True`` 且正文形如 ``Error: Network error: …``
      - 某些 tool 在未报错时也返回 ``Error: Element not found: a``（点不到元素）
    所以**不能只看 is_error**——``Element not found`` 是业务事实（该元素不存在），
    应转成 ``answer=None`` 走 unresolved，而不是整个站点作废。
    """
    if is_error:
        return True
    return text.strip().startswith("Error:")


# ═══════════════════════════════════════════════════════════════════════
# 反爬拦截识别
# ═══════════════════════════════════════════════════════════════════════
#
# 为什么要单独认出来
# ------------------
# 搜索引擎对连发请求会返回验证码页。实测：同批连跑 3 个 task，
# 第 1 个正常，第 2、3 个被重定向到
# ``wappass.baidu.com/static/captcha/tuxing_v2.html``（标题「百度安全验证」）。
#
# 这类页面的表现是「正文极短 + 零链接」，而当前控制流把它记成
# 「搜索页未提取到候选」——**措辞指向「这批素材没价值」，
# 真相却是「基础设施被拦了」**。二者处置完全不同：
# 前者该换素材，后者该加间隔/换引擎。
#
# 混在一起的代价是**静默的**：跑一批 20 条，被拦掉 6 条，
# 报表上表现为「这批搜索页质量差」，没人会去查反爬。
#
# 只看 URL 与标题，不看正文
# -------------------------
# 百度验证码页正文是「网络不给力，请稍后重试」——**那句话字面是网络错误**，
# 在验证码页上只是模板文案。若把它当判据，真网络故障就会被误判成反爬，
# 正好与本模块的目的相反。URL 路径（``/captcha``、``wappass``）与
# 页面标题（「安全验证」）才是无歧义的强信号。

#: URL 路径里的反爬特征。**全部是子路径标记**，不匹配裸域名。
_BLOCK_URL_MARKERS: Final = (
    "/captcha", "wappass", "antispider", "secverify", "/verifycode",
    "/challenge", "/sorry/index",
)

#: 标题里的反爬特征。
_BLOCK_TITLE_MARKERS: Final = (
    "安全验证", "验证码", "访问验证", "百度安全", "人机验证",
    "robot check", "just a moment", "attention required", "access denied",
)

#: 正文里的**限流**特征。只认「频繁」类措辞——它们在正文里是明确的限流信号，
#: 且与网络故障的措辞（「网络不给力」「连接超时」）不重叠。
_BLOCK_BODY_MARKERS: Final = (
    "访问过于频繁", "请求过于频繁", "操作过于频繁",
    "too many requests", "unusual traffic", "rate limit",
)


def detect_block(url: str, title: str, body: str = "") -> str:
    """这个页面是不是被反爬/限流拦了。返回原因串，空串 = 没被拦。

    纯字符串事实判定，**不做语义猜测**——URL 路径与页面标题都是
    已经观察到的客观字段，符合「代码管事实层」的分工。

    优先级 ``URL > 标题 > 正文``：越靠前越无歧义。
    """
    lowered = (url or "").lower()
    if any(marker in lowered for marker in _BLOCK_URL_MARKERS):
        return "captcha"
    head = (title or "").lower()
    if any(marker.lower() in head for marker in _BLOCK_TITLE_MARKERS):
        return "captcha"
    text = (body or "").lower()
    if any(marker.lower() in text for marker in _BLOCK_BODY_MARKERS):
        return "rate_limit"
    return ""