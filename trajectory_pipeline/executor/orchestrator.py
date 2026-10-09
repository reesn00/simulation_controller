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
import time
from dataclasses import dataclass, field
from typing import Any, Mapping

from trajectory_pipeline.executor.branches import RunLedger, validate_observation_for_decision
from trajectory_pipeline.executor.dom import DEFAULT_MAX_CHARS, detect_block
from trajectory_pipeline.executor.steps import search as search_step
from trajectory_pipeline.executor.steps import visit as visit_step
from trajectory_pipeline.executor.steps.search import Candidate
from trajectory_pipeline.perception.base import Observation, Perceptor


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

    @property
    def succeeded(self) -> bool:
        return any(v.success for v in self.visits)

    def to_json(self) -> dict[str, Any]:
        """序列化。刻意只保留**可审计**的字段，不留页面句柄。"""
        return {
            "task_id": self.task_id,
            "title": self.title,
            "query": self.query,
            "search_url": self.search_url,
            "perceptor": self.perceptor,
            "candidate_source": self.candidate_source,
            "search_blocked": self.search_blocked,
            "candidate_filter": dict(self.candidate_filter),
            "provenance": dict(self.provenance),
            "elapsed_ms": self.elapsed_ms,
            "warnings": list(self.warnings),
            "search_observation": _obs_json(self.search_obs),
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
        "interactive_elements": [
            {"ref": e.ref, "tag": e.tag, "label": e.label[:120]}
            for e in obs.interactive_elements[:80]
        ],
        "links": [{"text": l.text[:80], "href": l.href} for l in obs.links[:50]],
    }


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
        provenance: Mapping[str, Any] | None = None,
    ) -> RunRecord:
        """跑完一个 task 的全部环节。**不抛异常**——异常折算进 warnings。

        ``search_query`` 由模块 1（:mod:`trajectory_pipeline.taskgen`）产出时
        优先使用；不给才退回本层的启发式拼接。两条路径都留着是因为
        ``search_query`` 有两种来源，而它们**不该被混为一谈**：
        taskgen 给的是「按 persona 的指代方式渲染出的用户表述所对应的检索」，
        启发式给的是「按片名硬拼」。前者带 provenance，后者不带。

        ``provenance`` 原样进存档（见 :class:`RunRecord` 的说明）。
        **本方法不读它的内容**——它是切片轴，不是控制流输入。
        这一点必须成立：一旦执行端开始"根据 provenance 决定跑不跑"，
        采样与执行就耦合了，而那样的批次无法归因（分不清是 persona 的
        问题还是采样规则的问题）。要跳过的条目在
        :mod:`trajectory_pipeline.executor.plan` 里跳，不在这里跳。
        """
        started = time.monotonic()
        query = search_query or search_step.build_query(title, persona=persona)
        url = search_step.search_url(query, self._cfg.engine)
        ledger = RunLedger(task_id=task_id)
        record = RunRecord(
            task_id=task_id, title=title, query=query, search_url=url,
            ledger=ledger, perceptor=getattr(self._perceptor, "name", "?"),
            provenance=dict(provenance or {}),
        )

        # ── 环节 0：搜索页 ──────────────────────────────────────────
        try:
            await self._driver.goto(url)
            record.search_obs = await self._driver.observe(max_chars=self._cfg.max_chars)
        except Exception as exc:
            record.warnings.append(f"搜索页采集失败: {type(exc).__name__}: {exc}")
            record.elapsed_ms = int((time.monotonic() - started) * 1000)
            return record

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
        record.candidates = search_step.extract_candidates(
            record.search_obs, engine=self._cfg.engine, limit=self._cfg.max_candidates,
            stats=record.candidate_filter,
        )
        if not record.candidates:
            record.warnings.append(
                f"搜索页未提取到候选（links={len(record.search_obs.links)}, "
                f"degraded={list(record.search_obs.degraded)}，"
                f"过滤掉 {record.candidate_filter}）"
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
                          f"站点遍历超时（>{self._cfg.per_site_timeout_s}s）")
            result.note("超时")
            record.warnings.append(f"{candidate.url} 超时")
            return result
        except Exception as exc:
            ledger.record(candidate.url, "unresolved",
                          f"站点遍历异常: {type(exc).__name__}: {exc}")
            result.note(f"异常: {exc}")
            record.warnings.append(f"{candidate.url} 异常: {exc}")
            return result