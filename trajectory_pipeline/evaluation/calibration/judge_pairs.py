"""judge 逐行读数 → T-2 / T-3 的配对输入（台账 **M-56**）。

为什么要有这一层转换
--------------------
:func:`~...calibration.triggers.load_judge_pairs` 要的是**平铺行**::

    {"golden_id": ..., "dimension": ..., "score": ..., "reasons": [...]}

而今天能产出的 judge 逐行读数有**两份产物，两份都不是那个形状**：

1. ``evaluation.cli --include-llm-judge --json`` ⇒ ``{"summary", "samples"}``，
   维度 verdict 挂在**样本行**的 ``row["d3"]`` 上，行键是 ``sample_id``；
2. ``evaluation.scout`` 三件套 ⇒ 维度 verdict 挂在**嵌套**的
   ``row["scores"] = [{dimension, score, reasons}]`` 上，行键也是 ``sample_id``。

⇒ **三处对不上**：JSON 键（``samples`` ≠ ``rows``）· 粒度（嵌套 ≠ 平铺）·
键值（``sample_id`` ≠ ``golden_id``）。``triggers`` 的读法是 fail-closed 的：
缺 ``golden_id`` 直接抛，**不按 url / sample_id 猜一个键出来**——猜出来的配对
会让 kappa 建在一个配不上的分母上，而 kappa 对配对样本量极敏感
（``triggers._judge_gap`` 的两条理由）。

这一层做且只做三件事
--------------------
1. **把 judge 产物摊平成「一行一个 (golden_id, dimension)」**；
2. **把行键换成 ``golden_id``**——按 **(task_id, run_id, url)** 与冻结档对上，
   **不是**按 ``sample_id`` 猜：``golden_id`` 的第三段是 ``md5(url)[:8]``
   （``golden/builder.py::_url_hash``），而冻结档每行都带 ``url``
   ⇒ 对得上就是对得上，对不上就**报出来**（不硬凑、不丢）；
3. **报出「配上了但金标侧还没数」的那些维度**——那才是 T-2 剩下的阻塞：
   d3 / d9 的**金标**还没填 ⇒ 配对行落盘了也算不出 kappa。

⚠️ **只收 LLM judge 的两个维度**（``JUDGE_DIMS``）：同一份产物里还有
d2 / d4 / d5 / d6（**代码** judge），把它们一并写进配对文件 = 把两个评分者
混进同一份读数 ⇒ 报表上出现「两个评分者的分歧率」而没有额外信号 ⇒ 违反
CLAUDE.md 的「禁止投票/集成」。代码 judge 的读数走另一条线（``agreement``）。

⚠️ **这个模块不跑 judge、也不填金标**：judge 要 LLM（生产链上默认关，见
``evaluation/cli.py::LLM_SCORERS``），金标要人。它只做形状转换与对账。

跑法（**必须在仓库根跑**）::

    python -m trajectory_pipeline.evaluation.calibration.judge_pairs \\
        --report trajectory_pipeline/output/pipeline/evaluation/judge_v16.json \\
        --version 16
"""

from __future__ import annotations

import argparse
import json
import pathlib
import sys
from dataclasses import dataclass, field
from typing import Any, Mapping, Sequence

from trajectory_pipeline.evaluation.calibration.worklist import (
    WorklistError,
    load_frozen_rows,
)
from trajectory_pipeline.evaluation.golden.builder import GOLDEN_ROOT

#: LLM judge 的两个维度——只有 ``--include-llm-judge`` 才产出。
#:
#: ⚠️ **不要把 d2 / d4 / d5 / d6 收进来**：那些是**代码** judge，混进同一份
#: 配对文件 = 两个评分者混成一份读数（禁止投票/集成）。
JUDGE_DIMS: tuple[str, ...] = ("d3", "d9")

#: 配对文件的缺省名（落在 ``v{N}/`` 下，与快照 / 分歧卡 / triggers 挨着）。
PAIRS_FILE = "judge_pairs.jsonl"

