"""训练态观察渲染——**代码做**的，不用 LLM。

P1 存的是**取证态**观察：正文全文、80 个交互元素、50 条链接。
喂给模型的那一份必须裁剪（全文进训练窗口会把信噪比压垮），于是有了
「同一份观察的两种形态」这条口径：

    P1（取证）→ :func:`render` → P2 训练态

六条硬规矩
----------
1. **裁剪必须留痕。** :func:`render` 返回的 ``limits`` 逐项写明裁了什么。
   没留痕的裁剪和没裁剪一样坏——读样本的人以为模型看到的是全量，
   于是把「模型漏掉了页面上有的信息」判成模型缺陷，而真相是 assembler
   裁掉了。

2. **上游的截断要透传，不能被本层的裁剪盖住。**
   P1 的 ``truncated``（dom 层 6000 字符预算用尽）与本层的裁剪是两件事，
   但模型看到的是同一个不完整的页面。只写自己的 ``limits`` 而不透传
   上游的 ``truncated``，就会造出「我裁了但页面本来是全的」这种错觉。
   所以 ``truncated`` 分 ``source`` 与 ``view`` 两个来源，都如实报。

3. **降级项原样带出。** P1 的 ``degraded`` 记录了本次采集没采到的项
   （links / interactive / counts…）。**没有它，「没采到」与「确实为空」
   在观察里完全一样**，模型会学着在证据缺失时编结论。这正是
   fail-closed 在训练侧的对应物——闸门不在数据侧，就在模型学到的行为里。

4. **本层永不补内容。** 观察里没有的，渲染后还是没有。
   渲染器一旦开始「顺手补上默认标题」「把空 body 写成（无正文）」，
   它就变成了另一个感知层，而感知层的输出必须能溯源到
   :class:`~trajectory_pipeline.perception.base.Observation`（不变式 I1）。
   空的就是空的——**空本身是信号**：模型要学会看到空证据时说找不到。

5. **导航噪声不进训练文本**（方案 23 R1-1）。P1 的正文/元素/链接里有大量
   与任务无关的噪声：热搜榜、ICP 备案号、页脚、广告、推荐流、站内导航。
   它们占掉回执的绝大部分字数，却对「这一步该点什么」零信息。
   **只在训练侧剔除**——P1 存档一个字不动（证据与语料是两件事）。

6. **动作目标必须保真**（方案 23 R1-2）。限量会裁掉元素/链接，而下一步
   动作的点击目标/网址可能正好在被裁的那一段。训练样本里出现「点了一个
   观察里没有的东西」等于在教模型编目标。所以 :func:`render` 接受
   ``keep``：**下一步动作的可读目标无条件保留**，排在限量的最前面。

   同理，``action_error``（P1 ``steps[i].error``）要在回执里显式标成
   「本步动作无效」——它与「采集降级」是两件事：一个是动作白做，
   一个是这块信息没采到。训练侧分不清就会学错归因。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Mapping, Sequence

from trajectory_pipeline.assembler.schema import BODY_KEY, LEGACY_BODY_KEY
from trajectory_pipeline.common.elements import (
    ELEMENT_VOCAB_HTML_TAG,
    element_keys,
    element_name,
    element_role,
    vocabulary_of_archive_elements,
)

#: 训练态的正文上限。取 320 是方案 23 R1-1 实测出来的（A5：单条回执中位数
#: ≤800；主树 camofox-1 的 4071 条可见步实测中位 **772**）：播放页的关键信息
#: （片名、集数、播放入口、简介）通常在首屏内；先把导航噪声剔掉，再按任务
#: 相关度取前 320 字。这个数字一旦改了，历史样本之间就不再可比——所以它
#: **进 ``limits``**，改数字会让差异可见，而不是悄悄生效。
VIEW_BODY_CHARS = 320
#: 元素与链接的上限。限量会裁掉动作目标，所以 :func:`render` 用 ``keep``
#: 把下一步动作的目标无条件保留（见模块 docstring 第 6 条）。
VIEW_ELEMENTS = 10
VIEW_LINKS = 4

#: **读步的窗口**（方案 23 R4-1）：读是模型主动要的证据，给得比普通回执多，
#: 但仍要裁到训练窗口能承受的量。普通步的 320/10/4 会把 ``snapshot`` 与
#: 普通回执压成同一个样子——读原语就白加了。三个数同样进 ``limits``。
VIEW_READ_BODY_CHARS = 2000
VIEW_READ_ELEMENTS = 50
VIEW_READ_LINKS = 50

#: 整行精确匹配就算噪声的站内导航词。**只认整行**——正文里出现「首页」
#: 两个字不等于这一行是导航。
NAV_LINE_WORDS = frozenset({
    "首页", "首页", "图片", "新闻", "视频", "贴吧", "知道", "文库", "地图",
    "更多", "登录", "注册", "登录/注册", "设置", "意见反馈", "帮助", "客服",
    "关于我们", "联系我们", "隐私政策", "用户协议", "版权声明", "友情链接",
    "广告服务", "招聘", "举报", "网站地图", "热搜", "排行榜", "上一页",
    "下一页", "末页", "展开", "收起", "返回顶部", "下载客户端", "关注",
    "收藏", "分享", "评论", "点赞", "发帖", "百度首页", "个人中心",
})

#: 页脚/备案/广告等噪声行。命中即整行丢弃——这些行在绝大多数页面上
#: 是模板，与任务无关；**误伤正文的风险由「任务相关行优先」兜住**
#: （含任务片名/播放关键词的行评分更高，先占预算）。
NOISE_LINE_RE = re.compile(
    r"ICP备|ICP证|网文|许可证|备案号|Copyright|©|版权所有|免责声明|友情链接|"
    r"合作伙伴|关注我们|扫码|下载APP|客户端|用户协议|隐私政策|意见反馈|"
    r"返回顶部|广告|推广|赞助|举报电话|举报邮箱"
)

#: 在线视频场景的「任务相关」信号词。命中即视为有用行，优先占正文预算。
TASK_KEYWORD_RE = re.compile(
    r"播放|在线观看|高清|正片|全集|主演|导演|类型|地区|年份|简介|剧情|片长|"
    r"语言|评分|免费|会员|登录|订阅|预告|蓝光|完整版|在线|观看|第\s*\d+\s*集|"
    r"HD|超清|片名|上映|演员|制片|国家|时长|集数|更新"
)

#: 热搜榜/排行编号行（``1 某条新闻``）。只在整行较短时丢弃——
#: 长行更可能是正文里的编号列表。
HOT_LIST_RE = re.compile(r"^\d{1,2}[.、\s]\s*\S")


@dataclass(frozen=True, slots=True)
class ViewLimits:
    """本层裁剪的**全部**事实。逐项列出，不给「大约」。

    ``None`` = 这一项没裁（可与「裁了 0 条」区分开）。
    ``body_lines_dropped`` / ``elements_dropped`` / ``links_dropped`` 记的是
    **噪声与去重**丢掉的条数；``body_chars`` / ``elements`` / ``links`` 记的是
    **上限触发**时的那个上限。
    """

    body_chars: int | None = None
    elements: int | None = None
    links: int | None = None
    body_lines_dropped: int | None = None
    elements_dropped: int | None = None
    links_dropped: int | None = None

    def to_json(self) -> dict[str, Any]:
        return {
            "body_chars": self.body_chars,
            "elements": self.elements,
            "links": self.links,
            "body_lines_dropped": self.body_lines_dropped,
            "elements_dropped": self.elements_dropped,
            "links_dropped": self.links_dropped,
            "cropped": bool(
                self.body_chars or self.elements or self.links
                or self.body_lines_dropped or self.elements_dropped
                or self.links_dropped
            ),
        }


def _pick_body(obs: Mapping[str, Any]) -> tuple[str, str]:
    """取正文原文，返回 ``(正文, 来源)``。

    降级来源标成 ``body_preview_only`` 而不是 ``body_preview``：
    后者读起来像个正常的来源名，扫一眼就过去了；前者才带得走
    「这份正文只有 400 字」这层警告。
    """
    body = obs.get(BODY_KEY)
    if isinstance(body, str) and body:
        return body, BODY_KEY
    legacy = obs.get(LEGACY_BODY_KEY)
    if isinstance(legacy, str) and legacy:
        return legacy, "body_preview_only"
    return "", "missing"


def _line_score(line: str, focus: Sequence[str], keep: Sequence[str]) -> int:
    """一行正文的**任务相关度**。分数只决定先后，不决定去留。

    - 4：下一步动作的目标（``keep``）——必须在预算里；
    - 3：任务片名等 ``focus`` 词；
    - 2：播放/影片元数据关键词；
    - 1：正常长度的内容行；
    - 0：短行（多半是按钮/占位）。
    """
    if any(k and k in line for k in keep):
        return 4
    if any(f and f in line for f in focus):
        return 3
    if TASK_KEYWORD_RE.search(line):
        return 2
    return 1 if len(line) >= 6 else 0


def _is_noise_line(line: str) -> bool:
    if line in NAV_LINE_WORDS:
        return True
    if NOISE_LINE_RE.search(line):
        return True
    return bool(HOT_LIST_RE.match(line) and len(line) < 40)


def _digest_body(
    body: str,
    *,
    focus: Sequence[str],
    keep: Sequence[str],
    limit: int = VIEW_BODY_CHARS,
) -> tuple[str, int, bool]:
    """正文 → 任务相关摘要。

    Returns:
        ``(摘要, 噪声/重复丢弃的行数, 是否触到字数上限)``。

    规则：先剔噪声行与重复行；保住 ``keep`` 行（无条件、全文）；
    其余按相关度排序填入剩余预算，最后**按原顺序**拼回去——
    打乱顺序会让「页面从上到下」这个结构信号消失。
    """
    if not body.strip():
        return "", 0, False
    raw_lines = [ln.strip() for ln in body.splitlines()]
    non_empty = [ln for ln in raw_lines if ln]
    kept: list[tuple[int, int, str]] = []
    seen: set[str] = set()
    for i, line in enumerate(raw_lines):
        if not line or line in seen or _is_noise_line(line):
            continue
        seen.add(line)
        kept.append((i, _line_score(line, focus, keep), line))

    dropped = len(non_empty) - len(kept)
    if not kept:
        return "", dropped, False

    must = [t for t in kept if t[1] >= 4]
    rest = sorted((t for t in kept if t[1] < 4), key=lambda t: (-t[1], t[0]))
    chosen: list[tuple[int, str]] = [(i, line) for i, _, line in must]
    used = sum(len(line) + 1 for _, line in chosen)
    truncated = False
    for i, _score, line in rest:
        remaining = limit - used
        if remaining <= 0:
            truncated = True
            break
        if len(line) + 1 > remaining:
            piece = line[:remaining]
            if piece:
                chosen.append((i, piece))
            truncated = True
            break
        chosen.append((i, line))
        used += len(line) + 1
    chosen.sort(key=lambda t: t[0])
    text = "\n".join(line for _, line in chosen)
    if len(text) > limit:
        truncated = True
    return text, dropped, truncated


def _select_elements(
    elements: Sequence[Mapping[str, Any]],
    *,
    vocabulary: str | None,
    keep: Sequence[str],
    limit: int = VIEW_ELEMENTS,
) -> tuple[list[Mapping[str, Any]], int, bool]:
    """元素表：去噪、去重、限量，**保住下一步动作的目标**。

    Returns:
        ``(入选元素, 噪声/去重丢弃数, 是否触到数量上限)``。
    """
    kind_key, text_key = element_keys(
        vocabulary or vocabulary_of_archive_elements(list(elements))
    )

    def matches(e: Mapping[str, Any]) -> bool:
        name = str(e.get(text_key) or "").strip()
        if not name:
            return False
        return any(k and (k == name or k in name) for k in keep)

    picked: list[Mapping[str, Any]] = []
    seen: set[tuple[str, str]] = set()

    def add(e: Mapping[str, Any]) -> bool:
        role = str(e.get(kind_key) or "")
        name = str(e.get(text_key) or "").strip()
        if not name:
            return False
        key = (role, name)
        if key in seen:
            return False
        seen.add(key)
        picked.append(e)
        return True

    # keep 目标先占位：即使它在导航词里、即使后面已经满了。
    for e in elements:
        if matches(e):
            add(e)
    for e in elements:
        if matches(e):
            continue
        role = str(e.get(kind_key) or "")
        name = str(e.get(text_key) or "").strip()
        if not name or name in NAV_LINE_WORDS or len(name) <= 1:
            continue
        add(e)
    # ``dropped`` 只数**噪声/去重**掉的那些；上限裁掉的用 ``capped`` 单独表达。
    dropped = max(len(elements) - len(picked), 0)
    return picked[:limit], dropped, len(picked) > limit


def _select_links(
    links: Sequence[Mapping[str, Any]],
    *,
    keep: Sequence[str],
    limit: int = VIEW_LINKS,
) -> tuple[list[Mapping[str, Any]], int, bool]:
    """链接表：去掉不可导航/噪声/重复项，限量，**保住下一步动作的网址**。

    Returns:
        ``(入选链接, 噪声/去重丢弃数, 是否触到数量上限)``。
    """
    def href_of(l: Mapping[str, Any]) -> str:
        return str(l.get("href") or "").strip()

    def text_of(l: Mapping[str, Any]) -> str:
        return str(l.get("text") or "").strip()

    def matches(l: Mapping[str, Any]) -> bool:
        href = href_of(l)
        return bool(href) and any(k and (k == href or k in href) for k in keep)

    picked: list[Mapping[str, Any]] = []
    seen: set[tuple[str, str]] = set()

    def add(l: Mapping[str, Any]) -> bool:
        href = href_of(l)
        if not href or href.startswith("javascript:") or href == "#":
            return False
        key = (text_of(l), href)
        if key in seen:
            return False
        seen.add(key)
        picked.append(l)
        return True

    for l in links:
        if matches(l):
            add(l)
    for l in links:
        if matches(l):
            continue
        text = text_of(l)
        if text in NAV_LINE_WORDS or NOISE_LINE_RE.search(text):
            continue
        add(l)
    dropped = max(len(links) - len(picked), 0)
    return picked[:limit], dropped, len(picked) > limit


def _empty_view() -> dict[str, Any]:
    return {
        "url": "", "page_title": "", "body_text": "",
        "body_source": "missing", "body_chars": 0,
        "truncated": {"source": False, "view": False},
        "degraded": ["observation_missing"],
        "interactive_elements": [], "links": [],
        "video_tag_count": 0, "iframe_count": 0,
        "action_error": "",
        "read": "",
        "limits": ViewLimits().to_json(),
    }


def render(
    obs: Mapping[str, Any] | None,
    *,
    focus: Sequence[str] = (),
    keep: Sequence[str] = (),
    action_error: str = "",
) -> dict[str, Any]:
    """渲染一条训练态观察。

    输出的键**只有一份契约**——P2/P3 都读它，训练视图渲染器
    （模块 5 的 ``views.py``）也在它之上做，不再各自裁一遍。

    Args:
        obs: P1 的取证态观察。
        focus: 任务相关词（片名等）。命中的正文行优先占预算。
        keep: **下一步动作的目标**（元素名 / 网址）。无条件保留，
            见模块 docstring 第 6 条。
        action_error: 本步动作的失败原因（P1 ``steps[i].error``）。
            **不混进 ``degraded``**：那是「信息没采到」，这是「动作白做」。
    """
    if not obs:
        out = _empty_view()
        out["action_error"] = str(action_error or "")
        return out

    #: 读标记（方案 23 R4-1）。空串 = 普通整页观察。
    read = str(obs.get("read") or "")

    # **读步只渲染被读的那一块**：``snapshot`` 只留正文、``interactive``
    # 只留元素表、``links`` 只留链接表。其余块在这条观察里本来就没采
    # （见 :func:`executor.agent_loop._read`），但万一上游多带了，也不能
    # 让它混进「我是按读的语义渲染的」这份视图——训练侧会把没读的块
    # 当成读过。窗口同时换成读步的宽口径。
    body_limit = VIEW_READ_BODY_CHARS if read == "snapshot" else VIEW_BODY_CHARS
    element_limit = (
        VIEW_READ_ELEMENTS if read == "interactive" else VIEW_ELEMENTS
    )
    link_limit = VIEW_READ_LINKS if read == "links" else VIEW_LINKS

    raw_body, body_source = _pick_body(obs)
    if read and read != "snapshot":
        body, body_lines_dropped, body_truncated = "", 0, False
        body_source = "missing"
    else:
        body, body_lines_dropped, body_truncated = _digest_body(
            raw_body, focus=focus, keep=keep, limit=body_limit
        )
    elements = (
        list(obs.get("interactive_elements") or ())
        if read in ("", "interactive") else []
    )
    # 键名跟着**存档的方言**走（P4）：老存档写 ``tag``/``label``，aria 面
    # （camoufox / camofox）批次写 ``role``/``name``。这一层拿不到 ``driver``，
    # 所以按存档自己的
    # 键名判方言（:func:`vocabulary_of_archive_elements`——键名本来就是
    # 这个轴的投影，读回来不是猜）。
    #
    # ⚠️ **不带 ``ref``**：``ref`` 是会话内句柄，进训练数据等于教模型背
    # 一个随机数，且样本永远不可回放（P2 契约 ``TestNoRefInActions``）。
    # 所以这里不用 :func:`element_json`（它会写 ref），只借它的键名。
    vocabulary = vocabulary_of_archive_elements(
        list(obs.get("interactive_elements") or ())
    )
    kind_key, text_key = element_keys(vocabulary)
    selected_elements, elements_dropped, elements_capped = _select_elements(
        elements, vocabulary=vocabulary, keep=keep, limit=element_limit
    )
    links = list(obs.get("links") or ()) if read in ("", "links") else []
    selected_links, links_dropped, links_capped = _select_links(
        links, keep=keep, limit=link_limit
    )
    # 「动作无效」与「采集降级」分列：前者是这一步白做，后者是这块没采到。
    limits = ViewLimits(
        body_chars=body_limit if body_truncated else None,
        elements=element_limit if elements_capped else None,
        links=link_limit if links_capped else None,
        body_lines_dropped=body_lines_dropped or None,
        elements_dropped=elements_dropped or None,
        links_dropped=links_dropped or None,
    )

    return {
        "url": str(obs.get("url") or ""),
        "page_title": str(obs.get("page_title") or ""),
        "body_text": body,
        #: 正文**实际来自哪里**。``body_preview_only`` 意味着这份正文
        #: 只有 400 字符——rationale 的实体核查必须知道这件事，
        #: 否则会把落在预览外的实体判成幻觉（那是没存，不是幻觉）。
        "body_source": body_source,
        "body_chars": len(body),
        #: 上游截断（dom 层预算用尽）与本层裁剪分列，合在一起才是
        #: 「模型看到的东西不完整」的完整描述。
        "truncated": {
            "source": bool(obs.get("truncated")),
            "view": bool(body_truncated or elements_capped or links_capped),
        },
        "degraded": list(obs.get("degraded") or ()),
        "interactive_elements": [
            {kind_key: element_role(e), text_key: element_name(e)}
            for e in selected_elements
        ],
        "links": [
            {"text": str(l.get("text") or ""), "href": str(l.get("href") or "")}
            for l in selected_links
        ],
        "video_tag_count": int(obs.get("video_tag_count") or 0),
        "iframe_count": int(obs.get("iframe_count") or 0),
        "action_error": str(action_error or ""),
        # 读标记原样带出：P3/T 的排版要靠它决定「只排版被读的块」。
        "read": read,
        "limits": limits.to_json(),
    }


def element_line(element: Any, vocabulary: str | None = None) -> str:
    """把一个元素渲染成提示词里的一行——**写法跟着方言走**（P4）。

        html_tag    ``  - <button> 播放``
        aria_role   ``  - [button] 播放``

    两种写法是**故意不同**的，不是审美选择：aria 面（camoufox / camofox）的
    kind 槽装的是
    ARIA role，若沿用尖括号，``<link>`` 就会被读成一个 HTML 标签——把
    「这列是标签名」这个错口径写进提示词，而提示词是模型学到的唯一口径。
    方括号标的是「这行说的是角色」，它同时是新旧样本**不混池**在提示词
    侧的那一半：光把存档键名改掉、提示词两边长一样，模型看不出差别。

    ``vocabulary=None`` 时按元素自己的键名推（存档字典的情形）；传
    :class:`~trajectory_pipeline.perception.base.InteractiveElement` 这类
    值对象时键名不在字典里，必须显式给方言。
    """
    if vocabulary is None:
        vocabulary = vocabulary_of_archive_elements([element])
    head = (
        f"<{element_role(element)}>"
        if vocabulary == ELEMENT_VOCAB_HTML_TAG
        else f"[{element_role(element)}]"
    )
    return f"  - {head} {element_name(element)}".rstrip()


def render_all(
    observations: Sequence[Mapping[str, Any]],
    *,
    focuses: Sequence[Sequence[str]] | None = None,
    keeps: Sequence[Sequence[str]] | None = None,
    action_errors: Sequence[str] | None = None,
) -> list[dict[str, Any]]:
    """批量渲染。输入是 :class:`~trajectory_pipeline.assembler.schema.Sample`
    的 ``observations`` 原样。

    ``focuses`` / ``keeps`` / ``action_errors`` 与 ``observations`` **按下标
    对齐**；缺省 = 这一条没有任务词 / 没有下一步动作 / 动作没报错。
    """
    out: list[dict[str, Any]] = []
    for i, obs in enumerate(observations):
        out.append(render(
            obs,
            focus=focuses[i] if focuses and i < len(focuses) else (),
            keep=keeps[i] if keeps and i < len(keeps) else (),
            action_error=(action_errors[i]
                          if action_errors and i < len(action_errors) else ""),
        ))
    return out


__all__ = [
    "NAV_LINE_WORDS",
    "NOISE_LINE_RE",
    "TASK_KEYWORD_RE",
    "VIEW_BODY_CHARS",
    "VIEW_ELEMENTS",
    "VIEW_LINKS",
    "VIEW_READ_BODY_CHARS",
    "VIEW_READ_ELEMENTS",
    "VIEW_READ_LINKS",
    "ViewLimits",
    "element_line",
    "render",
    "render_all",
]