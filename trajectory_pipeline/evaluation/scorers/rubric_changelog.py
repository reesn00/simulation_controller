"""rubric 变更记录的机器可读契约（方案 16 §5.5 / §5.8，C-12 + C-18）。

它回答两个以前答不出来的问题
----------------------------
1. **这次改版是谁逼出来的、动的是哪几条、预期什么效果**（C-12）——变更记录
   以前只有「版本 / 日期 / 变更内容 / 修订人」，查不到是哪批数据、哪张分歧卡
   逼出来的，也没有栏位可以驳回「以『评估器做不到』为由放宽」。
2. **改了 A 条款，怎么证明 B 条款的分数没动**（C-18）——§5.5 第 1 条要求
   「一次改版只动列出的条款」，而这句话以前只是散文。现在「条款」列是机器
   可读的，:func:`clause_invariance` 拿两版快照做机械 diff。

唯一真源：rubric 文档里那张表
-----------------------------
不另建一份 JSONL——抄两份必然漂（CLAUDE.md 纪律 6 的同一条道理）。本模块
**解析** ``docs/设计方案/在线视频场景rubric.md`` 里的 ``**变更记录**`` 表，
格式错当场抛 :class:`ChangelogError`。``RUBRIC_VERSION`` 必须能在表里找到
对应行：版本号升了却没写记录，契约测试当场红。

前向生效，不伪造历史
--------------------
机制从 :data:`TRACEABLE_SINCE`（下一个 rubric 版本）起生效。之前的行允许
``预期效果`` / ``分歧卡`` 留 ``—``：那几版本来就没有卡、也没有可证伪预期，
**事后补一个数字进去就是编数据**——与 H-15「裁定是前向的」同一条规矩。

两条便宜但真会咬人的规矩
------------------------
- **`预期效果` 不许写"希望变好"**：要么量化（含数字 + 比较符/箭头，如
  ``D-3.5 分歧 12 → ≤4``），要么写 ``逐位不变``（这条由 diff 守卫直接证伪）。
- **`责任区分声明` 不许只写"评估器做不到"**：必须说清「哪种情形下判不了」。
  纯借口（去掉借口词后剩不下内容）直接驳回。

``调权重`` 是**空类**
---------------------
本 rubric 没有权重体系（``G-7`` 明写批次级与样本级不可相加），所以
:data:`WEIGHT_TABLE_EXISTS` 为 False 时用到「调权重」的行会被驳回。留着这个
取值是为了将来真有权重表时不必改契约，**不是现在有东西可调**。
"""

from __future__ import annotations

import json
import pathlib
import re
import sys
from dataclasses import dataclass
from typing import Any, Iterable, Mapping, Sequence

from trajectory_pipeline.evaluation.scorers.rubric_version import (
    DIM_CLAUSES,
    RUBRIC_DOC,
    RUBRIC_VERSION,
    TIERED_DIMS,
    clause_of,
)


class ChangelogError(Exception):
    """变更记录的格式 / 规矩不成立。**一律 fail-closed**，不猜、不回落。"""


# ---------------------------------------------------------------------------
# 封闭集与小表
# ---------------------------------------------------------------------------

KIND_ADD = "新增"
KIND_REWORD = "修改表述"
KIND_DEPRECATE = "废弃"
KIND_RETIER = "调锚点分档"
KIND_REWEIGHT = "调权重"

#: ``change_kind`` 封闭集（方案 16 §5.8）。顺序即报表顺序。
CHANGE_KINDS: tuple[str, ...] = (
    KIND_ADD,
    KIND_REWORD,
    KIND_DEPRECATE,
    KIND_RETIER,
    KIND_REWEIGHT,
)

#: 本 rubric 有没有权重表。``G-7`` 明写批次级与样本级不可相加 ⇒ 现在没有。
#: 将来真引入权重表时改成 True，并同时给 ``G-7`` 改版——两件事必须一起做，
#: 否则「调权重」这一栏又开始骗人。
WEIGHT_TABLE_EXISTS = False

#: 机制生效的 rubric 版本（含）。**固定值，不随版本号上浮**——它记的是
#: 「从哪一版起有卡可指」，那一版之后永远要遵守。
TRACEABLE_SINCE = "1.3"

#: 变更记录表的列（顺序固定，缺一列 / 换序都抛）。前四列是原文就有的，
#: 后五列是 C-12 / C-18 补的。
#: ``条款`` 列是方案 §5.8 没列的一栏——§5.5 说「未列条款逐位不变」，那就得先有
#: 一个地方「列」，否则 diff 守卫没有分母。
COLUMNS: tuple[str, ...] = (
    "版本",
    "日期",
    "条款",
    "变更类型",
    "变更内容",
    "预期效果",
    "分歧卡",
    "责任区分声明",
    "修订人",
)

#: 文档里的「空值」记号。**不是垃圾桶**：只允许出现在
#: :data:`TRACEABLE_SINCE` 之前的行（`预期效果` / `分歧卡`），或首版 `新增` 行
#: 的 `责任区分声明`。
EMPTY_CELL = "—"

