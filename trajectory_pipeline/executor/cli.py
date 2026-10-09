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
  与 ``user_prompt`` 均为空，含义是「不是 taskgen 跑的」。
- ``run --plan gen_out.json``：计划路径。消费 :mod:`plan` 里那份
  **按文件格式读**的计划（不 import taskgen），检索式、**用户原话**与
  provenance 都来自计划，原样进存档供 persona 切片。控制流一行没变。

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
from typing import Any, Mapping
from urllib.parse import urlsplit

from trajectory_pipeline.executor.archive import (
    DEFAULT_ROOT,
    REVIEWED_SUFFIX,
    P1Archive,
)
from trajectory_pipeline.executor.browser.mcp_client import EXE_ENV_VAR, McpClient
from trajectory_pipeline.executor.branches import NON_SAMPLE_BRANCHES
from trajectory_pipeline.executor.browser.obscura_driver import (
    FORBIDDEN_TOOLS,
    ObscuraDriver,
)
from trajectory_pipeline.executor.dom import DEFAULT_MAX_CHARS, detect_block
from trajectory_pipeline.executor.integrity import (
    Severity, check_root, format_report,
)
from trajectory_pipeline.executor.orchestrator import Orchestrator, RunConfig
from trajectory_pipeline.executor.plan import PlanError, load_plan
from trajectory_pipeline.executor.review_queue import (
    VERDICTS,
    apply_verdicts,
    collect as collect_reviews,
    write_queue as write_review_queue,
)
from trajectory_pipeline.executor.steps import search as search_step
from trajectory_pipeline.taskgen.sampler import W1_DEGENERATE_DIMS
from trajectory_pipeline.perception import questions
from trajectory_pipeline.perception.replay import (
    format_divergences, format_summary, replay_archive, summarize,
)
from trajectory_pipeline.perception.base import Q
from trajectory_pipeline.perception.factory import build_perceptor, describe


def _reconfigure_stdio() -> None:
    """放宽 ``errors``，并在**非 tty** 时强制 UTF-8。

    两件事分开做，各有各的理由：

    ``errors="replace"``
        Windows 控制台默认 GBK——探针上的 ✅/⚠️ 曾把整条链带崩。

    ``encoding="utf-8"``（**仅非 tty**）
        重定向或走管道时 Python 按 locale 编码输出，也就是 cp936。实测
        ``cli check-archives > out.txt`` 写出的首字节是 ``0xB4 0xE6``
        （GBK 的「存」），按 UTF-8 读直接报错——**CI 日志、重定向文件、
        任何下游读档的脚本全是坏的**，而这些恰恰是没有控制台代码页、
        消费方是程序的场合。UTF-8 是那类场合唯一说得通的编码。

        tty 时**不改**：交互式 PowerShell 的当前编码本就正常，
        强行换掉会改变用户看到的渲染结果，那是他们的选择不是我们的。

    实测这条正是 :mod:`trajectory_pipeline.executor.integrity` 要区分的那
    类假象的来源之一：**终端渲染乱码 ≠ 存档内容坏了**，两者必须分开。
    """
    for stream in (sys.stdout, sys.stderr):
        if not hasattr(stream, "reconfigure"):
            continue
        try:
            piped = not stream.isatty()
        except (AttributeError, ValueError):      # 被 pytest 等换成无 isatty 的对象
            piped = True
        stream.reconfigure(
            errors="replace", **({"encoding": "utf-8"} if piped else {})
        )


def _make_perceptor(args: argparse.Namespace, title: str):
    """构造该 task 的感知器。**每个 task 一个**。

    ``title`` 进构造参数是因为判断点 ① 需要目标片名——它是**任务的属性**，
    不在 ``Observation`` 里（那是页面的属性）。塞进 Observation 会污染事实层
    （P1 存档的观察字段会多出不属于页面的东西）。
    """
    return build_perceptor(mode=getattr(args, "perceptor", None),
                           target_title=title)


