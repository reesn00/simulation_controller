"""量**冻结的金标候选集**与**负样本池**之间的漂移（键对不上有多少、哪些）。

为什么要有这支
--------------
候选集 ``evaluation/golden/data/candidates.jsonl`` 是**冻结件**（v13），
池是 **append-only** 的活资产 ⇒ 池一增删，候选集里就会有一批键**在池里找不到**
⇒ 人核标签（``annotations.jsonl``）挂在那批键上 ⇒ 漂移会**静默掉线**
（避坑指南 §8.32 记的就是这个机制）。

⇒ 每次动池之后都该量一次：**漂移多少、其中多少是本轮新增的**。
「本轮新增」要跟**动池之前**的池对照才算得出来 ⇒ 本脚本接受
``--pool-before``（动池前的池文件）做对照。

⚠️ **两个口径都报**（各有用处，别混）：
- **去重键**：``(task_id, url, branch)`` ⇒ 键空间的大小；
- **按行**：候选集里**有多少行**挂空 ⇒ 人核标注实际会掉多少。

⚠️ **判据口径**：候选行的分支在 ``code.branch``（**不在顶层**）——
顶层没有 ``branch`` 字段 ⇒ 按顶层取会得到 322 个「空分支」键，看着像全漂了。

用法（**必须在仓库根跑**）::

    uv run python trajectory_pipeline/research/probes/gold_candidate_drift.py
    uv run python trajectory_pipeline/research/probes/gold_candidate_drift.py \\
        --pool-before trajectory_pipeline/output/q23_backup_2026-10-28/negative.jsonl
"""

from __future__ import annotations

import argparse
import json
import pathlib
from collections import Counter

CANDIDATES = pathlib.Path(
    "trajectory_pipeline/evaluation/golden/data/candidates.jsonl")
POOL = pathlib.Path("trajectory_pipeline/output/pipeline/negative.jsonl")


def _read_jsonl(path: pathlib.Path) -> list[dict]:
    if not path.exists():
        return []
    return [json.loads(line) for line in
            path.read_text(encoding="utf-8").splitlines() if line.strip()]


def _cand_key(row: dict) -> tuple[str, str, str]:
    """候选行的键——**分支在 ``code.branch``**，不在顶层。"""
    code = row.get("code") or {}
    return (str(row.get("task_id") or ""),
            str(row.get("url") or ""),
            str(code.get("branch") or ""))


def _pool_key(row: dict) -> tuple[str, str, str]:
    return (str(row.get("task_id") or ""),
            str(row.get("url") or ""),
            str(row.get("branch") or ""))


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="金标候选集 ↔ 负样本池 的漂移读数")
    ap.add_argument("--candidates", default=str(CANDIDATES))
    ap.add_argument("--pool", default=str(POOL))
    ap.add_argument("--pool-before", default="",
                    help="动池**之前**的池文件（做「本轮新增」的对照）")
    args = ap.parse_args(argv)

    cands = _read_jsonl(pathlib.Path(args.candidates))
    pool = _read_jsonl(pathlib.Path(args.pool))
    if not cands:
        print(f"[FAIL] 候选集读不到：{args.candidates}")
        return 2
    if not pool:
        print(f"[FAIL] 池读不到：{args.pool}")
        return 2

    cand_keys = {_cand_key(r) for r in cands}
    pool_keys = {_pool_key(r) for r in pool}
    dangling = {k for k in cand_keys if k not in pool_keys}
    rows_dangling = [r for r in cands if _cand_key(r) not in pool_keys]

    print(f"候选集 {len(cands)} 行 / {len(cand_keys)} 个去重键")
    print(f"池     {len(pool)} 行 / {len(pool_keys)} 个去重键")
    print(f"漂移：{len(dangling)} 个键 · {len(rows_dangling)} 行")
    print("  按分支：" + ", ".join(
        f"{b}×{n}" for b, n in Counter(k[2] for k in dangling).most_common()))

    if args.pool_before:
        before = _read_jsonl(pathlib.Path(args.pool_before))
        if not before:
            print(f"[FAIL] 对照池读不到：{args.pool_before}")
            return 2
        before_keys = {_pool_key(r) for r in before}
        old_dangling = {k for k in cand_keys if k not in before_keys}
        old_rows = [r for r in cands if _cand_key(r) not in before_keys]
        new = dangling - old_dangling
        new_rows = [r for r in cands
                    if _cand_key(r) in new]
        print(f"对照（动池前 {len(before)} 行）：漂移 {len(old_dangling)} 个键 · "
              f"{len(old_rows)} 行")
        print(f"★ 本轮新增漂移：**{len(new)} 个键 · {len(new_rows)} 行** ⇒ "
              + ", ".join(f"{b}×{n}" for b, n in
                          Counter(k[2] for k in new).most_common()))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
