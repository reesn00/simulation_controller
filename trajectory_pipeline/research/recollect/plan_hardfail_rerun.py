"""从**批次存档**反推「硬故障重跑计划」——把「哪些任务的存档里有采集侧硬故障」
变成可执行的计划文件。

为什么是「硬故障」这一族
------------------------
增量九十六 §7-4 把 28 份批次存档的 428 条 outcome 按「**结论是不是采集侧造成的**」
分了六类，结论是：**B1 / B2 / B4 共 26 条是「同参数重跑就会不同」的**——
网络瞬时（B1）、执行层失效（B2）、LLM 后端 500 / token 预算耗尽（B4）。
⇒ 这 26 条落在 **11 个 task** 上 ⇒ **这 11 个任务必要重跑**。

⚠️ **B3 / B5 / B6（步数预算耗尽 / `press_unsupported` 采集降级 / 死循环）不在本计划里**
——那三族**同参数重跑大概率同样**⇒ 要跑得**先调参**（步数预算、`max_tokens`），
否则只是再烧一遍时间。**D 类（`blocked` 环境级阻断）**也不在：同指纹当天
可能就进得去（方案 20 §R0 实测 12 进 / 8 拦）⇒ 重跑价值低。

判据（**机器码 + 证据首段**，两处都用同一个函数）
------------------------------------------------
硬故障的判据是**证据首段的固定文案**——它由采集侧写死，不随站点变：

======================  ==========================================
``B1``                  证据以 ``导航失败（这次没打开`` 开头
``B2``                  证据以 ``执行层会话失效`` 或
                        ``导航成功但取不到快照`` 开头
``B4a``                 证据以 ``后端未作答（http:500）`` 开头
``B4b``                 证据以 ``后端未作答（budget:max_tokens`` 开头
======================  ==========================================

⚠️ **B4 必须拆成两半**（增量九十六那版把它当一族，是个错）：

* ``B4a``（``http:500``）= **瞬时后端故障** ⇒ 同参数重跑就会不同 ⇒ **该重跑**；
* ``B4b``（``budget:max_tokens 用尽``）= **输出撞了 token 上限** ⇒
  **同一页、同参数重跑大概率同样撞** ⇒ **重跑没用，要调参**
  （证据原文自己写着「调大 TRAJECTORY_LLM_MAX_TOKENS」）⇒
  **不纳入本计划**（与 B3 / B5 / B6 同归「先调参」那一族）。

⚠️ **判据单点**：:func:`hard_kind` 是**唯一**的实现——本脚本、处置台账、
后续复算都从这里取，**不许第二份**（抄一份必然漂）。

实例怎么挑
----------
**用「刚跑过的那份批次存档」反推**，不是从该 task 的**所有**存档里挑
（:mod:`plan_from_corpus` 的 :func:`_pick_instance` 挑的是「站点最多」那份）——
本批要的是**同一个任务实例再跑一遍**（同一 ``task_id``、同一片名、同一检索式、
同一句用户原话）⇒ 新旧两份存档才能**逐行对照**，把「这次没打开」与
「站点变了」分开。

⚠️ **判据不另写**：实例的字段拼装与「能不能跑」的六条硬条件，都
**import** :mod:`plan_from_corpus` 的 :func:`_instance_of` / :func:`_reject_reason`
——**一份判据两处用**。本脚本**只加一条**：实例来源必须是**批次存档**。

fail-closed
-----------
挑不出合法实例（缺片名 / 检索式 / 用户原话 / persona 切片）的任务
**不进计划**，但**要列进 ``skipped``**——静默丢掉等于「少跑了却看不出来」。

用法（**必须在仓库根跑**）::

    uv run python trajectory_pipeline/research/recollect/plan_hardfail_rerun.py \\
        --out trajectory_pipeline/output/pipeline/plan_rerun_hardfail.json \\
        --batch-id rerun-hardfail-2026-10-28

    uv run python -m trajectory_pipeline.executor.cli run \\
        --plan trajectory_pipeline/output/pipeline/plan_rerun_hardfail.json \\
        --executor-mode agent --driver camofox --perceptor llm --log-level info

⚠️ **只读脚本**：读存档、写字计划文件，**不联网、不开浏览器**。
⚠️ **跑必须串行**——上游 ``lock_queue`` 会**毁掉别人的页**⇒ 并发跑互相踩。
"""

from __future__ import annotations

import argparse
import json
import pathlib
import sys
from typing import Any

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parents[2]))

