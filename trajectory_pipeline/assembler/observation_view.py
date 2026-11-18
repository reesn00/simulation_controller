"""训练态观察渲染——**代码做**的，不用 LLM。

P1 存的是**取证态**观察：正文全文、200 个交互元素、200 条链接。
喂给模型的那一份必须裁剪（全文进训练窗口会把信噪比压垮），于是有了
「同一份观察的两种形态」这条口径：

    P1（取证）→ :func:`render` → P2 训练态

八条硬规矩
----------

1. **裁剪必须留痕。** :func:`render` 返回的 ``limits`` 逐项写明裁了什么。
   没留痕的裁剪和没裁剪一样坏——读样本的人以为模型看到的是全量，
   于是把「模型漏掉了页面上有的信息」判成模型缺陷，而真相是 assembler
   裁掉了。

2. **上游的截断要透传，不能被本层的裁剪盖住。**
   P1 的 ``truncated``（dom 层字符预算用尽，H-41 阶段 1 抬到 200000）与
   本层的裁剪是两件事，但模型看到的是同一个不完整的页面。只写自己的
   ``limits`` 而不透传上游的 ``truncated``，就会造出「我裁了但页面本来是
   全的」这种错觉。所以 ``truncated`` 分 ``source`` 与 ``view`` 两个来源，
   都如实报。

   ⚠️ **两支各有各的意思**（H-41 阶段 2 · 方案 26 §6.3 #9）：
   ``source`` = **上游采集侧**截断（取证态就不全）；``view`` = **本层
   训练态渲染**裁剪。**责任方不同**——上游截断要去改采集预算，本层裁剪
   是既定口径（``capped``）或模型的选择（``semantic``）。混成一句
   「被截断」，读的人会**去改错的东西**。

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
   ``{候选集哈希: 一条记录}``），按那几行**从 P1 原文重建**正文。

   三条硬保证：

   - **只可能漏，不可能造**：重建的每一行都逐字来自原文。模型加不了
     一个原文没有的词——这是「不让 LLM 写摘要」的全部理由。
   - **对不上就回退**：保留的行不在原文里 ⇒ 整条精炼作废，走规则裁剪
     （与今天逐字一致，零回归）。
   - **预算仍归规则层**（**仅限 ``capped`` 模式**）：模型只决定「哪些行
     相关」，多少字放得下仍由本模块的硬顶决定（动作目标照旧无条件保留）。
     ⇒ **``semantic`` 模式取消这条**（见第 8 条）。

   精炼的结果**必须留痕**（``body_refined`` / ``body_refined_lines`` /
   ``body_refined_dropped``）——不说破的话，读样本的人会把「精简过的
   正文」当成「页面就这么多内容」。

8. **两种渲染模式**（H-41 阶段 2 · 方案 26 §6）。

   - ``capped``（默认，:data:`VIEW_MODE_CAPPED`）：**规则层**决定留多少，
     触顶即**截断**。这条路径**一字不改**——它是语义模式的**回退路径**。
   - ``semantic``（:data:`VIEW_MODE_SEMANTIC`）：**模型**决定留哪些，
     剩下的**整体留下**，**一个字都不截**（正文 / 元素 / 链接三段都由
     模型选，渲染层只做**逐字核对重建**）。

   ⚠️ 换的**不是**「保不保真」，换的是「留多少由谁决定」：两种模式的
   保真口径**完全一致**（动作目标必须留下、只可能漏不可能造）。

   ⚠️ 语义模式**不可用时整条回退** ``capped``（精炼没跑 / 旁挂是旧协议 /
   模型选的东西对不上原文）⇒ 输出与**没精炼时逐字一致**（零回归），
   并留痕 ``semantic_fallback``。

   ⚠️ 语义模式下 ``truncated.view`` **恒 False**——本层**一个字都没截**。
   下游（D-3 / D-9 / D-10）看到 ``truncated`` 判真就把整批判成
   「不可判定」，而语义模式的正文**本来就是模型挑过的**，不是「被截的」。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping, Sequence

from trajectory_pipeline.assembler.schema import BODY_KEY, LEGACY_BODY_KEY
from trajectory_pipeline.common import body_lines, refine_sidecar, view_candidates
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

# ── 渲染模式 ────────────────────────────────────────────────────────

#: **规则裁剪**（默认）——按字数 / 条数上限裁。**一字不改**：它是语义渲染
#: 的**回退路径**（精炼不可用时必须逐字回到这里），改一行就等于改了两条
#: 路径的共同地基。
VIEW_MODE_CAPPED = "capped"

#: **语义渲染**（H-41 阶段 2）——正文 / 元素 / 链接**由模型选**，渲染层按
#: 模型的选择**逐字重建**，**没有字符硬顶**。
#:
#: 与 ``capped`` 的差别**只有一处**，但那处是全部：
#:
#: - ``capped``：**规则层**决定「留多少」⇒ 超限即**截断**（被禁的那种）；
#: - ``semantic``：**模型**决定「留哪些」⇒ 剩下的**整体留下**，
#:   **一个字都不截**。
#:
#: ⚠️ 语义模式下 ``truncated.view`` **恒为 False**——「模型看到了全部
#: 原文」与「规则层裁掉了后半段」是**两件事**，而下游（D-3 / D-9 / D-10）
#: 看到 ``truncated`` 判真就把整批判成「不可判定」。
VIEW_MODE_SEMANTIC = "semantic"

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
    **噪声与去重**丢掉的条数；``elements`` / ``links`` 记的是**上限触发**时
    的那个上限（两个都是常量，记账只是为了让「触没触顶」看得见）。

    ``body_chars`` 记的是**正文的实际长度**（H-41）——**不是上限值**：
    keep 行（下一步动作目标）**无条件全量入选**，它们本身超上限时正文
    会比上限长 ⇒ 记上限就是**账实不符**（实测：正文 900 字、账上 320）。
    「本层动没动过正文」由 ``cropped`` 与 ``truncated.view`` 表达——
    **长度**归 ``limits``，**信号**归 ``truncated``。

    ⚠️ **两种渲染模式的口径不同**（**读数不可混在一张表里比**）：

    - ``capped``（规则裁剪）：``elements_dropped`` / ``links_dropped`` 记的是
      **噪声与去重**丢掉的条数（**上限裁掉的**另由 ``elements`` / ``links``
      表达）；``body_lines_dropped`` 记的是**规则剔掉的**行数。
    - ``semantic``（语义渲染）：噪声行**也摆给模型了** ⇒ 规则层**一行都没剔**
      ⇒ ``body_lines_dropped`` = **0**；``elements_dropped`` /
      ``links_dropped`` 记的是「**没进最终视图**的条数」（= 模型没选的
      那些），与 ``body_refined_dropped`` 同属「模型丢的」那一笔账。
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


@dataclass(frozen=True, slots=True)
class _RefinedEntry:
    """一条精炼记录（**归一化后**）——新旧两种索引形状的共同表示。

    ``kept`` = 模型选的正文行；``elements`` / ``links`` = 模型选的
    元素 ``(role, name)`` / 链接 ``(text, href)``。
    """

    kept: tuple[str, ...] = ()
    elements: tuple[tuple[str, str], ...] = ()
    links: tuple[tuple[str, str], ...] = ()
    source: str = ""
    protocol: str = ""


def _pairs_of(items: Any, k1: str, k2: str) -> tuple[tuple[str, str], ...]:
    """``[{"role": …, "name": …}]`` → ``(("role值", "name值"), …)``。

    ⚠️ **只认两个键都在的条目**——缺一个就跳（宁缺毋滥：半条记录重建出
    的东西是「模型没选过却出现在语料里」，那就不是「选」了）。
    """
    out: list[tuple[str, str]] = []
    for item in items or ():
        if not isinstance(item, Mapping):
            continue
        second = str(item.get(k2) or "")
        if not second:
            continue
        out.append((str(item.get(k1) or ""), second))
    return tuple(out)


def _refined_entry(
    refined: Mapping[str, Any] | None,
    key: str,
) -> _RefinedEntry | None:
    """按**键**在精炼索引里找这一条观察的精炼记录。

    找不到 = 这一条没精炼过（或精炼的是**另一份**候选集）⇒ ``None`` ⇒
    走规则裁剪。

    ⚠️ **不猜、不近似匹配**：候选集不一样就是不一样。哈希是唯一的
    定位方式（见 :mod:`trajectory_pipeline.common.refine_sidecar` 的
    「键怎么选」——按步号索引要在 P2/T 两套坐标系之间维护映射）。

    ⚠️ **两种形状都吃**（H-41 阶段 2）：

    - **旧形状** ``{键: [行, …]}``（方案 25 / ``refine.v2`` 旁挂）——
      只有正文一个维度，**不能**用于语义渲染的元素 / 链接；
    - **新形状** ``{键: {kept, elements, links, source, protocol}}``
      （``refine.v4`` 旁挂）——三段齐全。

    同一个索引里混着两种形状也不会错：**逐条按值的类型判断**。
    """
    if not refined or not key:
        return None
    value = refined.get(key)
    if isinstance(value, Mapping):
        return _RefinedEntry(
            kept=tuple(str(x) for x in (value.get("kept") or ())),
            elements=_pairs_of(value.get("elements"), "role", "name"),
            links=_pairs_of(value.get("links"), "text", "href"),
            source=str(value.get("source") or ""),
            protocol=str(value.get("protocol") or ""),
        )
    if isinstance(value, (list, tuple)):
        return _RefinedEntry(kept=tuple(str(x) for x in value), source="llm")
    return None


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


def _rebuild_semantic_body(
    raw_body: str,
    picked: Sequence[str],
    *,
    focus: Sequence[str],
    keep: Sequence[str],
) -> tuple[str, int, int] | None:
    """语义模式：用模型选中的行**逐字重建**正文（**没有字符硬顶**）。

    Returns:
        ``(正文, 保留行数, 丢掉行数)``；
        ``None`` = 精炼不可用 ⇒ 调用方**整条回退规则裁剪**。

    与 :func:`_rebuild_from_refined` 的差别只有一处，但那是全部：
    **不再过一遍 :func:`_digest_body` 的字数预算**。语义模式下
    「保留哪些行」由模型说了算，**规则层不再有字数硬顶**——那道顶就是
    本方案要禁掉的截断（方案 26 §0 A2）。

    ⚠️ **保真口径不放宽**：模型选的行里没有「下一步动作目标」那一行时，
    渲染层**自己把目标行补回来**（同 :func:`_rebuild_from_refined` 第 3 关）。
    补的是**候选行**里的 keep 命中项——与规则路径同一份口径。

    ⚠️ **候选行含噪声行**（H-41 阶段 2 §6.3 #12）：语义模式下模型看到的是
    **全部非空行**（含规则判为噪声的，标着「可推翻」），所以这里也从
    **同一份**候选里取目标行——补的是模型**可能选中过**的那些行。

    ⚠️ **只可能漏，不可能造**：每一行都逐字来自 ``raw_body``。
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
            return None                      # 逐字核对不过 ⇒ 整条作废
        seen.add(line)
        ordered.append(line)
    if not ordered:
        # ⚠️ **两种「一行都没选」不是一回事**（H-41 阶段 3）：
        #
        # - **模型看过了、就是没选** ⇒ 保守口径**整条回退**规则裁剪。
        #   理由：语义模式是「全有或全无」——回退是**整条**回退，
        #   输出与 ``capped`` 逐字一致，且 ``semantic_fallback`` /
        #   ``truncated.view`` **如实记着**。反过来（正文留空、元素用模型的）
        #   才是混口径 ⇒ 宁可让这条走规则裁剪，也不要在语义观察里混进
        #   规则裁的内容。
        # - **压根没有候选行**（正文是空的 / 全是噪声）⇒ 模型**没得选**
        #   ⇒ 这不是「模型不想要」⇒ 正文就是空的，**不是失败**。
        #   ⚠️ 这条是**读步观察**（``read=interactive`` / ``links``，
        #   正文天然是空的）进精炼之后才有的情形（H-41 阶段 3 缺口 C）。
        candidates = body_lines.candidate_lines_with_noise(raw_body)
        if candidates:
            return None
        return "", 0, 0
    candidates = body_lines.candidate_lines_with_noise(raw_body)
    for line, _noise in candidates:
        if line in seen:
            continue
        if body_lines.line_score(line, focus, keep) >= 4:
            seen.add(line)
            ordered.append(line)
    ordered.sort(key=lambda line: positions[line])
    text = "\n".join(ordered)
    return text, len(ordered), max(len(candidates) - len(ordered), 0)


