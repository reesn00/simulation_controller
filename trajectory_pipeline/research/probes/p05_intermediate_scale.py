"""P0-5 规模：B/C 中间页里的 agent 动作有多少、丢了多少。

跑法（在 trajectory_pipeline/ 下）：
    uv run python research/probes/p05_intermediate_scale.py [存档目录...]

不给目录时扫 output/pipeline 下的常见批次目录。
"""
from __future__ import annotations

import json
import pathlib
import sys
from collections import Counter

DEFAULT_ROOTS = [
    "output/pipeline",
]


def _archives(root: pathlib.Path):
    for p in sorted(root.rglob("*.json")):
        if p.name.endswith(".reviewed.json"):
            continue
        try:
            d = json.loads(p.read_text(encoding="utf-8"))
        except (ValueError, OSError):
            continue
        if isinstance(d, dict) and "visits" in d:
            yield p, d


def main(argv: list[str]) -> int:
    roots = [pathlib.Path(x) for x in argv] or [pathlib.Path(x) for x in DEFAULT_ROOTS]
    n_arch = 0
    n_arch_with_inter = 0
    steps_by_mode: Counter[tuple[str, str]] = Counter()
    inter_archives: list[tuple[str, int, int]] = []
    for root in roots:
        if not root.exists():
            continue
        for p, d in _archives(root):
            n_arch += 1
            inters = d.get("intermediates") or ()
            if not inters:
                continue
            n_arch_with_inter += 1
            mode = str(d.get("executor_mode") or "?")
            n_steps = sum(len(i.get("steps") or ()) for i in inters)
            steps_by_mode[(mode, "intermediate")] += n_steps
            inter_archives.append((p.stem, len(inters), n_steps))
            # 对照：同一份存档里 visits 的步数
            steps_by_mode[(mode, "visits")] += sum(
                len(v.get("steps") or ()) for v in (d.get("visits") or ()))

    print(f"扫描存档 {n_arch} 份，其中带 intermediates 的 {n_arch_with_inter} 份")
    if not n_arch_with_inter:
        print("（没有 B/C 存档可供统计——先跑一批 B/C 路径的任务）")
        return 0
    print()
    print("步数按 (模式, 单元类型)：")
    for (mode, kind), n in sorted(steps_by_mode.items()):
        print(f"  {mode:10s} {kind:14s} {n}")
    print()
    top = sorted(inter_archives, key=lambda x: -x[2])[:10]
    print("中间页步数最多的 10 份（存档, 中间页数, 步数）：")
    for name, ni, ns in top:
        print(f"  {name:42s} {ni:3d} {ns:5d}")
    tot = sum(x[2] for x in inter_archives)
    print()
    print(f"中间页总步数 {tot}（这 {n_arch_with_inter} 份存档）")
    print("⚠️ 这些步在 P2 的 _units_of 里一条都不进 ⇒ 不进训练样本")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
