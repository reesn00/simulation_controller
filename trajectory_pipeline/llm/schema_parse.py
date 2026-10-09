"""结构化输出解析——本项目 LLM 后端的共享容错层。

**每一层容错都来自实测**，不是想象的（取证见
``output/pipeline/llm_probe.py`` 与本模块各函数 docstring）。后端不强制
``json_schema``，所以「模型会漂」不是风险而是**已观测事实**：

- 会套 ``<think>...</think>``（``llm/__init__.py`` 已记；raw CoT 受
  CLAUDE.md 红线约束，**必须剥掉且不落盘**）
- 会套 ``\\`\\`\\`json ... \\`\\`\\``` 代码块
- 即使 system prompt 写死了格式，仍会返回散文或 markdown 列表
- 会漂字段名（``ref`` / ``element_ref`` / ``控件ref`` 同义）

三条纪律：

1. **解析不出来就是 ``None``，不是 ``{}``。** ``{}`` 与「模型判了但字段
   为空」同形，而这两者在 I7 下走完全不同的路（前者必须 fail-closed）。
2. **容错只认形状，不认语义。** 别名表是显式的：加一个别名要写清
   「实测见过」，而不是把整个 dict 的键做模糊匹配——后者会让
   ``ref_lookup`` 之类的无关字段被误当成 ``ref``。
3. **不接受「差不多对」的数字。** ``confidence`` 越界就丢，不夹逼。
"""

from __future__ import annotations

import json
import re
from typing import Any, Mapping

# ═══════════════════════════════════════════════════════════════════════
# <think> 剥离
# ═══════════════════════════════════════════════════════════════════════

#: 未闭合的 ``<think>``：模型被截断时（max_tokens 用尽）会留下开标签。
#: 常见形态是 ``<think>`` 开、没 ``</think>`` 收，后面全是正文。
_THINK_OPEN = re.compile(r"<think>.*?(?:</think>|$)", re.DOTALL | re.IGNORECASE)

#: 少数模型用 ``<thinking>`` / ``<reasoning>``（OpenAI 系）。
_THINK_ALT = re.compile(
    r"<(thinking|reasoning)>.*?(?:</\1>|$)", re.DOTALL | re.IGNORECASE
)


def strip_think(text: str) -> str:
    """剥掉思维链包裹。**红线：raw CoT 不进任何持久化路径。``

    两条细节：

    - **未闭合也要剥**（``$`` 分支）。模型被 ``max_tokens`` 截断时
      会留下开标签没闭，此时若不剥，后面全是思维链正文，
      而它会被当成 JSON 去找——找不到，于是整条判定 fail-closed。
    - **剥完再剥一次**。``<think>`` 里套 ``<thinking>`` 的形态见过。
    """
    out = _THINK_OPEN.sub("", text)
    out = _THINK_ALT.sub("", out)
    return _THINK_OPEN.sub("", out).strip()


# ═══════════════════════════════════════════════════════════════════════
# JSON 抠取
# ═══════════════════════════════════════════════════════════════════════

_FENCE = re.compile(r"```(?:json|JSON)?\s*(.+?)```", re.DOTALL)


def _balanced(text: str, start: int) -> str | None:
    """从 ``start``（必须是 ``{``）取出一个**括号平衡**的 JSON 对象。

    不用正则找 ``\\{[^}]*\\}``——嵌套对象（``selected`` 是数组里的对象
    数组）会在第一个 ``}`` 处截断，而截断出来的片段恰好是合法 JSON 的
    概率不低，于是解析「成功」并得到一个静默缺字段的 dict。
    """
    depth = 0
    in_str = False
    esc = False
    for i in range(start, len(text)):
        ch = text[i]
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return text[start:i + 1]
    return None


