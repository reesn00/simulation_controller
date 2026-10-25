"""rubric 版本与评估器指纹——D10 / 软维度数字的「尺子编号」。

为什么单独一个模块
------------------
`builder`（写 manifest / 出 agreement）与 `scorers`（判分）都要用它，而两边
都不该成为另一方的 import 根：`builder → scorers` 会拖进 LLM 客户端，
`scorers → builder` 则是验收方反过来依赖报表。放这里，两边都只依赖本模块。

它回答的问题是现在答不出来的那句话：
**「v6 这批 74.7% 是哪版尺子量出来的？」**

两件事分开记
------------
- :data:`RUBRIC_VERSION` —— **人写的**版本号，唯一真源是 rubric 文档的
  ``**版本**: v1.2`` 行；本常量抄一份并**由 contract test 比对**，
  改文档不改常量（或反之）当场红。
- :func:`evaluator_version` —— **机器算的**指纹（sha256 前 12 位），覆盖
  D-3 / D-9 的 system prompt、契约枚举与 few-shot 池。prompt 改一个字，
  指纹就变，分歧卡由此能回答「这条是旧 prompt 还是新 prompt 判的」。
- :data:`ANCHOR_TABLES` —— **各 judge 各档的锚点释义**（方案 16 §7 / C-10）。
  prompt 由它渲染，不另写一份散文。

范围声明（不夸大）
------------------
:func:`evaluator_version` **只覆盖 LLM 判断题（D-3 / D-9）**。代码评分器
（D-2 / D-4 / D-5 / D-6）另有一把尺：:func:`code_scorer_version` 取的是
**源码内容**的指纹，不是 git commit——commit 不含工作树里未提交的改动，
而 M-29 的教训恰恰是「代码改了、commit 没动」。:func:`scorer_fingerprint`
把两把尺拼成一行（``code:…|llm:…``），写进冻结行的 ``scorer_version`` 栏，
让「这批分数是哪版评分器打的」有答案（C-11）。

两份都不造假版本号：**造一个永远不变的 ``code-v1`` 比没有更坏**，它会让
「没变过」看起来像被验证过。

fail-closed
-----------
rubric 文档读不到、或版本行解析不出来 → 抛 ``ValueError``，不返回空串。
数字旁边挂一个空版本号，比数字本身更误导。
"""

from __future__ import annotations

import hashlib
import json
import pathlib
import re

#: rubric 文档（与代码同版本控制）。解析它的 ``**版本**: vN`` 行。
RUBRIC_DOC = (
    pathlib.Path(__file__).resolve().parents[2]
    / "docs"
    / "设计方案"
    / "在线视频场景rubric.md"
)

#: 当前 rubric 版本。**改 rubric 必须同时改这里**（contract test 盯着）。
RUBRIC_VERSION = "1.2"

#: 文档版本行格式：``**版本**: v1.2 | **生效日期**: ...``
_VERSION_RE = re.compile(r"^\*\*版本\*\*[:：]\s*v([0-9]+(?:\.[0-9]+)*)", re.MULTILINE)


def documented_rubric_version(text: str | None = None) -> str:
    """从 rubric 文档正文解析版本号；解析不出抛 ``ValueError``（fail-closed）。

    ``text`` 显式提供时只解析该文本（测试用），不读盘。
    """
    if text is None:
        if not RUBRIC_DOC.exists():
            raise ValueError(f"rubric 文档不存在：{RUBRIC_DOC}")
        text = RUBRIC_DOC.read_text(encoding="utf-8")
    m = _VERSION_RE.search(text)
    if not m:
        raise ValueError(
            f"rubric 文档里找不到版本行（期望形如 '**版本**: v1.2'）：{RUBRIC_DOC}"
        )
    return m.group(1)