#: 转换报告的缺省名。
REPORT_FILE = "judge_pairs.report.json"


class JudgePairError(Exception):
    """judge 读数没法转成配对行（→ 退 2）。**不是驳回**：是形状不对。"""


@dataclass(frozen=True)
class Conversion:
    """一次转换的结果——**配对行 + 配不上的账 + 金标侧还缺什么**。"""

    #: 平铺配对行（``golden_id`` / ``dimension`` / ``score`` / ``reasons``）。
    pairs: tuple[dict[str, Any], ...]
    #: 配上了的行数（**行**，不是配对行数）。
    matched: int
    #: 配不上的行——每条带**为什么**（不静默丢）。
    unmatched: tuple[dict[str, Any], ...]
    #: 有 verdict 但**没有分**的行（judge 判不了 / 未评估）——单列，不写成配对行。
    no_score: tuple[dict[str, Any], ...]
    #: 非 judge 维度被跳过的条数（``{"d2": 128, ...}``）——**为什么跳**要说清。
    skipped_dims: dict[str, int]
    #: 每个维度上金标侧的状态：``{"d3": {"有分": 0, "判不了": 0, "缺这一维": 46}}``。
    gold_side: dict[str, dict[str, int]] = field(default_factory=dict)

    def summary(self) -> dict[str, Any]:
        """给报表 / JSON 用的计数视图。"""
        return {
            "pairs": len(self.pairs),
            "matched_rows": self.matched,
            "unmatched_rows": len(self.unmatched),
            "no_score_rows": len(self.no_score),
            "skipped_dims": dict(self.skipped_dims),
            "gold_side": {d: dict(v) for d, v in self.gold_side.items()},
        }


# ---------------------------------------------------------------------------
# 读产物
# ---------------------------------------------------------------------------


def dim_name(raw: Any) -> str:
    """``"D-3"`` / ``"d3"`` / ``3`` → ``"d3"``。认不出就抛（fail-closed）。

    ⚠️ **两种写法都要认**：评分器的 ``to_json()`` 写的是 ``"D-3"``（带横杠），
    而配置与真源里写的是 ``"d3"`` ⇒ 只认一种就会把另一半判成「维度名认不出」。
    """
    text = str(raw or "").strip()
    if not text:
        raise JudgePairError("维度名是空的")
    lowered = text.lower().replace("-", "").replace("_", "")
    if lowered.startswith("d"):
        lowered = lowered[1:]
    if not lowered.isdigit():
        raise JudgePairError(f"认不出的维度名 {raw!r}")
    return f"d{lowered}"


def read_report(path: pathlib.Path | str) -> list[dict[str, Any]]:
    """读 judge 产物，返回**样本行**列表。

    认三种外形（都是这棵树里真会产出的）：

    - ``{"samples": [...]}``——``evaluation.cli --json`` 的写法；
    - ``{"rows": [...]}``——已经是平铺行的写法；
    - ``[...]``——裸数组。

    ⚠️ **JSONL 也认**：逐行解析，第一行是包装对象就取包装里的列表，
    否则每一行都是一条样本行。
    """
    p = pathlib.Path(path)
    if not p.exists():
        raise JudgePairError(f"judge 读数文件不存在：{p}")
    raw = p.read_text(encoding="utf-8")
    try:
        data: Any = json.loads(raw)
    except json.JSONDecodeError:
        rows: list[dict[str, Any]] = []
        for i, line in enumerate(raw.splitlines(), 1):
            if not line.strip():
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError as exc:
                raise JudgePairError(f"{p} 第 {i} 行不是 JSON：{exc}") from exc
            if not isinstance(obj, Mapping):
                raise JudgePairError(f"{p} 第 {i} 行不是对象")
            rows.append(dict(obj))
        if rows and isinstance(rows[0], Mapping):
            inner = _rows_of(rows[0])
            if inner is not None and len(rows) == 1:
                return inner
        return rows

    inner = _rows_of(data) if isinstance(data, Mapping) else None
    if inner is not None:
        return inner
    if isinstance(data, list):
        return [dict(r) for r in data if isinstance(r, Mapping)]
    raise JudgePairError(f"{p} 既不是含 samples/rows 的对象，也不是数组")


