"""分诊档：校验与读写（方案 16 §3.1 / §4 / C-05）。

分诊 = 人给一张分歧卡贴一个桶。三条硬约束，三条都是防放水：

1. **桶来自六桶封闭集**（:mod:`buckets`，D-C2）：没有 ``other``，不认识就留空；
2. **``triage_evidence`` 必填**：写的是「为什么**不是别的桶**」。
   只写"判错了"等于没写——它挡不住"随手贴最近的那个桶"；
3. **``bucket`` 只能人写**（D-C3）：``rubric_*`` / ``gold_*`` 是尺子与真值的
   裁定；agent 至多写建议值（卡上是 ``suggested_*``），写进 ``bucket`` 一律拒。

资产 vs 派生（D-C1）
--------------------
分歧卡是**派生数据**（可重建，落 ``output/pipeline/calibration/``）；
分诊档是**人工裁定**（不可重建，落 ``evaluation/golden/data/triage/``，入库）。
所以改判的留痕靠版本控制：``add`` 默认 dry-run，覆盖已有桶要显式
``--replace``——**默认拒绝覆盖**，因为"手滑改了别人贴的桶"是这类文件最常见的损坏。
"""

from __future__ import annotations

import argparse
import datetime as _dt
import json
import pathlib
import sys
from typing import Any, Mapping, Sequence

from trajectory_pipeline.evaluation.calibration import buckets as buckets_mod
from trajectory_pipeline.evaluation.calibration.snapshot import HARD_DIM
from trajectory_pipeline.evaluation.golden.alignment import SOFT_DIM_NAMES
from trajectory_pipeline.evaluation.golden.builder import GOLDEN_ROOT

#: 人填分诊档的缺省位置（D-C1：**入版本控制**，与派生卡分开放）。
DEFAULT_TRIAGE_ROOT = GOLDEN_ROOT / "triage"
DEFAULT_TRIAGE_FILE = DEFAULT_TRIAGE_ROOT / "triage.jsonl"

#: 一条分诊至少要有这五个字段（其余字段可以原样透传）。
REQUIRED_FIELDS: tuple[str, ...] = (
    "golden_id",
    "dimension",
    "bucket",
    "triage_by",
    "triage_evidence",
)

#: 状态机（§3.3）。分诊写入的桶意味着这张卡已从 ``open`` 走到 ``assigned``。
STATUS_OPEN = "open"
STATUS_ASSIGNED = "assigned"
STATUSES: tuple[str, ...] = (
    STATUS_OPEN,
    STATUS_ASSIGNED,
    "fixed_pending_verify",
    "verified",
    "reopened",
    "accepted_exception",
    "escalated",
)

#: 机器身份的**显式名单**：这些 id 永远不许写 ``bucket``（D-C3）。
AGENT_ACTORS: frozenset[str] = frozenset(
    {"agent", "llm", "judge", "bot", "auto", "system", "model", "machine"}
)
#: 带命名空间的机器 id，如 ``agent:coder`` / ``llm:qwen``。
AGENT_PREFIXES: tuple[str, ...] = (
    "agent:", "agent/", "llm:", "llm/", "model:", "model/", "bot:", "auto:",
)


class TriageError(Exception):
    """分诊档不合法（→ 调用方退 2）。**不是驳回**：这是输入问题，不是结论。"""


# --------------------------------------------------------------------------
# 身份
# --------------------------------------------------------------------------


def actor_kind(actor: Any) -> str:
    """``"human"`` / ``"agent"``。空值与机器名单见下。

    只能按**名单 + 前缀**判，不能按"看起来像模型名"猜：猜错的代价是两个方向都坏
    ——把 machine 判成人 = 放水；把人的名字判成 machine = 分诊写不进来。
    所以机器侧必须显式列（``agent`` / ``llm:qwen`` 这类），其余一律当人。
    """
    s = str(actor or "").strip().lower()
    if not s:
        raise TriageError(
            "triage_by 不能为空：分诊要有签名。匿名的桶与没贴桶一样不可追溯"
        )
    if s in AGENT_ACTORS or s.startswith(AGENT_PREFIXES):
        return "agent"
    return "human"


# --------------------------------------------------------------------------
# 校验
# --------------------------------------------------------------------------


