"""主控制流——「搜索 → 取候选 → 逐站点遍历 → 记账」。

与存量编排最大的区别：**这个文件里没有 ``if`` 判断业务语义**。
所有「这是不是播放站 / 有没有播放控件 / 是不是播放页」的判断都在
:class:`Perceptor` 里，控制流只负责按答案分流。这不是风格偏好——
LLM 版的判断来自远端模型输出，把它写进 ``if`` 就等于让代码去猜模型的语义，
而那正是 v2 要消灭的东西。

「LLM 是传感器不是驾驶员」在这里体现为：

    决策权 = 本文件的控制流      —— 代码定
    感知内容 = Perceptor.decide  —— 插件给，可替换

W1 的诚实说明：``RulePerceptor`` 对 ``SELECT_PLAY_SITES`` / ``IS_REACHABLE``
只能返回 ``None``，所以 W1 批次的 ``IS_REACHABLE`` 环节全是 ``unresolved``。
这不是缺陷，是 fail-closed 的正常表现；W3 接上 ``LLMPerceptor`` 后同一份代码
不需要改动一行。
"""

from __future__ import annotations

import asyncio
import dataclasses
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Mapping

from trajectory_pipeline.executor import actions
from trajectory_pipeline.executor.actions import StepLog
from trajectory_pipeline.executor.branches import (
    RunLedger,
    validate_observation_for_decision,
)
from trajectory_pipeline.executor.dom import DEFAULT_MAX_CHARS, detect_block
from trajectory_pipeline.executor.steps import aggregator as aggregator_step
from trajectory_pipeline.executor.steps import article as article_step
from trajectory_pipeline.executor.steps import search as search_step
from trajectory_pipeline.executor.steps import site_search as site_search_step
from trajectory_pipeline.executor.steps import visit as visit_step
from trajectory_pipeline.executor.steps.aggregator import IntermediateResult
from trajectory_pipeline.executor.steps.search import Candidate
from trajectory_pipeline.perception.base import Decision, Observation, Perceptor, Q

#: 发现链主流程的运行日志。**与 ``record.warnings`` 分工**（2026-10-16）：
#: warnings 是存档字段——批次复盘的判据，跑完落盘才读得到；log 是实时
#: 输出——跑批时当场看链路走到哪、卡在哪。同一个问题两边都记不是
#: 冗余：复盘时终端早滚走了（没有 log），跑批时又要等落盘（没有
#: warnings 的实时性）。CLI 侧由 ``--log-level`` 控制（默认 INFO）。
log = logging.getLogger(__name__)

#: 存档里保留的交互元素 / 链接条数上限。**提取成常量不是为了省字**，
#: 是因为「落盘裁到几条」这件事现在有了判据意义——
#: D-2 的评分器靠 ``len(观察里存的)`` 与 ``elements_total`` 的差判断
#: 「目标是不是可能被裁掉了」，而这个差**只有在两处截断对齐时才有意义**。
#: 哪天这里改成 100 而别处还按 80 判断，D-2 会静默地把裁剪当成不存在。
OBS_ELEMENT_LIMIT = 80
OBS_LINK_LIMIT = 50

#: 感知层送模型的**链接窗口宽度**（= ``perception.llm_perceptor.LLM_LINKS``）。
#: executor 不能 import 感知层实现（01-架构总览 L49-50：executor 只依赖
#: ``perception/base.py`` 协议），所以这里复制一份，由单测断言与
#: ``LLM_LINKS`` 相等（test_search_pin.py::test_窗口常量与感知层同步），
#: 防止两侧漂移——漂移 = 钉窗校验按错的宽度算，钉了也白钉。
#: 04 方案 §2.4-3 同时要求 ``max_candidates ≤ LLM_LINKS``：超出时候选装
#: 不进窗口，钉窗必然失败，正是 :func:`candidates_outside_window` 要报警的
#: 情形。
PERCEPTION_LINK_WINDOW = 20

#: **P1 存档格式版本**。每份新存档写入时记下这个值，于是「这批数据是哪一批」
#: 能从存档本身读出来，而不用猜。
#:
#: ⚠️ **改这个值等于宣告一批存档与另一批不可比**——所以它只在**破坏性**
#: 变更时加：字段语义变了、键改名了、老键删了。加字段、补可选字段
#: **不动它**（下游对多出来的键本就容错，``split_archive`` 逐键判存在性）。
#:
#: 现状：``"2"`` = 2026-10-10 补齐 ``run_config`` / ``elements_total`` /
#: ``site_url`` / ``play_page_url`` / ``question`` / ``reached_play_page`` 之后。
#: **此前跑的那 38 份没有这个键**，它们不是「v1」——是**未标注**。
#: 没回填是因为回填即猜（见 :attr:`RunRecord.schema_version`）。
SCHEMA_VERSION = "2"

#: 发现路径三选一（2026-10-16 路径 B/C）。**单路径 per run**——一次
#: run 只走一条发现链，批次按 ``run_config.discovery_path`` 归因；
#: 不做「一次跑三条然后合并」，那会让三条链的候选混进同一批，
#: 「这个站是搜出来的还是聚合站引荐的」就再也分不开了。
#:
#: - ``direct``      路径 A：片名直搜（现状，默认——**完全向后兼容**）
#: - ``aggregator``  路径 B：通用聚合词搜聚合站 → 访问 → 入口外链校验
#: - ``article``     路径 C：片名直搜选文章页 → 访问 → 引荐外链校验
DISCOVERY_PATHS = ("direct", "aggregator", "article")


@dataclass
class RunConfig:
    """一次运行的参数。

    默认值全部**保守**：宁可少跑几个站，也不要产出看似完整实则证据不足的记录。
    """

    engine: str = search_step.DEFAULT_ENGINE
    max_candidates: int = 20
    max_chars: int | None = DEFAULT_MAX_CHARS
    stop_after_success: int = 0  # 0 = 跑满候选；>0 = 成功 N 个即停
    per_site_timeout_s: float = 90.0
    #: 发现路径（2026-10-16 路径 B/C）。默认 ``direct``：既有批次与
    #: 全部既有调用点**零改动**——「默认值不改变现状」是加配置的第一纪律。
    discovery_path: str = "direct"
    #: B/C：最多访问几个中间页（聚合站 / 文章）。预算耗尽只记 warning
    #: 不中断——那是「这一轮发现链的广度上限」，不是采集故障。
    max_intermediates: int = 3
    #: B/C：每个中间页最多校验几条引荐外链。外链校验每条都要开 tab
    #: 真实访问，上限是预算控制不是语义筛选。
    max_referral_links: int = 5

    def __post_init__(self) -> None:
        if self.discovery_path not in DISCOVERY_PATHS:
            raise ValueError(
                f"未知发现路径 {self.discovery_path!r}；可用：{DISCOVERY_PATHS}。"
                f"拼错路径会静默跑成 direct，而那批存档的归因就是错的——"
                f"与其猜，不如当场报错。"
            )


