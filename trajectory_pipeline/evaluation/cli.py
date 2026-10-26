"""模块 6 的命令行入口——**评分器此前没有任何调用方**。

⚠️ **为什么单独一个 CLI，不挂进 ``executor.cli``**
------------------------------------------------
依赖方向写死在 :mod:`trajectory_pipeline.docs.01-架构总览`::

    executor ──→ storage
    assembler ──→ storage
    evaluation ──→ storage

    evaluation ──→ executor.archive    （直接：``select_archives`` / ``DEFAULT_ROOT``）
    evaluation ──→ executor.branches   （间接：``executor.archive`` 取 ``BRANCH_LABELS``）
    evaluation ──→ executor.gatekeeper （间接：``executor.branches`` 取 ``_same_page``）

⚠️ 上面 ``evaluation ──→ executor.*`` 是**向上**依赖——方向与纪律相反，
但**是刻意的**：评分器要读 executor 产出的 P1。

``executor → evaluation`` 是**横向依赖**且方向相反——评分器要读 executor
产出的 P1，而 executor 不该知道下游有个评估器。挂进 ``executor.cli``
等于让执行层 import 它的验收方，一层一层传下去。

D-2/D-4/D-5/D-6 四个评分器（~1100 行）此前**只被各自的测试 import**，
在真实批次上一次都没跑过——与「判断点 ① 是死的」同一类病：报表上数字齐全，
没人看得出这支没接线。

数据源：**P1 存档**，不走 P2
-----------------------------
存档由 :func:`~trajectory_pipeline.executor.archive.select_archives` 挑选
（**优先取 ``.reviewed.json``**），再逐份喂
:func:`~trajectory_pipeline.assembler.schema.split_archive` 切出
:class:`~assembler.schema.Sample` **对象**——与 assembler 落 P2 时是同一条路径。

⚠️ **不能用 :func:`~assembler.schema.iter_samples`**：它**刻意跳过**
``.reviewed.json``，把挑选权留给 ``select_archives``（同一条跑两遍、
两遍复核结论可能不同）。已经选过一次再让它遍历，等于把刚人工确认的正样本
全滤掉——而 D-5 判的正是这一批。

⚠️ **评分器吃对象不吃 P2 字典**——``score_d2`` 取
``getattr(sample, "observations")``，而 P2 文件把它们放在
``six.observations`` 里，直接把 ``json.load`` 的 dict 喂进去会得到
``actions=[]``，于是 **221 条全判 undetermined**（实测踩过）。
想按 P2 文件评分，得先有 ``Sample`` 的反序列化入口，而它**不存在**
（测试里是手工 ``Sample(**base)`` 造的）。
"""

from __future__ import annotations

import argparse
import collections
import json
import pathlib
import sys
from collections.abc import Mapping
from typing import Any, Callable

from trajectory_pipeline.assembler.schema import split_archive
from trajectory_pipeline.evaluation.scorers.code_scorer import score_d2
from trajectory_pipeline.evaluation.scorers.d3_scorer import score_d3
from trajectory_pipeline.evaluation.scorers.d4_scorer import score_d4
from trajectory_pipeline.evaluation.scorers.d5_scorer import score_d5
from trajectory_pipeline.evaluation.scorers.d6_scorer import score_d6
from trajectory_pipeline.evaluation.scorers.d9_scorer import score_d9
from trajectory_pipeline.executor.archive import DEFAULT_ROOT, select_archives

#: ``短名 → 打分函数``。**只有这四个**——D-3 未建（判据依赖黄金集），
#: D-1 已划到批次级 B-1。短名进 argparse，长名（``D-5``）只在报告里出现，
#: 因为 rubric 里那些是评分标准的维度号，不是代码里的名字。
SCORERS: dict[str, Callable[[Any], Any]] = {
    "d2": score_d2,
    "d4": score_d4,
    "d5": score_d5,
    "d6": score_d6,
}

