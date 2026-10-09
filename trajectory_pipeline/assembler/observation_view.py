"""训练态观察渲染——**代码做**的，不用 LLM。

P1 存的是**取证态**观察：正文全文、80 个交互元素、50 条链接。
喂给模型的那一份必须裁剪（全文进训练窗口会把信噪比压垮），于是有了
「同一份观察的两种形态」这条口径：

    P1（取证）→ :func:`render` → P2 训练态

四条硬规矩
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
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

from trajectory_pipeline.assembler.schema import BODY_KEY, LEGACY_BODY_KEY

#: 训练态的正文上限。取 2000 是权衡后的值，不是拍脑袋：
#: 播放页的关键信息（片名、集数、播放入口）通常在首屏内，而 iframe 播放器的
#: 页面正文本身就很短。这个数字一旦改了，历史样本之间就不再可比——
#: 所以它**进 ``limits``**，改数字会让差异可见，而不是悄悄生效。
VIEW_BODY_CHARS = 2000
VIEW_ELEMENTS = 40
VIEW_LINKS = 30


@dataclass(frozen=True, slots=True)
class ViewLimits:
    """本层裁剪的**全部**事实。逐项列出，不给「大约」。

    空元组 = 本层没裁任何东西（可与「上游没截断」区分开）。
    """

    body_chars: int | None = None
    elements: int | None = None
    links: int | None = None

    def to_json(self) -> dict[str, Any]:
        return {
            "body_chars": self.body_chars,
            "elements": self.elements,
            "links": self.links,
            "cropped": bool(self.body_chars or self.elements or self.links),
        }


def _pick_body(obs: Mapping[str, Any]) -> tuple[str, bool, str]:
    """取正文，返回 ``(正文, 是否被本层裁短, 正文来自哪个键)``。

    降级来源标成 ``body_preview_only`` 而不是 ``body_preview``：
    后者读起来像个正常的来源名，扫一眼就过去了；前者才带得走
    「这份正文只有 400 字」这层警告。
    """
    body = obs.get(BODY_KEY)
    source = BODY_KEY
    if not (isinstance(body, str) and body):
        legacy = obs.get(LEGACY_BODY_KEY)
        if isinstance(legacy, str) and legacy:
            body, source = legacy, "body_preview_only"
        else:
            return "", False, "missing"
    if len(body) > VIEW_BODY_CHARS:
        return body[:VIEW_BODY_CHARS], True, source
    return body, False, source


def render(obs: Mapping[str, Any] | None) -> dict[str, Any]:
    """渲染一条训练态观察。

    输出的键**只有一份契约**——P2/P3 都读它，训练视图渲染器
    （模块 5 的 ``views.py``，待建）也在它之上做，不再各自裁一遍。
    """
    if not obs:
        return {
            "url": "", "page_title": "", "body_text": "",
            "body_source": "missing", "body_chars": 0,
            "truncated": {"source": False, "view": False},
            "degraded": ["observation_missing"],
            "interactive_elements": [], "links": [],
            "video_tag_count": 0, "iframe_count": 0,
            "limits": ViewLimits().to_json(),
        }

    body, cropped, body_source = _pick_body(obs)
    elements = list(obs.get("interactive_elements") or ())
    links = list(obs.get("links") or ())
    limits = ViewLimits(
        body_chars=VIEW_BODY_CHARS if cropped else None,
        elements=VIEW_ELEMENTS if len(elements) > VIEW_ELEMENTS else None,
        links=VIEW_LINKS if len(links) > VIEW_LINKS else None,
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
        "truncated": {"source": bool(obs.get("truncated")), "view": cropped},
        "degraded": list(obs.get("degraded") or ()),
        "interactive_elements": [
            {"tag": str(e.get("tag") or ""), "label": str(e.get("label") or "")}
            for e in elements[:VIEW_ELEMENTS]
        ],
        "links": [
            {"text": str(l.get("text") or ""), "href": str(l.get("href") or "")}
            for l in links[:VIEW_LINKS]
        ],
        "video_tag_count": int(obs.get("video_tag_count") or 0),
        "iframe_count": int(obs.get("iframe_count") or 0),
        "limits": limits.to_json(),
    }


def render_all(observations: tuple[Mapping[str, Any], ...] | list[Any]) -> list[dict[str, Any]]:
    """批量渲染。输入是 :class:`~trajectory_pipeline.assembler.schema.Sample`
    的 ``observations`` 原样。"""
    return [render(o) for o in observations]


__all__ = ["VIEW_BODY_CHARS", "VIEW_ELEMENTS", "VIEW_LINKS",
           "ViewLimits", "render", "render_all"]