def evaluator_version() -> str:
    """D-3 / D-9 判分面的指纹（sha256 前 12 位）。

    纳入：rubric 版本、锚点表、两段 system prompt、契约枚举（类别 / 子类 /
    verdict）、few-shot 池（按内容排序后序列化，顺序变化也算变化）。

    锚点表单列一份（哪怕它已经在 prompt 里）——**表的变动要能被指纹看见**，
    否则「改了表但 prompt 渲染没跟上」会静悄悄地过去。

    惰性 import scorers：本函数被 CLI 调用时才加载 LLM 侧常量，
    避免 `builder` 的其它子命令被拖慢。
    """
    from trajectory_pipeline.evaluation.scorers import d3_scorer
    from trajectory_pipeline.evaluation.scorers import d9_scorer

    parts = {
        "rubric_version": RUBRIC_VERSION,
        "anchors": {dim: dict(sorted(table.items())) for dim, table in ANCHOR_TABLES.items()},
        "d3_prompt": d3_scorer.D3_SYSTEM_PROMPT,
        "d3_categories": sorted(d3_scorer.D3_CATEGORIES),
        "d3_subtypes": sorted(d3_scorer.D3_SUBTYPES),
        "d9_prompt": d9_scorer.D9_SYSTEM_PROMPT,
        "d9_verdicts": sorted(d9_scorer.D9_VERDICTS),
        "d9_few_shot": json.loads(
            json.dumps(d9_scorer.D9_FEW_SHOT_EXAMPLES, ensure_ascii=False, sort_keys=True,
                       default=str)
        ),
    }
    blob = json.dumps(parts, ensure_ascii=False, sort_keys=True).encode("utf-8")
    return hashlib.sha256(blob).hexdigest()[:12]


# ---------------------------------------------------------------------------
# 代码评分器面的内容指纹（方案 16 C-11）
# ---------------------------------------------------------------------------
#
#: 代码评分器模块（D-2 / D-4 / D-5 / D-6）——**按源码内容**取指纹。
#:
#: 为什么不用 git commit：commit **不含工作树里未提交的改动**，而 M-29 的
#: 教训恰恰是「代码改了、commit 没动」——用 commit 当版本号，会把「同一个
#: commit 下的两个不同行为」记成同一个版本，正是校准回路要防的那件事。
#: 内容指纹对「改了就变」敏感，这才是版本号该有的性质。
CODE_SCORER_MODULES: tuple[str, ...] = (
    "code_scorer.py",
    "d4_scorer.py",
    "d5_scorer.py",
    "d6_scorer.py",
)

#: 版本**没量到**时的占位值。**不是空串**：空串会被读成「版本是空的」，
#: 而「没量到」与「版本为空」是两件事——前者是缺口，后者是数据。
VERSION_UNRECORDED = "unrecorded"


def code_scorer_version() -> str:
    """代码评分器面（D-2 / D-4 / D-5 / D-6）的内容指纹（sha256 前 12 位）。

    读不到某个模块 → 把 ``<missing>`` 写进被哈希的内容里，**不跳过**：
    跳过去等于把「少了一个评分器」记成「评分器没变」。
    """
    parts: list[str] = []
    here = pathlib.Path(__file__).resolve().parent
    for name in CODE_SCORER_MODULES:
        try:
            source = (here / name).read_text(encoding="utf-8")
        except OSError:
            source = "<missing>"
        parts.append(f"{name}:{source}")
    blob = "\n".join(parts).encode("utf-8")
    return hashlib.sha256(blob).hexdigest()[:12]


def scorer_fingerprint() -> str:
    """**整张评分面的指纹**——「这批分数是哪版评分器打的」。

    格式 ``code:<内容指纹>|llm:<prompt 指纹>``：

    - ``code``：:func:`code_scorer_version`（D-2/D-4/D-5 / D-6 的源码内容）；
    - ``llm``：:func:`evaluator_version`（rubric 版本 + D-3/D-9 prompt + 锚点）。

    这是 C-11 写进冻结行 ``scorer_version`` 栏的东西。**取不到就写
    ``unrecorded``，不写空串、不写常量**——常量会让「没量到」看起来像
    「量过且没变过」。
    """
    try:
        llm_fp = evaluator_version()
    except Exception:  # pragma: no cover - 惰性 import 失败时也不该崩
        llm_fp = VERSION_UNRECORDED
    return f"code:{code_scorer_version()}|llm:{llm_fp}"