#: 条款号体系的三个族（rubric 文档里 ``G-1..G-7`` 全局规则、``D-1..D-10``
#: 样本级维度、``B-1..B-4`` 批次级断言）。族规模是**结构事实**：加一条
#: G-8 要同时改这里与文档，契约测试盯着两边一致。
GLOBAL_CLAUSES: tuple[str, ...] = tuple(f"G-{i}" for i in range(1, 8))
SAMPLE_CLAUSES: tuple[str, ...] = tuple(f"D-{i}" for i in range(1, 11))
BATCH_CLAUSES: tuple[str, ...] = tuple(f"B-{i}" for i in range(1, 5))

#: rubric 文档里写着的条款号（不含档位）。
BASE_CLAUSES: tuple[str, ...] = GLOBAL_CLAUSES + SAMPLE_CLAUSES + BATCH_CLAUSES

#: 档位条款号（``D-3.5`` 这种）。文档里**没有**逐条写出来（评分锚是
#: 「5 = …」的行），所以由 :func:`~...rubric_version.clause_of` 投影，不手抄。
TIER_CLAUSES: tuple[str, ...] = tuple(
    clause_of(dim, tier)
    for dim in sorted(TIERED_DIMS)
    for tier in range(1, 6)
)

KNOWN_CLAUSES: frozenset[str] = frozenset(BASE_CLAUSES) | frozenset(TIER_CLAUSES)

#: 「整份」通配。**只允许首版 `新增` 行使用**：`§5.5` 第 2 条禁止整份重写，
#: 而首版本来就是整份新增。
WILDCARD = "*"

#: 变更记录的起点标记（rubric 文档 `**变更记录**：`）。
_CHANGELOG_MARKER = "**变更记录**"

_VERSION_CELL_RE = re.compile(r"^v(\d+)\.(\d+)\s*(?:[（(](.+)[）)])?$")

#: 分歧卡 id：``v7:E006__2729220b__e9b9542b:d10``（由
#: :func:`~...calibration.disagreement.disagreement_id` 生成）。
_CARD_ID_RE = re.compile(r"^v\d+:[^:\s|]+:d\d+$")

#: 变体分隔（多值单元格里用）。
_SPLIT_CELL_RE = re.compile(r"\s*[,，、;；]\s*")

#: 量化预期的判据：有数字，且有比较符 / 箭头。
_HAS_DIGIT_RE = re.compile(r"\d")
_QUANT_RE = re.compile(r"→|<=|>=|≤|≥|<|>")

#: 「不变预期」的两个说法——diff 守卫能直接证伪它们。
_NEUTRAL_PHRASES = ("逐位不变", "零变化")

#: 借口词表。**只用来识别"整句话就是个借口"**，不是文本审查。
EXCUSE_PHRASES: tuple[str, ...] = (
    "评估器做不到",
    "判据做不到",
    "代码做不到",
    "做不到",
    "evaluator cannot",
    "evaluator can't",
)

#: 去掉借口与标点后，声明至少还剩这么多字才算「说了情形」。
_MIN_DECLARATION_CHARS = 6


# ---------------------------------------------------------------------------
# 条款词表
# ---------------------------------------------------------------------------

def known_clauses() -> frozenset[str]:
    """全部合法条款号（含档位）。测试与校验共用一份。"""
    return KNOWN_CLAUSES


def clause_vocabulary_covers_dims() -> list[str]:
    """``DIM_CLAUSES`` 里没有落进条款词表的维度条款（应为空）。"""
    return sorted({v for v in DIM_CLAUSES.values() if v not in KNOWN_CLAUSES})


# ---------------------------------------------------------------------------
# 解析
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Entry:
    """变更记录的一行（**一版可以有多行**：补记另起一行，§5.5 只增不删）。"""

    version: str               # "1.2"
    label: str                 # 版本括号里的补充说明，如 "补记，2026-10-17"
    date: str
    clauses: tuple[str, ...]   # ("D-5",) / (WILDCARD,)
    kinds: tuple[str, ...]     # ("修改表述",)
    summary: str
    expected_effect: str       # EMPTY_CELL 折成 ""
    disagreement_ids: tuple[str, ...]
    responsibility: str        # EMPTY_CELL 折成 ""
    author: str
    line_no: int = 0

    @property
    def key(self) -> tuple[int, int]:
        major, minor = self.version.split(".")
        return int(major), int(minor)

    @property
    def wildcard(self) -> bool:
        return WILDCARD in self.clauses

    def touches(self, clause: str) -> bool:
        """这一行是否声称动过某条款（通配行动全部）。"""
        if self.wildcard:
            return True
        return clause in self.clauses or _base_of(clause) in self.clauses


def _base_of(clause: str) -> str:
    """``D-3.5`` → ``D-3``；``D-5`` → ``D-5``。

    改一条款的**档位**应当允许整条款的分数动（``D-3.5`` 与 ``D-3`` 是同一张
    规格卡的两种写法）；反过来，列了 ``D-3`` 就覆盖它的 5 个档位。
    """
    head, _, tail = clause.partition(".")
    return head if tail.isdigit() else clause


