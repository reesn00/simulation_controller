"""**两条护栏真跑批次**：把 19 个「有 give-up 命中」的任务汇成一份可跑计划。

为什么
----
两条护栏（**Q-24 甲** 归因对象 · `executor/steps/reachability.py::probe`；
**Q-25 甲** 终局形态 · `executor/branches.py::terminal_shape_branch`）
2026-10-28 落地（提交 ``755ac98``）——到今天为止**只有回放读数**
（`q24_q25_guard_replay.py`：全库 210 条「agent 放弃」visit 里**命中 30 条**），
**一次真跑都没经过**。

回放量的是「**护栏会动哪些行**」（拿旧存档 + 新判据再判一遍），
不是「**护栏上线后读数会变成多少**」（后者要等下一批真跑）。
这批要回答的正是后者：把**有 give-up 命中的全部 19 个任务**重跑一遍，
让护栏在**真跑**里自己判一次。

任务来源（**实例不重新反推**）
------------------------------
- **16 个**在 ``output/pipeline/plan_rerun_prod_m44.json`` 里
  （E002 · E021 · T011 · T012 · T013 · T014 · T018 · T024 · T025 ·
  T029 · T030 · T031 · T035 · T037 · T044 · T054）——**直接从现计划按
  task_id 过滤**（那 28 条的检索式 / persona / 片名都是现成的）。
- **3 个**（E014 · E028 · T048）在 m44 的 ``skipped`` 里（当时判
  「已有现行面」）⇒ 从 ``research/ab_baseline/plan-camoufox-prod1-rest.json``
  挑实例。

⚠️ **必须串行**：上游 lock_queue 会销毁别人的页 ⇒ 一批一次一个任务。
⚠️ **本脚本只拼计划，不开浏览器**：跑批是 ``cli run --plan``，不在这里。

用法（**必须在仓库根跑**）::

    uv run python trajectory_pipeline/research/probes/q24_q25_guard_rerun_plan.py
    uv run python trajectory_pipeline/research/probes/q24_q25_guard_rerun_plan.py --chunk-size 5
"""

from __future__ import annotations

import argparse
import json
import pathlib
import sys

sys.stdout.reconfigure(encoding="utf-8")

ROOT = pathlib.Path("trajectory_pipeline")
PIPELINE = ROOT / "output" / "pipeline"

M44_PATH = PIPELINE / "plan_rerun_prod_m44.json"
PROD1REST_PATH = ROOT / "research" / "ab_baseline" / "plan-camoufox-prod1-rest.json"

#: 批次身份。**新批次 ⇒ 新 batch_id**——纪律：「两批计划跑出来的存档必须在
#: ``batch_id`` 上分得开」，哪怕任务 id 一样、文件名一样。
BATCH_ID = "guard-rerun-2026-10-28"

#: 取自 m44 现计划的 16 个。
FROM_M44 = (
    "E002", "E021", "T011", "T012", "T013", "T014", "T018", "T024",
    "T025", "T029", "T030", "T031", "T035", "T037", "T044", "T054",
)
#: 取自 prod1-rest 的 3 个（m44 的 skipped 里）。
FROM_PROD1REST = ("E014", "E028", "T048")


