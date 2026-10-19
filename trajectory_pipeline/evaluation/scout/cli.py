"""scout 子命令:在 P2 与黄金集之间加 rubric 评分门禁。

入口:``python -m trajectory_pipeline.evaluation.scout.cli``

设计来源:``docs/设计方案/09-rubric-gate改造方案v04-实施细节.md``
"""

from __future__ import annotations

import argparse
import pathlib
import sys
from typing import Any

from trajectory_pipeline.evaluation.scout.io import run_scout


#: 默认 P2 根。与 :data:`trajectory_pipeline.evaluation.cli.DEFAULT_ROOT_PATH` 同源。
DEFAULT_P2_ROOT = pathlib.Path("output/pipeline")


def _stats_line(stats: dict[str, int]) -> str:
    pos = stats.get("positive", 0)
    neg = stats.get("negative", 0)
    qua = stats.get("quarantine", 0)
    skp = stats.get("skip", 0)
    mis = stats.get("missing_source", 0)
    return (f"pos={pos}  neg={neg}  quarantine={qua}  "
            f"skip={skp}  missing_source={mis}")


def _dry_run(p2_root: pathlib.Path) -> int:
    from trajectory_pipeline.evaluation.scout.io import (
        list_p2_samples, _resolve_sample, _score_one, _status_of,
    )
    from trajectory_pipeline.evaluation.scout.policy import classify, tags_for
    p2s = list_p2_samples(p2_root)
    counters: dict[str, int] = {
        "positive": 0, "negative": 0, "quarantine": 0, "skip": 0,
    }
    for p2 in p2s:
        sample = _resolve_sample(p2, p2_root)
        if sample is None:
            counters["skip"] += 1
            continue
        scores = _score_one(sample)
        status = _status_of(scores)
        rec: dict[str, Any] = {
            "sample_id": sample.sample_id,
            "task_id": p2.get("task_id", ""),
            "run_id": "",
            "status": status,
            "scores": scores,
            "tags": [],
            "evidence_supplement": {},
        }
        kind = classify(rec)  # type: ignore[arg-type]
        if kind == "skip":
            counters["skip"] += 1
        elif kind == "quarantine":
            counters["quarantine"] += 1
        elif kind == "negative":
            rec["tags"] = list(tags_for(rec))  # type: ignore[arg-type]
            counters["negative"] += 1
        else:
            counters["positive"] += 1
    print(f"[dry-run] 输入 {len(p2s)} 条 P2 样本")
    print(f"[dry-run] {_stats_line(counters)}")
    return 0


# ── aggregate 子命令 ────────────────────────────────────────────


def _cmd_aggregate(args: argparse.Namespace) -> int:
    """``scout aggregate`` 子命令:三件套 → trajectory_candidates.jsonl。

    设计基线:``docs/设计方案/14-完整任务视图与LS上传.md`` §4.2-§4.3
    (D14.5 主路径,Phase 3)。聚合策略:**min**——四维各自取最差 unit 的
    score,任一 unit 站错整条轨迹站错;任一维 ``score=None`` 短路到
    ``undetermined``。

    三态计数(scored / undetermined / not_run)落 ``trajectory_stats.json``
    同行——报告侧直接读,不再二次解析。
    """
    from trajectory_pipeline.evaluation.scout.aggregate import (
        aggregate_from_buckets,
    )

    scout_dir = pathlib.Path(args.scout_dir)
    if not scout_dir.is_dir():
        print(f"错误:{scout_dir} 不是目录", file=sys.stderr)
        return 2

    out_path = pathlib.Path(args.out) if args.out else None
    buckets = tuple(b.strip() for b in args.buckets.split(",") if b.strip())
    overwrite = bool(args.overwrite)

    trajectories, stats = aggregate_from_buckets(
        scout_dir,
        output_path=out_path,
        buckets=buckets,
        expected_unit_count=args.expected_unit_count,
        overwrite=overwrite,
    )

    scored = stats.get("scored", 0)
    undetermined = stats.get("undetermined", 0)
    not_run = stats.get("not_run", 0)
    total = scored + undetermined + not_run
    print(
        f"scout 聚合：{total} 条 trajectory  "
        f"(scored={scored}  undetermined={undetermined}  not_run={not_run})"
    )
    if out_path is None:
        out_path = scout_dir / "trajectory_candidates.jsonl"
    print(f"  → {out_path}")
    print(f"  → {out_path.parent / 'trajectory_stats.json'}")
    return 0


