"""**回放**两条护栏（owner 2026-10-28 裁定 · 台账 Q-24 甲 / Q-25 甲）在**全库存档**上的作用面。

为什么要有这个脚本
------------------
两条护栏都改的是**分支判据**，改完必须先知道「它们会动多少行」——
不量就上生产，等于拿一批**没量过**的判据去改标签。

两条护栏
--------
- **Q-24 甲（归因对象护栏）**：`executor/steps/reachability.py::probe` ——
  被导航的 URL 是**搜索引擎的结果中转链** ⇒ 机器码证明的是「这条链打不开」，
  不是「那个影视站不可达」⇒ 不配记 ``unreachable_hard``，降 ``unresolved``。
- **Q-25 甲（终局形态护栏）**：`executor/branches.py::terminal_shape_branch` ——
  agent 自报放弃 / 预算耗尽时，先看**最后一页的正文**（页面自己的证据，
  不是 agent 的叙述）⇒ 命中就落那条分支，不命中仍落 ``unresolved``。

⚠️ **这是回放，不是重跑**：读的是**已经落盘的存档**里的观察，
用**同一把判据**再跑一遍 ⇒ 量的是「护栏会动哪些行」，
不是「护栏上线后读数会变成多少」（后者要等下一批真跑）。

⚠️ **不给「护栏上线后一定更好」的结论**：护栏只是把「机器本可以判出来」的
形态从 ``unresolved`` 里捞出来；**捞出来的对不对，仍要人核**。

用法（**必须在仓库根跑**）::

    uv run python trajectory_pipeline/research/probes/q24_q25_guard_replay.py

    # 只看某一批（护栏上线后真跑的那批）⇒ 与「上线前的旧批」逐条对照
    uv run python trajectory_pipeline/research/probes/q24_q25_guard_replay.py --batch guard-rerun-2026-10-28
    uv run python trajectory_pipeline/research/probes/q24_q25_guard_replay.py --batch A,B --tasks T011,T013
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

from trajectory_pipeline.executor.branches import terminal_shape_branch
from trajectory_pipeline.executor.steps.search import engine_redirect_engine
from trajectory_pipeline.perception.replay import observation_of

#: agent 放弃收口的**可识别签名**（``AgentOutcome.give_up`` / 预算闸写进 reason 的原文）。
GIVE_UP_MARKERS = ("步数预算耗尽", "llm_calls 预算耗尽")


def _load(path: pathlib.Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}


def _last_observation(visit: dict) -> dict:
    """一个 visit 里**最后一个**观察（agent 放弃时它正看着的那一页）。"""
    obs: dict = {}
    for step in visit.get("steps") or ():
        if isinstance(step, dict) and isinstance(step.get("observation"), dict):
            obs = step["observation"]
    return obs


def main() -> int:
    ap = argparse.ArgumentParser(description="Q-24 甲 / Q-25 甲 护栏回放读数")
    ap.add_argument("--batch", default="",
                    help="只看这些 batch_id（逗号分隔）；留空 = 全库")
    ap.add_argument("--tasks", default="",
                    help="只看这些 task_id（逗号分隔）；留空 = 全部")
    args = ap.parse_args()
    want_batches = {b.strip() for b in args.batch.split(",") if b.strip()}
    want_tasks = {t.strip() for t in args.tasks.split(",") if t.strip()}

    archives = sorted(PIPELINE.glob("*.json"))
    print(f"扫描存档 {len(archives)} 份（{PIPELINE}）")

    uh_total = 0
    uh_redirect: list[tuple[str, str, str]] = []
    give_up_total = 0
    give_up_hits: list[tuple[str, str, str, str]] = []
    give_up_miss: list[tuple[str, str, str]] = []
    give_up_no_obs = 0
    give_up_other_host = 0
    give_up_kinds: collections.Counter[str] = collections.Counter()
    scanned = 0

    for path in archives:
        data = _load(path)
        if not data:
            continue
        if want_batches and str(data.get("batch_id") or "") not in want_batches:
            continue
        if want_tasks and str(data.get("task_id") or "") not in want_tasks:
            continue
        if not (data.get("outcomes") or data.get("visits")):
            # **不是 P1 存档**（执行计划等杂项）——不算进「扫描存档」份数，
            # 否则按 batch 过滤时计划文件会被算成存档（份数对不上，读数会被疑）。
            continue
        scanned += 1
        visits = data.get("visits") or []

        for out in data.get("outcomes") or ():
            if not isinstance(out, dict):
                continue
            if str(out.get("branch") or "") == "unreachable_hard":
                uh_total += 1
                engine = engine_redirect_engine(str(out.get("url") or ""))
                if engine:
                    uh_redirect.append(
                        (path.name, engine, str(out.get("url") or "")[:120])
                    )

        # ── Q-25 甲：agent 放弃收口前的终局形态 ──────────────────────
        # **认「放弃」的口径** = 存档里 agent visit 的 note 写着
        # 「agent 未到达：gave_up」/「agent 未找到：give_up」
        # （:func:`trajectory_pipeline.executor.steps.agent_visit`）——
        # 不用证据文本猜：证据文本有两种形态（agent 自述 / 「步数预算耗尽」），
        # 靠文本认会把「站点遍历超时」「导航失败」那类**别的路径**混进来。
        for visit in visits:
            if not isinstance(visit, dict):
                continue
            notes = " ".join(str(n) for n in (visit.get("notes") or ()))
            if "agent 未到达：gave_up" not in notes and "agent 未找到：give_up" not in notes:
                continue
            give_up_total += 1
            # 收口形态：agent 自述放弃 / 预算耗尽 —— 依据是**那条 outcome 的
            # evidence**（= ``AgentOutcome.reason``），不是 visit 的 note
            reason = ""
            for out in data.get("outcomes") or ():
                if not isinstance(out, dict):
                    continue
                if str(out.get("url") or "") in (
                    str(visit.get("landed_url") or ""),
                    str(visit.get("url") or ""),
                ):
                    reason = str(out.get("evidence") or "")
                    break
            if "预算耗尽" in reason:
                give_up_kinds["预算耗尽"] += 1
            else:
                give_up_kinds["agent 自述放弃"] += 1
            obs_json = _last_observation(visit)
            if not obs_json:
                give_up_no_obs += 1
                continue
            obs = observation_of(obs_json)
            # **归因对象 = 解析后的候选站**（落地页），不是候选 URL 本身
            # （候选 URL 常是搜索引擎中转链 ⇒ 拿它比会把真阳性全判成不明）
            site_url = str(visit.get("landed_url") or obs_json.get("url") or "")
            new_branch, why = terminal_shape_branch(obs, site_url=site_url)
            if new_branch:
                give_up_hits.append(
                    (path.name, new_branch, why, str(visit.get("landed_url") or "")[:100])
                )
            elif "归因对象不明" in why:
                give_up_other_host += 1
            else:
                give_up_miss.append(
                    (path.name, why, str(visit.get("landed_url") or "")[:100])
                )

    print("\n=== Q-24 甲（归因对象护栏） ===")
    print(f"  实际扫描存档 {scanned} 份")
    print(f"  全库 unreachable_hard outcome：{uh_total} 条")
    print(f"  其中 URL 是搜索引擎结果中转链：{len(uh_redirect)} 条"
          f" ⇒ 这 {len(uh_redirect)} 条会被降为 unresolved（不再进池）")
    for name, engine, url in uh_redirect:
        print(f"    {name}  引擎={engine}  {url}")

    print("\n=== Q-25 甲（终局形态护栏） ===")
    print(f"  全库「agent 放弃收口」的 visit：{give_up_total} 条"
          f"（{dict(give_up_kinds)}）")
    print(f"  存档里找不到最后一页观察：{give_up_no_obs} 条（判不了，不改判）")
    print(f"  护栏命中（会落分支）：{len(give_up_hits)} 条"
          f" · 去重后 {len({u for _, _, _, u in give_up_hits})} 个落地页")
    by_branch = collections.Counter(br for _, br, _, _ in give_up_hits)
    print(f"    分支分布：{dict(by_branch)}")
    for name, br, why, url in give_up_hits:
        print(f"    {name}  → {br}  {why}  [{url}]")
    print(f"  归因对象不明（最后一页不在候选站内）：{give_up_other_host} 条")
    print(f"  护栏不命中（仍落 unresolved）：{len(give_up_miss)} 条")
    reasons = collections.Counter(w.split("（")[0][:60] for _, w, _ in give_up_miss)
    for k, v in reasons.most_common(10):
        print(f"    {v} 条 · {k}")
    # 前置事实不成立的那些行要单独列出来——它们**被降级而不是改判**，
    # 理由文本是审计记录，不能吞掉。
    precond = [it for it in give_up_miss if "前置事实不成立" in it[1]]
    if precond:
        print(f"  其中「正文命中但前置事实不成立」⇒ 降级 unresolved：{len(precond)} 条")
        for name, why, url in precond:
            print(f"    {name}  {why}  [{url}]")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