def _rows_of(data: Mapping[str, Any]) -> list[dict[str, Any]] | None:
    """包装对象里取样本行（``samples`` / ``rows`` 两种键都认）。"""
    for key in ("samples", "rows"):
        value = data.get(key)
        if isinstance(value, list):
            return [dict(r) for r in value if isinstance(r, Mapping)]
    return None


def verdicts_of(row: Mapping[str, Any]) -> list[tuple[str, Mapping[str, Any]]]:
    """一份样本行里的 ``(维度, verdict)``。

    两种形状都认：

    - ``row["d3"] = {...}``——``evaluation.cli --json`` 的写法；
    - ``row["scores"] = [{"dimension": "D-3", ...}]``——scout 三件套的写法。

    ⚠️ **维度名以 verdict 自带的为准**（``row["scores"]`` 里每项带
    ``dimension``），顶层键只是**候选维度名**——两处都认，取自带的。
    """
    out: list[tuple[str, Mapping[str, Any]]] = []
    nested = row.get("scores")
    if isinstance(nested, Sequence) and not isinstance(nested, (str, bytes)):
        for item in nested:
            if not isinstance(item, Mapping):
                continue
            raw = item.get("dimension")
            if raw is None:
                continue
            out.append((dim_name(raw), item))
        return out
    for key, value in row.items():
        if not isinstance(value, Mapping):
            continue
        try:
            name = dim_name(key)
        except JudgePairError:
            continue
        out.append((name, value))
    return out


# ---------------------------------------------------------------------------
# 配对键
# ---------------------------------------------------------------------------


def row_key(row: Mapping[str, Any]) -> tuple[str, str, str]:
    """一份样本行的配对键 ``(task_id, run_id, url)``。

    ``task_id`` / ``run_id`` 从行上的字段取；没有就**从 ``sample_id`` 拆**
    （``<task_id>__<run_id>__<unit>``，见 ``assembler.schema.sample_id``）。
    ⚠️ **url 不许猜**：没有就是没有 ⇒ 那一行配不上，报出来。
    """
    task = str(row.get("task_id") or "")
    run = str(row.get("run_id") or "")
    if (not task or not run) and row.get("sample_id"):
        parts = str(row["sample_id"]).split("__")
        if len(parts) >= 2:
            task = task or parts[0]
            run = run or parts[1]
    url = str(row.get("url") or "")
    return task, run, url


def build_index(
    rows: Sequence[Mapping[str, Any]],
) -> tuple[dict[tuple[str, str, str], str], dict[str, Mapping[str, Any]]]:
    """冻结档 ⇒ ``(task_id, run_id, url) → golden_id`` 与 ``golden_id → 行``。

    ⚠️ **只收站点级行**（``source_kind != "p2_sample"``）：judge 逐行读数是
    **站点级**的（``split_archive`` 切出来的 site 单元），轨迹级样本的
    ``golden_id`` 是另一个命名空间（``<task>__<run>__<unit>``）⇒ 混进来
    会让同一个 url 挂上两个键，配对时不知道该选哪个。
    """
    index: dict[tuple[str, str, str], str] = {}
    by_gid: dict[str, Mapping[str, Any]] = {}
    for row in rows:
        if str(row.get("source_kind") or "") == "p2_sample":
            continue
        gid = str(row.get("golden_id") or "")
        if not gid:
            continue
        by_gid[gid] = row
        key = (
            str(row.get("task_id") or ""),
            str(row.get("run_id") or ""),
            str(row.get("url") or ""),
        )
        index.setdefault(key, gid)
    return index, by_gid