#: LLM 裁判层评分器（方案 10-§四 接入点）——**默认关**，
#: 由 ``--include-llm-judge`` 显式打开。理由：
#:
#: 1. **CLAUDE.md 红线**——「生产链上禁止投票/集成」。LLM judge 与代码 judge
#:    同时跑，报表上 6 个维度并列，**没有"judge 比代码 judge 准"的额外信号**，
#:    等于默示集成。
#: 2. **延迟与成本**——D-3 / D-9 走 LLM，每条样本 1-3s；批量跑会显著拖慢报表。
#:    D-9 还依赖 ``gates.gate()`` 的 detailed 形式（方案 10-§三·第三步），
#:    不是 ``sample.gate`` 一句话能拿到的，CLI 这层**不**主动调
#:    ``gates.gate()``（避免给生产链加 200% 跑代码的成本），d9_scorer 走
#:    ``gate_result=None → 回落 sample.gate.status`` 的快路径。
#: 3. **判据口径不同**——D-3「播放站识别」判的是与黄金集类别一致性，
#:    跑 CLI 没有黄金集对照等于裸跑出 1-5 分，**对账无门**。
#:
#: ``LLM_SCORERS`` 与 ``SCORERS`` **分开维护**：默认 SCORERS = 4 个代码 judge
#:    仍满足 ``tests/unit/test_evaluation_cli.py::test_没有把SCORERS改坏``
#:    的契约。LLM_SCORERS 仅在 ``--include-llm-judge`` 时合并进 dispatch。
LLM_SCORERS: dict[str, Callable[[Any], Any]] = {
    "d3": score_d3,
    "d9": score_d9,
}
DEFAULT_ROOT_PATH = pathlib.Path(__file__).resolve().parents[1] / DEFAULT_ROOT

#: **批次判据，两级**：
#:
#: 1. **权威** —— ``Sample.schema_version``（P1 写入时记录的
#:    :data:`~trajectory_pipeline.executor.orchestrator.SCHEMA_VERSION`）。
#:    认得出来就直接按版本分档，不做任何推断。
#: 2. **代理** —— 存档压根没有 ``schema_version`` 键（现存 38 份全在这），
#:    退回 ``degraded_from`` 含不含 :data:`LEGACY_MARKER`。该码由
#:    ``split_archive`` 写，与 ``executor.integrity`` 的
#:    ``run_config_missing`` 同源，两张表由
#:    ``tests/unit/test_integrity.py::TestDegradedMarkersMatchSplit``
#:    双向盯着。
#:
#: ⚠️ **代理切出来的那一轴是「字段齐不齐」，不是「数据多老」**
#: （2026-10-10 实测 38 份存档）。38 份**全都没有** ``schema_version``；
#: 其中 16 份缺 ``run_config``，而那 16 份的 74 个 ``outcome`` 一个都
#: 没有 ``site_url`` / ``play_page_url`` **键**，另 22 份的 131 个**全都
#: 有**——相关性 100%。于是 D-6 的「100% vs 60%」完全由缺件决定，
#: 跟新旧无关；那 60% 里剩下的不可判定 cause 是「片名在正文里检索不到」
#: （``d6_scorer._check_title``），与「跑在字段补齐之前」是两种病。
#: 存档里 ``provenance`` 全空、无 ``run_id``，**没有任何独立于字段存在性
#: 的时间锚**——「那 22 份是后来跑的」唯一证据就是它们字段多，循环论证。
#:
#: 所以分档名**按判据起，不按年代起**（见 :data:`BATCH_UNVERSIONED`），
#: 「为什么不可判定」交给 :func:`_marker_stats`——那张表是
#: ``split_archive`` 当场算出的事实。
#:
#: **不回填历史存档**：没有依据断定一份没版本号的存档是哪一批，
#: 而猜错的代价是一个静默错误的批次归属。
LEGACY_MARKER = "run_config"

