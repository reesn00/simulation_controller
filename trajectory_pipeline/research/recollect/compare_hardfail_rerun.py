"""对照「硬故障重跑」的**新旧两批**：那批采集侧硬故障，现在有没有变成真结论。

为什么需要一支对照探针
--------------------
重跑的**价值**不是「又跑了一遍」，而是回答一个问题：
**上次因为采集侧硬故障（网络瞬时 / 执行层失效 / 后端 500）而没拿到结论的那些站，
这次拿到结论了吗？** 拿到 ⇒ 那条从「没结论」变成「有结论（可能是正样本）」；
没拿到 ⇒ 要么站点真变了，要么采集侧还有毛病。

⚠️ **跨批对齐：两边各算两个标识，任一相等即配上**
------------------------------------------------
百度检索结果给的是 ``http://www.baidu.com/link?url=…``——**一次性跳转链**，
每次检索都不一样 ⇒ 按 ``url`` 对齐会把同一次重跑自己的新旧行也判成「不同」；
按 host 对齐更糟：跳不过去的行 ``landed_url`` 为空 ⇒ host 全是 ``baidu.com``
⇒ **所有跳转链行撞成一个桶**。

⚠️ **旧行与新行不在同一个键空间**——这是本探针第一版配不上的原因：

* **旧行**（上次硬故障）多半是**跳转链**（导航失败 ⇒ 拿不到真实地址）
  ⇒ 只能靠**检索结果标题**认站；
* **新行**（这次）多半**导航成功** ⇒ 地址就是真实站点 URL
  ⇒ 能算出 **host**。

⇒ 若两边各只取一个键（旧的取标题、新的取 host），**永远对不上**。
所以：**两边都算 title + host，配对时任一相等即算配上**，并按对上的是哪一个
分成证据档（**软证据不与强证据混算**）。

⚠️ **三个字段的语义**（读反了就会接错）：

============  ==========================================
``url``       这条 outcome **关于**的地址——导航成功就是
              解析后的真实地址，失败就是那条候选链接
``site_url``  **检索结果**那条链接（百度跳转链）
``candidates[].url`` → ``candidates[].text``  候选链接 → **标题**
``visits[].url`` → ``visits[].landed_url``   候选链接 → 落地地址
============  ==========================================

判据单点
--------
「什么算采集侧硬故障」由 :func:`plan_hardfail_rerun.hard_kind` 判定——**import 它**，
不另写一份（抄一份必然漂）。

用法（**必须在仓库根跑**）::

    uv run python trajectory_pipeline/research/recollect/compare_hardfail_rerun.py \\
        --plan trajectory_pipeline/output/pipeline/plan_rerun_hardfail.json \\
        --out trajectory_pipeline/output/pipeline/compare_hardfail_rerun.json

⚠️ **只读脚本**：读存档、写字报告，**不联网、不开浏览器**。
"""

from __future__ import annotations

import argparse
import json
import pathlib
import sys
from collections import Counter
from typing import Any
from urllib.parse import urlsplit

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parents[2]))

from plan_hardfail_rerun import hard_kind  # noqa: E402  —— 判据单点，不另写
from trajectory_pipeline.executor.branches import NON_SAMPLE_BRANCHES  # noqa: E402

PIPELINE = pathlib.Path("trajectory_pipeline/output/pipeline")

#: **搜索引擎跳转链**的 host——它们不是站点，**不能拿来当对齐键**
#: （跳不过去的行 ``landed_url`` 为空 ⇒ 全落 ``baidu.com`` ⇒ 撞成一个桶）。
REDIRECT_HOSTS = frozenset({"baidu.com", "m.baidu.com", "www.baidu.com"})


def _norm(url: str) -> str:
    """URL 归一（去尾斜杠）——拼接索引时两边要同形。"""
    return str(url or "").strip().rstrip("/")