@dataclass
class RunRecord:
    """一个 task 的完整运行记录——P1 存档的主体。"""

    task_id: str
    title: str
    query: str
    search_url: str
    #: **用户实际说的那句话**（persona 渲染后的原文）。空串 = 手工路径，
    #: 与 ``provenance`` 空 dict 同款约定：「不是 taskgen 跑的」。
    #:
    #: ⚠️ 与 :attr:`query` 是两件事，别混：``query`` 是拿去搜的检索串，
    #: ``user_prompt`` 是用户开口的那句。P2 六件套第 ③ 件要的是后者——
    #: 模型学的是「**用户这么说，我该怎么回**」，喂检索式等于让它学
    #: 从关键词反推意图，而那正是 persona 要制造的偏差。
    #:
    #: 它曾经只活在计划文件（``PlanEntry.prompt_text``）里，于是 **P2 拿不到
    #: 用户轮次**——而计划文件不是存档，批次跑完散在各处的计划文件对不上
    #: 存档就是一条**静默失配**（跟 provenance 那条同源，理由见本字段上方）。
    user_prompt: str = ""
    search_obs: Observation | None = None
    #: **搜索单元的终局反思**（INC46）。与 ``steps`` **平级**，不在
    #: ``outcomes`` 里：它是 LLM 产物，而 outcome 要保持采集事实层纯洁。
    #:
    #: ``None`` = 没生成（writer 未注入 / 后端不可用 / 生成失败）。
    #: 与「生成了但内容为空」是两回事——:class:`WriteResult` 的三态原样落盘。
    reflection: dict[str, Any] | None = None
    candidates: list[Candidate] = field(default_factory=list)
    visits: list[visit_step.VisitResult] = field(default_factory=list)
    #: 发现路径（2026-10-16 路径 B/C）。与 ``run_config.discovery_path``
    #: 同值——单独存一份是因为 run_config 是「参数快照」而这里是
    #: 「实际走的那条链」：将来若有「按观察动态换路径」的逻辑，
    #: 两者会分叉，届时读档的人需要知道**实际**走了哪条。
    discovery_path: str = "direct"
    #: B/C 路径的中间页访问结果（聚合站页 / 文章页）。direct 恒空。
    #: 每个元素是独立决策单元（P2 切条时 ``agg-N`` / ``article-N``）。
    intermediates: list[IntermediateResult] = field(default_factory=list)
    ledger: RunLedger | None = None
    warnings: list[str] = field(default_factory=list)
    elapsed_ms: int = 0
    perceptor: str = ""
    #: 候选来源。本字段是**不可省略的审计项**：W1 的候选来自代码启发式，
    #: 不是任何 Perceptor 的判断。读档的人若不知道这一点，会把
    #: 「W1 选出的站点」误读成「规则版能选出正确站点」——它不能。
    candidate_source: str = "heuristic"
    #: 取到的**搜索结果标题条数**。0 = 没取到，判断点 ① 会 fail-closed。
    #:
    #: 这是审计项，不是统计项：判断点 ① 要判的是「URL ↔ 片名」，
    #: 而 ``links()`` 的 text 对搜索结果页往往只有面包屑（实测 bing
    #: 32 条链接无一条含片名）。所以「① 为什么恒为 unresolved」这个问题，
    #: 答案是 0，而**光看分支统计看不出来**——它长得像「模型判不了」。
    result_titles: int = 0
    #: 模块 1（taskgen）的切片轴：persona 六维、检索模式、归一结论。
    #:
    #: ⚠️ **空 dict 的含义是「不是 taskgen 跑的」，不是「taskgen 没生效」**
    #: ——这两者在切片表里长得完全一样，必须能区分。
    #:
    #: 这个字段存在的理由：``--plan`` 跑出来的数据要按 persona 切片出分
    #: （"强口语 × 长尾这类组合是否退化"）。若 provenance 只活在计划文件里
    #: 而不进存档，那切片就只剩计划文件与存档的一份人工对表——
    #: 两者一旦错位（存档重跑、计划重生成），**错位是静默的**。
    provenance: dict[str, Any] = field(default_factory=dict)
    #: 搜索页被反爬拦截的原因（空 = 没被拦）。
    #:
    #: ⚠️ **这是运行级状态，不是站点级失败分支**，所以刻意**不进**
    #: ``RunLedger`` 的 8 条 outcome 分支体系——那条体系里每一支都对应
    #: 「这个站点看完之后的结论」，而反爬发生在**取候选之前**，
    #: 根本没有 outcome 可记。塞进去会让「负样本池」混入一批
    #: 连站点都没访问到的条目。
    #:
    #: 单开字段的代价是 report 要额外统计一次；换来的是
    #: 「被拦」与「没素材」在报表上永远分得开。
    search_blocked: str = ""
    #: 候选过滤记账：``{原因: 条数}``。静默剔除会让"引擎给了 50 个链接、
    #: 我们只跑了 11 个"这类损失不可见。
    candidate_filter: dict[str, int] = field(default_factory=dict)
    #: **本次运行的 :class:`RunConfig` 快照。**
    #:
    #: 这是评分维度 **B-2（检索覆盖完整性）** 的分母来源。缺它的话
    #: ``--max-candidates 5`` 跑出的「5/5 全处置」与默认上限跑出的
    #: 「5/20」在存档里**长得一模一样**——两个批次的覆盖率差 4 倍，
    #: 而读档的人无从分辨，那是任务标准还是覆盖率退化。
    #:
    #: 落盘里必须有的四项：
    #:   ``max_candidates``      候选上限，决定覆盖完整性分母
    #:   ``stop_after_success``  提前停止阈值，**改的是分子**
    #:   ``per_site_timeout_s``  单站超时——⚠️ **CLI 不设它**，走
    #:                          :class:`RunConfig` 默认值，所以它连
    #:                          ``--help`` 都查不到，只在源码里。
    #:                          而它是 B-4（异常处置）唯一的时限判据。
    #:   ``engine``             搜索引擎。不同引擎的候选分布差异很大，
    #:                          混在一起算覆盖率没有意义。
    #:
    #: 空 dict = 手工路径（与 ``provenance``、``user_prompt`` 同款约定）。
    run_config: dict[str, Any] = field(default_factory=dict)
    #: **搜索阶段**的动作流。站点级动作在各 ``VisitResult.steps`` 里——
    #: 拆开是因为搜索与遍历是两种不同的决策单元（P2 切成两条样本）。
    #:
    #: 语义见 :mod:`trajectory_pipeline.executor.actions`。同样**不参与判定**。
    steps: StepLog = field(default_factory=StepLog)
    #: **P1 存档格式版本**，写入时记录（见 :data:`SCHEMA_VERSION`）。
    #:
    #: 这个字段存在的理由是**批次身份必须能从存档本身读出来**。此前做不到：
    #: 「这批数据跑在字段补齐之前没有」只能靠 ``run_config`` 缺失去猜，
    #: 而那是**缺件代理不是身份**——将来某次 run 因崩溃漏写 ``run_config``，
    #: 同样被判成旧批次，报表上多出一条看不懂的数据，而**误分是静默的**。
    #:
    #: ⚠️ **历史存档不回填**。回填就是猜：凭什么断定一份存档「是 v2 的」？
    #: 凭什么断定一份没 ``schema_version`` 的「是 v1 的」？没有依据，
    #: 而猜错的代价是一个错误的批次归属。缺这个键 = **不知道是哪一批**，
    #: 由 :func:`~trajectory_pipeline.evaluation.cli.evaluate` 归入「未标注」
    #: 并退回到缺件代理——**代理明确标注为代理**，不冒充身份。
    schema_version: str = SCHEMA_VERSION

    def __post_init__(self) -> None:
        """拷一份 provenance，不留对入参的引用。

        :meth:`to_json` 是**归档那一刻**才序列化，而存档是整个批次唯一的
        真值源。若这里只存引用，调用方在归档前改一次入参字典（补个字段、
        改个归一结论），存档内容就跟着变——而存档文件名、报告、切片脚本
        全都不知道发生过这件事。

        ``orchestrator.run`` 自己已经拷过一次，这里再拷一次是有意的冗余：
        ``RunRecord`` 也可以被直接构造（单测、将来的重放入口），
        防御放在**值对象自己的边界**上，而不是靠每个调用点自觉。
        """
        self.provenance = dict(self.provenance or {})
        self.run_config = dict(self.run_config or {})

    @property
    def succeeded(self) -> bool:
        return any(v.success for v in self.visits)

    def to_json(self) -> dict[str, Any]:
        """序列化。刻意只保留**可审计**的字段，不留页面句柄。"""
        return {
            "schema_version": self.schema_version,
            "task_id": self.task_id,
            "title": self.title,
            "query": self.query,
            "user_prompt": self.user_prompt,
            "search_url": self.search_url,
            "perceptor": self.perceptor,
            "candidate_source": self.candidate_source,
            "result_titles": self.result_titles,
            "search_blocked": self.search_blocked,
            "candidate_filter": dict(self.candidate_filter),
            "run_config": dict(self.run_config),
            "provenance": dict(self.provenance),
            "elapsed_ms": self.elapsed_ms,
            "warnings": list(self.warnings),
            "search_observation": _obs_json(self.search_obs),
            "steps": [s.to_json(_obs_json) for s in self.steps.steps],
            # 搜索单元的终局反思（INC46）。与 ``steps`` 平级——它不是某一步
            # 的 rationale，是整个搜索单元看完之后的总结。
            "reflection": dict(self.reflection) if self.reflection else None,
            "discovery_path": self.discovery_path,
            # B/C 路径的中间页单元（2026-10-16）：就地构造（与 visits 同款），
            # 决策只落摘要——question/answer/evidence 足够人工复核复现，
            # 完整 Decision 的其余字段（source/fallback_used）已在对应
            # outcome 里。
            "intermediates": [
                {
                    "role": ir.role,
                    "url": ir.candidate.url,
                    "landed_url": ir.landed_url,
                    "notes": list(ir.notes or []),
                    "steps": [s.to_json(_obs_json) for s in ir.steps.steps],
                    "obs": _obs_json(ir.obs),
                    "is_intermediate": _decision_json(ir.is_intermediate),
                    "entries": _decision_json(ir.entries),
                    "referrals": [r.to_json() for r in ir.referrals],
                    "candidates": [
                        {
                            "url": c.url, "text": c.text, "rank": c.rank,
                            "host": c.host, "origin": c.origin, "parent": c.parent,
                        }
                        for c in ir.candidates
                    ],
                }
                for ir in self.intermediates
            ],
            "candidates": [
                {"url": c.url, "text": c.text, "rank": c.rank, "host": c.host}
                for c in self.candidates
            ],
            "visits": [
                {
                    "url": v.candidate.url,
                    "landed_url": v.landed_url,
                    "success": v.success,
                    "notes": list(v.notes or []),
                    "steps": [s.to_json(_obs_json) for s in v.steps.steps],
                    "site_obs": _obs_json(v.site_obs),
                    "player_obs": _obs_json(v.player_obs),
                    # 站点单元的终局反思（INC46），与 steps 平级。
                    "reflection": dict(v.reflection) if v.reflection else None,
                    # 站内搜索结果（2026-10-16 路径 B/C）。None = 没走
                    # 站内搜索（direct / 深层候选），与「走了但
                    # fail-closed」不同物。
                    "search": v.search.to_json() if v.search else None,
                }
                for v in self.visits
            ],
            "ledger": self.ledger.summary() if self.ledger else None,
            "outcomes": [
                o.to_json() for o in (self.ledger.outcomes if self.ledger else [])
            ],
        }


