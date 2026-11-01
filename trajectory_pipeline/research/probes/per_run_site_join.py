"""探针：候选集「站点行」与 P2「访问点」的**配对口径**是否一致（2026-11-01）。

两张表：

- **候选集站点行**：按 ``outcomes[]`` 枚举，一行 = 一条 outcome（同 url 取末条）；
- **P2 访问点**：按 ``visits[]`` 枚举，一行 = 一次访问（outcome 由
  ``outcomerules.resolve`` 逐档配：候选 → 落地 → 播放页，同档取末条）。

⇒ 两张表的**主键不同**（outcome ↔ visit），要合到一张表上就必须能配对。
本探针量三件事：

1. 有多少 outcome **配不到任何访问点**（孤儿记账）——它们在候选集里有行，
   但 P2 里没有对应的步区间；
2. 访问点配到的 outcome 下标，与「同 url 末条」的下标**不一致**的次数
   （两套口径打架的地方）；
3. 每条访问点配到的 outcome 的 url，与它自己的候选 url 是否相同
   （不同 ⇒ 候选集的 url 键与访问点对不上）。

用法（在仓库根跑）::

    python -m trajectory_pipeline.research.probes.per_run_site_join
"""

from __future__ import annotations

import collections
import json
import pathlib
import sys
from typing import Mapping

from trajectory_pipeline import outcomerules

ARCHIVES = (
    pathlib.Path(__file__).resolve().parents[2]
    / "output"
    / "pipeline"
)


def _load(p: pathlib.Path) -> list[dict]:
    if not p.exists():
        return []
    return [
        json.loads(line)
        for line in p.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def main() -> int:
    out = sys.stdout
    # ⚠️ worktree 里的 ``output/`` 是空的，存档在**主树** ⇒ 允许显式指目录。
    root = pathlib.Path(sys.argv[1]) if len(sys.argv) > 1 else ARCHIVES
    if not root.exists():
        print(f"存档目录不存在：{root}", file=sys.stderr)
        return 1

    n_archives = 0
    n_outcomes = 0
    n_orphan_outcomes = 0
    n_visits = 0
    n_visits_no_outcome = 0
    tier_vs_url_mismatch = 0
    url_differs = 0
    same_url_multi = 0
    orphan_examples: list[str] = []
    mismatch_examples: list[str] = []

    for p in sorted(root.glob("*.json")):
        if p.name.endswith(".reviewed.json"):
            continue
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            continue
        if "visits" not in data and "outcomes" not in data:
            continue
        n_archives += 1
        outcomes = data.get("outcomes") or []
        visits = data.get("visits") or ()
        n_outcomes += len(outcomes)

        # 同 url 的下标表（末条 = 最终结论，与 _site_entries 同口径）。
        last_at: dict[str, int] = {}
        for i, o in enumerate(outcomes):
            last_at[str(o.get("url") or "")] = i
        if any(
            sum(1 for o in outcomes if str(o.get("url") or "") == u) > 1
            for u in last_at
        ):
            same_url_multi += 1

        # 访问点 → outcome 下标（逐档 resolve，同档取末条）。
        hit_idx: set[int] = set()
        for i, visit in enumerate(visits, 1):
            n_visits += 1
            player_obs = visit.get("player_obs")
            player_url = (
                str(player_obs.get("url") or "")
                if isinstance(player_obs, Mapping)
                else ""
            )
            o = outcomerules.resolve(
                outcomes,
                str(visit.get("url") or ""),
                str(visit.get("landed_url") or ""),
                player_url,
            )
            if o is None:
                n_visits_no_outcome += 1
                continue
            idx = _index_of(outcomes, o)
            if idx is None:
                continue
            hit_idx.add(idx)
            url = str(o.get("url") or "")
            if url != str(visit.get("url") or "") and url != str(
                visit.get("landed_url") or ""
            ):
                url_differs += 1
            if last_at.get(url) != idx:
                tier_vs_url_mismatch += 1
                if len(mismatch_examples) < 8:
                    mismatch_examples.append(
                        f"{p.name} visit#{i} 解析到 outcome#{idx}"
                        f"（url={url}）而同 url 末条是 #{last_at.get(url)}"
                    )

        orphans = [i for i in range(len(outcomes)) if i not in hit_idx]
        n_orphan_outcomes += len(orphans)
        if orphans and len(orphan_examples) < 8:
            orphan_examples.append(
                f"{p.name}: {len(orphans)}/{len(outcomes)} 条 outcome 配不到访问点"
                f"（下标 {orphans[:5]}）"
            )

    print(f"存档 {n_archives} 份  outcome {n_outcomes} 条  访问点 {n_visits} 个")
    print(f"  配不到访问点的 outcome（孤儿记账）: {n_orphan_outcomes}")
    print(f"  访问点配不到 outcome: {n_visits_no_outcome}")
    print(f"  访问点配到的 outcome 与「同 url 末条」下标不一致: {tier_vs_url_mismatch}")
    print(f"  访问点配到的 outcome 的 url 与候选/落地 url 都不同: {url_differs}")
    print(f"  含同 url 多条 outcome 的存档: {same_url_multi}")
    for e in orphan_examples:
        print(f"    孤儿：{e}")
    for e in mismatch_examples:
        print(f"    打架：{e}")
    return 0


def _index_of(outcomes: list[dict], o: dict) -> int | None:
    for i, x in enumerate(outcomes):
        if x is o:
            return i
    return None


if __name__ == "__main__":
    sys.exit(main())
