"""**跨面可比的元素计数口径**——两面 ``elements_total`` 不同义的解法
（设计方案 04 §4.9.6 / 方案 15 R3 的共同前置）。

问题
----
两个执行层数出来的 ``elements_total`` **不是同一个东西**：

============  ==================================  ==========================
面            枚举基础                              过滤规则
============  ==================================  ==========================
obscura       上游 ``browser_interactive_elements``  上游自己的（**黑盒**）
camoufox      ``page.aria_snapshot()`` 的可访问性树  role 词表 → 无名 → 真同名
============  ==================================  ==========================

于是第一轮双跑记下的「105.5 → 31.5（−70%）」**不是一个可解释的量**：
差值里混着「枚举基础不同」与「过滤规则不同」两件事，
**查因果之前必须先让两面可比**。

解法（不是「调阈值」）
---------------------
**同一段 JS 在两个浏览器上各跑一次**。

``SHARED_DOM_JS`` 是**唯一真源**：它按一套写死的 DOM 规则枚举交互候选，
并给每个候选算出 ``(role, name)``。obscura 走 ``browser_evaluate``、
camoufox 走 ``page.evaluate``——**同一段代码、同一个 DOM、同一套规则**，
于是两面的计数**按构造可比**。残差只有「浏览器引擎的 DOM/ARIA 实现差异」，
而那正是要量的东西。

然后是**同一把尺子** :func:`filter_ladder`：role 词表 → 无名 → 真同名 → 截断。
camoufox 面的 :meth:`CamoufoxDriver.interactive` 改成调它（**行为不变**，
现有测试封着），共享口径也调它——**于是「规则」这一层也是同一把尺子**。

三句话说清产出
--------------
1. ``shared`` 账目——**跨面可比**：同一个 DOM、同一段 JS、同一把尺子；
2. ``face`` 账目——**本面自己**的枚举账目（camoufox 是 ARIA 阶梯，
   obscura 只有「上游给了几条」这一个数）；
3. 两者相减 ⇒ **−70% 落在哪一档**（枚举基础？规则？还是引擎？）。

⚠️ **这套口径不改变任何生产行为**
--------------------------------
``elements_total`` **一个字都没改**——D-2 打分（判据「被裁掉」vs「不存在」）
依赖它，改了就动到历史存档的语义。共享口径是**新增的一列**，
由 ``--element-basis`` 显式开启（多一次 evaluate 往返），
**默认关**：它是**测量仪器**，不是生产链的必需项。

⚠️ **共享口径的 name 是手算的，不是引擎算的
------------------------------------------------
camoufox 面用 playwright 的 ``aria_snapshot``（权威，实测 91/91 = 100% 一致），
共享口径用本模块的 ``nameOf`` 手算链——**与 playwright 的一致率实测 87.9%**
（设计方案 04 §4.5.3）。所以共享口径的**绝对值**不等于 camoufox 面的
生产值；它要回答的是「**两个浏览器看到的同一个页面**差多少」，不是
「camoufox 的元素表对不对」。**两者别混。**
"""

from __future__ import annotations

import json
from collections import Counter
from dataclasses import dataclass
from typing import Any, Iterable, Mapping, Sequence

__all__ = [
    "ROLE_VOCAB",
    "LADDER_STAGES",
    "SHARED_BASE_STAGES",
    "SHARED_DOM_JS",
    "SHARED_LIMIT",
    "ElementBasis",
    "filter_ladder",
    "parse_shared_basis",
]


#: 采集面枚举的 role 词表——**必须短**（设计方案 04 §4.5.2）。
#:
#: 只收本场景真用得到的：播放/提交按钮、搜索结果链接、站内搜索框、筛选器。
#: 词表之外的 role 一律不枚举——宁可少列，也不要列一堆用不上、且各自带
#: 一堆边界情况的 role。**这张表就是「哪些元素会出现在观察里」的全部定义。**
#:
#: 2026-10-21 从 ``camoufox_driver.py`` 搬到这里：**它现在是两把尺子共用的词表**
#: （camoufox 面自己的 ARIA 阶梯 + 共享口径的阶梯），留在驱动里会让
#: 「共享口径」抄第二份——抄两份必然漂，而漂了之后 −70% 又变成不可解释的量。
ROLE_VOCAB: tuple[str, ...] = (
    "button", "link", "textbox", "searchbox", "combobox",
    "checkbox", "radio", "tab",
)