def _outcome_json_for(
    record: RunRecord, result: visit_step.VisitResult
) -> Mapping[str, Any] | None:
    """找这一步访问的**最终判定**（给 :meth:`Orchestrator._write_reflection`）。

    匹配顺序**先候选 url、再落地 url**（与
    :func:`~trajectory_pipeline.assembler.schema._outcome_for` 同款）：
    ``visits[i].url`` 是候选链接，而 ``outcomes[j].url`` 记的是落地地址，
    ``http→https`` 跳转时两者不等，只按候选 url 匹配会**静默**地一条都
    对不上——于是反思拿到 ``None``，写成「没有结论」，而实际是有分支的。

    ⚠️ 这里匹配的是 ``site_url`` 字段优先于 ``url``：``RunLedger`` 同时
    记了两者，而候选链接在 ``site_url`` 里。
    """
    outcomes = list(record.ledger.outcomes if record.ledger else ())
    for o in outcomes:
        if str(getattr(o, "site_url", "") or "") == str(result.candidate.url or ""):
            return o.to_json()
    for want in (str(result.candidate.url or ""), str(result.landed_url or "")):
        if not want:
            continue
        for o in outcomes:
            if str(getattr(o, "url", "") or "") == want:
                return o.to_json()
    return None


def _decision_json(decision: Decision | None) -> dict[str, Any] | None:
    """Decision → 摘要 JSON（中间页单元落盘用）。

    只落 ``question`` / ``answer`` / ``evidence`` 三件：人工复核要复现
    一个决策，有这三样就够；``source`` / ``fallback_used`` / 完整
    ``payload`` 已随该决策的记账（``outcomes[]``）落盘，这里再放一份
    是同一个事实的两处真值。
    """
    if decision is None:
        return None
    return {
        "question": decision.question,
        "answer": decision.answer,
        "evidence": decision.evidence,
    }


def _obs_json(obs: Observation | None) -> dict[str, Any] | None:
    """Observation → JSON。落**可归因的证据**，不是给人眼看的摘要。

    ⚠️ **正文全文落盘**（``body_text``），早期版本只留 ``body_preview`` 前 400
    字符，理由是「体积失控、需要时按 url 重抓」。那个理由在真实运行里
    站不住：

        - 站点会失效、下线、改版，重抓拿到的是**另一个页面**，
          而存档里那条结论的证据已经不存在了；
        - 被反爬拦截时根本重抓不回来；
        - rationale 的实体核查（模块 4，待建）要拿 rationale 引用的实体
          去对观察存档——只有摘要时，落在 400 字符之后的实体一律判成幻觉，
          而那**不是幻觉，是没存**。

    P1 是批次唯一的真值源，证据链一旦断就是**不可逆**的；体积只是磁盘。
    D8 已把产物隔离在 ``output/pipeline/``，这条与存量不共享磁盘预算。

    ``body_len`` / ``body_source`` / ``truncated`` 一并保留——它们是
    「这份正文可不可信」的判据，与正文本身同等重要。
    """
    if obs is None:
        return None
    return {
        "url": obs.url,
        "page_title": obs.page_title,
        "body_text": obs.body_text,
        "body_len": len(obs.body_text),
        "body_source": obs.body_source,
        "truncated": obs.truncated,
        "raw_len": obs.raw_len,
        "stripped_ratio": round(obs.stripped_ratio, 4),
        "degraded": list(obs.degraded),
        "video_tag_count": obs.video_tag_count,
        "iframe_count": obs.iframe_count,
        # 落盘的条数（可能被裁）与**采到的条数**（可能被驱动层 limit 截断）
        # 成对落盘。D-2 评分器靠两者的差判断「目标是不是可能落在裁剪区外」
        # —— 详见 Observation.elements_total 的说明，那条规则在 v1.1 里
        # 是唯一一条不补数据就必然产生反向错判的边界规则。
        "elements_total": obs.elements_total or len(obs.interactive_elements),
        "links_total": obs.links_total or len(obs.links),
        "interactive_elements": [
            {"ref": e.ref, "tag": e.tag, "label": e.label[:120]}
            for e in obs.interactive_elements[:OBS_ELEMENT_LIMIT]
        ],
        "links": [
            {"text": l.text[:80], "href": l.href} for l in obs.links[:OBS_LINK_LIMIT]
        ],
    }