#: 分档名。**描述判据，不描述年代。**
#:
#: 「旧批次 / 当前批次」这种叫法在现存数据上是**凭空断言**：144 条被叫
#: 「当前批次」，而它们的存档同样没有 ``schema_version``，与另外 77 条
#: 出自同一次采集。事实上的「当前批次」一条都没有。
#:
#: 分档是附加视图，不是剔除——各档都进总分母，
#: 见 :func:`evaluate` 里的说明。
BATCH_UNVERSIONED = "未标版本"
BATCH_UNVERSIONED_MISSING = f"未标版本·缺 {LEGACY_MARKER}"


def _picked(root: pathlib.Path, limit: int | None) -> list[pathlib.Path]:
    """挑存档——走 :func:`select_archives`，**优先取 ``.reviewed.json``**。

    这不是可有可无：人工回填的正样本只存在于复核档里（``branch=None``
    + ``source=human``），只读原档等于把 W1 的正样本当成没有——
    而 D-5 的决策正确性判的正是这一批。
    """
    sel = select_archives(root)
    files = [p for p, _reviewed in sel]
    return files[:limit] if limit else files


def _agg() -> collections.Counter:
    return collections.Counter()


def _batch_of(sample: Any) -> str:
    """这条样本落在哪一档。

    **权威判据是 ``schema_version``**——P1 写入时记的格式版本，认得出来
    就按它分，不做任何推断。认不出来（现存 38 份历史存档全如此）才退回
    :data:`LEGACY_MARKER` 那个**缺件代理**。

    ⚠️ 无版本号的档名**不写「旧批次/当前批次」**：这两档在现存数据上
    出自同一次采集，写成年代就是在替没有依据的数据断言新旧。理由见
    :data:`BATCH_UNVERSIONED` 的注释。
    """
    version = str(getattr(sample, "schema_version", "") or "")
    if version:
        return f"v{version}"
    degraded = getattr(sample, "degraded_from", ()) or ()
    return (BATCH_UNVERSIONED_MISSING if LEGACY_MARKER in degraded
            else BATCH_UNVERSIONED)


def _has_unversioned(rows: list[dict[str, Any]]) -> bool:
    """本批**有没有样本的档位是代理出来的**。

    ⚠️ **不能靠 :func:`_batch_basis` 的返回值判断**——那是个给人读的
    字符串，混合档返回「混合…」不含「无版本号」，于是
    ``basis.startswith("无版本号")`` 为假、代理警告消失，而那批样本
    仍在用缺件代理分档。**恰恰是新存档落盘的那一刻（混合档）警告自动
    没了**，正是最该警告的状态变成最安静的状态。判据直接从 ``rows`` 取：
    只要有一条 ``schema_version`` 为空就是。
    """
    return any(not str(r.get("schema_version") or "") for r in rows)


def _batch_basis(rows: list[dict[str, Any]]) -> str:
    """这一批的分档**依据**是什么——必须与分档结果一起报出去。

    两种依据的可信度差一个量级：``schema_version`` 是写入时记的事实，
    ``run_config`` 缺失是事后推断。分栏只印数字不印依据，读的人会把
    推断当成事实——而下一批数据里那个推断就可能不成立了。

    混合档的措辞**必须带上「哪一半是推断的」**：只写「混合」等于把两半
    平权，读的人会以为整张分栏都升级成事实判据了。
    """
    versions = {str(r.get("schema_version") or "") for r in rows}
    if len(versions) > 1:
        return ("混合（有版本号的按 schema_version 分；"
                "无版本号的那部分按缺件代理分档）")
    only = next(iter(versions)) if versions else ""
    if only:
        return f"schema_version={only}"
    return f"无版本号，按 degraded_from 含 {LEGACY_MARKER!r} 推测"


