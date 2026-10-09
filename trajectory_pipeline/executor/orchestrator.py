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
import time
from dataclasses import dataclass, field
from typing import Any, Mapping

from trajectory_pipeline.executor import actions
from trajectory_pipeline.executor.actions import StepLog
from trajectory_pipeline.executor.branches import RunLedger, validate_observation_for_decision
from trajectory_pipeline.executor.dom import DEFAULT_MAX_CHARS, detect_block
from trajectory_pipeline.executor.steps import search as search_step
from trajectory_pipeline.executor.steps import visit as visit_step
from trajectory_pipeline.executor.steps.search import Candidate
from trajectory_pipeline.perception.base import Decision, Observation, Perceptor, Q

#: 存档里保留的交互元素 / 链接条数上限。**提取成常量不是为了省字**，
#: 是因为「落盘裁到几条」这件事现在有了判据意义——
#: D-2 的评分器靠 ``len(观察里存的)`` 与 ``elements_total`` 的差判断
#: 「目标是不是可能被裁掉了」，而这个差**只有在两处截断对齐时才有意义**。
#: 哪天这里改成 100 而别处还按 80 判断，D-2 会静默地把裁剪当成不存在。
OBS_ELEMENT_LIMIT = 80
OBS_LINK_LIMIT = 50


@dataclass
class RunConfig:
    """一次运行的参数。

    默认值全部**保守**：宁可少跑几个站，也不要产出看似完整实则证据不足的记录。
    """

    engine: str = search_step.DEFAULT_ENGINE
    max_candidates: int = 20
    max_chars: int | None = DEFAULT_MAX_CHARS
    stop_after_success: int = 0        # 0 = 跑满候选；>0 = 成功 N 个即停
    per_site_timeout_s: float = 90.0


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
    candidates: list[Candidate] = field(default_factory=list)
    visits: list[visit_step.VisitResult] = field(default_factory=list)
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
                }
                for v in self.visits
            ],
            "ledger": self.ledger.summary() if self.ledger else None,
            "outcomes": [o.to_json() for o in (self.ledger.outcomes if self.ledger else [])],
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
        "links": [{"text": l.text[:80], "href": l.href}
                  for l in obs.links[:OBS_LINK_LIMIT]],
    }