def _semantic_elements(
    cands: Sequence[view_candidates.ElementCandidate],
    *,
    total: int,
    keep: Sequence[str],
    picked: Sequence[tuple[str, str]],
) -> tuple[list[Mapping[str, Any]], int] | None:
    """元素：**模型选**的那几条**逐字重建**（**没有条数上限**）。

    Returns:
        ``(入选元素, 丢弃数)``；
        ``None`` = 模型选的东西**对不上原文** ⇒ 整条回退规则裁剪。

    ⚠️ **候选集与精炼侧同一份**（:func:`trajectory_pipeline.common.
    view_candidates.element_candidates`，``filter_noise=False``）——
    候选集不同 ⇒ 「模型报的第 7 号」与「渲染层找的第 7 号」不是同一个
    东西 ⇒ 训练样本里出现「模型选了 A、渲染层放进 B」。
    ⇒ 候选集**必须**是同一份构造，两边只 import 那个模块。

    ⚠️ **keep 命中的元素无条件入选**（与规则路径同款）：训练样本里不能
    出现「点了一个观察里没有的东西」。模型没选它 ⇒ **补回来**，
    不是「它没选就没有」。

    ⚠️ **候选集是传进来的**（不是这里现算）：**它同时是索引键的一部分**
    （:func:`trajectory_pipeline.common.refine_sidecar.selection_key`）
    ⇒ 精炼侧与渲染侧必须算**同一份** ⇒ 现算两次也行，但传一份更省，
    也更不容易两边算岔。
    """
    known = {(c.role, c.name) for c in cands}
    wanted = {(str(r), str(n)) for r, n in picked}
    if wanted and not wanted <= known:
        return None                          # 对不上 ⇒ 整条作废
    wanted |= {
        (c.role, c.name) for c in cands
        if view_candidates.element_matches_keep(c.name, keep)
    }
    selected = [c.element for c in cands if (c.role, c.name) in wanted]
    return selected, max(total - len(selected), 0)


