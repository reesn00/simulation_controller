"""从**存量语料**反推「重跑计划」——把「哪些任务必要重跑」变成可执行的计划文件。

为什么不用 ``cli gen`` 重新采样
--------------------------------
``gen`` 采的是**新任务**（骨架 × 人设的随机组合）；**重跑**要的是**同一个任务
实例**再跑一遍——同一 ``task_id``、同一片名、同一检索式、同一句用户原话。
只有这样新旧两份存档才能**逐行对照**，把「口径变了」与「站点变了」分开。
存量 P1 存档里这几个字段全都带着（``task_id`` / ``title`` / ``query`` /
``user_prompt`` / ``provenance``），所以重跑计划**从存档反推**，不猜。

「必要重跑」的判据（两条，缺一不可）
----------------------------------
1. **没有现行面的存档**——执行层已从 camoufox 换成 camofox（2026-10-26），
   形态已从 scripted 换成 agent。存量 89 份里只有 12 份是 agent 形态、
   9 份在 camofox 面，覆盖 6 个 ``task_id``。**其余任务在现行面上是空的**。
2. **不是探针产物**——``candidate_source='direct'`` + ``search_url='<proberun>'``
   的存档是直采探针（``P033`` × 7），不属生产语料；``TPIN*`` 不在骨架库里
   且 ``user_prompt`` 为空（人设没落地 ⇒ 产出进不了训练语料）——两者都不重跑。

跑法::

    uv run python trajectory_pipeline/research/recollect/plan_from_corpus.py \
        --out trajectory_pipeline/output/pipeline/plan_rerun_prod_m44.json \
        --batch-id rerun-prod-m44-2026-10-27

    uv run python -m trajectory_pipeline.executor.cli run \
        --plan trajectory_pipeline/output/pipeline/plan_rerun_prod_m44.json \
        --executor-mode agent --driver camofox --perceptor llm

⚠️ **只读脚本**：它读存档、写字计划文件，**不联网、不开浏览器**。
⚠️ **fail-closed**：挑不出合法实例（缺片名 / 检索式 / 用户原话）的任务
**不进计划**，但**要列进 ``skipped``**——静默丢掉等于「少跑了却看不出来」。
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
from typing import Any

#: P1 存档目录（**仓库根相对**）。
PIPELINE = Path("trajectory_pipeline/output/pipeline")

#: 骨架库（判断 task_id 是不是生产语料里的任务）。
SKELETONS = Path("trajectory_pipeline/taskgen/data/tasks.yaml")

#: 现行执行面 / 现行形态。**双条件**才算「已有现行面样本」。
CURRENT_DRIVER = "camofox"
CURRENT_MODE = "agent"


def _is_archive(path: Path) -> bool:
    name = path.name
    if ".reviewed." in name or name.startswith("plan"):
        return False
    return path.suffix == ".json"


def _load_archive(path: Path) -> dict[str, Any] | None:
    """读一份存档；不是 P1 存档就返回 ``None``。"""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(data, dict):
        return None
    if "ledger" not in data or "candidates" not in data:
        return None
    return data


def _skeleton_rows() -> dict[str, dict[str, Any]]:
    """骨架库（生产真源）→ ``task_id`` → 骨架行。**不手抄名单**。"""
    import yaml

    raw = yaml.safe_load(SKELETONS.read_text(encoding="utf-8"))
    rows = raw.get("tasks") if isinstance(raw, dict) else raw
    return {str(r.get("task_id") or ""): r for r in (rows or [])}


def _rendered_by_persona(archived_prompt: str, skeleton: dict[str, Any]) -> bool | None:
    """存档里的用户原话**是不是 persona 渲染出来的**？

    ``provenance.rewritten`` 缺失时（10-11 那一批老存档还没有这个字段）
    **不猜**——拿存档的用户原话与骨架的 ``initial_request`` 比：

    - 一样 ⇒ 没渲染（persona 没落地）⇒ ``rewritten=False``
    - 不一样 ⇒ 渲染过 ⇒ ``rewritten=True``

    比不出来（骨架行缺 ``initial_request``）⇒ 返回 ``None``，
    调用方**跳过这条**——「判不出」不等于「是」。
    """
    if not skeleton:
        return None
    origin = str(skeleton.get("initial_request") or "").strip()
    if not origin:
        return None
    return archived_prompt.strip() != origin


def _is_probe_archive(data: dict[str, Any]) -> bool:
    """直采探针存档（``candidate_source='direct'`` + ``<proberun>`` 搜索 URL）。"""
    return (
        str(data.get("candidate_source") or "") == "direct"
        and str(data.get("search_url") or "") == "<proberun>"
    )


def _instance_of(data: dict[str, Any], path: Path,
                 skeleton: dict[str, Any] | None = None) -> dict[str, Any]:
    """把一份存档翻译成一条计划实例。

    字段对位：``query`` → ``search_query``、``user_prompt`` → ``prompt_text``。
    ``retrieval_mode`` 写死 ``single_title``——**有片名且检索式含片名**才是
    W1 可跑的单站任务；不符的实例在 :func:`_pick_instance` 里被拒掉。

    ⚠️ ``rewritten`` **不猜**：``provenance`` 里没有就与骨架原文比
    （见 :func:`_rendered_by_persona`），比不出来标 ``None``。
    """
    provenance = dict(data.get("provenance") or {})
    provenance.setdefault("persona_id", "")
    prompt = str(data.get("user_prompt") or "")
    rewritten: bool | None = provenance.get("rewritten")
    if rewritten is None:
        rewritten = _rendered_by_persona(prompt, skeleton or {})
    return {
        "task_id": str(data.get("task_id") or ""),
        "persona_id": str(provenance.get("persona_id") or ""),
        "scenario_id": "media_lookup_standard",
        "prompt_text": prompt,
        "search_query": str(data.get("query") or ""),
        "has_standard": True,
        "retrieval_mode": "single_title",
        "title": str(data.get("title") or ""),
        "rewritten": rewritten,
        "provenance": provenance,
    }


def _reject_reason(inst: dict[str, Any]) -> str:
    """一条实例为什么不能跑。**返回空串 = 可以跑**。

    四条硬条件与 :mod:`executor.plan` 的消费端对齐：

    - 片名非空——判断点 ① 的唯一判据来源；
    - 检索式非空——否则退化成搜空串，产出的是搜索引擎首页的候选；
    - 用户原话非空——否则 P2 六件套第 ③ 件恒为空，进不了训练语料；
    - **``persona_id`` 非空**——消费端 :func:`executor.plan.parse_plan`
      对它是 ``_require_str``，**空值直接 PlanError 把整份计划打掉**。
      ⚠️ 这一条不是形式主义：p0 校准跑会**覆盖 provenance**（只剩
      ``p0_target`` / ``p0_source``），那份存档往往站点最多——**挑它当
      重跑实例就是挑一份 persona 切片不可用的素材**。
    """
    if not inst["title"].strip():
        return "no_title"
    if not inst["search_query"].strip():
        return "empty_query"
    if not inst["prompt_text"].strip():
        return "empty_prompt"
    if not inst["persona_id"].strip():
        return "no_persona_provenance"
    if inst["rewritten"] is None:
        # 判不出「persona 有没有落地」⇒ 不进计划（fail-closed）。
        return "rewritten_unknown"
    if not inst["rewritten"]:
        return "not_rewritten"
    if inst["title"] not in inst["search_query"]:
        # 检索式里没有片名 ⇒ 这不是「单站可播放页」判据下的那个任务
        return "title_not_in_query"
    return ""


def _pick_instance(archives: list[tuple[Path, dict[str, Any]]],
                   skeleton: dict[str, Any] | None = None
                   ) -> tuple[dict | None, str]:
    """从同一个 task 的多份存档里挑一条**当重跑实例**。

    排序键（从强到弱）：**persona 维度齐全** → 站点数 → 负样本数 →
    文件名（稳定）。

    ⚠️ **persona 维度排在站点数前面**：站点最多的那份常常是 p0 校准跑，
    而它的 provenance 被覆盖成人设无关的 ``p0_target``。拿它当重跑实例，
    新存档的 persona 切片就是空的——**站点多 ≠ 能用**。

    ⚠️ **挑不出就返回 ``None`` + 理由**——不猜、不拿残档顶替。
    """
    best: tuple[tuple[int, int, int, str], dict[str, Any]] | None = None
    reasons: list[str] = []
    for path, data in archives:
        inst = _instance_of(data, path, skeleton)
        reason = _reject_reason(inst)
        if reason:
            reasons.append(f"{path.name}:{reason}")
            continue
        led = data.get("ledger") or {}
        key = (
            1 if inst["persona_id"].strip() else 0,
            int(led.get("total_sites") or 0),
            int(led.get("negative_samples") or 0),
            path.name,
        )
        if best is None or key > best[0]:
            best = (key, inst)
    if best is None:
        return None, ("no_usable_instance:" + ",".join(reasons[:3])) if reasons else "no_instance"
    # 留个来源锚点：这条重跑实例是从哪份存档反推的（人核时对得上）
    best[1]["_source"] = best[0][3]
    return best[1], ""


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", required=True, help="计划落盘路径")
    ap.add_argument("--batch-id", default="", help="批次身份（写进计划顶层）")
    ap.add_argument("--include-covered", action="store_true",
                    help="把「已有现行面存档」的任务也放进计划（默认不放——"
                         "它们**不**属于必要重跑）")
    ap.add_argument("--dry-run", action="store_true", help="只打印，不落盘")
    args = ap.parse_args()

    if not PIPELINE.exists():
        print(f"[FAIL] 存档目录不存在：{PIPELINE}（从仓库根跑）")
        return 2

    skeleton_rows = _skeleton_rows()
    skeleton_ids = set(skeleton_rows)

    groups: dict[str, list[tuple[Path, dict[str, Any]]]] = {}
    probes: list[str] = []
    non_skeleton: dict[str, list[str]] = {}
    for path in sorted(PIPELINE.glob("*.json")):
        if not _is_archive(path):
            continue
        data = _load_archive(path)
        if data is None:
            continue
        tid = str(data.get("task_id") or "")
        if _is_probe_archive(data):
            probes.append(f"{tid}:{path.name}")
            continue
        if tid not in skeleton_ids:
            non_skeleton.setdefault(tid, []).append(path.name)
            continue
        groups.setdefault(tid, []).append((path, data))

    rows: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    instances: list[dict[str, Any]] = []
    sources: dict[str, str] = {}
    for tid, archives in sorted(groups.items()):
        covered = any(
            str(d.get("driver") or "") == CURRENT_DRIVER
            and str(d.get("executor_mode") or "") == CURRENT_MODE
            for _, d in archives
        )
        sites = max(int(((d.get("ledger") or {}).get("total_sites")) or 0)
                    for _, d in archives)
        neg = sum(int(((d.get("ledger") or {}).get("negative_samples")) or 0)
                  for _, d in archives)
        inst, reason = _pick_instance(archives, skeleton_rows.get(tid))
        if inst is None:
            skipped.append({"task_id": tid, "reason": reason,
                            "archives": [p.name for p, _ in archives]})
            continue
        rows.append({"task_id": tid, "n_archives": len(archives), "sites": sites,
                     "neg": neg, "covered": covered})
        if covered and not args.include_covered:
            skipped.append({"task_id": tid, "reason": "covered_by_current_face",
                            "detail": f"已有 {CURRENT_DRIVER}/{CURRENT_MODE} 存档"})
            continue
        instances.append(inst)
        sources[tid] = inst.pop("_source", "")

    # 排序：**残档优先**（现存一条站点都没采到 ⇒ 现在等于没数据），
    # 其次按现存负样本数升序（语料最薄的地方先补），最后按 task_id 稳定排。
    meta = {r["task_id"]: r for r in rows}
    instances.sort(key=lambda i: (
        0 if meta[i["task_id"]]["sites"] == 0 else 1,
        meta[i["task_id"]]["neg"],
        i["task_id"],
    ))

    report = {
        "purpose": "从存量语料反推的重跑计划：同一任务实例重跑（可逐行对照）",
        "current_face": {"driver": CURRENT_DRIVER, "mode": CURRENT_MODE},
        "corpus": {
            "archives_scanned": sum(len(v) for v in groups.values()),
            "task_ids": len(groups),
            "covered_task_ids": sum(1 for r in rows if r["covered"]),
            "rerun_task_ids": len(instances),
        },
        "excluded": {
            "probe_archives": probes,
            "not_in_skeleton_library": non_skeleton,
        },
        "skipped": skipped,
        "tasks": rows,
        "instance_source": sources,
    }
    plan = {
        "batch_id": args.batch_id,
        "report": report,
        "instances": instances,
    }

    print(f"存档扫描：{report['corpus']['archives_scanned']} 份 / "
          f"{report['corpus']['task_ids']} 个 task")
    print(f"  已有现行面（{CURRENT_DRIVER}/{CURRENT_MODE}）存档："
          f"{report['corpus']['covered_task_ids']} 个 task")
    print(f"  ⇒ 计划里（必要重跑）：{report['corpus']['rerun_task_ids']} 个 task")
    if probes:
        print(f"  排除探针存档 {len(probes)} 份（candidate_source=direct）")
    for tid, files in sorted(non_skeleton.items()):
        print(f"  排除 {tid}：不在骨架库（{len(files)} 份）")
    for s in skipped:
        print(f"  跳过 {s['task_id']}：{s['reason']}"
              + (f"（{s.get('detail', '')}）" if s.get("detail") else ""))
    print()
    for n, inst in enumerate(instances, 1):
        m = meta[inst["task_id"]]
        print(f"  [{n:>2}/{len(instances)}] {inst['task_id']:<6} "
              f"存量{m['n_archives']}份/{m['sites']}站/{m['neg']}负 "
              f"→ {inst['title']} | {inst['persona_id']}")

    if args.dry_run:
        print("\n--dry-run：未落盘")
        return 0
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(plan, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n→ {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