from plan_from_corpus import (  # noqa: E402  —— 一份判据两处用
    _instance_of,
    _is_archive,
    _is_probe_archive,
    _load_archive,
    _reject_reason,
    _skeleton_rows,
)

#: 批次存档目录（**仓库根相对**）。
PIPELINE = pathlib.Path("trajectory_pipeline/output/pipeline")

#: 判据用的批次身份——**增量九十六 §7-4 复核的就是这一批**。
BATCH_ID = "rerun-prod-m44-2026-10-27"

#: **硬故障三族**的证据首段前缀 ⇒ 类别。
#: ⚠️ 这张表是**判据单点**：:func:`hard_kind` 与台账/编年史都指它。
HARD_PREFIXES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("B1", ("导航失败（这次没打开",)),
    ("B2", ("执行层会话失效", "导航成功但取不到快照")),
    ("B4a", ("后端未作答（http:500）",)),
    ("B4b", ("后端未作答（budget:max_tokens",)),
)

#: **同参数重跑就会不同**的那几族 ⇒ 才值得重跑。
#: ⚠️ ``B4b``（token 上限撞顶）**不在内**——同一页同参数重跑大概率同样撞
#: ⇒ 要先调 ``TRAJECTORY_LLM_MAX_TOKENS``（属「先调参」那一族）。
RETRY_WORTHY: frozenset[str] = frozenset({"B1", "B2", "B4a"})


def hard_kind(evidence: str) -> str | None:
    """这条 outcome 是不是**采集侧硬故障**？是 ⇒ 返回族号（``B1``/``B2``/``B4a``/``B4b``）。

    ⚠️ **判据单点**——本函数是唯一实现。理由文本里带的那句
    「导航失败（这次没打开…）」等是**采集侧写死的文案**，不随站点变
    ⇒ 用它分类比解析自由文本稳。
    """
    ev = str(evidence or "")
    for kind, prefixes in HARD_PREFIXES:
        if any(ev.startswith(p) for p in prefixes):
            return kind
    return None


