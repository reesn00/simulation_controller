"""训练态观察渲染——**代码做**的，不用 LLM。

P1 存的是**取证态**观察：正文全文、80 个交互元素、200 条链接。
喂给模型的那一份必须裁剪（全文进训练窗口会把信噪比压垮），于是有了
「同一份观察的两种形态」这条口径：

    P1（取证）→ :func:`render` → P2 训练态

七条硬规矩
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

   元素/链接的限量也**按任务相关度取，不按页面顺序截前 N 条**（2026-11-10
   owner 指出：搜索页的 10 元素窗口里一条搜索结果都没有）。搜索页的
   前 50 条链接几乎全是站内导航、相关搜索与热搜，按页面顺序截前 4 条
   等于把结果区整段扔掉。非 keep 项按「片名+播放词 > 片名 > 播放词 >
   其他」排序取前 N，同分保持页面顺序；**入选后仍按页面顺序输出**——
   页面从上到下这个结构信号不丢。

6. **动作目标必须保真**（方案 23 R1-2）。限量会裁掉元素/链接，而下一步
   动作的点击目标/网址可能正好在被裁的那一段。训练样本里出现「点了一个
   观察里没有的东西」等于在教模型编目标。所以 :func:`render` 接受
   ``keep``：**下一步动作的可读目标无条件保留**，排在限量的最前面。

   同理，``action_error``（P1 ``steps[i].error``）要在回执里显式标成
   「本步动作无效」——它与「采集降级」是两件事：一个是动作白做，
   一个是这块信息没采到。训练侧分不清就会学错归因。

7. **精炼是「换一种裁法」，不是「少裁一点」**（方案 25）。本模块**仍然
   不调 LLM**——装配层是纯代码（``docs/01-架构总览.md`` 模块 5）。它只
   接受一份**旁挂**（:mod:`trajectory_pipeline.common.refine_sidecar`：
   ``{正文内容哈希: 保留的行}``），按那几行**从 P1 原文重建**正文。

   三条硬保证：

   - **只可能漏，不可能造**：重建的每一行都逐字来自原文。模型加不了
     一个原文没有的词——这是「不让 LLM 写摘要」的全部理由。
   - **对不上就回退**：保留的行不在原文里 ⇒ 整条精炼作废，走规则裁剪
     （与今天逐字一致，零回归）。
   - **预算仍归规则层**：模型只决定「哪些行相关」，多少字放得下仍由
     本模块的硬顶决定（动作目标照旧无条件保留）。

   精炼的结果**必须留痕**（``body_refined`` / ``body_refined_lines`` /
   ``body_refined_dropped``）——不说破的话，读样本的人会把「精简过的
   正文」当成「页面就这么多内容」。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping, Sequence

from trajectory_pipeline.assembler.schema import BODY_KEY, LEGACY_BODY_KEY
from trajectory_pipeline.common import body_lines, refine_sidecar
from trajectory_pipeline.common.body_lines import (
    HOT_LIST_RE,
    NAV_LINE_WORDS,
    NOISE_LINE_RE,
    TASK_KEYWORD_RE,
)
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

#: 噪声规则与相关度打分**搬到地基**（:mod:`trajectory_pipeline.common.
#: body_lines`）——LLM 精炼（:mod:`trajectory_pipeline.refine`）要用**同一份**
#: 规则构造候选行。两份实现漂了不报错，只是「给模型看的候选集」与
#: 「渲染时剔掉的集」对不上，精炼结果里会多出本该剔掉的行。
#:
#: 上面四个名字**继续从本模块导出**（历史 import 路径不变），
#: 值全部来自地基那份。


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

    ⚠️ 实现搬到地基（:func:`trajectory_pipeline.common.body_lines.
    line_score`）——LLM 精炼用**同一份**打分口径。
    """
    return body_lines.line_score(line, focus, keep)