def _load(path: pathlib.Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def _host(url: str) -> str:
    """取 host（去掉 ``www.``、端口、路径）——跨批对齐的标识之一。"""
    u = str(url or "").strip()
    if not u:
        return ""
    if "://" not in u:
        u = "http://" + u
    host = (urlsplit(u).hostname or "").lower()
    return host[4:] if host.startswith("www.") else host


def _outcome_keys(data: dict[str, Any]) -> list[dict[str, Any]]:
    """给每条 outcome 算出**两个跨批标识**：**标题** + **host**。

    ⚠️ **两个都要算**，因为新旧两批常常各只有得着一个：

    * **host**（强证据）——被导航的地址本身就是真实站点 URL
      （直链站：``v.qq.com`` / ``www.iqiyi.com`` / ``www.yn-mg.cn``…）；
    * **标题**（软证据）——跳转链行导航失败、拿不到真实地址时，
      只能靠检索结果标题（``candidates[].text``）认出是哪个站。
      ⚠️ **泛标题**（「立即播放」这种链接文字）可能指不同的站
      ⇒ 本档内被多个候选共用时标 ``title_shared``。

    ⇒ 配对时**任一相等即配上**（见 :func:`_pair`），配上了才知道对上是哪一个。
    """
    titles: dict[str, str] = {}
    title_n: Counter[str] = Counter()
    for c in data.get("candidates") or ():
        u = _norm(c.get("url"))
        t = str(c.get("text") or "")
        if u:
            titles[u] = t
            title_n[t] += 1
    landed: dict[str, str] = {}
    for v in data.get("visits") or ():
        u = _norm(v.get("url"))
        if u:
            landed[u] = str(v.get("landed_url") or "")

    rows: list[dict[str, Any]] = []
    for o in data.get("outcomes") or ():
        if not isinstance(o, dict):
            continue
        u = _norm(o.get("url"))
        site = _norm(o.get("site_url"))
        title = titles.get(site) or titles.get(u) or ""
        land = landed.get(site) or landed.get(u) or ""
        host = _host(land) if land else ""
        if not host or host in REDIRECT_HOSTS:
            host = _host(u)
        if host in REDIRECT_HOSTS:
            host = ""
        rows.append({
            "title": title,
            "title_shared": bool(title) and title_n[title] > 1,
            "host": host,
            "url": str(o.get("url") or ""),
            "branch": str(o.get("branch") or ""),
            "evidence": str(o.get("evidence") or ""),
        })
    return rows


def _pair(old_row: dict[str, Any], new_rows: list[dict[str, Any]]) -> list[tuple[str, dict[str, Any]]]:
    """配对：返回 ``[(证据档, 新行), …]``。

    ⚠️ **证据档从强到弱**：

    ==============  ==========================================
    ``host+title``  host 与标题**都对上**⇒ 同一个站，无异议
    ``host``        只 host 对上（标题变了 / 取不到）
    ``title``       只标题对上（host 一侧为空或不同）
    ``title_shared``  标题对上但**该标题被多个候选共用**
                    ⇒ 它不标识唯一站点 ⇒ 最软
    ``title_conflict``  标题对上但**两边 host 都非空且不同**
                    ⇒ **认错了站** ⇒ 不投票，交给人判
    ==============  ==========================================

    ⚠️ 旧行与新行可能**同时**被多个新行命中（同名站有多条）⇒ 返回全部，
    由调用方按「结论是否一致」决定是取一条还是判歧义。
    """
    hits: list[tuple[str, dict[str, Any]]] = []
    for r in new_rows:
        o_host = old_row["host"]
        n_host = r["host"]
        o_title = old_row["title"]
        n_title = r["title"]
        same_host = bool(o_host) and o_host == n_host
        same_title = bool(o_title) and o_title == n_title
        if same_host and same_title:
            hits.append(("host+title", r))
        elif same_host:
            hits.append(("host", r))
        elif same_title:
            if o_host and n_host:
                # ⚠️ 标题一样但落地站不同 ⇒ 多半是泛标题（「立即播放」）
                # ⇒ **不投票**。
                hits.append(("title_conflict", r))
            elif old_row["title_shared"] or r["title_shared"]:
                hits.append(("title_shared", r))
            else:
                hits.append(("title", r))
    return hits


def _new_archives(batch_id: str) -> dict[str, list[tuple[pathlib.Path, dict[str, Any]]]]:
    """按批次身份收新存档（原档 + 复核档都收；复核档优先）。"""
    found: dict[str, list[tuple[pathlib.Path, dict[str, Any]]]] = {}
    for path in sorted(PIPELINE.glob("*.json")):
        data = _load(path)
        if not data:
            continue
        if str(data.get("batch_id") or "") != batch_id:
            continue
        tid = str(data.get("task_id") or "")
        found.setdefault(tid, []).append((path, data))
    return found


def _pick(archives: list[tuple[pathlib.Path, dict[str, Any]]]) -> tuple[pathlib.Path, dict[str, Any]]:
    """**复核档优先**（``select_archives`` 的同一口径）。"""
    reviewed = [(p, d) for p, d in archives if p.name.endswith(".reviewed.json")]
    return reviewed[0] if reviewed else archives[0]


def main(argv: list[str] | None = None) -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, OSError):
        pass
    ap = argparse.ArgumentParser(description="硬故障重跑 · 新旧对照")
    ap.add_argument("--plan", default=str(PIPELINE / "plan_rerun_hardfail.json"))
    ap.add_argument("--out", default=str(PIPELINE / "compare_hardfail_rerun.json"))
    args = ap.parse_args(argv)

    plan = _load(pathlib.Path(args.plan))
    if not plan:
        print(f"[FAIL] 计划读不到：{args.plan}")
        return 2
    batch_id = str(plan.get("batch_id") or "")
    new_by_task = _new_archives(batch_id)

    report: dict[str, Any] = {
        "batch_id": batch_id,
        "align_key": "两边各算 title+host，任一相等即配上；证据档分档统计",
        "tasks": [],
    }
    totals: Counter[str] = Counter()
    by_conf: Counter[tuple[str, str]] = Counter()
    unreadable: list[str] = []

    for t in plan.get("report", {}).get("tasks", []):
        tid = str(t.get("task_id") or "")
        old_path = PIPELINE / str(t.get("archive") or "")
        old = _load(old_path)
        if not old:
            unreadable.append(tid)
            continue

        old_rows = _outcome_keys(old)
        old_hard = []
        for r in old_rows:
            kind = hard_kind(r["evidence"])
            if not kind:
                continue
            old_hard.append({**r, "kind": kind,
                             "evidence_head": r["evidence"][:120]})

        new_list = new_by_task.get(tid) or []
        if not new_list:
            report["tasks"].append({
                "task_id": tid, "status": "not_run_yet", "old_hard": old_hard})
            totals["not_run_yet"] += len(old_hard)
            continue

        new_path, new = _pick(new_list)
        new_rows = _outcome_keys(new)

        pairs: list[dict[str, Any]] = []
        for h in old_hard:
            hits = _pair(h, new_rows)
            # ⚠️ **同一行对上一堆结论 ⇒ 不替人投票**：结论不同就摆成「歧义」，
            # 硬挑第一条会把「同一标题的泛链接」（如「立即播放」）配错站。
            branches = {r["branch"] for _, r in hits}
            if hits and len(branches) > 1:
                state = "ambiguous"
                conf = hits[0][0]
                r = hits[0][1]
            elif hits:
                conf, r = hits[0]
                still = hard_kind(r["evidence"])
                if still:
                    state = "still_hard"
                elif not r["branch"]:
                    state = "success"          # 无记账分支 ⇒ **正样本**落地
                elif r["branch"] in NON_SAMPLE_BRANCHES:
                    # ⚠️ `unresolved` / `player_unverified` / … **不是真结论**——
                    # 它们是「没判出来」（不进池）⇒ 跟硬故障一样，**结论仍欠着**。
                    state = "no_conclusion"
                else:
                    state = "negative"
            else:
                state = "not_covered"
                conf = ""
                r = {"branch": "", "evidence": ""}
            detail = {
                "kind": h["kind"], "confidence": conf,
                "title": h["title"], "host": h["host"],
                "old_evidence": h["evidence_head"],
                "new_branch": r["branch"],
                "new_evidence": r["evidence"][:120],
                "state": state,
            }
            totals[state] += 1
            by_conf[(conf or "none", state)] += 1
            pairs.append(detail)

        report["tasks"].append({
            "task_id": tid,
            "status": "compared",
            "old_archive": old_path.name,
            "new_archive": new_path.name,
            "old_hard": old_hard,
            "pairs": pairs,
        })

    report["totals"] = dict(totals)
    report["by_confidence"] = {f"{c}|{s}": n for (c, s), n in sorted(by_conf.items())}
    report["totals_note"] = (
        "success = 这次拿到了成功（无记账分支 ⇒ 正样本落地）；"
        "negative = 这次拿到了真负分支（结论落地、进池）；"
        "no_conclusion = 这次仍**没判出来**（unresolved / player_unverified 等 "
        "NON_SAMPLE 分支 ⇒ 结论仍欠着）；"
        "still_hard = 这次仍是采集侧硬故障；"
        "ambiguous = 同一行对上一堆结论 ⇒ 不替人投票；"
        "not_covered = 这次没走到这个站（检索结果变了 / 被跳过）"
    )
    report["confidence_note"] = (
        "host+title = 强（host 与标题都对上）；"
        "host = 强（host 对上）；"
        "title = 软（只标题对上）；"
        "title_shared = 更软（该标题被多个候选共用）；"
        "title_conflict = 标题对上但两边 host 不同（多半认错站 ⇒ 不投票）；"
        "⚠️ **软证据那一档不与强证据混算**"
    )
    report["unreadable_old_tasks"] = unreadable

    print(f"批次 {batch_id}")
    print(f"  旧硬故障 {sum(totals.values())} 条 ⇒ "
          + " · ".join(f"{k} {v}" for k, v in sorted(totals.items())))
    print("  按对齐证据档（**软证据不与强证据混算**）：")
    for conf in ("host+title", "host", "title", "title_shared", "title_conflict", "none"):
        row = {s: n for (c, s), n in by_conf.items() if c == conf}
        if row:
            print(f"    {conf:14s} " + " · ".join(f"{s} {n}" for s, n in sorted(row.items())))
    for t in report["tasks"]:
        if t["status"] != "compared":
            print(f"    {t['task_id']:6s} {t['status']}")
            continue
        for p in t["pairs"]:
            print(f"    {t['task_id']:6s} {p['kind']:4s} [{p['confidence']:14s}] "
                  f"{(p['title'] or p['host'])[:30]:30s} ⇒ "
                  f"{p['state']:12s} {p['new_branch']}")

    out_path = pathlib.Path(args.out)
    out_path.write_text(json.dumps(report, ensure_ascii=False, indent=2),
                        encoding="utf-8")
    print(f"报告 → {out_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