async def cmd_run(args: argparse.Namespace) -> int:
    if args.plan:
        return await _run_plan(args)
    return await _run_manual(args)


async def _run_manual(args: argparse.Namespace) -> int:
    """手工路径：``--task-id`` + ``--title``。**不带 provenance**。

    这条路径的存档 ``provenance`` 为空——含义是「不是 taskgen 跑的」。
    要切片出分就得走 ``--plan``。

    ⚠️ **``--title`` 缺失即报错，不拿 ``--task-id`` 顶替。**
    实测踩过的坑（2026-10-09）：少了 ``--title`` 就跑 ``T001 在线观看``，
    搜回来的是**轮胎** T001（泰坦途 Turanza 的胎压监测系统），
    于是判断点 ① 交出 6 条 ``not_play_site``，每条理由都写得
    像模像样——「T001（泰坦途）轮胎商品详情页，非影视作品观看页」。
    整批 6 条负样本、0 条成功、**一条异常日志都没有**，而每一条都是垃圾。

    这是**最贵的一种静默**：它不报错、不空转、产出的每条记录自身自洽，
    读存档时看到的只是一份「模型很有道理」的拒绝记录。
    片名是判断点 ① 的唯一判据来源（见 ``llm_perceptor`` 的
    ``_title_link_sufficiency``），拿编号冒充片名等于让这道题
    在一个错误的前提上自信作答——与 ``OBSCURA_EXE``、LLM 后端配置
    同一条纪律：**缺件即报错，不猜**。
    """
    if not args.title:
        print(f"[FAIL] 缺 --title。{args.task_id} 是**任务编号**不是作品名，"
              f"拿它当片名会搜到编号里的词（实测：T001 → 轮胎 T001），"
              f"而每一批错误素材都不会报错。"
              f"要么 --title <作品名>（可重复），要么 --plan <plan.json>。")
        return 2

    try:
        client = McpClient.from_env()
    except RuntimeError as exc:
        print(f"[FAIL] {exc}")
        return 2

    titles = [t.strip() for t in args.title]
    cfg = RunConfig(
        engine=args.engine,
        max_candidates=args.max_candidates,
        max_chars=args.max_chars,
        stop_after_success=args.stop_after_success,
    )
    archive = P1Archive(Path(args.out) if args.out else None)
    records = []

    async with client:
        driver = ObscuraDriver(client)
        print(f"server = {client.info.name} {client.info.version} "
              f"({client.info.tool_count} tools)")
        for title in titles:
            # **每个 task 一个 perceptor**：LLM 版的判断点 ① 需要目标片名，
            # 而片名是任务的属性不是页面的属性，靠构造注入。
            perceptor = _make_perceptor(args, title)
            print(f"perceptor = {describe(perceptor)}")
            orch = Orchestrator(driver, perceptor, cfg)
            print(f"\n[{args.task_id}] {title}")
            # 不传 user_prompt：手工路径没有 persona 渲染的提问，
            # 存档里留空串（含义与 provenance 空 dict 相同）。
            rec = await orch.run(args.task_id, title)
            records.append(rec)
            path = archive.write(rec, task_id=args.task_id)
            _print_summary(rec, path)

    _print_batch_footer(records)
    return 0