def _text(value: Any) -> str:
    return str(value if value is not None else "").strip()


def _validate_dimension(dimension: str) -> str:
    if dimension == HARD_DIM or dimension in SOFT_DIM_NAMES:
        return dimension
    raise TriageError(
        f"dimension {dimension!r} 不认识：硬维度只有 {HARD_DIM}，"
        f"软维度白名单 {sorted(SOFT_DIM_NAMES)}。"
        "（写错大小写最常见：`D10` 与实际键 `d10` 不是一个东西，"
        "它不会报错，只会一条都配不上）"
    )


def validate_row(row: Mapping[str, Any]) -> dict[str, Any]:
    """校验并规范化一条分诊。返回**新 dict**，不改入参。"""
    if not isinstance(row, Mapping):
        raise TriageError(f"分诊行必须是对象（JSON object），拿到 {type(row).__name__}")
    out: dict[str, Any] = dict(row)
    for key, value in list(out.items()):
        if isinstance(value, str):
            out[key] = value.strip()

    # 签名先单独判：它空着的时候，"缺字段"那句会把「分诊要有签名」吞掉，
    # 而这两件事对填表人的含义不同。
    by_kind = actor_kind(out.get("triage_by"))
    missing = [
        f for f in REQUIRED_FIELDS if f != "triage_by" and not _text(out.get(f))
    ]
    if missing:
        raise TriageError(
            f"分诊行缺字段 {missing}：{row!r}\n"
            "必填五项 = golden_id / dimension / bucket / triage_by / triage_evidence。"
            "`triage_evidence` 写的是「为什么不是别的桶」——没有它就只剩「顺手贴了一个」"
        )

    bucket = _text(out["bucket"])
    try:
        buckets_mod.require_bucket(bucket)
    except KeyError as exc:
        raise TriageError(str(exc.args[0])) from exc
    out["bucket"] = bucket

    out["dimension"] = _validate_dimension(_text(out["dimension"]))
    out["golden_id"] = _text(out["golden_id"])

    by = _text(out["triage_by"])
    if by_kind == "agent":
        note = (
            "（D-C3：rubric_* / gold_* 是尺子与真值的裁定）"
            if buckets_mod.is_human_only(bucket)
            else ""
        )
        raise TriageError(
            f"bucket 只能人填，`triage_by={by}` 是机器身份{note}。"
            "agent 只能写建议值（卡上的 suggested_*），由人确认后才进 bucket"
        )
    out["triage_by"] = by

    evidence = _text(out["triage_evidence"])
    out["triage_evidence"] = evidence

    # 责任方由桶推导，不许手填第二份（填错也不会报错，只会让 C-07 的分布错位）。
    derived = buckets_mod.owner_of(bucket)
    given = _text(out.get("responsibility"))
    if given and given != derived:
        raise TriageError(
            f"responsibility={given!r} 与桶 {bucket!r} 的责任方 {derived!r} 不一致："
            "责任方由桶推导（§4），不是自由填写项"
        )
    out["responsibility"] = derived

    status = _text(out.get("status")) or STATUS_ASSIGNED
    if status not in STATUSES:
        raise TriageError(
            f"status={status!r} 不在状态机里（§3.3）：{', '.join(STATUSES)}。"
            "`accepted_exception` 是**状态不是桶**，别把它写进 bucket"
        )
    out["status"] = status
    return out