def _cells(line: str) -> list[str]:
    """拆一行 markdown 表格。**单元格里不许有 `|`**（有就抛，不猜边界）。"""
    body = line.strip()
    if not body.startswith("|") or not body.endswith("|"):
        raise ChangelogError(f"不是表格行：{line!r}")
    return [c.strip() for c in body.strip("|").split("|")]


def _table_lines(text: str) -> list[tuple[int, str]]:
    """取 ``**变更记录**`` 之后的那张表（行号从 1 起）。"""
    lines = text.splitlines()
    start = None
    for idx, line in enumerate(lines):
        if line.strip().startswith(_CHANGELOG_MARKER):
            start = idx
            break
    if start is None:
        raise ChangelogError(f"rubric 文档里找不到 {_CHANGELOG_MARKER} 标记")
    out: list[tuple[int, str]] = []
    for idx in range(start + 1, len(lines)):
        line = lines[idx]
        if line.strip():
            if not line.lstrip().startswith("|"):
                break
            out.append((idx + 1, line))
        elif out:
            # 表内允许空行？不允许——表已经开始，空行即结束（免得吞掉下一节）。
            break
    if len(out) < 3:
        raise ChangelogError(
            f"{_CHANGELOG_MARKER} 之后没有表格（至少要表头 / 分隔 / 一行数据）"
        )
    return out


def _split_multi(cell: str) -> tuple[str, ...]:
    if cell in ("", EMPTY_CELL):
        return ()
    return tuple(t for t in _SPLIT_CELL_RE.split(cell) if t)


def parse_entries(text: str | None = None) -> list[Entry]:
    """解析变更记录表；任何结构问题抛 :class:`ChangelogError`。

    ``text`` 显式提供时只解析该文本（测试用），不读盘。
    """
    if text is None:
        if not RUBRIC_DOC.exists():
            raise ChangelogError(f"rubric 文档不存在：{RUBRIC_DOC}")
        text = RUBRIC_DOC.read_text(encoding="utf-8")

    rows = _table_lines(text)
    header_no, header_line = rows[0]
    header = _cells(header_line)
    if tuple(header) != COLUMNS:
        raise ChangelogError(
            f"变更记录表头不对（第 {header_no} 行）。期望 {list(COLUMNS)}，"
            f"实得 {header}。改表头等于改契约，先改 COLUMNS 再改文档"
        )
    sep = _cells(rows[1][1])
    if len(sep) != len(COLUMNS) or not all(set(c) <= {"-", ":", " "} for c in sep):
        raise ChangelogError(f"第 {rows[1][0]} 行不是表格分隔行：{rows[1][1]!r}")

    entries: list[Entry] = []
    for line_no, line in rows[2:]:
        cells = _cells(line)
        if len(cells) != len(COLUMNS):
            raise ChangelogError(
                f"第 {line_no} 行有 {len(cells)} 格，表头是 {len(COLUMNS)} 格"
                f"（单元格里不许出现 `|`）：{line!r}"
            )
        cells_by_name = dict(zip(COLUMNS, cells))
        m = _VERSION_CELL_RE.match(cells_by_name["版本"])
        if not m:
            raise ChangelogError(
                f"第 {line_no} 行版本格不认识：{cells_by_name['版本']!r}"
                "（期望 `v1.2` 或 `v1.2（补记，2026-10-17）`）"
            )
        entries.append(
            Entry(
                version=f"{m.group(1)}.{m.group(2)}",
                label=(m.group(3) or "").strip(),
                date=cells_by_name["日期"],
                clauses=_split_multi(cells_by_name["条款"]),
                kinds=_split_multi(cells_by_name["变更类型"]),
                summary=cells_by_name["变更内容"],
                expected_effect=(
                    "" if cells_by_name["预期效果"] == EMPTY_CELL
                    else cells_by_name["预期效果"]
                ),
                disagreement_ids=_split_multi(cells_by_name["分歧卡"]),
                responsibility=(
                    "" if cells_by_name["责任区分声明"] == EMPTY_CELL
                    else cells_by_name["责任区分声明"]
                ),
                author=cells_by_name["修订人"],
                line_no=line_no,
            )
        )
    if not entries:
        raise ChangelogError("变更记录表里一行数据都没有")
    return entries


# ---------------------------------------------------------------------------
# 校验（规矩本身）
# ---------------------------------------------------------------------------

def is_falsifiable(text: str) -> bool:
    """``预期效果`` 是否可证伪。

    两种可接受形态：

    - **量化**：既有数字又有比较符 / 箭头（``D-3.5 分歧 12 → ≤4``）；
    - **不变**：写了「逐位不变」/「零变化」——这正是
      :func:`clause_invariance` 能机械证伪的命题。

    「希望变好」「更贴合准则」这类都不算：没有分母、没有方向，永远无法说它没达到。
    """
    if any(p in text for p in _NEUTRAL_PHRASES):
        return True
    return bool(_HAS_DIGIT_RE.search(text)) and bool(_QUANT_RE.search(text))


