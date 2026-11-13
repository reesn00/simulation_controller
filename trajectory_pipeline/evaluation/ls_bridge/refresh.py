"""ls_bridge.refresh: 把 LS 上**已存在**的轨迹卡就地刷新到当前产物。

为什么需要它（台账 §H-34）：``push_index`` 按 ``sample_id`` 去重
（「**宁可不推也不推重复**」）⇒ T 视图 ``--overwrite`` 重建之后，
那批旧卡的 ``data`` **永远停在重建前的内容**。
⚠️ 实测（2026-11-11）：76/76 条有差异（``messages`` / ``openai`` / ``audit`` /
``scorecard`` 全变 —— 近几个 commit 改了渲染）⇒ 「重推」这条路**够不着**它们，
只能**就地刷**。

手段（owner 2026-11-04 已裁过的「就地重推」，台账 H-27）：
``PATCH /api/tasks/{id}`` + ``PATCH /api/predictions/{id}``
⇒ **不删卡、不新建卡** ⇒ task id 与标注归属不变。

⚠️ **三条纪律**（都写死在代码里，不是文档里的建议）：

1. **绝不 PATCH ``label_config``** —— 那会**覆盖标注员在 LS 端的改动**
   （模块 docstring 的纪律）⇒ 刷内容与刷表单配置是**两件事**，这里只做前者。
2. **默认只刷没有人工标注的** —— ``PATCH data`` 会不会影响 ``annotation``
   **没验过** ⇒ 不猜：有人工标注的**报出来并跳过**，要刷得显式
   ``--include-annotated``。
3. **保留旧卡的 ``uploaded_at``** —— 它是「这条卡**什么时候**上的 LS」的事实；
   刷新只改内容，不改这个时间戳（否则「上传时间」会变成「刷新时间」）。
   ⚠️ 比对时也**不比它** —— 它每次 push 都会变，拿它比会把「内容没变」误报成「有差异」。

⚠️ **本模块不动 ``push_index``**：刷新改的是**内容**不是**条目**
⇒ 本地台账不新增行（它记的是「哪些 sample 推过」，不是「内容是什么」）。
"""

from __future__ import annotations

import json
import logging
import pathlib
from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping

from trajectory_pipeline.evaluation.ls_bridge.exporter import (
    export_trajectory_one,
    read_trajectory_records,
)
from trajectory_pipeline.judgement_causes import llm_unanswered

log = logging.getLogger(__name__)

#: 比对时**跳过**的键 —— 每次 push 都会变，拿它比会把「内容没变」误报成差异。
VOLATILE_KEYS = frozenset({"uploaded_at"})


def _norm(value: object) -> str:
    """稳定序列化（键排序）—— 比「看起来一样」用。"""
    return json.dumps(value, sort_keys=True, ensure_ascii=False, default=str)


def diff_fields(
    old_data: Mapping[str, Any], new_data: Mapping[str, Any]
) -> tuple[str, ...]:
    """两份 ``task.data`` 的**差异字段名**（跳过 :data:`VOLATILE_KEYS`）。

    ⚠️ 比的是**全量**（不是挑几个字段看）—— 挑字段比会漏：
    「差异在``messages`` 还是``audit``」是**诊断信息**，
    「有没有差异」才是**判定**。
    """
    keys = set(old_data) | set(new_data)
    out: list[str] = []
    for key in sorted(keys - VOLATILE_KEYS):
        if _norm(old_data.get(key)) != _norm(new_data.get(key)):
            out.append(key)
    return tuple(out)


@dataclass(frozen=True)
class RefreshItem:
    """一条待刷的卡。"""

    sample_id: str
    ls_task_id: int
    diff_fields: tuple[str, ...]
    new_data: dict[str, Any]
    #: 旧卡的 ``uploaded_at``（刷进新 data 时保留）。
    uploaded_at: str
    #: 预标注的 id（LS 上没有这条预标注时为 ``None``）。
    prediction_id: int | None = None
    new_prediction: dict[str, Any] | None = None


