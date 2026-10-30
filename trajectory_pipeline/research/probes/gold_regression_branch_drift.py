"""金标 vs 今天盘上的存档：**冻结时点的机器结论**漂了多少、哪些金标因此可疑。

问三件事（都是 2026-10-30 排优先级时必须先量出来的）：

1. **底料漂移**：冻结档的 ``code.branch``（采集时写下的机器结论）≠
   今天存档的 ``outcomes[].branch`` ⇒ 冻结档的 pred 侧是**旧口径**。
2. **族 B（金标陈旧）**：金标写 ``unreachable_hard``，而 Q-23（2026-10-28）
   已逐条真浏览器复核推翻它（「打得开」⇒ 这次没打开 ⇒ ``unresolved``）⇒
   **裁定已闭、只是金标没跟上** ⇒ 要人改金标。
3. **金标写 ``success`` 而今天存档说「没打开 / 内容坏 / 组件未验证」**
   ⇒ 假正样本的嫌疑 ⇒ 要人看。

⚠️ **为什么不能只看冻结档**：``code.branch`` 是**冻结时点**的机器结论，
今天的存档被 relabel 过（Q-23 那轮、判据②回落那轮）⇒ 两边不同源。
**这不代表金标错了**——金标是**人**的裁定，机器结论变��不等于裁定变；
只有「裁定所依据的事实被推翻」（族 B）和「明显自相矛盾」（success 类）才是问题。

跑法（**必须在仓库根跑**）::

    python trajectory_pipeline/research/probes/gold_regression_branch_drift.py
"""

from __future__ import annotations

import argparse
import json
import pathlib
import sys
from typing import Any

REPO = pathlib.Path(__file__).resolve().parents[2]
GOLD_ROOT = REPO / "evaluation" / "golden" / "data"
#: 产物目录在**包内**（``trajectory_pipeline/output/pipeline/``）——不是仓库根。
PIPELINE = REPO / "output" / "pipeline"

SPLITS = ("calibration", "regression", "holdout")


def _load_jsonl(path: pathlib.Path) -> list[dict[str, Any]]:
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def _archive_outcomes(task_id: str, run_id: str) -> dict[str, dict[str, Any]]:
    """``{url: outcome}``——同 run 内按 URL 取。

    ⚠️ 两处踩过的坑：

    1. **归档文件名是 ``{task_id}__{run_id}.json``**，而冻结行里的 ``run_id``
       **不带 task 前缀**（``run_id="1cf5deca"`` + ``task_id="TPIN02"``）；
    2. **``.reviewed.json`` 取代原档、且优先**
       （``executor/archive.py::select_archives``）⇒ 先读它，原档只补它没有的 URL。
    """
    out: dict[str, dict[str, Any]] = {}
    stems = [f"{task_id}__{run_id}", run_id] if task_id else [run_id]
    for stem in stems:
        for name in (f"{stem}.reviewed.json", f"{stem}.json"):
            p = PIPELINE / name
            if not p.exists():
                continue
            data = json.loads(p.read_text(encoding="utf-8"))
            for outcome in data.get("outcomes") or []:
                url = str(outcome.get("url") or "")
                if url:
                    out.setdefault(url, outcome)
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description="金标 vs 今天存档：漂移 / 族 B / success 存疑")
    ap.add_argument("--version", default="v16", help="冻结版本目录名（默认 v16）")
    ap.add_argument("--split", default="all", choices=("all", *SPLITS))
    args = ap.parse_args()

    splits = SPLITS if args.split == "all" else (args.split,)
    totals = {"drift": 0, "stale_uh": 0, "success_doubt": 0, "rows": 0}
    for split in splits:
        path = GOLD_ROOT / args.version / f"{split}.jsonl"
        rows = _load_jsonl(path)
        drift: list[tuple[str, str, str, str]] = []
        stale_uh: list[str] = []
        success_doubt: list[tuple[str, str]] = []
        for row in rows:
            gid = str(row.get("golden_id") or "")
            gold_label = str((row.get("golden") or {}).get("label") or "")
            frozen_branch = str((row.get("code") or {}).get("branch") or "")
            outcome = _archive_outcomes(
                str(row.get("task_id") or ""), str(row.get("run_id") or "")
            ).get(str(row.get("url") or ""))
            if outcome is None:
                continue
            live_branch = str(outcome.get("branch") or "")
            if live_branch != frozen_branch:
                drift.append((gid, frozen_branch, live_branch, gold_label))
            if gold_label == "unreachable_hard" and live_branch == "unresolved":
                stale_uh.append(gid)
            if gold_label == "success" and live_branch in (
                "unresolved",
                "broken",
                "component_unverified",
                "no_play_control",
            ):
                success_doubt.append((gid, live_branch))

        totals["rows"] += len(rows)
        totals["drift"] += len(drift)
        totals["stale_uh"] += len(stale_uh)
        totals["success_doubt"] += len(success_doubt)
        print(f"== {split}（{len(rows)} 行）")
        print(f"   底料漂移（冻结 code.branch ≠ 今天存档）: {len(drift)} 行")
        for gid, fb, lb, gl in drift:
            print(f"     {gid}  冻结 {fb} → 今天 {lb}   金标 {gl}")
        print(f"   族 B（金标 unreachable_hard，Q-23 已推翻）: {len(stale_uh)} 行")
        for gid in stale_uh:
            print(f"     {gid}")
        print(f"   金标写 success 而存档说「没打开/坏/未验证」: {len(success_doubt)} 行")
        for gid, lb in success_doubt:
            print(f"     {gid}  今天存档 = {lb}")
        print()

    print(
        f"合计：{totals['rows']} 行 · 漂移 {totals['drift']}"
        f" · 族 B {totals['stale_uh']} · success 存疑 {totals['success_doubt']}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