def _gold_state(
    row: Mapping[str, Any] | None, dimension: str
) -> str:
    """金标侧这一格的状态：``有分`` / ``判不了`` / ``缺这一维``。"""
    if row is None:
        return "缺这一维"
    golden = row.get("golden")
    scores = golden.get("dimension_scores") if isinstance(golden, Mapping) else None
    if not isinstance(scores, Mapping) or dimension not in scores:
        return "缺这一维"
    value = scores.get(dimension)
    return "判不了" if value is None else "有分"


# ---------------------------------------------------------------------------
# 转换
# ---------------------------------------------------------------------------


def convert(
    report_rows: Sequence[Mapping[str, Any]],
    index: Mapping[tuple[str, str, str], str],
    rows_by_gid: Mapping[str, Mapping[str, Any]],
    *,
    dimensions: Sequence[str] = JUDGE_DIMS,
) -> Conversion:
    """judge 产物 ⇒ 配对行（+ 配不上的账 + 金标侧的账）。

    ⚠️ **三处 fail-closed**：

    1. **配不上的行不丢**：进 ``unmatched``，每条带为什么；
    2. **没有分的行不写成配对行**：``score=None`` 落进配对文件后，
       ``triggers.t2_judge_human`` 会在 ``float("None")`` 上抛 ``ValueError``
       ⇒ **整个维度被静默跳过**（读数上看起来像「这个维度没分歧」）⇒
       这里单列成 ``no_score``，不混进 ``pairs``；
    3. **非 judge 维度不收**：单列成 ``skipped_dims``。
    """
    wanted = {d for d in dimensions}
    pairs: list[dict[str, Any]] = []
    unmatched: list[dict[str, Any]] = []
    no_score: list[dict[str, Any]] = []
    skipped: dict[str, int] = {}
    gold_side: dict[str, dict[str, int]] = {}
    matched = 0

    for row in report_rows:
        sample_id = str(row.get("sample_id") or "")
        verdicts = verdicts_of(row)
        row_gid = str(row.get("golden_id") or "")
        key = row_key(row)
        gid = row_gid or index.get(key, "")
        if not row_gid and not gid:
            if not key[2]:
                unmatched.append(
                    {
                        "sample_id": sample_id,
                        "reason": (
                            "行里没有 url ⇒ 配对键缺一块（golden_id 的第三段是 "
                            "url 的 md5，猜不得）"
                        ),
                    }
                )
            else:
                unmatched.append(
                    {
                        "sample_id": sample_id,
                        "url": key[2],
                        "reason": (
                            f"冻结档里没有 (task={key[0]}, run={key[1]}, "
                            f"url={key[2]}) 这一行 ⇒ 配不上"
                        ),
                    }
                )
            continue
        matched += 1
        for dimension, verdict in verdicts:
            if dimension not in wanted:
                skipped[dimension] = skipped.get(dimension, 0) + 1
                continue
            score = verdict.get("score")
            reasons = verdict.get("reasons") or ()
            if isinstance(reasons, str):
                reasons = [reasons]
            if score is None:
                no_score.append(
                    {
                        "sample_id": sample_id,
                        "golden_id": gid,
                        "dimension": dimension,
                        "kind": str(verdict.get("kind") or ""),
                    }
                )
                continue
            pairs.append(
                {
                    "golden_id": gid,
                    "dimension": dimension,
                    "score": score,
                    "reasons": [str(r) for r in reasons],
                }
            )
            state = _gold_state(rows_by_gid.get(gid), dimension)
            gold_side.setdefault(dimension, {})
            gold_side[dimension][state] = gold_side[dimension].get(state, 0) + 1

    return Conversion(
        pairs=tuple(pairs),
        matched=matched,
        unmatched=tuple(unmatched),
        no_score=tuple(no_score),
        skipped_dims=skipped,
        gold_side=gold_side,
    )


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def default_pairs_path(version: int) -> pathlib.Path:
    from trajectory_pipeline.evaluation.golden.builder import PIPELINE_ROOT

    return PIPELINE_ROOT / "calibration" / f"v{version}" / PAIRS_FILE