@dataclass(frozen=True)
class RefreshPlan:
    """刷新计划：待刷 / 无差异 / LS 上没有 / 有标注被跳过 / 失败。"""

    items: tuple[RefreshItem, ...] = ()
    unchanged: tuple[str, ...] = ()
    missing: tuple[str, ...] = ()
    skipped_annotated: tuple[str, ...] = ()
    failed: tuple[tuple[str, str], ...] = ()
    #: **因「裁判后端没答」被跳过**的卡（H-36）。⚠️ 这不是失败——是
    #: **不许拿不可信的判分去盖掉 LS 上已有的好卡**（刷新会把卡内容换成
    #: 当前产物；当前产物若因后端故障而缺判，刷新等于把好卡刷成「无法判定」）。
    skipped_untrusted: tuple[str, ...] = ()
    #: 只读诊断：抽样的差异字段分布（字段名 → 条数）。
    diff_histogram: dict[str, int] = field(default_factory=dict)

    @property
    def count(self) -> int:
        return len(self.items)

    def summary(self) -> str:
        return (
            f"待刷 {self.count} 条 · 无差异 {len(self.unchanged)} · "
            f"LS 上没有 {len(self.missing)} · 有标注跳过 {len(self.skipped_annotated)} · "
            f"判分不可信跳过 {len(self.skipped_untrusted)} · "
            f"失败 {len(self.failed)}"
        )


def _bucket_filter(buckets: Iterable[str]) -> set[str]:
    return {b.strip() for b in buckets if b.strip()}


def plan_refresh(
    scout_dir: pathlib.Path,
    *,
    client: Any,
    task_views_root: str | pathlib.Path | None = None,
    buckets: Iterable[str] = ("positive", "negative", "quarantine"),
    limit: int | None = None,
    include_annotated: bool = False,
    project_id: int | None = None,
) -> RefreshPlan:
    """读 scout 聚合产物 → 与 LS 上的卡逐条比对 → 出刷新计划（**不写**）。

    Args:
        scout_dir: 含 ``trajectory_candidates.jsonl`` 的目录。
        client: :class:`~trajectory_pipeline.evaluation.ls_bridge.client.LabelStudioClient`。
        task_views_root: T 视图根（默认 storage 默认根）。
        buckets: 只管这些桶的候选。
        limit: 最多计划多少条（调试用）。
        include_annotated: **连有人工标注的卡也刷**。
            ⚠️ 默认 False —— ``PATCH data`` 会不会影响 annotation **没验过**。
        project_id: LS 项目 id（``None`` ⇒ 不查项目，直接按 client 侧配置）。

    Returns:
        :class:`RefreshPlan`。
    """
    wanted = _bucket_filter(buckets)
    records = read_trajectory_records(pathlib.Path(scout_dir))
    if not records:
        return RefreshPlan()

    # LS 上已有的 task：按 sample_id 索引（**只认带 sample_id 的**）
    ls_tasks: dict[str, dict[str, Any]] = {}
    if project_id is not None:
        for task in client.list_recent_tasks(project_id, limit=1000):
            sid = str((task.get("data") or {}).get("sample_id") or "")
            if sid:
                ls_tasks[sid] = task

    items: list[RefreshItem] = []
    unchanged: list[str] = []
    missing: list[str] = []
    skipped_annotated: list[str] = []
    skipped_untrusted: list[str] = []
    failed: list[tuple[str, str]] = []
    histogram: dict[str, int] = {}

    for bucket, record in records:
        if wanted and bucket not in wanted:
            continue
        sid = str(record["trajectory_id"])
        # ⚠️ **H-36：判分不可信的不刷**——当前产物若因裁判后端故障而缺判，
        # 刷进去等于把 LS 上已有的好卡刷成「无法判定」。**刷新只改内容，
        # 不许把好内容刷坏**。
        if llm_unanswered(record):
            skipped_untrusted.append(sid)
            log.warning(
                "plan_refresh: %s 因裁判后端没答被跳过（不拿不可信判分盖旧卡）",
                sid,
            )
            continue
        ls_task = ls_tasks.get(sid)
        if ls_task is None:
            missing.append(sid)
            continue
        try:
            task, prediction, hits = export_trajectory_one(
                record, bucket, task_views_root=task_views_root
            )
        except Exception as exc:  # noqa: BLE001 - 单条失败不打断整批
            failed.append((sid, f"导出失败 {type(exc).__name__}: {exc}"))
            continue
        if task is None:
            failed.append((sid, f"T 视图读不到或投影不一致: {hits}"))
            continue

        old_data = ls_task.get("data") or {}
        new_data = dict(task.get("data") or {})
        diff = diff_fields(old_data, new_data)
        if not diff:
            unchanged.append(sid)
            continue

        # ⚠️ 纪律 2：有人工标注的**默认跳过**（PATCH 是否影响标注未验过）。
        try:
            annotations = client.list_task_annotations(int(ls_task["id"]))
        except Exception as exc:  # noqa: BLE001
            failed.append((sid, f"查标注失败 {type(exc).__name__}: {exc}"))
            continue
        if annotations and not include_annotated:
            skipped_annotated.append(sid)
            continue

        # ⚠️ 纪律 3：保留旧卡的 uploaded_at（它是「什么时候上的 LS」的事实）
        uploaded_at = str(old_data.get("uploaded_at") or "")
        if uploaded_at:
            new_data["uploaded_at"] = uploaded_at

        prediction_id: int | None = None
        new_prediction: dict[str, Any] | None = None
        if prediction:
            try:
                existing = client.list_task_predictions(int(ls_task["id"]))
            except Exception as exc:  # noqa: BLE001
                failed.append((sid, f"查预标注失败 {type(exc).__name__}: {exc}"))
                continue
            if existing:
                prediction_id = int(existing[0]["id"])
                new_prediction = {
                    "result": prediction["result"],
                    "score": prediction["score"],
                    "model_version": prediction.get("model_version", ""),
                }

        for key in diff:
            histogram[key] = histogram.get(key, 0) + 1
        items.append(RefreshItem(
            sample_id=sid,
            ls_task_id=int(ls_task["id"]),
            diff_fields=diff,
            new_data=new_data,
            uploaded_at=uploaded_at,
            prediction_id=prediction_id,
            new_prediction=new_prediction,
        ))
        if limit and len(items) >= limit:
            break

    return RefreshPlan(
        items=tuple(items),
        unchanged=tuple(unchanged),
        missing=tuple(missing),
        skipped_annotated=tuple(skipped_annotated),
        skipped_untrusted=tuple(skipped_untrusted),
        failed=tuple(failed),
        diff_histogram=dict(sorted(histogram.items(), key=lambda kv: -kv[1])),
    )


