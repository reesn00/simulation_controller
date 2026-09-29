"""orchestration.criterion_source — 把 simulate 端的 Criterion 验证结果读出来.

背景 (Label Studio 方案 F2)
---------------------------
评分卡 L0 ``criterion_coverage`` 是"明确的指令评分"的主载体 —— ``criterion``
就是用户指令的可验证条目。``CriterionResult`` 早就由 simulate 端产出
(``ValidationReport.criteria``), 但它只落在 ``output/runs/<run_id>/``:

* ``trajectory_archiver`` 是**纯字节拷贝** (不注入 run 元数据)
* ``gdr/parsers/`` 对 ``validation`` / ``criterion`` **零引用**

所以 C1 → C2 → C3 全链路都看不到 criterion 结果, 评分卡只能从 L1 起。

本模块提供 **只读** 侧的读取, 注入动作在 ``run_etl_once`` 里做 (etl 阶段),
这样 gdr / simulate_serve 保持零修改 (方案 §13.3 不变量)。

设计要点
--------
* **fail-soft**: run 文件缺失 / 损坏一律返回 ``None`` + warning, 绝不让 etl
  阶段失败。criterion 缺失只是让评分卡 L0 标记为 ``source: missing``。
* 只取**最后一轮** ``ValidationReport`` —— 早期轮次是追问前的中间态, 人工
  核对应以最终判定为准 (轮次数记在 ``rounds`` 里供追溯)。
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

#: 注入 ``session.metadata`` 的键名 (C3 meta.json 里的字段)。
CRITERION_METADATA_KEY = "criterion_results"

#: 同一位置的第二个键 (C3 meta.json 顶层字段)。
#:
#: gdr 判低分的理由。**这是"为什么这条样本分数低"的唯一解释** —— 少了它,
#: 标注员看到 L0=0 / 整体分低只会当成正常波动, 会照常标一条"可用", 等于
#: 把 gdr 拒收过的样本又标回训练集。结构合格但评分低的轨迹按 CLAUDE.md
#: "数据保留原则" 仍然推 LS 供人工复核, 所以这个字段是复核的前提, 不是装饰。
AUDIT_METADATA_KEY = "audit_reason"

#: ``ValidationReport`` → ``ValidationResult.verdict`` 聚合顺序 (fail-closed):
#: 与 ``simulate_serve.domain.validation.aggregate_results`` 保持一致。
_VERDICT_SEVERITY = ("fail", "error", "inconclusive", "pass")


def _validate_runs_dir(runs_dir: Path | None) -> Path | None:
    if runs_dir is None:
        return None
    path = Path(runs_dir)
    return path if path.is_dir() else None


def _read_reports(run_dir: Path) -> list[dict[str, Any]]:
    """读该 run 的全部 ValidationReport, 优先 append-only 的 validations.jsonl。

    ``run.json`` 作为兜底 (``validation_rounds`` 字段), 兼容只写了 run.json
    的历史 run。
    """
    jsonl = run_dir / "validations.jsonl"
    if jsonl.is_file():
        reports: list[dict[str, Any]] = []
        try:
            for line in jsonl.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if not line:
                    continue
                item = json.loads(line)
                if isinstance(item, dict):
                    reports.append(item)
        except (OSError, json.JSONDecodeError) as exc:
            log.warning("criterion_source: 读取 %s 失败: %s", jsonl, exc)
            reports = []
        if reports:
            return reports

    run_json = run_dir / "run.json"
    if run_json.is_file():
        try:
            raw = json.loads(run_json.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            log.warning("criterion_source: 读取 %s 失败: %s", run_json, exc)
            return []
        rounds = raw.get("validation_rounds") if isinstance(raw, dict) else None
        if isinstance(rounds, list):
            return [r for r in rounds if isinstance(r, dict)]
    return []


def _final_verdict(report: dict[str, Any]) -> str:
    """取报告判定; 缺失时按 criteria 严重度回退推导。"""
    verdict = report.get("verdict")
    if isinstance(verdict, str) and verdict:
        return verdict
    seen = {
        str(c.get("verdict"))
        for c in (report.get("criteria") or [])
        if isinstance(c, dict) and c.get("verdict")
    }
    for candidate in _VERDICT_SEVERITY:
        if candidate in seen:
            return candidate
    return "pass"


def _normalize_criterion(raw: Any) -> dict[str, Any] | None:
    """只保留评分卡需要的字段, 并归一成稳定类型。"""
    if not isinstance(raw, dict):
        return None
    criterion_id = raw.get("criterion_id")
    verdict = raw.get("verdict")
    if not criterion_id or not verdict:
        return None
    evidence = raw.get("evidence_ids") or ()
    return {
        "criterion_id": str(criterion_id),
        "verdict": str(verdict),
        "reason_code": str(raw.get("reason_code") or ""),
        "message": str(raw.get("message") or ""),
        "evidence_ids": [str(e) for e in evidence] if isinstance(evidence, (list, tuple)) else [],
        "retryable": bool(raw.get("retryable", False)),
    }


def _find_run_dir_by_session(runs_dir: Path, session_id: str) -> Path | None:
    """run_id 未知时的兜底: 扫 runs_dir 找 ``remote_session_id`` 匹配的 run。"""
    for run_json in sorted(runs_dir.glob("*/run.json")):
        try:
            raw = json.loads(run_json.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if isinstance(raw, dict) and raw.get("remote_session_id") == session_id:
            return run_json.parent
    return None


def load_criterion_evaluation(
    *,
    runs_dir: Path | None,
    run_id: str | None = None,
    session_id: str = "",
) -> dict[str, Any] | None:
    """读一个 session 的最终 Criterion 判定。

    Args:
        runs_dir: ``output/runs`` 目录; None 或不存在时直接返回 None。
        run_id: 已知 run_id 时直接定位 ``<runs_dir>/<run_id>``，省掉全量扫描。
        session_id: run_id 缺失时的兜底匹配键, 同时写入返回值供审计。

    Returns:
        可直接写进 ``session.metadata`` 的 dict::

            {
              "run_id": "run-xxx",
              "rounds": 3,
              "final_verdict": "pass | fail | inconclusive | error",
              "missing_items": ["..."],
              "criteria": [
                {"criterion_id", "verdict", "reason_code", "message",
                 "evidence_ids", "retryable"}, ...
              ],
            }

        无数据时返回 ``None``（调用方跳过注入）。
    """
    base = _validate_runs_dir(runs_dir)
    if base is None:
        return None

    run_dir: Path | None = None
    if run_id:
        # 显式给了 run_id 就严格按它定位: 目录不存在时返回 None, 不静默回退到
        # 扫描 —— 回退会掩盖"run_id 与实际产物不匹配"这类真问题。
        candidate = base / run_id
        run_dir = candidate if candidate.is_dir() else None
    elif session_id:
        run_dir = _find_run_dir_by_session(base, session_id)
    if run_dir is None:
        log.debug(
            "criterion_source: 未找到 run (run_id=%r, session_id=%r); 跳过注入",
            run_id, session_id,
        )
        return None

    reports = _read_reports(run_dir)
    if not reports:
        log.debug("criterion_source: %s 无 ValidationReport; 跳过注入", run_dir)
        return None

    # 最后一轮 = 最终判定 (早期轮次是追问前的中间态)
    final = reports[-1]
    criteria = [
        normalized
        for normalized in (_normalize_criterion(c) for c in (final.get("criteria") or []))
        if normalized is not None
    ]
    missing = final.get("missing_items") or []
    return {
        "run_id": run_dir.name,
        "rounds": len(reports),
        "final_verdict": _final_verdict(final),
        "missing_items": [str(m) for m in missing] if isinstance(missing, (list, tuple)) else [],
        "criteria": criteria,
    }


def inject_criterion_evaluation(session: Any, evaluation: dict[str, Any] | None) -> bool:
    """把评分写进 ``session.metadata``。返回是否真的注入。

    ``evaluation`` 为 None 时**不清空**已有值 —— 上游若已注入过（例如从 C2
    恢复的 session），不应被一次读取失败抹掉。
    """
    if not evaluation:
        return False
    meta = session.metadata if session.metadata is not None else {}
    meta[CRITERION_METADATA_KEY] = evaluation
    session.metadata = meta
    log.info(
        "criterion_inject: session=%s run=%s rounds=%s verdict=%s criteria=%d",
        getattr(session, "session_id", "?"),
        evaluation.get("run_id"),
        evaluation.get("rounds"),
        evaluation.get("final_verdict"),
        len(evaluation.get("criteria") or []),
    )
    return True


def inject_audit_reason(session: Any, audit_reason: str | None) -> bool:
    """把 gdr 的低分理由写进 ``session.metadata``。返回是否真的注入。

    ``audit_reason`` 为 None / 空串 / 纯空白时**返回 False 且不写键** ——
    正常通过的 session 不该留一个空的 ``audit_reason`` 键, 评分卡和
    label_config 都靠「键是否存在」判断这条要不要打低分标记, 空串会让两者
    都误判。空白一并 strip 是为了与 :func:`label_studio.scorecard.build_audit_marker`
    的口径一致 —— 两边对 "什么算没有理由" 判断不同, metadata 里就会留下一个
    评分卡看不见的脏键。

    与 :func:`inject_criterion_evaluation` 不同, 这里 ``None`` 的含义是
    "没有拒收理由"而非"读取失败", 所以是**真的不写**而不是保留原值。
    """
    if not audit_reason:
        return False
    reason = audit_reason.strip() if isinstance(audit_reason, str) else audit_reason
    if not reason:
        return False
    meta = session.metadata if session.metadata is not None else {}
    meta[AUDIT_METADATA_KEY] = reason
    session.metadata = meta
    log.info(
        "audit_inject: session=%s reason=%s",
        getattr(session, "session_id", "?"),
        audit_reason,
    )
    return True


__all__ = [
    "AUDIT_METADATA_KEY",
    "CRITERION_METADATA_KEY",
    "inject_audit_reason",
    "load_criterion_evaluation",
    "inject_criterion_evaluation",
]