# ---------------------------------------------------------------------------
# 维度 → rubric 条款号（方案 16 §5.5 / C-02）
# ---------------------------------------------------------------------------
#
#: 维度名 → rubric 条款号。**唯一真源**：分歧卡的 ``rubric_ref``、prompt 锚点表
#: （方案 16 §7）都从这里投影，不许各写一份。
#:
#: 条款号本来就存在于 rubric 文档里（``G-1..G-7`` / ``D-2..D-10`` / ``B-1..B-4``），
#: 这里做的是**让代码能引用它**。`d7` / `d8` 是批次级维度，条款号是 ``B-2`` /
#: ``B-4``——它们不进样本级条数统计，但映射必须齐，否则 dim 一出现就查不到。
DIM_CLAUSES: dict[str, str] = {
    "d2": "D-2",
    "d3": "D-3",
    "d4": "D-4",
    "d5": "D-5",
    "d6": "D-6",
    "d7": "B-2",   # 检索覆盖完整性（批次级）
    "d8": "B-4",   # 异常处置合规率（批次级）
    "d9": "D-9",
    "d10": "D-10",
}

#: 有 1–5 分档的条款——档位号直接拼在条款号后（``d3`` + 5 → ``D-3.5``）。
#: ``d7`` / ``d8`` 的 B 条款**不出分数**（B-1..B-4 是断言不是评分卡），
#: 所以它们不在本集合里；拼上去会造出一个 rubric 里不存在的条款号。
TIERED_DIMS: frozenset[str] = frozenset(
    {"d2", "d3", "d4", "d5", "d6", "d9", "d10"}
)


def clause_of(dimension: str, tier: int | float | None = None) -> str:
    """维度（+ 可选档位）→ rubric 条款号。

    ``clause_of("d3", 5) == "D-3.5"``；``clause_of("d10") == "D-10"``。

    未登记维度 → ``KeyError``（fail-closed，与本模块其它解析同款）：
    返回空串或原样透传会让拼错的 dim 带着一个假条款号进分歧卡。
    档位越界（不在 1–5）同样抛 ``ValueError``——``D-3.9`` 这种条款不存在。
    """
    try:
        base = DIM_CLAUSES[dimension]
    except KeyError:
        raise KeyError(
            f"维度 {dimension!r} 没有登记 rubric 条款号；先补 DIM_CLAUSES"
        ) from None
    if tier is None or dimension not in TIERED_DIMS:
        return base
    try:
        t = int(tier)
    except (TypeError, ValueError):
        raise ValueError(f"档位 {tier!r} 不是整数") from None
    if not 1 <= t <= 5:
        raise ValueError(f"档位 {t} 不在 1–5 内（{base}.{t} 不是 rubric 条款）")
    return f"{base}.{t}"


# ---------------------------------------------------------------------------
# 锚点表（方案 16 §7 / C-10）——owner 2026-10-23 裁定 D-C7
# ---------------------------------------------------------------------------
#
#: 维度 → 锚点表：**档位 → 该档的释义**（逐档与 rubric 文档同字）。
#:
#: **为什么要有这张表**：D-3 / D-9 的 system prompt 原本是一整段散文，锚点语义
#: 散落在 docstring 与手写的 ``"rubric §D-3 锚点 2"`` 里——改 rubric 时没人会
#: 同步它们，评分口径与提示词于是各走各的（方案 16 §7 的原话）。
#:
#: owner 已裁定（D-C7，2026-10-23）：**rubric 变更强制连带改 prompt**。机器盯
#: 的方式是三件事同时成立：
#:
#: 1. **prompt 从表渲染**（:func:`render_anchor_block`）——prompt 里没有第二份散文；
#: 2. **表与 rubric 文档逐档对齐**——:func:`anchors_drift` 现读文档比对，
#:    **改 rubric 某档而不改表 ⇒ 契约测试红**；
#: 3. **条款号不手抄**——条款号由 :func:`clause_of` 投影（``clause_of("d3", 5)
#:    == "D-3.5"``），抄第二份必然漂（§8 明写 :data:`DIM_CLAUSES` 是唯一真源）。
#:
#: ⚠️ §7 的示意把条款号写进表里（``dict[int, tuple[str, str]]``）；这里不抄——
#: 理由同上，条款号归 :data:`DIM_CLAUSES` 管。
ANCHOR_TABLES: dict[str, dict[int, str]] = {
    "d3": {
        5: "判对，且选中/排除理由引用了观察中的具体依据",
        4: '判对，理由为泛泛描述（"不是播放站"而无具体依据）',
        3: "ambiguous 站处置正确（判「选择进入验证」），但 ambiguous 未单列而被归入选中或排除",
        2: "判错（播放站判成非播放站，或反之）",
        1: "未判（fail-closed）且该站本可判",
    },
    "d9": {
        5: "全部锚定；不确定处如实表达",
        4: "1 处措辞含糊，无事实错误",
        3: "1 处轻微幻觉（不影响动作）或 1 处后见之明",
        2: "系统性不符或套话替代推理",
        1: "思考编造关键事实并驱动错误动作（与 D-2 联动）",
    },
}