def _cmd_verify(args: argparse.Namespace) -> int:
    """``scout verify`` 子命令:trajectory 产物体检(只读)。

    盯 ``trajectory_candidates.jsonl`` / ``trajectory_stats.json`` / T
    视图三者一致性(详见 :mod:`trajectory_pipeline.evaluation.scout.verify`)。
    error 级 issue → 返 1;只有 warning → 返 0;全过 → 返 0。
    """
    from trajectory_pipeline.evaluation.scout.verify import (
        format_report,
        verify_from_dir,
    )

    scout_dir = pathlib.Path(args.scout_dir)
    if not scout_dir.is_dir():
        print(f"错误:{scout_dir} 不是目录", file=sys.stderr)
        return 2

    report = verify_from_dir(scout_dir, task_views_root=args.task_views_root)
    print(f"[scout verify] scout_dir={scout_dir}")
    print(format_report(report))
    return 0 if report.ok else 1


def main(argv: list[str] | None = None) -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
    except (AttributeError, ValueError):
        pass

    ap = argparse.ArgumentParser(
        prog="python -m trajectory_pipeline.evaluation.scout.cli",
        description="三件套生成:positive / negative / quarantine 候选",
    )
    sub = ap.add_subparsers(dest="cmd", required=True)

    run_p = sub.add_parser(
        "run",
        help="P2 → scout 三件套(per-unit,正/负/缺判)",
    )
    run_p.add_argument(
        "--p2-root", default=str(DEFAULT_P2_ROOT),
        help=f"P1 存档 + P2 samples 共同根(默认 {DEFAULT_P2_ROOT})",
    )
    run_p.add_argument(
        "--out", required=True,
        help="三件套落盘目录(如 output/scout/2026-10-14)",
    )
    run_p.add_argument(
        "--mediabrowse-dir", default=None,
        help="可选:output/pipeline/mediabrowse/,L5 证据作 D-3 补充",
    )
    run_p.add_argument(
        "--dry-run", action="store_true",
        help="不落盘,只打印三态统计",
    )
    run_p.add_argument(
        "--stats", action="store_true",
        help="落盘后打印三态统计",
    )

    agg_p = sub.add_parser(
        "aggregate",
        help="scout 三件套 → trajectory_candidates.jsonl(per-Run 聚合,"
             "给 LS 上传 D14.5 主路径用)",
    )
    agg_p.add_argument(
        "--scout-dir", required=True,
        help="scout 三件套所在目录(如 output/scout/2026-10-14)",
    )
    agg_p.add_argument(
        "--out", default=None,
        help="聚合产物落盘路径;不传 = <scout-dir>/trajectory_candidates.jsonl",
    )
    agg_p.add_argument(
        "--buckets", default="positive,negative,quarantine",
        help="参与聚合的 bucket,逗号分隔(默认全三态)",
    )
    agg_p.add_argument(
        "--expected-unit-count", type=int, default=None,
        help="期望 unit 数(可选);少于此数记 missing_unit 留痕",
    )
    agg_p.add_argument(
        "--overwrite", action="store_true",
        help="覆盖已存在的 trajectory_candidates.jsonl。"
             "**默认不覆盖**——重建批次不该把别人刚标好的聚合产物抹掉",
    )

    ver_p = sub.add_parser(
        "verify",
        help="trajectory 产物体检(只读):jsonl / stats / T 视图 三者一致性",
    )
    ver_p.add_argument(
        "--scout-dir", required=True,
        help="含 trajectory_candidates.jsonl + trajectory_stats.json 的目录",
    )
    ver_p.add_argument(
        "--task-views-root", default=None,
        help="T 视图根(默认 storage.task_views_dir 默认根 = "
             "trajectory_pipeline/output/pipeline/task_views)",
    )

    args = ap.parse_args(argv)
    if args.cmd == "aggregate":
        return _cmd_aggregate(args)
    if args.cmd == "verify":
        return _cmd_verify(args)
    # run (原 main 逻辑)

    p2_root = pathlib.Path(args.p2_root)
    out_dir = pathlib.Path(args.out)
    mb_dir = pathlib.Path(args.mediabrowse_dir) if args.mediabrowse_dir else None

    if not p2_root.is_dir():
        print(f"错误:{p2_root} 不是目录", file=sys.stderr)
        return 2

    if args.dry_run:
        return _dry_run(p2_root)

    stats = run_scout(p2_root, out_dir, mediabrowse_dir=mb_dir)
    print(f"-> 已写 {out_dir}/{{positive,negative,quarantine,scout}}.jsonl")
    print(f"   {_stats_line(stats)}")
    print(f"   sum={sum(stats.values())}  (== 输入 P2 样本数)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