def _scan_batch() -> list[tuple[pathlib.Path, dict[str, Any]]]:
    """扫出**批次身份**那批存档（原档，不含 ``.reviewed``）。"""
    out: list[tuple[pathlib.Path, dict[str, Any]]] = []
    for path in sorted(PIPELINE.glob("*.json")):
        if not _is_archive(path):
            continue
        data = _load_archive(path)
        if data is None:
            continue
        if str(data.get("batch_id") or "") != BATCH_ID:
            continue
        out.append((path, data))
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="硬故障重跑计划")
    ap.add_argument("--out", required=True, help="计划落盘路径")
    ap.add_argument("--batch-id", default="rerun-hardfail-2026-10-28",
                    help="批次身份（写进计划顶层）")
    ap.add_argument("--dry-run", action="store_true", help="只打印，不落盘")
    args = ap.parse_args(argv)

    if not PIPELINE.exists():
        print(f"[FAIL] 存档目录不存在：{PIPELINE}（从仓库根跑）")
        return 2

    skeleton_rows = _skeleton_rows()
    archives = _scan_batch()
    if not archives:
        print(f"[FAIL] 批次 {BATCH_ID} 的存档一份都没有 ⇒ 不生成计划")
        return 2

    # ① 硬故障普查（逐条 outcome）
    per_task: dict[str, dict[str, list[dict[str, Any]]]] = {}
    total_outcomes = 0
    total_hard = 0
    for path, data in archives:
        tid = str(data.get("task_id") or "")
        bucket = per_task.setdefault(tid, {"B1": [], "B2": [], "B4a": [], "B4b": []})
        for out in data.get("outcomes") or ():
            total_outcomes += 1
            kind = hard_kind(str(out.get("evidence") or ""))
            if kind:
                bucket[kind].append({
                    "archive": path.name,
                    "url": str(out.get("url") or ""),
                    "branch": str(out.get("branch") or ""),
                    "evidence": str(out.get("evidence") or "")[:160],
                })
                total_hard += 1

    # ② 挑实例：**批次存档本身**反推（同一任务实例再跑一遍）
    instances: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    sources: dict[str, str] = {}
    tasks: list[dict[str, Any]] = []
    for tid, bucket in sorted(per_task.items()):
        n_hard = sum(len(v) for v in bucket.values())
        if not n_hard:
            continue
        # ⚠️ **只有「同参数重跑就会不同」的那族才值得重跑**——
        # 撞了 token 上限的（B4b）同页同参数重跑大概率同样撞 ⇒ 要**先调参**。
        n_worthy = sum(len(bucket[k]) for k in RETRY_WORTHY if k in bucket)
        if not n_worthy:
            skipped.append({
                "task_id": tid,
                "reason": "needs_param_tuning",
                "detail": "硬故障只有 B4b（token 上限撞顶）⇒ 同参数重跑大概率同样撞；"
                          "要先调 TRAJECTORY_LLM_MAX_TOKENS",
                "by_kind": {k: len(v) for k, v in bucket.items() if v},
            })
            continue
        # 该 task 在这一批里的存档（可能多份；取硬故障最多的那份做实例来源）
        batch_paths = [(p, d) for p, d in archives
                       if str(d.get("task_id") or "") == tid]
        if not batch_paths:
            skipped.append({"task_id": tid, "reason": "no_batch_archive"})
            continue
        picked: tuple[Path, dict[str, Any]] | None = None
        for p, d in batch_paths:
            n = len([o for o in d.get("outcomes") or ()
                     if hard_kind(str(o.get("evidence") or ""))])
            if picked is None or n > picked[1]:
                picked = (p, d)
        assert picked is not None
        path, data = picked
        inst = _instance_of(data, path, skeleton_rows.get(tid))
        reason = _reject_reason(inst)
        if reason:
            skipped.append({"task_id": tid, "reason": reason,
                            "archive": path.name})
            continue
        inst.pop("_source", None)
        inst["_hardfail"] = {k: len(v) for k, v in bucket.items() if v}
        tasks.append({
            "task_id": tid,
            "archive": path.name,
            "hard_failures": n_hard,
            "retry_worthy": n_worthy,
            "by_kind": {k: len(v) for k, v in bucket.items() if v},
            "hard_rows": [r for v in bucket.values() for r in v],
        })
        sources[tid] = path.name
        instances.append(inst)

    # ③ 排序：**可重跑的硬故障多的先跑**（语料最薄的地方先补），其次 task_id 稳定排。
    order = {t["task_id"]: i for i, t in enumerate(
        sorted(tasks, key=lambda t: (-t["retry_worthy"], t["task_id"])))}
    instances.sort(key=lambda i: (order[i["task_id"]], i["task_id"]))

    report = {
        "purpose": "硬故障重跑计划：同一任务实例再跑一遍（同参数重跑就会不同的那批）",
        "criterion": {
            "batch_id": BATCH_ID,
            "note": "B1/B2/B4a = 采集侧硬故障且**同参数重跑就会不同**（网络瞬时 / "
                    "执行层失效 / LLM 后端 500）⇒ 本计划只收这三族；"
                    "B4b（token 上限撞顶）与 B3/B5/B6 要先调参、"
                    "blocked 重跑价值低 ⇒ 都不在本计划里",
            "retry_worthy": sorted(RETRY_WORTHY),
            "prefixes": {k: list(v) for k, v in HARD_PREFIXES},
        },
        "corpus": {
            "archives_scanned": len(archives),
            "outcomes_scanned": total_outcomes,
            "hard_failures": total_hard,
            "tasks_with_hard_failures": len(tasks),
            "tasks_needing_param_tuning": sum(
                1 for s in skipped if s.get("reason") == "needs_param_tuning"),
        },
        "skipped": skipped,
        "tasks": tasks,
        "instance_source": sources,
    }
    plan = {"batch_id": args.batch_id, "report": report, "instances": instances}

    print(f"批次 {BATCH_ID}：{len(archives)} 份存档 / {total_outcomes} 条 outcome")
    print(f"  硬故障 {total_hard} 条 ⇒ 其中「同参数重跑就会不同」的 "
          f"{sum(t['retry_worthy'] for t in tasks)} 条，落在 {len(tasks)} 个 task 上")
    for t in sorted(tasks, key=lambda x: -x["retry_worthy"]):
        print(f"    {t['task_id']:6s} 可重跑 {t['retry_worthy']:3d} 条 / "
              f"共 {t['hard_failures']:3d} 条 {t['by_kind']}  ← {t['archive']}")
    for s in skipped:
        print(f"    ⚠️ {s['task_id']:6s} 跳过：{s['reason']}"
              f"{'（' + str(s.get('by_kind')) + '）' if s.get('by_kind') else ''}")
    print(f"  ⇒ 计划里 {len(instances)} 条（batch_id={args.batch_id}）")

    if args.dry_run:
        print("**dry-run，未落盘**")
        return 0
    out_path = pathlib.Path(args.out)
    out_path.write_text(json.dumps(plan, ensure_ascii=False, indent=2),
                        encoding="utf-8")
    print(f"计划 → {out_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