def evaluate(root: pathlib.Path, dims: list[str], *, limit: int | None = None,
             per_sample: bool = False, undetermined_only: bool = False
             , include_llm_judge: bool = False,
             ) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """跑一遍，返回 ``(汇总, 逐条记录)``。

    ⚠️ **分档不改变总分母**。``dimensions[dim]`` 仍是两档加在一起的全量——
    把旧批次从分母里拿掉就叫「剔除难看的」，那正是
    「不记批次元数据就会读成『质量变好了』」的另一种做法，而且更难发现：
    剔除后报表上每个数字都好看，没人知道曾经少算了什么。
    分档是**把差异显示出来**，不是把它藏起来。

    Args:
        include_llm_judge: 把 :data:`LLM_SCORERS`（D-3 / D-9）合并进 dispatch
            ——只有显式 ``--include-llm-judge`` 时为 True。LLM judge 走
            ``llm_client`` 为 ``None`` 的快路径，所有 verdict 均回 ``undetermined``
            或 ``not_evaluated``——这是设计行为，不是 bug；拿到黄金集接入 LLM 后
            才会出 ``scored`` 档。
    """
    #: LLM judge 仅在 ``--include-llm-judge`` 时并入 dispatch。默认 false 与
    #: :data:`SCORERS` 独立——见 ``test_没有把SCORERS改坏`` 契约。
    dispatch: dict[str, Callable[[Any], Any]] = (
        {**SCORERS, **LLM_SCORERS} if include_llm_judge else SCORERS
    )
    tally: dict[str, collections.Counter] = {d: _agg() for d in dims}
    #: ``批次 → 维度 → 计分``。只用于报表分栏，不参与总分。
    #: 键是动态的（有版本号的批次叫 ``v2``，没版本号的才退回两档），
    #: 所以这里不预置。
    by_batch: dict[str, dict[str, collections.Counter]] = {}
    batch_n: collections.Counter = collections.Counter()
    vetoes: list[dict[str, Any]] = []
    rows: list[dict[str, Any]] = []

    files = _picked(root, limit)
    n_samples = 0
    for path in files:
        try:
            samples = list(split_archive(path))
        except Exception as exc:                       # 单份存档坏掉不拖垮整批
            print(f"  [skip] {path.name} 切样本失败："
                  f"{type(exc).__name__}: {exc}", file=sys.stderr)
            continue
        for sample in samples:
            n_samples += 1
            batch = _batch_of(sample)
            batch_n[batch] += 1
            by_batch.setdefault(batch, {d: _agg() for d in dims})
            outcome = getattr(sample, "outcome", None)
            row: dict[str, Any] = {"archive": path.name,
                                   "sample_id": sample.sample_id,
                                   "batch": batch,
                                   "schema_version": str(
                                       getattr(sample, "schema_version", "") or ""),
                                   "degraded_from": list(
                                       getattr(sample, "degraded_from", ()) or ()),
                                   # ⚠️ 这一项**必须单独带出来**：
                                   # ``degraded_from`` 只记存档级缺件
                                   # （run_config / user_prompt / 正文），
                                   # 而 D-6 不可判定的主因在这里——
                                   # 老存档的 outcome 压根没有
                                   # ``site_url`` / ``play_page_url`` **键**。
                                   # 实测 16 份存档的 74 个 outcome 全无此键，
                                   # 就是它们把 D-6 顶成 100% 不可判定。
                                   # 缺键与空值在 P2 里同形，只有这里分得开。
                                   "p1_missing_fields": list(
                                       getattr(outcome, "p1_missing_fields",
                                               ()) or ())}
            for dim in dims:
                verdict = dispatch[dim](sample)
                row[dim] = verdict.to_json()
                key = str(verdict.score)
                tally[dim][key] += 1
                by_batch[batch][dim][key] += 1
                if getattr(verdict, "is_veto", False):
                    raw_branch = getattr(outcome, "branch", None)
                    vetoes.append({
                        "dimension": f"D-{dim[1:]}",   # 写死 d5 就等于假定
                        # 「一票否决只可能出在这一维」。今天成立，但下一维
                        # 拿到否决权时会被静默漏掉——清单里看不见的东西没人补。
                        "archive": path.name,
                        "sample_id": sample.sample_id,
                        # ⚠️ question/branch 挂在 **outcome** 上，不在 sample 上——
                        # 挂在 sample 上取到的一律是空串，而一票否决清单里
                        # 「有理由但看不到判的是哪道题」等于没报。
                        #
                        # ⚠️ branch=None 是**「这条判成功」**（成功样本的
                        # branch 恒为 None），与「字段没填」不是一回事——
                        # 渲染成空串就把两者合并了，清单里会读到「分支是空的」。
                        "question": str(getattr(outcome, "question", "") or ""),
                        "branch": ("(成功)" if raw_branch is None
                                   else str(raw_branch)),
                        "url": str(getattr(outcome, "url", "") or ""),
                        "reasons": list(verdict.reasons),
                    })
            rows.append(row)

    summary = {
        "archives": len(files),
        "samples": n_samples,
        "dimensions": {},
        # ⚠️ ``basis`` 与 ``batches`` 一起构成「批次口径」的完整说明。
        # 只给数字不给依据，读的人无从判断这个分档是按什么切的，
        # 而依据从「推断」变成「事实」的那天，报表上没有任何东西会提醒他。
        "batch_split": {"basis": _batch_basis(rows),
                        # ⚠️ ``proxied`` 与 ``basis`` 一起构成完整说明，
                        # 且它**不能从 basis 的字符串推出来**——混合档的
                        # basis 是「混合…」，不提示哪一半在用推断。见
                        # :func:`_has_unversioned`。
                        "proxied": _has_unversioned(rows),
                        "counts": dict(batch_n)},
        "batches": {},
        "vetoes": vetoes,
    }
    for dim in dims:
        c = tally[dim]
        scored = {k: v for k, v in c.items() if k != "None"}
        summary["dimensions"][dim] = {
            "dimension": f"D-{dim[1:]}",
            "scored": sum(scored.values()),
            "undetermined": c.get("None", 0),
            "distribution": {k: scored[k] for k in sorted(scored)},
        }
    for batch, tallies in by_batch.items():
        summary["batches"][batch] = {
            "samples": batch_n[batch],
            "dimensions": {
                dim: {
                    "dimension": f"D-{dim[1:]}",
                    "scored": sum(v for k, v in c.items() if k != "None"),
                    "undetermined": c.get("None", 0),
                    "distribution": {k: c[k] for k in sorted(c) if k != "None"},
                }
                for dim, c in tallies.items()
            },
        }
    return summary, rows