#: 过滤阶梯的**档位名**——**两把尺子共用这一套名字**，
#: 于是两本账能**逐档对齐**，差值落在哪一档一眼可见。
#:
#: ⚠️ **逐档对齐是「同一个观察里」的**。把各档的**中位数**摆成一列时，
#: 各中位数来自**不同的观察**（实测：candidates 52 − hidden 3 ≠ visible 31），
#: 所以那张表**不可相减**——要逐观察看落差，跑
#: ``research/probes/inc72_ladder_identity.py``。
#:
#: ⚠️ 这里**不含「枚举基础」那一档**：基础在两面**就是不同的东西**
#: （face = ARIA 节点数；shared = DOM 候选数），
#: 硬凑成同一个键名就成了「把两件事写成同一种数据」——
#: 所以基础各自用自己的键（``nodes`` / ``candidates``），**不合并**。
LADDER_STAGES: tuple[str, ...] = (
    "role_matched",       # 通过 role 词表
    "role_unmatched",     # 可见但 role 不在词表内
    "nameless_dropped",   # role 命中但没有可访问名
    "ambiguous_dropped",  # 有名字但同名整组冲突
    "kept",               # 截断后实际保留
    "truncated",          # 因 limit 被截掉
)

#: 共享口径**枚举基础**的档位名。**可见性单独占一档**，
#: 因为它是**三档过滤规则**里的第一档（设计方案 04 §4.9.6：可见性 / 词表 / 无名），
#: 混进别的档里就分不出「这一条是没找到」还是「这一条是隐形的」。
SHARED_BASE_STAGES: tuple[str, ...] = ("candidates", "hidden", "visible")

#: 共享口径的**测量上限**。它是一次测量，不是生产枚举——
#: 生产枚举的 200 条上限（D-2 的「被裁掉 vs 不存在」判据）在这里不适用，
#: 因为共享口径要数的是「**这个页面有多少**」，不是「我们留了多少」。
#: 真实页面的交互候选极少超过 500；超了会在账目里记 ``truncated``，不装作没有。
SHARED_LIMIT = 500