def extract_json(text: str) -> dict[str, Any] | None:
    """从模型原文里抠出 JSON 对象。**抠不出返回 ``None``**。

    取值顺序：代码块 → **从头往后**逐个 ``{`` 试平衡括号 → 整段。

    ⚠️ **必须从前往后。** 这里踩过：早期版本从后往前找，实测模型返回
    ``{"selected":[{"url":...}],"rejected":[]}`` 时，解析结果是**内层那个
    ``{"url":...}``**——内层的 ``{`` 位置更靠后，于是被优先选中，而它恰好
    是合法 JSON，于是「解析成功」并**静默**丢掉了外层所有键。
    下游读到 ``selected`` 缺失，当成模型没选任何站点，判定从 True 掉成 None。
    契约测试当时没抓到：只有判断点 ① 的 payload 是嵌套的，其余三题都是
    扁平单层 JSON——**从后往前照样能取对**。所以「一个能跑的解析器」
    不等于「对所有形态都对」。
    """
    clean = strip_think(text)
    if not clean:
        return None

    candidates: list[str] = []
    fenced = _FENCE.findall(clean)
    candidates.extend(block.strip() for block in fenced)

    for i, ch in enumerate(clean):
        if ch != "{":
            continue
        seg = _balanced(clean, i)
        if seg:
            candidates.append(seg)

    candidates.append(clean.strip())

    for raw in candidates:
        try:
            got = json.loads(raw)
        except ValueError:
            continue
        if isinstance(got, dict):
            return got
    return None


# ═══════════════════════════════════════════════════════════════════════
# 字段取值（别名容错）
# ═══════════════════════════════════════════════════════════════════════

#: 实测见过的同义键名。**刻意不写「模糊匹配」**——那会把无关字段
#: 误认成目标字段，而误认出来的是 ref（代码拿它去点，点错不可逆）。
ALIASES: dict[str, tuple[str, ...]] = {
    "answer": ("answer", "result", "conclusion", "结论", "答案"),
    "ref": ("ref", "element_ref", "elementRef", "target_ref", "控件ref"),
    "trailer_only": ("trailer_only", "is_trailer_only", "trailerOnly", "仅预告"),
    "trailer_suspect": ("trailer_suspect", "suspects", "suspect_labels"),
    "evidence": ("evidence", "why", "reason", "依据", "理由"),
    "selected": ("selected", "chosen", "play_sites", "选中"),
    "rejected": ("rejected", "excluded", "filtered", "排除"),
    "confidence": ("confidence", "conf", "置信度"),
}

_TRUE = {"true", "yes", "y", "1", "是", "对", "有", "能"}
_FALSE = {"false", "no", "n", "0", "否", "错", "无", "不能"}


def as_bool(value: Any) -> bool | None:
    """宽松布尔解析。**认不出返回 ``None``——不猜。``

    实测模型会在同一个字段上混用 ``true`` / ``"是"`` / ``"True"``。
    但「宽松」有边界：``"可能"`` / ``"不确定"`` 归 ``None``，
    因为它们语义上是 fail-closed 的信号，猜成 ``False``
    等于把「模型不确定」写成「这里没有播放控件」。
    """
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        # 数字**只认 0 与 1**。模型回 ``score: 2`` 时那是个评分不是布尔，
        # 而 ``value != 0`` 会把它读成 ``True``——把「评分 2 分」当成
        # 「判为真」，方向还是错的那一侧。
        if value == 1:
            return True
        if value == 0:
            return False
        return None
    if isinstance(value, str):
        t = value.strip().lower()
        if t in _TRUE:
            return True
        if t in _FALSE:
            return False
    return None


def as_str(value: Any) -> str:
    """转字符串。非字符串一律空串——**不 ``str()`` 一个 dict**。

    ``str({"a":1})`` 得到 ``"{'a': 1}"``，它在 ``ref`` 字段上会被
    当成合法 ref 传给 click，而那会点空。
    """
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return str(value)
    return ""


def field(data: Mapping[str, Any], name: str, default: Any = None) -> Any:
    """按别名表取值。**只认显式登记的别名**。"""
    for key in ALIASES.get(name, (name,)):
        if key in data and data[key] is not None:
            return data[key]
    return default


def as_confidence(value: Any) -> float | None:
    """置信度解析。越界或认不出返回 ``None``——**不夹逼**。

    夹逼（把 ``1.7`` 夹成 ``1.0``、把 ``0`` 夹成 ``0.1``）会让一个
    明显失真的模型输出看起来像合法置信度，而阈值判断正建立在这个数上。
    """
    if isinstance(value, bool):
        return None
    try:
        f = float(value)
    except (TypeError, ValueError):
        return None
    if not (0.0 <= f <= 1.0):
        return None
    return f