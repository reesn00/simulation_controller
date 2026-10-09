"""新树 CLI 入口——``python -m trajectory_pipeline.executor.cli run``。

子命令：

    run              跑一个 task（真实浏览器），落 P1
    gen              模块 1 采样：骨架 × persona → 任务实例计划（不联网）
    review           从 P1 存档导出**人工复核队列**（W1 正样本的唯一来源）
    check            只查能力（不跑任务）：obscura 可达性 / Perceptor 能力边界
    check-persona    画像库覆盖度体检 + 判分保真探针体检（不联网）
    report           汇总 P1 存档的分支分布

为什么要有 ``check``：``run`` 失败时无法区分「网络不通」「浏览器起不来」
「感知层没有能力」——三者的处置完全不同。``check`` 在跑之前就把这三件事分开了，
避免把环境问题误读成「这批素材质量差」。

``gen`` 与 ``run`` 是**分开的两步**而不是一个命令的两步：生成不联网、跑要联网，
合成一条命令的后果是"跑不出数据"时分不清是 taskgen 挂了还是 obscura 起不来——
而这正是 W1 反复踩过的区分难题。``run --plan <file>`` 是把两步接起来的方式，
但仍然是两条命令——**换数据来源不该让执行端变形**。

``run`` 的两条路径
----------------
- ``run --task-id T001 --title 功夫``：手工路径。存档的 ``provenance``
  为空，含义是「不是 taskgen 跑的」。
- ``run --plan gen_out.json``：计划路径。消费 :mod:`plan` 里那份
  **按文件格式读**的计划（不 import taskgen），检索式与 provenance 都来自计划，
  原样进存档供 persona 切片。控制流一行没变。

建议长批先 ``--dry-run``：真实浏览器每条约几十秒，跑之前值得先看一眼
"到底会跑哪些条目、跳过了哪些、为什么跳"。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from collections import Counter
from pathlib import Path
from typing import Any

from trajectory_pipeline.executor.archive import DEFAULT_ROOT, P1Archive
from trajectory_pipeline.executor.browser.mcp_client import EXE_ENV_VAR, McpClient
from trajectory_pipeline.executor.browser.obscura_driver import (
    FORBIDDEN_TOOLS,
    ObscuraDriver,
)
from trajectory_pipeline.executor.dom import DEFAULT_MAX_CHARS, detect_block
from trajectory_pipeline.executor.orchestrator import Orchestrator, RunConfig
from trajectory_pipeline.executor.plan import PlanError, load_plan
from trajectory_pipeline.executor.review_queue import (
    VERDICTS,
    apply_verdicts,
    collect as collect_reviews,
    write_queue as write_review_queue,
)
from trajectory_pipeline.executor.steps import search as search_step
from trajectory_pipeline.perception import questions
from trajectory_pipeline.perception.base import Q
from trajectory_pipeline.perception.rule_perceptor import RulePerceptor


def _reconfigure_stdio() -> None:
    """Windows 控制台默认 GBK——探针上的 ✅/⚠️ 曾把整条链带崩。
    只放宽 ``errors`` 不改 ``encoding``：PowerShell 下当前编码本就正常，
    不该被改掉。
    """
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(errors="replace")


async def cmd_run(args: argparse.Namespace) -> int:
    if args.plan:
        return await _run_plan(args)
    return await _run_manual(args)


async def _run_manual(args: argparse.Namespace) -> int:
    """手工路径：``--task-id`` + ``--title``。**不带 provenance**。

    这条路径的存档 ``provenance`` 为空——含义是「不是 taskgen 跑的」。
    要切片出分就得走 ``--plan``。
    """
    try:
        client = McpClient.from_env()
    except RuntimeError as exc:
        print(f"[FAIL] {exc}")
        return 2

    titles = [t.strip() for t in args.title] if args.title else [args.task_id]
    cfg = RunConfig(
        engine=args.engine,
        max_candidates=args.max_candidates,
        max_chars=args.max_chars,
        stop_after_success=args.stop_after_success,
    )
    archive = P1Archive(Path(args.out) if args.out else None)
    perceptor = RulePerceptor()
    records = []

    async with client:
        driver = ObscuraDriver(client)
        print(f"server = {client.info.name} {client.info.version} "
              f"({client.info.tool_count} tools)")
        orch = Orchestrator(driver, perceptor, cfg)
        for title in titles:
            print(f"\n[{args.task_id}] {title}")
            rec = await orch.run(args.task_id, title)
            records.append(rec)
            path = archive.write(rec, task_id=args.task_id)
            _print_summary(rec, path)

    _print_batch_footer(records)
    return 0


async def _run_plan(args: argparse.Namespace) -> int:
    """计划路径：消费 ``gen --out`` 的产物。

    与手工路径的差别**只有两处**：检索式来自计划、provenance 随存档落盘。
    控制流一行没变——这是「可替换插件」纪律的验收点：
    换掉数据来源不应该让执行端变形。
    """
    if args.title:
        print("[FAIL] --plan 与 --title 互斥：计划里的检索式与片名是配套的，"
              "用 --title 覆盖等于丢掉 persona 渲染的检索语义")
        return 2
    try:
        plan = load_plan(args.plan, include_unrewritten=args.include_unrewritten)
    except PlanError as exc:
        print(f"[FAIL] {exc}")
        return 2

    tasks = list(plan.tasks)
    if args.limit:
        tasks = tasks[: args.limit]

    _print_plan_header(plan, tasks, limited=bool(args.limit))
    if args.dry_run:
        print("\n--dry-run：不连浏览器、不落盘。")
        return 0
    if not tasks:
        print("\n没有可跑条目。先看上面的跳过明细，别把「全被跳过」"
              "读成「这批跑过了」。")
        return 1

    try:
        client = McpClient.from_env()
    except RuntimeError as exc:
        print(f"[FAIL] {exc}")
        return 2

    cfg = RunConfig(
        engine=args.engine,
        max_candidates=args.max_candidates,
        max_chars=args.max_chars,
        stop_after_success=args.stop_after_success,
    )
    archive = P1Archive(Path(args.out) if args.out else None)
    perceptor = RulePerceptor()
    records = []

    async with client:
        driver = ObscuraDriver(client)
        print(f"server = {client.info.name} {client.info.version} "
              f"({client.info.tool_count} tools)")
        orch = Orchestrator(driver, perceptor, cfg)
        for n, task in enumerate(tasks, 1):
            print(f"\n[{n}/{len(tasks)}] {task.task_id} | {task.persona_id} "
                  f"| {task.title or task.search_query}")
            rec = await orch.run(
                task.task_id, task.title or task.task_id,
                search_query=task.search_query,
                provenance=dict(task.provenance),
            )
            records.append(rec)
            path = archive.write(rec, task_id=task.task_id)
            _print_summary(rec, path)

    _print_batch_footer(records)
    return 0


def _print_plan_header(plan, tasks, *, limited: bool) -> None:
    """跳过明细**必须打印**——少跑的那些不留痕就等于没发生过。"""
    print(f"计划 {plan.source}")
    print(f"  文件内 {plan.total_in_file} 条 → 可跑 {len(tasks)} 条")
    if plan.skips:
        print(f"  跳过 {len(plan.skips)} 条：{plan.skip_summary()}")
        for s in plan.skips[:8]:
            print(f"      [{s.reason}] {s.task_id}（#{s.index}）{s.detail}")
        if len(plan.skips) > 8:
            print(f"      …另 {len(plan.skips) - 8} 条")
    if limited:
        print(f"  [warn] --limit 生效：只跑前 {len(tasks)} 条")
    rep = plan.report
    if rep:
        print(f"  采样记录：seed={rep.get('seed', '未记录')!r} "
              f"library={rep.get('library_digest', '?')[:12]} "
              f"模式={rep.get('by_mode')}")
    print(f"\n  {'#':>3} {'task_id':<8} {'persona':<28} {'检索式'}")
    for t in tasks[:10]:
        print(f"  {t.index:>3} {t.task_id:<8} {t.persona_id:<28} {t.search_query}")
    if len(tasks) > 10:
        print(f"  …另 {len(tasks) - 10} 条")
    print("  提示：真实浏览器每条约几十秒。跑长批之前建议先 --dry-run 看一眼。")


def _print_batch_footer(records) -> None:
    total = len(records)
    ok = sum(1 for r in records if r.succeeded)
    blocked = [r for r in records if r.search_blocked]
    print(f"\n共 {total} 个 task，成功 {ok}")
    if blocked:
        print(f"⚠️ 其中 **{len(blocked)}/{total} 个被反爬拦截**"
              f"（{Counter(r.search_blocked for r in blocked)}）——"
              f"这些一次搜索都没搜成，**不算素材质量差**。"
              f"加间隔或 `--engine bing` 重跑它们")
    if ok == 0 and not blocked:
        # 全 0 不一定是数据差，多半是 W1 的能力边界——直接说出来，
        # 免得读档的人把它当成「这批素材没价值」。不猜是哪一条原因：
        # 分支分布已经印在上面了，让读的人对着 numbers 自己对。
        print("提示：0 成功不等于没素材。W1 规则版有三处能力边界会压低成功数——"
              "IS_REACHABLE 恒为 None（不中断但给不出结论）、"
              "PLAYER_OK 只认 <video>/<audio> 标签（iframe 播放器判不出来）、"
              "存在性判定一律带 fallback_used。看上面的分支分布区分"
              "「素材质量差」和「这版规则判不了」。")
        print("另外：本批若来自 --plan，正样本还需走人工复核"
              "（review --write … → 填 verdict → review --apply）。")
    else:
        # 被拦截的**必须排除在切片之外**。它们一次搜索都没搜成，
        # 0 成功率会被读成「这个 persona 组合表现差」，而真相是
        # 「这三条压根没跑」——切片表的说服力会被这种行毁掉。
        runnable = [r for r in records if not r.search_blocked]
        if blocked:
            print(f"以下切片只含**实际跑过**的 {len(runnable)} 个 task"
                  f"（{len(blocked)} 个被拦截的已排除）")
        by_persona: dict[str, list[int]] = {}
        for r in runnable:
            pid = str(r.provenance.get("persona_id") or "(无 provenance)")
            by_persona.setdefault(pid, []).append(1 if r.succeeded else 0)
        if len(by_persona) > 1:
            print("按 persona 切片（成功/总数）——注意小样本别下结论：")
            for pid, vals in sorted(by_persona.items()):
                print(f"  {pid:<32} {sum(vals)}/{len(vals)}")


def _print_summary(rec, path: Path) -> None:
    if rec.search_blocked:
        print(f"  query      = {rec.query}")
        print(f"  **搜索页被反爬拦截（{rec.search_blocked}）——本次没搜成**")
        print(f"     引擎页 {rec.search_url[:70]}")
        for w in rec.warnings:
            print(f"  [warn] {w}")
        print(f"  → {path}")
        return
    s = rec.ledger.summary() if rec.ledger else {}
    print(f"  query      = {rec.query}")
    print(f"  candidates = {len(rec.candidates)} (来源: {rec.candidate_source}"
          f"{'，已过滤 ' + str(sum(rec.candidate_filter.values())) if rec.candidate_filter else ''})")
    for c in rec.candidates[:5]:
        print(f"      #{c.rank} {c.host}  {c.text[:30]}")
    print(f"  sites      = {s.get('total_sites', 0)}  成功 = {s.get('succeeded', 0)}")
    print(f"  branches   = {json.dumps(s.get('by_branch', {}), ensure_ascii=False)}")
    missing = s.get("missing_branches") or []
    if missing:
        print(f"  未覆盖分支 = {missing}")
    for w in rec.warnings:
        print(f"  [warn] {w}")
    print(f"  → {path}")


async def cmd_check(args: argparse.Namespace) -> int:
    print("== Perceptor 能力边界 ==")
    p = RulePerceptor()
    ready, reason = p.health()
    print(f"  rule: ready={ready} — {reason}")
    print("  W1 可判的题:")
    print("    FIND_PLAY_CONTROL — 播放控件词表 + 预告片判定")
    print("    PLAYER_OK        — <video>/<audio> 标签**存在性**（非「能播」）")
    print("                        iframe 数量不作判据：导航站满屏 iframe，"
          "实测会把 hao123 判成播放页")
    print("  W1 必然 None 的题（fail-closed）:")
    for qid in (Q.SELECT_PLAY_SITES, Q.IS_REACHABLE):
        print(f"    {qid}")
    print(f"\n  失败分支全集（{len(questions.all_branches())} 条）: "
          f"{sorted(questions.all_branches())}")
    print(f"  不计入负样本的分支: {sorted({'unresolved', 'trailer_suspect'})}")

    print("\n== 隐私红线 ==")
    print(f"  {EXE_ENV_VAR} 未设置" if not _env_ok() else f"  {EXE_ENV_VAR} 已设置")
    print(f"  禁用的 tool（{len(FORBIDDEN_TOOLS)}）: {sorted(FORBIDDEN_TOOLS)}")

    print("\n== obscura 连接 ==")
    try:
        client = McpClient.from_env()
    except RuntimeError as exc:
        print(f"  [SKIP] {exc}")
        return 0
    async with client:
        info = client.info
        print(f"  {info.name} {info.version} / 协议 {info.protocol_version} / "
              f"{info.tool_count} tools")
        drift = FORBIDDEN_TOOLS & {t["name"] for t in await client.list_tools()}
        print(f"  红线 tool 仍在服务端清单里（已在驱动层拦截）: {sorted(drift)}")
        driver = ObscuraDriver(client)
        await driver.goto(args.probe_url)
        obs = await driver.observe(max_chars=DEFAULT_MAX_CHARS)
        print(f"  探针页 {obs.url}: body={len(obs.body_text)}ch "
              f"source={obs.body_source} degraded={list(obs.degraded)}")
    return 0


def _env_ok() -> bool:
    import os

    return bool(os.environ.get(EXE_ENV_VAR, "").strip())


# ══════════════════════════════════════════════════════════════════════
# 模块 1：采样（不联网）
# ══════════════════════════════════════════════════════════════════════


def cmd_gen(args: argparse.Namespace) -> int:
    """采样任务实例并落一份「执行计划」。

    产出单独落盘的必要性：计划里带全量 provenance（persona 六维、
    检索模式、兼容性、归一结论），这些字段要在**跑完之后**用于切片出分。
    不落盘就等于让人在跑完后重新回忆"当时喂的是哪个人设"。
    """
    from trajectory_pipeline.taskgen.persona.library import (
        DEFAULT_LIBRARY_PATH,
        load_library,
    )
    from trajectory_pipeline.taskgen.sampler import sample_tasks
    from trajectory_pipeline.taskgen.skeleton import load_skeletons

    library = load_library(args.library)
    skeletons = load_skeletons(args.tasks)
    batch = sample_tasks(
        args.n, library=library, skeletons=skeletons,
        seed=args.seed, strata=tuple(args.strata), w1_only=not args.all_modes,
    )

    print(f"画像库 {library.source}（{len(library)} 条, digest={library.digest()}）")
    print(f"骨架   {len(skeletons)} 条")
    r = batch.report
    print(f"\n产出 {r.produced}/{r.requested} 条"
          f"（归一过 {r.normalize_accepted} / 退回 {r.normalize_rejected}）")
    print(f"  骨架使用 {len(r.skeleton_usage)} 个 | 人设使用 {len(r.persona_usage)} 个")
    print(f"  模式分布 {r.by_mode}")
    if r.excluded_by_skeleton_mode:
        print(f"  骨架库 W1 可跑 {r.skeletons_w1_runnable}/{r.skeletons_seen}"
              f"（排除 {sum(r.excluded_by_skeleton_mode.values())} 个："
              f"{r.excluded_by_skeleton_mode}）")
    for w in r.warnings():
        print(f"  [warn] {w}")

    if args.show:
        for inst in batch.instances[:args.show]:
            print(f"\n  [{inst.task_id} | {inst.persona_id} | "
                  f"spec={inst.provenance.get('actual_specificity')} | "
                  f"tier={inst.provenance.get('content_tier')}]")
            print(f"    表述：{inst.prompt_text}")
            print(f"    检索：{inst.search_query}")

    if args.out:
        out = Path(args.out)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(
            json.dumps(batch.to_json(), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        print(f"\n→ {out}")
    return 0


def cmd_check_persona(args: argparse.Namespace) -> int:
    """画像库覆盖度 + 判分保真探针体检。

    两个体检**分开报**，因为它们回答不同的问题：
    前者答"切片轴撑不撑得起结论"，后者答"渲染有没有丢判分要求"。
    合成一个数字的话，「探针 100% 通过」会被读成「画像库健康」——
    而前者可能正是因为太宽而恒过。
    """
    from trajectory_pipeline.taskgen.persona.library import load_library
    from trajectory_pipeline.taskgen.sampler import probe_skeletons
    from trajectory_pipeline.taskgen.skeleton import load_skeletons, mode_distribution

    library = load_library(args.library)
    skeletons = load_skeletons(args.tasks)

    print("== 骨架 ==")
    print(f"  {len(skeletons)} 条，模式分布 {mode_distribution(skeletons)}")
    print(f"  W1 可跑 {sum(1 for s in skeletons if s.runnable_in_w1)} 条")

    print("\n== 画像库覆盖度 ==")
    print(f"  {len(library)} 条，digest={library.digest()}")
    for dim, counts in library.coverage().items():
        present = {k: v for k, v in counts.items() if v}
        low = [k for k, v in present.items() if v <= 2]
        flag = f"  ⚠️ 仅 {low} 各 ≤2 条（切片表会出单格行）" if low else ""
        print(f"  {dim:20s} {present}{flag}")
    missing = library.missing_dims()
    degenerate = library.degenerate_dims()
    if missing:
        print(f"\n  [FAIL] 从未覆盖的取值：{missing}")
    if degenerate:
        print(f"  [WARN] 只落在 ≤2 档的维度：{degenerate}")
    if not missing and not degenerate:
        print("\n  覆盖度健康：无缺档、无退化维度")

    print("\n== 判分保真探针 ==")
    rep = probe_skeletons(library, skeletons, limit=args.probe_limit)
    print(f"  接受率 {rep['accept_rate']}（{rep['accepted']}/{rep['total']}）")
    if rep["by_constraint"]:
        print(f"  丢失分布：{rep['by_constraint']}")
        print("  ⚠️ 先判断是渲染器有 bug 还是探针过严，**不要直接调宽探针**")
    else:
        print("  无丢失")
    for row in rep["samples"][:3]:
        print(f"    {row['task_id']} {row['fail_kind']}: {row['reason']}")
        print(f"      渲染→{row['text'][:70]}")
        print(f"      原文→{row['origin'][:70]}")
    return 1 if missing else 0


# ══════════════════════════════════════════════════════════════════════
# 人工复核（W1 正样本来源）
# ══════════════════════════════════════════════════════════════════════


def cmd_review(args: argparse.Namespace) -> int:
    root = Path(args.out) if args.out else DEFAULT_ROOT
    items = collect_reviews(root)

    print(f"== 复核队列（源：{root}）==")
    print(f"  待复核 {len(items)} 条")
    if not items:
        print("  没有待复核项：说明这批里没有 unresolved / trailer_suspect，")
        print("  要么全判出来了（好），要么压根没跑到判断点 ④（看 report 的分支分布）")
        return 0

    by_branch = Counter(i.branch for i in items)
    for b, n in by_branch.most_common():
        print(f"    {b:20s} {n}")
    reached = sum(1 for i in items if i.reached_play_page)
    print(f"  走到了播放页 {reached} 条 —— 这些最可能是被规则漏掉的正样本")

    if args.write:
        n = write_review_queue(args.write, items)
        print(f"\n→ 已写 {n} 条到 {args.write}")
        print(f"  复核员在每行填 verdict（可选：{sorted(VERDICTS)}）")
        print("  与 reviewed_by，然后：")
        print(f"    python -m trajectory_pipeline.executor.cli review "
              f"--out {root} --verdicts {args.write} --apply")

    if args.show:
        for it in items[:args.show]:
            print(f"\n  [{it.branch}] {it.url}")
            print(f"    落地  : {it.landed_url}")
            print(f"    标题  : {it.page_title}")
            print(f"    video={it.video_tag_count} iframe={it.iframe_count} "
                  f"degraded={list(it.degraded)}")
            print(f"    证据  : {it.evidence}")
            if it.elements:
                print(f"    元素  : {[e['label'] for e in it.elements[:6]]}")
    return 0


def cmd_apply_review(args: argparse.Namespace) -> int:
    """把人工裁定回填进 P1 存档。

    **写新文件，不原地改**（``<name>.reviewed.json``）。原因与
    :mod:`trajectory_pipeline.executor.archive` 的原子写同源：
    回填是基于一份判断，可能整批作废；覆盖原档就没有回头路了。
    """
    root = Path(args.out) if args.out else DEFAULT_ROOT
    verdicts: dict[str, dict[str, Any]] = {}
    with Path(args.verdicts).open("r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            row = json.loads(line)
            v = str(row.get("verdict") or "").strip()
            if not v:
                continue
            key = str(row.get("landed_url") or row.get("url") or "")
            verdicts[key] = {"verdict": v,
                             "reviewed_by": str(row.get("reviewed_by") or ""),
                             "note": str(row.get("note") or "")}

    total = dict.fromkeys(["confirm_success", "confirm_negative", "trailer",
                           "skipped", "unreviewed", "ignored"], 0)
    written = 0
    for path in sorted(root.glob("*.json")):
        if path.name.endswith(".reviewed.json"):
            continue
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except ValueError:
            continue
        if not isinstance(data, dict) or "outcomes" not in data:
            continue
        merged, stats = apply_verdicts(data, verdicts)
        for k, v in stats.items():
            total[k] = total.get(k, 0) + v
        dst = path.with_suffix(".reviewed.json")
        dst.write_text(json.dumps(merged, ensure_ascii=False, indent=2),
                       encoding="utf-8")
        written += 1

    print(f"回填 {written} 个存档，合计 {dict(total)}")
    if total["confirm_success"]:
        print(f"  → 人工确认的正样本 {total['confirm_success']} 条"
              f"（source=human，branch_before_review 保留了代码原判）")
    if total["ignored"]:
        print(f"  [warn] {total['ignored']} 条裁定落在**非复核分支**的记录上，"
              f"已原样保留未改。真负样本/成功样本不在复核范围内，"
              f"误改会静默毁掉负样本池的纯度——检查 verdicts 文件是不是对错了存档")
    return 0


def cmd_report(args: argparse.Namespace) -> int:
    root = Path(args.out) if args.out else DEFAULT_ROOT
    files = sorted(root.glob("*.json"))
    if not files:
        print(f"{root} 下没有 P1 存档")
        return 0
    counts: Counter = Counter()
    missing: Counter = Counter()
    blocked: Counter = Counter()
    filters: Counter = Counter()
    legacy_recomputed: list[str] = []
    total_sites = 0
    usable = 0
    for f in files:
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
        except ValueError:
            print(f"[skip] {f.name} 不是合法 JSON")
            continue
        total_sites += data.get("ledger", {}).get("total_sites", 0)
        counts.update(data.get("ledger", {}).get("by_branch", {}))
        for b in data.get("ledger", {}).get("missing_branches", []):
            missing[b] += 1
        reason = str(data.get("search_blocked") or "")
        recomputed = False
        if "search_blocked" not in data:
            # 存量存档（该字段引入之前跑的）：从存档里的观察**确定性重算**。
            #
            # 不重算的话，这些存档会被算成「可跑」——而其中被验证码页拦下的
            # 恰恰是最不该被算作可跑的。与 provenance 那条同款：**缺字段 ≠ 空值**。
            # 判定用的是同一个 :func:`detect_block`，不是第二套逻辑。
            o = data.get("search_observation") or {}
            reason = detect_block(
                str(o.get("url") or ""), str(o.get("page_title") or ""),
                str(o.get("body_preview") or ""),
            )
            recomputed = bool(reason)
        if reason:
            blocked[reason] += 1
            if recomputed:
                legacy_recomputed.append(f.name)
        else:
            usable += 1
        filters.update(data.get("candidate_filter") or {})

    print(f"存档 {len(files)} 个，站点合计 {total_sites}")

    # 被反爬拦掉的**必须排在分支分布之前**。理由：它是运行级的，
    # 一次拦截会让后面所有分支分布都偏小，先看分支会被带着往下读。
    if blocked:
        print(f"\n⚠️ 被反爬拦截 {sum(blocked.values())}/{len(files)} 个存档"
              f"（可跑 {usable} 个）：{dict(blocked)}")
        print("   这些 task **一次搜索都没搜成**，它们的分支分布不参与下面的统计。")
        print("   处置：加请求间隔，或换引擎（--engine bing）。")
        print("   **不要**把它们当成「素材质量差」——那是两回事。")
        if legacy_recomputed:
            print(f"   （其中 {len(legacy_recomputed)} 个是**存量存档重算**得出——"
                  f"该字段引入之前跑的批次：{legacy_recomputed[:4]}"
                  f"{'…' if len(legacy_recomputed) > 4 else ''}）")
    elif usable:
        print(f"  无反爬拦截（{usable}/{len(files)} 个可跑）")

    if filters:
        top = ", ".join(f"{k}={v}" for k, v in filters.most_common(8))
        print(f"\n候选过滤合计：{top}")

    print("\n分支分布:")
    for b, n in counts.most_common():
        print(f"  {b:28s} {n}")
    absent = sorted(questions.all_branches() - set(counts))
    if absent:
        print(f"\n**全程未出现的分支（{len(absent)}）**: {absent}")
        print("   这些不是「样本少」，是「这条路径没接上」——先查控制流。")
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="trajectory_pipeline.executor.cli")
    sub = p.add_subparsers(dest="cmd", required=True)

    run = sub.add_parser("run", help="跑一个 task 并落 P1")
    run.add_argument("--task-id", default="T001")
    run.add_argument("--title", action="append", help="作品名，可重复；默认与 task-id 同名")
    run.add_argument("--engine", default=search_step.DEFAULT_ENGINE)
    run.add_argument("--max-candidates", type=int, default=20)
    run.add_argument("--max-chars", type=int, default=DEFAULT_MAX_CHARS)
    run.add_argument("--stop-after-success", type=int, default=0)
    run.add_argument("--out", default=None, help="P1 输出目录")
    # ── 计划路径（模块 1 → 执行端）──────────────────────────────
    run.add_argument("--plan", default=None,
                     help="消费 gen --out 的执行计划 JSON（与 --title 互斥）")
    run.add_argument("--limit", type=int, default=0, help="只跑前 N 条；0 = 全部")
    run.add_argument("--include-unrewritten", action="store_true",
                     help="连归一退回骨架原文的样本一起跑"
                          "（默认跳过：persona 未生效，对 persona 切片无价值）")
    run.add_argument("--dry-run", action="store_true",
                     help="只读计划并打印，不连浏览器、不落盘")
    run.set_defaults(func=cmd_run)

    gen = sub.add_parser("gen", help="模块 1 采样：骨架 × persona → 执行计划（不联网）")
    gen.add_argument("-n", type=int, default=20, help="采样条数")
    gen.add_argument("--seed", type=int, default=0)
    gen.add_argument("--library", default=str(
        Path(__file__).resolve().parents[1] / "taskgen" / "persona" / "library.yaml"))
    gen.add_argument("--tasks", default=str(
        Path(__file__).resolve().parents[2] / "simulate_serve" / "config" / "tasks.yaml"))
    gen.add_argument("--strata", action="append",
                     default=["genre", "verbal_style", "urgency"],
                     help="分层维度，可重复")
    gen.add_argument("--all-modes", action="store_true",
                     help="连 W1 跑不了的骨架一起采（需自行确认判分标准匹配）")
    gen.add_argument("--show", type=int, default=5, help="打印前 N 条样例")
    gen.add_argument("--out", default=None, help="计划落盘路径（JSON）")
    gen.set_defaults(func=cmd_gen)

    chk_p = sub.add_parser("check-persona", help="画像覆盖度 + 判分保真探针体检（不联网）")
    chk_p.add_argument("--library", default=str(
        Path(__file__).resolve().parents[1] / "taskgen" / "persona" / "library.yaml"))
    chk_p.add_argument("--tasks", default=str(
        Path(__file__).resolve().parents[2] / "simulate_serve" / "config" / "tasks.yaml"))
    chk_p.add_argument("--probe-limit", type=int, default=40,
                       help="探针抽多少个骨架")
    chk_p.set_defaults(func=cmd_check_persona)

    rev = sub.add_parser("review", help="导出人工复核队列（W1 正样本来源）")
    rev.add_argument("--out", default=None, help="P1 存档目录")
    rev.add_argument("--write", default=None, help="复核队列 jsonl 落盘路径")
    rev.add_argument("--verdicts", default=None, help="人工裁定 jsonl（--apply 时读）")
    rev.add_argument("--apply", action="store_true", help="把裁定回填进 P1")
    rev.add_argument("--show", type=int, default=0, help="打印前 N 条详情")
    rev.set_defaults(func=cmd_review)

    chk = sub.add_parser("check", help="查能力，不跑任务")
    chk.add_argument("--probe-url", default="https://example.com/")
    chk.set_defaults(func=cmd_check)

    rep = sub.add_parser("report", help="汇总 P1 分支分布")
    rep.add_argument("--out", default=None)
    rep.set_defaults(func=cmd_report)
    return p


def main(argv: list[str] | None = None) -> int:
    _reconfigure_stdio()
    args = build_parser().parse_args(argv)
    # 需要真实浏览器的命令才走 asyncio；gen / check-persona / review / report
    # 都不联网，让它们也走 asyncio 只会多一层没必要的 loop。
    if args.cmd in ("report", "gen", "check-persona", "review"):
        if args.cmd == "review" and getattr(args, "apply", False):
            return cmd_apply_review(args)
        return args.func(args)
    return asyncio.run(args.func(args))


if __name__ == "__main__":
    sys.exit(main())