#: rubric 文档里维度卡的表头：``**D-3 播放站识别正确性** | …``。
_CARD_HEAD_RE = re.compile(r"^\*\*([DGB]-\d+)\s", re.MULTILINE)
#: 维度卡里「评分锚」那一栏的字段名。
_ANCHOR_FIELD = "**评分锚**"
#: 档位行：``5 = 判对，且……``。
_TIER_LINE_RE = re.compile(r"^([1-5])\s*=\s*(.+)$")


def _norm_text(text: str) -> str:
    """比对用的归一化：压空白 + 统一引号。

    rubric 文档里 ASCII 引号与中文引号混用（``"不是播放站"`` / ``「选择进入验证」``），
    手抄锚点时引号形态会漂。归一化只动引号与空白，**语义一字不动**。
    """
    s = text.replace("“", '"').replace("”", '"')
    s = s.replace("‘", "'").replace("’", "'")
    s = s.replace("「", '"').replace("」", '"')
    return re.sub(r"\s+", "", s)


def _dimension_card(text: str, clause: str) -> str:
    """取 rubric 文档里某个维度卡（``D-3`` 那一段）的正文。找不到抛 ``ValueError``。"""
    heads = [(m.start(), m.group(1)) for m in _CARD_HEAD_RE.finditer(text)]
    for i, (pos, name) in enumerate(heads):
        if name == clause:
            end = heads[i + 1][0] if i + 1 < len(heads) else len(text)
            return text[pos:end]
    raise ValueError(f"rubric 文档里找不到 {clause} 的维度卡")


def _anchor_body(card: str) -> str:
    """取维度卡里「评分锚」那一栏的正文（到下一个 ``- **字段**`` 为止）。"""
    body: list[str] = []
    started = False
    for line in card.splitlines():
        if not started:
            if _ANCHOR_FIELD in line:
                started = True
                tail = line.split(_ANCHOR_FIELD, 1)[1].lstrip("：:").strip()
                if tail:
                    body.append(tail)
            continue
        if re.match(r"^\s*-\s*\*\*[^*]+\*\*", line):
            break  # 下一个字段（正例 / 反例 / 边界 / 备注）
        body.append(line)
    if not started:
        raise ValueError("维度卡里找不到「评分锚」栏")
    return "\n".join(body)


def _parse_tiers(body: str) -> dict[int, str]:
    """档位行 → ``{档位: 释义}``。

    两种写法都吃：逐行 ``- 5 = …``（D-3 / D-9）与一行写完的 ``5 = … | 4 = …``（D-2）。
    """
    tiers: dict[int, str] = {}
    for raw in body.splitlines():
        line = raw.strip()
        if line.startswith("- "):
            line = line[2:].strip()
        if not line:
            continue
        parts = line.split("|") if "|" in line else [line]
        for part in parts:
            m = _TIER_LINE_RE.match(part.strip())
            if m:
                tiers[int(m.group(1))] = m.group(2).strip()
    return tiers


def rubric_tier_text(dimension: str, tier: int, text: str | None = None) -> str:
    """rubric 文档现读该维该档的原文（fail-closed：读不到就抛，不返回空串）。

    ``text`` 显式给出时只解析该文本（测试用），不读盘。
    """
    if text is None:
        if not RUBRIC_DOC.exists():
            raise ValueError(f"rubric 文档不存在：{RUBRIC_DOC}")
        text = RUBRIC_DOC.read_text(encoding="utf-8")
    clause = clause_of(dimension)
    tiers = _parse_tiers(_anchor_body(_dimension_card(text, clause)))
    try:
        return tiers[int(tier)]
    except KeyError:
        raise ValueError(
            f"{clause} 档 {tier} 在 rubric 文档里找不到评分锚（读到 {sorted(tiers)}）"
        ) from None


