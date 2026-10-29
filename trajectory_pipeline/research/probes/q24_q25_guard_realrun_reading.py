"""**护栏真跑读数**：把「护栏上线后**真跑**记成了什么分支」读出来，并与旧批次对照。

回答的问题
----------
`q24_q25_guard_replay.py` 量的是**回放**：拿**旧存档** + **新判据**再判一遍，
回答「护栏会动哪些行」。它**不是**真跑读数——真跑读数是
「护栏在**这次运行里**判成了什么」。

本脚本读**存档里实际记下的分支**（``outcomes[].branch`` 与
``outcomes[].evidence``），回答三个问题：

1. **Q-25 甲（终局形态）真跑读数**：agent 放弃收口时，护栏把哪些落成了
   ``broken`` / ``blocked``，哪些仍落 ``unresolved``，以及**为什么**。
2. **Q-24 甲（归因对象）真跑读数**：导航失败落在**搜索引擎中转链**上的有几条，
   其中几条被降成 ``unresolved``（不进池），几条仍记了 ``unreachable_hard``。
3. **上线前后读数差**：同一 ``(task_id, url)`` 在**旧批次**（护栏落地前）与
   **新批次**（护栏落地后）**记的分支不同**的那些行——这就是「护栏真跑
   改变了什么」。

⚠️ **护栏只保证「机器可判」，不保证「判对」**——捞出来的行**仍要人核**
（见 `docs/07-人工复核与判读手册.md`）。本脚本只给读数，**不给结论**。

⚠️ **对照是「同 task_id + 同 url」，不是「同一次运行」**：站点会漂
（同一 URL 两次渲染不同 ⇒ 读数可能翻转），所以差值里**混着站点漂移**，
不是干净的护栏效应。**读差值要连 evidence 一起读**。

用法（**必须在仓库根跑**）::

    uv run python trajectory_pipeline/research/probes/q24_q25_guard_realrun_reading.py
    uv run python trajectory_pipeline/research/probes/q24_q25_guard_realrun_reading.py \\
        --batch guard-rerun-2026-10-28 --json <报告.json>
"""

from __future__ import annotations

import argparse
import collections
import json
import pathlib
import sys

sys.stdout.reconfigure(encoding="utf-8")

ROOT = pathlib.Path("trajectory_pipeline")
PIPELINE = ROOT / "output" / "pipeline"

from trajectory_pipeline.executor.steps.search import engine_redirect_engine

#: agent 放弃收口在 visit note 里的**可识别签名**
#: （``agent_visit`` 写 ``agent 未到达：{kind}``）。
GIVE_UP_MARKER = "agent 未到达：gave_up"

#: Q-25 甲的判据文本特征（``branches.terminal_shape_branch`` 写的依据）。
SHAPE_MARK = "页面正文命中终局形态"
#: Q-24 甲的判据文本特征（``reachability.probe`` 写的 reason）。
REDIRECT_MARK = "中转链"


def _load(path: pathlib.Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}
    return data if isinstance(data, dict) else {}


def _give_up_visits(data: dict) -> list[dict]:
    out = []
    for visit in data.get("visits") or ():
        if not isinstance(visit, dict):
            continue
        notes = " ".join(str(n) for n in (visit.get("notes") or ()))
        if GIVE_UP_MARKER in notes:
            out.append(visit)
    return out


def _outcome_of(data: dict, visit: dict) -> dict:
    """给一个 give-up visit 找它那条 outcome（按落地页 / 候选 URL 对齐）。"""
    targets = {str(visit.get("landed_url") or ""), str(visit.get("url") or "")}
    for out in data.get("outcomes") or ():
        if not isinstance(out, dict):
            continue
        if str(out.get("url") or "") in targets:
            return out
    return {}