def _is_noise_line(line: str) -> bool:
    """这一行是不是导航/页脚/广告噪声（实现见地基）。"""
    return body_lines.is_noise_line(line)


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

    ⚠️ 候选行的构造（剔噪 + 去重）搬到地基
    （:func:`trajectory_pipeline.common.body_lines.candidate_lines`），
    **行为与搬迁前逐字一致**——搬迁只是换个地方住。
    """
    if not body.strip():
        return "", 0, False
    raw_lines = [ln.strip() for ln in body.splitlines()]
    non_empty = [ln for ln in raw_lines if ln]
    candidates = body_lines.candidate_lines(body)
    dropped = len(non_empty) - len(candidates)
    if not candidates:
        return "", dropped, False

    kept: list[tuple[int, int, str]] = [
        (i, _line_score(line, focus, keep), line)
        for i, line in enumerate(candidates)
    ]
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


def _refined_pick(
    raw_body: str,
    refined: Mapping[str, Sequence[str]] | None,
) -> tuple[str, ...]:
    """按**正文内容哈希**在精炼索引里找这一条观察的保留行。

    找不到 = 这一条没精炼过（或精炼的是**另一份**正文）⇒ 空元组 ⇒
    走规则裁剪。

    ⚠️ **不猜、不近似匹配**：正文不一样就是不一样。内容哈希是唯一的
    定位方式（见 :mod:`trajectory_pipeline.common.refine_sidecar` 的
    「键怎么选」——按步号索引要在 P2/T 两套坐标系之间维护映射）。
    """
    if not refined or not raw_body:
        return ()
    picked = refined.get(refine_sidecar.body_key(raw_body))
    if not picked:
        return ()
    return tuple(str(x) for x in picked)


def _rebuild_from_refined(
    raw_body: str,
    picked: Sequence[str],
    *,
    focus: Sequence[str],
    keep: Sequence[str],
    limit: int,
) -> tuple[str, int, int, bool] | None:
    """用模型选中的行**重建**正文。

    Returns:
        ``(正文, 保留行数, 丢掉行数, 是否触到字数上限)``；
        ``None`` = 精炼不可用 ⇒ 调用方**回退到规则裁剪**。

    三道关（任一不过就整条作废，**不「差不多地」用一半**）
    ------------------------------------------------------
    1. **逐行核对**：保留的每一行都必须**逐字**出现在原文里。
       对不上 ⇒ 旁挂与正文不同源（改了 P1？换了正文？行号串了？）⇒
       宁可回退规则裁剪，也不要把一份对不上的正文写进训练集。
    2. **按原顺序重建**：模型可能乱序报行号；最终文本按**页面顺序**排
       ——「从上到下」这个结构信号不能因为模型排版乱了就丢。
    3. **硬预算**：重建后仍走一遍 :func:`_digest_body` 的填预算逻辑
       （动作目标无条件保留，其余按相关度）。模型只决定「哪些行相关」，
       **字数预算仍归规则层管**——两层分工不重叠。

    ⚠️ **保真口径不放宽**：模型选的行里没有「下一步动作目标」那一行时，
    渲染层**自己把目标行从原文补回来**——换了裁法不等于放松第 6 条
    （「训练样本里不能出现『点了一个观察里没有的东西』」）。
    正常流程下精炼层会先整条拦掉这种选择，但**渲染层是最后一道防线**，
    不能靠上游守。

    ⚠️ 这一层**只可能漏，不可能造**：最终文本的每一行都来自
    ``raw_body``，模型加不了一个原文没有的词。
    """
    positions: dict[str, int] = {}
    for i, raw in enumerate((raw_body or "").splitlines()):
        line = raw.strip()
        if line and line not in positions:
            positions[line] = i
    ordered: list[str] = []
    seen: set[str] = set()
    for line in picked:
        line = str(line).strip()
        if not line or line in seen:
            continue
        if line not in positions:
            return None                      # 第 1 关：逐字核对不过
        seen.add(line)
        ordered.append(line)
    if not ordered:
        return None
    # 第 3 关的前置：**动作目标无条件保留**——模型选的行里没有目标那一行
    # 时（正常流程下精炼层会整条拦掉，但渲染层**不能因此放松**第 6 条
    # 保真口径），渲染层**自己把目标行补回来**。
    # 取的是**候选行**里的 keep 命中项（与规则路径同一份口径：
    # 规则路径里 keep 行也是从候选行里挑出来、无条件入选的）。
    for line in body_lines.candidate_lines(raw_body):
        if line in seen:
            continue
        if body_lines.line_score(line, focus, keep) >= 4:
            seen.add(line)
            ordered.append(line)
    ordered.sort(key=lambda line: positions[line])
    text, _noise, truncated = _digest_body(
        "\n".join(ordered), focus=focus, keep=keep, limit=limit,
    )
    kept_lines = len(text.splitlines()) if text else 0
    total = len(body_lines.candidate_lines(raw_body))
    return text, kept_lines, max(total - kept_lines, 0), truncated


def _lcs_len(a: str, b: str) -> int:
    """最长公共子串长度（只算连续片段）。"""
    if not a or not b:
        return 0
    prev = [0] * (len(b) + 1)
    best = 0
    for ca in a:
        cur = [0] * (len(b) + 1)
        for j, cb in enumerate(b, 1):
            if ca == cb:
                cur[j] = prev[j - 1] + 1
                if cur[j] > best:
                    best = cur[j]
        prev = cur
    return best


def _focus_hit(text: str, focus: Sequence[str]) -> bool:
    """任务词命中：精确子串，或**最长公共子串 ≥ 3**。

    任务标题不总是结果条目里的连续子串——``戛纳最佳短片2025`` 在结果里
    被拆成 ``2025争议最大的戛纳爆款…``。只认整串会把它漏光；而 2 字命中
    （``最佳``/``短片``）又太松。3 是两边都站得住的折中：年份或片名主干
    至少 3 个字符。
    """
    return any(
        f and (f in text or _lcs_len(f, text) >= 3) for f in focus
    )


def _item_score(text: str, *, focus: Sequence[str]) -> int:
    """元素/链接文本的**任务相关度**。分数只决定谁先入选，不决定展示顺序。

    ``keep`` 命中的项不走这里（它们另行钉死），所以这里只排「其余候选」：

    - 3：片名 + 播放关键词同时命中——搜索页的真实结果条目就长这样
      （``《让子弹飞》电影全集高清免费完整版观看_可乐电影网``）；
    - 2：只命中片名——「相关搜索」条目、贴吧名这类，比结果条目弱一档，
      但远强于热搜榜；
    - 1：只命中播放关键词——播放页的控件；
    - 0：都没有。
    """
    score = 0
    if _focus_hit(text, focus):
        score += 2
    if TASK_KEYWORD_RE.search(text):
        score += 1
    return score


def _select_elements(
    elements: Sequence[Mapping[str, Any]],
    *,
    vocabulary: str | None,
    focus: Sequence[str] = (),
    keep: Sequence[str],
    limit: int = VIEW_ELEMENTS,
) -> tuple[list[Mapping[str, Any]], int, bool]:
    """元素表：去噪、去重、限量，**保住下一步动作的目标**。

    非 keep 项按 :func:`_item_score` 取前 N，同分保持页面顺序；
    入选后仍按页面顺序输出（keep 项在最前，见模块 docstring 第 6 条）。

    Returns:
        ``(入选元素, 噪声/去重丢弃数, 是否触到数量上限)``。
    """
    kind_key, text_key = element_keys(
        vocabulary or vocabulary_of_archive_elements(list(elements))
    )

    def name_of(e: Mapping[str, Any]) -> str:
        return str(e.get(text_key) or "").strip()

    def matches(e: Mapping[str, Any]) -> bool:
        name = name_of(e)
        if not name:
            return False
        return any(k and (k == name or k in name) for k in keep)

    pinned: list[Mapping[str, Any]] = []
    seen: set[tuple[str, str]] = set()

    def add(e: Mapping[str, Any]) -> bool:
        role = str(e.get(kind_key) or "")
        name = name_of(e)
        if not name:
            return False
        key = (role, name)
        if key in seen:
            return False
        seen.add(key)
        pinned.append(e)
        return True

    # keep 目标先占位：即使它在导航词里、即使后面已经满了。
    for e in elements:
        if matches(e):
            add(e)

    rest: list[tuple[int, int, Mapping[str, Any]]] = []
    for idx, e in enumerate(elements):
        if matches(e):
            continue
        role = str(e.get(kind_key) or "")
        name = name_of(e)
        if not name or name in NAV_LINE_WORDS or len(name) <= 1:
            continue
        key = (role, name)
        if key in seen:
            continue
        seen.add(key)
        rest.append((idx, _item_score(name, focus=focus), e))

    chosen = sorted(rest, key=lambda t: (-t[1], t[0]))[:max(limit - len(pinned), 0)]
    chosen.sort(key=lambda t: t[0])
    selected = pinned[:limit] + [e for _, _, e in chosen]
    # ``dropped`` 只数**噪声/去重**掉的那些；上限裁掉的用 ``capped`` 单独表达。
    dropped = max(len(elements) - (len(pinned) + len(rest)), 0)
    return selected, dropped, len(pinned) + len(rest) > limit


def _select_links(
    links: Sequence[Mapping[str, Any]],
    *,
    focus: Sequence[str] = (),
    keep: Sequence[str],
    limit: int = VIEW_LINKS,
) -> tuple[list[Mapping[str, Any]], int, bool]:
    """链接表：去掉不可导航/噪声/重复项，限量，**保住下一步动作的网址**。

    非 keep 项按 :func:`_item_score` 取前 N，同分保持页面顺序——
    搜索页的前 50 条链接全是站内导航/相关搜索/热搜，按页面顺序截前 4
    会把结果区整段扔掉（2026-11-10 owner 报的「搜索页信息没记录」）。

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

    pinned: list[Mapping[str, Any]] = []
    seen: set[tuple[str, str]] = set()

    def add(l: Mapping[str, Any]) -> bool:
        href = href_of(l)
        if not href or href.startswith("javascript:") or href == "#":
            return False
        key = (text_of(l), href)
        if key in seen:
            return False
        seen.add(key)
        pinned.append(l)
        return True

    for l in links:
        if matches(l):
            add(l)

    rest: list[tuple[int, int, Mapping[str, Any]]] = []
    for idx, l in enumerate(links):
        if matches(l):
            continue
        href = href_of(l)
        if not href or href.startswith("javascript:") or href == "#":
            continue
        text = text_of(l)
        if text in NAV_LINE_WORDS or NOISE_LINE_RE.search(text):
            continue
        key = (text, href)
        if key in seen:
            continue
        seen.add(key)
        rest.append((idx, _item_score(text, focus=focus), l))

    chosen = sorted(rest, key=lambda t: (-t[1], t[0]))[:max(limit - len(pinned), 0)]
    chosen.sort(key=lambda t: t[0])
    selected = pinned[:limit] + [l for _, _, l in chosen]
    dropped = max(len(links) - (len(pinned) + len(rest)), 0)
    return selected, dropped, len(pinned) + len(rest) > limit