def _print_batches(summary: dict[str, Any]) -> None:
    """按档打印。**分母仍是全量**——这里只解释差异从哪来。

    ⚠️ 标题不叫「批次分栏」：无版本号时那两档切的是**字段齐不齐**，
    不是批次。叫批次就是在给读的人一个它拿不到的保证。
    """
    split = summary.get("batch_split") or {}
    batches = summary.get("batches") or {}
    if len(batches) < 2:
        # 只有一档时不打印：空的分栏只会让人去找「另一档在哪」，
        # 找不到就以为分档功能坏了。
        return
    basis = str(split.get("basis") or "")
    # ⚠️ 读 summary 里的布尔，**不拿 basis 的字符串前缀判**——见
    # :func:`_has_unversioned`：混合档恰恰是最该打警告的那一档。
    proxied = bool(split.get("proxied"))

    print(f"\n  ── 分档（判据：{basis}）──")
    print(f"     ⚠ 各档都进上面的总分母，分档只用来定位差异来源")
    if proxied:
        print(f"     ⚠ **有样本没有 schema_version，它的档位是按缺件推测的**"
              f"——这批上它与「老」同向，换一批就不一定；"
              f"「为什么不可判定」看下面的缺件分布，那张表是事实")
    for name, b in batches.items():
        n = b.get("samples") or 0
        if not n:
            print(f"     {name}：0 条")
            continue
        print(f"     {name}（{n} 条）")
        for dim, st in (b.get("dimensions") or {}).items():
            total = st["scored"] + st["undetermined"]
            pct = st["undetermined"] / total if total else 0.0
            dist = " ".join(f"{k}分 {v}" for k, v in st["distribution"].items())
            print(f"       {st['dimension']}   出分 {st['scored']:>4}/{total}"
                  f"   不可判定 {st['undetermined']:>4} ({pct:>4.0%})   {dist}")
    # 「老批次跑在字段补齐之前」只在**确实存在版本档**时才讲——
    # 全批都没版本号时这句话是在解释一个不存在的分界。
    unversioned = [n for n in batches if not n.startswith("v")]
    if unversioned and len(unversioned) < len(batches):
        print(f"     {'/'.join(unversioned)} 的不可判定多半不是轨迹的问题，"
              f"是它跑在字段补齐之前——那批不重跑就别拿它判质量")