def _semantic_links(
    cands: Sequence[view_candidates.LinkCandidate],
    *,
    total: int,
    keep: Sequence[str],
    picked: Sequence[tuple[str, str]],
) -> tuple[list[Mapping[str, Any]], int] | None:
    """链接：**模型选**的那几条**逐字重建**（**没有条数上限**）。

    Returns:
        ``(入选链接, 丢弃数)``；
        ``None`` = 模型选的东西**对不上原文** ⇒ 整条回退规则裁剪。

    ⚠️ 与 :func:`_semantic_elements` 同款纪律（候选集同一份、keep 无条件
    入选、对不上就整条作废）。
    """
    known = {(c.text, c.href) for c in cands}
    wanted = {(str(t), str(h)) for t, h in picked}
    if wanted and not wanted <= known:
        return None
    wanted |= {
        (c.text, c.href) for c in cands
        if view_candidates.link_matches_keep(c.href, keep)
    }
    selected = [c.link for c in cands if (c.text, c.href) in wanted]
    return selected, max(total - len(selected), 0)


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
        "elements_refined": False, "elements_refined_count": 0,
        "links_refined": False, "links_refined_count": 0,
        "view_mode": VIEW_MODE_CAPPED,
        "semantic_fallback": False,
        "truncated": {"source": False, "view": False},
        "degraded": ["observation_missing"],
        "interactive_elements": [], "links": [],
        "video_tag_count": 0, "iframe_count": 0,
        "action_error": "",
        "read": "",
        "limits": ViewLimits().to_json(),
    }