def anchors_drift(dimension: str, text: str | None = None) -> list[str]:
    """锚点表与 rubric 文档不一致的档位清单（空列表 = 对齐）。

    **这就是 D-C7 的机械执行面**：改 rubric 某档而不改表 ⇒ 这里非空 ⇒
    契约测试红 ⇒ prompt 与准则不再各走各的。
    """
    table = ANCHOR_TABLES.get(dimension)
    if table is None:
        raise KeyError(f"维度 {dimension!r} 没有锚点表")
    drift: list[str] = []
    for tier, anchor in sorted(table.items(), reverse=True):
        clause = clause_of(dimension, tier)
        try:
            doc_text = rubric_tier_text(dimension, tier, text=text)
        except ValueError as exc:
            drift.append(f"{clause}：{exc}")
            continue
        if _norm_text(anchor) != _norm_text(doc_text):
            drift.append(f"{clause}：表={anchor!r} 文档={doc_text!r}")
    return drift


def anchor_clauses(dimension: str) -> tuple[str, ...]:
    """该维锚点表覆盖的条款号（由档位投影，**不手抄**）。

    ``anchor_clauses("d9") == ("D-9.5", "D-9.4", "D-9.3", "D-9.2", "D-9.1")``。
    """
    table = ANCHOR_TABLES.get(dimension)
    if not table:
        raise KeyError(f"维度 {dimension!r} 没有锚点表")
    return tuple(clause_of(dimension, tier) for tier in sorted(table, reverse=True))


def render_anchor_block(dimension: str) -> str:
    """把锚点表渲染成 prompt 里的一段（§7：prompt 由表渲染，不许另写散文）。

    这一段进的是 **system prompt**，所以措辞要防一件事：**裁判不能因为
    「档位摆在那里」而改口径或拒答**。因此段首写明「这一栏是评分员事后
    打档用的，你照旧只输出契约」。
    """
    table = ANCHOR_TABLES.get(dimension)
    if not table:
        raise KeyError(f"维度 {dimension!r} 没有锚点表")
    rows = "\n".join(f"- {tier} = {table[tier]}" for tier in sorted(table, reverse=True))
    return (
        f"\n\n## 评分锚（{clause_of(dimension)} 各档释义）\n"
        "（这一栏是**评分员事后打档**用的释义，**不是你的输出**——"
        "照旧只输出上面的 JSON 契约，不要因为档位而拒答或改契约。）\n"
        f"{rows}"
    )


def llm_part(fingerprint: str) -> str:
    """从评分面指纹里取 ``llm:`` 段——「prompt 面是哪版」。

    指纹不是本模块这个格式的（或压根没记）→ ``unrecorded``。
    **不返回空串**：空串会被读成「版本为空」而不是「没量到」。
    """
    for part in str(fingerprint).split("|"):
        if part.startswith("llm:"):
            return part[4:] or VERSION_UNRECORDED
    return VERSION_UNRECORDED


def code_part(fingerprint: str) -> str:
    """从评分面指纹里取 ``code:`` 段——「代码面是哪版」。

    与 :func:`llm_part` 同款纪律：取不到 → ``unrecorded``，**不返回空串**。

    为什么要有它（面感知可比性）：D-2 / D-4 / D-5 / D-6 的分数是**代码**打的，
    拿 prompt 面去比它们是**问错了问题**（方案 16 C-11 补齐）。
    """
    for part in str(fingerprint).split("|"):
        if part.startswith("code:"):
            return part[5:] or VERSION_UNRECORDED
    return VERSION_UNRECORDED


__all__ = [
    "ANCHOR_TABLES",
    "CODE_SCORER_MODULES",
    "DIM_CLAUSES",
    "RUBRIC_DOC",
    "RUBRIC_VERSION",
    "TIERED_DIMS",
    "VERSION_UNRECORDED",
    "anchor_clauses",
    "anchors_drift",
    "clause_of",
    "code_part",
    "code_scorer_version",
    "documented_rubric_version",
    "evaluator_version",
    "llm_part",
    "render_anchor_block",
    "rubric_tier_text",
    "scorer_fingerprint",
]