def is_excuse_only(text: str) -> bool:
    """``责任区分声明`` 是否只是个借口。

    做法很笨，但够用：把借口词与标点去掉，剩下不足
    :data:`_MIN_DECLARATION_CHARS` 个字 ⇒ 没说「哪种情形下判不了」。
    **这是下限不是审查**——检测不了措辞巧妙的借口，只挡最裸的那种。
    """
    stripped = text
    for phrase in EXCUSE_PHRASES:
        stripped = stripped.replace(phrase, "")
    stripped = re.sub(r"[\s，,。、；;：:（）()\[\]【】—\-…\.]+", "", stripped)
    return len(stripped) < _MIN_DECLARATION_CHARS


def validate_entries(entries: Sequence[Entry]) -> None:
    """把 §5.5 / §5.8 的规矩一次性核完；有问题抛 :class:`ChangelogError`。

    规矩清单（每条都在 ``tests/unit/test_rubric_changelog.py`` 里有反例）：

    1. 版本单调不减；首行必须是 ``新增`` + 通配条款；
    2. 条款号必须存在（含档位），通配 ``*`` 只许首行用；
    3. 变更类型必须在封闭集里，「调权重」在没有权重表时驳回；
    4. ``TRACEABLE_SINCE`` 起：预期效果必须可证伪、分歧卡至少一条且形如
       ``v{N}:{golden_id}:{dim}``；
    5. 责任区分声明除首版外必填，且不许只写「评估器做不到」；
    6. 变更内容 / 修订人不许空。
    """
    if not entries:
        raise ChangelogError("变更记录为空")

    first = entries[0]
    if KIND_ADD not in first.kinds:
        raise ChangelogError(
            f"第 {first.line_no} 行（首版 v{first.version}）必须是「{KIND_ADD}」"
        )
    if not first.wildcard:
        raise ChangelogError(
            f"第 {first.line_no} 行是首版，条款应当写 {WILDCARD!r}（整份新增）"
        )

    last_key: tuple[int, int] | None = None
    for idx, entry in enumerate(entries):
        where = f"第 {entry.line_no} 行（v{entry.version}"
        where += f" {entry.label}）" if entry.label else "）"

        if last_key is not None and entry.key < last_key:
            raise ChangelogError(
                f"{where}：版本倒退（上一行是 {'.'.join(map(str, last_key))}）"
            )
        last_key = entry.key

        if not entry.summary.strip():
            raise ChangelogError(f"{where}：变更内容不能空")
        if not entry.author.strip():
            raise ChangelogError(f"{where}：修订人不能空")

        if not entry.clauses:
            raise ChangelogError(f"{where}：条款不能空（整份改动用 {WILDCARD}）")
        for clause in entry.clauses:
            if clause == WILDCARD:
                if idx != 0:
                    raise ChangelogError(
                        f"{where}：{WILDCARD} 只许首版用——§5.5 第 2 条禁止整份重写"
                    )
                continue
            if clause not in KNOWN_CLAUSES:
                raise ChangelogError(
                    f"{where}：条款 {clause!r} 不在条款词表里"
                    f"（合法形如 G-1 / D-3 / D-3.5 / B-2）"
                )

        if not entry.kinds:
            raise ChangelogError(f"{where}：变更类型不能空")
        for kind in entry.kinds:
            if kind not in CHANGE_KINDS:
                raise ChangelogError(
                    f"{where}：变更类型 {kind!r} 不在封闭集 {list(CHANGE_KINDS)} 里"
                )
            if kind == KIND_REWEIGHT and not WEIGHT_TABLE_EXISTS:
                raise ChangelogError(
                    f"{where}：本 rubric 没有权重体系（G-7：批次级与样本级不可相加）"
                    f"，「{KIND_REWEIGHT}」是空类——真要调权重，得先有权重表"
                )

        traceable = entry.key >= _key(TRACEABLE_SINCE)
        if traceable:
            if not entry.expected_effect:
                raise ChangelogError(
                    f"{where}：v{TRACEABLE_SINCE} 起预期效果必填"
                    "（量化：`D-3.5 分歧 12 → ≤4`；或 `逐位不变`）"
                )
            if not is_falsifiable(entry.expected_effect):
                raise ChangelogError(
                    f"{where}：预期效果不可证伪：{entry.expected_effect!r}"
                    "（没有分母与方向的说法，永远无法判它没达到）"
                )
            if not entry.disagreement_ids:
                raise ChangelogError(
                    f"{where}：v{TRACEABLE_SINCE} 起必须至少指一张分歧卡"
                    "（形如 v7:E006__2729220b__e9b9542b:d10）"
                )
        for card_id in entry.disagreement_ids:
            if not _CARD_ID_RE.match(card_id):
                raise ChangelogError(
                    f"{where}：分歧卡 id 形状不对：{card_id!r}"
                    "（期望 v{N}:{golden_id}:{dimension}）"
                )

        # 首版没有「被放宽」这回事，其余每一行都得说清判不了的情形。
        if idx > 0:
            if not entry.responsibility:
                raise ChangelogError(
                    f"{where}：责任区分声明必填——必须写清「准则是哪种情形下判不了」"
                )
            if is_excuse_only(entry.responsibility):
                raise ChangelogError(
                    f"{where}：责任区分声明只是个借口（去掉「评估器做不到」后没剩内容）"
                    "。要写的是哪种情形下判不了，不是谁做不到"
                )