def _apply_selection(
    record: RunRecord,
    selection: Decision,
    ledger: RunLedger,
    *,
    branch: str = "not_play_site",
    gate: str = "判断点 ①",
) -> list[Candidate]:
    """按选题结论过滤候选，**并把落选的记成 ``branch`` 负分支**。

    落选必须记账，这是 CLAUDE.md「**每条分支都必须有对应样本入库**」
    的直接要求：负样本不是副产品。反过来「只记选中的」会让
    ``not_play_site`` 永远是 0，而报表上分部数字齐全，没人看得出这支空了。

    ``branch`` / ``gate``（2026-10-16 路径 B/C）：①B 记 ``not_aggregator``、
    ①C 记 ``not_article``，三路径共用这一份「选中保留 / 点名记负 /
    未点名记 unresolved」的语义——分开写三份，改一份漏两份的漂移
    比口径差异本身难查。默认值 = direct 现状，既有调用零改动。

    **匹配按 :attr:`Candidate.source_href`**（观察里的原始 href），而遍历用
    ``Candidate.url``（解包后）。感知层读的是观察，所以它回传的是前者。
    两个字段对不上时按后者兜底——百度 ``/link?url=`` 包裹的链接一旦
    匹配不上，表现是「整个站点集丢失」，且看不出是匹配问题。

    顺序保持**引擎原序**（``rank``），不按 selected 数组的顺序重排——
    重排等于用模型的判断覆盖引擎的相关性排序，那是没有依据的。
    """
    selected_rows = [
        r for r in (selection.payload.get("selected") or []) if isinstance(r, dict)
    ]
    reasons: dict[str, str] = {}
    for row in selected_rows:
        url = str(row.get("url") or "").strip()
        if url:
            reasons[url] = str(row.get("why") or selection.evidence)

    rejected = {
        str(r.get("url") or "").strip(): str(r.get("reason") or "")
        for r in (selection.payload.get("rejected") or [])
        if isinstance(r, dict)
    }

    kept: list[Candidate] = []
    dropped = 0
    undecided = 0
    for candidate in record.candidates:
        key = candidate.source_href or candidate.url
        if candidate.url in reasons or key in reasons:
            kept.append(candidate)
            continue
        # 判据是「模型**点名**了这个候选」，不是「模型给了理由」——
        # 理由可以为空，点名与否才是「它考虑过并否决了」与「它压根没看见」
        # 的唯一区别。
        named = key in rejected or candidate.url in rejected
        if not named:
            undecided += 1
            ledger.record(
                candidate.url,
                "unresolved",
                f"{gate} 未选中该站点，而模型未把它列入 rejected——"
                f"它多半压根没看到这条链接（送进模型的是排序后的前 "
                f"{len(record.search_obs.links)} 条里的一小段）。"
                f"**无证据不记负样本**：判成 {branch} 就是把"
                f"「模型没答」写成「这个站看不了」。",
                decision=selection,
                site_url=candidate.url,
            )
            continue
        dropped += 1
        why = (rejected.get(key) or rejected.get(candidate.url) or "").strip()
        ledger.record(
            candidate.url,
            branch,
            f"{gate} 排除该站点。{why}"
            if why
            else f"{gate} 排除该站点（模型点名了但未给出理由）",
            decision=selection,
            site_url=candidate.url,
        )
    record.warnings.append(
        f"{gate} 选出 {len(kept)}/{len(record.candidates)} 个候选，"
        f"{dropped} 个记 {branch}"
        + (
            f"，{undecided} 个模型未点名，记 unresolved（**不入负样本池**）"
            if undecided
            else ""
        )
    )
    return kept


def _is_site_homepage(url: str) -> bool:
    """URL 是否指向**站点首页**——委托 :func:`site_search.is_site_homepage`。

    实现只有一处（2026-10-16 路径 B/C）：direct 的首页丢弃（本文件）
    与 B/C 的站内搜索分叉（visit 链）必须同一把尺子，两边各写一份
    迟早漂移成「direct 丢的首页 visit 链不认」。函数名保留在本模块
    是既有测试与 mutation_check 锚点的 import 面。
    """
    return site_search_step.is_site_homepage(url)


def _drop_site_homepages(record: RunRecord, ledger: RunLedger) -> None:
    """把**站点首页**候选记成 ``not_play_site`` 并剔出候选集（原地改写）。

    这是**代码层的确定性事实核**，不是语义判断——搜索结果里 path 为
    ``/`` 的 URL 就是该站首页，而首页结构上不可能是某部作品的播放页。
    用得着它的是实测那批：hao123 被搜索引擎当成《功夫》的结果返回，
    访问后按「页面上没有播放控件」入负样本池。**结论方向碰巧对了，
    理由全错**——那个站根本不是内容站，问题是「压根不是搜索结果该
    返回的东西」，不是「去了发现看不了」。这两类混成一条，负样本池就
    再也答不出「为什么看不了」，而那正是它存在的唯一理由。

    **为什么不加进** ``EXCLUDED_HOST_ROOTS``：那样首页压根不成为候选，
    ``not_play_site`` 永远 0 条——而「每条分支都必须有对应样本入库」是
    硬要求。这条分支的样本只能从这里来，删掉就再也长不出来。

    **为什么落在判断点 ① 之后**：① 的 True / False / None 三条路径都要
    过这道核，而 :func:`_apply_selection` 只在 True 时被调，挂在它里面
    就只能覆盖三分之一。首页的否定不依赖模型是否作答，所以位置在
    「① 处理完、开始遍历之前」——对三条路径都生效的唯一位置。
    """
    kept: list[Candidate] = []
    dropped: list[Candidate] = []
    for candidate in record.candidates:
        (dropped if _is_site_homepage(candidate.url) else kept).append(candidate)
    if not dropped:
        return
    for candidate in dropped:
        ledger.record(
            candidate.url,
            "not_play_site",
            f"候选 URL 的 path 为空或 `/`（{candidate.url}）——是站点首页，"
            f"结构上不可能是某部作品的播放页。搜索引擎把导航站首页当搜索"
            f"结果是常见形态（实测 hao123）；这是**找错站**，不是「去了"
            f"发现看不了」，故不记 no_play_control。",
            site_url=candidate.url,
        )
    record.candidates = kept
    record.warnings.append(
        f"{len(dropped)} 个候选是站点首页（path 为 `/`），记 not_play_site 不访问："
        + "、".join(c.url for c in dropped[:3])
        + ("…" if len(dropped) > 3 else "")
    )