#: 缺件标记的两个来源。都读 ``split_archive`` **当场算出的结果**：
#: ``outcome.p1_missing_fields`` 记「P1 的 outcome 里压根没这个键」，
#: ``sample.degraded_from`` 记存档级缺件。**它们是事实**——键在不在
#: 不需要推断。跟分档那套推断分开摆，是因为「键不存在」能被核，
#: 「这批数据多老」不能。
MARKER_FIELDS = (("outcome", "p1_missing_fields"),
                 ("archive", "degraded_from"))


def _marker_stats(rows: list[dict[str, Any]],
                  dims: list[str]) -> dict[str, dict[str, Any]]:
    """每种缺件标记命中多少条样本，以及这些样本在各维度上的出分。

    这张表回答的是「哪些不可判定是采集缺件造成的」——分档回答不了，
    它只切一刀，而缺件有好几种且互相独立。
    """
    stats: dict[str, dict[str, Any]] = {}
    for r in rows:
        for scope, field in MARKER_FIELDS:
            for marker in r.get(field) or ():
                slot = stats.setdefault(
                    key := f"{scope}.{marker}",
                    {"samples": 0, "scored": {d: 0 for d in dims}})
                slot["samples"] += 1
                for d in dims:
                    if r[d]["kind"] == "scored":
                        slot["scored"][d] += 1
    return stats


def _why_lines(verdict: Mapping[str, Any]) -> list[str]:
    """一条判定要印的理由行：**判错的那几条先印**，再印说明性的 ``reasons``。

    ⚠️ 只印 ``reasons`` 会出现「判了 2 分、一个字理由都没有」的行——
    ``reasons`` 里装的是 ``skipped`` / ``undetermined`` 的说明（即
    「这条为什么没判」），**判低分的依据在 ``checks`` 里**。2026-10-23
    D-5 有了第一条会判低分的检查（结论的证据核），这个缺口立刻显形：
    10 条 2 分样本在明细里干净得像评分器乱打分，读的人无法复核。

    ``name`` / ``node`` 两种键都认：D-4 的检查字段叫 ``name``，
    D-5 的叫 ``node``——两个评分器各自演进，这里不逼它们统一。
    """
    out: list[str] = []
    for c in verdict.get("checks") or ():
        if c.get("kind") == "wrong":
            node = c.get("node") or c.get("name") or "（未命名检查）"
            out.append(f"✗ {node}：{c.get('why', '')}")
    out.extend(verdict.get("reasons") or ())
    return out[:2]