def _empty_view() -> dict[str, Any]:
    return {
        "url": "", "page_title": "", "body_text": "",
        "body_source": "missing", "body_chars": 0,
        "body_refined": False, "body_refined_lines": 0,
        "body_refined_dropped": 0,
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
    refined: Mapping[str, Sequence[str]] | None = None,
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
        refined: **精炼索引**（``{正文内容哈希: 保留的行}``，见
            :mod:`trajectory_pipeline.common.refine_sidecar`）。有它就用
            模型选出的行重建正文，没有就按规则裁剪。

            ⚠️ **精炼是「换一种裁法」，不是「少裁一点」**：它与规则裁剪
            走**同一个出口**（本函数），所以两者的保真口径（动作目标
            必须留下、只可能漏不可能造）完全一致。
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
    #: **精炼留痕**（方案 25）。三个字段**不进 ``limits`` / ``truncated``**——
    #: 那两个是「裁了多少」的账，精炼是「换了一种裁法」，混进去会破掉
    #: ``limits`` 的 7 键契约，也会让 ``gates._truncated_flag`` 的语义
    #: （上游截断 vs 本层触顶）变模糊。
    body_refined = False
    body_refined_lines = 0
    body_refined_dropped = 0
    if read and read != "snapshot":
        body, body_lines_dropped, body_truncated = "", 0, False
        body_source = "missing"
    else:
        picked = _refined_pick(raw_body, refined)
        rebuilt = (
            _rebuild_from_refined(
                raw_body, picked, focus=focus, keep=keep, limit=body_limit,
            )
            if picked else None
        )
        if rebuilt is None:
            # 没有精炼 / 精炼对不上 ⇒ **回退规则裁剪**（与今天逐字一致）。
            body, body_lines_dropped, body_truncated = _digest_body(
                raw_body, focus=focus, keep=keep, limit=body_limit
            )
        else:
            body, body_refined_lines, body_refined_dropped, body_truncated = rebuilt
            # 噪声/去重丢的行数照旧记（**另一笔账**，见
            # :func:`trajectory_pipeline.common.body_lines.noise_dropped`）。
            body_lines_dropped = body_lines.noise_dropped(raw_body)
            body_refined = True
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
        elements, vocabulary=vocabulary, focus=focus, keep=keep,
        limit=element_limit,
    )
    links = list(obs.get("links") or ()) if read in ("", "links") else []
    selected_links, links_dropped, links_capped = _select_links(
        links, focus=focus, keep=keep, limit=link_limit
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
        #: **精炼留痕**（2026-11-13 · 方案 25）：正文是模型选行裁出来的，
        #: 不是「页面前 N 字」。``body_refined_lines`` = 最终正文里
        #: 留下的行数，``body_refined_dropped`` = 原正文候选行里
        #: **没进最终正文**的行数（模型丢的 + 硬预算裁的）。
        #: 不说破的话，读样本的人会以为「模型看到的就是这些」。
        "body_refined": body_refined,
        "body_refined_lines": body_refined_lines,
        "body_refined_dropped": body_refined_dropped,
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
    refined: Mapping[str, Sequence[str]] | None = None,
) -> list[dict[str, Any]]:
    """批量渲染。输入是 :class:`~trajectory_pipeline.assembler.schema.Sample`
    的 ``observations`` 原样。

    ``focuses`` / ``keeps`` / ``action_errors`` 与 ``observations`` **按下标
    对齐**；缺省 = 这一条没有任务词 / 没有下一步动作 / 动作没报错。

    ``refined`` 是**一份精炼索引给全部观察共用**（按正文内容哈希定位，
    不按步号）——所以这里不用对齐下标。
    """
    out: list[dict[str, Any]] = []
    for i, obs in enumerate(observations):
        out.append(render(
            obs,
            focus=focuses[i] if focuses and i < len(focuses) else (),
            keep=keeps[i] if keeps and i < len(keeps) else (),
            action_error=(action_errors[i]
                          if action_errors and i < len(action_errors) else ""),
            refined=refined,
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