def _load(path: pathlib.Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _pick(plan: dict, task_ids: tuple[str, ...], *, source: str) -> list[dict]:
    """按 task_id 从一份计划里挑实例。**缺一条就报错**——不静默少一条。"""
    by_id: dict[str, dict] = {}
    for row in plan.get("instances") or ():
        tid = str(row.get("task_id") or "")
        if tid and tid not in by_id:
            by_id[tid] = row
    missing = [t for t in task_ids if t not in by_id]
    if missing:
        raise SystemExit(
            f"[FAIL] 计划 {source} 里没有这些 task_id：{', '.join(missing)}"
            f"（共 {len(by_id)} 条实例）。实例不重新反推——缺就是缺，不拿别的顶。"
        )
    out = []
    for tid in task_ids:
        row = dict(by_id[tid])
        row["_source_plan"] = source
        out.append(row)
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description="拼 M-47 护栏真跑批次的执行计划")
    ap.add_argument("--chunk-size", type=int, default=5,
                    help="每个分块计划放几个任务（串行跑，崩了从下一块接着跑）")
    args = ap.parse_args()

    m44 = _load(M44_PATH)
    prod1 = _load(PROD1REST_PATH)

    instances = (
        _pick(m44, FROM_M44, source="plan_rerun_prod_m44")
        + _pick(prod1, FROM_PROD1REST, source="plan-camoufox-prod1-rest")
    )

    report = {
        "purpose": "两条护栏（Q-24 甲 归因对象 / Q-25 甲 终局形态）的**真跑**读数",
        "why": "回放量的是「护栏会动哪些行」，不是「护栏上线后读数会变成多少」",
        "guardrails": [
            "executor/steps/reachability.py::probe（归因对象）",
            "executor/steps/search.py::engine_redirect_engine（识别中转链）",
            "executor/branches.py::terminal_shape_branch（终局形态）",
            "executor/agent_loop.py::give_up / _close_by_page（接入点）",
        ],
        "current_face": {"driver": "camofox", "mode": "agent"},
        "serial": True,
        "serial_why": "上游 lock_queue 会销毁别人的页 ⇒ 必须一次一个任务",
        "instance_source": {
            "plan_rerun_prod_m44": list(FROM_M44),
            "plan-camoufox-prod1-rest": list(FROM_PROD1REST),
        },
        "note": "实例**不重新反推**：m44 那 16 条按 task_id 过滤现计划，"
                "另外 3 条取 prod1-rest。",
        "n_instances": len(instances),
    }
    plan = {
        "batch_id": BATCH_ID,
        "report": report,
        "instances": [{k: v for k, v in row.items() if k != "_source_plan"}
                      for row in instances],
    }

    out = PIPELINE / f"plan_{BATCH_ID}.json"
    out.write_text(json.dumps(plan, ensure_ascii=False, indent=2), encoding="utf-8")

    # ── 校验：用生产侧同一个 loader 过一遍（不自己另写一套判据）────────
    from trajectory_pipeline.executor.plan import load_plan
    loaded = load_plan(out)
    print(f"合并计划落盘：{out}")
    print(f"  batch_id = {loaded.batch_id}")
    print(f"  可跑 {len(loaded.tasks)} 条 / 跳过 {len(loaded.skips)} 条")
    for skip in loaded.skips:
        print(f"    [skip] {skip.task_id}: {skip.reason} — {skip.detail}")
    if len(loaded.tasks) != len(instances):
        print(f"[FAIL] 计划里有 {len(instances)} 条实例，但只有 {len(loaded.tasks)} 条可跑"
              f" ⇒ 不跑（跑少了等于没验）。")
        return 2

    # ── 分块：串行跑，崩了从下一块接着 ────────────────────────────────
    size = max(1, int(args.chunk_size))
    chunks = [loaded.tasks[i:i + size] for i in range(0, len(loaded.tasks), size)]
    print(f"\n分块（每块 {size} 个任务，共 {len(chunks)} 块）：")
    for n, group in enumerate(chunks, 1):
        cpath = PIPELINE / f"plan_{BATCH_ID}_c{n}.json"
        sub = {
            "batch_id": BATCH_ID,
            "report": {**report, "chunk": n, "chunk_of": len(chunks),
                       "chunk_task_ids": [t.task_id for t in group]},
            "instances": [],
        }
        for t in group:
            row = next(r for r in plan["instances"] if r["task_id"] == t.task_id)
            sub["instances"].append(row)
        cpath.write_text(json.dumps(sub, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"  c{n}: {cpath.name}  "
              f"{', '.join(f'{t.task_id}({t.title})' for t in group)}")

    print("\n任务清单：")
    for row in instances:
        print(f"  {row['task_id']:<6} {row.get('title', ''):<16} "
              f"← {row['_source_plan']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
