"""语料清单（只读）：把 ``output/pipeline`` 里的 P1 存档按 task 汇总。

用途：跑之前先看清「现在有哪些任务、每条存档是什么面 / 什么分支分布 /
耗时多少」——**不重跑就得知道重跑的是什么**。

读法::

    uv run python trajectory_pipeline/research/probes/corpus_inventory.py
    uv run python trajectory_pipeline/research/probes/corpus_inventory.py --json

⚠️ 只读脚本，不写任何产物；判读口径以人读 evidence 为准（门禁不管）。
"""

from __future__ import annotations

import argparse
import collections
import json
import os
from pathlib import Path

PIPELINE = Path("trajectory_pipeline/output/pipeline")


def is_archive(path: Path) -> bool:
    name = path.name
    if ".reviewed." in name or name.startswith("plan"):
        return False
    return path.suffix == ".json"


def load(path: Path) -> dict | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(data, dict):
        return None
    # P1 存档的判别式：有 ledger + candidates
    if "ledger" in data and "candidates" in data:
        return data
    return None


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", action="store_true", help="输出机器可读")
    args = ap.parse_args()

    if not PIPELINE.exists():
        print(f"目录不存在: {PIPELINE}（从仓库根跑）")
        return 1

    archives = [p for p in sorted(PIPELINE.glob("*.json")) if is_archive(p)]
    per_task: dict[str, list[dict]] = collections.defaultdict(list)
    junk: list[str] = []
    for p in archives:
        data = load(p)
        if data is None:
            junk.append(p.name)
            continue
        tid = str(data.get("task_id") or "?")
        per_task[tid].append({"path": p.name, "data": data})

    rows = []
    for tid, items in sorted(per_task.items()):
        branches: collections.Counter = collections.Counter()
        faces: collections.Counter = collections.Counter()
        elapsed: list[int] = []
        neg = 0
        for it in items:
            d = it["data"]
            led = d.get("ledger") or {}
            for b, n in (led.get("by_branch") or {}).items():
                branches[b] += int(n)
            faces[str(d.get("driver") or led.get("driver") or "?")] += 1
            if isinstance(d.get("elapsed_ms"), int):
                elapsed.append(d["elapsed_ms"])
            neg += int(led.get("negative_samples") or 0)
        rows.append({
            "task_id": tid,
            "n_archives": len(items),
            "faces": dict(faces),
            "branches": dict(branches),
            "neg": neg,
            "elapsed_s": [round(e / 1000) for e in elapsed],
            "files": [it["path"] for it in items],
        })

    if args.json:
        print(json.dumps({"rows": rows, "junk": junk}, ensure_ascii=False, indent=2))
        return 0

    print(f"存档文件 {len(archives)}，认得 {sum(len(v) for v in per_task.values())}，"
          f"非 P1 存档 {len(junk)}")
    if junk:
        print("  非 P1（探测/评估产物，不计入语料）: " + ", ".join(junk))
    print()
    head = f"{'task_id':<10}{'份':>4}{'负':>4}  {'面':<34}{'分支分布':<46}耗时(s)"
    print(head)
    print("-" * len(head))
    for r in rows:
        faces = ",".join(f"{k}×{v}" for k, v in sorted(r["faces"].items()))
        br = ",".join(f"{k}×{v}" for k, v in sorted(r["branches"].items()))
        el = ",".join(str(x) for x in r["elapsed_s"])
        print(f"{r['task_id']:<10}{r['n_archives']:>4}{r['neg']:>4}  "
              f"{faces:<34}{br:<46}{el}")
    print()
    print(f"任务数 {len(rows)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
