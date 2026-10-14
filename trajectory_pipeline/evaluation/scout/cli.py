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


def main(argv: list[str] | None = None) -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
    except (AttributeError, ValueError):
        pass

    ap = argparse.ArgumentParser(
        prog="python -m trajectory_pipeline.evaluation.scout.cli",
        description="三件套生成:positive / negative / quarantine 候选",
    )
    ap.add_argument(
        "--p2-root", default=str(DEFAULT_P2_ROOT),
        help=f"P1 存档 + P2 samples 共同根(默认 {DEFAULT_P2_ROOT})",
    )
    ap.add_argument(
        "--out", required=True,
        help="三件套落盘目录(如 output/scout/2026-10-14)",
    )
    ap.add_argument(
        "--mediabrowse-dir", default=None,
        help="可选:output/pipeline/mediabrowse/,L5 证据作 D-3 补充",
    )
    ap.add_argument(
        "--dry-run", action="store_true",
        help="不落盘,只打印三态统计",
    )
    ap.add_argument(
        "--stats", action="store_true",
        help="落盘后打印三态统计",
    )
    args = ap.parse_args(argv)

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
