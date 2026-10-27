"""主控制流——「搜索 → 取候选 → 逐站点遍历 → 记账」。

与存量编排最大的区别：**这个文件里没有 ``if`` 判断业务语义**。
所有「这是不是播放站 / 有没有播放控件 / 是不是播放页」的判断都在
:class:`Perceptor` 里，控制流只负责按答案分流。这不是风格偏好——
LLM 版的判断来自远端模型输出，把它写进 ``if`` 就等于让代码去猜模型的语义，
而那正是 v2 要消灭的东西。

「代码层做护栏、LLM层做探索」在这里体现为：

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
from datetime import datetime, timezone
from typing import Any, Mapping

from trajectory_pipeline.executor import actions
from trajectory_pipeline.executor.actions import StepLog
from trajectory_pipeline.executor.branches import (
    RunLedger,
    validate_observation_for_decision,
)
# 采集面词表住在 ``executor/browser/``（方案 20 §R3）：面是**驱动实现的属性**，
# 账本与评估两侧都要用，谁也别反向 import 谁。
from trajectory_pipeline.executor.browser import access_surface
from trajectory_pipeline.executor.dom import DEFAULT_MAX_CHARS, detect_block
from trajectory_pipeline.executor.gatekeeper import BLOCK_VERDICTS, FailClosed, intercept
from trajectory_pipeline.executor.mode import AGENT, EXECUTOR_MODES
from trajectory_pipeline.executor.steps import (
    agent_intermediate,
)
from trajectory_pipeline.executor.steps import agent_visit as agent_visit_step
from trajectory_pipeline.executor.steps import aggregator as aggregator_step
from trajectory_pipeline.executor.steps import article as article_step
from trajectory_pipeline.executor.steps import search as search_step
from trajectory_pipeline.executor.steps import site_search as site_search_step
from trajectory_pipeline.executor.steps import visit as visit_step
from trajectory_pipeline.executor.steps.aggregator import IntermediateResult
from trajectory_pipeline.executor.steps.search import Candidate
from trajectory_pipeline import toolcatalog
from trajectory_pipeline.perception import questions
from trajectory_pipeline.common.elements import element_json
from trajectory_pipeline.perception.base import (
    Decision,
    LinkItem,
    Observation,
    Perceptor,
    Q,
)

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
#: 落盘的**元素名**裁到多少字符（``tag``/``label`` 时代的 ``label[:120]``）。
#: 名字本身不截断在内存里——P1 是证据，裁的只是落盘那份。
OBS_ELEMENT_NAME_LIMIT = 120

#: 感知层送模型的**链接窗口宽度**（= ``perception.llm_perceptor.LLM_LINKS``）。
#: executor 不能 import 感知层实现（01-架构总览 L49-50：executor 只依赖
#: ``perception/base.py`` 协议），所以这里复制一份，由单测断言与
#: ``LLM_LINKS`` 相等（test_search_pin.py::test_窗口常量与感知层同步），
#: 防止两侧漂移。
#:
#: 2026-10-16 方案 12 阶段 A1 起语义从「钉窗校验宽度」变为**分类任务的
#: 分批宽度**（:meth:`Orchestrator._classify_candidates` 按它把候选切成
#: 批，每批一份候选视图）——04 方案 §2.4-3 的 ``max_candidates ≤
#: LLM_LINKS`` 约束随之解除：超出的候选分批送，不再被窗口裁掉。
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
    #: **单站超时**（B-4 异常处置唯一的时限判据）。
    #:
    #: ⚠️ **90 → 180（2026-10-22，切 camoufox 后）**：90 是 obscura 时代定的
    #: （obscura 单步快）。camoufox 单步慢一个量级——真人鼠标轨迹回放带
    #: 停顿与 overshoot，加上 ``_settle`` 有界等 ``domcontentloaded``（2s），
    #: 实测单次点击上界 **13.3s**（headful 最坏；headless 下 34–71ms 不慢）。
    #: ``AgentBudget.max_steps=8`` ⇒ 8 步 × 15.3s ≈ **122s**，90 顶不住。
    #:
    #: **2026-10-22 再加一段**：``_settle`` 现在**先等新文档 commit**
    #: （有界 2.5s，见 ``camoufox_driver`` 的「动作之后的读稳」）——不等的
    #: 代价是**读到一半**（第一批生产批实测：同一页两次运行 ``iframe_count``
    #: 读出 3 与 0，那个 0 不是页面事实）。每步最坏再 +2.5s ⇒
    #: 8 步 × 17.8s ≈ **142s**，180 仍顶得住（余量从 58s 降到 38s）。
    #:
    #: 撞顶的后果**不是"慢"而是"错"**：站点遍历超时会被记成一条
    #: ``unreachable_hard`` ——**理由通顺的假负样本**（H-15/H-17 那一类）。
    #: 180 ≈ 1.3× 于实测上界，留了余量。
    #:
    #: ⚠️ 它曾经**连 ``--help`` 都查不到**（CLI 不设它，只在源码里）——
    #: 2026-10-22 起 ``--per-site-timeout`` 暴露出来，且**落进 ``run_config``**
    #: （B-4 的判据是它，不落盘就等于没有判据）。
    per_site_timeout_s: float = 180.0
    #: 发现路径（2026-10-16 路径 B/C）。默认 ``direct``：既有批次与
    #: 全部既有调用点**零改动**——「默认值不改变现状」是加配置的第一纪律。
    discovery_path: str = "direct"
    #: B/C：最多访问几个中间页（聚合站 / 文章）。预算耗尽只记 warning
    #: 不中断——那是「这一轮发现链的广度上限」，不是采集故障。
    max_intermediates: int = 3
    #: B/C：每个中间页最多校验几条引荐外链。外链校验每条都要开 tab
    #: 真实访问，上限是预算控制不是语义筛选。
    max_referral_links: int = 5
    #: 执行器模式（2026-10-17 方案 13 §6.1，A3-1 从私有环境变量转正为
    #: 配置字段）。``scripted`` = 固定序列（:func:`~.steps.visit.visit_site`），
    #: ``agent`` = 站内自主导航（:func:`~.steps.agent_visit.agent_visit`）。
    #: 默认 ``scripted``：既有调用点零改动——「默认值不改变现状」是
    #: 加配置的第一纪律。
    #:
    #: 取值合法性在 ``__post_init__`` 查（:mod:`.mode` 的纪律原文）：
    #: 未知值**报错**不猜——拼错会静默跑成 scripted 批，而存档里的
    #: ``executor_mode`` 会诚实写着 ``"scripted"``，A/B 对比那天没人
    #: 分得清那是没灰度还是打错了字。
    executor_mode: str = "scripted"

    def __post_init__(self) -> None:
        if self.discovery_path not in DISCOVERY_PATHS:
            raise ValueError(
                f"未知发现路径 {self.discovery_path!r}；可用：{DISCOVERY_PATHS}。"
                f"拼错路径会静默跑成 direct，而那批存档的归因就是错的——"
                f"与其猜，不如当场报错。"
            )
        if self.executor_mode not in EXECUTOR_MODES:
            raise ValueError(
                f"未知执行器模式 {self.executor_mode!r}；可用：{EXECUTOR_MODES}。"
                f"拼错会静默跑成 scripted 批，而存档里的 executor_mode "
                f"会诚实写着 'scripted'——A/B 对比那天没人分得清那是"
                f"没灰度还是打错了字。"
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
    #: **执行器模式**（2026-10-17 方案 13 §6.1，A2a 埋点）：
    #: ``scripted`` = 固定序列（:func:`~.steps.visit.visit_site`），
    #: ``agent`` = 站内自主导航（:func:`~.steps.agent_visit.agent_visit`）。
    #:
    #: 与 ``discovery_path`` **同一个理由单独存一份**：它是批次级属性，
    #: 不是候选级——A/B 对比要看的是「这一整批是谁跑的」。A3-1 起
    #: 取值来自 :attr:`RunConfig.executor_mode`，本字段仍是「实际走的
    #: 那条链」（与 run_config 是「参数快照」同理）。
    #:
    #: 默认 ``scripted`` 刻意在**构造期**定下（:mod:`.mode` 的纪律）：
    #: 已知的两条链在 P1 里同形，一旦允许逐候选切换，这批存档的
    #: 归因就回答不了「这条轨迹是哪种执行器产的」。
    executor_mode: str = "scripted"
    #: **执行层（驱动实现）**（2026-10-20 方案 15 §7 R1）：``obscura`` /
    #: ``camofox``。
    #:
    #: 与 :attr:`executor_mode` **同一个理由单独存一份**：它是批次级属性
    #: ——「这批是哪个执行层跑的」与「这批是 scripted 还是 agent 跑的」
    #: 是**两个正交的归因轴**，缺一个读档人就分不清。
    #:
    #: 它还是**工具面**的唯一来源：目标编码（``{tag,label}`` vs
    #: ``{role,name}``）由驱动决定，训练侧的声明按它折算
    #: （:func:`trajectory_pipeline.toolcatalog.surface_of_driver`）。
    #:
    #: **空串 = 没记**（不是「未知驱动」）：历史批次没有这个字段，而那批
    #: 真实跑的就是 obscura。补一个默认值等于凭空断言这份存档属于哪一批。
    driver: str = ""
    #: **执行层的指纹读数**（2026-10-22 P6 批次元数据，owner 裁定切 camoufox 后
    #: 从「未开工」变「必须」）。
    #:
    #: ⚠️ **形状随执行层变**（2026-10-26 换成 camofox）：
    #:
    #: - **camofox 面**（现行生产面）：**九项实测**（浏览器侧读回来）
    #:   ``user_agent`` / ``platform`` / ``language`` / ``languages`` /
    #:   ``timezone`` / ``screen`` / ``window`` / ``dpr`` / ``webdriver``
    #:   + 两项**本仓库能控的**（``engine`` / ``base_url``，后者是上游 REST
    #:   服务的 **origin**，已去掉路径与凭据）。
    #:   ⚠️ camofox 的 ``humanize`` / ``geoip`` / ``locale`` **由上游决定，
    #:   本仓库配不了**——那不是「读数」，是「管不着的事」，**不写成读数**。
    #: - **camoufox 面**（存量，已撤下）：九项**混合语义**——``screen`` /
    #:   ``window`` / ``dpr`` 是**实测值**，``humanize`` / ``geoip`` /
    #:   ``main_world_eval`` / ``headless`` / ``locale`` / ``os`` 是**请求值**
    #:   （没有可读回来的形态）。两种语义混在一份 dict 里，所以读侧必须
    #:   知道哪几项可信。
    #:
    #: ⚠️ **为什么它必须进存档**：指纹只在 launch 那一刻确定，
    #: **事后补不回来**。而 camoufox 的 ``window=`` 钉的是**外框**——
    #: 视口 = 外框 − chrome ⇒ **内高逐次摆动**。
    #: 不记它，批次分布的变化就会被读成「站点变了」而不是「指纹漂了」。
    #:
    #: ⚠️ **生产口径（``headless=True``）的实测读数**（2026-10-22，
    #: 5 次 ``check --driver camoufox``）：``screen`` ``[1920, 1080]``
    #: 恒定 · ``window``（inner）``[1920, 947]`` ×2 / ``[1920, 955]`` ×3 ·
    #: ``dpr`` ``1`` · ``timezone`` ``Asia/Shanghai``。
    #: **内宽恒 ``1920``**——**别拿 headful 的 ``1905`` 去比 headless 的
    #: 存档**（headful 口径才是 1905，见[设计方案 04 §4.3.1](../../../trajectory_pipeline/docs/设计方案/04-camoufox执行层替换方案.md)）。
    #:
    #: ⚠️ **「摆动 8px」是探针口径的读数，生产口径已经把它推翻**
    #: （2026-10-22 生产批补记）：第一批生产批
    #: （``output/pipeline/camoufox_prod1/``）8 条里读到的内高是
    #: **``909`` / ``947`` / ``955`` / ``965``——跨度 56px**。
    #: ⇒ **别把 955 当上界**：拿它当上界，``909`` 会被读成「指纹异常」，
    #: 而它其实是常态。**读法是「只认实测值」**，不是「拿某个数当基准」。
    #: 该批其余八项**全部恒定**
    #: ⇒ **漂的只有 chrome 高度（⇒ inner 高），不是窗口尺寸本身**。
    #:
    #: **空 dict = 没记**，不是「没有指纹」：obscura 是远端 MCP，
    #: 本地看不到它的指纹——那是「没测到」，不是「测出来是空的」。
    #: 两者混用会让「obscura 批次没有指纹」被读成「obscura 批次指纹为空」。
    fingerprint: dict[str, Any] = field(default_factory=dict)
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
    #: **本 run 的短哈希**（2026-10-19 增量六十一补）。
    #:
    #: 历史上只活在文件名 ``<task_id>__<run_id>.json`` 与 :attr:`batch_id`
    #: 同源——读侧只能从路径反推,而 task_view 渲染、LS 上传、批次报表
    #: 全需要它在 JSON 顶层。「在 writer 边界临时注入」是隐性耦合:
    #: RunRecord 不知情、测试 fixture 不造、读档人从路径反推才看得到。
    #: 现在下沉为 RunRecord 字段(默认空串,writer 边界填入,读档人直接看),
    #: 与 ``executor_mode`` / ``batch_id`` 同款——不可省略的运行级审计项。
    run_id: str = ""
    #: **本 run 所属的批次 id**（同 :attr:`run_id` 增补原因,2026-10-19）。
    #:
    #: ``T001`` / ``T002`` …来自 :class:`~.executor.task_generator.Plan`
    #: 顶层,作为批次报表切片键。空串 = 不在 plan 跑出来的批次(手工 / 测试)。
    batch_id: str = ""
    #: **Gatekeeper 事件账本**（2026-10-16 方案 12 阶段 G / D-A3 最小账本）。
    #: 每条 ``{stage, verdict, reason, disposition}``——拦截发生在哪个阶段、
    #: 判定结论（:class:`~trajectory_pipeline.executor.gatekeeper.Verdict`
    #: 封闭取值集）、原始原因串、处置档（D-A2 拍板：只有
    #: InjectGuidance / EscalateModel / FailClosed 三档且**全部可见**，
    #: 静默自愈已否决——「被拦→修复→重试」是训练信号本身）。
    #:
    #: 与 ``search_blocked`` 的分工：后者是搜索页拦截的**现行运行级字段**
    #: （报表口径已冻结，不进分支体系）；本字段是统一账本——A2 起
    #: agent 循环每步的拦截都记这里，将来接 Langfuse 就是它的投影（D-A3）。
    #: 空 list = 本次运行没有拦截事件（**与「没接线」同形是已知代价**，
    #: 阶段 G 只有搜索页一处写入点，A2 起写入点才多起来）。
    gatekeeper_events: list[dict[str, Any]] = field(default_factory=list)
    #: **LLM 调用账本**（2026-10-17 方案 13 §3.6，A2a-4）。每条
    #: ``{ts, caller, candidate, model, latency_ms, prompt_tokens,
    #: completion_tokens, error}``——agent 循环每次问模型记一行。
    #:
    #: 与 ``gatekeeper_events`` 同款可选字段纪律（不升 ``schema_version``）：
    #: 老存档没有它。空 list 照落——「这次运行没问模型」（scripted 批）
    #: 与「没这个键」在下游是两回事。
    #:
    #: **记账范围刻意只圈 agent_loop**（§3.6 的 A2a 决策）：感知层十一题
    #: 的调用不汇入——Decision 已有 latency_ms，把账本传进 perceptor 是对
    #: 感知层的侵入，而账本的用途是「agent 批次成本可比」，不是全管线审计。
    #: token 数为 ``None``（不是 0）= 后端没给 usage，与「这一调用一个
    #: token 没花」是两回事（理由见 ``llm.client._usage_of``）。
    #:
    #: ⚠️ ``ts`` 的粒度是**候选级**：循环体不引入时间源（否则每条记账都
    #: 是时间相关的不可测代码），由 orchestrator 在候选访问收口时统一加盖，
    #: 同一候选的各条共享一个时间戳。候选内的次序由 list 顺序保证，单条
    #: 耗时由 ``latency_ms`` 给出——ts 要回答的只是「这批成本是哪段时间
    #: 烧的」。超时/异常收口的候选可能带着没盖戳的残条落盘，那是诚实残账。
    llm_calls: list[dict[str, Any]] = field(default_factory=list)
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
        # 执行层指纹同款防引用共享（与 provenance/run_config 同理：
        # 指纹来自驱动的 session，而 session 是活的——调用方在归档前
        # 再读一次 driver.fingerprint 拿到的是**同一个 dict**，
        # 改它就等于改存档）。
        self.fingerprint = dict(self.fingerprint or {})
        # gatekeeper_events 同款防引用共享（与 provenance/run_config 同理：
        # 事件由 orchestrator 独占构造，但 RunRecord 也可能被直接构造）。
        self.gatekeeper_events = list(self.gatekeeper_events or [])

    @property
    def succeeded(self) -> bool:
        return any(v.success for v in self.visits)

    def to_json(self) -> dict[str, Any]:
        """序列化。刻意只保留**可审计**的字段，不留页面句柄。

        元素与目标的那两个槽**按方言投影**（P4）：一次 run 只有一个驱动，
        所以整份存档**只有一个方言**。方言来源见
        :func:`_vocabulary_of_run`——首选观察自己记下的，没有观察时才按
        ``driver`` 折算，认不出的 ``driver`` 在那一步**抛**。
        """
        vocabulary = _vocabulary_of_run(self)
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
            # Gatekeeper 事件账本（方案 12 阶段 G）。空 list 照落——
            # 「没有拦截事件」与「没这个键」在下游是两回事（缺件代理教训）。
            # ``blocked_surface`` / ``attempt`` 在这里盖（方案 20 §R3）——
            # 面从 ``driver`` 折算，见 :func:`_stamped_gatekeeper_events`。
            "gatekeeper_events": _stamped_gatekeeper_events(self),
            # LLM 调用账本（方案 13 §3.6，A2a-4）。空 list 照落——
            # scripted 批「没问模型」与「没这个键」在下游是两回事。
            "llm_calls": [dict(c) for c in self.llm_calls],
            "candidate_filter": dict(self.candidate_filter),
            "run_config": dict(self.run_config),
            "provenance": dict(self.provenance),
            "elapsed_ms": self.elapsed_ms,
            "warnings": list(self.warnings),
            "search_observation": _obs_json(self.search_obs),
            "steps": [s.to_json(_obs_json, vocabulary) for s in self.steps.steps],
            # 搜索单元的终局反思（INC46）。与 ``steps`` 平级——它不是某一步
            # 的 rationale，是整个搜索单元看完之后的总结。
            "reflection": dict(self.reflection) if self.reflection else None,
            "discovery_path": self.discovery_path,
            # 执行器模式（方案 13 §6.1，A2a）。**空串也不写**：这是
            # 「这批是谁跑的」的唯一答案，缺了它 A/B 两批存档在磁盘上
            # 长得一模一样——而它们的可比性假设根本不同（方案 12 §5.3
            # 第 3 条：agent 批的重跑可比性不成立）。
            "executor_mode": self.executor_mode,
            # 执行层（2026-10-20 方案 15 §7 R1）。**空串也写**：
            # 「这批是哪个执行层跑的」与「这批是哪种链跑的」是两个正交
            # 的归因轴，缺了这个字段，两批不同执行层的存档在磁盘上
            # 长得一模一样——而它们的动作编码不同义，混池就是造数据。
            "driver": self.driver,
            # 执行层指纹读数（2026-10-22 P6）。**空 dict 也写**——
            # 与 ``driver`` / ``executor_mode`` 同款约定：「没记」与
            # 「记了但是空」在下游是两回事（缺件代理教训）。
            "fingerprint": dict(self.fingerprint),
            # run_id / batch_id(2026-10-19 增量六十一):与 filename 同源,
            # 之前只活在 writer 边界注入,下沉为 RunRecord 字段后读档人
            # 直接看 JSON 顶层。空串 = 不在 plan 跑出来的批次(手工 / 测试),
            # 不会因「字段不存在」与「批次外运行」混淆。
            "run_id": self.run_id,
            "batch_id": self.batch_id,
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
                    "steps": [
                        s.to_json(_obs_json, vocabulary) for s in ir.steps.steps
                    ],
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
                    "steps": [
                        s.to_json(_obs_json, vocabulary) for s in v.steps.steps
                    ],
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


def _stamp_llm_calls(calls: list[dict[str, Any]]) -> None:
    """给账本里**还没盖戳**的条目补 ``ts``（方案 13 §3.6，A2a-4）。

    为什么不盖在循环里：``agent_loop._record`` 刻意不引入时间源——每条
    记账都变成时间相关的不可测代码。所以 ts 由调用方在**候选访问收口时**
    统一加盖（``RunRecord.llm_calls`` 字段 docstring 里写明的候选级粒度
    口径：同候选各条共享一个戳，候选内次序看 list 顺序，单条耗时有
    ``latency_ms``）。

    只补**缺戳**的（``setdefault``）：多个候选 sequential 共用同一个
    账本 list，先访问的候选已经盖过，重盖会把同一批条目刷成不同时间——
    账本的 ts 是该候选的成本封口时间，不是「最后一次序列化的时间」。
    """
    ts = datetime.now(timezone.utc).isoformat(timespec="seconds")
    for entry in calls:
        entry.setdefault("ts", ts)


def _access_surface_of(driver: str) -> str:
    """驱动名 → 采集面；认不出的**记 ``unrecorded`` 并点名**，不为它抛。

    只在**事后记账**（账本落盘）这一侧用。写金标那边（``builder.annotate`` /
    ``freeze``）走的是 ``access_surface.surface_of_driver`` 的**抛**口径——
    两处的差别见 :func:`_stamped_gatekeeper_events` 的 docstring。
    """
    try:
        return access_surface.surface_of_driver(driver)
    except ValueError:
        log.warning(
            "认不出的驱动实现名 %r：gatekeeper 账本的 blocked_surface 记 "
            "unrecorded（不猜是哪张面）", driver,
        )
        return access_surface.SURFACE_UNRECORDED


def _stamped_gatekeeper_events(record: "RunRecord") -> list[dict[str, Any]]:
    """拦截账本 → 落盘形态，补 ``blocked_surface`` / ``attempt``（方案 20 §R3）。

    两个字段回答两个不同的问题：

    - ``blocked_surface`` —— **在哪张采集面上被拦**。面只从
      ``RunRecord.driver`` 经 :func:`access_surface.surface_of_driver` 折算，
      **不在采集点手填**：agent 循环不知道驱动实现名，让它自报就是造第二处
      推断（CLAUDE.md 纪律 7）。

      ⚠️ 认不出的 ``driver`` 在这里落 ``unrecorded`` + 一条 warning，**不抛**
      ——这与 ``surface_of_driver`` 的「抛」不矛盾，因为**场合不同**：
      抛的那两处（``annotate`` / ``freeze``）写的是**金标**，一笔写错会跟着
      版本走；这里写的是**运行账本的一栏归因**，而 ``to_json`` 是落盘边界
      ——一条认不出的驱动名（重放的历史存档 / 手搓的 record / 测试替身
      ``FakePageDriver.name == "fake"``）不该让整份存档写不出去。生产路径上
      这个分支只可能由手改产生：``driver`` 取自驱动类自己的 ``name``，而
      ``--driver`` 是 ``choices=SUPPORTED_KINDS``。
    - ``attempt`` —— **同一 verdict 在本 run 里是第几次**（1 起）。
      不合并成一个「被拦次数」：CF 拦两次与 captcha 拦一次处置不同
      （前者换引擎才有救，后者多半是间隔问题），合成一个数会让两种情形
      在报表上是同一个格子（避坑 §3.7 的同一纪律：拦截率必须分桶）。
      全 run 被拦几次 = 有 ``blocked_surface`` 的行数。

    只盖 ``verdict ∈ gatekeeper.BLOCK_VERDICTS`` 的事件（四条页面侧访问控制：
    CF 拦截 / 验证码 / 登录墙 / 付费墙）。其余一律**不盖**：

    - 升配 / 驳回两档没有 verdict——它们不是「被拦」，给它们安 ``attempt``
      会让「这次运行被拦几次」数出模型升配的次数；
    - ``dead_loop`` 有 verdict 但那是**行为失败**，不是页面属性（见
      :data:`~...gatekeeper.BLOCK_VERDICTS` 的说明）。

    没盖的键就是没有，**不写空串**。

    为什么不盖在循环里（与 :func:`_stamp_llm_calls` 同款理由）：``agent_loop``
    刻意不引入驱动实现名的知识，事件格式在那边只有 ``stage/verdict/reason/
    disposition`` 四栏；盖戳放在**序列化边界**，账本的生产者不参与归因。
    """
    surface = _access_surface_of(record.driver)
    blocking = {v.value for v in BLOCK_VERDICTS}
    seen: dict[str, int] = {}
    stamped: list[dict[str, Any]] = []
    for event in record.gatekeeper_events:
        row = dict(event)
        verdict = str(row.get("verdict") or "")
        if verdict in blocking:
            seen[verdict] = seen.get(verdict, 0) + 1
            row.setdefault("blocked_surface", surface)
            row.setdefault("attempt", seen[verdict])
        stamped.append(row)
    return stamped


def _vocabulary_of(driver: str) -> str:
    """驱动实现名 → 元素词汇表。**一条轴**：``driver`` → 工具面 → 方言。

    认不出的驱动名**抛**（``toolcatalog`` 那两个函数都不回落）——拿默认
    面去渲染一份 camofox 存档，写出来的键名与真实调用对不上，而报告上
    看不出任何异常。
    """
    return toolcatalog.dialect_of(
        toolcatalog.surface_of_driver(driver)
    ).vocabulary


def _run_observations(record: "RunRecord") -> list[Observation | None]:
    """这条 run 里出现过的**全部**观察，按落盘顺序。

    ``steps[].observation`` 与 ``visits[].site_obs`` 是同一份观察的两条
    路径（前者是本步取到的、后者是单元摘要），这里只要**任意一份**能答出
    方言，所以不区分来源、也不去重——重复读一次同值的字符串不影响结论。
    """
    out: list[Observation | None] = [record.search_obs]
    for steps_log in (record.steps,):
        out += [s.observation for s in steps_log.steps]
    for ir in record.intermediates:
        out.append(ir.obs)
        out += [s.observation for s in ir.steps.steps]
    for v in record.visits:
        out += [s.observation for s in v.steps.steps]
        out.append(v.site_obs)
        out.append(v.player_obs)
    return out


def _vocabulary_of_run(record: "RunRecord") -> str:
    """整条 run 的元素词汇表。

    **首选观察自己记下的** :attr:`Observation.element_vocabulary`——那是
    驱动实现在采集时写下的原始事实（某个 ``<input>`` 值来自哪个词表，
    只有采到它的那一刻能确定）。按 ``driver`` 再折一遍是**第二处推断**，
    一旦驱动的 ``name`` 与存档里的 ``driver`` 字面值不一致，两处就会
    各说各话，而症状是键名悄悄换了一套。

    run 里一份观察都没有时才退回 ``driver`` 折算（例如搜索页立刻被反爬
    拦下）：那时也没有元素可投影，但 ``driver`` **认不出仍然抛**——与工具
    面同一条纪律，不猜。
    """
    for obs in _run_observations(record):
        if obs is not None:
            return obs.element_vocabulary
    return _vocabulary_of(record.driver)


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
        # 键名跟着**方言**走（P4）：obscura 面写 ``{tag,label}``、aria 面
        # （camoufox / camofox）写 ``{role,name}``。统一键名而不统一值域，
        # 等于把两个方言写成同一种
        # 数据——而读档人无从判断该按哪套词表解释它。投影只有
        # :func:`common.elements.element_json` 一处。
        #
        # 方言**取自这份观察自己**（``obs.element_vocabulary``），不由调用方
        # 传：传参就有传错的可能，而传错的症状是「键名悄悄换了一套」，
        # 报告上完全看不出来。观察是采集那一刻写下的原始事实，它就是权威。
        "interactive_elements": [
            element_json(e, obs.element_vocabulary,
                         name_limit=OBS_ELEMENT_NAME_LIMIT)
            for e in obs.interactive_elements[:OBS_ELEMENT_LIMIT]
        ],
        "links": [
            {"text": l.text[:80], "href": l.href} for l in obs.links[:OBS_LINK_LIMIT]
        ],
        # **跨面可比的元素账目**（设计方案 04 §4.9.6）——**新增的一列，
        # 不改 ``elements_total``**。
        #
        # ⚠️ ``None`` 要**原样落盘**：它是「这一批没开 ``--element-basis``」，
        # 与「页面上一个元素都没有」是**两件事**，混同会让读档的人把
        # 「没测」读成「没有」。缺键与 ``null`` 同义，都表示没测。
        "element_basis": _element_basis_json(obs.element_basis),
        # **共享口径没测到的原因**（2026-10-21）。⚠️ **它与 ``degraded``
        # 分开**：``degraded`` 是内容块的降级通道，下游只判它非空——
        # 把测量失败混进去，一把坏尺子就能**作废整批数据**。
        "element_basis_error": obs.element_basis_error,
    }


def _element_basis_json(basis: Mapping[str, Any] | None) -> dict[str, dict[str, int]] | None:
    """两本账落盘。**空的账不落**。

    ⚠️ **空账会被读成「测了，什么都没有」**——而真相是「没测到」。
    所以某一本账没测到时**缺键**，不落 ``{}``。

    ``shared`` 键**可能缺**：共享口径没测到时（尺子坏了）只剩 face 账。
    """
    if basis is None:
        return None
    out: dict[str, dict[str, int]] = {}
    for side in ("shared", "face"):
        block = basis.get(side)
        if not isinstance(block, Mapping) or not block:
            continue
        out[side] = {str(k): int(v) for k, v in block.items()}
    return out or None


#: 分类任务的路径分发表（方案 12 阶段 A1）：``discovery_path`` →
#: ``(保留类, 负分支, 判断点名)``。三条路径共用一份「目标类保留 /
#: 其余点名记负 / 未点名记 unresolved」的分发语义，只有目标类与
#: 分支名不同——参数化而不是复制三份（与旧 ①B/①C 的 branch 参数化
#: 同款纪律）。类别名出自
#: :data:`trajectory_pipeline.perception.questions.CLASSIFY_CATEGORIES`。
_CLASSIFY_DISPATCH: Mapping[str, tuple[str, str, str]] = {
    "direct": ("video_site", "not_play_site", "判断点 ①"),
    "aggregator": ("aggregator", "not_aggregator", "判断点 ①B"),
    "article": ("article_post", "not_article", "判断点 ①C"),
}

#: B/C 路径的中间页角色（A2b）。**两种模式同值**——``role`` 是 P1
#: ``intermediates[]`` 的字段，说的是「这个单元是哪种中间页」，与它是
#: 谁执行出来的无关。它与模式分开放，是为了让 :meth:`Orchestrator.
#: _intermediate_visit` 里那唯一的模式判断只管选访问器。
_INTERMEDIATE_ROLES: Mapping[str, str] = {
    "aggregator": aggregator_step.ROLE_AGGREGATOR,
    "article": aggregator_step.ROLE_ARTICLE,
}


def _candidates_view(search_obs: Observation, batch: list[Candidate]) -> Observation:
    """把一批候选投影成分类题的观察（方案 12 阶段 A1）。

    links = 候选：``text`` = 结果标题（分类判据之首——标题里写着作品名
    的播放页是最强 video_site 证据）、``href`` = **解包后**的目标 URL
    （URL 形态是第二判据；百度 ``/link?url=`` 密文包裹形态对模型不可
    判读，而编号回指 ``i`` 不依赖 href）。url / page_title / body_text
    承自搜索页观察，保留出处上下文；交互元素清空——分类题只读链接表，
    页面 chrome 是纯 token 开销。

    ⚠️ **视图不落 P1**：``record.search_obs`` 存的是引擎原序的搜索页
    观察（钉窗已删，不再变形）；这份投影只进感知层请求，视图的 href
    同时就是分发的匹配键（``Candidate.url``）——「模型看到的链接」与
    「分发命中的候选」由同一构造同源。
    """
    return dataclasses.replace(
        search_obs,
        interactive_elements=(),
        links=tuple(LinkItem(text=c.text, href=c.url) for c in batch),
    )


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

    **为什么落在分类任务之后**：分类的各类分发（保留 / 记负 /
    unresolved / 启发式保留）都要过这道核——分类判定的是「这个链接
    是什么类型的站」，而首页的否定是 path 形态的**结构事实**，不依赖
    模型是否作答（模型把首页分类成 video_site 也拦），所以位置在
    「分类处理完、开始遍历之前」——对全部分发路径都生效的唯一位置。
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
        agent_backend: Any = None,
    ) -> None:
        # 两个模式的准入都在**构造期**查（:mod:`.mode` 的纪律）：
        # 运行期才发现「没有后端」，那一批已经跑掉一半了——前面那些
        # 候选是 scripted 还是 agent 产的，存档上回答不了。
        # 模式的取值合法性由 RunConfig.__post_init__ 查（A3-1 起模式
        # 的产地只有 config 一个——Orchestrator 不再收 executor_mode
        # 关键字，两个产地等于两种真相）；这里只查「agent 必须有后端」，
        # 那是 RunConfig 看不见注入侧、只有构造本类时才知道的事。
        self._driver = driver
        self._perceptor = perceptor
        self._cfg = config or RunConfig()
        mode = self._cfg.executor_mode
        if mode == AGENT and agent_backend is None:
            # **不静默退回 scripted**。退回的那一批在存档上写的是
            # 'scripted'，于是「这批是 agent 批、失败了 40%」这个
            # 结论会被读成「这批 scripted 跑得好好的」——配置错误
            # 伪装成了一次成功的灰度，而它是所有失败里最难发现的
            # 一种：没有任何报错，报表还挺好看。
            raise ValueError(
                "executor_mode=agent 但没注入 agent 后端。"
                "要 agent 模式就先把 LLM 后端配齐（TRAJECTORY_LLM_*）；"
                "配不齐请显式用 scripted 模式跑。"
            )
        self._mode = mode
        self._agent = agent_backend
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
        log.info("[%s] 发现链启动：path=%s mode=%s query=%r engine=%s",
                 task_id, self._cfg.discovery_path, self._mode, query,
                 self._cfg.engine)
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
            executor_mode=self._mode,
            # **实际跑这条链的驱动实现**（2026-10-20 方案 15 §7 R1）。
            # 它是「工具面」的唯一来源：目标编码（``{tag,label}`` vs
            # ``{role,name}``）由驱动决定，写错了训练数据的声明就与
            # 真实调用对不上——而那在报告上看不出任何异常。
            driver=str(getattr(self._driver, "name", "") or ""),
            # 执行层指纹读数（2026-10-22 P6）。**只取驱动自己有的**——
            # obscura 没有指纹可读（远端 MCP），那是「没测到」，
            # 补一个空 dict 与「实测为空」混用会让读档人分不清。
            fingerprint=dict(getattr(self._driver, "fingerprint", {}) or {}),
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
            # Gatekeeper 统一账本（方案 12 阶段 G）：同一拦截事件的
            # 结构化记账，与 search_blocked / warnings **并存不替代**——
            # 那两处是冻结口径的现行字段，这里是 D-A3 的最小事件账本
            # （将来 agent 模式的每步拦截都记这里）。处置是 FailClosed
            # 的运行级特例：整条 run 收口，**不落分支**（拦截发生在
            # 取候选之前，没有 outcome 可记）。
            record.gatekeeper_events.append({
                "stage": "search",
                "verdict": intercept(record.search_obs).value,
                "reason": blocked,
                "disposition": FailClosed(
                    reason=f"搜索页被反爬拦截（{blocked}）"
                ).to_event(),
            })
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

        # ── 环节 ①.4.5：候选钉进窗口 —— 已删除（方案 12 阶段 A1）─────
        # 旧机制（04 方案的同源修复）：把候选按 rank 钉到 links 最前，
        # 并重放感知层排序校验「候选 ⊆ 窗口」。分类任务的候选视图由
        # 构造保证「模型看得到全部候选」（见下方 ①.5），重排观察与
        # 重放校验都不再需要——search_obs 保持引擎原序，存档的观察
        # 忠实于页面而不是「钉过候选」的变形。机制墓碑与消解理由
        # 详见 ``steps/search.py`` 尾注。

        # ── 环节 ①.5：分类任务（方案 12 阶段 A1）────────────────────
        # 「这个链接是什么类型的站」是语义判断，代码做不了（URL 里的
        # iqiyi.com 只说明它是爱奇艺，是不是本片的播放页要看了才知道）。
        # 取候选是代码层的**结构性**过滤（引擎自家、gov.cn、help 页），
        # 分类在它之后：一道题问出四分类（video_site / aggregator /
        # article_post / noise），按 discovery_path 取对应类分发
        # （direct→video_site / aggregator→aggregator / article→
        # article_post），取代旧 ①/①B/①C 三道 SELECT_* 分叉——三题
        # 保留在注册表：站内搜索结果页仍复用 ①（见 steps/site_search.py），
        # 金标重放与 A/B 对照也需要它们。
        #
        # **候选视图**：分类的输入就是候选列表（links=候选、text=结果
        # 标题、href=解包后目标 URL），按 PERCEPTION_LINK_WINDOW 分批送
        # 感知层，「模型看得到全部候选」由构造保证（钉窗机制的消解理由
        # 见上方 ①.4.5 墓碑）。
        #
        # ⚠️ **None 不中断**（fail-closed 管的是「结论」不是「采集」）：
        # None 批次的候选按启发式保留全遍历（与旧 ① None 同口径）。
        # W1 规则版对分类题只会返回 None，若据此中断，候选一个都不跑，
        # 负样本池永远空——同 IS_REACHABLE 的处理。
        # **过滤前**的候选数：record.candidates 在分类分发后就被覆盖，
        # 用它打消息会得到「过滤后无候选（原有 0 个）」这种自相矛盾的
        # 句子（实测踩过：明明原有 6 个却报 0 个）。
        n_candidates = len(record.candidates)
        record.candidates = self._classify_candidates(record, ledger)

        # ── 环节 ①.6：站点首页核（仅 direct）／中间页遍历（B/C）────
        if self._cfg.discovery_path == "direct":
            # 位置在分类之后而不是里面：分类的各类分发路径都要过这道
            # 核（见 _drop_site_homepages 的位置说明）。
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
            log.warning("[%s] 分类+过滤后无候选（原有 %d 个，来源 %s），"
                        "run 提前结束",
                        task_id, n_candidates, record.candidate_source)
            record.warnings.append(
                f"分类+过滤后无候选（原有 {n_candidates} 个，"
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
        vocabulary = _vocabulary_of_run(record)
        steps = steps_log.steps
        for index, step in enumerate(steps):
            try:
                payload = self._writer(
                    action=step.action.to_json(vocabulary),
                    user_prompt=record.user_prompt,
                    target_title=record.title,
                    # INC46：**不喂本步观察**。历史是 ``(动作, 观察)`` 对——
                    # 动作前动机只该依据此前看到的东西（见
                    # ``rationale.writer.render_history``）。
                    history=[
                        (s.action.to_json(vocabulary),
                         _obs_json(s.observation))
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
        vocabulary = _vocabulary_of_run(record)
        steps = steps_log.steps
        try:
            return reflect(
                user_prompt=record.user_prompt,
                target_title=record.title,
                history=[
                    (s.action.to_json(vocabulary),
                     _obs_json(s.observation))
                    for s in steps
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

    def _classify_candidates(
        self, record: RunRecord, ledger: RunLedger
    ) -> list[Candidate]:
        """环节 ①.5 主体：分批分类 + 按路径分发（方案 12 阶段 A1）。

        每批 ≤ :data:`PERCEPTION_LINK_WINDOW` 条候选构造候选视图
        （:func:`_candidates_view`）问 ``Q.CLASSIFY_SEARCH_LINKS``，
        四分类结果按 ``discovery_path`` 分发（保留类见
        :data:`_CLASSIFY_DISPATCH`）：

        - 目标类 → 保留进遍历队列（顺序保持引擎原序——重排等于用
          模型判断覆盖引擎的相关性排序，没有依据）；
        - 其余类（模型点名）→ 记路径负分支，evidence 带模型给的
          ``why``。zhihu/douban/weibo 自结构排除表移出后，在 direct
          批次被分类为 ``article_post`` 而记 ``not_play_site`` 的正是
          **有证据的真负样本**（见 ``steps/search.py`` 拆分说明）；
        - 未点名 → 记 ``unresolved``（**无证据不记负样本**，与旧 ①
          同一条纪律：漏分类说明模型没看到或没答，不是「这个站看不了」）；
        - fail-closed 批次（answer=None）→ 该批候选按启发式保留、
          **不记账**（与旧 ① None 同口径：全遍历，不中断采集）。

        旧 False 分支（整页「没有可看站点」→ 中断遍历 + 全候选记账）
        在新设计里是分发的**涌现结果**：目标类 kept=0 且逐条点名记负，
        遍历自然没有目标——同一份账面，不再需要三条路径各自的 False
        文案。落选必须记账这条纪律不变（负样本不是副产品）。
        """
        keep_class, branch, gate = _CLASSIFY_DISPATCH[self._cfg.discovery_path]
        task_id = record.task_id
        candidates = record.candidates
        batches = [
            candidates[i : i + PERCEPTION_LINK_WINDOW]
            for i in range(0, len(candidates), PERCEPTION_LINK_WINDOW)
        ]
        kept: list[Candidate] = []
        heuristic_kept: list[Candidate] = []
        dropped = 0
        undecided = 0
        classified_batches = 0
        for batch in batches:
            decision = self._perceptor.decide(
                Q.CLASSIFY_SEARCH_LINKS,
                _candidates_view(record.search_obs, batch),
            )
            if decision.answer is None:
                heuristic_kept.extend(batch)
                record.warnings.append(
                    f"{gate} 分类任务 fail-closed（{decision.evidence}）——"
                    f"该批 {len(batch)} 个候选仍按代码启发式全遍历，不中断采集"
                )
                continue
            classified_batches += 1
            # url → (类别, why) 点名表。匹配键 = 候选视图的 href
            # （= Candidate.url，见 _candidates_view——「模型看到的链接」
            # 与「分发命中的候选」同源，不靠 source_href 猜测）。
            # 同一 url 出现在两个类别时**先到先得**：类别互斥由题面
            # 约束（「归入且只归入一类」），违反时取先出现的，evidence
            # 里两类的 why 都不会丢——行本身都在 payload 里。
            named: dict[str, tuple[str, str]] = {}
            for cls in questions.CLASSIFY_CATEGORIES:
                for row in decision.payload.get(cls) or []:
                    if not isinstance(row, dict):
                        continue
                    url = str(row.get("url") or "").strip()
                    if url and url not in named:
                        named[url] = (cls, str(row.get("why") or ""))
            for candidate in batch:
                hit = named.get(candidate.url)
                if hit is None:
                    undecided += 1
                    ledger.record(
                        candidate.url,
                        "unresolved",
                        f"{gate} 分类任务未点名该候选——**无证据不记负样本**，"
                        f"转人工复核（漏分类可能是模型漏答，不是"
                        f"「这个站看不了」）。整体判据：{decision.evidence}",
                        decision=decision,
                        site_url=candidate.url,
                    )
                elif hit[0] == keep_class:
                    kept.append(candidate)
                else:
                    dropped += 1
                    why = hit[1].strip()
                    ledger.record(
                        candidate.url,
                        branch,
                        f"{gate} 分类为 {hit[0]}（非本路径目标类 "
                        f"{keep_class}）。{why}"
                        if why
                        else f"{gate} 分类为 {hit[0]}（非本路径目标类 "
                        f"{keep_class}，模型未给出理由）",
                        decision=decision,
                        site_url=candidate.url,
                    )
        if classified_batches:
            record.candidate_source = "perceptor"
            record.warnings.append(
                f"{gate}（分类任务）自 {len(candidates)} 个候选保留 "
                f"{len(kept)} 个（目标类 {keep_class}），{dropped} 个记 {branch}"
                + (
                    f"，{undecided} 个模型未点名，记 unresolved"
                    f"（**不入负样本池**）"
                    if undecided
                    else ""
                )
                + (
                    f"；{len(heuristic_kept)} 个因 fail-closed 批次"
                    f"按启发式保留"
                    if heuristic_kept
                    else ""
                )
            )
            log.info(
                "[%s] %s 分类完成：保留 %d/%d（%d 批中 %d 批 fail-closed）",
                task_id, gate, len(kept), len(candidates), len(batches),
                len(batches) - classified_batches,
            )
        else:
            record.candidate_source = "heuristic"
            log.info(
                "[%s] %s 分类任务全程 fail-closed，%d 个候选按启发式全遍历",
                task_id, gate, len(candidates),
            )
        # 遍历顺序保持引擎原序：kept / heuristic_kept 按批分段收集，
        # 多批混合时直接拼接会打乱批间顺序，按原序回放一次。
        selected_urls = {c.url for c in kept} | {c.url for c in heuristic_kept}
        return [c for c in candidates if c.url in selected_urls]

    async def _visit_guarded(
        self,
        candidate: Candidate,
        ledger: RunLedger,
        record: RunRecord,
        *,
        allow_site_search: bool = False,
        title: str = "",
    ) -> visit_step.VisitResult:
        """单站点遍历的超时与异常兜底。**两种模式的兜底是同一套**。

        单个站点超时不该拖垮整批——但**必须记账**，否则这次运行会因为
        「超时」而在样本分布里消失，变成一条看不见的损失。

        ⚠️ 分派在**这里**而不是塞进 ``visit_site`` 内部：控制流里没有
        ``if 模式`` 的分支，模式只在这一处起作用。若分派下沉到
        ``visit_site`` 里，「scripted 基线与黄金对齐」这件事就变成了
        一个需要额外证明的命题（它可能被 agent 路径改到）。

        ⚠️ agent 模式**共用** ``per_site_timeout_s``：一个候选里最多
        12 次 llm_call，90s 的默认值会被走得满的候选撞到。这是已知
        的口径（超时即 unresolved，与 scripted 同形），A3 若要把
        agent 的超时单列，改的是这里一处。
        """
        result = visit_step.VisitResult(candidate=candidate)
        try:
            if self._mode == AGENT:
                coro = agent_visit_step.agent_visit(
                    self._driver,
                    self._perceptor,
                    ledger,
                    candidate,
                    self._agent,
                    max_chars=self._cfg.max_chars,
                    title=title,
                    gatekeeper_events=record.gatekeeper_events,
                    # §3.6 账本（A2a-4）：与事件账本同款透传——同一个
                    # list 对象，循环往里 append，这里在收口时统一盖 ts。
                    llm_calls=record.llm_calls,
                )
                visited = await asyncio.wait_for(
                    coro, timeout=self._cfg.per_site_timeout_s
                )
                _stamp_llm_calls(record.llm_calls)
                return visited
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
        """（中间页访问器, 角色标签）——**模式在中间页链上只出现这一次**。

        两条链的被调函数签名不同：agent 链**不收 ``perceptor``**（三道
        判断题一道都不跑），scripted 链不收 ``backend``。各自配一个组装
        方法，:meth:`_intermediate_guarded` 就只管超时与异常兜底、不碰
        模式——把签名差异暴露到那一层，等于让「超时兜底」这个与模式
        无关的职责背上模式的包袱。

        角色只由 ``discovery_path`` 决定（两种模式同值，``role`` 是 P1
        字段而不是模式的函数），所以它与模式无关地单独算。
        """
        role = _INTERMEDIATE_ROLES[self._cfg.discovery_path]
        if self._mode == AGENT:
            return self._intermediate_visit_agent, role
        return self._intermediate_visit_scripted, role

    async def _intermediate_visit_agent(
        self, candidate: Candidate, ledger: RunLedger, record: RunRecord
    ) -> IntermediateResult:
        """agent 链的中间页访问的**参数组装**（模式词汇的容器之一）。"""
        fn = (
            agent_intermediate.visit_aggregator_agent
            if self._cfg.discovery_path == "aggregator"
            else agent_intermediate.visit_article_agent
        )
        result = await fn(
            self._driver,
            ledger,
            candidate,
            self._agent,
            max_chars=self._cfg.max_chars,
            max_links=self._cfg.max_referral_links,
            engine=self._cfg.engine,
            stats=record.candidate_filter,
            title=record.title,
            gatekeeper_events=record.gatekeeper_events,
            # §3.6 账本（A2a-4）：与 ``_visit_guarded`` 同款透传 + 收口盖戳。
            llm_calls=record.llm_calls,
        )
        _stamp_llm_calls(record.llm_calls)
        return result

    async def _intermediate_visit_scripted(
        self, candidate: Candidate, ledger: RunLedger, record: RunRecord
    ) -> IntermediateResult:
        """scripted 链的中间页访问的参数组装。**外部行为与 A2b 前一致**。"""
        fn = (
            aggregator_step.visit_aggregator
            if self._cfg.discovery_path == "aggregator"
            else article_step.visit_article
        )
        return await fn(
            self._driver,
            self._perceptor,
            ledger,
            candidate,
            max_chars=self._cfg.max_chars,
            max_links=self._cfg.max_referral_links,
            engine=self._cfg.engine,
            stats=record.candidate_filter,
        )

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

        本方法**不碰模式**：两个访问器在 :meth:`_intermediate_visit` 里
        就选好了，这里拿到的已经是一个签名统一的
        ``(candidate, ledger, record) -> coro``。
        """
        visit, role = self._intermediate_visit()
        try:
            return await asyncio.wait_for(
                visit(candidate, ledger, record),
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