async def _run_plan(args: argparse.Namespace) -> int:
    """计划路径：消费 ``gen --out`` 的产物。

    与手工路径的差别**只有三处**：检索式、用户原话、provenance 都来自计划。
    控制流一行没变——这是「可替换插件」纪律的验收点：
    换掉数据来源不应该让执行端变形。

    ⚠️ 三处里 ``user_prompt`` 最容易在重构中被当成冗余参数删掉
    （它看着像 ``search_query`` 的重复），而删掉的后果是**静默**的：
    P2 六件套第 ③ 件恒为空，P1 里看不出任何异常。
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

    # 片名为空的条目**在开浏览器之前**挡掉。与手工路径同一条理由
    # （见 _run_manual 的注释）：顶替成 task_id 会产出一整批自洽而
    # 全错的素材，且不报任何错。这里把坏条目全列出来而不是第一个就退——
    # 一份坏计划通常不止一条坏，逐条报清楚比反复试省事。
    untitled = [t.task_id for t in tasks if not (t.title or "").strip()]
    if untitled:
        print(f"[FAIL] 计划里有 {len(untitled)} 条没有片名："
              f"{', '.join(untitled)}。"
              f"片名是判断点 ① 的唯一判据来源，拿 task_id 顶替会产出"
              f"自洽而全错的负样本，且不报错。重跑 gen 生成计划。")
        return 2

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
    records = []

    async with client:
        driver = ObscuraDriver(client)
        print(f"server = {client.info.name} {client.info.version} "
              f"({client.info.tool_count} tools)")
        for n, task in enumerate(tasks, 1):
            # 上面的检查已保证 title 非空，这里直接取
            title = task.title
            # 每 task 一个：判断点 ① 要目标片名（构造注入，见 _make_perceptor）
            orch = Orchestrator(driver, _make_perceptor(args, title), cfg)
            print(f"\n[{n}/{len(tasks)}] {task.task_id} | {task.persona_id} "
                  f"| {title}")
            rec = await orch.run(
                task.task_id, title,
                search_query=task.search_query,
                user_prompt=task.prompt_text,
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
    for mode in ("rule", "llm"):
        try:
            p = build_perceptor(mode=mode)
        except Exception as exc:
            # 显式 llm 未配置后端时会抛——那是配置问题，报出来而不是崩掉整条 check
            print(f"  {mode}: 不可用 — {type(exc).__name__}: {exc}")
            continue
        ready, reason = p.health()
        print(f"  {mode}: ready={ready} — {reason}")

    rule = build_perceptor(mode="rule")
    print("\n  规则版（W1）可判的题:")
    print("    FIND_PLAY_CONTROL — 播放控件词表 + 预告片判定")
    print("    PLAYER_OK        — <video>/<audio> 标签**存在性**（非「能播」）")
    print("                        iframe 数量不作判据：导航站满屏 iframe，"
          "实测会把 hao123 判成播放页")
    print("  规则版（W1）必然 None 的题（fail-closed）:")
    for qid in (Q.SELECT_PLAY_SITES, Q.IS_REACHABLE):
        print(f"    {qid}")

    try:
        build_perceptor(mode="llm")
    except Exception:
        print("  LLM 版（W3）: 未配置后端，四题全部走语义判定")
    else:
        from trajectory_pipeline.perception.llm_perceptor import CONFIDENCE_JUDGED

        print("  LLM 版（W3）四题全走语义判定，但有三条代码层守卫不可绕过:")
        print("    采集充分性预检 — 正文与元素皆空时不问模型（实测模型会判否）")
        print("    确定性事实优先 — <video> 存在性不问模型")
        print("    ref 白名单校验 — 模型回传的 ref 必须真的在观察里")
        print(f"    置信度先验 = {CONFIDENCE_JUDGED}（未校准，**不是模型自报**）")

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

    _print_dimension_spread(rep.get("dimension_spread") or {})
    return 1 if missing else 0


def _print_dimension_spread(spread: Mapping[str, Any]) -> None:
    """报「**实际落地**的维度分布」——与上面的「库覆盖度」不是一回事。

    覆盖度答「库里有没有这一档」，这里答「这批样本真正落到了几档」。
    两者会在降级维度上分叉：库里 ``task_specificity`` 三档齐全，
    而渲染后可能全是指名。**只有后者能预警切片表里那一维会退化成单行。**

    单开一节而不是并进覆盖度：合成一个数字的话，「探针 100% 通过」
    会被读成「画像库健康」——那两件事回答的根本不是同一个问题。
    """
    if not spread:
        return
    print("\n== 实际落地的维度分布（切片分母看这一节）==")
    # 两种退化**成因不同、处置也不同**，必须分开报：
    #   结构性 —— 渲染器在 W1 下表达不出这一维，补画像库也没用，只能等 W3；
    #   稀疏性 —— 维度是好的，只是最小档样本太少，加采样量或调配比即可。
    # 合成一个「退化维度」列表会把后者说成前者，而处置恰好相反。
    structural: list[str] = []
    sparse: list[str] = []
    for dim, d in spread.items():
        total = d["total"] or 1
        down_rate = d["downgraded"] / total
        # 分类**不看** ``degenerate`` 标志：那是「最小档够不够大」的判据，
        # 而结构性退化是「渲染器有没有表达出来」的判据——两者正交。
        # 实测 actual_specificity 正是交叉情形：降级率 28%（结构性），
        # 同时最小档 0.5%（稀疏）。按 degenerate 归类会把它报成
        # 「加采样量即可」，而真实原因是渲染器——加了也白加。
        if dim in W1_DEGENERATE_DIMS and down_rate > 0:
            mark = "  ⚠️ 结构性退化"
            structural.append(dim)
        elif d["degenerate"]:
            mark = "  ⚠️ 最小档过稀"
            sparse.append(dim)
        else:
            mark = ""
        print(f"  {dim:20s} actual={d['actual']}"
              f"  降级 {d['downgraded']}/{total} ({down_rate:.0%})"
              f"  最小档 {d.get('smallest_share', 0):.1%}{mark}")

    if not structural and not sparse:
        print("\n  各维度均落到 ≥2 档且最小档 ≥5%，切片表不会退化成单行")
        return
    if structural:
        print(f"\n  ⚠️ **结构性退化维度：{structural}**")
        print("     这些维度在 W1 下**渲染器结构上表达不出来**——不是画像库缺档，"
              "补画像库解决不了，只能等 W3 的 LLM 改写器。")
        print("     切片分母**必须**用 actual_*：用 persona 的 requested 值算分母"
              "会得出「该维度表现正常」，")
        print("     那是**一个根本没发生的实验有了结论**，比没有这个维度更糟。")
    if sparse:
        print(f"\n  ⚠️ **最小档过稀：{sparse}**")
        print("     这些维度本身是好的（渲染器能表达），只是某一档样本占比 <5%，"
              "切片表里那一行的结论是噪声。")
        print("     处置：加大 -n、调整 --strata 配比，或合并该档——"
              "**不是**渲染器的问题。")


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
        if path.name.endswith(REVIEWED_SUFFIX):
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
        dst = path.with_suffix(REVIEWED_SUFFIX)
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


def cmd_replay(args: argparse.Namespace) -> int:
    """感知层回放：在存档里的真实观察上跑感知实现，**不开浏览器**。

    选档案走 ``archive.select_archives``——与 ``report`` / ``check-archives``
    同一口径。:mod:`~trajectory_pipeline.perception.replay` 本身不 import
    executor（它只把 P1 当数据文件读），挑档案这件事留在这里做。
    """
    root = Path(args.out) if args.out else DEFAULT_ROOT
    picked = [p for p, _reviewed in P1Archive(root).select_archives()]
    if not picked:
        print(f"{root} 下没有 P1 存档")
        return 0

    files = picked[: args.limit] if args.limit else picked
    wanted = ("rule", "llm") if args.perceptor == "both" else (args.perceptor,)

    comparisons: list[Any] = []
    names: list[str] = []
    for path in files:
        try:
            doc = json.loads(path.read_text(encoding="utf-8"))
        except ValueError as exc:
            print(f"[skip] {path.name} 不是合法 JSON：{exc}")
            continue
        # **逐档构造**：判断点 ① 要目标片名，而片名是**任务的属性**。
        # 取全库第一个非空值是错的——T001 的「功夫」问不到 T018 的片名，
        # 而这种错法不会报错，只会让 ① 选出错的站。
        title = str(doc.get("title") or "")
        built: dict[str, Any] = {}
        for mode in wanted:
            try:
                built[mode] = build_perceptor(mode=mode, target_title=title)
            except Exception as exc:        # 缺后端等：说清缺什么，不静默退回
                print(f"[skip] {path.name} 的 {mode} 版没起来：{exc}")
        if not built:
            continue
        comparisons.extend(replay_archive(path, built))
        names = [m for m in wanted if m in built]

    if not comparisons:
        print("没有可回放的单元——存档里既无搜索观察也无访问记录，"
              "或整批都被反爬拦了")
        return 0
    print(format_summary(summarize(comparisons), names))
    print(format_divergences(comparisons, limit=args.show))
    return 0


def cmd_check_archives(args: argparse.Namespace) -> int:
    """存档完整性体检。

    与 ``report`` 的分工：``report`` 说「这批有多少条分支结论」，
    这里说「那些结论背后有没有东西」。两者共用
    :func:`~trajectory_pipeline.executor.archive.select_archives` 挑档案，
    所以不会出现「体检说 4 个档全过、报表说 9 个档里 5 个有问题」。
    """
    root = Path(args.out) if args.out else DEFAULT_ROOT
    report = check_root(root)
    print(format_report(report))
    if args.fail_on_degraded and report.by_severity(Severity.DEGRADED):
        return 1
    return 0 if report.ok else 1


def cmd_purge_run(args: argparse.Namespace) -> int:
    """回滚一个 run——**存档与它并进池的负样本一起删**。

    默认**只列不删**（``--apply`` 才真删）。这条命令动的是负样本池，
    而池是训练数据的直接输入，删错了不可逆——所以默认口径必须是
    「先看清楚」。

    什么时候该用：某个 run 的**前提**错了，于是它整批结论都不成立。
    实测（2026-10-09）：``run`` 少给 ``--title`` 就拿 ``T001`` 当片名，
    搜成「轮胎 T001」，产出 6 条 ``not_play_site``——那些站确实不是
    T001 轮胎的观看页（标签对检索式是真的），但它们要表达的是
    「这里看不了《功夫》」，而**存档里看不出任何异常**。

    什么时候**不该**用：个别几条标签被证明错了（``check-archives``
    的 ``negative_contradicted_by_success`` 会指出来）。那种按 URL
    逐条定，不要整批回滚——同 run 里可能有完全正确的结论。
    """
    root = Path(args.out) if args.out else DEFAULT_ROOT
    store = P1Archive(root)
    try:
        archives, rows = store.purge_run(args.run_id)
    except ValueError as exc:
        print(f"[FAIL] {exc}")
        return 2

    print(f"run_id = {args.run_id}   根目录 = {root}")
    print(f"\n存档（{len(archives)}）:")
    for path in archives:
        print(f"  {path.name}")
    print(f"\n负样本池行（{len(rows)}）:")
    for row in rows:
        print(f"  [{row.get('branch')}] {str(row.get('url'))[:78]}")
        print(f"      {str(row.get('evidence'))[:110]}")
    if not archives and not rows:
        print("\n这个 run_id 下没有东西。run_id 是存档文件名里 __ 后半段"
              "（T001__b3a3548a.json → b3a3548a），别把整条 task_id 填进来。")
        return 1

    if not args.apply:
        print("\n以上为**预演**。确认无误后加 --apply 执行。")
        return 0
    n_arc, n_rows = store.apply_purge(archives, rows)
    print(f"\n已删除：存档 {n_arc} 个，池行 {n_rows} 条。"
          f"建议接着跑 check-archives 看池与全库是否还自相矛盾。")
    return 0


def cmd_report(args: argparse.Namespace) -> int:
    root = Path(args.out) if args.out else DEFAULT_ROOT
    # 只认真存档（同目录的探针取证不参与），人工复核档取代原档。
    # 分母错的报表比没有报表更坏，理由见 archive.select_archives。
    picked = P1Archive(root).select_archives()
    if not picked:
        print(f"{root} 下没有 P1 存档")
        return 0
    files = [p for p, _reviewed in picked]
    reviewed_n = sum(1 for _p, rev in picked if rev)
    counts: Counter = Counter()
    missing: Counter = Counter()
    blocked: Counter = Counter()
    filters: Counter = Counter()
    legacy_recomputed: list[str] = []
    outcomes: list[Mapping[str, Any]] = []
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
        outcomes.extend(o for o in (data.get("outcomes") or []) if isinstance(o, Mapping))
        reason = str(data.get("search_blocked") or "")
        recomputed = False
        if "search_blocked" not in data:
            # 存量存档（该字段引入之前跑的）：从存档里的观察**确定性重算**。
            #
            # 不重算的话，这些存档会被算成「可跑」——而其中被验证码页拦下的
            # 恰恰是最不该被算作可跑的。与 provenance 那条同款：**缺字段 ≠ 空值**。
            # 判定用的是同一个 :func:`detect_block`，不是第二套逻辑。
            #
            # 正文取 ``body_text``（全文）优先、``body_preview`` 兜底：
            # 限流词（"访问过于频繁"）完全可能落在摘要之外，用摘要判会漏，
            # 而漏掉的结果是「被拦的存档被算成可跑」。``body_preview`` 分支
            # 服务的是摘要时代落的老存档——那些存档本来就只剩摘要可用。
            o = data.get("search_observation") or {}
            reason = detect_block(
                str(o.get("url") or ""), str(o.get("page_title") or ""),
                str(o.get("body_text") or o.get("body_preview") or ""),
            )
            recomputed = bool(reason)
        if reason:
            blocked[reason] += 1
            if recomputed:
                legacy_recomputed.append(f.name)
        else:
            usable += 1
        filters.update(data.get("candidate_filter") or {})

    head = f"存档 {len(files)} 个，站点合计 {total_sites}"
    print(head + (f"（其中 {reviewed_n} 个已过人工复核）" if reviewed_n else ""))

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

    _print_domain_reach(outcomes)
    return 0


#: 「零成功域名」进候选黑名单的最低访问次数。
#:
#: 1 次访问 1 次失败说明不了任何问题——可能是这个片没有、可能是这一次
#: 网络抖动。低于这个数就报出来，只会让黑名单里塞满一次性噪音。
_ZERO_SUCCESS_MIN_VISITS = 2


def _site_domain(url: str) -> str:
    """存档里一条 outcome 的站点域名。

    **只剥 ``www.``，不做 eTLD+1 归并**：``v.youku.com`` 与 ``www.youku.com``
    在反检测这件事上是**两个不同的入口**（跳转链、指纹、TLS 都不一样），
    归并掉就看不见「一个入口通一个不通」这种最该看到的信号。
    真要归并得用公共后缀表，离线环境没有，而两段式启发式会在
    ``.com.cn`` / ``.co.uk`` 上错切——错切出来的域名分布是**假的**。
    """
    try:
        host = (urlsplit(url).hostname or "").lower()
    except ValueError:
        return ""
    return host[4:] if host.startswith("www.") else host


def _print_domain_reach(outcomes: list[Mapping[str, Any]]) -> None:
    """成功站点域名分布——**D7 验收项**（设计方案里标 ❌ 未做的那条）。

    它回答的是一个别处问不出来的问题：**obscura 的反检测实际通过率是多少**。
    分支分布答不了——``unresolved`` 有九成是「页面拿到了但判不出播放器」，
    那既可能是被拦了，也可能是页面本身没播放器，两者在那一列里长一样。

    成功还要按来源拆开报，因为「规则版找到了播放页」和「人工看完截图
    确认这站能看」是**两回事**：规则版只认 ``<video>`` 标签，真实视频站
    几乎全是 JS/canvas 播放器，W1 的一条都不该由它判成功。合成一个
    「成功 N」会把这两件事一起说出去，而结论只站得住其中一种。

    分类**从 ``branch`` 现算**，不读存档里那份冗余的 ``is_negative_sample``：
    它是 ``branch`` 的纯函数（见
    :attr:`~trajectory_pipeline.executor.branches.SiteOutcome.is_negative_sample`），
    而 ``branch not in NON_SAMPLE_BRANCHES`` 这条规则必须与负样本池
    （:meth:`~trajectory_pipeline.executor.archive.P1Archive._append_negatives`）
    **完全一致**——报表说「负」而池里没有、或反过来，两边一比就发现是口径分叉。
    老存档缺这个字段时，读它会把真负样本静默掉进「未判」桶。
    """
    print("\n== 成功站点域名分布（D7 验收：反检测实际通过率）==")
    stats: dict[str, Counter] = {}
    by_source: Counter = Counter()
    fallback_success = 0
    for o in outcomes:
        domain = _site_domain(str(o.get("url") or ""))
        if not domain:
            continue
        bucket = stats.setdefault(domain, Counter())
        bucket["visited"] += 1
        branch = o.get("branch")
        if branch is None:
            bucket["success"] += 1
            source = str(o.get("source") or "")
            by_source[source or "?"] += 1
            if o.get("fallback_used"):
                fallback_success += 1
        elif branch not in NON_SAMPLE_BRANCHES:
            bucket["negative"] += 1
        else:
            # unresolved / trailer_suspect：判不出来，**不是负样本**。
            bucket["unresolved"] += 1

    if not stats:
        print("  存档里没有 outcomes——先跑 run（不是 review 那步的产物）。")
        return

    visited = sum(b["visited"] for b in stats.values())
    success = sum(b["success"] for b in stats.values())
    rate = success / max(1, visited)
    print(f"  访问 {visited} 站 / 成功 {success} 站（{rate:.1%}），"
          f"落在 {len(stats)} 个域名上")

    winners = sorted(((d, b) for d, b in stats.items() if b["success"]),
                     key=lambda kv: (-kv[1]["success"], kv[0]))
    for domain, b in winners:
        print(f"    {domain:32s} 成功 {b['success']} / 访问 {b['visited']}"
              f"    负 {b['negative']} · 未判 {b['unresolved']}")

    if not winners:
        print("    **本批零成功**。规则版只认 <video>/<audio>，真实视频站"
              "（JS/canvas/iframe 播放器）一条都判不出来，这是设计上的预期。")
        print("    W1 正样本走人工复核：review --write → 人工填 verdict → "
              "review --verdicts … --apply，然后重跑本命令。")

    src = " / ".join(f"{k} {v}" for k, v in by_source.most_common())
    print(f"  成功来源：{src or '（无）'}"
          + (f"，其中走 fallback {fallback_success}" if fallback_success else ""))
    if fallback_success:
        print(f"    ⚠️ {fallback_success}/{success} 成功是 **fallback** 判出来的，"
              "不是语义判定。")
        print("       存在性 ≠ 能正常播放，也 ≠ 播的是这部片子——"
              "正样本基线被它抬高，成功率会虚高。")

    dead = sorted(((d, b) for d, b in stats.items()
                   if not b["success"] and b["visited"] >= _ZERO_SUCCESS_MIN_VISITS),
                  key=lambda kv: (-kv[1]["visited"], kv[0]))
    if dead:
        # 带上 负/未判 拆分，不只是访问数。**零成功是 W1 的常态**，
        # 而「零成功」有两种成因——全是真负样本（这站没这部片）还是
        # 全是 unresolved（页面拿到了但判不出播放器）——处置完全相反：
        # 前者该拉黑，后者该换感知层。只报访问数会把它们混成一条。
        detail = " · ".join(
            f"{d} {b['visited']}（负 {b['negative']} · 未判 {b['unresolved']}）"
            for d, b in dead)
        print(f"  零成功域名（累计访问 ≥2，可进候选黑名单）：{detail}")
        print("    注意：这是**候选**不是判决——同站不同片结果可能不同，"
              "拉黑前先看 negative.jsonl 里那几条的证据。")


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="trajectory_pipeline.executor.cli")
    sub = p.add_subparsers(dest="cmd", required=True)

    run = sub.add_parser("run", help="跑一个 task 并落 P1")
    run.add_argument("--task-id", default="T001")
    run.add_argument("--title", action="append",
                     help="作品名，可重复。**不给即报错**——task-id 是编号不是片名，"
                          "拿它顶替会搜到编号里的词并产出整批假负样本")
    run.add_argument("--engine", default=search_step.DEFAULT_ENGINE)
    run.add_argument("--max-candidates", type=int, default=20)
    run.add_argument("--max-chars", type=int, default=DEFAULT_MAX_CHARS)
    run.add_argument("--stop-after-success", type=int, default=0)
    run.add_argument("--out", default=None, help="P1 输出目录")
    run.add_argument("--perceptor", choices=("rule", "llm", "auto"), default=None,
                     help="感知实现。默认读 TRAJECTORY_PERCEPTOR，再默认 auto"
                          "（配了 LLM 后端就用 LLM 版，否则规则版）")
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
        Path(__file__).resolve().parents[1] / "taskgen" / "data" / "tasks.yaml"))
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
        Path(__file__).resolve().parents[1] / "taskgen" / "data" / "tasks.yaml"))
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

    # 命名不叫 check：那已经是「查 obscura 能力」了，两个都叫 check
    # 会让人以为 check-archives 也要浏览器。它不联网、不要 OBSCURA_EXE。
    ck_a = sub.add_parser("check-archives", help="P1 存档完整性体检（不联网）")
    ck_a.add_argument("--out", default=None, help="P1 存档目录")
    ck_a.add_argument("--fail-on-degraded", action="store_true",
                      help="把 DEGRADED 也算失败。默认只拦 FATAL——"
                           "降级样本仍可用，只是不能直接当训练数据")
    ck_a.set_defaults(func=cmd_check_archives)

    rp = sub.add_parser("replay", help="感知层回放：在存档的真实观察上跑感知实现（不联网）")
    rp.add_argument("--out", default=None, help="P1 存档目录")
    rp.add_argument("--perceptor", choices=("rule", "llm", "both"), default="rule",
                    help="默认只跑规则版（**不需要任何 LLM 后端**，"
                         "于是「W1 在真实数据上答得出来几道」完全离线可测）")
    rp.add_argument("--limit", type=int, default=0, help="只回放前 N 份存档；0 = 全部")
    rp.add_argument("--show", type=int, default=20, help="列出前 N 条分歧")
    rp.set_defaults(func=cmd_replay)

    pg = sub.add_parser(
        "purge-run",
        help="回滚一个 run：删它的存档 + 它并进 negative.jsonl 的行")
    pg.add_argument("--run-id", required=True,
                    help="存档文件名里 __ 后半段（T001__b3a3548a.json → b3a3548a）")
    pg.add_argument("--out", default=None, help="P1 存档目录")
    pg.add_argument("--apply", action="store_true",
                    help="**默认只列不删**。池是训练数据的直接输入，删错不可逆")
    pg.set_defaults(func=cmd_purge_run)
    return p


def main(argv: list[str] | None = None) -> int:
    _reconfigure_stdio()
    args = build_parser().parse_args(argv)
    # 需要真实浏览器的命令才走 asyncio；gen / check-persona / review / report
    # 都不联网，让它们也走 asyncio 只会多一层没必要的 loop。
    if args.cmd in ("report", "gen", "check-persona", "review", "check-archives",
                    "replay", "purge-run"):
        if args.cmd == "review" and getattr(args, "apply", False):
            return cmd_apply_review(args)
        return args.func(args)
    return asyncio.run(args.func(args))


if __name__ == "__main__":
    sys.exit(main())