def _key(version: str) -> tuple[int, int]:
    major, minor = version.split(".")
    return int(major), int(minor)


def versions(entries: Iterable[Entry]) -> list[str]:
    """出现过的 rubric 版本（去重保序）。"""
    seen: dict[str, None] = {}
    for entry in entries:
        seen.setdefault(entry.version, None)
    return list(seen)


def check_current(entries: Sequence[Entry], *, version: str = RUBRIC_VERSION) -> Entry:
    """``RUBRIC_VERSION`` 必须在变更记录里有一行；没有就抛。

    这条堵的是最常见的漂：**版本号升了、记录忘了写**。升版本本身是
    ``rubric_version.RUBRIC_VERSION`` 与文档版本行两处的事，第三处就是这张表。
    """
    hits = [e for e in entries if e.version == version]
    if not hits:
        raise ChangelogError(
            f"变更记录里没有 v{version} 这一版（RUBRIC_VERSION={version}）；"
            f"记录在的版本：{versions(entries)}"
        )
    return hits[-1]


def changed_clauses_between(
    entries: Sequence[Entry], before: str, after: str
) -> frozenset[str]:
    """``(before, after]`` 这一段里声称动过的条款（含 ``*``）。

    区间是**左开右闭**：``before`` 那一版的改动已经生效，不该再算进本轮。
    """
    if _key(before) >= _key(after):
        raise ChangelogError(
            f"rubric 版本没有前进（{before} → {after}），这一段没有改动可算"
        )
    out: set[str] = set()
    for entry in entries:
        if _key(before) < entry.key <= _key(after):
            out.update(entry.clauses)
    if not out:
        raise ChangelogError(
            f"变更记录里 {before} → {after} 之间一条改动都没有；"
            "版本升了却没有记录（先补表，再谈「没动别的」）"
        )
    return frozenset(out)


# ---------------------------------------------------------------------------
# 条款级 diff 守卫（C-18 的机械那一半）
# ---------------------------------------------------------------------------

def clause_scores(
    snapshot: Mapping[str, Any],
) -> dict[str, dict[tuple[str, str], dict[str, Any]]]:
    """快照 → ``{条款号: {(golden_id, dimension): {pred, gold}}}``。

    **按条目自己的 ``rubric_ref`` 归组**，不按维度重算：改 ``D-3.5`` 只该动
    落在 ``D-3.5`` 上的条目，其余档位同一维度必须一动不动。判不了条款号的
    条目（``rubric_ref`` 为空）归到 ``""`` 组——它们**同样要接受不变检查**，
    不能被悄悄跳过。
    """
    out: dict[str, dict[tuple[str, str], dict[str, Any]]] = {}
    for entry in snapshot.get("entries", []):
        clause = str(entry.get("rubric_ref") or "")
        key = (str(entry.get("golden_id") or ""), str(entry.get("dimension") or ""))
        out.setdefault(clause, {})[key] = {
            "pred": entry.get("pred_value"),
            "gold": entry.get("gold_value"),
        }
    return out


def _clause_matches(clause: str, changed: Iterable[str]) -> bool:
    """该条款是否被本轮改动覆盖（列了 ``D-3`` 覆盖 ``D-3.5``，反之亦然）。"""
    for item in changed:
        if item == WILDCARD:
            return True
        if item == clause or _base_of(item) == _base_of(clause):
            return True
    return False


