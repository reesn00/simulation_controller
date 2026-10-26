"""新树 CLI 入口——``python -m trajectory_pipeline.executor.cli run``。

子命令：

    run              跑一个 task（真实浏览器），落 P1
    gen              模块 1 采样：骨架 × persona → 任务实例计划（不联网）
    review           从 P1 存档导出**人工复核队列**（W1 正样本的唯一来源）
    check            只查能力（不跑任务）：执行层可达性 / Perceptor 能力边界
                     （``--driver camoufox`` 探 D11 的会话层与实测指纹）
    check-persona    画像库覆盖度体检 + 判分保真探针体检（不联网）
    regenerate-rationales  存量 rationale 重生成（INC46 动作前动机口径，
                     需 TRAJECTORY_LLM_*；建议先 --dry-run）
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
import dataclasses
import inspect
import json
import logging
import os
import re
import shutil
import string
import sys
import uuid
from contextlib import AsyncExitStack, asynccontextmanager
from types import MappingProxyType
from collections import Counter
from pathlib import Path
from typing import Any, Callable, Final, Mapping
from urllib.parse import urlsplit

from trajectory_pipeline.executor.agent_loop import AgentBackend
from trajectory_pipeline.executor.archive import (
    DEFAULT_ROOT,
    REVIEWED_SUFFIX,
    CredentialLeak,
    P1Archive,
    select_archives,
)
from trajectory_pipeline.executor.browser.mcp_client import (
    EXE_ENV_VAR,
    McpClient,
    McpError,
)
from trajectory_pipeline.executor.branches import NON_SAMPLE_BRANCHES, RunLedger
from trajectory_pipeline.executor import downgrade as downgrade_mod
from trajectory_pipeline.executor import relabel as relabel_mod
from trajectory_pipeline.executor import pool_drop as pool_drop_mod
from trajectory_pipeline.executor.browser.driver_factory import (
    DEFAULT_KIND,
    SUPPORTED_KINDS,
    make_driver,
)
from trajectory_pipeline.executor.browser.obscura_driver import FORBIDDEN_TOOLS
# 增量二十五：``DriverError`` 给 _collect_l4_text 鸭子协议降级用。
from trajectory_pipeline.executor.browser.page_driver import DriverError
from trajectory_pipeline.executor.dom import DEFAULT_MAX_CHARS, detect_block
from trajectory_pipeline.executor.integrity import (
    Severity, check_root, format_report,
)
from trajectory_pipeline.executor.mode import (
    AGENT,
    ENV_EXECUTOR_MODE,
    EXECUTOR_MODES,
    configured_mode,
)
from trajectory_pipeline.executor.orchestrator import (
    DISCOVERY_PATHS,
    Orchestrator,
    RunConfig,
    RunRecord,
)
from trajectory_pipeline.executor.plan import PlanError, load_plan
from trajectory_pipeline.executor.probe import (
    DEFAULT_TIMEOUT_S as MEDIA_DEFAULT_TIMEOUT_S,
    FFPROBE_BIN,
    PARTIAL_FREE_MAX_DURATION_S,
    PARTIAL_FREE_MAX_CURRENT_TIME_S,
    dump_media_result,
    dump_result,
    ffprobe_available,
    probe,
    probe_media,
    run_ffprobe_subprocess,
    to_branch,
    to_media_branch,
    probe_browser_media,
    to_browser_branch,
    dump_browser_result,
    BROWSER_DEFAULT_TIMEOUT_S,
    PROBE_WAIT_S,
)
# 2026-10-13 增量二十五：L4 VLM 复核串联。
# INC28_L1_WIRING_v1 配套：L1 ffprobe 真接入 BrowserProbeResult。
# 2026-10-13 增量三十四：MultimVLMReviewer + build_reviewer 工厂接入，
# CLI 加 --reviewer 选项（heuristic | multim）。
from trajectory_pipeline.executor.review.vlm_reviewer import (
    HeuristicVLMReviewer,
    VLMReviewInput,
    VLMReviewResult,
    VLMReviewer,
)
from trajectory_pipeline.executor.review.multim_vlm import (
    MultimVLMReviewer,
    build_reviewer,
)
from trajectory_pipeline.executor.review_queue import (
    VERDICTS,
    ReviewQueueError,
    apply_verdicts,
    collect as collect_reviews,
    dispatch_l6,
    prefill as rq_prefill,
    settle_same_site as rq_settle,
    write_queue as write_review_queue,
)
from trajectory_pipeline.executor.steps import search as search_step
from trajectory_pipeline.executor.steps.visit import visit_site
from trajectory_pipeline.assembler import builder, views
from trajectory_pipeline.taskgen.sampler import W1_DEGENERATE_DIMS
from trajectory_pipeline.perception import questions
from trajectory_pipeline.perception.replay import (
    format_divergences, format_summary, replay_archive, summarize,
)
from trajectory_pipeline.common.elements import element_name
from trajectory_pipeline.perception.base import Observation, Q
from trajectory_pipeline.perception.factory import build_perceptor, describe
# 增量二十六：L3 规则层独立判定（不调 LLM，单 obs body_text 全文扫描）。
# 与 L4 同 fail-closed 跳过；Question 不走 REGISTRY，由 L5 仲裁层消费。
from trajectory_pipeline.executor.arbitration import arbitrate as arbitrate_l5
from trajectory_pipeline.perception.rule_perceptor import RulePerceptor


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


def _make_rationale_writer():
    """构造 rationale 生成器。**没有可用后端时返回 ``(None, 原因)``。**

    ``None`` 的含义是「这一步没长出来」，不是「跑过了且没问题」——
    组装期据此把闸门记 ``not_run``。**绝不静默合成一段占位文字**：
    那会让闸门跑在一段没人写过的文本上，结论全部失真。

    这里 ``return None`` 而 ``raise`` 是刻意的，与 ``--perceptor auto``
    的口径一致：W1 规则版批次必须在没有后端的机器上能跑通。
    **但原因必须带回来**——只返回 ``None`` 而不说是为什么，
    就会重现「能力不可用」那个症状：报表上只看到闸门全批
    ``not_run``，没人知道是没配后端还是代码坏了。
    """
    try:
        from trajectory_pipeline.llm.client import LLMClient, LLMConfig
        from trajectory_pipeline.rationale import writer as rationale_writer
    except ImportError as exc:
        return None, f"import 失败 {type(exc).__name__}: {exc}"
    try:
        cfg = LLMConfig.from_env()
    except Exception as exc:                     # 未配齐或配置不合法
        return None, f"LLM 后端不可用 {type(exc).__name__}: {exc}"

    client = LLMClient(cfg)

    def write(**kw):
        """薄适配：把 :class:`WriteResult` 折成 P1 里落盘的形状。

        **三态原样透传**（靠 ``WriteResult.to_json``）：``written`` 带
        ``rationale`` 文本；``unavailable`` / ``empty`` 的 ``rationale`` 是
        ``None`` 并带 ``reason``——**文本位置留空**，因为「模型这轮没说话」
        与「有文本但被红线拒写」在下游必须分得开（前者不该进负样本池，
        后者更不该）。这里不做任何判定：**闸门在组装期跑**。
        """
        return rationale_writer.write_one(client, **kw).to_json()

    def reflect(**kw):
        """终局反思入口（INC46）。落 unit 级 ``reflection`` 键。

        与 ``write`` **同形状**（都是 :meth:`WriteResult.to_json`），
        于是 P1 里的 ``reflection`` 与 ``steps[i].rationale`` 同构，
        组装期用同一套代码读。

        挂成属性而不是让 ``write`` 自己分派：逐步入口的 kwargs 里没有
        能区分「这是第几步」的东西，靠参数嗅探去分派等于把两个语义
        不同的调用塞进一个签名，而「本步观察能不能喂」正是 INC46 要
        钉死的那条区别。
        """
        return rationale_writer.write_reflection(client, **kw).to_json()

    write.reflect = reflect             # type: ignore[attr-defined]
    write.close = client.close          # type: ignore[attr-defined]
    write.model = cfg.model             # type: ignore[attr-defined]
    return write, ""


def _describe_rationale(writer, why: str) -> str:
    """把 writer 的有无**连同原因**明写出来。

    没有它时闸门是 ``not_run``，而报表上「没跑」与「跑过且通过」长得很像——
    这条口径与判断点 ① 的 fail-closed 是同一个道理：**能力不可用要说出来**。
    """
    if writer is None:
        return f"不可用（{why}）—— **不生成 rationale**，闸门记 not_run（≠ 通过）"
    return f"已接上（{writer.model}）"


def _make_agent_backend():
    """组装 agent 模式的后端四件套。**没有可用后端时返回 ``(None, 原因)``**。

    与 :func:`_make_rationale_writer` 同款形状：不说原因的话，报表上只看
    到这批「没灰度」，没人知道是没配后端还是代码坏了（能力不可用
    必须说出来，理由见 :func:`_describe_rationale`）。

    ⚠️ 它立在**本文件**而不是 ``executor`` 里：:class:`AgentBackend` 装的是
    LLM 客户端与 llm 侧的解析器，而 ``executor`` 不 import ``llm``
    （01-架构总览 L26-50）。本文件是唯一能同时看见两边的边界层——
    ``_make_rationale_writer`` 也是为了同一条边而活在这里。
    """
    try:
        from trajectory_pipeline.llm import schema_parse
        from trajectory_pipeline.llm.client import LLMClient, LLMConfig
    except ImportError as exc:
        return None, f"import 失败 {type(exc).__name__}: {exc}"
    try:
        cfg = LLMConfig.from_env()
    except Exception as exc:                     # 未配齐或配置不合法
        return None, f"{type(exc).__name__}: {exc}"
    try:
        # §3.5 G2（A2a-4）：升配三键全没配 → None（无升配，不 fail）；
        # 配了一半 → LLMUnavailable，与主后端同一条「缺件即报错」纪律——
        # 静默当无升配处理，那一半配置就成了没人读的尸体。
        esc_cfg = LLMConfig.escalate_from_env(base=cfg)
    except Exception as exc:
        return None, f"升配后端配置不合法：{type(exc).__name__}: {exc}"

    return (
        AgentBackend(
            client=LLMClient(cfg),
            # 与 :func:`agent_loop` docstring 里点名的生产侧解析器同款：
            # 剥 ``<think>`` → 抠 JSON。解析器**注入**而不是在 executor
            # 里 import，是为了让格式契约（schema）留在 loop 侧、
            # 文本解析留在 llm 侧（见 agent_loop 模块 docstring）。
            json_of=lambda raw: schema_parse.extract_json(
                schema_parse.strip_think(raw)
            ),
            escalate=LLMClient(esc_cfg) if esc_cfg is not None else None,
        ),
        "",
    )


def _describe_agent(backend) -> str:
    """把 agent 后端的状态写清楚。**升配配没配都要写在明面上**——
    「未配置」与「已配置」在报表上不写出来，读的人就会默认它是配好的
    （能力不可用必须说出来，理由见 :func:`_describe_rationale`）。"""
    model = getattr(getattr(backend.client, "cfg", None), "model", "?")
    esc = getattr(backend, "escalate", None)
    if esc is None:
        return f"已接上（{model}；升配后端=未配置，EscalateModel 只落事件）"
    esc_model = getattr(getattr(esc, "cfg", None), "model", "?")
    return f"已接上（{model}；升配后端={esc_model}）"


def _resolve_executor(requested: str | None = None) -> tuple[str, Any, str]:
    """构造期解析执行器模式：``(mode, backend, 打印文案)``。

    优先级（A3-1 转正）：``--executor-mode``（run 子命令参数）>
    ``TRAJECTORY_EXECUTOR_MODE``（私有环境变量，A3 结束后撤）> 默认
    ``scripted``。两条门同一套缺件纪律——

    **缺件即报错，不猜**（与 ``--perceptor llm`` 未配后端报错同款口径）：
    agent 模式没有后端时若静默退回 scripted，那一批在存档上写的是
    ``executor_mode="scripted"``，于是「agent 批失败了 40%」会被读成
    「scripted 跑得好好的」——配置错误伪装成一次成功的灰度。
    """
    try:
        mode = requested if requested is not None else configured_mode()
    except ValueError as exc:
        return "", None, f"[FAIL] {exc}"
    via = "--executor-mode" if requested is not None else ENV_EXECUTOR_MODE
    if mode != AGENT:
        return mode, None, (
            f"executor = scripted（经 {via}；agent 模式用 "
            f"--executor-mode agent 切——需配齐 TRAJECTORY_LLM_*）"
        )
    backend, why = _make_agent_backend()
    if backend is None:
        return "", None, (
            f"[FAIL] 要 agent 模式（经 {via}），但 agent 后端不可用：{why}。"
            f"要么配齐 TRAJECTORY_LLM_*，要么显式用 scripted 跑"
            f"（agent 模式没有后端时不会静默退回——那会让配置错误"
            f"伪装成一次成功的灰度）。"
        )
    return mode, backend, f"executor = agent（经 {via}；{_describe_agent(backend)}）"


async def cmd_run(args: argparse.Namespace) -> int:
    if args.plan:
        return await _run_plan(args)
    return await _run_manual(args)


def _positive_float(raw: str) -> float:
    """argparse 的 ``type``：只收**正的有限小数**。

    超时是 0 或负数没有业务含义——而它一旦被放行，那批会「立刻超时」，
    产出的是**看起来正常的空批**。缺省即报错，不猜。
    """
    try:
        value = float(raw)
    except (TypeError, ValueError):
        raise argparse.ArgumentTypeError(f"不是数字：{raw!r}") from None
    if value != value:                      # NaN
        raise argparse.ArgumentTypeError(f"不是数字：{raw!r}")
    if value in (float("inf"), float("-inf")):
        raise argparse.ArgumentTypeError(f"必须是有限数：{raw!r}")
    if value <= 0:
        raise argparse.ArgumentTypeError(f"必须大于 0：{raw!r}")
    return value


def _per_site_timeout(args: argparse.Namespace) -> float:
    """单站超时的取值。**不传 = 走源码默认值**，不猜。

    ⚠️ **不能用 ``or``**：`0` 是假值 ⇒ ``--per-site-timeout 0`` 会被
    静默读成默认值 180。「没给」与「给了 0」是两件事——后者是配错了，
    配错了该当场报错，不该悄悄换个值继续跑。
    """
    value = getattr(args, "per_site_timeout", None)
    if value is None:
        return RunConfig.per_site_timeout_s
    return float(value)


async def _launch_camoufox(*, headless: bool = True) -> Any:
    """起一个 camoufox 会话。**唯一的 launch 入口**（测试可打桩）。

    ⚠️ 为什么单列一支：``CamoufoxSession.launch`` 会**真的下载 fpgen 模型**
    并起浏览器进程。测试若走生产链默认（camoufox）就会在测试里
    拉网络 + 起进程——慢、且被 ``pytest-socket`` 拦成红。

    单列一个函数不是为了让生产代码好写，是为了让**测试有一个可打桩的缝**：
    测试打桩 ``cli._launch_camoufox``，就不用真起浏览器。

    另一个更根本的缝在**调用方**：测试应当显式钉住 ``--driver obscura``
    （它本来就在 monkeypatch ``cli.make_driver``），而不是靠默认值碰巧
    不联网。**默认值变了，测试就该显式钉住驱动**——这是这次切换的教训。
    """
    from trajectory_pipeline.executor.browser.camoufox_session import CamoufoxSession

    return await CamoufoxSession.launch(headless=headless)


@asynccontextmanager
async def _driver_for_run(kind: str, *, shared: Any = None,
                          headless: bool = True,
                          collect_element_basis: bool = False):
    """一次 Run 的执行层句柄（P5）。**两种实现两种生命周期**：

    - ``obscura``：整批复用一条 MCP 连接上的驱动（``shared``）。P5 之前就是
      这样，**一行行为都不改**——把连接改成每 Run 一条，是另一件事，不该混在
      换执行层这一批里。
    - ``camoufox``：**一个 Run 一次 launch**（设计方案 04 §4.3）——身份、
      指纹、cookie 全随 Run 重建。跨 Run 复用会把上一部作品的会话状态带进
      下一条样本，而归因要的正是「这一条样本是在哪个身份下采到的」。
      launch 失败**抛**（``DriverError`` / playwright 异常）——那是能力问题，
      由调用方决定整批是否继续，不在这里吞。

    ``headless`` 只对 camoufox 分支有意义（obscura 是远端 MCP，没有这个概念）：
    **无人值守默认无头**（2026-10-21 口径反转——headful 窗口不可渲染时
    单次点击要 5–13s，会把一半的点击打成超时假负样本，见
    :meth:`CamoufoxSession.launch`）。``--headful`` 显式切回有头，
    给「人守着的桌面」那条路。

    ``collect_element_basis`` 传给 camoufox 分支的驱动；obscura 分支的驱动是
    批次级共享的（``shared``），采集开关在**造它的时候**就定下了（见
    ``_run_manual`` / ``_run_plan``）——共享对象的开关不能逐 Run 改。

    认不出的 ``kind`` 由 :func:`make_driver` 抛 ``ValueError``，本函数不预判。
    """
    if shared is not None:
        yield shared
        return
    session = await _launch_camoufox(headless=headless)
    try:
        yield make_driver(kind=kind, camoufox_session=session,
                          collect_element_basis=collect_element_basis)
    finally:
        await session.close()


@asynccontextmanager
async def _probe_driver(kind: str, *, headless: bool = True,
                        collect_element_basis: bool = False):
    """探针工具的驱动会话：**整批一条**。

    与 :func:`_driver_for_run` 的区别是**粒度**，不是实现：

    - :func:`_driver_for_run`：**一个 Run 一次 launch**（生产链，每 task 一个
      Run）——身份、指纹、cookie 全随 Run 重建，跨 Run 复用会把上一部作品
      的会话状态带进下一条样本。
    - 本函数：**一条命令跑一批 URL**（``probe`` / ``proberun`` /
      ``mediabrowse`` 都是这个形态）。每 URL 启一次浏览器要秒级 launch，
      慢到不可接受 ⇒ 整批复用一条会话。

    ⚠️ **两种实现的会话纪律相反**（obscura 一条 MCP 连接 / camoufox 一次
    launch），但**「一条命令一条会话」对两者都成立**——obscura 本来就是
    整批复用一条连接。

    认不出的 ``kind`` 由 :func:`make_driver` 抛 ``ValueError``，本函数不预判。
    """
    if kind == "obscura":
        client = McpClient.from_env()
        async with client:
            yield make_driver(client, kind="obscura",
                              collect_element_basis=collect_element_basis)
        return
    session = await _launch_camoufox(headless=headless)
    try:
        yield make_driver(kind=kind, camoufox_session=session,
                          collect_element_basis=collect_element_basis)
    finally:
        await session.close()


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

    kind = getattr(args, "driver", DEFAULT_KIND)
    client = None
    if kind == "obscura":
        try:
            client = McpClient.from_env()
        except RuntimeError as exc:
            print(f"[FAIL] {exc}")
            return 2

    titles = [t.strip() for t in args.title]
    # 执行器模式在**构造期**定下（``executor.mode`` 的纪律）：模式是
    # 批次级属性，不该等跑到某个候选才决定。解析先于 RunConfig 构造——
    # 模式的产地是 RunConfig（A3-1 转正），backend 随模式一起在这里备好。
    mode, agent_backend, banner = _resolve_executor(args.executor_mode)
    if not mode:
        print(banner)
        return 2
    cfg = RunConfig(
        engine=args.engine,
        max_candidates=args.max_candidates,
        max_chars=args.max_chars,
        stop_after_success=args.stop_after_success,
        discovery_path=args.discovery_path,
        max_intermediates=args.max_intermediates,
        max_referral_links=args.max_referral_links,
        executor_mode=mode,
        # 单站超时（2026-10-22 起暴露到 CLI）。**不传就用源码默认值**——
        # 落进 run_config 是 B-4 唯一的时限判据，不落盘等于没有判据。
        per_site_timeout_s=_per_site_timeout(args),
    )
    archive = P1Archive(Path(args.out) if args.out else None)
    records = []
    writer, why = _make_rationale_writer()
    print(f"rationale = {_describe_rationale(writer, why)}")
    print(banner)

    async with AsyncExitStack() as batch:
        shared = None
        if client is not None:
            client = await batch.enter_async_context(client)
            print(f"server = {client.info.name} {client.info.version} "
                  f"({client.info.tool_count} tools)")
            # obscura：整批一条连接复用（P5 前就是这条路径，行为不变）
            # **kind 必须显式写 obscura**：这条分支只在 ``kind == "obscura"``
            # 时才走到（client 是 McpClient），而 DEFAULT_KIND 已是 camoufox
            # ——不写就会拿 obscura 的连接去造 camoufox 的驱动。
            shared = make_driver(
                client,
                kind="obscura",
                collect_element_basis=getattr(args, "element_basis", False),
            )
        print(f"driver = {kind}")
        for title in titles:
            # **每个 task 一个 perceptor**：LLM 版的判断点 ① 需要目标片名，
            # 而片名是任务的属性不是页面的属性，靠构造注入。
            perceptor = _make_perceptor(args, title)
            print(f"perceptor = {describe(perceptor)}")
            # camoufox 面**一个 Run 一次 launch**（见 _driver_for_run）；
            # obscura 面复用批次驱动，yield 的就是同一个对象。
            async with _driver_for_run(
                kind, shared=shared,
                headless=not getattr(args, "headful", False),
                collect_element_basis=getattr(args, "element_basis", False),
            ) as driver:
                orch = Orchestrator(driver, perceptor, cfg,
                                    rationale_writer=writer,
                                    agent_backend=agent_backend)
                print(f"\n[{args.task_id}] {title}")
                # 不传 user_prompt：手工路径没有 persona 渲染的提问，
                # 存档里留空串（含义与 provenance 空 dict 相同）。
                rec = await orch.run(args.task_id, title)
            records.append(rec)
            path = archive.write(rec, task_id=args.task_id, batch_id=args.task_id)
            _print_summary(rec, path)
            # 增量三十六：L6 人工触发器——run 路径末尾自动派发。
            # 与人工 cli review --write 共用同源判据（REVIEW_BRANCHES），
            # 但落到独立 jsonl（review_auto/l6_dispatch.jsonl）。
            _dispatch_l6_summary(rec, args.task_id)

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

    kind = getattr(args, "driver", DEFAULT_KIND)
    client = None
    if kind == "obscura":
        try:
            client = McpClient.from_env()
        except RuntimeError as exc:
            print(f"[FAIL] {exc}")
            return 2

    # 执行器模式在**构造期**定下（``executor.mode`` 的纪律）：手工路径
    # 与计划路径在这里是同一套，换数据来源不该换执行链。解析先于
    # RunConfig 构造——模式的产地是 RunConfig（A3-1 转正）。
    mode, agent_backend, banner = _resolve_executor(args.executor_mode)
    if not mode:
        print(banner)
        return 2
    cfg = RunConfig(
        engine=args.engine,
        max_candidates=args.max_candidates,
        max_chars=args.max_chars,
        stop_after_success=args.stop_after_success,
        discovery_path=args.discovery_path,
        max_intermediates=args.max_intermediates,
        max_referral_links=args.max_referral_links,
        executor_mode=mode,
        # 单站超时（2026-10-22 起暴露到 CLI）。**不传就用源码默认值**——
        # 落进 run_config 是 B-4 唯一的时限判据，不落盘等于没有判据。
        per_site_timeout_s=_per_site_timeout(args),
    )
    archive = P1Archive(Path(args.out) if args.out else None)
    records = []
    writer, why = _make_rationale_writer()
    print(f"rationale = {_describe_rationale(writer, why)}")
    print(banner)

    async with AsyncExitStack() as batch:
        shared = None
        if client is not None:
            client = await batch.enter_async_context(client)
            print(f"server = {client.info.name} {client.info.version} "
                  f"({client.info.tool_count} tools)")
            # obscura：整批一条连接复用（行为与 P5 前一致）
            # **kind 必须显式写 obscura**：这条分支只在 ``kind == "obscura"``
            # 时才走到（client 是 McpClient），而 DEFAULT_KIND 已是 camoufox
            # ——不写就会拿 obscura 的连接去造 camoufox 的驱动，**当场抛
            # ValueError**（"camoufox 实现需要 CamoufoxSession，未提供"）。
            # ⚠️ **手工路径与计划路径是两处独立调用点**，改一处漏一处
            # ⇒ 手工路径（``run --task-id``）不炸、计划路径（``run --plan``）
            # 炸，而后者才是批量生产入口。**两处都要写。**
            shared = make_driver(
                client,
                kind="obscura",
                collect_element_basis=getattr(args, "element_basis", False),
            )
        print(f"driver = {kind}")
        for n, task in enumerate(tasks, 1):
            # 上面的检查已保证 title 非空，这里直接取
            title = task.title
            # 每 task 一个：判断点 ① 要目标片名（构造注入，见 _make_perceptor）
            # camoufox 面一个 Run 一次 launch（_driver_for_run）。
            async with _driver_for_run(
                kind, shared=shared,
                headless=not getattr(args, "headful", False),
                collect_element_basis=getattr(args, "element_basis", False),
            ) as driver:
                orch = Orchestrator(driver, _make_perceptor(args, title), cfg,
                                    rationale_writer=writer,
                                    agent_backend=agent_backend)
                print(f"\n[{n}/{len(tasks)}] {task.task_id} | {task.persona_id} "
                      f"| {title}")
                rec = await orch.run(
                    task.task_id, title,
                    search_query=task.search_query,
                    user_prompt=task.prompt_text,
                    provenance=dict(task.provenance),
                )
            records.append(rec)
            path = archive.write(rec, task_id=task.task_id, batch_id=args.task_id)
            _print_summary(rec, path)
            # 增量三十六：L6 自动派发——与 _run_manual 同口径。
            _dispatch_l6_summary(rec, task.task_id)

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
    print("    PLAYER_OK        — **判不出来就 None**（不凭标签存在性判成功，"
          "2026-10-23 · M-26）")
    print("                        <video>/<audio> 标签数与 iframe 数都**不作判据**：")
    print("                        前者会把付费墙页读成「能看正片」，"
          "后者会把导航站读成播放页（hao123 满屏 iframe）")
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
        print("    付费墙门禁语境 — 「有播放页但要付费」不算成功（④ 代码层先判）")
        print("    ref 白名单校验 — 模型回传的 ref 必须真的在观察里")
        print(f"    置信度先验 = {CONFIDENCE_JUDGED}（未校准，**不是模型自报**）")
        print("    ⚠️ 媒体计数不作判据 — <video> 存在性不许许诺成功"
              "（2026-10-23 · M-26）")

    print(f"\n  失败分支全集（{len(questions.all_branches())} 条）: "
          f"{sorted(questions.all_branches())}")
    print(f"  不计入负样本的分支: {sorted({'unresolved', 'trailer_suspect'})}")

    print("\n== 隐私红线 ==")
    print(f"  {EXE_ENV_VAR} 未设置" if not _env_ok() else f"  {EXE_ENV_VAR} 已设置")
    print(f"  禁用的 tool（{len(FORBIDDEN_TOOLS)}）: {sorted(FORBIDDEN_TOOLS)}")

    if args.driver == "camoufox":
        return await _check_camoufox(
            args.probe_url,
            action_smoke=getattr(args, "action_smoke", False),
            headless=not getattr(args, "headful", False),
            collect_element_basis=getattr(args, "element_basis", False),
        )

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
        driver = make_driver(
            client,
            kind="obscura",
            collect_element_basis=getattr(args, "element_basis", False),
        )
        await driver.goto(args.probe_url)
        obs = await driver.observe(max_chars=DEFAULT_MAX_CHARS)
        print(f"  探针页 {obs.url}: body={len(obs.body_text)}ch "
              f"source={obs.body_source} degraded={list(obs.degraded)}")
        if obs.element_basis is not None:
            _print_element_basis(obs.element_basis, obs.element_basis_error)
        elif getattr(args, "element_basis", False):
            print("  元素账目: [FAIL] 开了 --element-basis 却没采到"
                  "（见上面的 degraded）")
    return 0


def _print_element_basis(basis: Mapping[str, Any],
                         error: str | None = None) -> None:
    """打印两本账（shared / face）。

    **两本账平级打**，不合成一个数——合成就是「让两面可比」的失败版。

    ⚠️ **缺键与「空账」不是一回事**（2026-10-21）：共享口径没测到时
    存档里**缺** ``shared`` 键，而它的原因记在 ``element_basis_error`` 里。
    打印时把它显式说出来——否则「shared 缺了」会被读成「这一档是 0」。
    """
    print("  元素账目（跨面可比口径）:")
    for side in ("shared", "face"):
        block = dict(basis.get(side, {}) or {})
        if not block:
            if side == "shared" and error:
                print(f"    {side:<7} [FAIL] 没测到——{error}")
            else:
                print(f"    {side:<7} （本面给不出这一档）")
            continue
        print(f"    {side:<7} " + "  ".join(f"{k}={v}" for k, v in block.items()))
    if error:
        print(f"    ⚠️ 共享口径没测到：{error}")


async def _check_camoufox(probe_url: str, *, action_smoke: bool = False,
                          headless: bool = True,
                          collect_element_basis: bool = False) -> int:
    """``check --driver camoufox``：起一次会话，打印**实测指纹**并跑一遍采集面原语。

    为什么不并进上面那段：obscura 的探针是「MCP 连得上吗 + 采得到吗」，
    camoufox 没有 MCP 连接这一层，探的是**另两件事**——
    ①身份/几何有没有被钉住（没钉住则批次不可归因，方案 §4.3）；
    ②采集面原语在真实站点上通不通（``snapshot``/``count``/``interactive``/
      ``links``），**并且**元素枚举的账目读得出来
      （P3-α：``interactive`` 的过滤丢弃是静默的，只能靠账目看）。

    ``headless`` 缺省**无头**（与生产口径一致，2026-10-21）——所以这里打印的
    指纹读数就是**无人值守批次真正的指纹**。要看有头口径的读数加 ``--headful``。

    失败返回 1（obscura 段不可达时返回 0）——obscura 那段不可达是「这台机器
    没配」的常态，camoufox 是**本仓库自己的依赖**，起不来就是环境坏了。
    """
    print("\n== camoufox 会话 ==")
    from trajectory_pipeline.executor.browser.camoufox_session import CamoufoxSession

    try:
        session = await CamoufoxSession.launch(headless=headless)
    except Exception as exc:                     # noqa: BLE001
        # launch 可能抛任何东西（playwright 启动失败 / 浏览器二进制缺失 /
        # screen 约束不被接受），这里只报类型与消息，不分类——分类等于猜。
        print(f"  [FAIL] launch 失败 — {type(exc).__name__}: {exc}")
        return 1

    async with session:
        print("  指纹（批次元数据口径；os/geoip/locale/humanize 是请求值，"
              "其余是实测值）:")
        for k, v in session.fingerprint.items():
            print(f"    {k:<10} = {v}")
        print("  现场读数（诊断用，不进批次元数据）:")
        for k, v in session.readings.items():
            print(f"    {k:<10} = {v}")

        driver = make_driver(kind="camoufox", camoufox_session=session,
                             collect_element_basis=collect_element_basis)
        try:
            await driver.goto(probe_url)
            snap = await driver.snapshot(max_chars=DEFAULT_MAX_CHARS)
            media = await driver.count("video, audio")
            iframes = await driver.count("iframe")
            elements = await driver.interactive()
        except DriverError as exc:
            print(f"  [FAIL] {exc}")
            return 1
        print(f"\n  探针页 {snap.url}")
        print(f"    title={snap.title!r} body={len(snap.body)}ch "
              f"source={snap.body_source} truncated={snap.truncated} "
              f"video/audio={media} iframe={iframes}")

        # 采集面（P3-α）：把过滤账目和元素原表都打出来。**账目要看**——
        # 「枚举出 0 个」在元素表上空着，与「这页没有可点目标」同形；
        # 账目能指出是过滤掉的还是真没有。
        stats = getattr(driver, "last_enumeration", {})
        print(f"\n  可交互元素 {len(elements)} 个（tag 列是 ARIA role）：")
        for el in elements:
            print(f"    {el.ref:<4} {el.role:<10} {el.name}")
        if stats:
            print("    过滤账目: " + "  ".join(f"{k}={v}" for k, v in stats.items()))

        # 跨面可比口径（可选）：两本账打出来，好和 obscura 面**同一段 JS**
        # 的读数直接并排比。
        if collect_element_basis:
            try:
                shared = await driver.element_basis()
            except DriverError as exc:
                # 共享口径坏了**不等于这次检查失败**——它是**测量仪器**坏了，
                # 不是页面不可信。face 账目照旧打出来（它不依赖共享口径）。
                print(f"    共享口径: [FAIL] {exc}")
                _print_element_basis({"face": dict(stats)}, str(exc))
            else:
                _print_element_basis({"shared": shared, "face": dict(stats)})

        # 采集面剩余原语（P3-β）：只报**条数**，不在这里 dump 内容——
        # 逐条核对走 `probe_observe --driver camoufox`（它把整份 Observation
        # 落盘并跑契约断言），这里只回答「接线通不通」。
        try:
            links = await driver.links()
        except DriverError as exc:
            print(f"    链接: [FAIL] {exc}")
        else:
            print(f"    链接: {len(links)} 条（前 3 条）")
            for link in links[:3]:
                print(f"      {link.text[:40]!r} → {link.href[:80]}")

        if action_smoke:
            await _camoufox_action_smoke(driver, elements)
    return 0


async def _camoufox_action_smoke(driver: Any, elements: Any) -> None:
    """``--action-smoke``：真填一次、真点一次，打印结果账目。

    为什么要在 CLI 里留这一条而不是只写单测：单测用的是假 page，
    它证明「我们调了哪几个 API」，**不证明那些 API 在真实页面上能跑**。
    P2 的验收判据是「动作的 camoufox 调用可原样执行」——那条只能在真浏览器上量。

    目标一律取自**当前页的枚举结果**（不写死选择器）：要验的正是
    「枚举出来的 role+name 能被动作面定位」这条闭环。**顺序有意义**：
    先填词 + 回车（可能跳转到结果页），最后才点击（点击会离开本页）。
    """
    print("\n  动作面冒烟:")
    before = await driver.snapshot(max_chars=1)

    # ① fill + press：站内搜索的真实形态。回车带目标（压在搜索框上），
    #    不走页面级按键——后者会把「搜索框没找到」变成「在随便哪里敲回车」。
    box = next((e for e in elements if e.role in ("textbox", "searchbox")), None)
    if box is None:
        print("    [SKIP] fill/press：本页没有枚举出 textbox/searchbox")
    else:
        print(f"    fill {box.role} {box.name!r} ← '冒烟测试'")
        try:
            await driver.fill(role=box.role, name=box.name, text="冒烟测试")
            print(f"      {driver.last_action}")
            await driver.press("Enter", role=box.role, name=box.name)
            print(f"      {driver.last_action}")
        except DriverError as exc:
            print(f"    [FAIL] fill/press: {exc}")

    # ② 「不存在」那一态：页面上不可能有的名字，必须静默返回 ``not_found``
    #    而不是抛——这是假负样本防线，每次冒烟都读一遍。
    await driver.click(role="button", name="__trajectory_no_such_button__")
    print(f"    click 不存在目标 → {driver.last_action}（必须是 not_found）")

    # ③ 真点一次（放最后：点了可能导航走）。
    #
    # ⚠️ 先看页面换没换。**回车提交会导航**，而 `role+name` 是**页面级**定位：
    # 拿上一页枚举出来的名字去点新页，如果新页恰好有同名元素，会**静默点中
    # 那个不该点的元素**（不像 ref 那样必然失效报错）。所以导航后必须重新枚举。
    mid = await driver.snapshot(max_chars=1)
    if mid.url != before.url:
        elements = await driver.interactive()
        print(f"    页面已变（{mid.url[:60]}…）→ 重新枚举 {len(elements)} 个元素后再点")
    picked = next((e for e in elements if e.role in ("link", "button")), None)
    if picked is None:
        print("    [SKIP] click：本页没有枚举出 link/button")
        return
    print(f"    click {picked.role} {picked.name!r}")
    picked_url = mid.url  # 点击**前**的 URL：后面判断「跳没跳」只能拿它当基线
    try:
        await driver.click(role=picked.role, name=picked.name)
    except DriverError as exc:
        print(f"    [FAIL] click: {exc}")
        return
    print(f"      {driver.last_action}")
    after, reads, moved = await _after_action_snapshot(driver, clicked_from=picked_url)
    tail = "" if moved else "  ⚠️ URL 未变（点击没有导航，或跳转比 2s 还慢）"
    print(f"    页面: 点击前 {picked_url} → 点击后 {after.url}（读 {reads} 次）{tail}")
    empty = "  ⚠️ 2s 内没读到 title（可能是过渡态，也可能这页本来没有 <title>）" if not after.title else ""
    print(f"    标题: 点击后 {after.title!r}{empty}")


async def _after_action_snapshot(
    driver: Any, *, clicked_from: str, attempts: int = 8, delay: float = 0.25
) -> tuple[Any, int, bool]:
    """动作之后读观察：**先等 URL 真的变，再等新页读稳**，返回 ``(观察, 读了几次, 变没变)``。

    为什么不能直接 ``snapshot()`` 一次（2026-10-20 冒烟踩到，三个版本三种错法）：

    1. **读到的是整个旧页**：``click()`` 返回时不保证导航已经开始，旧页在导航前
       **天然稳定**——所以「连续两次读数一致就算稳」在这里**恒真**（那版报告
       ``URL = /s?…``，0.5s 后它其实跳到了首页）。
    2. **读到过渡态**：导航已开始但新文档还没 commit，``title`` 是空串
       （那版打出 ``标题: '百度一下，你就知道' → ''``，页面上明明有标题）。
    3. **过渡态本身也可能是稳定的**：空标题能连读两次一致——只要求「两次一致」
       仍会把过渡态交出去。所以第二阶段还要**等 title 读得出来**。

    于是两段式：①等 URL 相对动作前**变化**（导航开始的唯一硬信号）；
    ②等 ``(url, title)`` 一致**且** title 非空。

    「title 非空」只是「新文档 commit 了」的**廉价代理**，不是判据——
    真有页面没有 ``<title>``，所以预算耗尽仍返回最后一次读数（``moved`` 照实），
    由调用方打出「标题为空」的提示。**不猜、不编标题、不反复等下去。**

    这条对整条链的影响不止冒烟：动作之后立刻 ``interactive()`` 会枚举到**旧页**，
    于是下一步拿着旧页的名字去动作——症状见[02-避坑指南 §8.8](../docs/02-避坑指南.md)。
    """
    last = await driver.snapshot(max_chars=1)
    reads = 1
    moved = last.url != clicked_from
    for _ in range(attempts - 1):
        await asyncio.sleep(delay)
        cur = await driver.snapshot(max_chars=1)
        reads += 1
        if not moved:
            # 第一阶段：等导航**开始**（URL 变化）。旧页在导航前是稳定态，
            # 「两次读数一致」在这里没有意义——那正是上面第 1 条踩的坑。
            moved = cur.url != clicked_from
            last = cur
            continue
        # 第二阶段：导航已开始，还要等到 title 读得出来且连续两次一致
        if cur.title and (cur.url, cur.title) == (last.url, last.title):
            return cur, reads, True
        last = cur
    return last, reads, moved


def _env_ok() -> bool:
    import os

    return bool(os.environ.get(EXE_ENV_VAR, "").strip())


# ══════════════════════════════════════════════════════════════════════
# 取证探针（联网）
# ══════════════════════════════════════════════════════════════════════
#
# 为什么不写成一次性脚本
# ----------------------
# 存档里两边的证据互相矛盾时，**唯一能分清的是现场**。这个动作在
# 2026-10-10 一天里发生了三次（youku id_XNDE / id_XMTQ / bilibili），
# 每次都是手写一个一次性脚本——而其中一次因为「一半看存档、一半看现场」
# 判错了一笔（两个优酷 URL 强度不一致）。
#
# **混用两套取证强度是最容易犯也最难发现的错**：两笔都写进报告，
# 笔迹一样，只有回看代码才知道哪笔没开浏览器。所以这个动作必须是
# 一个**只有一个模式**的入口：要么全现场，要么都不现场。

#: 正文里出现即「**这一项与站点性质有关**」的词。刻意只列这类，
#: 不列「免费观看」——它在导航栏里的频率远高于付费提示
#: （实测优酷导航栏首词就是「续费」）。
_SITATION_MARKS = ("完整", "正片", "未删减", "无删减", "全片", "高清",
                   "会员", "开通", "续费", "立即播放", "播放")


async def _probe_one(driver: Any, url: str, *,
                     preview: int, max_chars: int | None) -> None:
    """取一次现场事实并打印。**只报事实，不下结论。**

    不下结论是关键：这个命令的用途就是给两边的争议**摆事实**，
    它一旦开始输出「所以这是付费墙」，就把取证变成了表态——
    而表态会顺着人的倾向走（当初 id_XMTQ 就是只看存档就判了付费墙，
    补了现场才发现判对了，但**那次是运气**）。

    驱动由调用方给（:func:`_probe_driver`）——**一条命令一条会话**，
    不每个 URL 重开浏览器（camoufox 每次 launch 要秒级）。
    """
    try:
        await driver.goto(url)
        obs = await driver.observe(max_chars=max_chars)
    except Exception as exc:
        print(f"  [FAIL] {type(exc).__name__}: {exc}")
        return
    finally:
        await driver.close_tab()

    body = obs.body_text or ""
    print(f"  落地 URL   : {obs.url}")
    print(f"  页面标题   : {obs.page_title}")
    print(f"  video={obs.video_tag_count}  iframe={obs.iframe_count}  "
          f"元素={len(obs.interactive_elements)}  正文={len(body)}字  "
          f"降级={list(obs.degraded)}")

    from trajectory_pipeline.perception.rule_perceptor import (
        classify_paywall, is_play_control,
    )

    pw = classify_paywall(body)
    print(f"  付费墙词表 : 「{pw or '（未命中）'}」")
    hits = [e.name for e in obs.interactive_elements if is_play_control(e.name)]
    print(f"  播放控件词表命中 {len(hits)} 个: {hits[:8]}")
    marks = [w for w in _SITATION_MARKS if w in body]
    print(f"  正文出现   : {marks}")
    if preview > 0:
        print(f"  正文前 {preview}: {body[:preview]!r}")
    # 「采集失败」与「页面真没有」在存档里同形，现场能分开：
    # 三项全空 + 有降级项 = 采集失败；元素 0 但正文长 = 页面确实这样。
    if obs.degraded and not obs.interactive_elements and not body.strip():
        print(f"  ⚠ 三项全空且有降级项 —— 这是**采集失败**，"
              f"不能据此判「页面没有播放控件」")


async def cmd_probe(args: argparse.Namespace) -> int:
    urls: list[str] = list(args.url or [])
    if args.from_archive:
        urls += _urls_of(args.from_archive, limit=args.limit)
    # 去重但**保持命令行给的顺序**：取证是一次性动作，
    # 顺序颠倒会让「先看的那笔」被后面的覆盖掉。
    urls = list(dict.fromkeys(urls))
    if not urls:
        print("没给 URL：传位置参数，或用 --from-archive 读存档里的",
              file=sys.stderr)
        return 2

    print(f"== 取证探针 · {len(urls)} 个 URL（全部走现场，"
          f"不读存档证据）==")
    kind = getattr(args, "driver", DEFAULT_KIND)
    # obscura 缺件**先查**（``RuntimeError`` 若包住整个循环，会把批内单站的
    # ``RuntimeError`` 吞成「整批失败」——分不清「没配好」与「这一站炸了」）。
    if kind == "obscura":
        try:
            McpClient.from_env()
        except RuntimeError as exc:
            print(f"  [FAIL] {exc}", file=sys.stderr)
            return 2

    async with _probe_driver(
            kind, headless=not getattr(args, "headful", False)) as driver:
        print(f"  driver  = {driver.name}")
        for url in urls:
            print(f"\n── {url}")
            await _probe_one(driver, url, preview=args.preview,
                             max_chars=DEFAULT_MAX_CHARS)
    print("\n⚠ 上面是**事实**，不是结论。贴标签仍需人工裁定"
          "（review --verdicts ... --apply）")
    return 0


async def cmd_proberun(args: argparse.Namespace) -> int:
    """P1 验证工具：直接对一组 URL 跑完整 visit，**绕过搜索引擎**。

    2026-10-12 增量十三 → 阶段 1 验证工具。每 URL 走一遍完整控制流
    （IS_REACHABLE → FIND_PLAY_CONTROL → PLAYER_OK 触发 decision_branch），
    落 P1 存档，便于对照 ``decision_branch`` 对四个新分支
    （``login_required`` / ``blocked`` / ``broken`` / ``partial_free``）的真实覆盖。

    ⚠️ **生产链上不走这条路径**——W1 主链仍走 :func:`cli run` →
    ``orchestrator.run`` → 搜索引擎 → search_step.extract_candidates → visit。
    本命令仅供离线验证新分支触发条件；请勿把 ``proberun`` 的产物混入
    训练批次（它们的 ``provenance.source = "proberun"`` 是标记）。

    与 ``probe`` 不同点：``probe`` 只看 driver 现场事实、不下结论；
    ``proberun`` 走完 visit 整链（perceptor → decision_branch → record），
    落 20 活跃分支。这是「取证」与「判定」的本质区别，不要混用。
    """
    if not args.from_urls:
        print("[FAIL] --from-urls <path> 是必填（每行一个 URL）", file=sys.stderr)
        return 2
    try:
        urls = _load_url_lines(args.from_urls)
    except (FileNotFoundError, ValueError) as exc:
        print(f"[FAIL] {exc}", file=sys.stderr)
        return 2
    if not args.title:
        print("[FAIL] --title 是必填（visit 的目标片名，构造注入给 perceptor）",
              file=sys.stderr)
        return 2

    kind = getattr(args, "driver", DEFAULT_KIND)

    cfg = RunConfig(engine=args.engine, max_chars=args.max_chars)
    # 写进 ``output/pipeline/proberun/`` ——验证工具的产物**不**混进生产
    # 归档（``output/pipeline/*__*.json``）被训练批次 glob 收走（见
    # :class:`P1Archive` ``subdir`` 注释）。
    archive = P1Archive(Path(args.out) if args.out else None, subdir="proberun")
    perceptor = _make_perceptor(args, args.title)
    pname = getattr(perceptor, "name", "?")

    print(f"== proberun：{len(urls)} 个 URL，每 URL 一个独立 mini-run ==")
    print(f"  perceptor = {pname}")
    print(f"  task_id   = {args.task_id}")
    print(f"  title     = {args.title!r}")

    # obscura 缺件**先查**：``McpClient.from_env`` 抛 ``RuntimeError``，
    # 而那个异常若包住整个循环就会把批内单站的 ``RuntimeError`` 也吞成
    # 「整批失败」——分不清「没配好」与「这一站炸了」。缺件即报错，不猜。
    if kind == "obscura":
        try:
            McpClient.from_env()
        except RuntimeError as exc:
            print(f"[FAIL] {exc}")
            return 2

    async with _probe_driver(
            kind, headless=not getattr(args, "headful", False)) as driver:
        print(f"  driver    = {driver.name}")
        from trajectory_pipeline.executor.steps.search import Candidate
        for n, url in enumerate(urls, 1):
            # 用 URL 自身作 run_id 后缀（hex 化），同 task 多 URL 不冲突
            run_id = uuid.uuid4().hex[:8]
            ledger = RunLedger(task_id=args.task_id)
            record = RunRecord(
                task_id=args.task_id,
                title=args.title,
                query="<proberun>",
                search_url="<proberun>",
                user_prompt="",
                ledger=ledger,
                perceptor=pname,
                provenance={"source": "proberun", "url": url},
                run_config=dataclasses.asdict(cfg),
                # **执行层必须记**：工具面（``{tag,label}`` vs ``{role,name}``）
                # 由它折算。不记 ⇒ 新批次会被当成 obscura-1 面渲染，
                # 而真实调用是另一种编码——「声明与真实调用对不上」，
                # 报告上看不出异常。
                driver=driver.name,
                fingerprint=dict(getattr(driver, "fingerprint", {}) or {}),
            )
            candidate = Candidate(url=url, text=url, rank=0, source_href=url)
            record.candidates = [candidate]
            record.candidate_source = "direct"

            print(f"\n[{n}/{len(urls)}] {url[:90]}")
            try:
                result = await visit_site(
                    driver, perceptor, ledger, candidate,
                    max_chars=cfg.max_chars,
                )
                record.visits.append(result)
            except Exception as exc:  # 兜底：单站炸不该毁整批
                record.warnings.append(
                    f"visit 异常: {type(exc).__name__}: {exc}"
                )

            record.ledger = ledger
            path = archive.write(record, task_id=args.task_id, run_id=run_id, batch_id=args.task_id)
            # 末段分支分布
            branch_count: dict[str, int] = {}
            for o in ledger.outcomes:
                key = o.branch if o.branch else "success"
                branch_count[key] = branch_count.get(key, 0) + 1
            print(f"  branches = {json.dumps(branch_count, ensure_ascii=False)}")
            print(f"  → {path}")
    return 0


async def cmd_staticprobe(args: argparse.Namespace) -> int:
    """L0 静态可达性探测（HTTP 层，无浏览器）—— 阶段 4 第一站。

    2026-10-12 增量十八配套。绕开搜索引擎与浏览器，对一组 URL 跑
    HTTP 层可达性探测（:func:`trajectory_pipeline.executor.probe.probe`），
    落 JSON 到 ``output/pipeline/staticprobe/`` 子目录（不被训练批次
    glob 收走）。

    与 :func:`cmd_proberun` 的差异：proberun 走 visit 整链（perceptor
    → decision_branch → record），下 20 活跃分支结论；staticprobe 只
    跑 HTTP 层探测，**不下分支结论**——由 ``to_branch()`` 在归档后
    单独映射（只产 ``broken`` / ``blocked`` / ``None``，与阶段 1 的
    分支口径一致——阶段 1 当时 13 活跃分支，2026-10-16 后为 20 支）。

    ⚠️ **生产链上不走这条路径**——这是离线验证工具，与 proberun
    同款，产物进 ``staticprobe/`` 子目录隔离。

    关键边界（设计文档 §6）：
    - **不复用 :mod:`executor.steps.reachability`**——后者刻意不用
      HTTP 状态码；L0 是新通道。
    - **网络层异常不归任何分支**——``unreachable_hard`` 由 DriverError
      触发，留给 reachability。
    - **L0 HTTP 404/5xx 初筛只产 ``broken``**，不产 ``unreachable_hard``。
    """
    if not args.from_urls:
        print("[FAIL] --from-urls <path> 是必填（每行一个 URL）",
              file=sys.stderr)
        return 2
    try:
        urls = _load_url_lines(args.from_urls)
    except (FileNotFoundError, ValueError) as exc:
        print(f"[FAIL] {exc}", file=sys.stderr)
        return 2
    if not args.title:
        print("[FAIL] --title 是必填（任务名留痕 + 归档前缀；不参与探测逻辑）",
              file=sys.stderr)
        return 2

    archive = P1Archive(Path(args.out) if args.out else None,
                        subdir="staticprobe")

    print(f"== staticprobe：{len(urls)} 个 URL，HTTP 层可达性探测（无浏览器）==")
    print(f"  task_id = {args.task_id}")
    print(f"  timeout = {args.timeout}s")

    branch_count: Counter[str] = Counter()
    status_count: Counter[str] = Counter()
    leak_count = 0

    for n, url in enumerate(urls, 1):
        run_id = uuid.uuid4().hex[:8]
        print(f"\n[{n}/{len(urls)}] {url[:90]}")
        try:
            result = await probe(url, timeout_s=args.timeout)
        except Exception as exc:                              # noqa: BLE001
            # 探测函数自身已对 httpx 异常分门别类；这里兜的是「不该出
            # 现的意外」——单站炸不该毁整批。
            print(f"  [FAIL] probe 抛异常：{type(exc).__name__}: {exc}",
                  file=sys.stderr)
            continue

        branch = to_branch(result)
        branch_count[branch or "passes"] += 1
        status_count[result.landing_status] += 1

        print(f"  status_code = {result.status_code}")
        print(f"  landing     = {result.landing_status}")
        print(f"  elapsed_ms  = {result.elapsed_ms}")
        print(f"  redirects   = {result.redirect_count}")
        print(f"  branch      = {branch or '(passes to next layer)'}")
        if result.block_reason:
            print(f"  block_reason= {result.block_reason}")
        if result.error:
            print(f"  error       = {result.error}")

        path = archive.path_for(args.task_id, run_id)
        try:
            written = dump_result(result, path)
            print(f"  → {written}")
        except CredentialLeak as exc:
            leak_count += 1
            print(f"  [FAIL] {exc}", file=sys.stderr)

    print(f"\n== 汇总 ==")
    print(f"  branches = {dict(branch_count)}")
    print(f"  landing  = {dict(status_count)}")
    if leak_count:
        print(f"  ⚠️ {leak_count}/{len(urls)} 因凭据命中被拒写盘（fail-closed）")
    return 0


async def cmd_mediaprobe(args: argparse.Namespace) -> int:
    """L1 媒体解析（HTTP 拉页 + ffprobe 直连，无浏览器）—— 阶段 4 第二站。

    2026-10-13 增量二十二配套。绕开搜索引擎与浏览器，对一组 URL 跑
    HTTP 层拉页 + ffprobe 直连媒体（:func:`trajectory_pipeline.executor.probe.probe_media`），
    落 JSON 到 ``output/pipeline/mediaprobe/`` 子目录（不被训练批次
    glob 收走），与 L0 ``staticprobe`` 同款。

    与 :func:`cmd_staticprobe` 的差异：L0 只跑 HTTP GET 探测可访问性；
    L1 在来源页 200 后再走 ``HTML 解析 media/iframe`` + ``iframe 1 跳`` +
    ``ffprobe 直连每条 media``，**下分支结论**（由 ``to_media_branch()``
    映射：``broken`` / ``partial_free`` / ``None``）。

    ⚠️ **生产链上不走这条路径**——这是离线验证工具，与 ``proberun`` 同款，
    产物进 ``mediaprobe/`` 子目录隔离。

    关键边界（设计文档 §6 / 增量二十二）：

    - **ffprobe 未装 → fail-closed**——与 ``OBSCURA_EXE`` 同款纪律：缺即报错，
      不拿「子进程不可用」猜「媒体不存在」。
    - **iframe 1 跳解析**——``MAX_IFRAME_HOPS = 1``，超过即停。
    - **凭据闸门复用 :func:`scan_credentials`**——iframe 拉回的 HTML / media URL
      可能含 cookie / token，**不能绕过**（与 L0 同款）。
    - **L1 与 L0 不重叠**——L0 在 HTTP 层已报 ``broken`` / ``blocked``；到 L1 时
      来源页已经 200，L1 只产 ``broken``（媒体坏）与 ``partial_free``（试看）。
    """
    if not args.from_urls:
        print("[FAIL] --from-urls <path> 是必填（每行一个 URL）",
              file=sys.stderr)
        return 2
    try:
        urls = _load_url_lines(args.from_urls)
    except (FileNotFoundError, ValueError) as exc:
        print(f"[FAIL] {exc}", file=sys.stderr)
        return 2
    if not args.title:
        print("[FAIL] --title 是必填（任务名留痕 + 归档前缀；不参与探测逻辑）",
              file=sys.stderr)
        return 2

    if not ffprobe_available(bin_name=FFPROBE_BIN):
        print(
            f"[FAIL] ffprobe 未在本机 PATH 中找到（bin_name={FFPROBE_BIN}）。"
            f"L1 媒体解析需要 ffprobe；安装并重试。"
            f"也可换用别的二进制名（需改源码默认）——本 CLI 与 ``OBSCURA_EXE`` "
            f"同款纪律：缺件即报错，不猜。",
            file=sys.stderr,
        )
        return 3

    archive = P1Archive(Path(args.out) if args.out else None,
                        subdir="mediaprobe")

    print(f"== mediaprobe：{len(urls)} 个 URL，HTTP 拉页 + ffprobe 直连（无浏览器）==")
    print(f"  task_id = {args.task_id}")
    print(f"  timeout = {args.timeout}s")
    print(f"  ffprobe = {FFPROBE_BIN} (PARTIAL_FREE_MAX_DURATION_S={PARTIAL_FREE_MAX_DURATION_S})")

    branch_count: Counter[str] = Counter()
    media_count_total = 0
    leak_count = 0

    for n, url in enumerate(urls, 1):
        run_id = uuid.uuid4().hex[:8]
        print(f"\n[{n}/{len(urls)}] {url[:90]}")
        try:
            result = await probe_media(
                url, timeout_s=args.timeout,
                runner=run_ffprobe_subprocess,
            )
        except Exception as exc:                              # noqa: BLE001
            # 探测函数自身已对 httpx / ffprobe 异常分门别类；
            # 这里兜的是「不该出现的意外」——单站炸不该毁整批。
            print(f"  [FAIL] probe 抛异常：{type(exc).__name__}: {exc}",
                  file=sys.stderr)
            continue

        branch = to_media_branch(result)
        branch_count[branch or "passes"] += 1
        media_count_total += len(result.media_urls)

        print(f"  media_urls    = {len(result.media_urls)}")
        print(f"  iframe_urls   = {len(result.iframe_urls)}")
        print(f"  probed        = {len(result.probed)} "
              f"(ok={sum(1 for p in result.probed if p.ok)})")
        print(f"  elapsed_ms    = {result.elapsed_ms}")
        print(f"  branch        = {branch or '(passes to next layer)'}")
        if result.probed:
            short = [p for p in result.probed if p.ok and p.duration_s <= PARTIAL_FREE_MAX_DURATION_S]
            if short:
                print(f"  short_media   = {len(short)} (≤{PARTIAL_FREE_MAX_DURATION_S}s)")
            broken = [p for p in result.probed if not p.ok]
            if broken:
                print(f"  failed_probed = {len(broken)} (ffprobe failed)")

        path = archive.path_for(args.task_id, run_id)
        try:
            written = dump_media_result(result, path)
            print(f"  → {written}")
        except CredentialLeak as exc:
            leak_count += 1
            print(f"  [FAIL] {exc}", file=sys.stderr)

    print(f"\n== 汇总 ==")
    print(f"  branches = {dict(branch_count)}")
    print(f"  media_url_total = {media_count_total}")
    if leak_count:
        print(f"  ⚠️ {leak_count}/{len(urls)} 因凭据命中被拒写盘（fail-closed）")
    return 0





# cmd_mediabrowse 函数本体（带前后各 2 空行）—— 供 inserter 拼接。
# 中文以 UTF-8 落盘；中文 / emoji 不会破坏这里因为我们走 Path.write_text。
async def cmd_mediabrowse(args: argparse.Namespace) -> int:
    """L2 浏览器媒体探针（JS-aware，依赖 PageDriver 协议）—— 阶段 4 第三站。

    2026-10-13 增量二十三配套。绕开搜索引擎；在 PageDriver 协议上跑
    probe_browser_media 拿 <video> / <audio> 真实 playback 状态
    （currentTime / readyState / videoWidth / error code 等），
    落 JSON 到 output/pipeline/mediabrowse/ 子目录（不被训练批次
    glob 收走）。

    与 cmd_mediaprobe 的差异：L1 在 HTTP 层 + ffprobe 直连媒体文件，
    **拿不到真实 playback 时序**——它只能看 mp4 头部 & metadata；
    L2 用浏览器 JS 拿两次 currentTime 采样比对，确认是否真推进、
    readyState / videoWidth 是否正常、media error.code 是什么。
    partial_free 的判定在 L2 这层才算完整（DOM 真实状态）。

    关键边界（设计文档 §6 / 增量二十三）：
    - **依赖 PageDriver 协议**——不直连 obscura，留协议层替换余地。
    - **默认 fake driver**——CLI 不打开真浏览器，只验契约与
      to_branch 映射；KPI 验证 partial_free 形态必须接 obscura
      （L2 与 L0/L1 的本质区别：没真浏览器时 L2 只能探 mock）。
    - **凭据闸门复用 scan_credentials**——同 L1。
    - **L2 与 L0/L1 不重叠**——L2 只能新增「JS-aware 黑屏」与
      「试看形态二次确认」。L0 已判 broken/blocked；L1 已判媒体坏。
    """
    if not args.from_urls:
        print("[FAIL] --from-urls <path> 是必填（每行一个 URL）",
              file=sys.stderr)
        return 2
    if not args.title:
        print("[FAIL] --title 是必填（任务名留痕 + 归档前缀；不参与探测逻辑）",
              file=sys.stderr)
        return 2

    driver_choice = getattr(args, "driver", "fake")
    if driver_choice == "fake":
        return await _mediabrowse_with_driver(args, FakePageDriver())
    if driver_choice == "obscura":
        # 真实驱动：复用一条 MCP 连接跑整批
        # （ObscuraDriver docstring 要求：多站遍历应复用连接 + tab 切换，
        # 不是每步重连——避免 50MB V8 isolate × N 的浪费）
        try:
            client = McpClient.from_env()
        except RuntimeError as exc:
            print(f"[FAIL] {exc}", file=sys.stderr)
            return 3
        try:
            async with client:
                driver_obj = make_driver(client, kind="obscura")
                return await _mediabrowse_with_driver(args, driver_obj)
        except McpError as exc:
            print(f"[FAIL] obscura MCP 连接失败: {exc}", file=sys.stderr)
            return 3
    if driver_choice == "camoufox":
        # **生产面**（2026-10-22 owner 裁定后）。L2 要在**真实生产面**上
        # 采两次 ``currentTime`` 判「真推进」——探针跑的浏览器不是生产面，
        # 结论就不能外推（与「工具声明按面取」同一条纪律）。
        try:
            async with _probe_driver(
                    "camoufox",
                    headless=not getattr(args, "headful", False)) as driver_obj:
                return await _mediabrowse_with_driver(args, driver_obj)
        except RuntimeError as exc:
            print(f"[FAIL] {exc}", file=sys.stderr)
            return 3
    print(
        f"[FAIL] --driver 取值 {driver_choice!r} 不在 "
        f"('fake', 'obscura', 'camoufox') 内",
        file=sys.stderr,
    )
    return 2


_BODY_INNER_TEXT_JS: str = "document.body.innerText"
_BODY_EXCERPT_MAX_CHARS: int = 1200
_SNAPSHOT_EXCERPT_MAX_CHARS: int = 600


async def _collect_l4_text(
    driver: PageDriver,
    *,
    timeout_s: float,
) -> tuple[str, str]:
    """L4 文本采集——鸭子调用 driver.evaluate(innerText) / driver.snapshot()。

    Returns:
        (body_excerpt, snapshot_excerpt) 元组。任何一项失败 → 空字符串。
        不抛异常——L4 文本降级不能毁整批（L5 仲裁走 MediaSignal 兜底）。

    关键边界：
    - 真实 ObscuraDriver.snapshot() 返 CleanedSnapshot（带 title / body），
      fake 通过同名方法返字符串。鸭子兼容靠 str() 化即可。
    - body_excerpt 取 innerText 前 1200 字符——长文 / 列表页会截断但
      不影响关键词命中（前几句总是最权威的页面引文）。
    """
    body_excerpt = ""
    snapshot_excerpt = ""
    try:
        raw = await asyncio.wait_for(
            driver.evaluate(_BODY_INNER_TEXT_JS), timeout=timeout_s
        )
        if isinstance(raw, str):
            body_excerpt = raw[:_BODY_EXCERPT_MAX_CHARS]
    except (asyncio.TimeoutError, DriverError, Exception):  # noqa: BLE001
        pass

    try:
        snap = await asyncio.wait_for(
            driver.snapshot(max_chars=_SNAPSHOT_EXCERPT_MAX_CHARS),
            timeout=timeout_s,
        )
        snapshot_excerpt = str(snap)[:_SNAPSHOT_EXCERPT_MAX_CHARS]
    except (asyncio.TimeoutError, DriverError, Exception):  # noqa: BLE001
        pass

    return body_excerpt, snapshot_excerpt


async def _run_l4_review_async(
    driver: PageDriver,
    result: BrowserProbeResult,
    page_quotes: tuple[str, ...] = (),
    *,
    timeout_s: float = 8.0,
    reviewer: VLMReviewer | None = None,
    reviewer_name: str = "heuristic",
) -> tuple[VLMReviewResult, str, str]:
    """L4 VLM 复核 async 入口（cli 主循环直接 await）。

    Returns:
        ``(VLMReviewResult, body_excerpt, snapshot_excerpt)`` 三元组。
        主循环用 ``body_excerpt / snapshot_excerpt`` 写回 ``BrowserProbeResult``，
        让 dump JSON 落盘时能看到 L4 实际消费的页面正文（记 3）。

        失败 sentinel: ``note="vlm_error"``, ``label="unknown"``,
        ``paywall_type="none"``, ``confidence=0.0``，附空字符串 body/snapshot。

    .. note::
        增量三十四（2026-10-13）：``reviewer_name`` 关键字参数引入，
        默认 ``"heuristic"``，与历史行为完全一致；显式 ``"multim"``
        时走 :class:`MultimVLMReviewer`（无 key 自动降级到启发式）。
        ``reviewer`` 形参保留——测试 / 上层可直接注入 mock。
    """
    if reviewer is None:
        reviewer = build_reviewer(reviewer_name)

    body = ""
    snapshot = ""
    try:
        body, snapshot = await _collect_l4_text(driver, timeout_s=timeout_s)
    except Exception as exc:  # noqa: BLE001
        return _vlm_error_sentinel(
            f"text collect fail: {type(exc).__name__}: {exc}"
        ), body, snapshot

    inp = VLMReviewInput(
        signal=result.signal,
        body_excerpt=body,
        snapshot_excerpt=snapshot,
        page_quotes=page_quotes,
        final_url=result.final_url,
    )
    try:
        return reviewer.review(inp), body, snapshot
    except Exception as exc:  # noqa: BLE001
        return _vlm_error_sentinel(
            f"reviewer fail: {type(exc).__name__}: {exc}"
        ), body, snapshot


def _vlm_error_sentinel(reason: str) -> VLMReviewResult:
    """L4 reviewer 失败时的 sentinel result——fail-soft 不外抛。

    L5 仲裁看到 note="vlm_error" + confidence=0.0 时降权（与 L3 rule
    命中冲突时不强行仲裁），让人工复核队列兜底。
    """
    return VLMReviewResult(
        label="unknown",
        paywall_type="none",
        evidence=f"VLM 复核失败: {reason[:120]}",
        confidence=0.0,
        note="vlm_error",
        source="heuristic",
    )


async def _mediabrowse_with_driver(args: argparse.Namespace,
                                   driver_obj: PageDriver) -> int:
    """L2 mediabrowse 主循环（driver 生命周期由 cmd_mediabrowse 负责）。

    cmd_mediabrowse 拆分后：driver 选择 + 连接管理在顶层，主循环放这里。
    fake / obscura 两条路径都走同一份循环（探针 + 凭据闸门 + 落盘）。

    2026-10-13 增量二十五：每 URL probe 之后串 :func:`_run_l4_review`
    把 VLMReviewResult 写到 ``result.vlm_review`` 字段；文本源由
    :func:`_collect_l4_text` 鸭子采集，driver 不支持 .snapshot() /
    .evaluate(innerText) 时降级为空字符串。
    """
    try:
        urls = _load_url_lines(args.from_urls)
    except (FileNotFoundError, ValueError) as exc:
        print(f"[FAIL] {exc}", file=sys.stderr)
        return 2

    archive = P1Archive(Path(args.out) if args.out else None,
                        subdir="mediabrowse")

    print(f"== mediabrowse：{len(urls)} 个 URL，L2 JS-aware 浏览器探针 ==")
    print(f"  task_id = {args.task_id}")
    print(f"  timeout = {args.timeout}s")
    print(f"  wait    = {args.wait_s}s (PROBE_WAIT_S={PROBE_WAIT_S})")
    print(f"  driver  = {driver_obj.name} (PARTIAL_FREE_MAX_CURRENT_TIME_S="
          f"{PARTIAL_FREE_MAX_CURRENT_TIME_S})")

    branch_count = Counter()
    leak_count = 0

    for n, url in enumerate(urls, 1):
        run_id = uuid.uuid4().hex[:8]
        print(f"\n[{n}/{len(urls)}] {url[:90]}")
        try:
            result = await probe_browser_media(
                driver_obj, url,
                timeout_s=args.timeout,
                wait_s=args.wait_s,
            )
        except Exception as exc:                              # noqa: BLE001
            # 探测函数自身已对 asyncio / driver 异常分门别类；
            # 这里兜的是「不该出现的意外」——单站炸不该毁整批。
            print(f"  [FAIL] probe 抛异常：{type(exc).__name__}: {exc}",
                  file=sys.stderr)
            continue

        branch = to_browser_branch(result)
        branch_count[branch or "passes"] += 1

        # 增量二十八：L1 ffprobe 真接入——L2 之后、L4 之前注入。
        # 与 L4 / L5 同款 fail-soft：异常不毁整批；credential / driver 短路跳过。
        # L1 不封装浏览器，只跑 HTTP GET 来源页 + ffprobe 子进程。
        # 触发条件：--no-l1 关闭 / ffprobe_available() 失败 / fail-closed → 跳过。
        l1_probe_result = None
        if getattr(args, "no_l1", False):
            # 用户显式关 L1——保持 l1_probe=None。
            pass
        elif result.note in ("credential_leak", "driver_missing", "timeout", "driver_error"):
            # fail-closed 路径：L2 已经短路，凭据闸门语义不再污染。
            pass
        elif not ffprobe_available():
            # 缺 ffprobe 二进制——降级跳过，不阻塞主链。
            pass
        else:
            try:
                _l1_timeout = float(getattr(args, "l1_timeout", 10.0))
                l1_probe_result = await probe_media(
                    url, timeout_s=_l1_timeout,
                    runner=run_ffprobe_subprocess,
                )
                _l1_branch = to_media_branch(l1_probe_result)
                print(f"  l1_branch      = {_l1_branch or '(passes to next layer)'}")
                print(f"  l1_media_urls  = {len(l1_probe_result.media_urls)}")
                print(f"  l1_probed_ok   = {sum(1 for p in l1_probe_result.probed if p.ok)}"
                      f"/{len(l1_probe_result.probed)}")
                print(f"  l1_elapsed_ms  = {l1_probe_result.elapsed_ms}")
            except Exception as exc:  # noqa: BLE001
                print(f"  [FAIL] L1 ffprobe 异常：{type(exc).__name__}: {exc}",
                      file=sys.stderr)
                l1_probe_result = None
        result = dataclasses.replace(result, l1_probe=l1_probe_result)

        # 增量二十五：L4 VLM 复核（每 URL probe 完成后跑）—— fail-soft，
        # 不让 reviewer 错误毁整批；vlm_review 失败时返 sentinel（note=vlm_error）
        # 让 dump 仍能写盘，仲裁层看到 note="vlm_error" 降权处理。
        # 注意：credential_leak / driver_missing / timeout / driver_error
        # 这些 fail-closed result 没真实 DOM——不跑 L4（避免污染凭据闸门
        # fail-closed 语义：test_credential_url_blocks_before_driver 守的就是这点）。
        if result.note in ("credential_leak", "driver_missing", "timeout", "driver_error"):
            # fail-closed 路径：不调 driver，不污染凭据闸门 fail-closed 语义。
            vlm_review = _vlm_error_sentinel(f"skipped: probe note={result.note}")
            l4_body = ""
            l4_snapshot = ""
        else:
            try:
                vlm_review, l4_body, l4_snapshot = await _run_l4_review_async(
                    driver_obj, result,
                    page_quotes=(),
                    timeout_s=getattr(args, "l4_timeout", 8.0),
                    reviewer_name=getattr(args, "reviewer", "heuristic"),
                )
            except Exception as exc:  # noqa: BLE001
                # _run_l4_review_async 自身不抛——这里兜 async 协议外的死循环 / OOM。
                print(f"  [FAIL] L4 复核抛异常：{type(exc).__name__}: {exc}",
                      file=sys.stderr)
                vlm_review = _vlm_error_sentinel(
                    f"outer try: {type(exc).__name__}: {exc}"
                )
                l4_body = ""
                l4_snapshot = ""
        # frozen dataclass 不可变 → dataclasses.replace 出新实例。
        # 增量二十五：L4 采集的 body/snapshot 文本也一并写回 result——
        # 让 dump JSON 落盘时能看到 L4 实际消费的页面正文（记 1）。
        result = dataclasses.replace(
            result,
            vlm_review=vlm_review,
            body_excerpt=l4_body,
            snapshot_excerpt=l4_snapshot,
        )

        # 增量二十六：L3 规则层独立判定（在 L4 之后、L5 之前；触发条件同 L4 fail-closed）。
        # L3 不调 LLM，直接对 body_text 做全文关键词扫描，产出 access_kind / paywall_type。
        # L5 仲裁层会消费 result.l3_decision + result.vlm_review 做一致/冲突判定。
        l3_decision = None
        if result.note in ("credential_leak", "driver_missing", "timeout", "driver_error"):
            # fail-closed：没有真实 DOM，L3 无依据可读。
            l3_decision = None
        else:
            try:
                _l3_obs = Observation(
                    url=result.final_url or result.requested_url,
                    page_title="",
                    body_text=l4_body,  # L4 已采集的 body；snapshot 不参与 L3
                )
                _l3 = RulePerceptor()
                l3_decision = _l3.decide_business_signal(_l3_obs)
                # 如果 L3 识别到 paywall，再问 paywall_type 子问题（细粒度定档）。
                if l3_decision.payload.get("access_kind") == "paywall":
                    l3_paywall_dec = _l3.decide_paywall_type(_l3_obs)
                    # paywall_type 合并到 payload；保留 business.signal 的 kind/confidence。
                    merged_payload = dict(l3_decision.payload)
                    merged_payload.update(l3_paywall_dec.payload)
                    l3_decision = dataclasses.replace(
                        l3_decision,
                        payload=MappingProxyType(merged_payload),
                    )
            except Exception as exc:  # noqa: BLE001
                # L3 同步扫描，不应抛错；任何异常都降级为 None（fail-closed 同 L4）。
                print(f"  [FAIL] L3 规则层异常：{type(exc).__name__}: {exc}",
                      file=sys.stderr)
                l3_decision = None
        result = dataclasses.replace(result, l3_decision=l3_decision)

        # 增量二十万：L5 仲裁层注入\uff08L3 之后001L5 仲裁层输出 SiteOutcome\uff09。
        l5_outcome = None
        try:
            l5_outcome = arbitrate_l5(result)
        except Exception as exc:  # noqa: BLE001
            # L5 应在 L3 有输出时仍能运行\uff0c仅作最后 catch\uff1bfail-closed \u2192 None
            print(f"  [FAIL] L5 仲裁层异常：{type(exc).__name__}: {exc}", file=sys.stderr)
            l5_outcome = None
        result = dataclasses.replace(result, l5_outcome=l5_outcome)

        sig = result.signal
        print(f"  driver        = {result.driver_name}")
        print(f"  media_count   = {result.media_count}")
        print(f"  play_started  = {sig.play_started}")
        print(f"  progress      = {sig.timeupdate_progress} "
              f"(current_time={sig.current_time_sec}s)")
        print(f"  frames_decoded= {sig.frames_decoded}")
        print(f"  black_screen  = {sig.black_screen}")
        print(f"  media_error   = {sig.media_error}")
        print(f"  elapsed_ms    = {result.elapsed_ms}")
        print(f"  branch        = {branch or '(passes to next layer)'}")
        if result.note:
            print(f"  note          = {result.note}")
        if result.error:
            print(f"  error         = {result.error}")

        # 增量二十五：L4 VLM 复核结果打印——3 行主体。
        rv = vlm_review
        print(f"  vlm_label     = {rv.label}")
        print(f"  vlm_paywall   = {rv.paywall_type}")
        print(f"  vlm_confidence= {rv.confidence}")
        if rv.price is not None:
            print(f"  vlm_price     = {rv.price} {rv.currency or ''}")
        if rv.note:
            print(f"  vlm_note      = {rv.note}")
        # evidence 单独打一行（避免与其他字段同行太长）
        print(f"  vlm_evidence  = {rv.evidence[:80]}")

        # 增量二十六：L3 规则层判定结果 4 行打印
        l3d = result.l3_decision
        if l3d is None:
            print("  l3_kind       = (skipped)")
            print("  l3_paywall_type = (n/a)")
        else:
            l3p = l3d.payload
            l3_kind = l3p.get('access_kind', '(unknown)')
            l3_ptype = l3p.get('paywall_type', '(none)')
            l3_hit = l3p.get("hit_keyword", "") or ""
            print(f"  l3_kind       = {l3_kind}")
            print(f"  l3_paywall_type = {l3_ptype}")
            print(f"  l3_confidence = {l3d.confidence}")
            print(f"  l3_hit        = {l3_hit[:60]!r}")

        path = archive.path_for(args.task_id, run_id)
        try:
            written = dump_browser_result(result, path)
            print(f"  → {written}")
        except CredentialLeak as exc:
            leak_count += 1
            print(f"  [FAIL] {exc}", file=sys.stderr)
            continue
        # 增量三十六：L6 人工触发器——mediabrowse 路径末尾自动派发。
        # 判据与 run 路径同源：REVIEW_BRANCHES（unresolved /
        # trailer_suspect / player_unverified）+ AUTO_REVIEW_NOTES
        # （vlm_error / credential_leak / timeout / driver_missing /
        # driver_error）双轨触发；详见 review_queue.extract_from_browser_result。
        _dispatch_l6_summary(result, args.task_id)

    print(f"\n== 汇总 ==")
    print(f"  branches = {dict(branch_count)}")
    if leak_count:
        print(f"  ⚠️ {leak_count}/{len(urls)} 因凭据命中被拒写盘（fail-closed）")
    return 0


class FakePageDriver:
    """L2 mediabrowse 默认 in-process mock PageDriver。

    2026-10-13 增量二十三配套。L2 :func:`probe_browser_media` 只需 3 个原语：
    ``name`` / ``goto`` / ``evaluate``。``evaluate`` 返回 ``{"count": 0, "media": []}``
    → :func:`_parse_media_payload` 出空 :class:`MediaSignal` → ``media_count=0``
    → :func:`to_browser_branch` 返 ``None``（passes）。

    设计动机：CLI 默认不开真浏览器——KPI 验证 partial_free 形态必须接
    obscura driver 才能跑（这是 L2 与 L0/L1 的本质区别：**没真浏览器时
    L2 只能探 mock，无法验 partial_free**）。

    关键边界：
    - **仅实现 L2 探针用到的 3 原语**——其他原子原语（snapshot / links /
      interactive / click 等）靠鸭子类型按需调用，L2 探针不触达。
    - **不抛异常**——与真 obscura 协议的「失败抛 DriverError」不同，
      fake 默认走 happy path；要测错误分支请走单测。
    """

    name: str = "fake"

    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []
        # 增量二十五：L4 文本输入预设（test 驱动 fake body/snapshot 文本）。
        self._fake_body_text: str = ""
        self._fake_snapshot_text: str = ""

    def set_body_text(self, text: str) -> None:
        """预设置 fake body innerText（让 :meth:`evaluate` 命中时返文本）。"""
        self._fake_body_text = text

    def set_snapshot_text(self, text: str) -> None:
        """预设置 fake snapshot 文本（同上）。"""
        self._fake_snapshot_text = text

    async def goto(self, url: str, *, wait_until: str = "load") -> None:
        self.calls.append(("goto", url))

    async def evaluate(self, expression: str) -> Mapping[str, Any]:
        # L4 文本采集：识别 `document.body.innerText` → 返回字符串。
        # 注意：协议返回类型实际是 ``Any``——L4 文本采集需要返回
        # 字符串，鸭子类型兼容 obscura 真实驱动（同样返字符串）。
        if expression.strip().startswith("document.body.innerText"):
            self.calls.append(("evaluate:body", expression[:32]))
            return self._fake_body_text

        # L2 探针只看 ``{count, media: [...]}`` 形态；返回空集让 ``to_branch``
        # 走 ``None``（passes），验证 fake driver 的 happy-path 契约。
        self.calls.append(("evaluate", expression[:32]))
        return {"count": 0, "media": []}

    async def snapshot(
        self, *, max_chars: int | None = None, degraded: list[str] | None = None
    ) -> str:
        # L4 文本采集：snapshot 返字符串（同 evaluate("document.body.innerText")）。
        # 真实 ObscuraDriver.snapshot() 返 CleanedSnapshot（带 title / body）；
        # fake 简化——只预设 body / snapshot 文本。
        self.calls.append(("snapshot", str(max_chars)))
        if degraded is not None:
            degraded.append("fake_snapshot")
        return self._fake_snapshot_text or self._fake_body_text

    async def close(self) -> None:
        self.calls.clear()


def _load_url_lines(path: str) -> list[str]:
    """读 URL 文件——每行一条；``#`` 开头视为注释。

    失败的姿势：空文件抛 :class:`ValueError`，文件缺失抛
    :class:`FileNotFoundError`——两者都让 ``cmd_proberun`` 显式 fail
    不要「静默通过、跑 0 条」。空跑 0 条时终端上「proberun 完成」与
    「proberun 没东西可跑」长得一模一样，而后者经常是 URL 文件路径
    拼错的产物。
    """
    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(f"--from-urls 文件 {p} 不存在")
    out: list[str] = []
    for raw in p.read_text(encoding="utf-8").splitlines():
        s = raw.strip()
        if not s or s.startswith("#"):
            continue
        out.append(s)
    if not out:
        raise ValueError(f"--from-urls 文件 {p} 无 URL（除 # 注释外）")
    return out


#: L6 自动派发的末尾打印格式。**只一行汇总**，不逐条打印（生产链
#: 长跑时逐条 = 刷屏）。失败原因写在 ``dispatch_l6`` 内部，已经
#: fail-soft 返回 ``written=0, skipped=0``——这种情况不打空行，避免
#: 误读成「派发成功 0 条」。
_L6_SUMMARY: Final = "  L6 dispatch: +{written} 条（去重跳过 {skipped}）"


def _dispatch_l6_summary(item: Any, task_id: str) -> None:
    """把 :func:`review_queue.dispatch_l6` 包装成「一行打印 + 异常吞掉」。

    L6 派发是**派发管线**而不是**生产管线**：派发失败不该让整条 CLI
    崩掉（落 P1 已写完，那才是核心资产）。失败在控制台冒一行提示就
    够了——与 ``check-archives`` 的「**跳过**必须看得见」同源纪律，
    跳过 N 条总比「静默吞掉」强。
    """
    try:
        written, skipped = dispatch_l6(item, task_id=task_id)
    except Exception as exc:                              # noqa: BLE001
        print(f"  L6 dispatch fail: {type(exc).__name__}: {exc}",
              file=sys.stderr)
        return
    if written or skipped:
        print(_L6_SUMMARY.format(written=written, skipped=skipped))


def _urls_of(archive: str, *, limit: int | None) -> list[str]:
    """从 P1 存档里取待复核的 URL。

    存在的意义是**不用手打 URL**：当初那个一次性脚本把 3 个 URL
    硬编码在源码里，抄错一个字符就探到别的页面，而报告上完全看不出来。
    """
    try:
        items = collect_reviews(Path(archive))
    except Exception as exc:
        print(f"  [SKIP] 读 {archive} 失败：{type(exc).__name__}: {exc}",
              file=sys.stderr)
        return []
    urls = [i.landed_url or i.url for i in items if (i.landed_url or i.url)]
    return urls[:limit] if limit else urls


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
                print(f"    元素  : {[element_name(e) for e in it.elements[:6]]}")
    return 0


def cmd_downgrade(args: argparse.Namespace) -> int:
    """把**判定不成立**的 outcome 降为 ``unresolved``（取证后的处置）。

    ⚠️ **默认 dry-run。** 降级改的是结论，而 ``purge-run`` 之外没有
    反向操作——先看清单，确认了再 ``--apply``。

    判据与动作是分开的：**动作**在 :mod:`downgrade`（证据戳、ledger
    记账、``visits[].success`` 同步），**判据**在命令行。这么分是因为
    三批数据三种判据而处置动作只有一份——2026-10-10 那三个一次性脚本
    各自实现了一遍处置，于是其中一份的匹配条件一放宽就多降了 8 条。
    """
    root = Path(args.out) if args.out else DEFAULT_ROOT
    patterns: list[re.Pattern[str]] = []
    for raw in args.evidence or ():
        try:
            patterns.append(re.compile(raw))
        except re.error as exc:
            print(f"[stop] --evidence 正则不合法 {raw!r}：{exc}", file=sys.stderr)
            return 2
    if args.url and patterns:
        print("[stop] --url 与 --evidence **不要同时给**：两个条件是 OR 还是 "
              "AND 取决于实现，而这种含糊正是那 8 条误伤的入口。"
              "\n       要按 URL 挑就只用 --url；要按判定正文挑就只用 "
              "--evidence（它已足够精确）。", file=sys.stderr)
        return 2
    if not args.url and not patterns:
        print("[stop] 得给 --url 或 --evidence 之一", file=sys.stderr)
        return 2
    # ⚠️ **必须在碰盘之前校验**。理由必填这条原先落在
    # ``apply_downgrades`` 里，于是它要**先把整个存档目录走一遍**、
    # 撞上第一条命中才抛——而抛出来是裸 traceback，看起来像崩溃，
    # 实际上只是一次参数写漏。参数校验全在这一段里做完。
    if not (args.reason or "").strip():
        print("[stop] 降级必须给 --reason：它是这条日后唯一的依据",
              file=sys.stderr)
        return 2

    select = (downgrade_mod.select_by_url(*args.url) if args.url
              else downgrade_mod.select_by_evidence(
                  patterns, branches=args.branch or None))
    summaries = downgrade_mod.apply_to_root(
        root, select=select, reason=args.reason or "", dry_run=not args.apply)

    if not summaries:
        print("没有匹配的 outcome —— 归档可能已被降过（幂等），或判据不匹配")
        return 0
    total = sum(s["downgraded"] for s in summaries)
    verb = "降级" if args.apply else "**将**降级"
    print(f"== {verb} {total} 条 outcome → "
          f"{downgrade_mod.TARGET_BRANCH}（{len(summaries)} 份存档）==")
    for s in summaries:
        branches = " ".join(f"{k}×{v}" for k, v in sorted(s["branches"].items()))
        extra = (f" 同步 visits[].success {s['visits']} 处"
                 if s.get("visits") else "")
        print(f"  {s['archive']:32s} {s['downgraded']:>3d} 条  {branches}{extra}")
    if not args.apply:
        print("\n⚠ 这是 dry-run。加 --apply 才落盘。")
        print("  顺序：本命令 → review --verdicts ... --apply"
              "（复核档是重新合并出来的，不受本命令影响）")
    else:
        print("\n⚠ 只动了原档。复核档要重跑 review --apply 才会同步——"
              "在本命令里改它是**白改**，下一次合并就被覆盖回去。")
    return 0


def cmd_relabel(args: argparse.Namespace) -> int:
    """把「标签错了但仍是负样本」的记录**改分支**（取证后的处置）。

    ⚠️ **默认 dry-run。** 改判改的是**训练资产**——负样本池是跨 run
    累积的 append-only 资产，池没有反向操作（``purge-run`` 是按 run
    回滚，不是按标签回滚）。先看清单，确认了再 ``--apply``。

    与相邻两条处置路径的分工（互不重叠）：

    * ``cli downgrade``：判定**不成立** → 落 ``unresolved``（**离开**池）；
    * ``cli review --apply``：只对 ``REVIEW_BRANCHES``（反思档）生效——
      「从没判过 → 人判」；
    * **本命令**：判定**成立**但**贴错了支** → 换到正确的负分支
      （**留在**池里）。

    处置动作在 :mod:`relabel`（存档与**池**一起改、ledger 记账），
    **判据在命令行**——与 ``cli downgrade`` 同一分工。
    """
    root = Path(args.out) if args.out else DEFAULT_ROOT
    patterns: list[re.Pattern[str]] = []
    for raw in args.evidence or ():
        try:
            patterns.append(re.compile(raw))
        except re.error as exc:
            print(f"[stop] --evidence 正则不合法 {raw!r}：{exc}", file=sys.stderr)
            return 2
    # ⚠️ **必须在碰盘之前校验**（与 cli downgrade 同款）：理由必填这条
    # 若落在模块里，它要先把整个目录走一遍、撞上第一条命中才抛——
    # 而抛出来是裸 traceback，看起来像崩溃，实际只是一次参数写漏。
    if not (args.reason or "").strip():
        print("[stop] 改判必须给 --reason：它是这条日后唯一的依据",
              file=sys.stderr)
        return 2
    ok, why = relabel_mod.check_target(args.to)
    if not ok:
        print(f"[stop] {why}", file=sys.stderr)
        return 2
    if not (args.url or args.branch or patterns):
        print("[stop] 得给 --url / --branch / --evidence 之一"
              "（不给就是全选——那会改判池里的每一条）", file=sys.stderr)
        return 2

    # 判据组合：**同类 OR、异类 AND**（写死在 help 里，不靠实现去猜）。
    sels: list[Callable[[relabel_mod.Candidate], bool]] = []
    if args.url:
        sels.append(relabel_mod.select_by_url(*args.url))
    if args.branch:
        sels.append(relabel_mod.select_by_branch(*args.branch))
    if patterns:
        sels.append(relabel_mod.select_by_evidence(patterns))

    def _select(cand: relabel_mod.Candidate) -> bool:
        return all(s(cand) for s in sels)

    try:
        summary = relabel_mod.relabel_to_root(
            root, select=_select, to_branch=args.to,
            reason=args.reason or "", dry_run=not args.apply)
    except relabel_mod.RelabelError as exc:
        print(f"[stop] {exc}", file=sys.stderr)
        return 2

    if not summary["relabelled"] and not summary["rejected"]:
        print("没有匹配的记录 —— 判据不匹配，或已改判过（幂等）")
        return 0
    verb = "改判" if args.apply else "**将**改判"
    print(f"== {verb} {summary['relabelled']} 条记录 → "
          f"{summary['to_branch']}（{summary['outcomes']} 条 outcome、"
          f"{summary['pool_rows']} 行池、{len(summary['archives'])} 份存档）==")
    for name, n in sorted(summary["archives"].items()):
        print(f"  {name:38s} {n:>3d} 条")
    if summary["skipped"]:
        print(f"  （另有 {summary['skipped']} 条已是目标分支，跳过）")
    if summary["merged"]:
        print(f"\n⚠ {summary['merged']} 条**并入已有行**：池里已经有同一 "
              f"(task_id, url) 的 {summary['to_branch']} 标签，")
        print("  本次**删掉**贴错标签的那一行，不新建行（同键只能有一行）。")
    if summary["rejected"]:
        print(f"\n⚠ {len(summary['rejected'])} 条**被拒**（未改）：")
        for what, why in summary["rejected"]:
            print(f"  {what}\n      {why}")
    if not args.apply:
        print("\n⚠ 这是 dry-run。加 --apply 才落盘。")
        print("  落盘时**存档与负样本池一起改**——只改一边会让门禁报"
              "「没并池」。")
    else:
        print("\n⚠ 复核档（`.reviewed.json`）若存在也已同步；"
              "下一次 review --apply 会从**原档**重新合并——"
              "原档也改了，所以改判不会被覆盖回去。")
    return 0


def cmd_pool_drop(args: argparse.Namespace) -> int:
    """**撤掉负样本池里的行**（**默认 dry-run**）——按 URL 逐条定。

    什么时候用：某一行池记录的**结论不成立**（门禁的
    ``negative_contradicted_by_success`` 会指出来），而**同 run 里
    其他行是对的** ⇒ 整批回滚会误伤。

    ⚠️ **它只动池，不动存档**——存档侧的结论由 ``cli downgrade`` /
    ``cli relabel`` 改。顺序永远是**先改存档、后撤池行**：反过来的话，
    撤池时存档还写着旧结论 ⇒ 撤掉的那行在存档里还有支撑，看着像撤错了。

    ⚠️ **撤掉的行进 ``pool_dropped.jsonl`` 审计日志**（原行 + 理由 +
    时间）——池是 append-only 资产，撤行是唯一的删除动作，必须留痕。
    """
    root = Path(args.out) if args.out else DEFAULT_ROOT
    if not (args.reason or "").strip():
        print("[stop] 撤池行必须给 --reason：它是这条日后唯一的依据",
              file=sys.stderr)
        return 2
    if not (args.url or args.task_id or args.branch):
        print("[stop] 撤池行必须给判据（--url / --task-id / --branch）"
              "——不给就是全选，而池是 append-only 资产，全选等于清空训练集",
              file=sys.stderr)
        return 2
    try:
        out = pool_drop_mod.drop_from_root(
            root, url_fragments=tuple(args.url or ()),
            task_id=args.task_id, branch=args.branch,
            reason=args.reason or "", dry_run=not args.apply)
    except ValueError as exc:
        print(f"[stop] {exc}", file=sys.stderr)
        return 2

    rows = out["selected"]
    if not rows:
        print("没有匹配的池行 —— 判据不匹配，或已撤过（幂等）")
        return 0
    verb = "撤掉" if args.apply else "**将**撤掉"
    print(f"== {verb} {len(rows)} 行池记录 ==")
    for row in rows:
        print(f"  [{row.get('task_id')}] {row.get('branch')}  "
              f"{str(row.get('url'))[:76]}")
        print(f"      {str(row.get('evidence'))[:110]}")
    if not args.apply:
        print("\n⚠ 这是 dry-run。加 --apply 才落盘。")
        print("  顺序：**先改存档（cli downgrade）→ 再撤池行**。")
    else:
        print(f"\n已撤 {out['dropped']} 行。审计日志：{out['log']}")
        print("接着跑：cli build --overwrite  →  cli check-archives")
    return 0


def cmd_review_queue(args: argparse.Namespace) -> int:
    """改**复核队列文件**本身（预填 / 收敛），不动 P1。

    ⚠️ **为什么不并进 ``review``**：``review --apply`` 会写
    ``.reviewed.json``，那是**基于人的判断**、不可回退的动作；
    而预填与收敛只动一份还没提交裁定的工作稿。把两类动作放同一个
    子命令下，误加一个 ``--apply`` 就把「按形态预填」变成
    「把自动判定提交成人工结论」——而后者记的是 ``auto:`` 来源，
    **冒充人看过证据**。
    """
    if not (args.prefill or args.settle):
        print("没指定动作：用 --prefill 和/或 --settle", file=sys.stderr)
        return 2
    path = Path(args.queue)
    if not path.exists():
        print(f"{path} 不存在——先 review --write 导出队列", file=sys.stderr)
        return 2

    rows = [json.loads(line) for line in
            path.read_text(encoding="utf-8").splitlines() if line.strip()]

    if args.prefill:
        stats = rq_prefill(rows)
        print(f"预填 {path}")
        for k, n in stats.items():
            if n:
                print(f"    {k:24s} {n}")
        left = sum(1 for r in rows if not str(r.get("verdict") or "").strip())
        print(f"    → 仍留空 {left} 条（有元素，证据够，留给人看）")

    if args.settle:
        n = rq_settle(rows)
        print(f"同 URL 裁定收敛：改动 {n} 行"
              f"（「没证据」档让位于结论档）")

    # 原子替换。**先写同目录的 .tmp 再 replace**：直接就地改时，
    # 中途崩了会留下半截 JSON，而下次读它的人看不出是崩了还是格式坏了。
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as fh:
        for row in rows:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")
    os.replace(tmp, path)
    print(f"已写回 {path}")
    print("  提交裁定：review --verdicts <queue> --apply")
    return 0


def _load_verdicts(path: str | Path) -> tuple[dict[str, dict[str, Any]],
                                              list[tuple[str, set[str]]]]:
    """读人工裁定，返回 ``({url: 裁定}, 冲突列表)``。

    ⚠️ **同 URL 的多条裁定必须一致，否则当场中止**。

    队列是**逐存档导出的**，而同一个站点会在多份存档里各出现一次——
    实测 106 行只对应 **21 个唯一 URL**（爱奇艺那一个 URL 就占 20 行）。
    而裁定是按 URL 应用的,于是 ``dict[key] = ...`` 天然是**后者覆盖前者**：
    复核员在第 2 行认真裁了 ``confirm_success``，第 40 行没填或随手填了别的，
    他的判断就被**静默吃掉**，而回填照常成功、一行报错都没有。

    这正是 CLAUDE.md「分歧必须暴露给人看」要挡的那类静默合并。
    取「最后一个」不是策略——它只是文件顺序的副产物。

    顺带把未知裁定在这里就挡掉：:func:`apply_verdicts` 虽然也会抛，
    但那是**逐存档**抛的，抛之前已经有别的存档写完 ``.reviewed.json`` 了，
    留下一半新一半旧。**先全查一遍再动任何文件**。
    """
    verdicts: dict[str, dict[str, Any]] = {}
    seen: dict[str, set[str]] = {}
    bad: list[tuple[str, str]] = []
    with Path(path).open("r", encoding="utf-8") as fh:
        for lineno, line in enumerate(fh, 1):
            line = line.strip()
            if not line:
                continue
            row = json.loads(line)
            v = str(row.get("verdict") or "").strip()
            if not v:
                continue
            key = str(row.get("landed_url") or row.get("url") or "")
            if v not in VERDICTS:
                bad.append((key, v))
                continue
            seen.setdefault(key, set()).add(v)
            verdicts[key] = {"verdict": v,
                             "reviewed_by": str(row.get("reviewed_by") or ""),
                             "note": str(row.get("note") or "")}
    if bad:
        raise ReviewQueueError(
            f"第 {','.join(str(b[0]) for b in bad[:3])}… 行等处出现未知裁定 "
            f"{sorted({b[1] for b in bad})}；可选：{sorted(VERDICTS)}"
        )
    conflicts = [(k, s) for k, s in seen.items() if len(s) > 1]
    return verdicts, conflicts


def cmd_apply_review(args: argparse.Namespace) -> int:
    """把人工裁定回填进 P1 存档。

    **写新文件，不原地改**（``<name>.reviewed.json``）。原因与
    :mod:`trajectory_pipeline.executor.archive` 的原子写同源：
    回填是基于一份判断，可能整批作废；覆盖原档就没有回头路了。
    """
    root = Path(args.out) if args.out else DEFAULT_ROOT
    verdicts, conflicts = _load_verdicts(args.verdicts)
    if conflicts:
        print(f"[stop] 同一 URL 出现了**互相冲突**的人工裁定 {len(conflicts)} 处。"
              f"\n       下面这些必须先定成同一个值——一个站点只有一个结论：")
        for key, seen in conflicts:
            print(f"         {key}\n           取值：{sorted(seen)}")
        print("\n       已中止，**未写任何 .reviewed.json**。")
        return 2

    total = dict.fromkeys(["confirm_success", "confirm_negative",
                           "confirm_not_play_site", "confirm_paywall",
                           "trailer", "skipped", "unreviewed", "ignored",
                           "off_question", "branch_written"], 0)
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
    if total["off_question"]:
        # **这一段必须先说**：裁定数 ≠ 写进分支的数。跨判断点的裁定没写
        # branch，只落在 site_verdict 上。不先讲清楚，后面那句
        # 「人工确认的正样本 N 条」会被读成「N 条成了成功样本」。
        print(f"  ⚠ {total['off_question']} 条裁定**跨判断点**，未写 branch，"
              f"结论落在 site_verdict 上。\n"
              f"    人工给的是**站点级**事实，outcome 是**决策单元级**的；"
              f"硬贴会造出决策树（rubric D-5）里不存在的组合，"
              f"实测 15 条因此被 D-5 判成一票否决、门禁 Test同源 当场红。")
    if total["confirm_success"]:
        print(f"  → 人工确认「这站能看」{total['confirm_success']} 条，"
              f"其中 **{total['branch_written']} 条写成了成功样本**"
              f"（source=human，branch_before_review 保留了代码原判）"
              f"，其余降级为 site_verdict")
    if total["confirm_not_play_site"] or total["confirm_paywall"]:
        print(f"  → 人工判定「不是播放站」{total['confirm_not_play_site']} 条、"
              f"「需订阅/付费」{total['confirm_paywall']} 条")
    if any(total.get(k) for k in (
            "confirm_login", "confirm_blocked", "confirm_broken",
            "confirm_partial_free")):
        print(f"  → 精细裁定「登录墙/阻断/损坏/部分免费」"
              f"{total.get('confirm_login', 0)} / "
              f"{total.get('confirm_blocked', 0)} / "
              f"{total.get('confirm_broken', 0)} / "
              f"{total.get('confirm_partial_free', 0)} 条")
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


def cmd_compare_drivers(args: argparse.Namespace) -> int:
    """双跑对账（设计方案 04 §6.1）。**只读存档，不开浏览器、不联网。**

    ``--a`` 是基线（当前生产链 obscura），``--b`` 是候选（camoufox）。
    退出码：0 = 机械判据全过；1 = 有判据不达标；2 = 有判不了的条目
    （例如基线中位数为 0，比值无从计算）——**「判不了」不等于「过了」**。
    """
    from trajectory_pipeline.executor import dual_run

    a = dual_run.summarize(args.a_label, Path(args.a))
    b = dual_run.summarize(args.b_label, Path(args.b))
    report = dual_run.compare(a, b)
    print(dual_run.format_report(report))
    if args.json:
        out = Path(args.json)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(report, ensure_ascii=False, indent=2),
                       encoding="utf-8")
        print(f"\n报告已落盘：{out}")
    return {"pass": 0, "fail": 1}.get(report["verdict"], 2)


def cmd_build(args: argparse.Namespace) -> int:
    """P1 → P2 → P3 组装。**全离线，不开浏览器。**

    两段可分别跑（``--stage p2`` / ``--stage p3``），因为它们的输入不同：
    ``p2`` 读 P1 存档，``p3`` 读 P2 样本。P1 变了只需重跑 ``p2``；
    只是视图渲染口径改了只需重跑 ``p3``——而 P1 贵、P2 便宜。

    **默认不覆盖已落盘的产物**（同 ``--apply`` 那条纪律：重建批次不该
    悄悄抹掉别人标好的东西）。要重建整批给 ``--overwrite``。

    ⚠️ **读哪一份 P1 由 :func:`select_archives` 决定**（人工复核档优先）。
    这一步以前是 builder 自己 glob 原档，于是同一条 run 有两套结论：
    ``report`` / ``check-archives`` / ``evaluation`` 看复核档，P2 看原档——
    重生成写复核档，而 build 读原档，**跑完重生成 P2 一个字节都不变**。
    解析放在本层（executor 能看见 ``select_archives``，assembler 不能），
    名单显式传给 builder，不破坏 assembler 不 import executor 的契约。

    返回码：0 = 全部写出；1 = 有跳过（已存在或写失败）。跳过的条数与
    原因逐条印出——**「跳过」必须看得见**，否则批次看起来只是少了几条。
    """
    root = Path(args.out) if args.out else DEFAULT_ROOT
    stage = args.stage
    era_filter = getattr(args, "rationale_era", builder.ERA_ALL) or builder.ERA_ALL

    if stage in ("p2", "all"):
        picked = [path.name for path, _reviewed in select_archives(root)]
        rep = builder.build_all(source_root=root, target_root=root,
                                overwrite=args.overwrite,
                                archive_filter=picked,
                                era_filter=era_filter)
        print(f"P1 → P2：{len(rep.archives)} 份存档，"
              f"切出 {rep.total_samples} 条样本，写出 {rep.total_written} 条")
        degraded = sum(a.degraded for a in rep.archives)
        negatives = sum(a.negatives for a in rep.archives)
        print(f"  降级样本 {degraded} 条（degraded_from 非空，缺件但仍是真实数据）")
        print(f"  真负样本 {negatives} 条（branch ∈ 真负样本分支）")
        print(f"  rationale 口径：pre_action {rep.total_pre_action} 条 / "
              f"post_hoc {rep.total_post_hoc} 条")
        if rep.total_era_skipped:
            print(f"  按口径筛掉 {rep.total_era_skipped} 条"
                  f"（--rationale-era {era_filter}）——它们在名单里，只是没落盘")
        if rep.mixed_era:
            # **必须显式印**，不能只躺在数字里：两代混训的后果是
            # 「有时可以未卜先知、有时不行」，而报表上只表现为分数略降。
            print("  ⚠️ 混批：pre_action 与 post_hoc 同时存在。"
                  "要么跑 regenerate-rationales 把旧档重生成，"
                  "要么 --rationale-era pre_action 构同口径批次。"
                  "（旧口径样本的闸门记 not_run——跨口径不判）")
        bad = [a for a in rep.archives if a.sample_count == 0]
        if bad:
            print(f"\n切不出样本的存档 {len(bad)} 份"
                  f"（坏档或被反爬拦下——后者是正常形态）：")
            for a in bad[:10]:
                why = a.skipped[0] if a.skipped else "无 outcome"
                print(f"  {a.archive}：{why}")
            if len(bad) > 10:
                print(f"  …另有 {len(bad) - 10} 份")
        print(f"  → {rep.sample_dir}")

    if stage in ("p3", "all"):
        vrep = views.build_views_from_dir(root=root, overwrite=args.overwrite)
        print(f"\nP2 → P3：{len(vrep.samples)} 条样本，"
              f"写出 {vrep.total_written} 份视图（每条 {len(views.VIEW_NAMES)} 份）")
        if vrep.total_skipped:
            # 按视图名归组，**不逐条印**。实测二次跑（默认不覆盖）会跳过
            # 196×3 = 588 条，逐条印就是 588 行噪声——门禁一旦天天报同一批
            # 已知项，人就开始整体跳过它，那它存在的意义也没了
            # （同 integrity.py 的 DEGRADED 归组）。
            by_view: Counter = Counter()
            for r in vrep.samples:
                for s in r.skipped:
                    name = s.split("_")[-1].split(" ")[0] or "?"
                    by_view[name] += 1
            for name, n in by_view.most_common():
                print(f"  {name} 跳过 {n} 份（已存在或写失败；要重建加 --overwrite）")
        print(f"  → {vrep.views_dir}")

    return 0


def cmd_task_view(args: argparse.Namespace) -> int:
    """P1 → T 落盘——把 per-Run 完整任务视图渲染出来，给 LS 上传 / 人工复核 / L4 用。

    与 ``cmd_build`` 同款纪律（全离线 / 不开浏览器），但**只跑 T 这一段**。
    T 与 P3 是**两种独立渲染**：P3 是 P2 的另一份渲染（per-unit，给 SFT 训练）；
    T 是 P1 的另一份渲染（per-Run，给 LS 上传 / 复核）。P3 训练契约不动——
    T 单独跑，不依附 ``cmd build``。

    三种调用形态：

    - ``--p1 <file>...`` 渲染指定的 P1 存档（**支持多次**——重跑某几条）；
    - ``--all`` 渲染 ``--out`` 目录下全部 P1 存档（默认根 = executor
      :data:`DEFAULT_ROOT`）；
    - ``--p1`` / ``--all`` + ``--overwrite`` 重建已存在的 T 视图。

    默认不覆盖已落盘的产物（与 ``cmd build`` 同款——重建批次不该把别人
    刚标好的视图抹掉）。

    Returns:
        始终 0（与 :func:`cmd_build` 同款纪律——跳过走 stdout 印出，
        不靠退出码传达）。跳过的条数与原因逐组印出——**「跳过」必须
        看得见**，否则批次看起来只是少了几条。
    """
    from trajectory_pipeline.assembler import task_view as tv
    from trajectory_pipeline.executor.archive import DEFAULT_ROOT

    root = Path(args.out) if args.out else DEFAULT_ROOT
    overwrite = bool(args.overwrite)
    if args.all:
        # 整批：枚举 P1 存档目录
        results = tv.build_all_task_views(
            source_root=root, target_root=root, overwrite=overwrite,
            backfill_from_filename=bool(
                getattr(args, "backfill_from_filename", True),
            ),
        )
        # 跳过原因归组打印（与 cmd_build 的 P3 段同款纪律——
        # 二次跑会跳过 N 条，逐条印是噪声）。
        by_reason: Counter = Counter()
        for r in results.results:
            for s in r.skipped:
                # s 形如 "T001__a1.json: 已存在" 或 "T001__a1: 缺 payload 键 [...]"
                # 抓 ``:`` 后的原因段做归组
                reason = s.split(":", 1)[1].strip() if ":" in s else s
                by_reason[reason] += 1
        print(f"P1 → T：{len(results.results)} 份存档，"
              f"写出 {results.total_written} 份 T 视图"
              f"（每份单文件三视图合一）")
        for reason, n in by_reason.most_common():
            print(f"  跳过 {n} 份（{reason}；要重建加 --overwrite）")
        print(f"  → {results.target_dir}")
        return 0

    if args.p1:
        for spec in args.p1:
            p = Path(spec)
            if not p.exists():
                print(f"  跳过 {p}（文件不存在）", flush=True)
                continue
            try:
                archive = json.loads(p.read_text(encoding="utf-8"))
            except (OSError, ValueError) as exc:
                print(f"  跳过 {p}（读档失败 {type(exc).__name__}: {exc}）",
                      flush=True)
                continue
            rep = tv.build_task_view(archive, root=root, overwrite=overwrite)
            if rep.written:
                print(f"  ✓ {rep.trajectory_id} → {p.name}", flush=True)
            else:
                reason = rep.skipped[0] if rep.skipped else "未写"
                print(f"  跳过 {rep.trajectory_id}（{reason}；要重建加 --overwrite）",
                      flush=True)
        return 0

    # 既没传 --p1 也没传 --all：必须二选一
    print("error: 必须传 --p1 <file>... 或 --all", flush=True)
    return 2


def _derived_products(root: Path, run_id: str) -> tuple[Path, ...]:
    """一个 run 的**派生产物**（P2 样本 / P3 视图 / T 视图）。

    为什么回滚要连它们一起删
    ------------------------
    P1 存档删了、负样本池行删了，**派生的训练数据却留在原地**——
    那批样本的标签已经被判为「前提不成立」，却还躺在 ``samples/`` 里，
    等着被渲染进 P3、被上传给 Label Studio、被喂进 SFT。

    而且它们**再也重建不了**：源存档没了，重建只会跳过（造不出新样本），
    于是这批旧样本就成了永久的、与 catalog 漂移的僵尸——训练集里一半
    样本带新工具声明、一半带旧的，而**报告上看不出任何异常**。

    ⚠️ 2026-10-20 实测：盘上 19 条孤儿样本就是这么来的——3 个 run 被
    回滚过，而当时的 ``purge-run`` 只删存档与池行。更麻烦的是**那条命令
    当时也清不掉它们**：它只数 P1 存档，于是报「这个 run_id 下没有东西」
    就退出了，孤儿**永久留下**。本函数的存在就是为了让回滚能收干净。

    ⚠️ 三个目录的**路径权威**分别在
    :func:`trajectory_pipeline.assembler.builder.samples_dir`、
    :func:`trajectory_pipeline.storage.views_dir`、
    :func:`trajectory_pipeline.storage.task_views_dir`——本函数只调它们，
    **不自己拼字面量**（改存储布局只动那三处）。

    ⚠️ ``run_id`` 会被拼进 glob 模式，所以这里**限制字符集**：glob 里的
    ``*``/``?``/``[`` 会匹配到别的 run。这条命令会**删文件**，匹配面必须
    收紧。
    """
    run_id = (run_id or "").strip()
    if not run_id or any(
        ch not in (string.ascii_letters + string.digits + "_-") for ch in run_id
    ):
        return ()

    from trajectory_pipeline.storage import task_views_dir, views_dir

    out: list[Path] = []
    for pattern, directory in (
        (f"*__{run_id}__*.json", builder.samples_dir(root)),
        (f"*__{run_id}__*.json", views_dir(root)),
        (f"*__{run_id}.json", task_views_dir(root)),
    ):
        if directory.is_dir():
            out.extend(directory.glob(pattern))
    return tuple(sorted(set(out)))


def cmd_purge_run(args: argparse.Namespace) -> int:
    """回滚一个 run——**存档、派生产物与它并进池的负样本一起删**。

    默认**只列不删**（``--apply`` 才真删）。这条命令动的是负样本池
    与训练样本，删错了不可逆——所以默认口径必须是「先看清楚」。

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
    derived = _derived_products(root, args.run_id)

    print(f"run_id = {args.run_id}   根目录 = {root}")
    print(f"\n存档（{len(archives)}）:")
    for path in archives:
        print(f"  {path.name}")
    print(f"\n负样本池行（{len(rows)}）:")
    for row in rows:
        print(f"  [{row.get('branch')}] {str(row.get('url'))[:78]}")
        print(f"      {str(row.get('evidence'))[:110]}")
    print(f"\n派生产物 · P2 样本 / P3 视图 / T 视图（{len(derived)}）:")
    for path in derived:
        try:
            shown = path.relative_to(root)
        except ValueError:
            shown = path
        print(f"  {shown}")
    if not archives and not rows and not derived:
        print("\n这个 run_id 下没有东西。run_id 是存档文件名里 __ 后半段"
              "（T001__b3a3548a.json → b3a3548a），别把整条 task_id 填进来。")
        return 1

    if not args.apply:
        print("\n以上为**预演**。确认无误后加 --apply 执行。")
        return 0
    n_arc, n_rows = store.apply_purge(archives, rows)
    n_derived = 0
    for path in derived:
        try:
            path.unlink(missing_ok=True)
            n_derived += 1
        except OSError:
            # 删不掉就报出来，不静默——留下的孤儿比没回滚更糟
            print(f"  [WARN] 删不掉: {path}")
    print(f"\n已删除：存档 {n_arc} 个，池行 {n_rows} 条，派生产物 {n_derived} 个。"
          f"建议接着跑 check-archives 看池与全库是否还自相矛盾。")
    if n_derived < len(derived):
        print(f"⚠️ 有 {len(derived) - n_derived} 个派生产物没删掉——"
              f"它们会留在训练集里，重建也救不回来（源存档已删）。")
    return 0


#: 存量 rationale 重生成前的**备份子目录**（INC46）。
#:
#: 必须是**子目录**而不是 ``output/pipeline/`` 下的同级文件：
#: ``assembler.schema.iter_samples`` 用 ``Path(root).glob("*.json")`` 收档，
#: 备份若与真档同级，重建 P2 时会把备份也当成一批样本切出来——
#: 批次数字翻倍，而两批内容一模一样，报表上看不出多了一份。
#: 放在子目录里，顶层 glob 天然拾不到。
BACKUP_DIRNAME = "_backup_pre_motivation"


def _regenerate_units(archive: Mapping[str, Any]) -> list[dict[str, Any]]:
    """一份 P1 存档 → 待重生成的**决策单元**列表。

    与 :func:`~trajectory_pipeline.assembler.schema.split_archive` 的切条
    口径**同源**：一个决策单元一条（搜索一条 + 每个站点一条）。
    刻意不从 assembler 里 import 一个切条函数——那里产出的是 ``Sample``
    （带 actions/observations 视图），而这里要的是**能写回原档的路径**
    （改 ``steps[i].rationale`` 与 unit 级 ``reflection``），
    两组东西的形状不同，合成一个只会让两边都别扭。

    每项：``{"label", "steps", "outcome", "unit", "write_reflection"}``。

    ``unit`` 是**单元本体**（搜索单元 = 存档顶层、站点单元 = ``visits[i]``），
    用来判它已经是哪一代口径（``reflection`` 键在不在，同
    :func:`~trajectory_pipeline.assembler.schema.rationale_era_of` 的判据）。
    """
    units: list[dict[str, Any]] = []

    def top_steps() -> list[dict[str, Any]]:
        return list(archive.get("steps") or [])

    if top_steps():
        units.append({
            "label": "search",
            "steps": top_steps(),
            "outcome": None,
            "unit": archive,
            "write_reflection": lambda payload, arc=archive: arc.__setitem__(
                "reflection", payload),
        })
    outcomes = list(archive.get("outcomes") or ())
    for i, visit in enumerate(archive.get("visits") or (), 1):
        vsteps = list(visit.get("steps") or [])
        if not vsteps:
            continue
        units.append({
            "label": f"site-{i}",
            "steps": vsteps,
            "outcome": _outcome_json_in(outcomes, visit),
            "unit": visit,
            "write_reflection": lambda payload, v=visit: v.__setitem__(
                "reflection", payload),
        })
    return units


def _step_written(step: Mapping[str, Any]) -> bool:
    """这一步现有 rationale 是不是**新口径下写成了**。

    ``status`` 是 ``WriteResult.to_json`` 的三态之一。缺键 / 不是 Mapping
    一律按「没写成」处理——重写它是安全的（旧档的 rationale 要么没写成，
    要么会被整段换掉，见 ``--only-old`` 的口径）。
    """
    payload = step.get("rationale")
    return (isinstance(payload, Mapping)
            and str(payload.get("status") or "") == "written")


def _pending_steps(unit: Mapping[str, Any], only_old: bool) -> list[Mapping[str, Any]]:
    """这个单元里**这一轮要重写**的步骤。

    ``only_old=False`` → 全部（当前默认，兼容既有批次行为）。
    ``only_old=True`` → 旧口径单元整段重写；已跨代单元只补没写成的那几步
    （已有 ``written`` 的一个不动——省调用，也不会把好文本换成
    ``unavailable``）。
    """
    steps = list(unit["steps"])
    if not only_old:
        return steps
    if "reflection" not in unit["unit"]:
        return steps
    return [s for s in steps if not _step_written(s)]


def _needs_reflection(unit: Mapping[str, Any], only_old: bool) -> bool:
    """这一轮要不要（重）写 unit 级 reflection。

    已有 reflection 键的单元在 ``--only-old`` 下**不重掷**：它是已经写好的
    总结，补缺步骤不该顺带改结论；而旧口径单元根本没有那份总结。
    """
    if not only_old:
        return True
    return "reflection" not in unit["unit"]


def _outcome_json_in(outcomes: Sequence[Mapping[str, Any]],
                     visit: Mapping[str, Any]) -> Mapping[str, Any] | None:
    """按 url 找站点单元的最终判定。**先候选 url、再落地 url。**

    与 :func:`~trajectory_pipeline.assembler.schema._outcome_for` 同款顺序
    （那里的长注释解释了为什么两个都要试）。这里**不 import** 那份实现：
    它返回 ``OutcomeRef``（P2 侧的建模），而这里要喂给 writer 的是
    P1 的原始 JSON——转一次再转回来只是多一次走样机会。
    """
    for want in (str(visit.get("url") or ""), str(visit.get("landed_url") or "")):
        if not want:
            continue
        for o in outcomes:
            if str(o.get("url") or "") == want:
                return o
    return None


def cmd_regenerate_rationales(args: argparse.Namespace) -> int:
    """存量 rationale 重生成（INC46：事后反思 → 动作前动机）。

    为什么要整批重生成而不是「新数据用新口径、老数据留着」：
    P2/P3 是**混批**训练的，一批事后反思混在一批动作前动机里，
    模型学到的是「有时可以未卜先知、有时不行」——那比全错更难发现，
    因为它在报表上表现为「分数略有下降」而不是「坏了一批」。

    规模账（2026-10-25 实测：63 档旧口径 = 467 step + 309 reflection ≈
    **776 次 LLM 调用**）是这条命令唯一的真实成本，所以默认口径是
    **先 ``--dry-run``**，整批时加 ``--only-old``（见下）。

    ⚠️ **写的是 ``select_archives`` 挑出来的那一份**（人工复核档优先）。
    ``build`` 读的也是同一份（``cmd_build`` 显式解析名单）——两条命令
    曾经各看各的：这条写复核档，build 读原档，于是**跑完重生成 P2 一个
    字节都不变**。

    失败纪律：**单步失败落 ``status="unavailable"`` 并继续，不中断整批**。
    一次后端抖动不该让 776 次调用里已经成功的 775 次作废，
    而缺的那些在 P2 侧会变成 ``rationale_missing``，本来就是可视的。

    ``--only-old``（**整批跑时该加**）的语义：

    - **还没有 unit 级 ``reflection``** 的单元（旧口径）→ 整段重写 +
      补 reflection；
    - **已有 reflection 但有步骤没写成** 的单元 → 只补那几步
      （已有 ``written`` 的一个不动），reflection 不重写；
    - 已完整跨代的单元 → **跳过**，一次调用都不花。

    没有它时，每个单元都整段重写——那会把上一轮已经写好的文本重新掷一遍
    骰子，一次后端抖动就能把好数据换成 ``unavailable``。
    """
    root = Path(args.out) if args.out else DEFAULT_ROOT
    writer, why = _make_rationale_writer()
    if writer is None and not args.dry_run:
        print(f"[FAIL] rationale writer 不可用（{why}）——"
              f"重生成需要 TRAJECTORY_LLM_* 环境变量配好的后端。"
              f"只想看规模就加 --dry-run")
        return 2

    archives = [(p, reviewed) for p, reviewed in select_archives(root)]
    if args.only:
        want = args.only if args.only.endswith(".json") else args.only + ".json"
        archives = [a for a in archives if a[0].name == want]
        if not archives:
            print(f"[FAIL] {root} 下没有 {want}")
            return 2

    if not archives:
        print(f"[FAIL] {root} 下没有可重生成的存档")
        return 2

    only_old = getattr(args, "only_old", False)

    if args.dry_run:
        n_steps = 0
        n_reflect = 0
        for path, _ in archives:
            try:
                arc = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                print(f"  {path.name}: 读不了（{exc}）——跳过")
                continue
            units = _regenerate_units(arc)
            s = sum(len(_pending_steps(u, only_old)) for u in units)
            # 「要重写几次 reflection」= 需要整段重写（旧口径）的单元数；
            # 补缺步骤的单元只补步骤，不重掷那份已经写好的总结。
            r = sum(1 for u in units if _needs_reflection(u, only_old))
            if s or r:
                n_steps += s
                n_reflect += r
                print(f"  {path.name}: {len(units)} 单元中待写 {s} 步 / "
                      f"{r} 条 reflection")
        print(f"\n[DRY-RUN] 将重写 {n_steps} 条 rationale + {n_reflect} 条 "
              f"reflection ≈ {n_steps + n_reflect} 次 LLM 调用。"
              f"未写盘、未备份。")
        return 0

    store = P1Archive(root)
    backup_dir = root / BACKUP_DIRNAME
    total_written = 0
    total_unavailable = 0

    for path, _reviewed in archives:
        try:
            arc = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            print(f"  {path.name}: 读不了（{exc}）——跳过，原档未动")
            continue
        if not isinstance(arc, dict):
            print(f"  {path.name}: 顶层不是 JSON 对象——跳过，原档未动")
            continue

        units = _regenerate_units(arc)
        if not units:
            print(f"  {path.name}: 0 单元（没有 steps）——跳过，原档未动")
            continue

        # `--only-old`：已完整跨代的单元一个字节都不动。
        todo = [
            u for u in units
            if _pending_steps(u, only_old) or _needs_reflection(u, only_old)
        ]
        if not todo:
            print(f"  {path.name}: 已是动作前动机口径，跳过（未写盘、未备份）")
            continue
        units = todo

        # 备份**首次写回前**做一次。已存在即跳过：重跑这条命令
        # （补跑失败的那几步）不该把上一轮的结果覆盖成「改之前的原始档」
        # ——那会让已经重生成的几百条退回成事后反思。
        backup = backup_dir / path.name
        if not backup.exists():
            backup_dir.mkdir(parents=True, exist_ok=True)
            shutil.copy2(path, backup)

        written = 0
        unavailable = 0
        for unit in units:
            steps = unit["steps"]
            pending = set(id(s) for s in _pending_steps(unit, only_old))
            for index, step in enumerate(steps):
                if id(step) not in pending:
                    continue
                try:
                    payload = writer(
                        action=step.get("action") or {},
                        user_prompt=str(arc.get("user_prompt") or ""),
                        target_title=str(arc.get("title") or ""),
                        history=[
                            (s.get("action") or {}, s.get("observation"))
                            for s in steps[:index]
                        ],
                    )
                except Exception as exc:  # noqa: BLE001 — 见函数 docstring
                    payload = {
                        "status": "unavailable",
                        "rationale": None,
                        "reason": f"{type(exc).__name__}: {exc}",
                        "degraded": [],
                    }
                step["rationale"] = payload
                if payload.get("status") == "written":
                    written += 1
                else:
                    unavailable += 1
            reflector = getattr(writer, "reflect", None)
            if reflector is not None and _needs_reflection(unit, only_old):
                try:
                    unit["write_reflection"](reflector(
                        user_prompt=str(arc.get("user_prompt") or ""),
                        target_title=str(arc.get("title") or ""),
                        history=[
                            (s.get("action") or {}, s.get("observation"))
                            for s in steps
                        ],
                        outcome=unit["outcome"],
                    ))
                except Exception as exc:  # noqa: BLE001 — 同上
                    unit["write_reflection"]({
                        "status": "unavailable",
                        "rationale": None,
                        "reason": f"{type(exc).__name__}: {exc}",
                        "degraded": [],
                    })

        # 原子写回：与 P1Archive 落盘同款（``.tmp`` + ``os.replace``）。
        # 刻意**复用** store 的私有方法而不是在这里再写一遍——
        # 「原子写」有两份实现时，改了一份忘了另一份，后果是某条路径
        # 上留下半截 JSON，而那正好是批跑到一半崩掉的场景。
        store._write_text(path, json.dumps(arc, ensure_ascii=False, indent=2))
        total_written += written
        total_unavailable += unavailable
        print(f"  {path.name}: written={written} unavailable={unavailable}")

    print(f"\n完成：{len(archives)} 档，written={total_written} "
          f"unavailable={total_unavailable}。原档已备份到 {backup_dir}")
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
    run.add_argument("--per-site-timeout", type=_positive_float, default=None,
                     help="单站超时（秒）。**它落进 run_config**——"
                          "B-4（异常处置）唯一的时限判据，不落盘等于没有判据。"
                          "缺省 180（2026-10-22 从 90 提上来：camoufox 单步慢"
                          "一个量级，实测单次点击上界 13.3s × 8 步 ≈ 122s；"
                          "撞顶会被记成 unreachable_hard ——理由通顺的假负样本）"
                          "。必须是正数：0 或负数没有业务含义，放行会产出一批"
                          "「立刻超时、看起来正常的空批」")
    # ── 发现链路径（2026-10-16 路径 B/C）──────────────────────────
    run.add_argument("--discovery-path", default="direct",
                     choices=DISCOVERY_PATHS,
                     help="发现链路径：direct（A，直搜，默认，与现状完全一致）/ "
                          "aggregator（B，聚合站发现）/ article（C，文章引荐）。"
                          "单路径 per run，批次按 run_config.discovery_path 归因")
    run.add_argument("--max-intermediates", type=int, default=3,
                     help="B/C 路径访问的中间页数上限。截断保序——只记警告不中断，"
                          "是预算控制不是语义筛选")
    run.add_argument("--max-referral-links", type=int, default=5,
                     help="每个中间页校验的外链数上限")
    run.add_argument("--driver", choices=SUPPORTED_KINDS, default=DEFAULT_KIND,
                     help="执行层实现（P5 双跑用）。默认值与 driver_factory 的 "
                          "DEFAULT_KIND 同源——切换生产链只改那一处。"
                          "obscura 整批一条 MCP 连接，camoufox 一个 Run 一次 "
                          "launch。注意：两种实现落出的存档**不是同一口径**"
                          "（元素键名与词表都随面变），不能混池")
    run.add_argument("--headful", action="store_true",
                     help="（仅 camoufox 面）用**有头**浏览器而不是无头。"
                          "缺省是**无头**——无人值守时 headful 窗口一旦不可渲染"
                          "（最小化/息屏/锁屏/遮挡），单次点击要 5–13s，"
                          "会把一半的点击打成超时假负样本（设计方案 04 §4.9.5）。"
                          "有头 = 人守着的桌面。两种口径都记进批次元数据")
    run.add_argument("--element-basis", action="store_true",
                     help="采**跨面可比的元素账目**（设计方案 04 §4.9.6）："
                          "同一段 JS + 同一把尺子在两个浏览器上各跑一次，"
                          "落进存档的 element_basis 列（shared/face 两本账）。"
                          "**默认关**——多一次 evaluate 往返，是测量仪器不是"
                          "生产链必需项。开它是为了让判据②③的数**可比**，"
                          "不是改 elements_total 的语义")
    run.add_argument("--log-level", choices=("debug", "info", "warning", "error"),
                     default="info",
                     help="发现链运行日志级别（orchestrator 关键节点实时输出到"
                          "stderr；默认 info。warning 可只看告警，debug 留给"
                          "深挖）")
    run.add_argument("--out", default=None, help="P1 输出目录")
    run.add_argument("--perceptor", choices=("rule", "llm", "auto"), default=None,
                     help="感知实现。默认读 TRAJECTORY_PERCEPTOR，再默认 auto"
                          "（配了 LLM 后端就用 LLM 版，否则规则版）")
    # 执行器模式（A3-1 转正）：choices 用封闭集本身——多一个值就要改
    # mode.py，「参数集」与「取值集」两个产地的事不干。缺省 None = 读
    # 环境变量 fallback，再不济默认 scripted（RunConfig 的第一纪律：
    # 默认值不改变现状）。
    run.add_argument("--executor-mode", choices=EXECUTOR_MODES, default=None,
                     help="执行器模式：scripted=固定序列，agent=站内自主"
                          "导航（需配齐 TRAJECTORY_LLM_*，缺件报错不静默"
                          "退回 scripted）。缺省读 TRAJECTORY_EXECUTOR_MODE，"
                          "再缺省 scripted")
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

    rq = sub.add_parser(
        "review-queue",
        help="改复核队列工作稿：按形态预填 / 同 URL 裁定收敛（**不动 P1**）")
    rq.add_argument("queue", help="review --write 导出的 jsonl")
    rq.add_argument("--prefill", action="store_true",
                    help="门户站→not_play_site；零元素→need_browser（**只填空行**）")
    rq.add_argument("--settle", action="store_true",
                    help="同 URL 收敛成一个裁定，「没证据」档让位于结论档")
    rq.set_defaults(func=cmd_review_queue)

    dg = sub.add_parser(
        "downgrade", help="取证后把判定不成立的 outcome 降为 unresolved（**默认 dry-run**）")
    dg.add_argument("--out", default=None, help="P1 存档目录（默认 output/pipeline）")
    dg.add_argument("--url", nargs="+", default=None,
                    help="按 URL 片段挑（**不要**与 --evidence 同用）")
    dg.add_argument("--evidence", nargs="+", default=None,
                    help="按**原始判定正文**是否命中正则来挑（只看第一段）")
    dg.add_argument("--branch", nargs="+", default=None,
                    help="限定分支（不受理 unresolved）")
    dg.add_argument("--reason", default=None, help="降级理由，**必填**，原样进 evidence")
    dg.add_argument("--apply", action="store_true", help="落盘（默认只报清单）")
    dg.set_defaults(func=cmd_downgrade)

    rl = sub.add_parser(
        "relabel",
        help="把「标签错了但仍是负样本」的记录改分支（**默认 dry-run**）")
    rl.add_argument("--to", required=True,
                    help="目标分支。**必须仍是真负样本**"
                         "（unresolved / trailer_suspect / player_unverified "
                         "这类反思档不是负样本，走 cli review --apply）")
    rl.add_argument("--out", default=None, help="P1 存档目录（默认 output/pipeline）")
    rl.add_argument("--url", nargs="+", default=None,
                    help="按 URL 片段挑（多片段 **OR**）")
    rl.add_argument("--branch", nargs="+", default=None,
                    help="按**当前**分支挑（多名 **OR**）")
    rl.add_argument("--evidence", nargs="+", default=None,
                    help="按**原始判定正文**是否命中正则来挑（只看第一段）")
    rl.add_argument("--reason", default=None,
                    help="改判理由，**必填**，原样进 evidence")
    rl.add_argument("--apply", action="store_true", help="落盘（默认只报清单）")
    rl.set_defaults(func=cmd_relabel)

    pdr = sub.add_parser(
        "pool-drop",
        help="撤掉负样本池里的行（**默认 dry-run**；只动池，不动存档）")
    pdr.add_argument("--out", default=None, help="P1 存档目录（默认 output/pipeline）")
    pdr.add_argument("--url", nargs="+", default=None,
                     help="按 URL 片段挑（多片段 **OR**）")
    pdr.add_argument("--task-id", default=None,
                     help="限定 task_id（与 --url 同为 **AND**）")
    pdr.add_argument("--branch", default=None,
                     help="限定分支（与上面同为 **AND**）")
    pdr.add_argument("--reason", default=None,
                     help="撤行理由，**必填**，进 pool_dropped.jsonl 审计日志")
    pdr.add_argument("--apply", action="store_true", help="落盘（默认只报清单）")
    pdr.set_defaults(func=cmd_pool_drop)

    chk = sub.add_parser("check", help="查能力，不跑任务")
    chk.add_argument("--probe-url", default="https://example.com/")
    chk.add_argument(
        "--driver", choices=SUPPORTED_KINDS, default=DEFAULT_KIND,
        help="探哪条执行层。**缺省 = DEFAULT_KIND（camoufox，生产面）**——"
             "自检默认探的必须是**生产面**：探的不是生产面，读数就不能外推"
             "（与「工具声明按面取」同一条纪律）。"
             "obscura 要显式点名（回溯路径，需 OBSCURA_EXE）")
    chk.add_argument(
        "--action-smoke", action="store_true",
        help="（仅 --driver camoufox）动作面冒烟：点一次枚举出来的元素并打印账目。"
             "P2 的验收判据「动作可用」只有真浏览器能证明——单测用的是假 page。"
             "会改变页面状态（可能导航），故默认关",
    )
    chk.add_argument(
        "--headful", action="store_true",
        help="（仅 --driver camoufox）用**有头**浏览器。缺省无头——"
             "无人值守口径（设计方案 04 §4.9.5）。加这个开关看的是"
             "「人守着桌面」那条路的指纹读数",
    )
    chk.add_argument(
        "--element-basis", action="store_true",
        help="采**跨面可比的元素账目**（设计方案 04 §4.9.6）并打印两本账。"
             "默认关——它是测量仪器，不是生产链必需项",
    )
    chk.set_defaults(func=cmd_check)

    prb = sub.add_parser(
        "probe", help="取证探针：开浏览器看现场事实，用于分清存档里两边矛盾的证据")
    prb.add_argument("url", nargs="*", help="要探的 URL")
    prb.add_argument("--from-archive", default=None,
                     help="P1 存档目录：从该目录待复核项里取 URL（不用手打）")
    prb.add_argument("--limit", type=int, default=None, help="--from-archive 时取前 N 个")
    prb.add_argument("--preview", type=int, default=400, help="打印正文前 N 字")
    prb.add_argument("--driver", choices=SUPPORTED_KINDS, default=DEFAULT_KIND,
                     help="执行层实现；默认 = DEFAULT_KIND（camoufox）。"
                          "**探针要跑在生产面上**——跑的浏览器不是生产面，"
                          "现场事实就不能外推到生产批次。"
                          "obscura 需 OBSCURA_EXE 环境变量。")
    prb.add_argument("--headful", action="store_true",
                     help="camoufox 显式切回有头（默认无头：无人值守时"
                          "有头窗口不可渲染，单次点击要 5–13s）")
    prb.set_defaults(func=cmd_probe)

    # 2026-10-12 增量十三配套：阶段 1 新分支 ground-truth 验证工具。
    # 接受 URL 列表（每行一个），跳过搜索引擎、直接逐站 visit。
    # 与 ``probe`` 是两种不同工具：probe 看不结论、proberun 走完 visit。
    prn = sub.add_parser(
        "proberun",
        help="阶段 1 新分支验证：直接对一组 URL 走完整 visit（绕开搜索引擎，**不入主链**）",
    )
    prn.add_argument("--from-urls", default=None, metavar="PATH",
                     help="每行一个 URL 的文本文件；# 开头的行视为注释")
    prn.add_argument("--task-id", default="P001",
                     help="归档用 task_id；每个 URL 一个独立 run_id")
    prn.add_argument("--title", default=None,
                     help="目标片名（构造注入给 perceptor；**必填**）")
    prn.add_argument("--engine", default=search_step.DEFAULT_ENGINE,
                     help="仅作 RunConfig 留痕；不实际驱动搜索引擎")
    prn.add_argument("--max-chars", type=int, default=DEFAULT_MAX_CHARS)
    prn.add_argument("--perceptor", choices=("rule", "llm", "auto"), default=None,
                     help="感知层选择；与 cli run 一致")
    prn.add_argument("--out", default=None, help="P1 输出目录")
    prn.add_argument("--driver", choices=SUPPORTED_KINDS, default=DEFAULT_KIND,
                     help="执行层实现；默认 = DEFAULT_KIND（camoufox）。"
                          "obscura 需 OBSCURA_EXE 环境变量。")
    prn.add_argument("--headful", action="store_true",
                     help="camoufox 显式切回有头（默认无头）")
    prn.set_defaults(func=cmd_proberun)

    # 2026-10-12 增量十八配套：阶段 4 L0 静态可达性探测。HTTP 层，
    # 无浏览器，与 proberun 并列的离线验证工具；产物进 staticprobe/
    # 子目录隔离。
    stp = sub.add_parser(
        "staticprobe",
        help="L0 静态可达性探测（HTTP 层，无浏览器，**不入主链**）",
    )
    stp.add_argument("--from-urls", default=None, metavar="PATH",
                     help="每行一个 URL 的文本文件；# 开头的行视为注释")
    stp.add_argument("--task-id", default="P018",
                     help="归档用 task_id；每个 URL 一个独立 run_id")
    stp.add_argument("--title", default=None,
                     help="任务名（留痕用；不参与探测逻辑；**必填**）")
    stp.add_argument("--timeout", type=float, default=10.0,
                     help="单 URL 探测超时（秒）；默认 10")
    stp.add_argument("--out", default=None, help="输出目录")
    stp.set_defaults(func=cmd_staticprobe)

    # 2026-10-13 增量二十二配套：阶段 4 L1 媒体解析（HTTP 拉页 + ffprobe
    # 直连）。无浏览器，与 ``staticprobe`` 配对；产物进 ``mediaprobe/``
    # 子目录隔离。
    mdp = sub.add_parser(
        "mediaprobe",
        help="L1 媒体解析（HTTP 拉页 + ffprobe 直连，无浏览器，**不入主链**）",
    )
    mdp.add_argument("--from-urls", default=None, metavar="PATH",
                     help="每行一个 URL 的文本文件；# 开头的行视为注释")
    mdp.add_argument("--task-id", default="P022",
                     help="归档用 task_id；每个 URL 一个独立 run_id")
    mdp.add_argument("--title", default=None,
                     help="任务名（留痕用；不参与探测逻辑；**必填**）")
    mdp.add_argument("--timeout", type=float, default=10.0,
                     help="单 URL 拉页超时（秒）；默认 10")
    mdp.add_argument("--out", default=None, help="输出目录")
    mdp.set_defaults(func=cmd_mediaprobe)

    # 2026-10-13 增量二十三配套：阶段 4 L2 浏览器媒体探针（PageDriver 协议）。
    # 与 mediaprobe / staticprobe 配对——三个都是不入主链的离线验证工具。
    # 默认 --driver fake 不打开真浏览器；要真验 partial_free 形态需接
    # obscura driver（L4/L5 串接时一并做）。产物进 mediabrowse/ 子目录隔离。
    mbb = sub.add_parser(
        "mediabrowse",
        help=(
            "L2 浏览器媒体探针（JS-aware，PageDriver 协议，"
            "**不入主链**）"
        ),
    )
    mbb.add_argument("--from-urls", default=None, metavar="PATH",
                     help="每行一个 URL 的文本文件；# 开头的行视为注释")
    mbb.add_argument("--task-id", default="P023",
                     help="归档用 task_id；每个 URL 一个独立 run_id")
    mbb.add_argument("--title", default=None,
                     help="任务名（留痕用；不参与探测逻辑；**必填**）")
    mbb.add_argument("--timeout", type=float, default=15.0,
                     help="单 URL 浏览器探针超时（秒）；默认 15")
    mbb.add_argument("--wait-s", type=float, default=2.0,
                     help="L2 两次 currentTime 采样间隔（秒）；默认 2")
    mbb.add_argument("--driver", choices=("fake", "obscura", "camoufox"), default="fake",
                     help="PageDriver 选择；默认 fake 不联网（in-process mock，"
                          "FastPageDriver 鸭子兼容 L4 evaluate+snapshot），"
                          "obscura 真驱动经 stdio MCP 起 V8 isolate；"
                          "需 OBSCURA_EXE 环境变量指二进制位置。"
                          "camoufox 是**生产面**（2026-10-22 owner 裁定）——"
                          "L2 要在生产面上采两次 currentTime 判「真推进」，"
                          "探针跑的浏览器不是生产面，结论就不能外推。"
                          " 增量二十四/二十五/二十九已打通 L0→L1→L2→L4→L3→L5 全链路"
                          "（obscura SSRF guard 拦截 127.0.0.1，需公开 URL）。")
    mbb.add_argument("--headful", action="store_true",
                     help="camoufox 显式切回有头（默认无头）")
    mbb.add_argument("--out", default=None, help="输出目录")
    # 2026-10-13 增量二十八：L1 ffprobe 真接入的开关与超时。
    # --no-l1 关闭 ffprobe 层（缺 ffprobe 二进制或调试时用）；
    # --l1-timeout 控制 L1 整层（HTTP GET 来源页 + iframe 1 跳 + ffprobe 子进程并发）总超时。
    mbb.add_argument("--no-l1", action="store_true",
                     help="关闭 L1 ffprobe 媒体解析（默认开启；缺 ffprobe 时降级跳过）")
    mbb.add_argument("--l1-timeout", type=float, default=10.0,
                     help="L1 单 URL 总超时（秒）；默认 10")
    # 2026-10-13 增量三十四：L4 reviewer 选择（heuristic / multim）。
    # 默认 heuristic——不设 key 时走 multim 也只会降级 heuristic，没有收益。
    # multim 通道需要 OPENAI_API_KEY / ANTHROPIC_API_KEY 环境变量；
    # 无 key 时 :class:`MultimVLMReviewer` 自动降级到 :class:`HeuristicVLMReviewer`
    # 并打 source="multim_fallback" + note="multim_fallback:no_api_key" 标签。
    mbb.add_argument("--reviewer", choices=("heuristic", "multim"), default="heuristic",
                     help="L4 复核器选择：heuristic（默认，无外部依赖）/ "
                          "multim（真 LLM 文本理解；OpenAI gpt-4o-mini / "
                          "Anthropic claude-3-5-haiku 双通道，无 key 降级启发式）")
    mbb.set_defaults(func=cmd_mediabrowse)



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

    # P5 的验收工具：同一批任务在两个执行层上各跑一遍，逐条比（§6.1）。
    # 与 check-archives 的分工：那个问「每份存档背后有没有东西」，
    # 这个问「换了一层之后分布变成什么样了」。
    cmp_d = sub.add_parser(
        "compare-drivers",
        help="双跑对账：同一批任务在两个执行层上的分布差（不联网）",
    )
    cmp_d.add_argument("--a", required=True,
                       help="A 侧（**基线**，通常是 obscura）的 P1 存档目录")
    cmp_d.add_argument("--b", required=True,
                       help="B 侧（候选，通常是 camoufox）的 P1 存档目录")
    cmp_d.add_argument("--a-label", default="A")
    cmp_d.add_argument("--b-label", default="B")
    cmp_d.add_argument("--json", default=None,
                       help="把完整报告写成 JSON（负样本原文、逐分支读数都在里面）")
    cmp_d.set_defaults(func=cmd_compare_drivers)

    rp = sub.add_parser("replay", help="感知层回放：在存档的真实观察上跑感知实现（不联网）")
    rp.add_argument("--out", default=None, help="P1 存档目录")
    rp.add_argument("--perceptor", choices=("rule", "llm", "both"), default="rule",
                    help="默认只跑规则版（**不需要任何 LLM 后端**，"
                         "于是「W1 在真实数据上答得出来几道」完全离线可测）")
    rp.add_argument("--limit", type=int, default=0, help="只回放前 N 份存档；0 = 全部")
    rp.add_argument("--show", type=int, default=20, help="列出前 N 条分歧")
    rp.set_defaults(func=cmd_replay)

    bd = sub.add_parser("build", help="P1 → P2 → P3 组装（不联网，不开浏览器）")
    bd.add_argument("--out", default=None, help="P1 存档目录")
    bd.add_argument("--stage", choices=("p2", "p3", "all"), default="all",
                    help="只跑 P2（读 P1）/ 只跑 P3（读 P2）/ 两段都跑")
    bd.add_argument("--overwrite", action="store_true",
                    help="覆盖已落盘的产物。**默认不覆盖**——重建批次不该"
                         "悄悄抹掉别人标好的样本")
    bd.add_argument("--rationale-era",
                    choices=(builder.ERA_ALL,
                             "pre_action", "post_hoc"),
                    default=builder.ERA_ALL,
                    help="只落这一代 rationale 口径的样本。**默认 all**——"
                         "P2 也是评估语料，默认不悄悄少数据；但 all 而两代"
                         "并存时会显式报「混批」。同口径批次用 pre_action")
    bd.set_defaults(func=cmd_build)

    tv = sub.add_parser(
        "task-view",
        help="P1 → T 落盘（per-Run 完整任务视图，给 LS 上传 / 复核用；不联网）",
    )
    tv.add_argument("--p1", nargs="+", default=None,
                    help="指定一份或多份 P1 存档路径（可重跑某几条）")
    tv.add_argument("--all", action="store_true",
                    help="渲染 --out 目录下全部 P1 存档")
    tv.add_argument("--out", default=None,
                    help="P1 存档目录（--p1 不传时也作默认根）")
    tv.add_argument("--overwrite", action="store_true",
                    help="覆盖已存在的 T 视图。**默认不覆盖**——重建批次不该"
                         "悄悄抹掉别人刚标好的视图")
    tv.add_argument("--backfill-from-filename", dest="backfill_from_filename",
                    action="store_true", default=True,
                    help="**过渡期策略**：存量 P1 JSON 顶层缺 ``run_id`` 时，"
                         "从文件名 ``<task>__<run>.json`` 派生补到内存 dict，"
                         "渲染出 T 视图（**不写回盘**）。新 P1（archive.write "
                         "增量六十一修复后自带）该参数无操作。要走纯纪律路径"
                         "（缺字段就 skipped）传 ``--no-backfill-from-filename``")
    tv.add_argument("--no-backfill-from-filename",
                    dest="backfill_from_filename", action="store_false",
                    help="显式关闭过渡期回填（缺字段就 skipped，不静默派生）")
    tv.set_defaults(func=cmd_task_view)

    pg = sub.add_parser(
        "purge-run",
        help="回滚一个 run：删它的存档 + 它并进 negative.jsonl 的行")
    pg.add_argument("--run-id", required=True,
                    help="存档文件名里 __ 后半段（T001__b3a3548a.json → b3a3548a）")
    pg.add_argument("--out", default=None, help="P1 存档目录")
    pg.add_argument("--apply", action="store_true",
                    help="**默认只列不删**。池是训练数据的直接输入，删错不可逆")
    pg.set_defaults(func=cmd_purge_run)

    rg = sub.add_parser(
        "regenerate-rationales",
        help="存量 rationale 重生成（INC46：事后反思 → 动作前动机）")
    rg.add_argument("--out", default=None, help="P1 存档目录")
    rg.add_argument("--only", default=None,
                    help="只重生成这一份存档（文件名，如 E005__e35b7455.json）")
    rg.add_argument("--dry-run", action="store_true",
                    help="**默认建议先跑它**：只打印将重写多少条，不写盘、"
                         "不备份、不调 LLM")
    rg.add_argument("--only-old", action="store_true",
                    help="整批跑时**该加**：只动旧口径单元（无 unit 级 "
                         "reflection 键）与已跨代但仍有未写步骤的单元；"
                         "已完整的单元一个字节都不动、不花 LLM 调用")
    rg.set_defaults(func=cmd_regenerate_rationales)
    return p


def main(argv: list[str] | None = None) -> int:
    _reconfigure_stdio()
    args = build_parser().parse_args(argv)
    # 运行日志（2026-10-16）：只在**真实 CLI 入口**配置，不在
    # ``cmd_run`` 里——测试直接调 ``cmd_run`` / ``_run_plan``（手工
    # Namespace），在那里读 ``args.log_level`` 会打出新的必填字段；
    # 放这里则 ``getattr`` 拿不到就跳过，别的子命令零感知。
    # ``basicConfig`` 对已配置的 root 是 no-op，pytest 的日志插件
    # 先接管时不会打架。
    level_name = getattr(args, "log_level", None)
    if level_name:
        logging.basicConfig(
            level=getattr(logging, level_name.upper(), logging.INFO),
            format="%(asctime)s %(levelname)-7s %(name)s | %(message)s",
        )
    if args.cmd == "review" and getattr(args, "apply", False):
        return cmd_apply_review(args)
    # 要不要走 asyncio **从函数本身推，不从命令名查表**。
    #
    # 原来是一份手写的命令名清单，于是每加一个子命令都得记得改它——
    # 漏改的后果是 `asyncio.run(0)`：`ValueError: a coroutine was
    # expected`，**而且崩在所有输出之后**。清单看起来干完了活，
    # 退出码却是非零，``--apply`` 那类命令尤其吓人：输出打完了，
    # 像是执行过，其实整条命令被判失败。
    #
    # 两份东西必须一致时，**别让第二份存在**——它和第一份不同源，
    # 而不同源的后果是安静地不一致（见避坑指南 §一）。
    if inspect.iscoroutinefunction(args.func):
        return asyncio.run(args.func(args))
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