def validate_rows(rows: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """批量校验 + **按键去重**：同一 ``(golden_id, dimension)`` 只能有一个桶。

    同键同桶 = 幂等重复，收下第一份；同键不同桶 = **拒绝**，因为"该信哪一条"
    没有正确默认值——让表owner 自己删掉一条，比悄悄取后者安全。
    """
    out: list[dict[str, Any]] = []
    seen: dict[tuple[str, str], dict[str, Any]] = {}
    for row in rows:
        valid = validate_row(row)
        key = (valid["golden_id"], valid["dimension"])
        prev = seen.get(key)
        if prev is None:
            seen[key] = valid
            out.append(valid)
            continue
        if prev["bucket"] != valid["bucket"]:
            raise TriageError(
                f"同一 ({key[0]}, {key[1]}) 出现两个桶："
                f"{prev['bucket']!r} 与 {valid['bucket']!r}。"
                "一条卡只能有一个归属——先删掉错的那条，别指望「后写的赢」"
            )
    return out


# --------------------------------------------------------------------------
# 读
# --------------------------------------------------------------------------


def read_rows(path: pathlib.Path | str) -> list[dict[str, Any]]:
    p = pathlib.Path(path)
    if not p.exists():
        raise TriageError(f"分诊档不存在：{p}")
    rows: list[dict[str, Any]] = []
    for lineno, line in enumerate(p.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError as exc:
            raise TriageError(f"{p}:{lineno} 不是合法 JSON：{exc}") from exc
    return rows


def load_triage(paths: Sequence[str] | None) -> list[dict[str, Any]] | None:
    """``None`` = 没给（该项 n/a）；给了但空 = 用法错误；给了就读 + **逐条校验**。

    这是分诊档的**唯一加载器**：``verify`` / ``gate`` 都走它，不各读一遍。
    """
    if paths is None:
        return None
    if not paths:
        raise TriageError("给了 --triage 但没给路径：要么给表，要么别写这个选项")
    rows: list[dict[str, Any]] = []
    for path in paths:
        rows.extend(read_rows(path))
    return validate_rows(rows)


# --------------------------------------------------------------------------
# 写
# --------------------------------------------------------------------------


def _now() -> str:
    return _dt.datetime.now().astimezone().isoformat(timespec="seconds")


def add_rows(
    rows: Sequence[Mapping[str, Any]],
    *,
    path: pathlib.Path | str | None = None,
    apply: bool = False,
    replace: bool = False,
) -> dict[str, Any]:
    """把分诊追加进人填档。**默认 dry-run**；``--replace`` 才覆盖已有桶。

    返回一份说明（``path`` / ``added`` / ``replaced`` / ``skipped`` / ``rows``），
    调用方拿它打印或断言。**没有写入口的旁路**：写之前一律 :func:`validate_rows`。
    """
    target = pathlib.Path(path) if path else DEFAULT_TRIAGE_FILE
    existing = read_rows(target) if target.exists() else []
    merged = validate_rows(existing)
    index = {_key(r): i for i, r in enumerate(merged)}

    added = replaced = skipped = 0
    for row in validate_rows(rows):
        key = _key(row)
        if key not in index:
            row["triage_at"] = row.get("triage_at") or _now()
            index[key] = len(merged)
            merged.append(row)
            added += 1
            continue
        at = index[key]
        if _same_triage(merged[at], row):
            skipped += 1  # 幂等：同键同桶同理由，重复跑一遍不该变成两行
            continue
        if not replace:
            raise TriageError(
                f"({key[0]}, {key[1]}) 已有分诊：bucket={merged[at]['bucket']!r}，"
                f"这次是 {row['bucket']!r}。默认**不覆盖**——改判要留痕，"
                "确认要改请加 --replace（旧值仍留在版本控制历史里）"
            )
        row["triage_at"] = row.get("triage_at") or _now()
        merged[at] = row
        replaced += 1

    if apply:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(
            "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in merged),
            encoding="utf-8",
        )
    return {
        "path": str(target),
        "applied": bool(apply),
        "added": added,
        "replaced": replaced,
        "skipped": skipped,
        "rows": merged,
    }


#: 工作表 → 分诊档时**只保留**这些字段。分诊档是**裁定**，不是表单的副本：
#: 证据原文、冻结行事实、建议理由都会随重算变，抄进裁定档就等于让今天的裁定
#: 引用着下一版的观察。留 ``suggested_bucket`` 是因为它是**人当时看到的那个
#: 建议值**——C-07 的「预填命中率」只能靠它算，事后重算会漂。
KEPT_FIELDS: tuple[str, ...] = (
    "golden_id",
    "dimension",
    "bucket",
    "triage_by",
    "triage_evidence",
    "status",
    "suggested_bucket",
)


def strip_row(row: Mapping[str, Any]) -> dict[str, Any]:
    """把工作表的一行压成分诊档的字段集。**建议值也走封闭集**：脏值会污染 C-07。"""
    out: dict[str, Any] = {}
    for field in KEPT_FIELDS:
        value = row.get(field)
        if value is None or (isinstance(value, str) and not value.strip()):
            continue
        if field == "suggested_bucket":
            try:
                buckets_mod.require_bucket(str(value).strip())
            except KeyError as exc:
                raise TriageError(
                    f"suggested_bucket={value!r} 不是六桶之一：{exc.args[0]}"
                ) from exc
        out[field] = value
    return out


def incomplete(rows: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """工作表里**还没填完**的行（逐行全部列出来，不只报第一条）。

    只报第一条是最气人的失败方式：填表人要来回跑 N 次才知道还差几行。
    """
    bad: list[dict[str, Any]] = []
    for lineno, row in enumerate(rows, 1):
        missing = [f for f in REQUIRED_FIELDS if not _text(row.get(f))]
        if missing:
            bad.append(
                {
                    "line": lineno,
                    "golden_id": _text(row.get("golden_id")),
                    "dimension": _text(row.get("dimension")),
                    "missing": missing,
                }
            )
    return bad


def import_worksheet(
    path: pathlib.Path | str,
    *,
    target: pathlib.Path | str | None = None,
    apply: bool = False,
    replace: bool = False,
) -> dict[str, Any]:
    """批量导入人填工作表（C-06）：**全填完才收**，写之前仍走 :func:`add_rows`。

    半填的表**整批拒**，不做"填了几行就写几行"：那种行为会让覆盖率看起来在涨，
    而涨的只是人填表的耐心。拒绝时把没填的行逐条列出来。
    """
    rows = read_rows(path)
    if not rows:
        raise TriageError(f"工作表 {path} 是空的：没有可导入的分诊")
    bad = incomplete(rows)
    if bad:
        detail = "\n".join(
            f"  第 {b['line']} 行 {b['golden_id']}:{b['dimension']} 缺 {b['missing']}"
            for b in bad
        )
        raise TriageError(
            f"工作表还有 {len(bad)} 行没填完（整批不导入）：\n{detail}\n"
            "三栏都要人给：bucket / triage_by / triage_evidence"
        )
    return add_rows(
        [strip_row(r) for r in rows],
        path=target,
        apply=apply,
        replace=replace,
    )


def _key(row: Mapping[str, Any]) -> tuple[str, str]:
    return (str(row.get("golden_id") or ""), str(row.get("dimension") or ""))


def _same_triage(a: Mapping[str, Any], b: Mapping[str, Any]) -> bool:
    """判「是不是同一条分诊」时**不看 ``triage_at``**：时间戳每次都新，比它永远不等。"""
    return all(a.get(f) == b.get(f) for f in ("bucket", "triage_by", "triage_evidence"))


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------


def summarize(rows: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    by_bucket: dict[str, int] = {}
    by_loop: dict[str, int] = {}
    for row in rows:
        bucket = str(row.get("bucket") or "")
        by_bucket[bucket] = by_bucket.get(bucket, 0) + 1
        loop = buckets_mod.loop_of(bucket) if bucket else ""
        by_loop[loop] = by_loop.get(loop, 0) + 1
    return {
        "n": len(rows),
        "by_bucket": dict(sorted(by_bucket.items())),
        "by_loop": dict(sorted(by_loop.items())),
        "untriaged": sum(1 for r in rows if not r.get("bucket")),
    }


def main(argv: list[str] | None = None) -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
    except (AttributeError, ValueError):
        pass
    ap = argparse.ArgumentParser(
        prog="triage",
        description=(
            "分诊档校验 / 追加（方案 16 C-05）：六桶封闭集 + 理由必填 + "
            "bucket 只能人填（D-C2 / D-C3）"
        ),
    )
    sub = ap.add_subparsers(dest="cmd", required=True)

    p_check = sub.add_parser("check", help="校验分诊档并打印桶分布")
    p_check.add_argument(
        "paths", nargs="*", default=None,
        help=f"分诊档路径（缺省 {DEFAULT_TRIAGE_FILE}）",
    )

    p_add = sub.add_parser("add", help="追加一条分诊（默认 dry-run）")
    p_add.add_argument("--golden-id", required=True)
    p_add.add_argument("--dimension", required=True, help="d10 或软维度名")
    p_add.add_argument("--bucket", required=True,
                       help=f"六桶之一：{', '.join(buckets_mod.BUCKETS)}")
    p_add.add_argument("--by", required=True, help="分诊人 id（机器身份会被拒绝）")
    p_add.add_argument("--evidence", required=True,
                       help="为什么**不是别的桶**（必填）")
    p_add.add_argument("--path", default="", help=f"分诊档（缺省 {DEFAULT_TRIAGE_FILE}）")
    p_add.add_argument("--replace", action="store_true",
                       help="覆盖已有桶（默认拒绝；旧值仍留在版本控制历史里）")
    p_add.add_argument("--apply", action="store_true", help="真写盘（默认 dry-run）")

    p_imp = sub.add_parser(
        "import",
        help="批量导入人填工作表（worklist build 产出的 worksheet.jsonl）",
    )
    p_imp.add_argument("worksheet", help="工作表路径")
    p_imp.add_argument("--path", default="", help=f"分诊档（缺省 {DEFAULT_TRIAGE_FILE}）")
    p_imp.add_argument("--replace", action="store_true",
                       help="覆盖已有桶（默认拒绝；旧值仍留在版本控制历史里）")
    p_imp.add_argument("--apply", action="store_true", help="真写盘（默认 dry-run）")
    args = ap.parse_args(argv)

    try:
        if args.cmd == "check":
            paths = args.paths if args.paths else [str(DEFAULT_TRIAGE_FILE)]
            # 缺省档不存在 → 退 2：**0 行不是"一切正常"**（09 号纪律，
            # 与 evaluation.cli 在 0 数据下不报警是同一类坑）
            rows = load_triage(paths)
            assert rows is not None
            info = summarize(rows)
            print(f"分诊档 {len(paths)} 份 · 共 {info['n']} 行")
            for bucket in buckets_mod.BUCKETS:
                n = info["by_bucket"].get(bucket, 0)
                mark = "（只能人填）" if buckets_mod.is_human_only(bucket) else ""
                print(f"  {bucket:<18}{n:>4}  {buckets_mod.owner_of(bucket)}{mark}")
            print(f"  按回路：{info['by_loop']}")
            if info["untriaged"]:
                print(f"  ⚠ 有 {info['untriaged']} 行没有桶（fail-closed 的空桶）")
            return 0

        if args.cmd == "import":
            imported = import_worksheet(
                args.worksheet,
                target=args.path or None,
                apply=args.apply,
                replace=args.replace,
            )
            info = summarize(imported["rows"])
            print(
                f"{'已写' if imported['applied'] else '（dry-run）拟写'} "
                f"{imported['path']}：新增 {imported['added']} / "
                f"覆盖 {imported['replaced']} / 跳过 {imported['skipped']}，"
                f"共 {info['n']} 行"
            )
            print(f"  按桶：{info['by_bucket']}")
            print(f"  按回路：{info['by_loop']}")
            return 0

        result = add_rows(
            [{
                "golden_id": args.golden_id,
                "dimension": args.dimension,
                "bucket": args.bucket,
                "triage_by": args.by,
                "triage_evidence": args.evidence,
            }],
            path=args.path or None,
            apply=args.apply,
            replace=args.replace,
        )
    except TriageError as exc:
        print(f"错误：{exc}", file=sys.stderr)
        print(
            "退出码 2（输入不合法）：这不是「驳回」，是分诊档本身没通过校验",
            file=sys.stderr,
        )
        return 2

    wanted = (args.golden_id, args.dimension)
    shown = next(
        (r for r in result["rows"] if _key(r) == wanted), None
    )
    print(json.dumps(shown, ensure_ascii=False, indent=1))
    if result["applied"]:
        print(
            f"→ 已写 {result['path']}（新增 {result['added']} / "
            f"覆盖 {result['replaced']} / 跳过 {result['skipped']}）"
        )
    else:
        print(f"（dry-run）加 --apply 才落盘 → {result['path']}")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())


__all__ = [
    "AGENT_ACTORS",
    "AGENT_PREFIXES",
    "DEFAULT_TRIAGE_FILE",
    "DEFAULT_TRIAGE_ROOT",
    "KEPT_FIELDS",
    "REQUIRED_FIELDS",
    "STATUSES",
    "STATUS_ASSIGNED",
    "TriageError",
    "actor_kind",
    "add_rows",
    "import_worksheet",
    "incomplete",
    "load_triage",
    "main",
    "read_rows",
    "strip_row",
    "summarize",
    "validate_row",
    "validate_rows",
]