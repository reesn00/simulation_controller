"""**两代存档的逐 URL 对照**——同一件事，两代跑出**互相矛盾的结论**。

2026-10-22 的两批 camoufox 生产批次（修复前 / 修复后跑**同一份 12 条计划**）
给出这个能力：修复后驱动不再「读到一半」，所以**同一个 URL** 上两代的结论
可以直接对撞。

**为什么要对撞**：修复前那批的结论有一类不是页面事实（读到了半页），
而那一类**在存档里看不出异常**——它只会变成**理由通顺的假负样本**。
能把它点出来的唯一办法是**换一个读数更真的运行去读同一页**。

⇒ **本探针只报读数，不下结论**——「这一行该不该留」归人（CLAUDE.md：
「判断『结论对不对』不在门禁里」）。

⚠️ **对撞的两侧必须是「同一批页面」才有意义**：两批跑的是**同一份计划**
⇒ 检索式、persona、候选集生成口径一致；但**站点会漂**（同一条计划的两次
运行拿到的候选 URL 不完全相同），所以「只在一代出现」的条目**不算矛盾**，
只算「这一代没走到那页」。

用法（**必须在仓库根跑**）::

    python trajectory_pipeline/research/probes/compare_generations.py \
        --before trajectory_pipeline/output/pipeline \
        --after trajectory_pipeline/output/pipeline/camoufox_prod2 \
        --driver camoufox
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter, defaultdict
from pathlib import Path

#: 「成功」在 outcome 里的形状：``branch`` 为空（不是某个负分支名）。
#: 依据是 ``ledger.by_branch`` 只统计负分支，而 ``ledger.succeeded``
#: 与「``branch`` 为空」的条数相等（实测 E022：12 条 outcome = 3 负 + 9 成功）。
def _is_success(outcome: dict) -> bool:
    return not str(outcome.get("branch") or "").strip()


def _utf8_console() -> None:
    """把标准输出改成 utf-8（Windows 控制台默认 GBK：中文花屏、``⇒`` 直接抛）。"""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):    # pragma: no cover - 老解释器
            pass


def _collect(root: Path, driver: str,
             run_ids: set[str] | None = None) -> tuple[dict, dict, Counter]:
    """读一个目录里的存档。

    返回 ``(按 (task_id, url) 归的结论, 按 task 的分支分布, 读到的 task 数)``。

    ⚠️ **按 ``driver`` 字段筛**：存量 obscura 批次**没记 ``driver``**
    （该字段 2026-10-22 才进 P1），不筛会把它们一起对撞进来——
    那是**两代执行层混在一张表里**，读数会变成「都不是这两代的数」。
    """
    from trajectory_pipeline.executor import dual_run

    verdicts: dict[tuple[str, str], list[tuple[str, bool]]] = defaultdict(list)
    by_task: Counter = Counter()
    tasks: Counter = Counter()
    for record in dual_run.load_runs(root):
        if str(record.get("driver") or "") != driver:
            continue
        run_id = str(record.get("run_id") or "")
        if run_ids and run_id not in run_ids:
            continue
        task_id = str(record.get("task_id") or "")
        if not task_id:
            continue
        tasks[task_id] += 1
        for outcome in record.get("outcomes") or []:
            url = str(outcome.get("url") or "")
            if not url:
                continue
            branch = str(outcome.get("branch") or "")
            verdicts[(task_id, url)].append((branch, _is_success(outcome)))
            if branch:
                by_task[(task_id, branch)] += 1
    return verdicts, by_task, tasks


def main(argv: list[str] | None = None) -> int:
    _utf8_console()
    ap = argparse.ArgumentParser(description="两代存档的逐 URL 对照")
    ap.add_argument("--before", required=True, help="修复前那代的存档目录")
    ap.add_argument("--after", required=True, help="修复后那代的存档目录")
    ap.add_argument("--driver", default="camoufox",
                    help="只对撞这个 driver 的存档（默认 camoufox）")
    ap.add_argument("--before-run-ids", default="",
                    help="只对撞修复前这几份存档（逗号分隔的 run_id；**先筛后对撞**，"
                         "否则根目录里的存量批次会被一起拉进来）")
    ap.add_argument("--after-run-ids", default="",
                    help="同上，筛修复后那几份")
    ap.add_argument("--out", default="", help="把读数写成 JSON")
    args = ap.parse_args(argv)

    before_root, after_root = Path(args.before), Path(args.after)
    for root in (before_root, after_root):
        if not root.exists():
            print(f"[FAIL] 目录不存在: {root}")
            return 2

    b_ids = {s.strip() for s in args.before_run_ids.split(",") if s.strip()} or None
    a_ids = {s.strip() for s in args.after_run_ids.split(",") if s.strip()} or None
    b_verdicts, b_by_task, b_tasks = _collect(before_root, args.driver, b_ids)
    a_verdicts, a_by_task, a_tasks = _collect(after_root, args.driver, a_ids)

    shared = sorted(set(b_verdicts) & set(a_verdicts))
    print(f"修复前: {len(b_tasks)} 个 task · {sum(len(v) for v in b_verdicts.values())} 条结论")
    print(f"修复后: {len(a_tasks)} 个 task · {sum(len(v) for v in a_verdicts.values())} 条结论")
    print(f"两侧都出现的 (task, url): {len(shared)}")

    # ── 矛盾：同一页，两代结论相反 ──
    contradictions: list[dict] = []
    for key in shared:
        b = b_verdicts[key]
        a = a_verdicts[key]
        b_neg = {br for br, ok in b if not ok}
        b_ok = any(ok for _, ok in b)
        a_neg = {br for br, ok in a if not ok}
        a_ok = any(ok for _, ok in a)
        if b_neg and a_ok and not a_neg:
            kind = "前负后成"
        elif b_ok and a_neg and not b_neg:
            kind = "前成后负"
        elif b_neg and a_neg:
            kind = "都负（分支不同）" if b_neg != a_neg else "都负（同分支）"
        elif b_ok and a_ok:
            kind = "都成"
        else:
            kind = "混合"
        if kind in ("前负后成", "前成后负", "都负（分支不同）"):
            contradictions.append({
                "task_id": key[0], "url": key[1], "kind": kind,
                "before": sorted(b_neg) or ["成功"],
                "after": sorted(a_neg) or ["成功"],
            })

    by_kind: Counter = Counter(c["kind"] for c in contradictions)
    print(f"\n-- 对撞结论 --")
    for kind, n in by_kind.most_common():
        print(f"  {kind}: {n}")

    hard = [c for c in contradictions if c["kind"] in ("前负后成", "前成后负")]
    if hard:
        print(f"\n-- ★互相矛盾（{len(hard)} 条）--")
        for c in hard:
            print(f"  [{c['task_id']}] {c['url'][:76]}")
            print(f"      前: {c['before']}   后: {c['after']}")
    other = [c for c in contradictions if c["kind"] == "都负（分支不同）"]
    if other:
        print(f"\n-- 分支不同（{len(other)} 条）--")
        for c in other:
            print(f"  [{c['task_id']}] {c['url'][:76]}")
            print(f"      前: {c['before']}   后: {c['after']}")

    # ── 逐 task 的分支分布 ──
    print("\n-- 逐 task 分支分布（负分支计数）--")
    for task_id in sorted(set(b_tasks) | set(a_tasks)):
        b_keys = {k: v for k, v in b_by_task.items() if k[0] == task_id}
        a_keys = {k: v for k, v in a_by_task.items() if k[0] == task_id}
        print(f"  {task_id}:  前 {dict(b_keys) or '（无负）'}")
        print(f"        后 {dict(a_keys) or '（无负）'}")

    if args.out:
        payload = {
            "before": str(before_root), "after": str(after_root),
            "shared_pairs": len(shared),
            "contradiction_counts": dict(by_kind),
            "contradictions": contradictions,
        }
        Path(args.out).write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"\n已写 {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