@dataclass(frozen=True, slots=True)
class _SemanticParts:
    """语义渲染的产物——三块**逐字重建**的结果。

    ⚠️ **全有或全无**：三块里任何一块对不上原文 ⇒ 整条回退规则裁剪。
    混着来（「正文用模型的、元素用规则的」）会造出一份**两套口径**的
    观察，读的人分不清哪部分是模型说的——那比全旧或全新都难查。
    """

    body: str = ""
    body_lines: int = 0
    body_dropped: int = 0
    body_lines_dropped: int = 0
    #: ⚠️ **正文这一块是不是真的被精炼过**（H-41 阶段 3）——
    #: 读步（正文压根不渲染）**不算**；正文**一个候选都没有**也不算
    #: （没东西可选 ≠ 模型不想要）⇒ ``body_refined`` 必须照实说，
    #: 否则回执会印出「[内容已精简: 保留 0 行]」——**那是谎报**
    #: （模型会读成「这页没内容」）。
    body_refined: bool = False
    elements: tuple[Mapping[str, Any], ...] = ()
    elements_dropped: int = 0
    links: tuple[Mapping[str, Any], ...] = ()
    links_dropped: int = 0


def _render_semantic(
    *,
    raw_body: str,
    has_body: bool,
    has_elements: bool,
    has_links: bool,
    elem_cands: Sequence[view_candidates.ElementCandidate],
    link_cands: Sequence[view_candidates.LinkCandidate],
    elements_total: int,
    links_total: int,
    entry: _RefinedEntry | None,
    focus: Sequence[str],
    keep: Sequence[str],
) -> _SemanticParts | None:
    """语义渲染：把模型的选择**逐字重建**成三块。**不可用 ⇒ ``None``**。

    ⚠️ **什么算「不可用」**（三样，缺一不可）：

    1. **没有精炼记录**（没跑精炼 / 旁挂没落盘 / 候选集对不上）；
    2. **这条记录不是语义协议产出的**（``protocol`` 不是
       :data:`trajectory_pipeline.common.refine_sidecar.ENTRY_PROTOCOL`）
       ——老协议只选正文，元素 / 链接没选过 ⇒ 不能拿它当语义渲染用
       （那会造出「正文是模型选的、元素是规则选的」这种混口径观察）；
    3. **精炼没成功**（``source != "llm"``——模型没回话 / 校验不过 /
       预算用尽）。

    ⇒ 三样都过，才重建；否则调用方**整条回退规则裁剪**。

    ⚠️ **候选集是传进来的**：它同时是**索引键**的一部分
    （:func:`trajectory_pipeline.common.refine_sidecar.selection_key`）
    ⇒ 精炼侧与渲染侧必须是**同一份**。
    """
    if entry is None:
        return None
    if entry.source != "llm":
        return None
    if entry.protocol != refine_sidecar.ENTRY_PROTOCOL:
        return None

    body = ""
    body_lines = 0
    body_dropped = 0
    body_lines_dropped = 0
    body_refined = False
    if has_body:
        rebuilt = _rebuild_semantic_body(
            raw_body, entry.kept, focus=focus, keep=keep,
        )
        if rebuilt is None:
            return None
        body, body_lines, body_dropped = rebuilt
        # ⚠️ **规则层一行都没剔**（噪声行也摆给模型了）⇒ 这笔账是 0。
        # 「模型没选的行数」记在 ``body_refined_dropped``（**另一笔账**）。
        body_lines_dropped = 0
        # ⚠️ **正文这一块是不是真的被精炼过**（H-41 阶段 3）：正文
        # **一个候选都没有** ⇒ 模型没得选 ⇒ 那不是「精炼过」
        # ⇒ ``body_refined`` 照实说 False（否则回执印「保留 0 行」=
        # **谎报**）。⚠️ 读步（``has_body`` 为假）压根不渲染正文 ⇒ 同样
        # **不算**精炼过（那一支精炼的是元素 / 链接）。
        body_refined = bool(body_lines)

    picked_elements: tuple[Mapping[str, Any], ...] = ()
    elements_dropped = 0
    if has_elements:
        got = _semantic_elements(
            elem_cands, total=elements_total, keep=keep, picked=entry.elements,
        )
        if got is None:
            return None
        picked_elements, elements_dropped = got

    picked_links: tuple[Mapping[str, Any], ...] = ()
    links_dropped = 0
    if has_links:
        got = _semantic_links(
            link_cands, total=links_total, keep=keep, picked=entry.links,
        )
        if got is None:
            return None
        picked_links, links_dropped = got

    return _SemanticParts(
        body=body,
        body_lines=body_lines,
        body_dropped=body_dropped,
        body_lines_dropped=body_lines_dropped,
        body_refined=body_refined,
        elements=picked_elements,
        elements_dropped=elements_dropped,
        links=picked_links,
        links_dropped=links_dropped,
    )