#: **共享口径的 DOM 枚举 JS——唯一真源，两个面跑同一段。**
#:
#: ⚠️ 它是**手算的** role / name，不是引擎算的（见模块 docstring 末段）。
#: 它要回答的是「**同一个页面在两个浏览器上差多少**」，
#: 所以**规则必须写死、两个面必须共用**——规则的差异会把引擎差异盖掉。
#:
#: 产出（``JSON.stringify`` 的字符串，driver 侧 ``json.loads``）：
#:
#:     {"nodes": 候选总数, "hidden": 不可见被跳过数, "pairs": [[role, name], …]}
#:
#: 用 ``JSON.stringify`` 是**必须的**：obscura 的 ``browser_evaluate`` 返回
#: JS 值的 ``toString()``，对象会变成 ``[object Object]``（tools.md 记过）。
SHARED_DOM_JS = r"""
(function () {
  var SELECTOR = "a[href],button,input,select,textarea,summary,"
    + "[contenteditable],[onclick],[tabindex],[role]";
  var INPUT_ROLE = {
    button: "button", submit: "button", reset: "button", image: "button",
    checkbox: "checkbox", radio: "radio", search: "searchbox",
    range: "slider", number: "spinbutton", text: "textbox",
    email: "textbox", tel: "textbox", url: "textbox", password: "textbox"
  };

  function tagOf(el) { return (el.tagName || "").toLowerCase(); }

  function eligible(el) {
    if (el.hidden) return false;
    if (tagOf(el) === "input"
        && (el.getAttribute("type") || "").toLowerCase() === "hidden") {
      return false;
    }
    var style = window.getComputedStyle ? window.getComputedStyle(el) : null;
    if (style && (style.display === "none" || style.visibility === "hidden")) {
      return false;
    }
    if (el.getClientRects && el.getClientRects().length === 0) return false;
    return true;
  }

  function roleOf(el) {
    var raw = el.getAttribute("role");
    if (raw) {
      var first = raw.trim().split(/\s+/)[0];
      if (first) return first;
    }
    var tag = tagOf(el);
    if (tag === "a") return el.hasAttribute("href") ? "link" : "generic";
    if (tag === "button" || tag === "summary") return "button";
    if (tag === "select") return "combobox";
    if (tag === "textarea") return "textbox";
    if (tag === "input") {
      var type = (el.getAttribute("type") || "text").toLowerCase();
      return INPUT_ROLE[type] || "textbox";
    }
    if (el.isContentEditable) return "textbox";
    if (el.hasAttribute("onclick")) return "button";
    if (el.hasAttribute("tabindex")) return "button";
    return "generic";
  }

  function nameOf(el) {
    var aria = (el.getAttribute("aria-label") || "").trim();
    if (aria) return aria;
    var ids = el.getAttribute("aria-labelledby");
    if (ids) {
      var parts = [];
      var list = ids.trim().split(/\s+/);
      for (var i = 0; i < list.length; i++) {
        var ref = document.getElementById(list[i]);
        if (ref) parts.push((ref.textContent || "").trim());
      }
      var joined = parts.join(" ").trim();
      if (joined) return joined;
    }
    var tag = tagOf(el);
    if (tag === "input" || tag === "select" || tag === "textarea") {
      var labels = el.labels;
      if (labels && labels.length) {
        var buf = "";
        for (var j = 0; j < labels.length; j++) {
          buf += " " + (labels[j].textContent || "");
        }
        buf = buf.trim();
        if (buf) return buf;
      }
      var fallbacks = ["placeholder", "title", "alt", "value"];
      for (var k = 0; k < fallbacks.length; k++) {
        var v = (el.getAttribute(fallbacks[k]) || "").trim();
        if (v) return v;
      }
      return "";
    }
    var text = (el.textContent || "").replace(/\s+/g, " ").trim();
    if (text) return text.slice(0, 120);
    var img = el.querySelector("img[alt]");
    if (img) {
      var alt = (img.getAttribute("alt") || "").trim();
      if (alt) return alt;
    }
    var title = (el.getAttribute("title") || "").trim();
    if (title) return title;
    return "";
  }

  var nodes = document.querySelectorAll(SELECTOR);
  var pairs = [];
  var hidden = 0;
  for (var n = 0; n < nodes.length; n++) {
    var el = nodes[n];
    if (!eligible(el)) { hidden += 1; continue; }
    pairs.push([roleOf(el), nameOf(el)]);
  }
  return JSON.stringify({nodes: nodes.length, hidden: hidden, pairs: pairs});
})()
"""


@dataclass(frozen=True, slots=True)
class ElementBasis:
    """一个观察上的**两本元素账**。

    ``shared``
        **跨面可比的那本**——同一段 JS、同一把尺子，两个浏览器各跑一次。
        键 = :data:`SHARED_BASE_STAGES` + :data:`LADDER_STAGES`。
    ``face``
        **本面自己**的枚举账目，键 = ``nodes`` + :data:`LADDER_STAGES`。
        **键可以缺**：obscura 面的上游工具只给最终列表，
        给不出「role 命中几条 / 无名丢掉几条」——**缺就是缺，不填 0**
        （填 0 会被读成「一条都没被过滤」）。

    两本账**逐档对齐** ⇒ **差值落在哪一档**：枚举基础？过滤规则？还是引擎？
    ⚠️ 逐档对齐是**同一个观察里**的；把各档的中位数摆成一列时**不可相减**
    （各中位数来自不同观察）。
    """

    shared: Mapping[str, int]
    face: Mapping[str, int]

    def to_json(self) -> dict[str, dict[str, int]]:
        """落盘形状。**两本账平级**——不合成一个数。

        合成一个数就是「让两面可比」的失败版：把两个不同义的数相加或取平均，
        得到的数**在两边都不是任何一个真实集合的大小**。
        """
        return {
            "shared": {k: int(v) for k, v in self.shared.items()},
            "face": {k: int(v) for k, v in self.face.items()},
        }


