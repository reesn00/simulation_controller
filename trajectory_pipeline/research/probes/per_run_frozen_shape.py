"""探针：v16 冻结档的行形状与标注落点（2026-10-30 · 方案 23 前置）。

回答：
1. 冻结档三档里各有多少 **站点级**行、多少 **p2 轨迹级**行？
2. 306 条人核标注里，有几条挂在 p2 轨迹级行上？（若是，则「轨迹级标注」
   不是新发明，而是**已存在的另一种 scope**，迁移只需把两种 scope 归位。）
3. 候选集里 p2_sample 行的 golden_id 现在长什么样（per-Run 之后应是
   ``{task}__{run}`` 两段）。
"""

from __future__ import annotations

import collections
import json
import pathlib
import sys

DATA = pathlib.Path(__file__).resolve().parents[2] / "evaluation" / "golden" / "data"


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
    manifest = json.loads((DATA / "v16" / "manifest.json").read_text(encoding="utf-8"))
    print("v16 manifest counts:", json.dumps(manifest.get("counts"), ensure_ascii=False))

    for fname in ("calibration.jsonl", "regression.jsonl", "holdout.jsonl"):
        rows = _load(DATA / "v16" / fname)
        kinds = collections.Counter(str(r.get("source_kind")) for r in rows)
        with_gold = collections.Counter(
            str(r.get("source_kind")) for r in rows if r.get("golden")
        )
        print(f"\n{fname}: {len(rows)} 行  kind={dict(kinds)}  带金标={dict(with_gold)}")
        sample = rows[0] if rows else {}
        print("  首行键:", sorted(sample.keys()))

    anns = _load(DATA / "annotations.jsonl")
    cand = {str(c.get("golden_id")): c for c in _load(DATA / "candidates.jsonl")}
    by_kind = collections.Counter()
    for a in anns:
        c = cand.get(str(a.get("golden_id")))
        by_kind[str((c or {}).get("source_kind") or "（不在候选集里）")] += 1
    print(f"\n306 条标注按候选行的 source_kind: {dict(by_kind)}")

    p2_ids = [
        str(c.get("golden_id"))
        for c in _load(DATA / "candidates.jsonl")
        if c.get("source_kind") == "p2_sample"
    ]
    print(f"\np2_sample 候选 {len(p2_ids)} 条，id 样例：{p2_ids[:5]}")
    print(f"  两段 id（per-Run 形态）: {sum(1 for i in p2_ids if i.count('__') == 1)}")
    print(f"  三段及以上（旧形态）: {sum(1 for i in p2_ids if i.count('__') >= 2)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