def clause_invariance(
    before: Mapping[str, Any],
    after: Mapping[str, Any],
    *,
    changed: Iterable[str],
) -> dict[str, Any]:
    """「未列条款逐位不变」的机械判据（方案 16 §5.5 第 1 条）。

    输入两份快照与**本次改版声称动过的条款**；输出谁动了。

    - 动了的判据：同一 ``(golden_id, dimension)`` 的 ``pred_value`` 不相等；
    - **只在一版里出现的条目不算动**（两版之间加了 / 去了行是金标集的事），
      但计数单列，别让"配对少了"冒充"没动"；
    - **金标侧动了不算违规**：重标走的是 `gold_defect` 窗口（铁律 2），与 rubric
      改版是两条路；但它会改变 D-10 的分子，所以单列成 ``gold_moved`` 报出来。

    ⚠️ **继承式重冻结会让这条判据空跑**：``freeze --from-version`` 把上一版的行
    原样搬过来，pred 逐位相同是构造出来的，不是被证明的。这种情况
    ``vacuous`` 为真，报告里会写明——**空跑不等于通过**。
    """
    changed_set = frozenset(changed)
    b = clause_scores(before)
    a = clause_scores(after)

    moved: list[dict[str, Any]] = []
    gold_moved: list[dict[str, Any]] = []
    checked = 0
    only_before: list[str] = []
    only_after: list[str] = []
    enforced: set[str] = set()

    for clause in sorted(set(b) | set(a)):
        if _clause_matches(clause, changed_set):
            continue
        enforced.add(clause)
        bmap, amap = b.get(clause, {}), a.get(clause, {})
        for key in sorted(set(bmap) | set(amap)):
            ident = f"{key[0]}:{key[1]}"
            if key not in bmap:
                only_after.append(ident)
                continue
            if key not in amap:
                only_before.append(ident)
                continue
            checked += 1
            bp, ap = bmap[key]["pred"], amap[key]["pred"]
            if bp != ap:
                kind = (
                    "appeared" if bp is None
                    else "disappeared" if ap is None
                    else "changed"
                )
                moved.append({
                    "clause": clause,
                    "golden_id": key[0],
                    "dimension": key[1],
                    "before": bp,
                    "after": ap,
                    "kind": kind,
                })
            bg, ag = bmap[key]["gold"], amap[key]["gold"]
            if bg != ag:
                gold_moved.append({
                    "clause": clause,
                    "golden_id": key[0],
                    "dimension": key[1],
                    "before": bg,
                    "after": ag,
                })

    # 「一处都没动」要在**全部**条款上数（含本轮声称改过的那几条）：只有在连
    # 该动的都没动时，才能说这次 diff 什么也没看到——那通常是继承式重冻结。
    total_pairs = 0
    any_change = False
    for clause in set(b) | set(a):
        bmap, amap = b.get(clause, {}), a.get(clause, {})
        for key in set(bmap) & set(amap):
            total_pairs += 1
            if bmap[key] != amap[key]:
                any_change = True

    return {
        "changed": sorted(changed_set),
        "wildcard": WILDCARD in changed_set,
        "enforced_clauses": sorted(enforced),
        "available_clauses": sorted(set(b) | set(a)),
        "compared": checked,
        # 受检条款上一条都没配上 ⇒ **判不了**，不是通过。
        # 快照里只有本轮改过的那些条款时就会这样（0 条样本 ≠ 一切正常）。
        "measurable": checked > 0,
        "moved": moved,
        "gold_moved": gold_moved,
        "unpaired": {
            "only_before": sorted(only_before),
            "only_after": sorted(only_after),
            "count": len(only_before) + len(only_after),
        },
        # 一处都没动 ⇒ 极可能是继承式重冻结（pred 由构造而来，不是被证明）
        "vacuous": total_pairs > 0 and not any_change,
        "ok": not moved,
    }


# ---------------------------------------------------------------------------
# 引用的卡必须真的存在（C-12 的「指回」那一半）
# ---------------------------------------------------------------------------

def verify_card_ids(
    entries: Sequence[Entry], *, root: pathlib.Path | str
) -> list[tuple[str, str]]:
    """核对每张被引用的分歧卡在该版本的卡文件里真的存在。

    返回 ``[(card_id, 说明)]``；空列表表示全部核过。**卡文件缺了也报**——
    「核不了」与「核过了」不是一回事（同 ``n/a 不算通过``）。
    """
    by_version: dict[int, list[str]] = {}
    for entry in entries:
        for card_id in entry.disagreement_ids:
            by_version.setdefault(int(card_id.split(":", 1)[0][1:]), []).append(card_id)

    problems: list[tuple[str, str]] = []
    for version, wanted in sorted(by_version.items()):
        path = pathlib.Path(root) / f"v{version}" / "disagreements.jsonl"
        if not path.exists():
            for card_id in wanted:
                problems.append((
                    card_id,
                    f"核不了：{path} 不存在（先跑 agreement --version {version} "
                    "--emit-disagreements）",
                ))
            continue
        seen: set[str] = set()
        for line in path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            try:
                seen.add(str(json.loads(line).get("disagreement_id")))
            except json.JSONDecodeError as exc:  # pragma: no cover - 坏文件
                raise ChangelogError(f"{path} 不是 JSONL：{exc}") from exc
        for card_id in wanted:
            if card_id not in seen:
                problems.append((card_id, f"该版本卡文件里没有这一条（{path}）"))
    return problems


# ---------------------------------------------------------------------------
# 渲染
# ---------------------------------------------------------------------------

def render_check(entries: Sequence[Entry], *, version: str = RUBRIC_VERSION) -> str:
    current = check_current(entries, version=version)
    lines = [
        f"rubric 变更记录 · {len(entries)} 行 / {len(versions(entries))} 个版本"
        f" · 当前 RUBRIC_VERSION = {version}",
        f"追溯线 TRACEABLE_SINCE = {TRACEABLE_SINCE}"
        f"（自该版起：预期效果 / 分歧卡 / 责任区分声明必填）",
        "",
    ]
    for entry in entries:
        head = f"v{entry.version}"
        if entry.label:
            head += f"（{entry.label}）"
        clauses = ", ".join(entry.clauses)
        kinds = ", ".join(entry.kinds)
        lines.append(f"  {head:<24} {kinds:<14} {clauses}")
        if entry.expected_effect:
            lines.append(f"    └ 预期：{entry.expected_effect}")
        if entry.disagreement_ids:
            lines.append(f"    └ 分歧卡：{', '.join(entry.disagreement_ids)}")
    lines.append("")
    lines.append(
        f"最新一行：v{current.version}"
        f"{'（' + current.label + '）' if current.label else ''}"
        f" · 条款 {', '.join(current.clauses)}"
    )
    return "\n".join(lines)