def _apply_selection(
    record: RunRecord,
    selection: Decision,
    ledger: RunLedger,
) -> list[Candidate]:
    """按判断点 ① 的结论过滤候选，**并把落选的记成 ``not_play_site``**。

    落选必须记账，这是 CLAUDE.md「**每条分支都必须有对应样本入库**」
    的直接要求：负样本不是副产品。反过来「只记选中的」会让
    ``not_play_site`` 永远是 0，而报表上分部数字齐全，没人看得出这支空了。

    **匹配按 :attr:`Candidate.source_href`**（观察里的原始 href），而遍历用
    ``Candidate.url``（解包后）。感知层读的是观察，所以它回传的是前者。
    两个字段对不上时按后者兜底——百度 ``/link?url=`` 包裹的链接一旦
    匹配不上，表现是「整个站点集丢失」，且看不出是匹配问题。

    顺序保持**引擎原序**（``rank``），不按 selected 数组的顺序重排——
    重排等于用模型的判断覆盖引擎的相关性排序，那是没有依据的。
    """
    selected_rows = [
        r for r in (selection.payload.get("selected") or [])
        if isinstance(r, dict)
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
    for candidate in record.candidates:
        key = candidate.source_href or candidate.url
        if candidate.url in reasons or key in reasons:
            kept.append(candidate)
            continue
        dropped += 1
        why = (rejected.get(key) or rejected.get(candidate.url) or "").strip()
        ledger.record(
            candidate.url,
            "not_play_site",
            f"判断点 ① 未选中该站点。{why}" if why
            else "判断点 ① 未选中该站点（模型未给出排除理由）",
            decision=selection, site_url=candidate.url,
        )
    record.warnings.append(
        f"判断点 ① 选出 {len(kept)}/{len(record.candidates)} 个候选，"
        f"{dropped} 个记 not_play_site"
    )
    return kept


class Orchestrator:
    """主控制流。一个实例跑一个 task。"""

    def __init__(
        self,
        driver: Any,
        perceptor: Perceptor,
        config: RunConfig | None = None,
    ) -> None:
        self._driver = driver
        self._perceptor = perceptor
        self._cfg = config or RunConfig()

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
        query = search_query or search_step.build_query(title, persona=persona)
        url = search_step.search_url(query, self._cfg.engine)
        ledger = RunLedger(task_id=task_id)
        record = RunRecord(
            task_id=task_id, title=title, query=query, search_url=url,
            user_prompt=user_prompt,
            ledger=ledger, perceptor=getattr(self._perceptor, "name", "?"),
            provenance=dict(provenance or {}),
            # 运行参数进存档：覆盖完整性（B-2）与异常处置（B-4）
            # 两项断言的分母/判据都在这里，见 RunRecord.run_config。
            run_config=dataclasses.asdict(self._cfg),
        )

        # ── 环节 0：搜索页 ──────────────────────────────────────────
        step_search = record.steps.act(actions.goto(url))
        try:
            await self._driver.goto(url)
            record.search_obs = await self._driver.observe(max_chars=self._cfg.max_chars)
        except Exception as exc:
            # 搜索页拿不到时整条 run 提前返回，动作流里必须留下这一步：
            # 「搜都没搜成」与「搜了但没素材」在报表上是两回事，
            # 而动作流是这个区分在样本层的落点。
            record.steps.settle(step_search, error=f"{type(exc).__name__}: {exc}")
            record.warnings.append(f"搜索页采集失败: {type(exc).__name__}: {exc}")
            record.elapsed_ms = int((time.monotonic() - started) * 1000)
            return record
        record.steps.settle(step_search, record.search_obs)

        # ── 环节 0.5：反爬拦截 ─────────────────────────────────────
        # 必须在取候选**之前**判：验证码页的表现是"正文短 + 零链接"，
        # 混进下面的"未提取到候选"里，两者处置完全不同（加间隔/换引擎
        # vs 换素材），而混在一起的代价是静默的——报表上只看得见
        # "这批搜索页质量差"，没人会想到去查反爬。
        blocked = detect_block(
            record.search_obs.url, record.search_obs.page_title,
            record.search_obs.body_text,
        )
        if blocked:
            record.search_blocked = blocked
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
            record.search_obs = dataclasses.replace(
                record.search_obs,
                links=tuple(
                    dataclasses.replace(link, text=titles.get(link.href) or link.text)
                    for link in record.search_obs.links
                ),
            )
        else:
            record.warnings.append(
                f"未取到 {self._cfg.engine} 的结果标题（{title_why}）"
                f"——判断点 ① 将因输入不含片名而 fail-closed"
            )
        record.candidates = search_step.extract_candidates(
            record.search_obs, engine=self._cfg.engine, limit=self._cfg.max_candidates,
            stats=record.candidate_filter, titles=titles,
        )
        if not record.candidates:
            record.warnings.append(
                f"搜索页未提取到候选（links={len(record.search_obs.links)}, "
                f"degraded={list(record.search_obs.degraded)}，"
                f"过滤掉 {record.candidate_filter}）"
            )
            record.elapsed_ms = int((time.monotonic() - started) * 1000)
            return record

        # ── 环节 ①.5：判断点 ①（语义层）──────────────────────────
        # 「这个链接是不是能看《功夫》的站」是语义判断，代码做不了
        # （URL 里的 iqiyi.com 只说明它是爱奇艺）。取候选是代码层的
        # **结构性**过滤（搜索引擎自家、gov.cn、help 页），判断点 ①
        # 在它之后：先剔掉根本不是内容站的，再问「哪些是这个作品的播放站」。
        #
        # ⚠️ **None 时不中断**（fail-closed 管的是「结论」不是「采集」）。
        # W1 的规则版对 ① 只会返回 None，若据此中断，候选一个都不跑，
        # 负样本池永远空。同 IS_REACHABLE 的处理。
        selection = self._perceptor.decide(Q.SELECT_PLAY_SITES, record.search_obs)
        if selection.answer is True:
            record.candidates = _apply_selection(record, selection, ledger)
            record.candidate_source = "perceptor"
        elif selection.answer is False:
            # False 是货真价实的结论（「这页没有该片的可看站点」），
            # 与 IS_REACHABLE 的处理一致：**中断遍历**，后续判断无从谈起。
            # 但每个候选仍要记 not_play_site——中断若不记账，这批结论
            # 就没有任何样本，而报表上只会看到「这批没跑出东西」。
            #
            # 判错的代价由人工复核承担：evidence 带着模型给的理由，
            # 重跑一次即可，不是不可逆。
            record.warnings.append(
                f"判断点 ① 判 False：搜索结果里没有《{record.title}》的可看站点 "
                f"（{selection.evidence}）；{len(record.candidates)} 个候选全部"
                f"记 not_play_site，不访问"
            )
            for candidate in record.candidates:
                ledger.record(candidate.url, "not_play_site",
                              f"判断点 ① 判 False：{selection.evidence}",
                              decision=selection)
            record.candidates = []
            record.candidate_source = "perceptor"
        else:
            record.candidate_source = "heuristic"
            record.warnings.append(
                f"判断点 ① 未取得结论（{selection.evidence}）——"
                f"候选仍按代码启发式全遍历，不中断采集"
            )

        if not record.candidates:
            record.warnings.append(
                f"判断点 ① 过滤后无候选（原有 {len(record.candidates)} 个）"
            )
            record.elapsed_ms = int((time.monotonic() - started) * 1000)
            return record

        # ── 环节 ②：逐站点遍历 ────────────────────────────────────
        successes = 0
        for candidate in record.candidates:
            if self._cfg.stop_after_success and successes >= self._cfg.stop_after_success:
                break
            result = await self._visit_guarded(candidate, ledger, record)
            record.visits.append(result)
            if result.success:
                successes += 1

        record.ledger = ledger
        record.elapsed_ms = int((time.monotonic() - started) * 1000)
        return record

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
            return {}, (f"{search_step.TITLE_FIELD}/{search_step.URL_FIELD} "
                        f"长度不等或为空（抽到字段 {sorted(extracted)}）")
        return titles, ""

    async def _visit_guarded(
        self,
        candidate: Candidate,
        ledger: RunLedger,
        record: RunRecord,
    ) -> visit_step.VisitResult:
        """单站点遍历的超时与异常兜底。

        单个站点超时不该拖垮整批——但**必须记账**，否则这次运行会因为
        「超时」而在样本分布里消失，变成一条看不见的损失。
        """
        result = visit_step.VisitResult(candidate=candidate)
        try:
            return await asyncio.wait_for(
                visit_step.visit_site(
                    self._driver, self._perceptor, ledger, candidate,
                    max_chars=self._cfg.max_chars,
                ),
                timeout=self._cfg.per_site_timeout_s,
            )
        except asyncio.TimeoutError:
            ledger.record(candidate.url, "unresolved",
                          f"站点遍历超时（>{self._cfg.per_site_timeout_s}s）",
                          site_url=candidate.url)
            result.note("超时")
            record.warnings.append(f"{candidate.url} 超时")
            return result
        except Exception as exc:
            ledger.record(candidate.url, "unresolved",
                          f"站点遍历异常: {type(exc).__name__}: {exc}",
                          site_url=candidate.url)
            result.note(f"异常: {exc}")
            record.warnings.append(f"{candidate.url} 异常: {exc}")
            return result