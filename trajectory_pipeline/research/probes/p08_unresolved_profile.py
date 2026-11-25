"""P0-8 探针：unresolved 的构成（按 exit_stage / exit_signal 分桶）。

跑法（在 trajectory_pipeline/ 下）：
    uv run python research/probes/p08_unresolved_profile.py <目录> [<目录>...]

要点：`unresolved` 是一个**大筐**——「我们坏了」和「站点不给」都往里装。
不按 exit_stage 分开看，读到的就是「成功率低」这一句废话。
"""
from __future__ import annotations

import json
import pathlib
import sys
from collections import Counter


def main(argv: list[str]) -> int:
    if not argv:
        print(__doc__)
        return 2
    for root in (pathlib.Path(a) for a in argv):
        if not root.exists():
            print(f"[跳过] {root} 不存在")
            continue
        rows: list[tuple[str, str, str, str]] = []
        for p in sorted(root.glob("*.json")):
            if p.name.endswith(".reviewed.json"):
                continue
            try:
                d = json.loads(p.read_text(encoding="utf-8"))
            except (ValueError, OSError):
                continue
            if not isinstance(d, dict) or "outcomes" not in d:
                continue
            mode = str(d.get("executor_mode") or "scripted")
            for o in d.get("outcomes") or ():
                rows.append((
                    p.stem,
                    mode,
                    str(o.get("branch") or ""),
                    str(o.get("exit_stage") or o.get("exit_signal") or ""),
                ))

        if not rows:
            print(f"\n=== {root}：没有可读的存档 ===")
            continue
        print(f"\n=== {root} ===")
        print(f"存档 {len({r[0] for r in rows})} 份，站点级 outcome {len(rows)} 条")

        by_branch = Counter(r[2] for r in rows)
        print("\nbranch 分布：")
        for b, n in by_branch.most_common():
            print(f"  {b or '?':24s} {n}")

        unres = [r for r in rows if r[2] == "unresolved"]
        print(f"\nunresolved 共 {len(unres)} 条，按 exit_stage/exit_signal 分桶：")
        for (stage,), n in Counter((r[3],) for r in unres).most_common():
            print(f"  {(stage or '（空·非 agent 收口）'):30s} {n}")

        print("\n逐条（存档 | 模式 | 出口）：")
        for stem, mode, branch, stage in unres:
            print(f"  {stem[:34]:34s} {mode:9s} {stage or '（空）'}")

        # 关键判读：unresolved 里多少是「我们坏了」、多少是「站点不给」
        ours = {"agent.stale_context", "agent.contract", "agent.backend",
                "intermediate.exception"}
        n_ours = sum(1 for r in unres if r[3] in ours)
        print(f"\n⚠️ 「我们这边的问题」{n_ours} 条 / 「站点或预算」{len(unres) - n_ours} 条")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