def render_audit(report: Mapping[str, Any]) -> str:
    before, after = report["before"], report["after"]
    lines = [
        f"条款级不变守卫 · v{before['version']}（rubric {before['rubric_version']}）"
        f" → v{after['version']}（rubric {after['rubric_version']}）",
        f"本轮声称改过：{', '.join(report['changed'])}",
        f"受检条款：{len(report['enforced_clauses'])} 个 · 配对条目 {report['compared']} 条",
        f"两版实际出现过的条款：{', '.join(report['available_clauses']) or '（无）'}",
    ]
    unpaired = report["unpaired"]
    if unpaired["count"]:
        lines.append(
            f"⚠️ 未配对 {unpaired['count']} 条（只在旧版 {len(unpaired['only_before'])}"
            f" / 只在新版 {len(unpaired['only_after'])}）——它们不参与判定"
        )
    if report["vacuous"]:
        lines.append(
            "⚠️ 一处都没动：若本轮是 `freeze --from-version` 继承行，这条守卫是"
            "**空跑**（pred 由构造而来，不是被证明）。空跑不等于通过"
        )
    if report["gold_moved"]:
        lines.append(
            f"ℹ️ 金标侧动了 {len(report['gold_moved'])} 条——不算违规（重标走"
            "gold_defect 窗口），但它会改 D-10 分子"
        )
        for item in report["gold_moved"][:5]:
            lines.append(
                f"    {item['clause']} {item['golden_id']}:{item['dimension']}"
                f" {item['before']!r} → {item['after']!r}"
            )
    if not report["measurable"]:
        lines.append(
            "⚠️ 受检条款上一条都没配上 ⇒ **这条判据这次没测到东西**"
            "（不是通过：0 条样本 ≠ 一切正常）"
        )
    elif report["moved"]:
        lines.append(f"✗ 未列条款里有 {len(report['moved'])} 条分数动了：")
        for item in report["moved"][:20]:
            lines.append(
                f"    {item['clause']} {item['golden_id']}:{item['dimension']}"
                f" {item['before']!r} → {item['after']!r}（{item['kind']}）"
            )
        if len(report["moved"]) > 20:
            lines.append(f"    … 还有 {len(report['moved']) - 20} 条")
    else:
        lines.append("✓ 未列条款逐位不变")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

#: 派生快照根。与 ``verify.DEFAULT_SNAPSHOT_ROOT`` / ``builder.PIPELINE_ROOT``
#: 是同一个位置——**不在模块顶层 import 它们**：本模块的解析半边要能在契约
#: 测试里零依赖加载（``builder`` 会拖进整个报表模块）。抄一份常量有漂的风险，
#: 所以由 ``test_派生根与verify同源`` 钉住三者相等。
DEFAULT_CALIBRATION_ROOT = (
    pathlib.Path(__file__).resolve().parents[2]
    / "output"
    / "pipeline"
    / "calibration"
)


def main(argv: list[str] | None = None) -> int:
    import argparse

    # stderr 也重设：本命令最重要的输出恰恰是**退 2 时那几句「测不成」**，
    # 而 GBK 控制台下不重设就是乱码（``verify`` / ``gate`` 只设了 stdout）。
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
        except (AttributeError, ValueError):
            pass

    ap = argparse.ArgumentParser(
        prog="rubric_changelog",
        description=(
            "rubric 变更记录契约（方案 16 C-12 / C-18）："
            "check = 校验文档里的表；audit = 两版快照的条款级不变守卫"
        ),
    )
    sub = ap.add_subparsers(dest="cmd", required=True)

    p_check = sub.add_parser("check", help="校验变更记录（条款 / 类型 / 预期 / 卡 / 声明）")
    p_check.add_argument(
        "--version", default=RUBRIC_VERSION, help=f"当前版本（默认 {RUBRIC_VERSION}）"
    )
    p_check.add_argument(
        "--verify-cards",
        action="store_true",
        help="顺带核对被引用的分歧卡在对应版本的卡文件里真的存在",
    )
    p_check.add_argument(
        "--calibration-root",
        default="",
        help="派生根（配 --verify-cards；默认 output/pipeline/calibration）",
    )

    p_audit = sub.add_parser("audit", help="两版快照 → 未列条款是否逐位不变")
    p_audit.add_argument("--before", type=int, required=True, help="基线版本号")
    p_audit.add_argument("--after", type=int, required=True, help="本轮版本号")
    p_audit.add_argument(
        "--calibration-root",
        default=str(DEFAULT_CALIBRATION_ROOT),
        help="派生快照根",
    )
    p_audit.add_argument("--json-out", default="", help="报告落成 JSON")

    args = ap.parse_args(argv)

    try:
        entries = parse_entries()
        validate_entries(entries)
    except ChangelogError as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return 2

    if args.cmd == "check":
        try:
            print(render_check(entries, version=args.version))
            if args.verify_cards:
                root = args.calibration_root or str(DEFAULT_CALIBRATION_ROOT)
                problems = verify_card_ids(entries, root=root)
                if problems:
                    for card_id, why in problems:
                        print(f"错误：分歧卡 {card_id} {why}", file=sys.stderr)
                    return 2
                cited = sum(len(e.disagreement_ids) for e in entries)
                if not cited:
                    # 一条卡号都没引：核过 0 条不等于核过了。追溯线之前的老行
                    # 本来就该是 `—`，所以这里**不判红**（否则 1.3 之前是死门），
                    # 但绝不印 ✓——那会把「没得核」读成「全对得上」。
                    print(
                        "⚠ 没有任何卡号可核（追溯线 "
                        f"{TRACEABLE_SINCE} 之前的行允许填 `—`）"
                        "——**核不了 ≠ 核过了**"
                    )
                else:
                    print(f"✓ 引用的分歧卡全部存在（核了 {cited} 条）")
        except ChangelogError as exc:
            print(f"错误：{exc}", file=sys.stderr)
            return 2
        return 0

    return _run_audit(args, entries)