def _print_markers(summary: dict[str, Any],
                   rows: list[dict[str, Any]]) -> None:
    """印缺件分布。**这一张是事实**，与分档的推断相对。

    独立于分档打印：只有一档时没有分栏，但缺件分布照样有话可说，
    而且那正是此时唯一能解释不可判定的东西。
    """
    label = {d: st["dimension"] for d, st in summary["dimensions"].items()}
    dims = list(label)
    total = len(rows)

    def line(name: str, n: int, scored: dict[str, int], denom: int) -> str:
        parts = "  ".join(f"{label[d]} {scored[d]:>3}/{denom}" for d in dims)
        return f"     {name:30s} {n:>4} 条   {parts}"

    whole = {d: sum(1 for r in rows if r[d]["kind"] == "scored") for d in dims}
    stats = _marker_stats(rows, dims)

    print(f"\n  ── 缺件分布（P1 里压根没这个键 = 事实；分档 = 推断）──")
    if not stats:
        print(line("— 无任何缺件 —", total, whole, total))
        return
    for key, slot in sorted(stats.items(), key=lambda kv: -kv[1]["samples"]):
        print(line(key, slot["samples"], slot["scored"], slot["samples"]))
    # 对照行放最后：每行的分母是组内条数，与对照行一比就知道
    # 「缺这个键贡献了多少不可判定」。
    print(line("— 全部样本（对照）—", total, whole, total))


def _print(summary: dict[str, Any], rows: list[dict[str, Any]],
           *, per_sample: bool, undetermined_only: bool) -> None:
    dims = summary["dimensions"]
    label = {d: st["dimension"] for d, st in dims.items()}
    print(f"== 评估 · {summary['archives']} 份存档 → "
          f"{summary['samples']} 条样本 ==")
    for dim, st in dims.items():
        total = st["scored"] + st["undetermined"]
        pct = st["undetermined"] / total if total else 0.0
        dist = " ".join(f"{k}分 {v}" for k, v in st["distribution"].items())
        mark = ""
        if st["scored"] == 0 and total:
            mark = "  ⚠ **全维不可判定**"
        elif pct > 0.5:
            mark = "  ⚠ 不可判定过半"
        print(f"  {st['dimension']}   出分 {st['scored']:>4}/{total}"
              f"   不可判定 {st['undetermined']:>4} ({pct:>4.0%})"
              f"   {dist}{mark}")

    # ── 分档 + 缺件分布 ───────────────────────────────────────────
    # 放在全量之后、否决之前：读报表的人先看到「总的怎样」，再看到
    # 「其中哪一段被什么带下来的」。顺序反过来，读的人会先被分档
    # 引导去归因，忘了先看全貌——而全貌才是要交付的那个数。
    # 分档在前（推断），缺件分布紧随（事实）：先说「这是猜的」，再给
    # 「能核的」——顺序反过来，读的人会把推断当结论。
    _print_batches(summary)
    _print_markers(summary, rows)

    vetoes = summary["vetoes"]
    if vetoes:
        by_dim = collections.Counter(v["dimension"] for v in vetoes)
        print(f"\n  🚨 一票否决 {len(vetoes)} 条  " + "  ".join(
            f"{d} {n}" for d, n in sorted(by_dim.items())) + "：")
        for v in vetoes[:10]:
            print(f"     [{v['dimension']}] {v['sample_id']:40s} "
                  f"branch={v['branch']!r} question={v['question']!r}")
            for line_ in _why_lines(v):
                print(f"        {line_}")
        if len(vetoes) > 10:
            print(f"     …还有 {len(vetoes) - 10} 条（--json 看全量）")
    else:
        print("\n  一票否决 0 条")

    if per_sample or undetermined_only:
        # 逐条明细也要带档位标记，否则与上面的分栏对不上——
        # 读的人拿着「D-6 缺 run_config 那档 100% 不可判定」去明细里找，
        # 一条也找不到。
        # 整批同档时不加这一列：满屏 ``当`` 字只会挤掉真正要看的分数。
        show_batch = len(summary.get("batches") or {}) > 1
        print()
        for row in rows:
            if undetermined_only:
                # 与 ``--help`` 一致：**不可判定 *或* 有扣分**。
                # 只按 kind 过滤会让判了低分的行一条都不出现——而
                # 「扣分」正是这个开关存在的理由之一（2026-10-23 修：
                # 10 条 2 分样本在 ``--only-undetermined`` 下全被跳过）。
                if all(
                    row[d]["kind"] == "scored" and (row[d]["score"] or 0) >= 5
                    for d in row
                    if d in summary["dimensions"]
                ):
                    continue
            bad = [d for d in dims
                   if row[d]["kind"] != "scored" or row[d]["score"] < 5]
            if not bad and not per_sample:
                continue
            scores = " ".join(
                f"{d}={row[d]['score'] if row[d]['score'] is not None else '—'}"
                for d in dims)
            mark = f"[{row.get('batch')}] " if show_batch else ""
            print(f"  {row['sample_id']:44s} {mark}{scores}")
            for d in bad:
                for line_ in _why_lines(row[d]):
                    print(f"      {label[d]} {line_}")


