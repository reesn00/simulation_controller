"""量一个批次的**失败模式画像**：每条 outcome 的证据归到哪一族，各族多少。

为什么要有这支
--------------
「重跑有没有用」不看**分支分布**（那个混着真实结论），看的是
**采集侧/环境侧失败模式的分布**——重跑该把「采集侧硬故障」换掉，
如果换成了另一种采集侧失败（例：会话失效 → 站点遍历超时），
那就是**换了症状没换病**，必须说清楚。

⚠️ **判据只认证据首段的固定文案**（采集侧写死，不随站点变）
⇒ 分类稳；自由文本（如「已找到播放页但点不开」）**不算**失败模式
——那是**真实结论**，归 ``结论类``。

用法（**必须在仓库根跑**）::

    uv run python trajectory_pipeline/research/probes/failure_mode_profile.py \\
        --batch rerun-prod-m44-2026-10-27

    uv run python trajectory_pipeline/research/probes/failure_mode_profile.py \\
        --batch rerun-prod-m44-2026-10-27 --batch rerun-hardfail-2026-10-28 --compare

⚠️ **只读脚本**：读存档、打印读数，**不联网、不开浏览器、不改任何东西**。
"""

from __future__ import annotations

import argparse
import json
import pathlib
import sys
from collections import Counter
from typing import Any

PIPELINE = pathlib.Path("trajectory_pipeline/output/pipeline")

#: 证据首段前缀 ⇒ 失败模式名。
#: ⚠️ **判据单点**：这张表 + :func:`mode_of` 是唯一实现。
#: ⚠️ 顺序有意义——先匹配更长的前缀（``B2`` 的两个前缀都比 ``B1`` 长，但互不包含）。
MODES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("B4b·token上限撞顶", ("后端未作答（budget:max_tokens",)),
    ("B4a·后端500", ("后端未作答（http:500）",)),
    ("B2·执行层会话失效", ("执行层会话失效", "导航成功但取不到快照")),
    ("B1·导航失败没打开", ("导航失败（这次没打开",)),
    ("遍历超时", ("站点遍历超时",)),
    ("环境级阻断", ("环境级阻断",)),
)


def mode_of(evidence: str) -> str:
    """这条 outcome 的证据属于哪一族失败？不属于 ⇒ ``""``（真实结论类）。"""
    ev = str(evidence or "")
    for name, prefixes in MODES:
        if any(ev.startswith(p) for p in prefixes):
            return name
    return ""


def _load(path: pathlib.Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def _profile(batch_id: str, only_tasks: set[str] | None = None) -> dict[str, Any]:
    """收一个批次的画像。**只收原档**（``.reviewed`` 不收——那是复核后的分支）。

    ⚠️ ``only_tasks`` 是**同任务集对照**用的——重跑批是**专挑有硬故障的任务**
    跑的 ⇒ 拿整批比整批是**条件不同**的比法（分母里混了没被挑中的任务）。
    """
    tasks: dict[str, int] = {}
    modes: Counter[str] = Counter()
    branches: Counter[str] = Counter()
    n_outcomes = 0
    n_archives = 0
    for path in sorted(PIPELINE.glob("*.json")):
        if path.name.endswith(".reviewed.json"):
            continue
        data = _load(path)
        if not data:
            continue
        if str(data.get("batch_id") or "") != batch_id:
            continue
        tid = str(data.get("task_id") or "")
        if only_tasks is not None and tid not in only_tasks:
            continue
        n_archives += 1
        for o in data.get("outcomes") or ():
            if not isinstance(o, dict):
                continue
            n_outcomes += 1
            m = mode_of(str(o.get("evidence") or ""))
            if m:
                modes[m] += 1
            else:
                modes["结论类"] += 1
            b = o.get("branch")
            branches[str(b) if b else "(成功·无记账分支)"] += 1
        tasks[tid] = tasks.get(tid, 0) + 1
    return {
        "batch_id": batch_id,
        "archives": n_archives,
        "tasks": len(tasks),
        "outcomes": n_outcomes,
        "modes": dict(modes),
        "branches": dict(branches),
    }


def main(argv: list[str] | None = None) -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, OSError):
        pass
    ap = argparse.ArgumentParser(description="批次失败模式画像")
    ap.add_argument("--batch", action="append", default=[],
                    help="批次身份（可给多次；两次 ⇒ 对照）")
    ap.add_argument("--compare", action="store_true",
                    help="两个批次对照：失败模式占比的变化")
    ap.add_argument("--tasks", default="",
                    help="**同任务集对照**：只取这些 task_id（逗号分隔）——"
                         "重跑批是专挑有硬故障的任务跑的 ⇒ 整批比整批是条件不同的比法")
    args = ap.parse_args(argv)

    if not args.batch:
        ap.error("至少给一个 --batch")

    only = {t.strip() for t in args.tasks.split(",") if t.strip()} or None
    profiles = [_profile(b, only) for b in args.batch]
    for p in profiles:
        print(f"批次 {p['batch_id']}：{p['archives']} 份存档 / {p['tasks']} 个 task / "
              f"{p['outcomes']} 条 outcome")
        if not p["outcomes"]:
            print("  [FAIL] 这批没有 outcome —— 批次身份写错了？")
            continue
        fail = {k: v for k, v in p["modes"].items() if k != "结论类"}
        nfail = sum(fail.values())
        print(f"  失败模式 {nfail}/{p['outcomes']} 条"
              + (f"（{nfail / p['outcomes']:.0%}）" if p["outcomes"] else ""))
        for k, v in sorted(fail.items(), key=lambda kv: -kv[1]):
            print(f"    {k:22s} {v:4d}  {v / p['outcomes']:.0%}")
        print(f"  分支分布：" + ", ".join(
            f"{k}×{v}" for k, v in sorted(p["branches"].items(), key=lambda kv: -kv[1])))

    if args.compare and len(profiles) == 2:
        a, b = profiles
        scope = "**同任务集**" if only else "整批（⚠️ 条件不同：新批是专挑有硬故障的任务重跑的）"
        print(f"\n对照 {a['batch_id']} → {b['batch_id']}（{scope}）：")
        keys = sorted(set(a["modes"]) | set(b["modes"]))
        for k in keys:
            va, vb = a["modes"].get(k, 0), b["modes"].get(k, 0)
            pa = va / a["outcomes"] if a["outcomes"] else 0
            pb = vb / b["outcomes"] if b["outcomes"] else 0
            # ⚠️ **箭头比的是占比**不是条数——分母变了的时候条数会骗人
            arrow = "↑" if pb > pa + 1e-9 else ("↓" if pb < pa - 1e-9 else "=")
            print(f"  {k:22s} {va:4d} ({pa:4.0%}) → {vb:4d} ({pb:4.0%})  {arrow}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