def filter_ladder(
    pairs: Iterable[Sequence[str]],
    *,
    role_vocab: Iterable[str] = ROLE_VOCAB,
    limit: int = SHARED_LIMIT,
) -> tuple[list[tuple[str, str]], dict[str, int]]:
    """过滤阶梯——**两把尺子共用这一把**。

    顺序（与 camoufox 面 :meth:`CamoufoxDriver.interactive` 逐条一致）：
    role 词表 → 无名 → 真同名整组 → 截断。

    同名整组**整组丢**，而不是留第一个：留哪个是赌（属「猜测」），
    而 ``get_by_role(role, name, exact=True)`` 对同名会触发 strict mode 报错——
    留下等于给动作面埋一颗必然失败的雷。

    Returns:
        ``(kept, ledger)``——``kept`` 是 ``(role, name)`` 列表，
        ``ledger`` 的键见 :data:`LADDER_STAGES`。
        ⚠️ **账目里没有「枚举基础」那一档**：基础由调用方自己加
        （face 用 ``nodes``、shared 用 ``candidates``）——
        两面的基础**不是同一个东西**，硬塞进同一个键名就是
        「把两件事写成同一种数据」。
    """
    vocab = frozenset(role_vocab)
    all_pairs = [(str(r), str(n)) for r, n in pairs]
    by_role = [p for p in all_pairs if p[0] in vocab]
    named = [p for p in by_role if p[1]]
    occurrences = Counter(named)
    unique = [p for p in named if occurrences[p] == 1]
    kept = unique[: max(int(limit), 0)]
    ledger = {
        "role_matched": len(by_role),
        "role_unmatched": len(all_pairs) - len(by_role),
        "nameless_dropped": len(by_role) - len(named),
        "ambiguous_dropped": len(named) - len(unique),
        "kept": len(kept),
        "truncated": len(unique) - len(kept),
    }
    return kept, ledger


def parse_shared_basis(raw: Any) -> tuple[list[tuple[str, str]], dict[str, int]]:
    """解析共享口径 JS 的返回，算出账目。

    **fail-closed**：拿不到就抛，不返回空账——空账会被读成
    「这个页面上一个交互元素都没有」，而真相是「没测到」。
    """
    payload: Any = raw
    if isinstance(payload, str):
        text = payload.strip()
        if not text:
            raise ValueError("共享口径返回空串——没测到，不是「没有元素」")
        try:
            payload = json.loads(text)
        except ValueError as exc:
            raise ValueError(f"共享口径返回的不是 JSON: {text[:120]!r}") from exc
    if not isinstance(payload, Mapping):
        raise ValueError(f"共享口径返回的不是对象: {type(payload).__name__}")
    pairs_raw = payload.get("pairs")
    if not isinstance(pairs_raw, Sequence) or isinstance(pairs_raw, (str, bytes)):
        raise ValueError("共享口径返回里没有 pairs 列表")
    pairs: list[tuple[str, str]] = []
    for item in pairs_raw:
        if not isinstance(item, Sequence) or isinstance(item, (str, bytes)) or len(item) != 2:
            raise ValueError(f"共享口径的 pair 形状不对: {item!r}")
        pairs.append((str(item[0]), str(item[1])))

    kept, ladder = filter_ladder(pairs)
    candidates = payload.get("nodes")
    hidden = payload.get("hidden")
    base = {
        "candidates": int(candidates) if isinstance(candidates, int) else len(pairs),
        "hidden": int(hidden) if isinstance(hidden, int) else 0,
        "visible": len(pairs),
    }
    return kept, {**base, **ladder}
