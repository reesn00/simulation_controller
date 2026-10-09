"""persona × 骨架交叉采样 → 可执行的 :class:`TaskInstance` 序列。

一句话职责
----------
**persona 管输入分布**（谁在问、怎么说），**scenario 管后续轮次**（被追问时怎么反应）。
本模块把前者接上控制流，把后者原样带进 provenance。

三种排除，每一种都记账
----------------------
交叉采样的结果不等于"能跑的样本"。三类组合必须显式排除：

============================  =========  ==================================
被排除的组合                   数量      为什么
============================  =========  ==================================
``纯描述`` × ``single_title``    41/44   用户说不出片名却要求找到那部资源，
                                          正确响应是先澄清，W1 无此环节
无片名骨架（aggregate 等）       17/98   W1 成功判据是"找到一个可播放站点页"，
                                          与"找一个集合"不匹配
骨架缺片名 → 空检索式           同上     检索式为空，控制流会退化成搜空串
============================  =========  ==================================

⚠️ **排除必须计数并上报**，理由不是形式主义：排除比例一大，
"本批次 300 条"实际上只来自少数几个骨架，切片表里会出现一条
样本量极大的行和一堆零样本的行——而"哪些组合没被覆盖"这件事
恰恰是评估最需要的输入。静默排除会让报告看起来覆盖良好。
"""

from __future__ import annotations

import random
from dataclasses import dataclass, field
from typing import Any, Mapping, Sequence

from trajectory_pipeline.taskgen import normalizer as nz
from trajectory_pipeline.taskgen.persona import renderer
from trajectory_pipeline.taskgen.persona.library import PersonaLibrary
from trajectory_pipeline.taskgen.persona.schema import PersonaProfile
from trajectory_pipeline.taskgen.skeleton import (
    TaskInstance,
    TaskSkeleton,
    load_skeletons,
    mode_distribution,
)

#: 采样时的哈希盐。改了它，``seed`` 相同也会换一批组合——
#: 所以它必须**当成契约**看待，不能随手调。
SALT: str = "v2.1"