def apply_refresh(plan: RefreshPlan, *, client: Any) -> dict[str, Any]:
    """执行刷新计划。**逐条独立** —— 一条失败不打断其余。

    ⚠️ **只 PATCH 两处**：``/api/tasks/{id}`` 的 ``data`` 与
    ``/api/predictions/{id}`` ⇒ **绝不碰 ``label_config``**。
    """
    tasks_patched = 0
    predictions_patched = 0
    failed: list[tuple[str, str]] = []
    for item in plan.items:
        try:
            client.update_task_data(item.ls_task_id, item.new_data)
            tasks_patched += 1
        except Exception as exc:  # noqa: BLE001 - 单条失败不打断整批
            failed.append((item.sample_id, f"PATCH task: {type(exc).__name__}: {exc}"))
            continue
        if item.prediction_id is not None and item.new_prediction:
            try:
                client.update_prediction(
                    item.prediction_id,
                    result=item.new_prediction["result"],
                    score=item.new_prediction["score"],
                    model_version=item.new_prediction.get("model_version", ""),
                )
                predictions_patched += 1
            except Exception as exc:  # noqa: BLE001
                failed.append(
                    (item.sample_id, f"PATCH prediction: {type(exc).__name__}: {exc}")
                )
    return {
        "tasks_patched": tasks_patched,
        "predictions_patched": predictions_patched,
        "failed": failed,
    }


__all__ = [
    "VOLATILE_KEYS",
    "RefreshItem",
    "RefreshPlan",
    "apply_refresh",
    "diff_fields",
    "plan_refresh",
]
