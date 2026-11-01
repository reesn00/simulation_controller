"""探针：黄金集「站点级 → 轨迹级」迁移的形状事实（2026-10-30 · 方案 23）。

回答四个问题，每个答案都会改变迁移设计：

1. 306 条人核标注落在多少条轨迹上？（决定轨迹级标注单元的体量）
2. 一条轨迹平均有几个被标注站点？（决定 ``site_outcomes[]`` 的长度分布）
3. 同一条轨迹内，人核标签是否互相冲突？（决定能否机械折叠成轨迹级标签）
4. v16 冻结档里，一条轨迹的被标注站点是否**跨档**？（决定 D-10 分母会不会被
   「按轨迹重切档」改动——这是绑定口径，分母不能悄悄变）

用法（在仓库根跑）::

    python -m trajectory_pipeline.research.probes.per_run_golden_shape
"""

from __future__ import annotations

import collections
import json
import pathlib
import sys

DATA = pathlib.Path(__file__).resolve().parents[2] / "evaluation" / "golden" / "data"


def _load(name: str) -> list[dict]:
    p = DATA / name
    if not p.exists():
        return []
    return [
        json.loads(line)
        for line in p.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def _traj_of(golden_id: str) -> str:
    """站点级 golden_id → 轨迹级 id：去掉最后的 ``__<url_hash>`` 段。

    负样本池的 id 形式是 ``<traj>__<url_hash>__neg_<branch>``，也取前三段。
    """
    parts = golden_id.split("__")
    if len(parts) >= 3 and parts[-1].startswith("neg_"):
        return "__".join(parts[:2])
    if len(parts) >= 3:
        return "__".join(parts[:2])
    return golden_id


def main() -> int:
    anns = _load("annotations.jsonl")
    cands = _load("candidates.jsonl")
    print(f"annotations.jsonl  {len(anns)} 行")
    print(f"candidates.jsonl   {len(cands)} 行")

    by_kind = collections.Counter(c.get("source_kind") for c in cands)
    print(f"candidates by source_kind: {dict(by_kind)}")

    # ── 1/2/3：标注落在多少轨迹上、每轨几个站点、标签是否冲突 ──────────
    traj_sites: dict[str, list[dict]] = collections.defaultdict(list)
    for a in anns:
        traj_sites[_traj_of(str(a.get("golden_id") or ""))].append(a)

    print(f"\n被标注轨迹数: {len(traj_sites)}")
    counts = collections.Counter(len(v) for v in traj_sites.values())
    print(f"每轨标注条数分布: {dict(sorted(counts.items()))}")

    conflicting = []
    for traj, rows in traj_sites.items():
        labels = {str(r.get("label") or "") for r in rows}
        if len(labels) > 1:
            conflicting.append((traj, sorted(labels), len(rows)))
    print(f"同轨标签互相冲突的轨迹数: {len(conflicting)}")
    for traj, labels, n in sorted(conflicting, key=lambda x: -x[2])[:10]:
        print(f"    {traj}  {n} 条  {labels}")

    # ── 4：v16 冻结档里轨迹是否跨档 ───────────────────────────────────
    vdir = DATA / "v16"
    site_split: dict[str, str] = {}
    for split, fname in (
        ("calibration", "calibration.jsonl"),
        ("regression", "regression.jsonl"),
        ("holdout", "holdout.jsonl"),
    ):
        for row in _load(f"v16/{fname}"):
            gid = str(row.get("golden_id") or "")
            site_split[gid] = split

    traj_splits: dict[str, set[str]] = collections.defaultdict(set)
    for gid, split in site_split.items():
        traj_splits[_traj_of(gid)].add(split)
    span = {t: s for t, s in traj_splits.items() if len(s) > 1}
    print(f"\nv16 冻结档：站点行 {len(site_split)} 条，覆盖轨迹 {len(traj_splits)} 条")
    print(f"  跨档轨迹数: {len(span)}")
    for t, s in sorted(span.items())[:10]:
        print(f"    {t} → {sorted(s)}")

    # 被标注站点在各档的分布（D-10 分母的构成）
    ann_ids = {str(a.get("golden_id") or "") for a in anns}
    dist = collections.Counter(
        site_split.get(gid, "(不在 v16)") for gid in ann_ids
    )
    print(f"  306 条标注按档分布: {dict(dist)}")
    print(f"  其中落在 calibration（头条分母档）的: {dist.get('calibration', 0)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