def main(argv: list[str] | None = None) -> int:
    # Windows 控制台默认 GBK，编不出 ``⚠`` / ``—`` / ``→`` 这些符号——
    # 实测直接 ``UnicodeEncodeError`` 崩在第一行分数上，**报表一个字都看不到**。
    # ``executor.cli`` 早就这么做了，本模块是漏的。``try`` 包住是因为
    # pytest 的 capsys 会把 stdout 换成不带 ``reconfigure`` 的对象。
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except (AttributeError, ValueError):
        pass
    ap = argparse.ArgumentParser(
        prog="python -m trajectory_pipeline.evaluation.cli",
        description="跑 D-2/D-4/D-5/D-6 四个代码评分器")
    ap.add_argument("--out", default=str(DEFAULT_ROOT_PATH),
                    help="P1 存档目录（默认 output/pipeline，优先取 .reviewed.json）")
    ap.add_argument("--dimension", default="d2,d4,d5,d6",
                    help="逗号分隔，默认全跑。可选 " + ",".join(SCORERS))
    ap.add_argument("--limit", type=int, default=None, help="只跑前 N 份存档")
    ap.add_argument("--per-sample", action="store_true", help="逐条列出")
    ap.add_argument("--only-undetermined", action="store_true",
                    help="只列不可判定或有扣分的样本")
    ap.add_argument("--include-llm-judge", action="store_true",
                    help="启用 LLM judge（D-3 / D-9）——**默认关**。"
                         "生产链上禁止 judge 与代码 judge 混合解读，"
                         "见 :data:`LLM_SCORERS` 模块级注释。")
    ap.add_argument("--json", dest="json_out", default=None,
                    help="把结果落成 JSON")
    args = ap.parse_args(argv)

    dims = [d.strip().lower() for d in args.dimension.split(",") if d.strip()]
    #: 默认维度池 = :data:`SCORERS`（代码 judge 4 个）。带 ``--include-llm-judge``
    #: 时并入 :data:`LLM_SCORERS`（D-3 / D-9）。两个 dict 仍分开维护——见各自
    #: 模块级注释。
    available = set(SCORERS)
    if args.include_llm_judge:
        available |= set(LLM_SCORERS)
    unknown = [d for d in dims if d not in available]
    if unknown:
        print(f"未知维度 {unknown}；可选 {sorted(available)}", file=sys.stderr)
        return 2

    root = pathlib.Path(args.out)
    if not root.is_dir():
        print(f"{root} 不是目录", file=sys.stderr)
        return 2

    summary, rows = evaluate(root, dims, limit=args.limit,
                             per_sample=args.per_sample,
                             undetermined_only=args.only_undetermined,
                             include_llm_judge=args.include_llm_judge)
    _print(summary, rows, per_sample=args.per_sample,
           undetermined_only=args.only_undetermined)

    if args.json_out:
        p = pathlib.Path(args.json_out)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps({"summary": summary, "samples": rows},
                                ensure_ascii=False, indent=1), encoding="utf-8")
        print(f"\n→ 已写 {p}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