def main() -> int:
    ap = argparse.ArgumentParser(description="护栏真跑读数")
    ap.add_argument("--batch", default="guard-rerun-2026-10-28",
                    help="本批（护栏上线后）的 batch_id")
    ap.add_argument("--compare-batches", default="",
                    help="对照批的 batch_id（逗号分隔）；留空 = 与「除本批外"
                         "所有 camofox/agent 存档」对照")
    ap.add_argument("--json", default="", help="结构化读数落盘路径")
    args = ap.parse_args()

    new: list[tuple[str, dict]] = []
    old: list[tuple[str, dict]] = []
    for path in sorted(PIPELINE.glob("*.json")):
        data = _load(path)
        if not data or "outcomes" not in data:
            continue
        batch = str(data.get("batch_id") or "")
        if batch == args.batch:
            new.append((path.name, data))
        elif args.compare_batches:
            if batch in {b.strip() for b in args.compare_batches.split(",") if b.strip()}:
                old.append((path.name, data))
        else:
            old.append((path.name, data))

    print(f"本批 {args.batch}：{len(new)} 份存档")
    print(f"对照批：{len(old)} 份存档")

    # ── §A Q-25 甲（终局形态）真跑读数 ──────────────────────────────
    give_up = 0
    hit = 0
    hit_branch: collections.Counter[str] = collections.Counter()
    miss_branch: collections.Counter[str] = collections.Counter()
    downgrade = 0
    other_host = 0
    no_obs = 0
    hit_rows: list[dict] = []

    for name, data in new:
        for visit in _give_up_visits(data):
            give_up += 1
            out = _outcome_of(data, visit)
            evidence = str(out.get("evidence") or "")
            branch = str(out.get("branch") or "")
            landed = str(visit.get("landed_url") or "")[:100]
            if SHAPE_MARK in evidence:
                hit += 1
                hit_branch[branch or "（空）"] += 1
                if "前置事实不成立" in evidence:
                    downgrade += 1
                if "归因对象不明" in evidence:
                    other_host += 1
                hit_rows.append({
                    "archive": name, "task_id": data.get("task_id"),
                    "branch": branch, "evidence": evidence,
                    "landed_url": landed, "url": str(out.get("url") or "")[:100],
                })
            else:
                miss_branch[branch or "（空）"] += 1

    print("\n=== §A Q-25 甲（终局形态）真跑读数 ===")
    print(f"  本批「agent 放弃收口」的 visit：{give_up} 条")
    print(f"  护栏命中（判据文本在案）：{hit} 条 · 分支分布 {dict(hit_branch)}")
    print(f"    其中「正文命中但前置事实不成立」⇒ 降级：{downgrade} 条")
    print(f"    其中「归因对象不明」：{other_host} 条")
    print(f"  护栏不命中（仍落 unresolved）：{sum(miss_branch.values())} 条"
          f" · 分支分布 {dict(miss_branch)}")
    for row in hit_rows:
        print(f"    {row['archive']} [{row['task_id']}] → {row['branch']}  "
              f"{row['evidence'][:80]}  [{row['landed_url']}]")

    # ── §B Q-24 甲（归因对象）真跑读数 ──────────────────────────────
    nav_fail = 0
    nav_fail_redirect = 0
    downgraded = 0
    still_hard = 0
    uh_total = 0
    uh_redirect = 0
    for name, data in new:
        for out in data.get("outcomes") or ():
            if not isinstance(out, dict):
                continue
            branch = str(out.get("branch") or "")
            evidence = str(out.get("evidence") or "")
            url = str(out.get("url") or "")
            engine = engine_redirect_engine(url)
            if branch == "unreachable_hard":
                uh_total += 1
                if engine:
                    uh_redirect += 1
            if "导航失败" in evidence:
                nav_fail += 1
                if engine:
                    nav_fail_redirect += 1
                    if REDIRECT_MARK in evidence:
                        downgraded += 1
            if branch == "unreachable_hard" and engine:
                still_hard += 1

    print("\n=== §B Q-24 甲（归因对象）真跑读数 ===")
    print(f"  本批 outcome 总数："
          f"{sum(len(d.get('outcomes') or ()) for _, d in new)} 条")
    print(f"  导航失败（evidence 含「导航失败」）：{nav_fail} 条"
          f" · 其中落在搜索引擎中转链上：{nav_fail_redirect} 条")
    print(f"    ⇒ 护栏触发并降为 unresolved：{downgraded} 条")
    print(f"  unreachable_hard outcome：{uh_total} 条"
          f" · 其中 URL 是中转链：{uh_redirect} 条（⇒ 应为 0）")
    print(f"  仍记 unreachable_hard 且 URL 是中转链：{still_hard} 条"
          f"（⇒ 应为 0）")

    # ── §C 上线前后读数差（同 task_id + 同 url）─────────────────────
    def _branch_map(rows: list[tuple[str, dict]]) -> dict[tuple[str, str], tuple[str, str]]:
        out: dict[tuple[str, str], tuple[str, str]] = {}
        for _, data in rows:
            tid = str(data.get("task_id") or "")
            for o in data.get("outcomes") or ():
                if not isinstance(o, dict):
                    continue
                url = str(o.get("url") or "")
                if not url:
                    continue
                out[(tid, url)] = (str(o.get("branch") or ""), str(o.get("evidence") or ""))
        return out

    m_new = _branch_map(new)
    m_old = _branch_map(old)
    common = sorted(set(m_new) & set(m_old))
    diff = [(k, m_old[k], m_new[k]) for k in common if m_old[k][0] != m_new[k][0]]

    print("\n=== §C 上线前后读数差（同 task_id + 同 url）===")
    print(f"  两批都有记录的 (task_id, url)：{len(common)} 条")
    print(f"  分支不同：{len(diff)} 条")
    for (tid, url), (ob, _), (nb, ne) in diff:
        print(f"    {tid}  {ob or '（空）'} → {nb or '（空）'}  {url[:80]}")
        print(f"        新 evidence: {ne[:120]}")

    payload = {
        "batch": args.batch,
        "new_archives": [n for n, _ in new],
        "compare_archives": [n for n, _ in old],
        "q25": {
            "give_up_visits": give_up, "hits": hit,
            "hit_branches": dict(hit_branch), "downgraded": downgrade,
            "attribution_unknown": other_host,
            "miss_branches": dict(miss_branch), "hit_rows": hit_rows,
        },
        "q24": {
            "nav_fail": nav_fail, "nav_fail_redirect": nav_fail_redirect,
            "downgraded": downgraded,
            "unreachable_hard": uh_total, "unreachable_hard_redirect": uh_redirect,
            "still_hard_redirect": still_hard,
        },
        "diff": [
            {"task_id": tid, "url": url,
             "old_branch": ob, "new_branch": nb, "new_evidence": ne}
            for (tid, url), (ob, _), (nb, ne) in diff
        ],
    }
    if args.json:
        out = pathlib.Path(args.json)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(payload, ensure_ascii=False, indent=2),
                       encoding="utf-8")
        print(f"\n结构化读数落盘：{out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
