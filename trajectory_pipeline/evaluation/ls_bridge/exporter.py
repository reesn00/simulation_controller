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
* **无 tier / min_score 过滤** —— 上传侧不二次审判, bucket 即口径。
  默认三态全传（positive + negative + quarantine）: quarantine 是
  "评分器缺判"而非"数据劣质", 上传后由人工裁决（决策 2026-10-15:
  放宽 quarantine 口径, 缺判数据进 LS 供人工分析）。
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
    derive_overall,
    render_risk_hints,
    render_scorecard_text,
)
from trajectory_pipeline.evaluation.ls_bridge.settings import LSSettings
from trajectory_pipeline.evaluation.scout.types import (
    AggregationLogEntry,
    ScoutRecord,
    TrajectoryRecord,
)
from trajectory_pipeline.storage.read_p3 import read_views
from trajectory_pipeline.storage.task_views import read_task_view

log = logging.getLogger(__name__)

#: bucket → scout 产物文件名。三态全部可传 —— quarantine 样本的评分卡
#: 会带 missing 维度提示, 标注页由人工裁决"合格 / 劣质 / 无法判定"。
BUCKET_FILES: dict[str, str] = {
    "positive": "positive_candidates.jsonl",
    "negative": "negative_candidates.jsonl",
    "quarantine": "quarantine.jsonl",
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
# trajectory 主路径:读聚合产物(D14.5) → per-Run 导出
# ---------------------------------------------------------------------------


def _derive_trajectory_bucket(record: TrajectoryRecord) -> str:
    """``TrajectoryRecord`` → bucket 标签(``positive``/``negative``/``quarantine``)。

    派生顺序(决策 D14.5):

    1. ``status`` 不是 ``scored`` → ``quarantine``(``undetermined`` /
       ``not_run`` 都进 quarantine;**桶由 status 一票决定**,不走 score 派生——
       score 派生只看四维到齐/低分/veto,而 not_run 时 score 字段可能仍
       有数,但那不是「判了」而是「没跑通」)
    2. 否则 :func:`derive_overall`(veto 先于低分先于缺判)→
       ``negative`` / ``positive``;都不是(极端 case)→ ``quarantine``。
    """
    status = str(record.get("status", ""))
    if status != "scored":
        return "quarantine"
    overall = derive_overall(record)  # type: ignore[arg-type]
    kind = overall.get("kind")
    if kind == "negative":
        return "negative"
    if kind == "positive":
        return "positive"
    return "quarantine"


def _render_aggregation_log(entries: list[AggregationLogEntry]) -> str:
    """聚合 log → 多行文本(给 ``audit_text`` 复用)。"""
    if not entries:
        return "(无聚合留痕)"
    lines: list[str] = []
    for entry in entries:
        unit = entry.get("unit", "-")
        action = entry.get("action", "?")
        reason = entry.get("reason", "")
        lines.append(f"[{unit}] {action}: {reason}")
    return "\n".join(lines)


def _render_trajectory_audit_text(
    record: TrajectoryRecord, bucket: str
) -> str:
    """per-Run 任务机器立场 + 聚合留痕(标注员一眼看到最差 unit 是谁)。

    与 :func:`render_audit_text` 的差异:多了「参与 unit」+「聚合留痕」两段,
    任务级桶不直接来自单 unit,而是来自最差 unit —— 这两段是给标注员的
    解释链。
    """
    lines: list[str] = []
    if bucket == "negative":
        lines.append("机器判定(任务级): 劣质(任一 unit 低分/veto), 请重点复核。")
    elif bucket == "quarantine":
        lines.append("机器判定(任务级): 无法判定(存在缺判维度), 请人工裁决 合格/劣质/无法判定。")
    else:
        lines.append("机器判定(任务级): 合格(无负向信号)。")

    tags = record.get("tags") or []
    if tags:
        lines.append("机器标签(并集): " + ", ".join(tags))

    units = record.get("unit_outcomes") or []
    if units:
        lines.append(f"参与 unit: {', '.join(units)}")

    log_entries = record.get("aggregation_log") or []
    if log_entries:
        lines.append("── 聚合留痕 ──")
        for entry in log_entries:
            unit = entry.get("unit", "-")
            action = entry.get("action", "?")
            reason = entry.get("reason", "")
            lines.append(f"  [{unit}] {action}: {reason}")

    for score in record.get("scores") or []:
        if score.get("is_veto"):
            reasons = "; ".join(score.get("reasons") or [])
            lines.append(f"{score['dimension']}: VETO — {reasons}")
        elif score.get("score") is not None and score["score"] <= 2:
            reasons = "; ".join(score.get("reasons") or [])
            lines.append(f"{score['dimension']}: 低分 — {reasons}")
        elif score.get("score") is None:
            reasons = "; ".join(score.get("reasons") or [])
            lines.append(f"{score['dimension']}: 缺判 — {reasons or '无理由记录'}")
    return "\n".join(lines)


def read_trajectory_records(
    scout_dir: Path,
) -> list[tuple[str, TrajectoryRecord]]:
    """读 ``trajectory_candidates.jsonl``(D14.5 聚合产物)→ ``(bucket, record)`` 列表。

    bucket 派生见 :func:`_derive_trajectory_bucket`——**不读 raw bucket 文件名**,
    trajectory_candidates.jsonl 是单一文件不分桶(聚合期已经消解)。

    坏行 / 缺主键行跳过并计数(同 :func:`read_scout_records` 的容错纪律)。
    """
    # 延迟 import:scout.aggregate 顶层 import ls_bridge.exporter,反向 import
    # 会触发循环。TRAJECTORY_BUCKET_FILE 是常量,函数级 import 走 sys.modules
    # 缓存,不会重复加载。
    from trajectory_pipeline.evaluation.scout.aggregate import TRAJECTORY_BUCKET_FILE

    path = Path(scout_dir) / TRAJECTORY_BUCKET_FILE
    if not path.is_file():
        log.warning("read_trajectory_records: %s 不存在, 跳过", path)
        return []
    out: list[tuple[str, TrajectoryRecord]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            raw = json.loads(line)
        except (json.JSONDecodeError, ValueError):
            log.warning("read_trajectory_records: %s 有坏行, 已跳过", path)
            continue
        if not isinstance(raw, dict) or not raw.get("trajectory_id"):
            continue
        bucket = _derive_trajectory_bucket(raw)  # type: ignore[arg-type]
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


def render_audit_text(record: ScoutRecord, bucket: str = "positive") -> str:
    """劣质/缺判标记: tags + 各维 veto / 低分 / 缺判理由。

    三态各自的开场行让标注员第一眼知道机器立场:
    * negative —— 失败维度与理由, 重点复核
    * quarantine —— 缺判维度与原因, 人工裁决"合格/劣质/无法判定"
    * positive —— 无负向信号
    """
    lines: list[str] = []
    if bucket == "negative":
        lines.append("机器判定: 劣质(存在低分/veto 维度), 请重点复核。")
    elif bucket == "quarantine":
        lines.append("机器判定: 无法判定(存在缺判维度), 请人工裁决 合格/劣质/无法判定。")
    else:
        lines.append("机器判定: 合格(无负向信号)。")
    tags = record.get("tags") or []
    if tags:
        lines.append("机器标签: " + ", ".join(tags))
    for score in record.get("scores") or []:
        if score.get("is_veto"):
            reasons = "; ".join(score.get("reasons") or [])
            lines.append(f"{score['dimension']}: VETO — {reasons}")
        elif score.get("score") is not None and score["score"] <= 2:
            reasons = "; ".join(score.get("reasons") or [])
            lines.append(f"{score['dimension']}: 低分 — {reasons}")
        elif score.get("score") is None:
            reasons = "; ".join(score.get("reasons") or [])
            lines.append(f"{score['dimension']}: 缺判 — {reasons or '无理由记录'}")
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
        "audit_text": render_audit_text(record, bucket),
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


def export_trajectory_one(
    record: TrajectoryRecord,
    bucket: str,
    *,
    task_views_root: str | Path | None = None,
    scorecard_enabled: bool = True,
    scan_settings: Any = None,
) -> tuple[dict[str, Any] | None, dict[str, Any] | None, list[ScanHit]]:
    """单条 :class:`TrajectoryRecord` → ``(task, prediction, scan_hits)``。

    读 **T 视图**(:func:`read_task_view`)拿 messages/openai/meta,不是
    P3 单 unit 视图。T 视图缺失返回 ``(None, None, [])`` —— 主路径
    fail-closed,「T 没落」时整条不推半份(决策 D14.3)。

    评分卡构造走 :func:`build_scorecard`:把 ``trajectory_id`` 当
    ``sample_id`` 喂进去,其他字段一一对得上 —— 不重写一份。

    ``data`` 里多出两个 per-Run 专用字段:
    * ``unit_outcomes`` —— 各 unit sample_id,给标注员跳查
    * ``aggregation_log_text`` —— min/veto/缺 unit 决策留痕,人工复查
      聚合口径
    """
    from trajectory_pipeline.evaluation.ls_bridge.settings import CredentialScanSettings

    tid = str(record["trajectory_id"])
    views = read_task_view(tid, root=task_views_root)
    if views is None:
        return None, None, []

    # 构造 ScoutRecord-shape 喂 build_scorecard:trajectory_id 当 sample_id。
    pseudo_scout: ScoutRecord = {
        "sample_id": tid,
        "task_id": str(record["task_id"]),
        "run_id": str(record["run_id"]),
        "status": record["status"],  # type: ignore[typeddict-item]
        "scores": record["scores"],
        "tags": record["tags"],  # type: ignore[typeddict-item]
        "evidence_supplement": record.get("evidence_supplement") or {},
    }
    scorecard = build_scorecard(pseudo_scout, classify_kind=bucket)
    if not scorecard_enabled:
        scorecard = {**scorecard, "enabled": False}

    tags = list(record.get("tags") or [])
    data: dict[str, Any] = {
        "sample_id": tid,
        "task_id": pseudo_scout["task_id"],
        "run_id": pseudo_scout["run_id"],
        "status": bucket,
        "tags": tags,
        "tags_text": ", ".join(tags) if tags else "无",
        "audit_text": _render_trajectory_audit_text(record, bucket),
        "scorecard_text": render_scorecard_text(scorecard),
        "risk_hints_text": render_risk_hints(scorecard),
        "messages_text": render_messages_text(views.get("messages")),
        "openai_text": render_openai_text(views.get("openai")),
        "meta_text": render_meta_text(views.get("meta")),
        "messages_view_raw": views.get("messages"),
        "openai_view_raw": views.get("openai"),
        "meta_view_raw": views.get("meta"),
        # per-Run 专用字段(标注员跳查 + 复查聚合口径)
        "unit_outcomes": list(record.get("unit_outcomes") or []),
        "aggregation_log_text": _render_aggregation_log(
            list(record.get("aggregation_log") or [])
        ),
    }

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
    task_views_root: str | Path | None = None,
    limit: int | None = None,
    legacy_unit_trajectory: bool = False,
) -> ExportPlan:
    """scout 目录 → 逐条导出 → 凭据扫描 → 汇总成 :class:`ExportPlan`。

    路径(决策 D14.5):

    * **trajectory 主路径** (``legacy_unit_trajectory=False``, 默认):
      读 ``trajectory_candidates.jsonl``(per-Run 聚合产物)→ 读 T 视图
      → 上传。LS 标注员看完整任务,不是单 unit。
    * **per-unit 旧路径** (``legacy_unit_trajectory=True``, 过渡期):
      读 scout 三件套 + P3 视图,行为与 Phase 3 之前一致。**默认不开启**,
      显式传旗标才走 —— 见 :func:`_export_batch_legacy_unit`。

    过滤只有两道(两路径共用):视图缺失(skip, fail-closed 不推半份
    数据)→ 凭据扫描(reject, fail-closed 留痕)。v1 的 tier / min_score /
    skip_task_ids 过滤不迁移 —— scout 已分流, 上传侧不再二次审判。
    """
    settings = settings or LSSettings()
    plan = ExportPlan()

    if legacy_unit_trajectory:
        return _export_batch_legacy_unit(
            scout_dir, settings=settings, buckets=buckets,
            views_root=views_root, limit=limit,
        )

    # ── trajectory 主路径 ──
    records = read_trajectory_records(Path(scout_dir))
    if limit is not None:
        records = records[: max(0, int(limit))]

    for bucket, record in records:
        tid = str(record.get("trajectory_id", ""))
        task, prediction, hits = export_trajectory_one(
            record,
            bucket,
            task_views_root=task_views_root,
            scorecard_enabled=settings.scorecard.enabled,
            scan_settings=settings.credential_scan,
        )
        if task is None and hits:
            plan.rejected.append((tid, hits))
            log.error(
                "export_batch(trajectory): %s 命中凭据扫描, 拒推 (%s)",
                tid,
                "; ".join(h.describe() for h in hits),
            )
            continue
        if task is None:
            plan.skipped.append((tid, "T 视图缺失或不可读"))
            continue
        plan.tasks.append(task)
        if prediction is not None:
            plan.predictions.append(prediction)
    return plan


def _export_batch_legacy_unit(
    scout_dir: Path,
    *,
    settings: LSSettings,
    buckets: Iterable[str],
    views_root: str | Path | None,
    limit: int | None,
) -> ExportPlan:
    """per-unit 旧路径(``--legacy-unit-trajectory`` 旗标)。

    行为与 Phase 3 之前一致:读 scout 三件套 + P3 视图,逐 unit 上传。
    过渡期(3 周)结束后移除。打印 deprecation banner,提醒上游
    ``ls_bridge.cli._cmd_upload``。
    """
    log.warning(
        "DEPRECATED: --legacy-unit-trajectory 走 per-unit P3 路径, "
        "该旗标计划 3 周后移除(决策 D14.5)"
    )
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
                "_export_batch_legacy_unit: %s 命中凭据扫描, 拒推 (%s)",
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
    "read_trajectory_records",
    "render_messages_text",
    "render_openai_text",
    "render_meta_text",
    "render_audit_text",
    "build_task_data",
    "build_task",
    "build_prediction",
    "export_one",
    "export_trajectory_one",
    "export_batch",
    "push_batch",
]