def _run_audit(args: Any, entries: Sequence[Entry]) -> int:
    """两版快照 → 条款级不变守卫。退出码 **0 通过 / 1 驳回 / 2 测不成**。"""
    from trajectory_pipeline.evaluation.calibration.snapshot import load_snapshot

    root = pathlib.Path(args.calibration_root)
    snaps: dict[str, dict[str, Any]] = {}
    for side, version in (("before", args.before), ("after", args.after)):
        path = root / f"v{version}" / "snapshot.json"
        if not path.exists():
            print(
                f"错误：{side} 快照不存在：{path}\n"
                f"先跑 agreement --version {version} --emit-disagreements",
                file=sys.stderr,
            )
            return 2
        try:
            snaps[side] = load_snapshot(path)
        except (OSError, ValueError) as exc:
            print(f"错误：读 {side} 快照失败：{exc}", file=sys.stderr)
            return 2

    meta = {side: (snap.get("meta") or {}) for side, snap in snaps.items()}
    schema = {side: str(snaps[side].get("schema_version") or "") for side in snaps}
    if schema["before"] != schema["after"]:
        print(
            f"错误：两版快照 schema 不同（{schema['before']} vs {schema['after']}），"
            "不可比",
            file=sys.stderr,
        )
        return 2

    rv_before = str(meta["before"].get("rubric_version") or "")
    rv_after = str(meta["after"].get("rubric_version") or "")
    if not rv_before or not rv_after:
        print(
            "错误：快照里没有 rubric_version（meta 缺字段）——无从判断这一轮改没改尺子",
            file=sys.stderr,
        )
        return 2
    if rv_before == rv_after:
        print(
            f"错误：两版 rubric 版本相同（{rv_after}），本轮没有尺子改动，"
            "这条守卫无从判定\n"
            "（`n/a 不算通过`：要说「没动别的条款」，得先有一次真正的改版）",
            file=sys.stderr,
        )
        return 2

    try:
        changed = changed_clauses_between(entries, rv_before, rv_after)
    except ChangelogError as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return 2

    report = clause_invariance(snaps["before"], snaps["after"], changed=changed)
    report["before"] = {
        "version": args.before,
        "rubric_version": rv_before,
    }
    report["after"] = {
        "version": args.after,
        "rubric_version": rv_after,
    }
    print(render_audit(report))
    if args.json_out:
        out = pathlib.Path(args.json_out)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(report, ensure_ascii=False, indent=1), encoding="utf-8")
        print(f"→ 报告已写 {out}")
    if not report["measurable"]:
        print(
            "错误：受检条款上一条都没配上，这条判据**测不成**"
            f"（快照里出现过的条款：{', '.join(report['available_clauses']) or '无'}；"
            f"本轮改过：{', '.join(report['changed'])}）。\n"
            "0 条样本 ≠ 一切正常——`n/a 不算通过`",
            file=sys.stderr,
        )
        return 2
    return 0 if report["ok"] else 1


__all__ = [
    "BASE_CLAUSES",
    "BATCH_CLAUSES",
    "CHANGE_KINDS",
    "COLUMNS",
    "DEFAULT_CALIBRATION_ROOT",
    "ChangelogError",
    "EMPTY_CELL",
    "EXCUSE_PHRASES",
    "Entry",
    "GLOBAL_CLAUSES",
    "KIND_ADD",
    "KIND_DEPRECATE",
    "KIND_RETIER",
    "KIND_REWEIGHT",
    "KIND_REWORD",
    "KNOWN_CLAUSES",
    "SAMPLE_CLAUSES",
    "TIER_CLAUSES",
    "TRACEABLE_SINCE",
    "WEIGHT_TABLE_EXISTS",
    "WILDCARD",
    "changed_clauses_between",
    "check_current",
    "clause_invariance",
    "clause_scores",
    "is_excuse_only",
    "is_falsifiable",
    "known_clauses",
    "main",
    "parse_entries",
    "render_audit",
    "render_check",
    "validate_entries",
    "verify_card_ids",
    "versions",
]


if __name__ == "__main__":
    raise SystemExit(main())