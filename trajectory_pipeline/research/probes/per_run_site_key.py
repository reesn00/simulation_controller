"""探针：候选集「站点行」的 url 键集合在三种口径下差多少（2026-11-01）。

背景：候选集的站点行按 **url** 建键（``_url_hash(outcome.url)``），
而人核标注就是挂在这个键上的 ⇒ **键集合变了 = 人核标签掉线**。
（``annotations.jsonl`` 有 **306 行**，但只有 **294 个不同的 ``golden_id``**
——多出来的 12 行是 round=2 的重测 ⇒ 键集合比的是**不同的 id**。）

三套口径：

- **A（现行）**：``outcomes[]`` 枚举，同 url 取**末条**；
- **B（访问阶段）**：只取**被访问点配到过**的 outcome（同 url 取末条）；
- **C（访问阶段优先 + 兜底孤儿）**：先按 B；某 url 一条都没被访问点配到时，
  才用该 url 的末条 outcome（孤儿记账不丢）。

⇒ 量三件事：

1. 三套口径的 url 键集合两两差多少（按轨迹）；
2. **标注的键**在每套口径下还挂不挂得上（这才是硬指标）；
3. 访问点与 outcome 的配对关系在 B/C 下能不能一一对上。

用法（在仓库根跑）::

    python -m trajectory_pipeline.research.probes.per_run_site_key \
        <存档目录> <黄金集 data 目录>
"""

from __future__ import annotations

import collections
import json
import pathlib
import sys
from typing import Any, Mapping

from trajectory_pipeline import outcomerules


def _load(p: pathlib.Path) -> list[dict]:
    if not p.exists():
        return []
    return [
        json.loads(line)
        for line in p.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def _url_hash(url: str) -> str:
    """与 ``evaluation.golden.builder._url_hash`` 同口径（复制实现，探针不 import 评估侧）。"""
    import hashlib

    return hashlib.md5(url.encode("utf-8")).hexdigest()[:8]


def _visit_matched(outcomes: list[dict], visits: list[dict]) -> dict[int, int]:
    """访问点 → outcome 下标（逐档 resolve，同档取末条）。"""
    out: dict[int, int] = {}
    for i, visit in enumerate(visits, 1):
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
            continue
        for j, x in enumerate(outcomes):
            if x is o:
                out[i] = j
                break
    return out


def main() -> int:
    if len(sys.argv) < 3:
        print(__doc__, file=sys.stderr)
        return 2
    root = pathlib.Path(sys.argv[1])
    data = pathlib.Path(sys.argv[2])

    set_a: set[str] = set()
    set_b: set[str] = set()
    set_c: set[str] = set()
    n_trajectories = 0
    n_visit_matched_pairs = 0
    visits_without_matched = 0

    for p in sorted(root.glob("*.json")):
        if p.name.endswith(".reviewed.json"):
            continue
        try:
            data_j = json.loads(p.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            continue
        if "visits" not in data_j and "outcomes" not in data_j:
            continue
        n_trajectories += 1
        outcomes = data_j.get("outcomes") or []
        visits = data_j.get("visits") or []
        task, run = p.stem.split("__", 1)

        matched = _visit_matched(outcomes, visits)
        n_visit_matched_pairs += len(matched)
        visits_without_matched += len(visits) - len(matched)

        by_url: dict[str, list[int]] = collections.defaultdict(list)
        for i, o in enumerate(outcomes):
            by_url[str(o.get("url") or "")].append(i)

        # A：同 url 取末条
        a_by_url = {u: idxs[-1] for u, idxs in by_url.items()}
        # 访问点配到的下标
        matched_idx = set(matched.values())
        b_by_url: dict[str, int] = {}
        for u, idxs in by_url.items():
            hits = [i for i in idxs if i in matched_idx]
            if hits:
                b_by_url[u] = hits[-1]
        # C：B 优先，缺则用末条
        c_by_url = dict(b_by_url)
        for u, idxs in by_url.items():
            c_by_url.setdefault(u, idxs[-1])

        for u, idx in a_by_url.items():
            set_a.add(f"{task}__{run}__{_url_hash(u)}")
        for u, idx in b_by_url.items():
            set_b.add(f"{task}__{run}__{_url_hash(u)}")
        for u, idx in c_by_url.items():
            set_c.add(f"{task}__{run}__{_url_hash(u)}")

    print(f"轨迹 {n_trajectories} 条  访问点→outcome 配对 {n_visit_matched_pairs}"
          f"  访问点没配上 {visits_without_matched}")
    if n_trajectories == 0:
        # ⚠️ **fail-closed**：存档根是空的 ⇒ 下面的「0/294 挂不上」是**假读数**
        # （worktree 的 output/ 就是空的 ⇒ 数据在主树）⇒ 显式退 2，不装成读数。
        print(
            f"存档目录 {root} 里**没有 P1 存档** ⇒ 键集合是空的 ⇒ "
            "下面那些「挂不上」全是假的。\n"
            "⚠️ **在 worktree 里 `output/pipeline/` 是空的**（数据在主树）⇒ "
            "把第一个参数指成主树的那个绝对路径。",
            file=sys.stderr,
        )
        return 2
    print(f"url 键集合大小：A={len(set_a)}  B={len(set_b)}  C={len(set_c)}")
    print(f"  A-B（只有 A 有：B 口径下会丢的行）: {len(set_a - set_b)}")
    print(f"  B-A（只有 B 有）: {len(set_b - set_a)}")
    print(f"  A-C: {len(set_a - set_c)}   C-A: {len(set_c - set_a)}")

    anns = _load(data / "annotations.jsonl")
    ann_ids = {str(a.get("golden_id") or "") for a in anns}
    n_rows = len(anns)
    print(f"\n标注：{n_rows} 行 / {len(ann_ids)} 个不同的 golden_id"
          f"（差额是 round=2 的重测行）")
    print("每个不同的 golden_id 在每套口径下挂得上多少：")
    for name, s in (("A", set_a), ("B", set_b), ("C", set_c)):
        hit = len(ann_ids & s)
        print(f"  {name}: {hit}/{len(ann_ids)}  挂不上 {len(ann_ids - s)}")
        for miss in sorted(ann_ids - s)[:6]:
            print(f"      挂不上：{miss}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
