"""存量轨迹数据盘点（只读）：把盘上「已经跑出来的轨迹数据」按层摊开。

用途：回答「现在到底有哪些轨迹数据、每层多少、哪些是交付面、哪些有洞」。
**只读脚本，不写任何产物**；判读口径以人读 evidence 为准（门禁不管）。

读法::

    uv run python trajectory_pipeline/research/probes/corpus_survey.py
    uv run python trajectory_pipeline/research/probes/corpus_survey.py --json

⚠️ **口径边界**（三条，读数前先看）：

1. **P1 主档 / 复核档不是两份数据**——``<task>__<run>.reviewed.json`` 是同一条
    run 的**人工复核版**，同名时它取代主档。所以「唯一轨迹数」按 ``task__run``
    去重，**不是**文件数。
2. **交付面 ≠ 存量面**——按方案 23 R-5，``camofox-1`` 是现行交付面，
    ``obscura-1`` / ``camoufox-1`` 是**只读存量**（不再产出，仍读得懂）。
    混池即错（台账 H-14 / M-21）。
3. **P2 的 ``outcome`` 取末条结论**，站点级细节在 ``site_outcomes[]``——
    **一条 = 一条轨迹 = 一个 Run**（per-Run 口径），两层分母不能混。
4. **旧面默认不再重渲染**（owner 裁定 2026-11-10）——``cli build`` / ``cli task-view``
    **只构交付面**；旧面 P2 / P3 / T 要重建得显式 ``--include-legacy``。
    ⇒ **「旧面没有 P2」是口径、不是缺口**：旧面 P1 源档仍在盘上（可重建），
    但按口径不渲染。读「洞」的时候这两类必须分开（见 ``gaps`` 的两个字段）。

与 [`corpus_inventory.py`](corpus_inventory.py) 的分工：那个脚本只盘 P1
主档（按 task 汇总分支/面/耗时），这个脚本**跨层**（P1 → P2 → P3 → T →
负样本池），并做层间一致性核对（哪些 P1 没有 P2、哪些 P2 没有 T 视图）。
"""

from __future__ import annotations

import argparse
import collections
import json
from pathlib import Path

PIPELINE = Path("trajectory_pipeline/output/pipeline")

#: 顶层非轨迹产物（探测 / 评估 / 计划文件），不算 P1 存档。
_NOT_ARCHIVE_PREFIX = ("plan_", "eval_", "inc73", "agreement_", "compare_", "probe_observe")

#: 现行交付面（方案 23 R-5）。
DELIVERY_SURFACE = "camofox-1"

#: driver → 工具面。口径与 ``toolcatalog.surface_of_driver`` 一致，但盘点脚本
#: **不 import 包**（它是跨层只读探针）：认不出的 driver 归 ``"?"`` 而不是抛——
#: 盘点要的是「读得出来」，不是「配错就崩」。``driver=None`` 落 obscura-1
#: （早期档的默认面），与 toolcatalog 的 ``DEFAULT_SURFACE`` 同。
_DRIVER_SURFACE = {"camofox": "camofox-1", "camoufox": "camoufox-1", "obscura": "obscura-1"}


def _surface_of_driver(driver) -> str:
    """driver → 工具面（认不出的归 ``"?"``，不抛）。"""
    if driver is None:
        return "obscura-1"
    return _DRIVER_SURFACE.get(str(driver), "?")


def _is_p1_archive(path: Path) -> bool:
    """P1 存档的判别式：顶层 JSON 且带 ``ledger``（复核档同样算）。"""
    if path.suffix != ".json" or path.name.startswith(_NOT_ARCHIVE_PREFIX):
        return False
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    return isinstance(data, dict) and "ledger" in data and "candidates" in data


def _read_jsonl(path: Path) -> list[dict]:
    rows: list[dict] = []
    if not path.exists():
        return rows
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return rows


def _load_layer() -> dict:
    """P1 按 task__run 去重（复核档优先）。"""
    p1: dict[str, dict] = {}
    reviewed: set[str] = set()
    if PIPELINE.exists():
        for path in sorted(PIPELINE.glob("*.json")):
            if not _is_p1_archive(path):
                continue
            if path.name.endswith(".reviewed.json"):
                key = path.name[: -len(".reviewed.json")]
                reviewed.add(key)
            else:
                key = path.stem
            p1[key] = json.loads(path.read_text(encoding="utf-8"))
    return {"p1": p1, "reviewed": reviewed}


