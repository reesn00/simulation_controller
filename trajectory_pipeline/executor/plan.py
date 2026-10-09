"""执行计划（taskgen ``gen --out`` 的产物）的**消费端**。

交接面是**文件格式**，不是 import
--------------------------------
本模块**不 import** :mod:`trajectory_pipeline.taskgen`。理由与新树整体
纪律同源，但这里要说的更具体一点：taskgen 与 executor 是流水线的两段，
它们的耦合点应该是**一份可读的 JSON**，而不是类对象。

这么定还有一条现实理由：plan 是**要长期存着的**。切片分析、失败归因、
半年后回看「这批数据是哪个人设配哪部片」，读的都是那个 JSON 文件——
如果消费端 import 生成端，那么「能读这份文件」就等于「生成端还在原地」，
而生成端一改字段，旧计划全废。**按格式读，格式就冻住了。**

三类跳过，每一类都记账
--------------------
读进来的计划里有一部分是**跑不了的**。它们不报错，但**必须计数并打印**
——静默跳过的后果是"跑了 20 条"而实际只跑了 12 条，读的人不知道少的那
8 条是什么：

============================  ======  ==========================================
跳过原因                      默认    为什么
============================  ======  ==========================================
``mode`` 非 ``single_title``  跳过    W1 成功判据是"找到一个可播放站点页"，
                                      与聚合/片名未知类任务不匹配（见
                                      taskgen.skeleton 的模块 docstring）
``search_query`` 为空          跳过    控制流会退化成**搜空串**，产出的是
                                      搜索引擎首页的候选——那不是"这个任务失败"，
                                      是"根本没在跑这个任务"
``rewritten=False``            跳过    归一退回骨架原文 = **persona 没落地**。
                                      跑出来的结果在切片表里长得跟
                                      "这个 persona 组合表现差"一模一样，
                                      实际是"这个人设根本没生效"。
                                      要跑必须显式 ``--include-unrewritten``
============================  ======  ==========================================

最后一条是接线时才暴露的：sampler 产出计划时**故意不丢**未归一的实例
（骨架原文本身就是合法表述，丢掉等于白采一次画像），所以它们会出现在
plan 里。是否跑由执行端决定，但**决定必须在输出里留痕**。

fail-closed
-----------
计划文件缺失、结构不符、条目缺 ``task_id`` —— 一律 :class:`PlanError`
**显式抛**。静默返回空列表的表现是"跑完了，零条数据"，而零条数据
和"这批全被跳过了"在终端上长得一模一样。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Final, Mapping, Sequence

#: W1 能跑的检索模式。与 :data:`taskgen.skeleton.RetrievalMode` 同值，
#: 但**故意重声明而不 import**——理由见模块 docstring「交接面是文件格式」。
W1_RUNNABLE_MODE: Final = "single_title"


class PlanError(ValueError):
    """计划文件不可用。**显式抛**，不静默返回空。"""


@dataclass(frozen=True, slots=True)
class PlannedTask:
    """计划里的一条可执行任务。

    刻意只保留执行端真正用得到的字段。判分标准、constraints、scenario
    这些留在 plan 的 ``provenance`` 里随存档带走，但**执行端一个都不读**——
    W1 的控制流不需要知道判分标准长什么样，它只负责把观察交给 Perceptor。
    """

    index: int
    task_id: str
    persona_id: str
    title: str | None
    search_query: str
    prompt_text: str
    retrieval_mode: str
    rewritten: bool
    provenance: Mapping[str, Any] = field(default_factory=dict)

    def to_json(self) -> dict[str, Any]:
        return {
            "index": self.index,
            "task_id": self.task_id,
            "persona_id": self.persona_id,
            "title": self.title,
            "search_query": self.search_query,
            "prompt_text": self.prompt_text,
            "retrieval_mode": self.retrieval_mode,
            "rewritten": self.rewritten,
            "provenance": dict(self.provenance),
        }


@dataclass(frozen=True, slots=True)
class PlanSkip:
    """一条被跳过的记录。**理由必须留痕**，否则"少跑的那些"就消失了。"""

    index: int
    task_id: str
    reason: str
    detail: str = ""

    def to_json(self) -> dict[str, Any]:
        return {
            "index": self.index,
            "task_id": self.task_id,
            "reason": self.reason,
            "detail": self.detail,
        }


@dataclass(frozen=True, slots=True)
class LoadedPlan:
    """一份计划的加载结果。``tasks`` 可直接喂给 Orchestrator。"""

    source: str
    tasks: tuple[PlannedTask, ...]
    skips: tuple[PlanSkip, ...]
    report: Mapping[str, Any] = field(default_factory=dict)

    @property
    def total_in_file(self) -> int:
        """文件里原本有多少条。**不等于** ``len(tasks)``。"""
        return len(self.tasks) + len(self.skips)

    def skip_summary(self) -> dict[str, int]:
        out: dict[str, int] = {}
        for s in self.skips:
            out[s.reason] = out.get(s.reason, 0) + 1
        return out

    def to_json(self) -> dict[str, Any]:
        return {
            "source": self.source,
            "total_in_file": self.total_in_file,
            "runnable": len(self.tasks),
            "skipped": len(self.skips),
            "skip_summary": self.skip_summary(),
            "report": dict(self.report),
            "tasks": [t.to_json() for t in self.tasks],
            "skips": [s.to_json() for s in self.skips],
        }


def _require_str(row: Mapping[str, Any], key: str, index: int) -> str:
    value = row.get(key)
    text = str(value or "").strip()
    if not text:
        raise PlanError(f"计划第 {index} 条缺 {key}: {sorted(row)[:8]}")
    return text


def parse_plan(data: object, *, source: str = "<memory>",
               include_unrewritten: bool = False) -> LoadedPlan:
    """计划 dict → :class:`LoadedPlan`。**不碰文件**，便于离线单测。"""
    if not isinstance(data, Mapping):
        raise PlanError(f"{source} 不是 JSON 对象（实际 {type(data).__name__}）")
    rows = data.get("instances")
    if not isinstance(rows, Sequence) or isinstance(rows, (str, bytes)):
        raise PlanError(
            f"{source} 缺 instances 列表；这不是 gen --out 的产物"
            f"（顶层键：{sorted(data)[:8]}）"
        )

    tasks: list[PlannedTask] = []
    skips: list[PlanSkip] = []
    for index, row in enumerate(rows):
        if not isinstance(row, Mapping):
            raise PlanError(f"计划第 {index} 条不是对象（{type(row).__name__}）")
        task_id = _require_str(row, "task_id", index)
        persona_id = _require_str(row, "persona_id", index)
        mode = _require_str(row, "retrieval_mode", index)
        query = str(row.get("search_query") or "").strip()
        rewritten = bool(row.get("rewritten", True))

        if mode != W1_RUNNABLE_MODE:
            skips.append(PlanSkip(
                index, task_id, "mode_not_runnable",
                f"检索模式 {mode!r}，W1 判据是单站可播放页（--all-modes 产的"
                f"计划里会出现这类；拿单站判据评聚合任务会把不完整输出记成成功）",
            ))
            continue
        if not query:
            skips.append(PlanSkip(
                index, task_id, "empty_query",
                "检索式为空；跑下去会**搜空串**并拿到搜索引擎首页的候选",
            ))
            continue
        if not rewritten and not include_unrewritten:
            skips.append(PlanSkip(
                index, task_id, "not_rewritten",
                "归一退回骨架原文（persona 未生效）；跑出来对 persona 切片无价值。"
                "确需跑请加 --include-unrewritten",
            ))
            continue

        tasks.append(PlannedTask(
            index=index,
            task_id=task_id,
            persona_id=persona_id,
            title=str(row.get("title") or "").strip() or None,
            search_query=query,
            prompt_text=str(row.get("prompt_text") or ""),
            retrieval_mode=mode,
            rewritten=rewritten,
            provenance=dict(row.get("provenance") or {}),
        ))

    report = data.get("report")
    return LoadedPlan(
        source=source,
        tasks=tuple(tasks),
        skips=tuple(skips),
        report=dict(report) if isinstance(report, Mapping) else {},
    )


def load_plan(path: Path | str, *, include_unrewritten: bool = False) -> LoadedPlan:
    """读 ``gen --out`` 的产物。"""
    p = Path(path)
    if not p.exists():
        raise PlanError(
            f"找不到执行计划 {p}；先跑：python -m trajectory_pipeline.executor.cli "
            f"gen -n <条数> --out {p}"
        )
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except ValueError as exc:
        raise PlanError(f"读 {p} 失败（不是合法 JSON）: {exc}") from exc
    return parse_plan(data, source=str(p), include_unrewritten=include_unrewritten)