@dataclass(frozen=True, slots=True)
class SamplingReport:
    """采样记账。**每一条都该在看批次结果时被打印出来。**"""

    requested: int
    produced: int
    skeletons_seen: int
    skeletons_w1_runnable: int
    personas_seen: int
    personas_compatible: int
    excluded_by_skeleton_mode: dict[str, int] = field(default_factory=dict)
    #: 全库的检索模式分布。**无条件**记录，与 :attr:`excluded_by_skeleton_mode`
    #: 分开是有原因的：后者只在 ``w1_only=True`` 时非空（真被排除的那些），
    #: 而模式分布是**骨架库的属性**，任何采样参数下都成立。
    #:
    #: 早期版本把两者塞进同一个字段，于是 ``--all-modes`` 下报出
    #: 「98 次因骨架模式排除」——而那次采样**一个都没排除**。
    #: 计数说谎比不计数更坏：读的人会以为真的排掉了 98 个骨架，
    #: 进而以为"剩下 81 个都被用上了"，而实际用的是 98 个里的任意若干。
    mode_distribution: dict[str, int] = field(default_factory=dict)
    excluded_by_compatibility: int = 0
    normalize_accepted: int = 0
    normalize_rejected: int = 0
    skeleton_usage: dict[str, int] = field(default_factory=dict)
    persona_usage: dict[str, int] = field(default_factory=dict)
    by_mode: dict[str, int] = field(default_factory=dict)
    library_digest: str = ""
    rewriter: str = "null"
    #: 采样随机种子。**必须落盘**：计划文件若说不清自己是怎么采出来的，
    #: 那"重跑一批对照"就只能靠碰运气重跑同一个 ``-n``——而 ``-n`` 变了
    #: 组合也全变了。``library_digest`` 只锁住画像库，锁不住抽样本身。
    seed: int | None = None

    def to_json(self) -> dict[str, Any]:
        return {
            "requested": self.requested,
            "produced": self.produced,
            "seed": self.seed,
            "skeletons_seen": self.skeletons_seen,
            "skeletons_w1_runnable": self.skeletons_w1_runnable,
            "personas_seen": self.personas_seen,
            "personas_compatible": self.personas_compatible,
            "excluded_by_skeleton_mode": dict(self.excluded_by_skeleton_mode),
            "mode_distribution": dict(self.mode_distribution),
            "excluded_by_compatibility": self.excluded_by_compatibility,
            "normalize_accepted": self.normalize_accepted,
            "normalize_rejected": self.normalize_rejected,
            "skeleton_usage": dict(self.skeleton_usage),
            "persona_usage": dict(self.persona_usage),
            "by_mode": dict(self.by_mode),
            "library_digest": self.library_digest,
            "rewriter": self.rewriter,
        }

    def warnings(self) -> tuple[str, ...]:
        """采样层的异常信号。**空元组才算健康**。"""
        out: list[str] = []
        if self.produced < self.requested:
            out.append(
                f"要 {self.requested} 条只产出 {self.produced} 条："
                f"排除 {sum(self.excluded_by_skeleton_mode.values())} 次"
                f"（骨架模式）+ {self.excluded_by_compatibility} 次"
                f"（人设不兼容）"
            )
        used = len(self.skeleton_usage)
        if self.produced and used < max(3, self.produced // 10):
            out.append(
                f"{self.produced} 条样本只落在 {used} 个骨架上，"
                f"骨架多样性过低——同片重复采样不增加任何信息量"
            )
        if self.normalize_rejected:
            rate = self.normalize_rejected / max(1, self.produced)
            out.append(
                f"归一退回 {self.normalize_rejected} 条（{rate:.0%}）："
                f"渲染结果丢了判分要求。若比例高，先看 probe 报告判断是"
                f"渲染器有 bug 还是探针过严，**不要直接调宽探针**"
            )
        if self.excluded_by_skeleton_mode:
            total = sum(self.excluded_by_skeleton_mode.values())
            out.append(
                f"骨架库里 {total} 个 task 不在 W1 范围内："
                f"{self.excluded_by_skeleton_mode}"
                f"（判分标准与单站判据不匹配）；本批只用了其余的 "
                f"{self.skeletons_w1_runnable} 个"
            )
        elif self.mode_distribution and len(self.mode_distribution) > 1:
            out.append(
                f"全模式采样（含 W1 判据不匹配的 {self.mode_distribution}）；"
                f"**产出里会有跑不了的条目**——执行端会跳过并记账。"
                f"确认判分标准匹配再采，或去掉 --all-modes"
            )
        return tuple(out)


@dataclass(frozen=True, slots=True)
class SampleBatch:
    """采样产出。``instances`` 可直接喂给 :mod:`executor.orchestrator`。"""

    instances: tuple[TaskInstance, ...]
    report: SamplingReport

    def to_json(self) -> dict[str, Any]:
        return {
            "report": self.report.to_json(),
            "instances": [i.to_json() for i in self.instances],
        }


def sample_tasks(
    n: int,
    *,
    library: PersonaLibrary,
    skeletons: Sequence[TaskSkeleton] | None = None,
    seed: int = 0,
    strata: Sequence[str] = ("genre", "verbal_style", "urgency"),
    w1_only: bool = True,
) -> SampleBatch:
    """交叉采样 ``n`` 个任务实例。

    ``w1_only=True``（默认）时排除 W1 跑不了的骨架。**设 False 请先读
    :func:`executor.orchestrator` 的成功判据**——那不是"多跑点数据"，
    是拿单站判据去评集合型任务，会把不完整的输出记成成功。

    配对策略：先轮转骨架（保证**任务面**均匀），再轮转画像。
    反过来（先画像）会让同一个骨架连着配很多画像，
    于是"某部片子失败"变成"这个人设失败"——两种分布不能混。
    """
    all_skeletons = tuple(skeletons) if skeletons is not None else load_skeletons()
    pool = [s for s in all_skeletons if s.runnable_in_w1] if w1_only else list(all_skeletons)

    mode_counts: dict[str, int] = {}
    excluded: dict[str, int] = {}
    for s in all_skeletons:
        mode_counts[s.retrieval_mode] = mode_counts.get(s.retrieval_mode, 0) + 1
        # 只有 ``w1_only=True`` 时才真有"因模式被排除"这回事；且判据取自
        # ``runnable_in_w1`` 属性本身，不硬编码模式名——属性才是真值源。
        #
        # ⚠️ 这里**只记非可跑模式**。早期版本把整个 mode 分布塞进
        # ``excluded``，于是 CLI 打印「排除 98 个」而实际只排了 17 个
        # （81 个 single_title 是被**用上**的）。字段名说 excluded、
        # 值却含"未排除"的项，读的人只能自己减一遍才知道真相——
        # 而多数人不会减。
        if w1_only and not s.runnable_in_w1:
            excluded[s.retrieval_mode] = excluded.get(s.retrieval_mode, 0) + 1

    if n <= 0:
        return SampleBatch((), SamplingReport(
            requested=n, produced=0,
            skeletons_seen=len(all_skeletons), skeletons_w1_runnable=len(pool),
            personas_seen=len(library), personas_compatible=0,
            excluded_by_skeleton_mode=excluded,
            mode_distribution=mode_counts,
            library_digest=library.digest(),
            seed=seed,
        ))
    if not pool:
        raise ValueError("没有可在 W1 执行的骨架；先看 mode_distribution")

    rng = random.Random(f"{SALT}|{seed}|{n}")

    # 每条样本都要**独立**取画像（允许重复），否则同一画像会连着配多个骨架，
    # 切片表里出现人为的相关性。取重复的概率由库大小决定，这是有意的：
    # 44 条画像要撑 300 条样本，重复不可避免，但顺序不该有规律。
    personas: list[PersonaProfile] = []
    excluded_compat = 0
    while len(personas) < n:
        p = library.profiles[rng.randrange(len(library.profiles))]
        s = pool[rng.randrange(len(pool))]
        if not renderer.is_compatible(p, s):
            excluded_compat += 1
            if excluded_compat > n * 20:      # 20 倍的尝试量还配不出来 → 停手
                raise ValueError(
                    f"尝试 {excluded_compat} 次仍配不出兼容组合；"
                    f"人设与骨架的取值域可能已经不匹配，先查 library / skeleton"
                )
            continue
        personas.append(p)

    skeleton_seq = [pool[rng.randrange(len(pool))] for _ in range(n)]
    personas = personas[:n]

    instances: list[TaskInstance] = []
    accepted = rejected = 0
    sk_usage: dict[str, int] = {}
    pp_usage: dict[str, int] = {}
    for sk, pp in zip(skeleton_seq, personas):
        res = renderer.render(sk, pp, seed=seed)
        inst = nz.build_instance(
            sk, pp,
            rendered_prompt=res.prompt_text,
            search_query=res.search_query,
            provenance_extra=res.provenance_extra(),
        )
        if inst.rewritten:
            accepted += 1
        else:
            rejected += 1
        instances.append(inst)
        sk_usage[sk.task_id] = sk_usage.get(sk.task_id, 0) + 1
        pp_usage[pp.persona_id] = pp_usage.get(pp.persona_id, 0) + 1

    by_mode: dict[str, int] = {}
    for inst in instances:
        by_mode[inst.retrieval_mode] = by_mode.get(inst.retrieval_mode, 0) + 1

    report = SamplingReport(
        requested=n,
        produced=len(instances),
        skeletons_seen=len(all_skeletons),
        skeletons_w1_runnable=len(pool),
        personas_seen=len(library),
        personas_compatible=sum(
            1 for p in library
            if any(renderer.is_compatible(p, s) for s in pool)
        ),
        excluded_by_skeleton_mode=excluded,
        mode_distribution=mode_counts,
        excluded_by_compatibility=excluded_compat,
        normalize_accepted=accepted,
        normalize_rejected=rejected,
        skeleton_usage=sk_usage,
        persona_usage=pp_usage,
        by_mode=by_mode,
        library_digest=library.digest(),
        seed=seed,
    )
    return SampleBatch(tuple(instances), report)


def probe_skeletons(
    library: PersonaLibrary,
    skeletons: Sequence[TaskSkeleton] | None = None,
    *,
    limit: int | None = None,
) -> dict[str, Any]:
    """对真实骨架跑一遍渲染 + 探针体检。

    与 :func:`trajectory_pipeline.taskgen.normalizer.probe_report` 的区别：
    这里是在**真实骨架**上跑（不是随手编的样本），
    所以产出的接受率可以直接回答"这批库和这批任务配不配"。
    离线单测用的是自造样本，覆盖不到真实措辞的边角——那些边角
    （例如「注明可观看的集数范围」这种嵌套要求）恰恰是探针最容易
    误判的地方。
    """
    skels = tuple(skeletons) if skeletons is not None else load_skeletons()
    if limit is not None:
        skels = skels[:limit]

    triples: list[tuple[TaskSkeleton, PersonaProfile, str]] = []
    rng = random.Random(f"{SALT}|probe")
    runnable = [s for s in skels if s.runnable_in_w1]
    for s in runnable:
        for p in library.profiles:
            if not renderer.is_compatible(p, s):
                continue
            res = renderer.render(s, p, seed=0)
            triples.append((s, p, res.prompt_text))
            if len(triples) >= (limit or len(runnable)) * 4:
                break

    report = nz.probe_report(triples)
    report["mode_distribution"] = mode_distribution(skels)
    return report