def survey() -> dict:
    """跨层盘点，返回机器可读结构。"""
    layer = _load_layer()
    p1 = layer["p1"]
    reviewed = layer["reviewed"]

    samples_dir = PIPELINE / "samples"
    p2 = {}
    if samples_dir.exists():
        for p in sorted(samples_dir.glob("*.json")):
            p2[p.stem] = json.loads(p.read_text(encoding="utf-8"))

    legacy_face: dict[str, str] = {}
    legacy_dir = PIPELINE / "legacy"
    if legacy_dir.exists():
        for p in sorted(legacy_dir.rglob("samples/*.json")):
            legacy_face[p.stem] = p.parts[-3] if len(p.parts) >= 3 else "?"

    # 旧面 P1 源档（可重建，但按口径默认不重渲染）——「旧面没有 P2」是口径不是缺口
    legacy_source = {
        k for k, v in p1.items()
        if _surface_of_driver((v or {}).get("driver")) != DELIVERY_SURFACE
    }

    views_dir = PIPELINE / "views"
    views_by_sample: dict[str, list[str]] = collections.defaultdict(list)
    if views_dir.exists():
        for p in views_dir.glob("*.json"):
            stem, kind = p.name.rsplit("_", 1)
            views_by_sample[stem].append(kind.replace(".json", ""))

    tv_dir = PIPELINE / "task_views"
    task_views = {p.stem for p in tv_dir.glob("*.json")} if tv_dir.exists() else set()

    def face_of(stem: str) -> str:
        """P1 属于哪个面 + P2 构没构（面由 driver 折算，不靠产物目录反推）。"""
        d = p1.get(stem) or {}
        surface = _surface_of_driver(d.get("driver"))
        if stem in p2:
            state = "已构建"
        elif stem in legacy_face:
            state = "存量面"
        else:
            state = "未构建"
        return f"{surface}({state})"

    # —— 分支分布：P1 ledger（站点级累计）——
    site_branch = collections.Counter()
    for d in p1.values():
        for branch, n in ((d.get("ledger") or {}).get("by_branch") or {}).items():
            site_branch[branch] += int(n)

    # —— P2 两层分母 ——
    run_branch = collections.Counter()
    p2_site_branch = collections.Counter()
    gate = collections.Counter()
    succ = collections.Counter()
    titles = collections.Counter()
    for d in p2.values():
        outcome = d.get("outcome") or {}
        run_branch[str(outcome.get("branch"))] += 1
        gate[(d.get("gate") or {}).get("status")] += 1
        succ[str(outcome.get("succeeded"))] += 1
        titles[d.get("title")] += 1
        for site in d.get("site_outcomes") or []:
            p2_site_branch[str((site.get("outcome") or {}).get("branch"))] += 1

    pool = _read_jsonl(PIPELINE / "negative.jsonl")

    sites = sum(int((d.get("ledger") or {}).get("total_sites") or 0) for d in p1.values())
    succeeded = sum(int((d.get("ledger") or {}).get("succeeded") or 0) for d in p1.values())

    return {
        "p1": {
            "unique_runs": len(p1),
            "reviewed_runs": len(reviewed),
            "unreviewed_runs": len(p1) - len(reviewed),
            "tasks": len({d.get("task_id") for d in p1.values()}),
            "driver": dict(collections.Counter(str(d.get("driver")) for d in p1.values())),
            "executor_mode": dict(collections.Counter(str(d.get("executor_mode")) for d in p1.values())),
            "perceptor": dict(collections.Counter(str(d.get("perceptor")) for d in p1.values())),
            "engine": dict(collections.Counter(str((d.get("run_config") or {}).get("engine")) for d in p1.values())),
            "face": dict(collections.Counter(face_of(k) for k in p1)),
            "sites_total": sites,
            "sites_succeeded": succeeded,
            "success_rate": round(succeeded / sites, 4) if sites else 0.0,
            "site_branch": dict(site_branch.most_common()),
        },
        "p2": {
            "delivery_samples": len(p2),
            "legacy_samples": len(legacy_face),
            "legacy_by_face": dict(collections.Counter(legacy_face.values())),
            "legacy_source_runs": len(legacy_source),
            "tasks": len({d.get("task_id") for d in p2.values()}),
            "gate": dict(gate),
            "succeeded": dict(succ),
            "run_branch": dict(run_branch.most_common()),
            "site_branch": dict(p2_site_branch.most_common()),
            "site_rows": sum(len(d.get("site_outcomes") or []) for d in p2.values()),
            "actions_total": sum(len(((d.get("six") or {}).get("actions")) or []) for d in p2.values()),
            "titles": dict(titles.most_common()),
            "unique_titles": len(titles),
            "top_title": titles.most_common(1)[0] if titles else None,
        },
        "p3": {
            "view_files": sum(len(v) for v in views_by_sample.values()),
            "samples": len(views_by_sample),
            "kinds": dict(collections.Counter(k for v in views_by_sample.values() for k in v)),
        },
        "task_view": {"files": len(task_views)},
        "pool": {
            "rows": len(pool),
            "unique_urls": len({r.get("url") for r in pool}),
            "unique_task_url": len({(r.get("task_id"), r.get("url")) for r in pool}),
            "by_branch": dict(collections.Counter(r.get("branch") for r in pool).most_common()),
        },
        "gaps": {
            # 真洞：交付面 P1 却没有 P2
            "p1_without_p2": sorted(set(p1) - set(p2) - set(legacy_face) - legacy_source),
            # 旧面未重渲染（口径，**不是**洞）
            "p1_legacy_without_p2": sorted(legacy_source - set(p2) - set(legacy_face)),
            "p2_without_task_view": sorted(set(p2) - task_views),
            "p2_without_views": sorted(set(p2) - set(views_by_sample)),
        },
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", action="store_true", help="输出机器可读")
    args = ap.parse_args()

    if not PIPELINE.exists():
        print(f"目录不存在: {PIPELINE}（从仓库根跑）")
        return 1

    s = survey()

    if args.json:
        print(json.dumps(s, ensure_ascii=False, indent=2))
        return 0

    p1, p2 = s["p1"], s["p2"]
    print("=" * 72)
    print(f"P1  唯一轨迹 {p1['unique_runs']} 条（{p1['reviewed_runs']} 条有人工复核版 / "
          f"{p1['unreviewed_runs']} 条没有）· {p1['tasks']} 个 task")
    print(f"    面分布     {p1['face']}")
    print(f"    driver     {p1['driver']}")
    print(f"    形态       {p1['executor_mode']}   感知层 {p1['perceptor']}")
    print(f"    站点访问   {p1['sites_total']} · 成功 {p1['sites_succeeded']} "
          f"= {p1['success_rate']*100:.1f}%")
    print()
    print(f"P2  交付面 {p2['delivery_samples']} 份 · 存量面（盘上） {p2['legacy_samples']} 份 "
          f"({p2['legacy_by_face']}) · {p2['tasks']} 个 task")
    print(f"    旧面 P1 源档 {p2['legacy_source_runs']} 条 ⇒ 可重建但**默认不重渲染**"
          f"（要建加 --include-legacy）")
    print(f"    末条结论   {p2['run_branch']}")
    print(f"    gate       {p2['gate']}")
    print(f"    站点明细   {p2['site_rows']} 行 · 动作总数 {p2['actions_total']}")
    print(f"    片名       唯一 {p2['unique_titles']} 个 · 最多 {p2['top_title']}")
    print()
    print(f"P3  视图 {s['p3']['view_files']} 份 → {s['p3']['kinds']}（{s['p3']['samples']} 个样本）")
    print(f"T   任务视图 {s['task_view']['files']} 份")
    print()
    pool = s["pool"]
    print(f"池  负样本 {pool['rows']} 行 · 唯一 URL {pool['unique_urls']} · "
          f"唯一 (task,url) {pool['unique_task_url']}")
    for branch, n in pool["by_branch"].items():
        print(f"      {branch:<24}{n:>5}")
    print()
    gaps = s["gaps"]
    print(f"洞  P1 无 P2 {len(gaps['p1_without_p2'])} · P2 无 T 视图 {len(gaps['p2_without_task_view'])} · "
          f"P2 无 P3 视图 {len(gaps['p2_without_views'])}")
    for k in gaps["p1_without_p2"]:
        print(f"      {k}")
    print(f"    旧面未重渲染（**不是洞**） {len(gaps['p1_legacy_without_p2'])} 条 P1 源档"
          f" —— 要建加 --include-legacy")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
