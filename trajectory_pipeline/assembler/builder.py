"""P2 落盘——``assembler.schema.split_archive`` 的产物写出。

设计纪律
--------

1. **切条逻辑不住这里。** :mod:`~trajectory_pipeline.assembler.schema`
   是 :func:`trajectory_pipeline.assembler.schema.split_archive` 的
   **唯一**所有者。builder 只负责「把内存里的 :class:`Sample` 变成磁盘上的
   JSON 文件」——切错了修 schema，不在 builder 里打补丁。
2. **读 P1 按文件格式，不 import executor。** 与 ``assembler/schema.py``
   同一条纪律。理由（见 ``01-架构总览.md`` §模块 5）：P1 贵、P2 便宜，
   重建一批 P2 必须独立于 executor 的当前版本。由
   ``tests/contract/test_p2_contract.py::TestNoExecutorImport`` 用 AST 强制。
3. **训练态观察走 :mod:`~trajectory_pipeline.assembler.observation_view`，
   不重新发明裁剪。** P1 是**取证态**（全文 6000 字符、80 元素、50 链接），
   P2 是**训练态**（裁到 2000/40/30）。两边各取所需，但裁剪规则只在
   observation_view 一处。builder 调 ``render()``，不调 ``view.limits``。
4. **写原子化。** 先写 ``.json.tmp``、``os.replace`` 覆盖目标。
   批次中断不会留下半截 P2 文件——而半截 P2 比缺 P2 更难查（文件名
   存在但读不出 JSON）。
5. **样本级错误不打断整批。** 一条样本序列化失败，**记进 ``skipped`` 返回**，
   后续样本继续；不让「这一条 body 里恰好有 U+FFFD」让整批重建作废。
   选档由调用方决定下一步（典型：记一行警告、重跑这条 source_archive）。

落盘形状
--------

::

    output/pipeline/samples/<sample_id>.json

``sample_id`` 已含 ``__``（``task_id__run_id__unit``），目录扁平无嵌套。
一份存档切出 N 条 → N 个文件、id 全在该 P1 唯一对应，**没有**「按 task
分组二级目录」。

返回
----

:class:`BuildReport`——按档案分别计切条数、跳过数、降级样本数。
调用方拿这份报告打日志 / 写进 batch metadata / 触发门禁。
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from trajectory_pipeline.assembler import observation_view
from trajectory_pipeline.assembler.schema import (
    RATIONALE_ERA_POST_HOC,
    RATIONALE_ERA_PRE_ACTION,
    Sample,
)

#: 样本落盘根。**与 :data:`trajectory_pipeline.executor.archive.DEFAULT_ROOT`
#: 同根**：所有产物收在 ``trajectory_pipeline/output/pipeline/``，与
#: 存量 v1 的 ``output/`` 完全隔离（D8）。
#:
#: 刻意**自定字面量**而不 import executor：assembler 不 import executor 是
#: 契约（``tests/contract/test_p2_contract.py::TestNoExecutorImport``），
#: 改 executor 的产物根时**也**要同步改这里——靠人盯。
DEFAULT_SAMPLES_PARENT = "pipeline"
DEFAULT_SAMPLES_DIRNAME = "samples"


def samples_dir(root: str | Path | None = None) -> Path:
    """``samples/`` 目录的绝对路径。

    ``root=None`` 时取 ``trajectory_pipeline/output/pipeline/samples/``——
    与 :data:`trajectory_pipeline.executor.archive.DEFAULT_ROOT` 同根，
    但**不走 executor 的 import 边界**（assembler 不 import executor 是契约）。

    ⚠️ **``parents[1]`` 不是 ``parents[2]``**：``__file__`` 在
    ``trajectory_pipeline/assembler/builder.py``，``parents[1]`` 才是
    ``trajectory_pipeline/`` 包根。写成 ``parents[2]`` 会落到**仓库根**的
    ``output/pipeline/``——CLAUDE.md 明令禁止碰的存量产物目录，
    而且**不报错**：``build_all`` 只是遍历到空目录、返回 0 条，
    看起来像「这批没切出样本」。由
    ``tests/unit/test_builder.py::Test产物根`` 盯住。
    """
    if root is None:
        root = Path(__file__).resolve().parents[1] / "output" / DEFAULT_SAMPLES_PARENT
    return Path(root) / DEFAULT_SAMPLES_DIRNAME


# ── 报告 ──────────────────────────────────────────────────────────

#: ``--rationale-era`` 的「不过滤」取值。**不写成空串**：空串在
#: :attr:`~trajectory_pipeline.assembler.schema.Sample.rationale_era`
#: 里已经有含义（**没有可判的 rationale**），拿它同时表示「全部」会让
#: 两个概念共用同一个字面量，而报表上分不出「筛了但没有」与「没筛」。
ERA_ALL = "all"


@dataclass(frozen=True, slots=True)
class ArchiveBuildResult:
    """单份 P1 存档的落盘结果。"""

    archive: str
    sample_count: int = 0
    written: int = 0
    skipped: tuple[str, ...] = ()  # 序列化失败 / 被口径筛掉的 sample_id
    degraded: int = 0  # degraded_from 非空的条数
    negatives: int = 0  # outcome.branch ∈ 真负样本分支的条数
    #: 切出来的样本按 rationale 口径分代计数（**筛之前**的账）。
    #: 筛掉的也计进来：这份报表要说的是「这批数据是什么」，
    #: 而不只是「这次写了什么」。
    pre_action: int = 0
    post_hoc: int = 0
    era_skipped: int = 0  # 被 ``--rationale-era`` 筛掉的条数


@dataclass(frozen=True, slots=True)
class BuildReport:
    """整批落盘汇总。"""

    archives: tuple[ArchiveBuildResult, ...]
    total_samples: int = 0
    total_written: int = 0
    total_skipped: int = 0
    sample_dir: str = ""
    total_pre_action: int = 0
    total_post_hoc: int = 0
    total_era_skipped: int = 0

    @property
    def ok(self) -> bool:
        """没有任何样本被跳过。样本数与写出数相等就 OK。"""
        return self.total_skipped == 0

    @property
    def mixed_era(self) -> bool:
        """这一批里**两种口径同时存在**。

        混批不是「有点小瑕疵」：模型看到的是「有时可以未卜先知、有时不行」，
        而报表上表现为分数略降——比全错更难发现。所以调用方必须把它
        印出来，不能只在数字里存在（见 ``cmd_build``）。
        """
        return self.total_pre_action > 0 and self.total_post_hoc > 0

    def by_archive(self) -> Mapping[str, ArchiveBuildResult]:
        return {a.archive: a for a in self.archives}


# ── 单样本 → JSON ──────────────────────────────────────────────────


def _sample_to_json(sample: Sample) -> dict[str, Any]:
    """把 :class:`Sample` 序列化成可落盘 dict。

    训练态观察是 :func:`observation_view.render` 的产物——P1 全文元素
    在这里裁到 ``VIEW_BODY_CHARS / VIEW_ELEMENTS / VIEW_LINKS``，
    裁剪项如实带进 ``limits``。**不**直接 ``sample.to_json()`` 的
    ``observations`` 字段，因为那条会保留 P1 全文——而 P2 是给模型看的，
    不是给门禁看的。
    """
    base = sample.to_json()
    base["six"]["observations"] = observation_view.render_all(list(sample.observations))
    return base


# ── 单条样本落盘 ──────────────────────────────────────────────────


def _write_one(
    sample: Sample,
    target_dir: Path,
) -> tuple[bool, str | None]:
    """写一条样本到 ``target_dir / <sample_id>.json``。

    Returns:
        ``(ok, skip_reason)``——写成功 ``(True, None)``；
        失败 ``(False, reason)``，**不抛异常**，让整批不被一条样本卡住。
    """
    sample_id = sample.sample_id
    target = target_dir / f"{sample_id}.json"
    tmp = target_dir / f"{sample_id}.json.tmp"
    try:
        blob = json.dumps(_sample_to_json(sample), ensure_ascii=False, allow_nan=False)
    except (TypeError, ValueError) as exc:
        return False, f"序列化失败：{type(exc).__name__}: {exc}"
    try:
        target_dir.mkdir(parents=True, exist_ok=True)
        tmp.write_text(blob, encoding="utf-8")
        os.replace(tmp, target)
    except OSError as exc:
        # 残留的 .tmp 留着无所谓——下次重建会覆盖。**不删**：删错了
        # 等于把唯一能恢复的中间产物一并擦掉。
        return False, f"写盘失败：{type(exc).__name__}: {exc}"
    return True, None


# ── 分支判定 ──────────────────────────────────────────────────────


#: 真负样本分支。**与 :data:`trajectory_pipeline.executor.branches.NON_SAMPLE_BRANCHES`
#: 和 :data:`trajectory_pipeline.executor.branches.COLLECTION_SIDE_BRANCHES`
#: 互补**：这里列的是「既不是『没判出来』、也不是『这次没看到』、
#: **确实进负样本池**」的那部分。
#:
#: 必须自己持有一份而**不 import executor**——P2 不 import executor 是契约
#: （P2 同样不 import 上游的 ``perception``：P2 在感知层下游）。
#: 两份表的**同步靠测试强制**（``tests/unit/test_builder.py`` 里
#: ``test_NEGATIVE_SAMPLE_BRANCHES与executor对得上``）——那张测试断言的是
#: **集合相等**，少一支多一支都红。
#:
#: ⚠️ **这张表少一支的后果是「报表少算」，不是「样本不入池」**——
#: 并池走 executor 侧
#: :attr:`~trajectory_pipeline.executor.branches.SiteOutcome.is_negative_sample`
#: （两段排除），与本表**各算各的**。2026-10-28 实测：本表漏了
#: ``subscription_wall`` 等 8 支 ⇒ ``cli build`` 报「真负样本 154 条」，
#: 而 P2 样本里属真负分支的实为 306 条 ⇒ **少算 152 条**
#: （113 ``subscription_wall`` + 37 ``login_wall_or_blocked``
#: + 2 ``trailer_only``）；``cli check-archives`` 用的是 executor 侧判据
#: ⇒ **两个报表对同一件事给出两个数**，读的人只能猜哪个对。
#:
#: 改名要同时改 :mod:`trajectory_pipeline.executor.branches.BRANCH_LABELS`。
#:
#: ⚠️ **``blocked`` 不在这张表里**（2026-10-26 · R1）。``NON_SAMPLE_BRANCHES``
#: 之外还有**第二段排除**：executor 侧
#: :data:`trajectory_pipeline.executor.branches.COLLECTION_SIDE_BRANCHES`
#: ——「这次没看到」不是「这里看不了」。一次反爬拦截对站点性质零信息
#: （同一 camoufox 指纹、同一天，10 个判过 CF 挑战的目标里 6 个当天
#: 就进得去），而四周前同一机制产出的 4 条「CF 挑战页 ⇒ no_play_control」
#: 正是这套判据下最贵的那类假负样本。
NEGATIVE_SAMPLE_BRANCHES: frozenset[str] = frozenset(
    {
        # ── 判断点 ①：候选本身不是「这里看不了」的地方 ──
        "not_play_site",  # 不是播放站点
        "not_aggregator",  # 不是影视聚合站 / 导航站
        "aggregator_no_entries",  # 聚合站上没有正式影视站入口
        "not_article",  # 不是文章 / 社区讨论页
        "article_no_links",  # 文章页没有可引荐的影视站外链
        "no_site_search",  # 首页型候选没有站内搜索框
        "site_search_no_results",  # 站内搜索未产出任何子候选
        # ── 判断点 ②：站本身的问题 ──
        "unreachable_hard",  # 站点不可达（DNS 解析失败 / 连接被拒）
        "broken",  # 内容损坏（404 / 媒体不可解码 / duration=0）
        "login_required",  # 登录后可免费观看（agent 无登录态）
        "subscription_wall",  # 有播放页，但需订阅/付费
        # ── 判断点 ③：页面上没有播放控件 ──
        "no_play_control",
        # ── 判断点 ④：控件在，但确认不了能不能用 ──
        "component_unverified",
        "partial_free",  # 有免费部分（试看/预告），完整内容需付费
        "trailer_only",  # 只有预告片，播放资源不存在
        # ── 已废弃的合并态：仍在池里（34 行）⇒ 仍算真负样本 ──
        "login_wall_or_blocked",
    }
)


def _is_negative(branch: str | None) -> bool:
    """``branch`` 是不是真负样本。``None`` = 成功，不是负样本。"""
    return branch is not None and branch in NEGATIVE_SAMPLE_BRANCHES


# ── 单份 P1 → N 个 P2 ─────────────────────────────────────────────


def build_from_archive(
    archive_path: str | Path,
    *,
    root: str | Path | None = None,
    overwrite: bool = False,
    era_filter: str = ERA_ALL,
) -> ArchiveBuildResult:
    """把一份 P1 存档切成 N 条样本并落盘。

    Args:
        archive_path: P1 存档路径（``T001__<run_id>.json``，也可以是
            ``<run_id>.reviewed.json``——**由调用方决定读哪一份**，
            见 :func:`build_all`）。
        root: 输出根；``None`` 时取 executor 的 :data:`DEFAULT_ROOT`。
        overwrite: 目标 P2 已存在时是否覆盖。**默认 False**——重建批
            不该悄悄把别人刚标好的样本抹掉。批量重建时显式传 True。
        era_filter: 只落这一代口径的样本（:data:`ERA_ALL` = 不筛）。
            用它是为了**构建同口径批次**：混批的伤害见
            :attr:`BuildReport.mixed_era`。筛掉的记进 ``skipped``
            并单独计数，**不静默消失**。

    Returns:
        单份存档的落盘结果。**异常只发生在档本身坏到切不出**（已
        :class:`P1ShapeError` 形态）；**写盘失败**走 :attr:`ArchiveBuildResult.skipped`。
    """
    p = Path(archive_path)
    out_dir = samples_dir(root)
    out_dir.mkdir(parents=True, exist_ok=True)

    name = p.name
    samples = list(_split_one(p))

    written = 0
    skipped: list[str] = []
    degraded = 0
    neg = 0
    era_skipped = 0
    # 分代账按**切出来的样本**记，不受过滤与 overwrite 影响：
    # 它描述的是「这份存档里有什么」。
    pre_action = sum(1 for s in samples
                     if s.rationale_era == RATIONALE_ERA_PRE_ACTION)
    post_hoc = sum(1 for s in samples
                   if s.rationale_era == RATIONALE_ERA_POST_HOC)
    for s in samples:
        if era_filter != ERA_ALL and s.rationale_era != era_filter:
            era_skipped += 1
            skipped.append(f"{s.sample_id} (rationale_era={s.rationale_era or '无'})")
            continue
        if not overwrite and (out_dir / f"{s.sample_id}.json").exists():
            # 不覆盖：等价于「这条已落盘」。计入 skipped 以便调用方感知
            # 「没有真重建」。理由见 overwrite 默认值注释。
            skipped.append(s.sample_id)
            continue
        ok, reason = _write_one(s, out_dir)
        if not ok:
            assert reason is not None
            skipped.append(f"{s.sample_id} ({reason})")
            continue
        written += 1
        if s.degraded_from:
            degraded += 1
        if s.outcome is not None and _is_negative(s.outcome.branch):
            neg += 1
    return ArchiveBuildResult(
        archive=name,
        sample_count=len(samples),
        written=written,
        skipped=tuple(skipped),
        degraded=degraded,
        negatives=neg,
        pre_action=pre_action,
        post_hoc=post_hoc,
        era_skipped=era_skipped,
    )


def _split_one(p: Path) -> Iterable[Sample]:
    """对单文件调 :func:`split_archive` 并校验。坏档抛 :class:`P1ShapeError`。"""
    from trajectory_pipeline.assembler.schema import split_archive, P1ShapeError

    try:
        return list(split_archive(p))
    except P1ShapeError:
        raise
    except (OSError, ValueError) as exc:
        # JSON 坏 / 文件读不到——门禁 :func:`check_archive` 会把它报成
        # FATAL。builder 这里照样抛出去，让上游能立刻看到。
        raise P1ShapeError(f"{p.name}: 读不出 P1 存档（{exc}）") from exc


# ── 全目录批量 ──────────────────────────────────────────────────


def build_all(
    source_root: str | Path | None = None,
    *,
    target_root: str | Path | None = None,
    overwrite: bool = False,
    archive_filter: Sequence[str] | None = None,
    era_filter: str = ERA_ALL,
) -> BuildReport:
    """把一批 P1 存档全切全落。

    Args:
        source_root: 读 P1 的根，``None`` = executor 的 :data:`DEFAULT_ROOT`。
        target_root: 写 P2 的根，``None`` = 同上。
        overwrite: 同 :func:`build_from_archive`。
        archive_filter: 只处理这些 archive 名（不含目录）。``None`` = 全部
            （按文件名排序）。**不传 ``__`` 的探测取证自动跳过**。
            ``.reviewed.json`` 的处理分两种：``None`` 时跳过（与
            :func:`iter_samples` 同一条纪律：不在这里重选）；**显式列进
            过滤名单时照读**——那份名单的挑选权已经交给了调用方
            （生产路径是 ``cmd_build`` 用
            :func:`~trajectory_pipeline.executor.archive.select_archives` 解析的，
            复核档优先）。两条口径分叉过一次：重生成写复核档、build 读原档，
            于是「跑完重生成」在 P2 上一个字节都没变。
        era_filter: 同 :func:`build_from_archive`。

    Returns:
        整批 :class:`BuildReport`。**单份 P1 切不出样本不打断整批**——
        那种坏档已在门禁 :func:`check_archive` 里被报成 FATAL，builder
        这边只把坏档**记进 :attr:`BuildReport.archives` 的 skipped**，
        后续存档继续。
    """
    from trajectory_pipeline.assembler.schema import P1ShapeError

    if source_root is None:
        # 与 :func:`samples_dir` 的根路径**同一份字面量**（不重算一遍
        # ``parents[n]``——两处各算一次就会漂，而漂了不报错）。
        src = Path(__file__).resolve().parents[1] / "output" / DEFAULT_SAMPLES_PARENT
    else:
        src = Path(source_root)

    out_dir = samples_dir(target_root or source_root)
    out_dir.mkdir(parents=True, exist_ok=True)

    allowed = set(archive_filter) if archive_filter is not None else None

    per_archive: list[ArchiveBuildResult] = []
    total_samples = 0
    total_written = 0
    total_skipped = 0
    total_pre_action = 0
    total_post_hoc = 0
    total_era_skipped = 0

    for path in sorted(src.glob("*.json")):
        if "__" not in path.stem:
            continue
        if allowed is None:
            # 没给名单才自己挑：复核档由调用方（``select_archives``）决定。
            if path.name.endswith(".reviewed.json"):
                continue
        elif path.name not in allowed:
            continue
        try:
            res = build_from_archive(
                path,
                root=target_root or source_root,
                overwrite=overwrite,
                era_filter=era_filter,
            )
        except P1ShapeError as exc:
            # 坏档：算 0 切出 0 写出、全部 skipped，让 :class:`BuildReport`
            # 仍能汇总，但坏档信息不丢。
            per_archive.append(
                ArchiveBuildResult(
                    archive=path.name,
                    sample_count=0,
                    written=0,
                    skipped=(f"<bad-archive>: {exc}",),
                    degraded=0,
                    negatives=0,
                )
            )
            total_skipped += 1
            continue
        per_archive.append(res)
        total_samples += res.sample_count
        total_written += res.written
        total_skipped += len(res.skipped)
        total_pre_action += res.pre_action
        total_post_hoc += res.post_hoc
        total_era_skipped += res.era_skipped

    return BuildReport(
        archives=tuple(per_archive),
        total_samples=total_samples,
        total_written=total_written,
        total_skipped=total_skipped,
        sample_dir=str(out_dir),
        total_pre_action=total_pre_action,
        total_post_hoc=total_post_hoc,
        total_era_skipped=total_era_skipped,
    )


# ── 调试 / 校验 ──────────────────────────────────────────────────


def load_sample(path: str | Path) -> dict[str, Any]:
    """读一条 P2 JSON。不做语义校验——落盘形状由 :class:`Sample` 自身的
    序列化方法保证，校验留给契约测试。
    """
    return json.loads(Path(path).read_text(encoding="utf-8"))


__all__ = [
    "DEFAULT_SAMPLES_DIRNAME",
    "ERA_ALL",
    "samples_dir",
    "ArchiveBuildResult",
    "BuildReport",
    "NEGATIVE_SAMPLE_BRANCHES",
    "build_from_archive",
    "build_all",
    "load_sample",
]
