"""**离线扫 P1 存档**：把方案 21 §R5 的验收标准**机械核一遍**。

为什么要有这个扫描器
--------------------
CLAUDE.md 的固定动作是「**每次真实运行后都要逐条打开存档读 evidence 核对**」，
而 `cli check-archives` 是它的**机械版**——**「结论对不对」不在门禁里**。
方案 21 §R5 的三条验收标准里有两条**恰恰是「结论对不对」那一类**：

1. ``fingerprint`` 非空、每个观察的 ``degraded`` 都能对上那一步的动作；
2. 同一档里没有一字不差重复的失败原因；执行层会话失效不记 ``unreachable_hard``；
3. **``unreachable_hard`` 的每一条，失败都落在「导航」路由上**（建页路由的失败
   一律记 ``unresolved``）——且同一批里同一个 URL 不该有互相矛盾的结论。

⇒ 本扫描器把**能机械判的三条**判完（③ 的后半句「同一 URL 不该有互相矛盾的
结论」要跨档配对，**留给人**）。**它不替人判「结论对不对」**——它只把
**形状不对**的那些行点名。

判据（全部是**结构**，不是猜文本）
--------------------------------

==========  ==========================  ==========================
检查        判据                        违反意味着
==========  ==========================  ==========================
C1 指纹     ``fingerprint`` 非空且 ≥11 键  指纹没补读到（缺陷②）
C2 建页路由  evidence 含                **采集侧故障被写成了站点性质**
            ``camofox_create_tab``
            ⇒ branch ≠
            ``unreachable_hard``
C3 导航路由  branch ==                  ``unreachable_hard`` 不是
            ``unreachable_hard``         站点事实（缺陷⑤）
            ⇒ evidence 含
            ``camofox_navigate``
C4 重复原因  **同一个 URL** 上同一个     死页级联（缺陷④）
            evidence 出现 ≥2 次
C5 假降级    ``degraded`` 含             **拿上一页的账记这一页的头**
            ``press_unsupported``        （缺陷③）
            而该步的动作不是 press
==========  ==========================  ==========================

⚠️ **C2 / C3 只对 camofox 面的存档有意义**——别的执行层的 evidence 没有
``camofox_*`` 字样 ⇒ 那两条**自动不适用**（不是「通过」，是「不适用」）。
⇒ 扫描器**逐档标注「适用 / 不适用」**，**不把「不适用」报成「通过」**。

⚠️ **只读、不改任何产物**。跑法（**必须在仓库根**）::

    uv run python -m trajectory_pipeline.research.probes.camofox_route_attribution_scan \
      --root trajectory_pipeline/output/pipeline

⚠️ **完全离线**（只读 JSON）⇒ **可以和真跑批次并发**，
但**批次正在写盘时读到的可能是不完整的一档** ⇒ 要判批次请**跑完之后**再扫。
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path
from typing import Any

#: 现行 P1 ``fingerprint`` 的**键数**（九项实测 + ``engine`` + ``base_url``）。
FINGERPRINT_KEYS = 11

#: camofox 面上游的两个 tool 名——**判据靠它们**，不靠错误文本。
CREATE_TOOL = "camofox_create_tab"
NAVIGATE_TOOL = "camofox_navigate"

#: 「这一页没了」那一族的错误体里带的 tool（换页自愈那一支）。
RECOVERY_HINT = "执行层换页自愈"


def _out(payload: dict[str, Any]) -> None:
    sys.stdout.reconfigure(encoding="utf-8")
    print(json.dumps(payload, ensure_ascii=False, indent=2))


def _iter_steps(archive: dict[str, Any]) -> list[tuple[str, dict[str, Any]]]:
    """把**档内所有步骤**摊平：``(来源, 步)``。

    ⚠️ **两处都有步骤**：档级 ``steps[]``（agent 循环的主干）与
    每档候选的 ``visits[].steps[]``（候选级）。**两处都要扫**——
    缺陷③（假降级）在两处都出现过。
    """
    out: list[tuple[str, dict[str, Any]]] = []
    for i, step in enumerate(archive.get("steps") or []):
        if isinstance(step, dict):
            out.append((f"steps[{i}]", step))
    for j, visit in enumerate(archive.get("visits") or []):
        if not isinstance(visit, dict):
            continue
        for i, step in enumerate(visit.get("steps") or []):
            if isinstance(step, dict):
                out.append((f"visits[{j}].steps[{i}]", step))
    return out


def _degraded(step: dict[str, Any]) -> list[str]:
    obs = step.get("observation")
    if not isinstance(obs, dict):
        return []
    degraded = obs.get("degraded")
    if isinstance(degraded, (list, tuple)):
        return [str(x) for x in degraded]
    return []


def _tool_of(step: dict[str, Any]) -> str:
    action = step.get("action")
    if not isinstance(action, dict):
        return ""
    return str(action.get("tool") or "")


def check_archive(path: Path) -> dict[str, Any]:
    """核一档，出一张**逐条点名**的账。"""
    try:
        archive = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001 — 扫不出来也要说，不能静默跳过
        return {"file": path.name, "readable": False, "error": f"{type(exc).__name__}: {exc}"}
    if not isinstance(archive, dict) or "outcomes" not in archive:
        return {"file": path.name, "readable": False, "error": "不是 P1 存档（没有 outcomes）"}

    violations: list[dict[str, Any]] = []
    notes: list[str] = []

    # ── C1 指纹 ────────────────────────────────────────────────
    fingerprint = archive.get("fingerprint")
    fp_keys = len(fingerprint) if isinstance(fingerprint, dict) else 0
    if fp_keys < FINGERPRINT_KEYS:
        violations.append({
            "check": "C1 指纹",
            "where": "档级",
            "detail": f"fingerprint 只有 {fp_keys} 个键（现行 {FINGERPRINT_KEYS} 项）",
        })

    # ── C2 / C3 归属：失败落在哪个路由 ─────────────────────────
    outcomes = [o for o in (archive.get("outcomes") or []) if isinstance(o, dict)]
    create_hits = 0
    nav_hits = 0
    unreachable: list[str] = []
    for i, outcome in enumerate(outcomes):
        branch = str(outcome.get("branch") or "")
        evidence = str(outcome.get("evidence") or "")
        url = str(outcome.get("url") or "")
        if CREATE_TOOL in evidence:
            create_hits += 1
            if branch == "unreachable_hard":
                violations.append({
                    "check": "C2 建页路由",
                    "where": f"outcomes[{i}]",
                    "detail": f"建页路由的失败被记成 unreachable_hard：{url[:110]}",
                    "evidence": evidence[:200],
                })
        if branch == "unreachable_hard":
            unreachable.append(url)
            if NAVIGATE_TOOL not in evidence:
                violations.append({
                    "check": "C3 导航路由",
                    "where": f"outcomes[{i}]",
                    "detail": f"unreachable_hard 的失败不在导航路由上：{url[:110]}",
                    "evidence": evidence[:200],
                })
            else:
                nav_hits += 1

    # ── C4 一字不差重复的失败原因（死页级联的指纹）──────────────
    # ⚠️ **判据是「同一个 URL + 同一句 evidence」**，不是「同一档里有两句
    # 一样的 evidence」——后者是**常态**（两个不同的候选撞同一种墙，
    # evidence 自然一模一样）⇒ 那样报就是**噪声**，报多了就没人看。
    # 死页级联的特征是**同一条候选**被反复打，每次都吐出同一句。
    seen: dict[tuple[str, str], int] = {}
    for outcome in outcomes:
        evidence = str(outcome.get("evidence") or "").strip()
        if not evidence:
            continue
        key = (str(outcome.get("url") or ""), evidence)
        seen[key] = seen.get(key, 0) + 1
    for (url, evidence), count in seen.items():
        if count >= 2:
            violations.append({
                "check": "C4 重复原因",
                "where": "档级",
                "detail": f"同一个 URL 上同一个失败原因出现 {count} 次",
                "url": url[:140],
                "evidence": evidence[:200],
            })

    # ── C5 假降级（拿上一页的账记这一页的头）────────────────────
    fake_degraded = 0
    for where, step in _iter_steps(archive):
        degraded = _degraded(step)
        if not degraded:
            continue
        tool = _tool_of(step)
        if "press_unsupported" in degraded and tool != "press":
            fake_degraded += 1
            violations.append({
                "check": "C5 假降级",
                "where": where,
                "detail": f"degraded 含 press_unsupported，但这一步的动作是 {tool or '(无)'}",
            })

    # ── 自愈记账（缺陷④：自愈必须留痕）────────────────────────
    warnings = archive.get("warnings") or []
    recoveries = sum(1 for w in warnings if isinstance(w, str) and RECOVERY_HINT in w)
    if recoveries:
        notes.append(f"换页自愈 {recoveries} 次（运行级 warning 已记账）")

    branches = Counter(str(o.get("branch") or "(null=成功)") for o in outcomes)
    return {
        "file": path.name,
        "readable": True,
        "task_id": archive.get("task_id"),
        "run_id": archive.get("run_id"),
        "driver": archive.get("driver"),
        "batch_id": archive.get("batch_id"),
        "elapsed_ms": archive.get("elapsed_ms"),
        "fingerprint_keys": fp_keys,
        "outcomes": len(outcomes),
        "branches": dict(branches),
        "camofox_applicable": bool(create_hits or nav_hits or unreachable),
        "create_route_failures": create_hits,
        "navigate_route_failures": nav_hits,
        "unreachable_hard": len(unreachable),
        "fake_degraded": fake_degraded,
        "recoveries": recoveries,
        "violations": violations,
        "notes": notes,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="离线扫 P1 存档：把方案 21 §R5 的验收标准机械核一遍"
    )
    parser.add_argument(
        "--root",
        default="trajectory_pipeline/output/pipeline",
        help="P1 存档所在目录（默认生产目录）",
    )
    parser.add_argument(
        "--run-ids",
        default="",
        help="只扫这几个 run_id（逗号分隔；**留空 = 扫全部**）",
    )
    parser.add_argument(
        "--strict",
        action="store_true",
        help="有违反就退非零（默认只报告，不拦）",
    )
    args = parser.parse_args(argv)

    root = Path(args.root)
    if not root.is_dir():
        print(f"目录不存在：{root}", file=sys.stderr)
        return 2

    wanted = {x.strip() for x in args.run_ids.split(",") if x.strip()}
    files = sorted(
        p for p in root.glob("*.json")
        if ".reviewed." not in p.name and p.name.endswith(".json")
    )
    if wanted:
        # 存档名是 ``{task_id}__{run_id}.json`` ⇒ 取最后一段去后缀就是 run_id。
        files = [p for p in files if p.name.split("__")[-1][: -len(".json")] in wanted]

    rows = [check_archive(p) for p in files]
    rows = [r for r in rows if r.get("readable")]

    total_violations = sum(len(r["violations"]) for r in rows)
    by_check: Counter = Counter()
    for r in rows:
        for v in r["violations"]:
            by_check[v["check"]] += 1

    _out({
        "scanned": len(rows),
        "violations_total": total_violations,
        "violations_by_check": dict(by_check),
        "rows": rows,
    })
    if args.strict and total_violations:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