def render(
    obs: Mapping[str, Any] | None,
    *,
    focus: Sequence[str] = (),
    keep: Sequence[str] = (),
    action_error: str = "",
    refined: Mapping[str, Any] | None = None,
    mode: str = VIEW_MODE_CAPPED,
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
        refined: **精炼索引**（按**键**定位，见
            :mod:`trajectory_pipeline.common.refine_sidecar`）。有它就用
            模型选出的内容重建，没有就按规则裁剪。

            ⚠️ **键按「这条记录选过的那几维」算**（H-41 阶段 3）：
            ``semantic``（三段协议）⇒ **候选集哈希**（正文 + 元素候选 +
            链接候选）；``capped``（老协议只选正文）⇒ **正文哈希**。

            ⚠️ **两种形状都吃**：旧形状 ``{键: [行, …]}``（只有正文
            一个维度，方案 25）、新形状
            ``{键: {kept, elements, links, source, protocol}}``
            （``refine.v4`` 旁挂，三段齐全）。
        mode: **渲染模式**——:data:`VIEW_MODE_CAPPED`（默认，规则裁剪）
            或 :data:`VIEW_MODE_SEMANTIC`（语义渲染：模型选，**不截**）。

            ⚠️ **两种模式的保真口径完全一致**（动作目标必须留下、只可能漏
            不可能造）——换的是「留多少」由谁决定，不是「保不保真」。

            ⚠️ 语义模式**不可用时整条回退规则裁剪**（精炼没跑 / 旁挂是旧
            协议 / 模型选的东西对不上原文）⇒ 输出与**没精炼时逐字一致**。
    """
    if mode not in (VIEW_MODE_CAPPED, VIEW_MODE_SEMANTIC):
        raise ValueError(
            f"认不出的渲染模式 {mode!r}——已知的："
            f"{[VIEW_MODE_CAPPED, VIEW_MODE_SEMANTIC]}"
        )
    if not obs:
        out = _empty_view()
        out["action_error"] = str(action_error or "")
        out["view_mode"] = mode
        return out

    #: 读标记（方案 23 R4-1）。空串 = 普通整页观察。
    read = str(obs.get("read") or "")

    # **读步只渲染被读的那一块**：``snapshot`` 只留正文、``interactive``
    # 只留元素表、``links`` 只留链接表。其余块在这条观察里本来就没采
    # （见 :func:`executor.agent_loop._read`），但万一上游多带了，也不能
    # 让它混进「我是按读的语义渲染的」这份视图——训练侧会把没读的块
    # 当成读过。窗口同时换成读步的宽口径。
    #
    # ⚠️ **哪几块渲染**由 :func:`trajectory_pipeline.common.view_candidates.
    # read_blocks` 定（**唯一真源**）：精炼侧摆给模型的候选必须与这里
    # 一致，否则模型选了不渲染的东西 ⇒ 逐字核对不过 ⇒ 整条回退。
    has_body, has_elements, has_links = view_candidates.read_blocks(read)
    body_limit = VIEW_READ_BODY_CHARS if read == "snapshot" else VIEW_BODY_CHARS
    element_limit = (
        VIEW_READ_ELEMENTS if read == "interactive" else VIEW_ELEMENTS
    )
    link_limit = VIEW_READ_LINKS if read == "links" else VIEW_LINKS

    raw_body, body_source = _pick_body(obs)
    elements_all = list(obs.get("interactive_elements") or ())
    links_all = list(obs.get("links") or ())
    elements = elements_all if has_elements else []
    links = links_all if has_links else []
    # 键名跟着**存档的方言**走（P4）：老存档写 ``tag``/``label``，aria 面
    # （camoufox / camofox）批次写 ``role``/``name``。这一层拿不到 ``driver``，
    # 所以按存档自己的
    # 键名判方言（:func:`vocabulary_of_archive_elements`——键名本来就是
    # 这个轴的投影，读回来不是猜）。
    #
    # ⚠️ **不带 ``ref``**：``ref`` 是会话内句柄，进训练数据等于教模型背
    # 一个随机数，且样本永远不可回放（P2 契约 ``TestNoRefInActions``）。
    # 所以这里不用 :func:`element_json`（它会写 ref），只借它的键名。
    vocabulary = vocabulary_of_archive_elements(elements_all)
    kind_key, text_key = element_keys(vocabulary)

    #: **精炼留痕**（方案 25）。这些字段**不进 ``limits`` / ``truncated``**——
    #: 那两个是「裁了多少」的账，精炼是「换了一种裁法」，混进去会破掉
    #: ``limits`` 的 7 键契约，也会让 ``gates._truncated_flag`` 的语义
    #: （上游截断 vs 本层触顶）变模糊。
    body_refined = False
    body_refined_lines = 0
    body_refined_dropped = 0
    elements_refined = False
    elements_refined_count = 0
    links_refined = False
    links_refined_count = 0
    #: **语义模式没走通**（精炼不可用 / 旧协议 / 对不上原文）⇒ 整条回退
    #: 规则裁剪。这条标记**独立于** ``body_refined``——读步（正文不渲染）
    #: 也会「没有正文精炼」，但那不是回退。
    semantic_fallback = False

    #: 精炼记录（**两种形状都吃**）。没有 ⇒ 走规则裁剪。
    #:
    #: ⚠️ **键按「这条记录选过的那几维」算**（H-41 阶段 3）：
    #:
    #: - **语义模式**（三段协议）⇒ 键 = **候选集哈希**（正文 + 元素候选
    #:   + 链接候选）⇒ 模型的判断只对它**当时看到的那份清单**成立；
    #: - **规则模式**（老协议，只选正文）⇒ 键 = **正文哈希**。
    #:
    #: ⚠️ **为什么语义模式不能按正文哈希找**（实测 2026-11-18）：
    #: 同一份正文在一条轨迹里可能被看到多次，而那几次的元素 / 链接
    #: 集合可以不同（动态页面、镜像站）⇒ 按正文哈希找 ⇒ **A 步的选择
    #: 被贴到 B 步上** ⇒ 逐字核对不过 ⇒ **整条回退规则裁剪**
    #: （实测 13 条回退里 12 条是这一条）。
    semantic: _SemanticParts | None = None
    if mode == VIEW_MODE_SEMANTIC:
        #: **候选集现算一份**（语义口径：不剔导航词）——它同时是
        #: **索引键的一部分** ⇒ 精炼侧与渲染侧必须算**同一份**。
        elem_cands = view_candidates.element_candidates(
            elements, vocabulary=vocabulary, filter_noise=False,
        )
        link_cands = view_candidates.link_candidates(links, filter_noise=False)
        entry = _refined_entry(
            refined,
            refine_sidecar.selection_key(
                raw_body,
                elements=[(c.role, c.name) for c in elem_cands],
                links=[(c.text, c.href) for c in link_cands],
            ),
        )
        semantic = _render_semantic(
            raw_body=raw_body,
            has_body=has_body,
            has_elements=has_elements,
            has_links=has_links,
            elem_cands=elem_cands,
            link_cands=link_cands,
            elements_total=len(elements),
            links_total=len(links),
            entry=entry,
            focus=focus,
            keep=keep,
        )
        if semantic is None:
            semantic_fallback = True
    else:
        entry = _refined_entry(refined, refine_sidecar.body_key(raw_body))
        if entry is None:
            # ⚠️ **旧形状**（正文哈希 → 行）：老旁挂与手工构造的索引
            #: 是这个键 ⇒ 认它（零回归）。⚠️ **只在规则模式兜这一手**：
            #: 语义模式按正文哈希兜底 = 把「A 步的选择」贴到「B 步」
            #: ⇒ 正是上面那条要防的污染。
            entry = _refined_entry(refined, refine_sidecar.selection_key(raw_body))

    if semantic is not None:
        body = semantic.body
        body_lines_dropped = semantic.body_lines_dropped
        #: 语义模式**没有字符硬顶** ⇒ 没有「触顶」这回事。
        body_truncated = False
        #: ⚠️ **读步（正文不渲染）不算「正文被精炼过」**（H-41 阶段 3）：
        #: 说成精炼过 ⇒ 回执印「[内容已精简: 保留 0 行]」⇒ **那是谎报**
        #: （模型会读成「这页没内容」）⇒ 那一支精炼的是**元素 / 链接**
        #: （``elements_refined`` / ``links_refined`` 各自说破）。
        body_refined = semantic.body_refined
        body_refined_lines = semantic.body_lines
        body_refined_dropped = semantic.body_dropped
        selected_elements = list(semantic.elements)
        elements_dropped = semantic.elements_dropped
        elements_capped = False
        elements_refined = True
        elements_refined_count = len(selected_elements)
        selected_links = list(semantic.links)
        links_dropped = semantic.links_dropped
        links_capped = False
        links_refined = True
        links_refined_count = len(selected_links)
    else:
        # ── 规则裁剪（**默认路径，一字不改**）────────────────────
        if not has_body:
            body, body_lines_dropped, body_truncated = "", 0, False
            body_source = "missing"
        else:
            # ⚠️ **语义模式回退时，精炼记录整条不算**（H-41 阶段 2）。
            #
            # 理由是**口径必须统一**：老协议（``select.v1``）只选了正文，
            # 元素 / 链接**压根没被选过** ⇒ 拿它渲语义模式会造出
            # 「正文是模型选的、元素是规则选的」那种**混口径**观察——
            # 读的人分不清哪部分是模型说的。
            #
            # ⇒ 语义模式不可用时**整条回退纯规则裁剪**，输出与
            # **没精炼时逐字一致**（方案 26 §0 A3）。
            picked = (
                entry.kept
                if (entry and mode == VIEW_MODE_CAPPED)
                else ()
            )
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
        selected_elements, elements_dropped, elements_capped = _select_elements(
            elements, vocabulary=vocabulary, focus=focus, keep=keep,
            limit=element_limit,
        )
        selected_links, links_dropped, links_capped = _select_links(
            links, focus=focus, keep=keep, limit=link_limit
        )
    # 「动作无效」与「采集降级」分列：前者是这一步白做，后者是这块没采到。
    limits = ViewLimits(
        # ⚠️ **记实际长度，不记上限值**（H-41）。
        #
        # 此前这里记的是 ``body_limit``（那个**常量**）⇒ **账实不符**：
        # keep 行（下一步动作目标）**无条件全量入选**，若它们本身超上限，
        # 正文实际是 900 字而账上写着 320 ⇒ 读的人以为「裁到了 320」，
        # 实际裁都没裁。实测复现：
        #
        #     body_text 实际长度 = 900
        #     limits = {'body_chars': 320, …, 'cropped': True}   ← 报 320
        #
        # ⇒ 现在记**实际长度**（本层真动了正文时），单一真源。
        # 「有没有动过」仍由 ``cropped`` / ``truncated.view`` 表达——
        # **长度**归 ``limits``，**信号**归 ``truncated``。
        #
        # ⚠️ **口径变了**（上限值 → 实际长度）⇒ **新旧批次不可混在一张
        # 表里比**（方案 26 §6.5）。
        #
        # ⚠️ **语义模式**记的是「模型选完之后的实际长度」——本层**没有
        # 字符硬顶**，动没动过由「模型有没有丢行」表达（``body_refined_
        # dropped``）。没有丢行 ⇒ 与原文逐字一样 ⇒ 不记。
        body_chars=(
            len(body)
            if (body_truncated or (semantic is not None and body_refined_dropped))
            else None
        ),
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
        #: **元素 / 链接的精炼留痕**（H-41 阶段 2）：语义模式下元素与链接
        #: **也是模型选的**，不是「按相关度取前 10 / 4 条」。
        #: 不说破的话，读样本的人会把「模型挑过的控件」当成「页面就这么多
        #: 控件」——那是在训练侧教出来的幻觉。
        "elements_refined": elements_refined,
        "elements_refined_count": elements_refined_count,
        "links_refined": links_refined,
        "links_refined_count": links_refined_count,
        #: **渲染模式**——``capped``（规则裁剪）/ ``semantic``（模型选）。
        #: ⚠️ 两种模式的 ``limits`` 口径**不完全一样**（语义模式没有
        #: 上限触发项，``elements_dropped`` 记的是「模型没选的条数」），
        #: ⇒ **两种模式的批次不可混在一张表里比**。
        "view_mode": mode,
        #: **语义模式没走通**（精炼不可用 / 旧协议 / 对不上原文）⇒ 整条
        #: 回退规则裁剪。这条标记与 ``body_refined`` 分开：读步本来就不
        #: 渲染正文，那不是回退。
        "semantic_fallback": semantic_fallback,
        #: 上游截断（dom 层预算用尽）与本层裁剪分列，合在一起才是
        #: 「模型看到的东西不完整」的完整描述。
        "truncated": {
            "source": bool(obs.get("truncated")),
            # ⚠️ **语义模式恒 False**（H-41 阶段 2）：那 10 / 4 / 320 的
            #: 硬顶**没有触发**——本层一个字都没截。下游（D-3 / D-9 /
            #: D-10）看到 ``truncated`` 判真就把整批判成「不可判定」，
            #: 而语义模式的正文**本来就是模型挑过的**，不是「被截的」。
            # ⚠️ **回退时强制 True**（诚实记账）：这一条是**规则裁剪**
            # 出来的，不能让人以为它也是模型挑的。
            "view": (
                True
                if semantic_fallback
                else bool(body_truncated or elements_capped or links_capped)
            ),
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
    refined: Mapping[str, Any] | None = None,
    mode: str = VIEW_MODE_CAPPED,
) -> list[dict[str, Any]]:
    """批量渲染。输入是 :class:`~trajectory_pipeline.assembler.schema.Sample`
    的 ``observations`` 原样。

    ``focuses`` / ``keeps`` / ``action_errors`` 与 ``observations`` **按下标
    对齐**；缺省 = 这一条没有任务词 / 没有下一步动作 / 动作没报错。

    ``refined`` 是**一份精炼索引给全部观察共用**（按**键**定位，
    不按步号）——所以这里不用对齐下标。

    ``mode`` 逐条透传（:data:`VIEW_MODE_CAPPED` / :data:`VIEW_MODE_SEMANTIC`）。
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
            mode=mode,
        ))
    return out


__all__ = [
    "NAV_LINE_WORDS",
    "NOISE_LINE_RE",
    "TASK_KEYWORD_RE",
    "VIEW_BODY_CHARS",
    "VIEW_ELEMENTS",
    "VIEW_LINKS",
    "VIEW_MODE_CAPPED",
    "VIEW_MODE_SEMANTIC",
    "VIEW_READ_BODY_CHARS",
    "VIEW_READ_ELEMENTS",
    "VIEW_READ_LINKS",
    "ViewLimits",
    "element_line",
    "render",
    "render_all",
]