class Orchestrator:
    """主控制流。一个实例跑一个 task。"""

    def __init__(
        self,
        driver: Any,
        perceptor: Perceptor,
        config: RunConfig | None = None,
        *,
        rationale_writer: Any = None,
    ) -> None:
        self._driver = driver
        self._perceptor = perceptor
        self._cfg = config or RunConfig()
        #: 模块 4 的 writer。**可调用对象，不是类型**——``executor``
        #: 不 import ``rationale``（依赖方向：llm ← {taskgen, perception,
        #: rationale}，executor 只依赖 perception 的协议）。构造它的是
        #: :mod:`trajectory_pipeline.executor.cli`，那一层可以 import 任何模块。
        #: 留 ``None``（W1 规则版批次）时全部步骤没有 rationale，
        #: 闸门记 ``not_run``——**这与「闸门跑过且通过」不是一回事**。
        self._writer = rationale_writer

    async def run(
        self,
        task_id: str,
        title: str,
        *,
        persona: object | None = None,
        search_query: str | None = None,
        user_prompt: str = "",
        provenance: Mapping[str, Any] | None = None,
    ) -> RunRecord:
        """跑完一个 task 的全部环节。**不抛异常**——异常折算进 warnings。

        ``search_query`` 由模块 1（:mod:`trajectory_pipeline.taskgen`）产出时
        优先使用；不给才退回本层的启发式拼接。两条路径都留着是因为
        ``search_query`` 有两种来源，而它们**不该被混为一谈**：
        taskgen 给的是「按 persona 的指代方式渲染出的用户表述所对应的检索」，
        启发式给的是「按片名硬拼」。前者带 provenance，后者不带。

        ``user_prompt`` 是**用户实际说的那句话**（persona 渲染后的原文），
        与 ``search_query`` 是两回事：后者是拿去搜的字符串。
        P2 六件套的第 ③ 件要的是前者，而它在存档里无处可寻——
        原因与修法见 :attr:`RunRecord.user_prompt`。

        ``provenance`` 原样进存档（见 :class:`RunRecord` 的说明）。
        **本方法不读它的内容**——它是切片轴，不是控制流输入。
        这一点必须成立：一旦执行端开始"根据 provenance 决定跑不跑"，
        采样与执行就耦合了，而那样的批次无法归因（分不清是 persona 的
        问题还是采样规则的问题）。要跳过的条目在
        :mod:`trajectory_pipeline.executor.plan` 里跳，不在这里跳。

        Raises:
            ValueError: ``title`` 为空。**这是全流程唯一允许抛出的情形**，
                因为它不是采集故障，是**调用方把必填项漏了**。
                其余一切（网络、渲染、站点结构、后端超时）都折算进
                warnings——那些是真采集，采不到就该留痕而不是中断。

                为什么要为这一条破例：片名是判断点 ① 的**唯一判据来源**
                （``llm_perceptor._title_link_sufficiency`` 靠它核对链接
                文本）。没有片名时 ① 的正确行为是 fail-closed，而调用方
                通常会在自己那一层先拿 task_id 顶上——实测 T001 顶上去
                就搜成了「轮胎 T001」，产出 6 条理由通顺的假负样本，
                **零异常、零空转、一条都看不出坏**。让它在调用方那层
                静默发生，比让它在这里炸掉贵得多。
        """
        if not (title or "").strip():
            raise ValueError(
                f"task {task_id!r} 没有片名。片名是判断点 ① 的唯一判据来源，"
                f"拿 task_id 顶替会产出一整批自洽而全错的素材且不报错——"
                f"请传真实片名。"
            )
        started = time.monotonic()
        # ── 检索词分叉（2026-10-16 路径 B/C）────────────────────────
        # B 用**通用聚合词**（与片名无关——任何片名的聚合站入口池是
        # 同一个）；C 与 A 同查询（文章推荐文本身按片名长出来）。
        # 片名在三条路径下都必填：B 的站内搜索（环节②）要拿它填搜索框。
        if self._cfg.discovery_path == "aggregator":
            query = search_step.aggregator_query(task_id)
        else:
            query = search_query or search_step.build_query(
                title, persona=persona
            )
        url = search_step.search_url(query, self._cfg.engine)
        log.info("[%s] 发现链启动：path=%s query=%r engine=%s",
                 task_id, self._cfg.discovery_path, query, self._cfg.engine)
        ledger = RunLedger(task_id=task_id)
        record = RunRecord(
            task_id=task_id,
            title=title,
            query=query,
            search_url=url,
            user_prompt=user_prompt,
            ledger=ledger,
            perceptor=getattr(self._perceptor, "name", "?"),
            provenance=dict(provenance or {}),
            discovery_path=self._cfg.discovery_path,
            # 运行参数进存档：覆盖完整性（B-2）与异常处置（B-4）
            # 两项断言的分母/判据都在这里，见 RunRecord.run_config。
            run_config=dataclasses.asdict(self._cfg),
        )
        if self._cfg.discovery_path == "aggregator" and search_query:
            # taskgen 的检索词是按 persona 渲染的**片名**检索，B 路径
            # 用不上——静默忽略会让「这批 B 为什么 query 是聚合词」
            # 查无对证，而 warning 一句话就能说清。
            record.warnings.append(
                f"B 路径（aggregator）忽略传入的检索词 {search_query!r}"
                f"——聚合站发现用通用聚合词 {query!r}（按 task 轮换），"
                f"片名 {title!r} 保留给环节② 的站内搜索"
            )

        # ── 环节 0：搜索页 ──────────────────────────────────────────
        step_search = record.steps.act(actions.goto(url))
        try:
            await self._driver.goto(url)
            record.search_obs = await self._driver.observe(
                max_chars=self._cfg.max_chars
            )
        except Exception as exc:
            # 搜索页拿不到时整条 run 提前返回，动作流里必须留下这一步：
            # 「搜都没搜成」与「搜了但没素材」在报表上是两回事，
            # 而动作流是这个区分在样本层的落点。
            record.steps.settle(step_search, error=f"{type(exc).__name__}: {exc}")
            log.warning("[%s] 搜索页采集失败：%s: %s",
                        task_id, type(exc).__name__, exc)
            record.warnings.append(f"搜索页采集失败: {type(exc).__name__}: {exc}")
            record.elapsed_ms = int((time.monotonic() - started) * 1000)
            return record
        record.steps.settle(step_search, record.search_obs)
        # 搜索步也补记 rationale——P2 按决策单元切条，搜索阶段是正式样本
        # （"一条 = 一个决策单元：搜索阶段一条 / 每个站点一条"），它没有
        # rationale 会让整条搜索样本恒 gate=not_run、D-9 永远不可判定。
        # 失败路径（search_obs=None）不产 search 样本，无须补记。
        await self._write_rationales(record.steps, record, "搜索页")

        # ── 环节 0.5：反爬拦截 ─────────────────────────────────────
        # 必须在取候选**之前**判：验证码页的表现是"正文短 + 零链接"，
        # 混进下面的"未提取到候选"里，两者处置完全不同（加间隔/换引擎
        # vs 换素材），而混在一起的代价是静默的——报表上只看得见
        # "这批搜索页质量差"，没人会想到去查反爬。
        blocked = detect_block(
            record.search_obs.url,
            record.search_obs.page_title,
            record.search_obs.body_text,
        )
        if blocked:
            record.search_blocked = blocked
            log.warning("[%s] 搜索页被反爬拦截（%s）：%r @ %s",
                        task_id, blocked, record.search_obs.page_title,
                        record.search_obs.url[:80])
            record.warnings.append(
                f"搜索页被反爬拦截（{blocked}）：{record.search_obs.page_title!r} "
                f"@ {record.search_obs.url[:80]}。"
                f"**这一条不是素材质量问题**，是 {record.query!r} 没搜成。"
                f"加请求间隔或换引擎（--engine bing）再试；"
                f"直接重跑同一引擎大概率仍是验证码页"
            )
            record.elapsed_ms = int((time.monotonic() - started) * 1000)
            return record

        warn = validate_observation_for_decision(record.search_obs, "SELECT_PLAY_SITES")
        if warn:
            record.warnings.append(warn)

        # ── 环节 ①：取候选（代码层，非感知层）─────────────────────
        record.candidate_filter = {}
        # 先把结果标题取回来。links() 的 text 对 bing 只有面包屑，
        # 而判断点 ① 要的就是「URL ↔ 片名」——取不到时它会 fail-closed，
        # **但这件事必须留在 warnings 里**：否则报表上只会看到
        # 「① 恒为 unresolved」，没人知道是采集层少给了判据。
        titles, title_why = await self._result_titles()
        if titles:
            record.result_titles = len(titles)
            # **把标题写回观察**。判断点 ① 拿到的就是这份 Observation，
            # 只改 candidates 不改它，等于修了下游却没修上游——
            # 而存档里存的也是这份观察，不改它就等于**存档与实际判定依据脱节**。
            #
            # 取标题必须走 ``search_step.title_for``：它同时试原始 href 与
            # 解包后的 URL，而百度结果页的 ``links[].href`` 是
            # ``/link?url=`` 包裹形态、``browser_extract`` 给的却是解包后的
            # 目标 URL。只按原始 href 查会**一条都命不中**，
            # 于是这里静默失败、存档的 links 仍是导航栏，而 ① 读的就是
            # 它——结果就是 ① 永远看不到片名、恒 fail-closed，
            # 症状却像「选择器失效」。
            record.search_obs = dataclasses.replace(
                record.search_obs,
                links=tuple(
                    dataclasses.replace(
                        link, text=search_step.title_for(titles, link.href) or link.text
                    )
                    for link in record.search_obs.links
                ),
            )
        else:
            record.warnings.append(
                f"未取到 {self._cfg.engine} 的结果标题（{title_why}）"
                f"——判断点 ① 将因输入不含片名而 fail-closed"
            )
        record.candidates = search_step.extract_candidates(
            record.search_obs,
            engine=self._cfg.engine,
            limit=self._cfg.max_candidates,
            stats=record.candidate_filter,
            titles=titles,
        )
        log.info("[%s] 环节① 取回候选 %d 个（links=%d，过滤记账 %s）",
                 task_id, len(record.candidates),
                 len(record.search_obs.links), dict(record.candidate_filter))
        if not record.candidates:
            log.warning("[%s] 搜索页未提取到候选，run 提前结束", task_id)
            record.warnings.append(
                f"搜索页未提取到候选（links={len(record.search_obs.links)}, "
                f"degraded={list(record.search_obs.degraded)}，"
                f"过滤掉 {record.candidate_filter}）"
            )
            record.elapsed_ms = int((time.monotonic() - started) * 1000)
            return record

        # ── 环节 ①.4.5：候选钉进窗口（同源修复，04 方案）────────────
        # 根因（05 文档 L95-121）：候选从**全量** links 提取（111 条 → 5 个，
        # 真实候选 index≥50），而感知层只看 _link_window=前 LLM_LINKS(20)
        # 条。模型看不见候选，只能选窗口里的站内搜索链接，又被
        # excluded_host:baidu.com 剔出候选 → 选中 ∩ 候选 = 空 → kept=0。
        # 修复：把候选链接按 rank 钉到 links 最前（text 已含片名，落在
        # _ordered_links 的 hit 区），其余保原序——「候选 ⊆ 窗口」由构造保证。
        record.search_obs = dataclasses.replace(
            record.search_obs,
            links=search_step.pin_candidates_first(
                record.search_obs.links, record.candidates
            ),
        )
        # 「窗口 ⊇ 候选」强制校验（fail-closed：校验失败只记 warning、
        # **不中断采集**）。重放感知层排序，凡是候选不在窗口内（target_title
        # 为空 / 候选 text 无片名落 miss 区 / max_candidates 超窗）都必须在
        # 存档里可归因——否则又会回到「kept=0 却查无实据」静默成一整批。
        _out_of_window = search_step.candidates_outside_window(
            record.search_obs.links,
            record.candidates,
            target_title=record.title,
            window_size=PERCEPTION_LINK_WINDOW,
        )
        if _out_of_window:
            record.warnings.append(
                f"候选钉窗失败：{len(_out_of_window)}/{len(record.candidates)} "
                f"个候选不在感知层窗口（前 {PERCEPTION_LINK_WINDOW} 条）内，"
                f"模型将看不到它们，kept 可能仍为 0。"
                f"（{'、'.join((c.text or '')[:20] or c.href for c in _out_of_window[:3])}"
                f"{'…' if len(_out_of_window) > 3 else ''}）"
                f"**不中断采集**：fail-closed 管的是结论不是采集。"
            )

        # ── 环节 ①.5：判断点 ①（语义层，三路径分叉）───────────────
        # 「这个链接是不是能看《功夫》的站」是语义判断，代码做不了
        # （URL 里的 iqiyi.com 只说明它是爱奇艺）。取候选是代码层的
        # **结构性**过滤（搜索引擎自家、gov.cn、help 页），判断点 ①
        # 在它之后：先剔掉根本不是内容站的，再问「哪些是这个作品的播放站」。
        #
        # ⚠️ **None 时不中断**（fail-closed 管的是「结论」不是「采集」）。
        # W1 的规则版对 ① 只会返回 None，若据此中断，候选一个都不跑，
        # 负样本池永远空。同 IS_REACHABLE 的处理。
        #
        # 2026-10-16 路径 B/C：①B（选聚合站）/ ①C（选文章页）与 ①
        # 共用「选中保留 / 点名记负 / 未点名记 unresolved」的语义，
        # 只有题目、负分支名与 False 文案不同——参数化而不是复制三份，
        # 改一份漏两份的漂移比口径差异本身难查。
        if self._cfg.discovery_path == "aggregator":
            question, branch, gate = (
                Q.SELECT_AGGREGATOR_RESULTS, "not_aggregator", "判断点 ①B",
            )
            false_conclusion = "搜索结果里没有聚合站候选"
        elif self._cfg.discovery_path == "article":
            question, branch, gate = (
                Q.SELECT_ARTICLE_RESULTS, "not_article", "判断点 ①C",
            )
            false_conclusion = "搜索结果里没有文章/社区帖候选"
        else:
            question, branch, gate = (
                Q.SELECT_PLAY_SITES, "not_play_site", "判断点 ①",
            )
            false_conclusion = f"搜索结果里没有《{record.title}》的可看站点"
        selection = self._perceptor.decide(question, record.search_obs)
        #: **过滤前**的候选数。三条分支都要用它，而 ``record.candidates``
        #: 在过滤后就被覆盖了——用它打消息会得到「过滤后无候选（原有 0 个）」
        #: 这种自相矛盾的句子（实测踩过：明明原有 6 个却报 0 个）。
        n_candidates = len(record.candidates)
        if selection.answer is True:
            record.candidates = _apply_selection(
                record, selection, ledger, branch=branch, gate=gate,
            )
            record.candidate_source = "perceptor"
            log.info("[%s] %s 判 True：选出 %d/%d 个候选",
                     task_id, gate, len(record.candidates), n_candidates)
        elif selection.answer is False:
            # False 是货真价实的结论（「这页没有该片的可看站点」），
            # 与 IS_REACHABLE 的处理一致：**中断遍历**，后续判断无从谈起。
            # 但每个候选仍要记负分支——中断若不记账，这批结论
            # 就没有任何样本，而报表上只会看到「这批没跑出东西」。
            #
            # 判错的代价由人工复核承担：evidence 带着模型给的理由，
            # 重跑一次即可，不是不可逆。
            record.warnings.append(
                f"{gate} 判 False：{false_conclusion} "
                f"（{selection.evidence}）；{len(record.candidates)} 个候选"
                f"不访问，**其中被模型点名的记 {branch}、未点名的记 unresolved**"
            )
            log.warning("[%s] %s 判 False：%s（%s）；%d 个候选不访问",
                        task_id, gate, false_conclusion, selection.evidence,
                        len(record.candidates))
            rejected = {
                str(r.get("url") or "").strip()
                for r in (selection.payload.get("rejected") or [])
                if isinstance(r, dict)
            }
            unnamed = 0
            for candidate in record.candidates:
                key = candidate.source_href or candidate.url
                # 同款判据：判 False 只说明「整体没有可看站点」，逐条
                # **被否决**仍需要模型点名。没有点名就没有证据，
                # 而 {branch} 是负样本分支——无证据的负样本就是
                # §5.13 那种「理由通顺的假负样本」，只是形态更隐蔽。
                if key in rejected or candidate.url in rejected:
                    ledger.record(
                        candidate.url,
                        branch,
                        f"{gate} 判 False：{selection.evidence}",
                        decision=selection,
                        site_url=candidate.url,
                    )
                else:
                    unnamed += 1
                    ledger.record(
                        candidate.url,
                        "unresolved",
                        f"{gate} 判 False，但模型未把该候选列入 "
                        f"rejected——**无证据不记负样本**，"
                        f"转人工复核。整体判据：{selection.evidence}",
                        decision=selection,
                        site_url=candidate.url,
                    )
            if unnamed:
                record.warnings.append(
                    f"  其中 {unnamed} 个模型未点名，记 unresolved（**不入负样本池**）"
                )
            record.candidates = []
            record.candidate_source = "perceptor"
        else:
            record.candidate_source = "heuristic"
            log.info("[%s] %s 未取得结论（fail-closed），%d 个候选按"
                     "启发式全遍历：%s",
                     task_id, gate, n_candidates, selection.evidence)
            record.warnings.append(
                f"{gate} 未取得结论（{selection.evidence}）——"
                f"候选仍按代码启发式全遍历，不中断采集"
            )

        # ── 环节 ①.6：站点首页核（仅 direct）／中间页遍历（B/C）────
        if self._cfg.discovery_path == "direct":
            # 位置在 ① 之后而不是里面：① 判 True / False / None 三条路径
            # 都要过这道核，而 _apply_selection 只在 True 时被调。
            _drop_site_homepages(record, ledger)
            log.info("[%s] direct 首页核完成：%d 个候选进入环节②",
                     task_id, len(record.candidates))
        elif record.candidates:
            # B/C：**首页不丢**——聚合站入口 / 文章外链给出来的候选
            # 很多是站点首页，那是站内搜索（环节②）的入口而不是
            # 「找错站」。中间页访问链在这里产出最终候选。
            await self._visit_intermediates(record, ledger)

        # ── 环节 ①.7：搜索单元的终局反思 ──────────────────────────
        # 放在候选定稿**之后**：搜索单元的总结要说的是「搜到了什么、
        # 接下来核验哪几个」，而候选此时才定。搜索单元没有站点级
        # outcome（``outcome=None``），它的「处置」是进入下一单元。
        record.reflection = await self._write_reflection(
            record.steps, record, "搜索页", outcome=None
        )

        if not record.candidates:
            log.warning("[%s] %s 过滤后无候选（原有 %d 个，来源 %s），"
                        "run 提前结束",
                        task_id, gate, n_candidates, record.candidate_source)
            record.warnings.append(
                f"{gate} 过滤后无候选（原有 {n_candidates} 个，"
                f"来源 {record.candidate_source}）"
            )
            record.elapsed_ms = int((time.monotonic() - started) * 1000)
            return record

        # ── 环节 ②：逐站点遍历 ────────────────────────────────────
        # B/C 的首页型候选走站内搜索（visit 链 ③.5 分叉）；direct 全程
        # ``allow_site_search=False``，visit 行为与改动前逐字一致。
        successes = 0
        allow_site_search = self._cfg.discovery_path in ("aggregator", "article")
        for candidate in record.candidates:
            if (
                self._cfg.stop_after_success
                and successes >= self._cfg.stop_after_success
            ):
                break
            # 子候选（origin="site_search"）**不再触发**站内搜索：
            # 它们是站内搜索派发的深层页，再搜就是循环派发——
            # 「首页→搜索→结果页又含首页链接」的形态真会出现。
            result = await self._visit_guarded(
                candidate, ledger, record,
                allow_site_search=(
                    allow_site_search and candidate.origin != "site_search"
                ),
                title=record.title,
            )
            await self._write_rationales(
                result.steps, record, result.candidate.url[:60]
            )
            # 站点单元的终局反思：放在逐步 rationale **之后**——它要看的
            # 是全单元历史与最终判定，而判定此时才记进 ledger。
            outcome = _outcome_json_for(record, result)
            result.reflection = await self._write_reflection(
                result.steps,
                record,
                result.candidate.url[:60],
                outcome=outcome,
            )
            record.visits.append(result)
            log.info("[%s] 环节② %s → %s%s", task_id, candidate.url,
                     (outcome or {}).get("branch") or "无记账分支",
                     "（成功）" if result.success else "")
            # 站内搜索派发的子候选追加进遍历队列（B/C）：列表迭代按
            # 索引推进，append 的元素会被本循环吃到。同站多入口是
            # 有意的（site_search 的设计口径），不按 host 去重——
            # 去重发生在中间页产出最终候选那一步（_visit_intermediates）。
            if (
                result.search
                and result.search.status == site_search_step.ROUTED
            ):
                log.info("[%s] 站内搜索 routed：%d 个子候选入队（%s）",
                         task_id, len(result.search.selected),
                         "、".join(c.url for c in result.search.selected[:3]))
                overflow = 0
                for sub in result.search.selected:
                    if len(record.candidates) >= self._cfg.max_candidates:
                        overflow += 1
                        continue
                    record.candidates.append(sub)
                if overflow:
                    record.warnings.append(
                        f"站内搜索子候选 {overflow} 个超出 max_candidates="
                        f"{self._cfg.max_candidates} 上限，未入遍历队列"
                        f"（总量约束，非语义筛选）"
                    )
            if result.success:
                successes += 1

        record.ledger = ledger
        record.elapsed_ms = int((time.monotonic() - started) * 1000)
        log.info("[%s] 发现链结束：访问 %d 站，成功 %d，elapsed %dms，"
                 "warnings %d 条",
                 task_id, len(record.visits), successes, record.elapsed_ms,
                 len(record.warnings))
        return record

    async def _write_rationales(
        self, steps_log: Any, record: RunRecord, label: str
    ) -> None:
        """为一段动作流的**每一步**生成 rationale（模块 4，D2「边写边生成」）。

        **为什么在这里而不是 visit_site 里**：动作流是**记录**、rationale 是
        **补记**，两者在一次访问结束时才都完整。放进 ``visit_site`` 会让
        执行层直接依赖 LLM 模块——而依赖方向是 executor → perception，
        writer 属于 rationale/llm 那一侧。CLI 注入可调用对象，
        :mod:`trajectory_pipeline.executor.steps.visit` 因此完全不知道
        rationale 的存在。

        调用点有两处：**搜索步**（``record.steps``，label="搜索页"）与
        **逐站点访问步**（``result.steps``，label=站点 URL 截断）。搜索步
        也走同一 writer——P2 按决策单元切条，搜索阶段是正式的一条样本，
        它没有 rationale 会让整条搜索样本恒 ``gate=not_run``、D-9 永远
        不可判定（2026-10-11 裁定为漏生成而非设计豁免，见 rubric 对齐账）。
        ``StepLog`` 是通用动作流，两个调用点都能 ``attach_rationale``。

        **D2 的实质是「生成时能看到那一步的动作与观察」，而不是
        「必须在线程内即时生成」**：两者在同一份数据上等价。而事后补记
        换来的是——writer 挂掉只少一段 rationale，不影响采集。

        **失败一律记账不抛异常**：rationale 缺失不该让一次真实访问整段作废。
        三种状态（``written`` / ``unavailable`` / ``empty``）原样落进 P1，
        由组装期决定闸门与 ``rationale_missing``——**别在这里判**。
        """
        if self._writer is None:
            return
        steps = steps_log.steps
        for index, step in enumerate(steps):
            try:
                payload = self._writer(
                    action=step.action.to_json(),
                    user_prompt=record.user_prompt,
                    target_title=record.title,
                    # INC46：**不喂本步观察**。历史是 ``(动作, 观察)`` 对——
                    # 动作前动机只该依据此前看到的东西（见
                    # ``rationale.writer.render_history``）。
                    history=[
                        (s.action.to_json(), _obs_json(s.observation))
                        for s in steps[:index]
                    ],
                )
            except Exception as exc:  # noqa: BLE001 — 见上方口径
                record.warnings.append(
                    f"rationale 生成异常（{label} 第 {index} 步）："
                    f"{type(exc).__name__}: {exc}——该步无 rationale，闸门记 not_run"
                )
                continue
            steps_log.attach_rationale(index, payload)

    async def _write_reflection(
        self,
        steps_log: Any,
        record: RunRecord,
        label: str,
        *,
        outcome: Mapping[str, Any] | None = None,
    ) -> dict[str, Any] | None:
        """为一个决策单元写**终局反思**，落 unit 级 ``reflection`` 键。

        INC46 的另一半：动作前动机把「事后口吻」从第 ``i`` 步赶了出去，
        但**收尾那一句总结本来就该是事后的**——它要说的是「这一趟下来
        确认了什么、怎么处置」，而所有观察此时都已是历史。

        与 :meth:`_write_rationales` 分开成两个入口而不是给逐步入口加个
        ``terminal`` 开关：两者的提示词、payload 形状、时序立场都不同，
        合成一个函数会让「本步观察能不能喂」这个最关键的区别退化成
        布尔参数的副作用。

        为什么挂在 ``writer.reflect`` 而不是复用 ``self._writer`` 本身：
        ``self._writer`` 是**逐步**入口（CLI 注入的可调用对象，签名是
        ``write_one`` 的 kwargs）。反思入口用 :func:`getattr` 取，
        **取不到就跳过**——纯函数的 writer（单测里的假 writer）本来
        就不该被要求实现第二个入口，而跳过的结果是这一单元没有
        reflection，组装期退回纯 disposition 话术，不是错误。

        Returns:
            reflection 的 JSON（``WriteResult.to_json`` 形状），
            ``None`` = 没生成。**返回而不是直接挂上**，因为 reflection
            是 **unit 级**的：搜索单元挂在 ``RunRecord``、站点单元挂在
            ``VisitResult``，两者都不是 ``StepLog`` 的字段——
            往动作流上挂一个不属于任何一步的东西，就是后来
            「这条总结到底属于哪一步」这种问题的来源。
        """
        if self._writer is None:
            return None
        reflect = getattr(self._writer, "reflect", None)
        if reflect is None:
            return None
        steps = steps_log.steps
        try:
            return reflect(
                user_prompt=record.user_prompt,
                target_title=record.title,
                history=[
                    (s.action.to_json(), _obs_json(s.observation)) for s in steps
                ],
                outcome=outcome,
            )
        except Exception as exc:  # noqa: BLE001 — 同逐步入口的纪律
            record.warnings.append(
                f"reflection 生成异常（{label}）：{type(exc).__name__}: {exc}"
                f"——该单元无 reflection，收尾退回纯 disposition 话术"
            )
            return None

    async def _result_titles(self) -> tuple[Mapping[str, str], str]:
        """取当前搜索页的「结果标题 ↔ URL」。返回 ``(映射, 失败原因)``。

        **引擎选择器会随改版失效，所以这里只认「抽到了就是抽到了」**：
        选择器失效、字段不等长、tool 报错，一律返回空映射 + 原因，
        由调用方决定后果（判断点 ① 退回 fail-closed）。反过来做——
        抽到了就信——才危险：一条错误对应关系会直接变成 ① 的唯一判据。

        单次 tool 失败**不重跑**：重试同一个选择器只会再失败一次。

        失败原因随返回值走，**不挂在实例属性上**：orchestrator 会跑多个
        task，挂在实例上前一个 task 的失败会渗进后一个 task 的存档。
        """
        fields = search_step.RESULT_SELECTORS.get(self._cfg.engine)
        if not fields:
            return {}, f"{self._cfg.engine} 未登记结果标题选择器"
        try:
            extracted = await self._driver.extract(fields)
        except Exception as exc:
            return {}, f"browser_extract 失败 {type(exc).__name__}: {exc}"
        titles = search_step.result_titles(extracted)
        if not titles:
            # 字段名写上游**真实返回**的那两个（不带 `[]`）——
            # 这条串是给人读的，照着入参写会让人去查一个不存在的键。
            return {}, (
                f"{search_step.TITLE_FIELD}/{search_step.URL_FIELD} "
                f"长度不等或为空（抽到字段 {sorted(extracted)}）"
            )
        return titles, ""

    async def _visit_guarded(
        self,
        candidate: Candidate,
        ledger: RunLedger,
        record: RunRecord,
        *,
        allow_site_search: bool = False,
        title: str = "",
    ) -> visit_step.VisitResult:
        """单站点遍历的超时与异常兜底。

        单个站点超时不该拖垮整批——但**必须记账**，否则这次运行会因为
        「超时」而在样本分布里消失，变成一条看不见的损失。
        """
        result = visit_step.VisitResult(candidate=candidate)
        try:
            return await asyncio.wait_for(
                visit_step.visit_site(
                    self._driver,
                    self._perceptor,
                    ledger,
                    candidate,
                    max_chars=self._cfg.max_chars,
                    allow_site_search=allow_site_search,
                    title=title,
                ),
                timeout=self._cfg.per_site_timeout_s,
            )
        except asyncio.TimeoutError:
            log.warning("[%s] 站点遍历超时 %s（>%ss）",
                        record.task_id, candidate.url,
                        self._cfg.per_site_timeout_s)
            ledger.record(
                candidate.url,
                "unresolved",
                f"站点遍历超时（>{self._cfg.per_site_timeout_s}s）",
                site_url=candidate.url,
            )
            result.note("超时")
            record.warnings.append(f"{candidate.url} 超时")
            return result
        except Exception as exc:
            log.warning("[%s] 站点遍历异常 %s：%s: %s",
                        record.task_id, candidate.url,
                        type(exc).__name__, exc)
            ledger.record(
                candidate.url,
                "unresolved",
                f"站点遍历异常: {type(exc).__name__}: {exc}",
                site_url=candidate.url,
            )
            result.note(f"异常: {exc}")
            record.warnings.append(f"{candidate.url} 异常: {exc}")
            return result

    # ── 路径 B/C：中间页遍历（2026-10-16）─────────────────────────

    def _intermediate_visit(self):
        """当前发现路径对应的中间页访问函数与角色标签。"""
        if self._cfg.discovery_path == "aggregator":
            return (
                aggregator_step.visit_aggregator,
                aggregator_step.ROLE_AGGREGATOR,
            )
        return article_step.visit_article, aggregator_step.ROLE_ARTICLE

    async def _visit_intermediates(
        self, record: RunRecord, ledger: RunLedger
    ) -> None:
        """B/C 路径：遍历中间页候选（聚合站页 / 文章页），产出最终候选。

        三个预算纪律（耗尽都是正常终态，记 warning 不中断不重试）：

        - ``max_intermediates``：访问几个中间页。截断保序——选题给出
          的顺序就是优先级，代码没有依据重排。
        - ``max_referral_links``：每个中间页校验几条外链（在
          :func:`visit_aggregator` / :func:`visit_article` 内截断）。
        - ``max_candidates``：最终候选总量。

        产出候选按 host **跨中间页累计**去重：两个聚合站引荐同一个站，
        第二个是重复发现不是新信息；host 未知的候选（opaque 重定向）
        不去重——没有依据的合并与没有依据的排除是同一种错。
        """
        visit, _role = self._intermediate_visit()
        pool = record.candidates[: self._cfg.max_intermediates]
        if len(record.candidates) > self._cfg.max_intermediates:
            record.warnings.append(
                f"中间页候选 {len(record.candidates)} 个超上限 "
                f"{self._cfg.max_intermediates}，截断为前 "
                f"{self._cfg.max_intermediates} 个（预算控制，非语义筛选）"
            )
        log.info("[%s] 中间页遍历开始：%d 个候选（上限 %d）",
                 record.task_id, len(pool), self._cfg.max_intermediates)
        final: list[Candidate] = []
        seen_hosts: set[str] = set()
        budget_hit = False
        for candidate in pool:
            if budget_hit:
                break
            ir = await self._intermediate_guarded(candidate, ledger, record)
            record.intermediates.append(ir)
            log.info(
                "[%s] 中间页（%s）%s → 产出 %d 个候选%s",
                record.task_id, ir.role, candidate.url, len(ir.candidates),
                f"（{'；'.join(ir.notes)}）" if ir.notes else "",
            )
            for c in ir.candidates:
                if len(final) >= self._cfg.max_candidates:
                    budget_hit = True
                    break
                host = c.host
                if host:
                    if host in seen_hosts:
                        continue
                    seen_hosts.add(host)
                final.append(c)
        if budget_hit:
            record.warnings.append(
                f"中间页产出候选达 max_candidates={self._cfg.max_candidates}"
                f" 上限，截断（总量约束，非语义筛选）"
            )
        record.candidates = final
        log.info("[%s] 中间页遍历完成：最终候选 %d 个（host 去重后）",
                 record.task_id, len(final))

    async def _intermediate_guarded(
        self,
        candidate: Candidate,
        ledger: RunLedger,
        record: RunRecord,
    ) -> IntermediateResult:
        """中间页访问的超时与异常兜底（与 :meth:`_visit_guarded` 同款纪律）。

        外链 ``dropped`` 的记账走 ``stats=record.candidate_filter``——
        与直搜链 ``extract_candidates`` 的过滤记账同一可见性口径，
        P1 里「引擎给了多少、我们丢了多少、为什么丢」一张表看全。
        """
        visit, role = self._intermediate_visit()
        try:
            return await asyncio.wait_for(
                visit(
                    self._driver,
                    self._perceptor,
                    ledger,
                    candidate,
                    max_chars=self._cfg.max_chars,
                    max_links=self._cfg.max_referral_links,
                    engine=self._cfg.engine,
                    stats=record.candidate_filter,
                ),
                timeout=self._cfg.per_site_timeout_s,
            )
        except asyncio.TimeoutError:
            log.warning("[%s] 中间页 %s 超时（>%ss）",
                        record.task_id, candidate.url,
                        self._cfg.per_site_timeout_s)
            ledger.record(
                candidate.url,
                "unresolved",
                f"中间页访问超时（>{self._cfg.per_site_timeout_s}s）",
                site_url=candidate.url,
            )
            ir = IntermediateResult(role=role, candidate=candidate)
            ir.note("超时")
            record.warnings.append(f"中间页 {candidate.url} 超时")
            return ir
        except Exception as exc:
            log.warning("[%s] 中间页 %s 异常：%s: %s",
                        record.task_id, candidate.url,
                        type(exc).__name__, exc)
            ledger.record(
                candidate.url,
                "unresolved",
                f"中间页访问异常: {type(exc).__name__}: {exc}",
                site_url=candidate.url,
            )
            ir = IntermediateResult(role=role, candidate=candidate)
            ir.note(f"异常: {exc}")
            record.warnings.append(f"中间页 {candidate.url} 异常: {exc}")
            return ir
