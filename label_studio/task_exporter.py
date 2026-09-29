"""label_studio.task_exporter: C3 4 视图文件 → LS ``task.data`` + ``predictions``.

方案 §4.1 的字段映射在这里落地。三点实施要点:

1. **``task_id`` 不在 meta.json 里** —— 只能从文件名 stem 解析
   (``T001__useramulation-xxx_refined``)。
2. **不持有 raw trajectory** —— C3 已是脱敏产物, 本模块只读 ``*.messages.json`` /
   ``*.openai.json`` / ``*.qwenjina.txt`` / ``*.meta.json`` 四份**视图文件**,
   绝不碰 C1 trajectory。
3. **R11 凭据扫描 fail-closed** —— 命中即 :class:`CredentialLeakDetected`,
   **不静默脱敏**。静默脱敏会让标注员看到的样本与训练用样本不一致, 污染标注
   语义; 宁可拒推让人来决定。

去重靠 LS 原生 ``inner_id = session_id``（方案 §16 R7）—— 不引本地索引文件。
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterator

from label_studio.errors import C3ParseError, CredentialLeakDetected
from label_studio.scorecard import build_risk_hints, build_scorecard
from label_studio.settings import (
    CredentialScanSettings,
    LabelStudioSettings,
    ScorecardSettings,
    UploadSettings,
)

log = logging.getLogger(__name__)

#: C3 文件 stem 形态: ``<TXXX|EXXX>__<session_id>_refined``。
#: ``session_id`` 本身可能含 ``__``, 所以用贪婪匹配 + 尾部锚定。
STEM_PATTERN = re.compile(
    r"^(?P<task_id>[TE]\d{3})__(?P<session_id>.+?)_refined$"
)

#: 4 视图文件名后缀（``save_session_v2`` 产出）。
_SUFFIX_MESSAGES = ".messages.json"
_SUFFIX_OPENAI = ".openai.json"
_SUFFIX_QWENJINA = ".qwenjina.txt"
_SUFFIX_META = ".meta.json"


# ---------------------------------------------------------------------------
# 解析
# ---------------------------------------------------------------------------


def parse_stem(path_or_stem: Path | str) -> tuple[str, str]:
    """从 C3 文件名解析 ``(task_id, session_id)``。

    接受完整路径或裸 stem。``*.meta.json`` / ``*.messages.json`` 等 4 视图
    后缀会先剥掉再匹配。

    Raises:
        C3ParseError: 形态不符 —— 这是**数据问题**, 调用方按 task 跳过。
    """
    stem = Path(path_or_stem).name if isinstance(path_or_stem, (Path,)) else str(path_or_stem)
    for suffix in (_SUFFIX_META, _SUFFIX_MESSAGES, _SUFFIX_OPENAI):
        if stem.endswith(suffix):
            stem = stem[: -len(suffix)]
            break
    else:
        if stem.endswith(".qwenjina.txt"):
            stem = stem[: -len(".qwenjina.txt")]
    if stem.endswith(".txt"):
        stem = stem[: -len(".txt")]

    matched = STEM_PATTERN.match(stem)
    if not matched:
        raise C3ParseError(
            f"无法从 C3 文件名解析 task_id/session_id: {stem!r} "
            f"(期望形如 T001__<session_id>_refined)"
        )
    return matched.group("task_id"), matched.group("session_id")


# ---------------------------------------------------------------------------
# R11 凭据扫描
# ---------------------------------------------------------------------------


@dataclass
class ScanHit:
    """一次扫描命中记录。**只记录位置, 不记录命中的原文**（§16 R9）。"""

    view: str
    pattern_index: int
    char_offset: int

    def describe(self) -> str:
        return f"view={self.view} pattern#{self.pattern_index} offset={self.char_offset}"


def scan_for_credentials(
    payload: Any, *, view: str, settings: CredentialScanSettings
) -> list[ScanHit]:
    """在 payload 中扫描凭据形态。

    扫描**序列化后的文本**而非 Python 对象 —— tool_call 的 ``input`` 可能是
    任意嵌套 dict, 序列化后统一成一条可 grep 的文本流, 也不会漏掉被
    ``str()`` 掩盖的值。
    """
    if not settings.enabled:
        return []
    try:
        text = payload if isinstance(payload, str) else json.dumps(payload, ensure_ascii=False)
    except (TypeError, ValueError):
        # 不可序列化本身就是异常形态, 交给上游 C3 解析报错, 这里不重复拦。
        return []

    hits: list[ScanHit] = []
    for index, source in enumerate(settings.patterns):
        try:
            matched = re.search(source, text)
        except re.error:
            log.warning("scan_for_credentials: pattern#%d 非法正则, 跳过", index)
            continue
        if matched:
            hits.append(ScanHit(view=view, pattern_index=index, char_offset=matched.start()))
    return hits


# ---------------------------------------------------------------------------
# 导出
# ---------------------------------------------------------------------------


@dataclass
class ExportPlan:
    """一次批量推送的计划 + 逐条结果。不含任何凭据。"""

    tasks: list[dict[str, Any]] = field(default_factory=list)
    predictions: list[dict[str, Any]] = field(default_factory=list)
    skipped: list[tuple[str, str]] = field(default_factory=list)  # (stem, reason)
    rejected: list[tuple[str, list[ScanHit]]] = field(default_factory=list)

    @property
    def count(self) -> int:
        return len(self.tasks)

    def summary(self) -> str:
        return (
            f"待推送 {len(self.tasks)} 条 / 预标注 {len(self.predictions)} 条 / "
            f"跳过 {len(self.skipped)} 条 / 拒推 {len(self.rejected)} 条"
        )


def find_c3_files(refine_dir: Path) -> list[Path]:
    """列出 ``refine_dir`` 下所有 C3 的 ``*.meta.json``（以 meta 为入口找齐 4 视图）。"""
    directory = Path(refine_dir)
    if not directory.is_dir():
        return []
    return sorted(directory.glob(f"*{_SUFFIX_META}"))


def _load_json(path: Path, view: str) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise C3ParseError(f"C3 {view} 缺失: {path}") from exc
    except (OSError, json.JSONDecodeError, ValueError) as exc:
        raise C3ParseError(f"C3 {view} 解析失败 {path.name}: {exc}") from exc


def _flatten_criteria(meta: dict[str, Any]) -> list[dict[str, Any]]:
    """把 ``criterion_results.criteria`` 摊成 label_config 可逐条渲染的列表。

    字段名对齐 LS 逐条控件要显示的内容: 编号 / 判定 / 原因码 / 说明 / 证据。
    判定值统一**大写** —— 标注员看到的是 "PASS" / "FAIL", 与本项目内部的
    小写枚举区分开, 避免两边对同一字符串理解不一致。
    """
    evaluation = meta.get("criterion_results")
    criteria = (evaluation or {}).get("criteria") if isinstance(evaluation, dict) else None
    if not isinstance(criteria, list):
        return []
    rows: list[dict[str, Any]] = []
    for item in criteria:
        if not isinstance(item, dict) or not item.get("criterion_id"):
            continue
        verdict = str(item.get("verdict") or "").upper()
        rows.append(
            {
                "criterion_id": item.get("criterion_id"),
                "verdict": verdict,
                "reason_code": item.get("reason_code", ""),
                "message": item.get("message", ""),
                "evidence_ids": item.get("evidence_ids") or [],
            }
        )
    return rows


def build_task_data(
    meta_path: Path,
    *,
    scorecard_settings: ScorecardSettings | None = None,
) -> tuple[dict[str, Any], dict[str, Any] | None]:
    """C3 meta.json → ``(task.data, scorecard)``。

    4 视图中缺哪份都不阻断 —— ``qf_text`` 本就可能不存在（F1 前的旧产物
    或无 user 轮的 session）, ``messages`` 缺失才是硬错误。

    Raises:
        C3ParseError: meta 或 messages 缺失 / 损坏。
    """
    meta_path = Path(meta_path)
    task_id, stem_session = parse_stem(meta_path)
    meta = _load_json(meta_path, "meta")
    if not isinstance(meta, dict):
        raise C3ParseError(f"C3 meta 顶层不是对象: {meta_path.name}")

    base = meta_path.name[: -len(_SUFFIX_META)]
    messages_path = meta_path.with_name(base + _SUFFIX_MESSAGES)
    openai_path = meta_path.with_name(base + _SUFFIX_OPENAI)
    qwenjina_path = meta_path.with_name(base + _SUFFIX_QWENJINA)

    messages = _load_json(messages_path, "messages")
    openai = _load_json(openai_path, "openai") if openai_path.exists() else None
    qf_text = (
        qwenjina_path.read_text(encoding="utf-8")
        if qwenjina_path.exists() else None
    )

    session_id = str(meta.get("session_id") or stem_session)
    scorecard = build_scorecard(
        meta, task_id=task_id, session_id=session_id, settings=scorecard_settings
    )

    data: dict[str, Any] = {
        "task_id": task_id,
        "session_id": session_id,
        "messages": messages,
        "qf_text": qf_text or "",
        "metadata": meta,
        "training_value_score": meta.get("training_value_score"),
        "complexity_tier": meta.get("complexity_tier"),
        # label_config 的 perItem 控件直接绑这个扁平列表: LS 的 data path
        # 过滤语法 ($scorecard.dimensions[?(...)]) 在各版本行为不一致,
        # 扁平化后绑定是稳定的。
        "criteria": _flatten_criteria(meta),
    }
    if openai is not None:
        data["openai"] = openai
    if scorecard.get("enabled"):
        data["scorecard"] = scorecard
    return data, scorecard


def build_task(
    data: dict[str, Any], scorecard: dict[str, Any] | None = None
) -> dict[str, Any]:
    """包成 LS import 需要的 task 对象。

    ``inner_id = session_id`` —— LS 原生去重键（方案 §16 R7）。同一 session 重跑
    ``upload`` 时 LS 覆盖而非新增, 所以不引本地索引文件。
    """
    return {"data": data, "inner_id": data["session_id"]}


#: ``overall.confidence`` 枚举 → LS ``score`` 数值。LS 用它给预测排序, 不参与
#: 标注语义; 映射只为让"低置信度的建议"排在后面, 人先看高置信度的。
_CONFIDENCE_TO_SCORE = {"high": 0.9, "medium": 0.6, "low": 0.3}


def build_prediction(
    data: dict[str, Any], scorecard: dict[str, Any] | None
) -> dict[str, Any] | None:
    """ML 预标注（方案 §5.2）—— **只填风险提示, 绝不填 overall_decision**。

    ``overall_decision`` 留空强制人工选择: 自动 accept/reject 预判会把
    最该看的 hard 样本自动 reject 掉, 与方案 §3 自相矛盾。

    ``result`` 里的键**都不在 label_config 的控件名里** —— LS 只渲染被
    控件引用的键, 其余原样保存在 annotation 记录中供事后审计。
    """
    if scorecard is None or not scorecard.get("enabled"):
        return None
    overall = scorecard.get("overall") or {}
    return {
        "task": data["session_id"],
        "result": {
            "risk_hints": build_risk_hints(scorecard),
            "auto_suggestion": overall.get("suggested_decision"),
            "auto_confidence": overall.get("confidence"),
            "auto_derivation": overall.get("derivation"),
        },
        "model_version": f"scorecard/{scorecard.get('schema_version', 'scorecard.v1')}",
        "score": _CONFIDENCE_TO_SCORE.get(str(overall.get("confidence")), 0.5),
    }


def export_one(
    meta_path: Path,
    *,
    scorecard_settings: ScorecardSettings | None = None,
    scan_settings: CredentialScanSettings | None = None,
) -> tuple[dict[str, Any] | None, dict[str, Any] | None, list[ScanHit]]:
    """单条 C3 → ``(task, prediction, scan_hits)``。

    扫描命中时返回 ``(None, None, hits)`` —— **由调用方决定 fail-closed 还是
    skip**, 本函数不自行抛异常, 便于批量场景逐条记录。
    """
    data, scorecard = build_task_data(meta_path, scorecard_settings=scorecard_settings)
    scan = scan_settings or CredentialScanSettings()

    hits: list[ScanHit] = []
    for view in ("messages", "qf_text", "openai", "metadata"):
        if view in data and data[view]:
            hits.extend(scan_for_credentials(data[view], view=view, settings=scan))
    if hits:
        return None, None, hits

    return build_task(data, scorecard), build_prediction(data, scorecard), []


def export_batch(
    refine_dir: Path,
    *,
    settings: LabelStudioSettings | None = None,
    task_id: str | None = None,
    min_score: float | None = None,
    complexity_tier: str | None = None,
    include_predictions: bool | None = None,
) -> ExportPlan:
    """扫目录 → 逐条导出 → 过滤 → 汇总成 :class:`ExportPlan`。

    过滤顺序：``skip_task_ids`` → ``tier`` → ``min_score`` → 凭据扫描。
    凭据扫描放在**最后**是有意的：过滤是"这次不推"，扫描是"这个样本有问题"，
    两者的处置完全不同（前者静默跳过即可，后者必须留痕）。
    """
    settings = settings or LabelStudioSettings()
    upload: UploadSettings = settings.upload
    plan = ExportPlan()
    predictions_on = (
        upload.include_predictions if include_predictions is None else include_predictions
    )
    min_score = upload.filter_min_training_value_score if min_score is None else min_score
    tiers = settings.tier_filter()

    for meta_path in find_c3_files(refine_dir):
        stem = meta_path.name[: -len(_SUFFIX_META)]
        try:
            parsed_task_id, _ = parse_stem(meta_path)
        except C3ParseError as exc:
            plan.skipped.append((stem, str(exc)))
            continue

        if task_id and parsed_task_id != task_id:
            continue
        if parsed_task_id in upload.skip_task_ids:
            plan.skipped.append((stem, f"task_id 在 skip_task_ids 中: {parsed_task_id}"))
            continue

        try:
            data, scorecard = build_task_data(
                meta_path, scorecard_settings=settings.scorecard
            )
        except C3ParseError as exc:
            plan.skipped.append((stem, str(exc)))
            continue

        tier = data.get("complexity_tier")
        if tiers and tier not in tiers:
            plan.skipped.append((stem, f"complexity_tier={tier} 不在 filter 中"))
            continue
        score = data.get("training_value_score")
        if isinstance(score, (int, float)) and score < min_score:
            plan.skipped.append((stem, f"training_value_score={score} < {min_score}"))
            continue

        hits: list[ScanHit] = []
        for view in ("messages", "qf_text", "openai", "metadata"):
            if data.get(view):
                hits.extend(
                    scan_for_credentials(
                        data[view], view=view, settings=settings.credential_scan
                    )
                )
        if hits:
            plan.rejected.append((stem, hits))
            log.error(
                "export_batch: %s 命中凭据模式, fail-closed 拒推: %s",
                stem, [h.describe() for h in hits],
            )
            continue

        plan.tasks.append(build_task(data, scorecard))
        if predictions_on:
            prediction = build_prediction(data, scorecard)
            if prediction:
                plan.predictions.append(prediction)

    return plan


def push_single_c3(
    meta_path: Path,
    *,
    settings: LabelStudioSettings,
    project_id: int,
    include_prediction: bool = True,
    client_factory: Callable[[], Any] | None = None,
) -> dict[str, Any]:
    """推送单条 C3（orchestration hook 与 ``--task-id`` 调试共用）。

    Args:
        client_factory: 注入 client 的工厂（测试用）；None 时按 settings 建。

    Returns:
        ``{"pushed": int, "rejected": bool, "reason": str|None, "task_id", "session_id"}``

    Raises:
        CredentialLeakDetected: 扫描命中（fail-closed）。
        C3ParseError: C3 缺失 / 形态不符。
    """
    task, prediction, hits = export_one(
        meta_path,
        scorecard_settings=settings.scorecard,
        scan_settings=settings.credential_scan,
    )
    task_id, _ = parse_stem(meta_path)
    if hits:
        # fail-closed: 静默脱敏会污染标注语义, 拒推并留痕让人决定。
        raise CredentialLeakDetected(
            f"C3 {meta_path.name} 命中凭据模式, 已拒推: "
            f"{[h.describe() for h in hits]}"
        )
    if task is None:
        raise C3ParseError(f"C3 导出为空: {meta_path.name}")

    if client_factory is None:
        from label_studio.client import build_client as _build_client

        client = _build_client(settings)
    else:
        client = client_factory()

    pushed = client.import_tasks(project_id, [task])
    if include_prediction and prediction:
        client.import_predictions(project_id, [prediction])
    return {
        "pushed": pushed,
        "rejected": False,
        "reason": None,
        "task_id": task_id,
        "session_id": task["inner_id"],
    }


def push_batch(
    plan: ExportPlan,
    *,
    settings: LabelStudioSettings,
    project_id: int,
    client_factory: Callable[[], Any] | None = None,
    on_progress: Callable[[int, int], None] | None = None,
) -> dict[str, Any]:
    """按 ``upload.batch_size`` 分批推送。``plan`` 为空时直接返回。"""
    if not plan.tasks:
        return {"tasks_pushed": 0, "predictions_pushed": 0, "batches": 0}

    if client_factory is None:
        from label_studio.client import build_client as _build_client

        client = _build_client(settings)
    else:
        client = client_factory()

    size = max(1, settings.upload.batch_size)
    tasks_pushed = 0
    predictions_pushed = 0
    batches = 0
    # predictions 按 inner_id 对齐到 task 批次 —— LS 要求 task 已存在。
    pred_by_id = {p["task"]: p for p in plan.predictions}

    for start in range(0, len(plan.tasks), size):
        batch = plan.tasks[start : start + size]
        tasks_pushed += client.import_tasks(project_id, batch)
        preds = [
            pred_by_id[t["inner_id"]]
            for t in batch
            if t.get("inner_id") in pred_by_id
        ]
        if preds:
            predictions_pushed += client.import_predictions(project_id, preds)
        batches += 1
        if on_progress:
            on_progress(min(start + size, len(plan.tasks)), len(plan.tasks))
    return {
        "tasks_pushed": tasks_pushed,
        "predictions_pushed": predictions_pushed,
        "batches": batches,
    }


def iter_task_ids(refine_dir: Path) -> Iterator[str]:
    """遍历目录下出现的 task_id（去重、保序）—— 供 CLI 报告。"""
    seen: set[str] = set()
    for meta_path in find_c3_files(refine_dir):
        try:
            parsed, _ = parse_stem(meta_path)
        except C3ParseError:
            continue
        if parsed not in seen:
            seen.add(parsed)
            yield parsed


__all__ = [
    "ExportPlan",
    "ScanHit",
    "STEM_PATTERN",
    "build_prediction",
    "build_task",
    "build_task_data",
    "export_batch",
    "export_one",
    "find_c3_files",
    "iter_task_ids",
    "parse_stem",
    "push_batch",
    "push_single_c3",
    "scan_for_credentials",
]