def _cmd_convert(args: argparse.Namespace) -> int:
    version = args.version
    try:
        rows = load_frozen_rows(args.golden_root, version)
    except WorklistError as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return 2
    index, rows_by_gid = build_index(rows)
    report_rows = read_report(args.report)
    dims = [d.strip().lower() for d in args.dimensions.split(",") if d.strip()]
    result = convert(report_rows, index, rows_by_gid, dimensions=dims)

    out = pathlib.Path(args.out) if args.out else default_pairs_path(version)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(
        "\n".join(json.dumps(p, ensure_ascii=False, sort_keys=True) for p in result.pairs)
        + ("\n" if result.pairs else ""),
        encoding="utf-8",
    )

    report = {
        "version": version,
        "report_source": str(args.report),
        "pairs_file": str(out),
        "dimensions": dims,
        **result.summary(),
        "unmatched": list(result.unmatched[:50]),
        "no_score": list(result.no_score[:50]),
    }
    report_path = out.with_name(REPORT_FILE)
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=1), encoding="utf-8")

    print(f"judge 产物 {len(report_rows)} 行 · 配对行 {len(result.pairs)}")
    print(f"  配上的样本行 {result.matched} · 配不上 {len(result.unmatched)}"
          f" · 有 verdict 无分 {len(result.no_score)}")
    if result.skipped_dims:
        print(f"  跳过的非 judge 维度（不收：混进来 = 两个评分者混成一份读数）："
              f"{result.skipped_dims}")
    for dimension, states in sorted(result.gold_side.items()):
        print(f"  {dimension} 金标侧：{states}")
    if result.unmatched:
        print("  ⚠ 配不上的行（前 5 条）：")
        for item in result.unmatched[:5]:
            print(f"    {item.get('sample_id') or '(无 sample_id)'}：{item['reason']}")
    print(f"\n→ {out}")
    print(f"→ {report_path}")
    print(
        "下一步：`triggers --version "
        f"{version} --judge-report {out}`"
        "（T-2 需要金标侧那一格也有数——上面「金标侧」那行写着还缺多少）"
    )
    return 0


def main(argv: list[str] | None = None) -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
        sys.stderr.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
    except (AttributeError, ValueError):
        pass
    ap = argparse.ArgumentParser(
        prog="judge_pairs",
        description=(
            "judge 逐行读数 → T-2 / T-3 的配对行（M-56）："
            "把 judge 产物摊平成带 golden_id 的平铺行"
        ),
    )
    ap.add_argument("--report", required=True, help="judge 逐行读数（JSON / JSONL）")
    ap.add_argument("--version", type=int, default=16, help="冻结版本号（配对与金标侧都从它取）")
    ap.add_argument("--golden-root", default=str(GOLDEN_ROOT), help="黄金集根（缺省包内 data/）")
    ap.add_argument("--out", default="", help=f"配对文件（缺省 v{{N}}/{PAIRS_FILE}）")
    ap.add_argument("--dimensions", default=",".join(JUDGE_DIMS),
                    help=f"只收这些维度（缺省 {','.join(JUDGE_DIMS)}；代码 judge 的维度不收）")
    args = ap.parse_args(argv)

    try:
        return _cmd_convert(args)
    except JudgePairError as exc:
        print(f"错误：{exc}", file=sys.stderr)
        print("退出码 2（读数没跑起来）：这不是「驳回」，是形状不对", file=sys.stderr)
        return 2


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())


__all__ = [
    "JUDGE_DIMS",
    "PAIRS_FILE",
    "REPORT_FILE",
    "Conversion",
    "JudgePairError",
    "build_index",
    "convert",
    "default_pairs_path",
    "dim_name",
    "main",
    "read_report",
    "row_key",
    "verdicts_of",
]
