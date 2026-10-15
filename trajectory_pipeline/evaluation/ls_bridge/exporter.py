"""ls_bridge.exporter: scout 候选 → LS task/prediction。

重写自 ``archive/v1/label_studio/task_exporter.py``（1050 行, C3 视图契约）,
v2 数据契约换成 **scout 三件套 + P3 视图**:

    scout_dir/positive_candidates.jsonl ─┐
    scout_dir/negative_candidates.jsonl ─┴→ ScoutRecord
                    ↓ storage.read_p3.read_views(sample_id)   fail-closed
              {messages, openai, meta} 三视图
                    ↓ build_task_data / build_scorecard
              task.data + scorecard.v2 prediction
                    ↓ scan（R11 凭据, fail-closed）
              ExportPlan → push_batch → LS

与 v1 的关键差异:

* **不发 ``inner_id``** —— v1 实测 LS 1.23 对字符串 inner_id 不去重
  （批量导入时该字段被静默忽略）, 台账才是唯一去重, 索性不发。
* **无 tier / min_score 过滤** —— scout 已三态分流, 上传侧只按 bucket
  取数（quarantine 留本地, 不上传）。
* prediction 的 ``task`` 字段在 build 时填 ``sample_id`` 占位,
  :func:`push_batch` 推送时替换成 LS 数字 id（``import/predictions``
  只认数字 id, 而 ``/import`` 返回体不带 id）。
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Mapping

from trajectory_pipeline.evaluation.ls_bridge.scan import ScanHit, scan_for_credentials
from trajectory_pipeline.evaluation.ls_bridge.scorecard import (
    CONFIDENCE_TO_SCORE,
    build_scorecard,
    render_risk_hints,
    render_scorecard_text,
)
from trajectory_pipeline.evaluation.ls_bridge.settings import LSSettings
from trajectory_pipeline.evaluation.scout.types import ScoutRecord
from trajectory_pipeline.storage.read_p3 import read_views

log = logging.getLogger(__name__)

#: bucket → scout 产物文件名。quarantine **刻意不在表内** —— 无法判定的
#: 样本没有标注价值, 留本地等上游修复。
BUCKET_FILES: dict[str, str] = {
    "positive": "positive_candidates.jsonl",
    "negative": "negative_candidates.jsonl",
}

#: label_config 里真实存在的控件名。``risk_hints`` 是 predictions 预标注
#: 的唯一落点 —— from_name 对不上时 LS 静默丢弃整条预测
#: (201 {"created": 0}), 由契约测试盯住。
RISK_HINTS_CONTROL = "risk_hints"
TASK_ANCHOR_CONTROL = "task_anchor"

#: 凭据扫描覆盖的 task.data 字段（渲染前的**原始视图**）。
_SCAN_VIEW_KEYS = ("messages_view_raw", "openai_view_raw", "meta_view_raw")


# ---------------------------------------------------------------------------
# scout 产物读取
# ---------------------------------------------------------------------------


def read_scout_records(
    scout_dir: Path, buckets: Iterable[str] = ("positive", "negative")
) -> list[tuple[str, ScoutRecord]]:
    """读 scout 三件套里的指定 bucket, 返回 ``(bucket, record)`` 列表。

    坏行跳过并计数（记 log）—— scout 产物是上游落盘的 JSONL, 单行损坏
    不该让整批上传失败。
    """
    out: list[tuple[str, ScoutRecord]] = []
    for bucket in buckets:
        filename = BUCKET_FILES.get(bucket)
        if filename is None:
            raise ValueError(f"未知 bucket: {bucket!r} (可选: {sorted(BUCKET_FILES)})")
        path = Path(scout_dir) / filename
        if not path.is_file():
            log.warning("read_scout_records: %s 不存在, 跳过该 bucket", path)
            continue
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                raw = json.loads(line)
            except (json.JSONDecodeError, ValueError):
                log.warning("read_scout_records: %s 有坏行, 已跳过", path)
                continue
            if isinstance(raw, dict) and raw.get("sample_id"):
                out.append((bucket, raw))  # type: ignore[arg-type]
    return out


# ---------------------------------------------------------------------------
# 渲染: P3 视图 → 标注页可读文本
# ---------------------------------------------------------------------------


def render_messages_text(messages: Any) -> str:
    """messages 视图（纯数组 ``{role, content}``）→ 逐轮文本。"""
    if not isinstance(messages, list):
        return "(messages 视图缺失或形态异常)"
    lines: list[str] = []
    for index, message in enumerate(messages, 1):
        if not isinstance(message, dict):
            continue
        role = str(message.get("role", "?"))
        content = message.get("content") or ""
        lines.append(f"── [{index}] {role} ──\n{content}")
    return "\n".join(lines) if lines else "(空轨迹)"


def render_openai_text(openai: Any) -> str:
    """openai 视图（function-calling 形态）→ 逐轮文本。

    tool_call 的 ``arguments`` 可能是任意 JSON 串, 原样缩进展示 —— 标注员
    需要核对的就是参数本身。
    """
    if not isinstance(openai, list):
        return "(openai 视图缺失或形态异常)"
    lines: list[str] = []
    for message in openai:
        if not isinstance(message, dict):
            continue
        role = str(message.get("role", "?"))
        if message.get("content"):
            lines.append(f"── {role} ──\n{message['content']}")
        for call in message.get("tool_calls") or []:
            if not isinstance(call, dict):
                continue
            function = call.get("function") or {}
            lines.append(
                f"── {role} → tool_call ──\n{function.get('name', '?')}"
                f"({function.get('arguments', '')})"
            )
    return "\n".join(lines) if lines else "(空)"


def render_meta_text(meta: Any) -> str:
    """meta 视图 → ``key: value`` 行。复杂值 JSON 序列化。"""
    if not isinstance(meta, dict):
        return "(meta 视图缺失或形态异常)"
    lines: list[str] = []
    for key in sorted(meta):
        value = meta[key]
        if isinstance(value, (dict, list)):
            value = json.dumps(value, ensure_ascii=False)
        lines.append(f"{key}: {value}")
    return "\n".join(lines) if lines else "(空 meta)"


def render_audit_text(record: ScoutRecord) -> str:
    """劣质标记: tags + 各维 veto / 低分理由。positive 也渲染（显示无信号）。"""
    lines: list[str] = []
    tags = record.get("tags") or []
    if tags:
        lines.append("机器标签: " + ", ".join(tags))
    for score in record.get("scores") or []:
        if score.get("is_veto") or (
            score.get("score") is not None and score["score"] <= 2
        ):
            reasons = "; ".join(score.get("reasons") or [])
            lines.append(f"{score['dimension']}: {'VETO' if score.get('is_veto') else '低分'} — {reasons}")
    if not lines:
        lines.append("无负向信号（scout 判为 positive）。")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# task / prediction 构建
# ---------------------------------------------------------------------------


def build_task_data(
    record: ScoutRecord, bucket: str, views: Mapping[str, Any]
) -> tuple[dict[str, Any], dict[str, Any]]:
    """ScoutRecord + P3 视图 → ``(task.data, scorecard)``。

    ``data`` 里同时放渲染文本（标注页展示）与原始视图（``*_view_raw``,
    凭据扫描用 + JSON 导出留底）。**不放 inner_id** —— LS 1.23 对字符串
    inner_id 不去重, 台账才是唯一防线。
    """
    scorecard = build_scorecard(record, classify_kind=bucket)
    tags = record.get("tags") or []
    data = {
        "sample_id": record["sample_id"],
        "task_id": record["task_id"],
        "run_id": record.get("run_id", ""),
        "status": bucket,
        "tags": list(tags),
        "tags_text": ", ".join(tags) if tags else "无",
        "audit_text": render_audit_text(record),
        "scorecard_text": render_scorecard_text(scorecard),
        "risk_hints_text": render_risk_hints(scorecard),
        "messages_text": render_messages_text(views.get("messages")),
        "openai_text": render_openai_text(views.get("openai")),
        "meta_text": render_meta_text(views.get("meta")),
        "messages_view_raw": views.get("messages"),
        "openai_view_raw": views.get("openai"),
        "meta_view_raw": views.get("meta"),
    }
    return data, scorecard


def build_task(data: dict[str, Any]) -> dict[str, Any]:
    """task.data → LS import payload。刻意不带 ``inner_id``（见模块 docstring）。"""
    return {"data": data}


def build_prediction(
    data: dict[str, Any], scorecard: dict[str, Any] | None
) -> dict[str, Any] | None:
    """scorecard.v2 → ML 预标注 —— **只填风险提示, 绝不填 overall_decision**。

    ⚠️ ``result`` 必须是 ``[{from_name, to_name, type, value}]`` region 列表,
    且 ``from_name`` 必须是 label_config 里**真实存在**的控件名。违反时
    LS 一声不吭: ``POST .../import/predictions`` 照样回 ``201 {"created": 0}``。
    ``task`` 在这里是 ``sample_id`` 占位, :func:`push_batch` 推送前替换成
    LS 数字 id。
    """
    if scorecard is None or not scorecard.get("enabled"):
        return None
    overall = scorecard.get("overall") or {}
    return {
        "task": data["sample_id"],
        "result": [
            {
                "from_name": RISK_HINTS_CONTROL,
                "to_name": TASK_ANCHOR_CONTROL,
                "type": "textarea",
                "value": {"text": render_risk_hints(scorecard).splitlines()},
            }
        ],
        "model_version": f"scorecard/{scorecard.get('schema_version', 'scorecard.v2')}",
        "score": CONFIDENCE_TO_SCORE.get(str(overall.get("confidence")), 0.5),
    }


# ---------------------------------------------------------------------------
# 批量导出
# ---------------------------------------------------------------------------


@dataclass
class ExportPlan:
    """一次批量推送的计划 + 逐条结果。不含任何凭据。"""

    tasks: list[dict[str, Any]] = field(default_factory=list)
    predictions: list[dict[str, Any]] = field(default_factory=list)
    skipped: list[tuple[str, str]] = field(default_factory=list)  # (sample_id, reason)
    rejected: list[tuple[str, list[ScanHit]]] = field(default_factory=list)

    @property
    def count(self) -> int:
        return len(self.tasks)

    def summary(self) -> str:
        return (
            f"待推送 {len(self.tasks)} 条 / 预标注 {len(self.predictions)} 条 / "
            f"跳过 {len(self.skipped)} 条 / 拒推 {len(self.rejected)} 条"
        )


def export_one(
    record: ScoutRecord,
    bucket: str,
    *,
    views_root: str | Path | None = None,
    scorecard_enabled: bool = True,
    scan_settings: Any = None,
) -> tuple[dict[str, Any] | None, dict[str, Any] | None, list[ScanHit]]:
    """单条 ScoutRecord → ``(task, prediction, scan_hits)``。

    视图缺失返回 ``(None, None, [])`` 的情况由调用方
    :func:`export_batch` 记 skipped —— 本函数只管"视图在手上"之后的链路,
    分工与 v1 ``export_one`` 一致（扫描命中返回 ``(None, None, hits)``,
    由调用方决定 fail-closed 记录方式）。
    """
    from trajectory_pipeline.evaluation.ls_bridge.settings import CredentialScanSettings

    views = read_views(record["sample_id"], root=views_root)
    if views is None:
        return None, None, []

    data, scorecard = build_task_data(record, bucket, views)
    if not scorecard_enabled:
        scorecard = {**scorecard, "enabled": False}

    scan = scan_settings if scan_settings is not None else CredentialScanSettings()
    hits: list[ScanHit] = []
    for view in _SCAN_VIEW_KEYS:
        if data.get(view):
            hits.extend(scan_for_credentials(data[view], view=view, settings=scan))
    if hits:
        return None, None, hits

    return build_task(data), build_prediction(data, scorecard), []


def export_batch(
    scout_dir: Path,
    *,
    settings: LSSettings | None = None,
    buckets: Iterable[str] = ("positive", "negative"),
    views_root: str | Path | None = None,
    limit: int | None = None,
) -> ExportPlan:
    """scout 目录 → 逐条导出 → 凭据扫描 → 汇总成 :class:`ExportPlan`。

    过滤只有两道: 视图缺失（skip, fail-closed 不推半份数据）→ 凭据扫描
    （reject, fail-closed 留痕）。v1 的 tier / min_score / skip_task_ids
    过滤不迁移 —— scout 已分流, 上传侧不再二次审判。
    """
    settings = settings or LSSettings()
    plan = ExportPlan()
    records = read_scout_records(Path(scout_dir), buckets)
    if limit is not None:
        records = records[: max(0, int(limit))]

    for bucket, record in records:
        sample_id = str(record.get("sample_id", ""))
        task, prediction, hits = export_one(
            record,
            bucket,
            views_root=views_root,
            scorecard_enabled=settings.scorecard.enabled,
            scan_settings=settings.credential_scan,
        )
        if task is None and hits:
            plan.rejected.append((sample_id, hits))
            log.error(
                "export_batch: %s 命中凭据扫描, 拒推 (%s)",
                sample_id,
                "; ".join(h.describe() for h in hits),
            )
            continue
        if task is None:
            plan.skipped.append((sample_id, "P3 视图缺失或不可读"))
            continue
        plan.tasks.append(task)
        if prediction is not None:
            plan.predictions.append(prediction)
    return plan


# ---------------------------------------------------------------------------
# 推送
# ---------------------------------------------------------------------------


def push_batch(
    plan: ExportPlan,
    *,
    settings: LSSettings,
    project_id: int,
    client: Any | None = None,
    index: Any | None = None,
    on_progress: Any | None = None,
) -> dict[str, Any]:
    """按 ``upload.batch_size`` 分批推送。``plan`` 为空时直接返回。

    Args:
        index: :class:`~trajectory_pipeline.evaluation.ls_bridge.push_index.PushIndex`,
            记录 ``sample_id → LS task id``。**不传就退化成不判重**（推重复）,
            所以生产路径必须传。

    去重说明: LS 1.23 **既不认字符串 inner_id、也不按 inner_id 去重**,
    所以判重只能靠本地台账。推完一批回头捞 LS 刚建的那批 id 存进台账 ——
    同一份数据同时供判重和预标注使用。
    """
    from trajectory_pipeline.evaluation.ls_bridge.client import build_client
    from trajectory_pipeline.evaluation.ls_bridge.push_index import PushIndex

    if not plan.tasks:
        return {
            "tasks_pushed": 0,
            "predictions_pushed": 0,
            "batches": 0,
            "skipped_duplicate": 0,
        }

    if client is None:
        client = build_client(settings)
    if index is None:
        index = PushIndex.load(PushIndex.default_path(project_id))

    size = max(1, settings.upload.batch_size)
    tasks_pushed = 0
    predictions_pushed = 0
    batches = 0
    skipped_duplicate = 0
    # predictions 按 sample_id 对齐到 task 批次 —— LS 要求 task 已存在。
    pred_by_sample = {p["task"]: p for p in plan.predictions}

    # 判重: 台账里有的一律不再推。LS 侧那份被删干净时台账会过期,
    # 但**宁可不推也不推重复** —— 重复样本会污染标注统计。
    fresh: list[dict[str, Any]] = []
    for task in plan.tasks:
        sample_id = str((task.get("data") or {}).get("sample_id") or "")
        if sample_id and index.has(sample_id):
            skipped_duplicate += 1
            continue
        fresh.append(task)
    if skipped_duplicate:
        log.info(
            "push_batch: %d 条已推过, 本次跳过 (LS 端不按 sample_id 去重)",
            skipped_duplicate,
        )

    for start in range(0, len(fresh), size):
        batch = fresh[start : start + size]
        tasks_pushed += client.import_tasks(project_id, batch)

        # 解析这批刚建出来的 LS task id。import 的返回体只有计数不带 id。
        pairs: list[tuple[str, int]] = []
        try:
            recent = client.list_recent_tasks(project_id, limit=len(batch))
        except Exception as exc:  # noqa: BLE001 - 预标注不该拖垮 task 推送
            log.warning("push_batch: 取不回刚推送的 task id, 预标注将跳过: %s", exc)
            recent = []
        id_by_sample = {
            str((item.get("data") or {}).get("sample_id") or ""): item.get("id")
            for item in recent
        }
        for task in batch:
            sample_id = str((task.get("data") or {}).get("sample_id") or "")
            ls_task_id = id_by_sample.get(sample_id)
            if isinstance(ls_task_id, int):
                pairs.append((sample_id, ls_task_id))
        if pairs:
            index.record_many(
                pairs,
                task_refs={
                    str((t.get("data") or {}).get("sample_id") or ""): str(
                        (t.get("data") or {}).get("task_id") or ""
                    )
                    for t in batch
                },
            )

        preds = []
        for sample_id, ls_task_id in pairs:
            pred = pred_by_sample.get(sample_id)
            if pred is not None:
                preds.append({**pred, "task": ls_task_id})
        if preds:
            predictions_pushed += client.import_predictions(project_id, preds)
        batches += 1
        if on_progress:
            on_progress(min(start + size, len(fresh)), len(fresh))

    return {
        "tasks_pushed": tasks_pushed,
        "predictions_pushed": predictions_pushed,
        "batches": batches,
        "skipped_duplicate": skipped_duplicate,
    }


__all__ = [
    "BUCKET_FILES",
    "RISK_HINTS_CONTROL",
    "TASK_ANCHOR_CONTROL",
    "ExportPlan",
    "read_scout_records",
    "render_messages_text",
    "render_openai_text",
    "render_meta_text",
    "render_audit_text",
    "build_task_data",
    "build_task",
    "build_prediction",
    "export_one",
    "export_batch",
    "push_batch",
]
