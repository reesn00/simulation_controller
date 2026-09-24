"""etl.parsers — C2 契约入口 + 训练集准入门控.

新架构 ``simulation server → gdr → etl`` 下，etl 不再消费原始 trajectory，
而是消费 gdr 产出的 C2 refined Session（详见 ``docs/contracts/C2-refined-session.md``）。

本包是 etl 对 C2 契约的**唯一入口**：
    1. ``load_refined_session`` — 加载 C2 refined Session 为 pydantic Session
    2. ``gate_then_load`` — 准入门控 (方案 etl-prune-frontload.md §5.2);
       读 metadata.trajectory_compare / trajectory_free 做训练集准入决策
    3. ``append_scoring_reject_fallback`` — etl 入口发现 reject 时的兜底
       audit 落盘 (gdr 已主动 reject 的 C2 理论上不该到这里, 兜底防漏)

约定：未来若 C2 schema 演进（``schema_version`` 升级），本包内部处理版本转换，
其他 etl 代码不动。
"""
from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from gdr.domain.schema import Session

log = logging.getLogger(__name__)


def load_refined_session(path: Path) -> Session:
    """把 gdr 产出的 C2 refined Session JSON 解析为 Session 对象.

    校验 ``schema_version`` 是 ``refined_session.v1``；后续 schema 升级时
    在此处做版本转换（旧版本 → 新版本），调用方零改动。

    Raises:
        ValueError: schema_version 不匹配时.
    """
    with path.open(encoding="utf-8") as f:
        raw = json.load(f)
    version = raw.get("schema_version")
    if version != "refined_session.v1":
        raise ValueError(
            f"unsupported refined_session schema_version: {version!r}; "
            f"expected 'refined_session.v1'"
        )
    return Session.model_validate(raw)


# ---------------------------------------------------------------------------
# 训练集准入门控 (方案 etl-prune-frontload.md §5.2)
# ---------------------------------------------------------------------------

_SCORE_REJECT_DECISION = "reject"
_COMPARE_OVERALL_FAIL = "fail"


def _current_settings_for_etl() -> Any:
    """从 gdr Settings 取配置; 失败回退 Namespace 默认 (etl 解耦测试)."""
    try:
        from gdr.config.settings import Settings
        return Settings()
    except Exception:
        from types import SimpleNamespace as _NS
        return _NS(
            scoring_reject_output_path="./audit/scoring_reject.jsonl",
            scoring_reject_audit_enabled=True,
        )


def gate_then_load(c2_path: Path) -> Session | None:
    """读 C2 refined Session, 应用门控决策 (etl 入口准入门控).

    三类决策:
        1. ``reject`` (红线违规 / 总分 < 4 / 子分门槛未达):
           返回 None; 调用方 (render_to_4_views) 走 audit/scoring_reject.jsonl 旁路.
        2. ``accept`` + ``trajectory_compare.overall == "fail"``:
           返回 Session 且 metadata["compare_warn"] = True,
           meta.json 同步写入 compare_diff_summary 供训练侧按需过滤.
        3. ``accept`` (默认):
           返回 Session 原样.

    注意: gdr 主流程已主动 reject 的 C2 不应到这里 (runner step 23 已
    _append_scoring_reject_queue 并 return None), 此处是兜底拦截.

    Args:
        c2_path: C2 refined Session JSON 路径.

    Returns:
        通过门控的 Session; None 表示应被 redirect 到 audit 旁路.
    """
    session = load_refined_session(c2_path)
    meta = session.metadata or {}

    free = meta.get("trajectory_free") or {}
    decision = free.get("decision")

    if decision == _SCORE_REJECT_DECISION:
        log.warning(
            "etl gate: scoring_reject detected in C2 %s (gdr should have "
            "intercepted); redirecting to audit",
            c2_path.name,
        )
        append_scoring_reject_fallback(session, c2_path)
        return None

    compare = meta.get("trajectory_compare") or {}
    overall = compare.get("overall")
    if overall == _COMPARE_OVERALL_FAIL and decision == "accept":
        meta["compare_warn"] = True
        meta["compare_diff_summary"] = (
            compare.get("instruction_adherence", {}).get("diff_summary", [])
        )
        log.info(
            "etl gate: trajectory_compare.overall=fail for %s; "
            "marking compare_warn, proceeding to 4-views",
            c2_path.name,
        )

    return session


def append_scoring_reject_fallback(session: Session, c2_path: Path) -> None:
    """etl 入口 reject 兜底 audit 落盘.

    与 gdr.pipeline.runner._append_scoring_reject_queue 互为兜底:
    gdr 主动 reject 的 session 已在 step 23 落 audit 并 return None,
    不会写到 C2; 若 C2 中仍出现 reject (gdr 配置被改 / 手动生成),
    etl 入口 gate_then_load 会调此函数补一份 audit 记录, 防止
    "gdr 拦截 + etl 兜底" 双保险缺失.

    Args:
        session: 已被 gate_then_load 判定 reject 的 session.
        c2_path: C2 文件路径 (供 source_file 字段).
    """
    cfg = _current_settings_for_etl()
    if not getattr(cfg, "scoring_reject_audit_enabled", True):
        return
    meta = session.metadata or {}
    free = meta.get("trajectory_free") or {}
    redline = free.get("redline") or {}
    absolute_quality = free.get("absolute_quality") or {}
    try:
        record = {
            "session_id": session.session_id,
            "source_file": str(c2_path),
            "scoring_reject": {
                "decision": free.get("decision"),
                "redline_violation": redline.get("violation", False),
                "redline_labels": redline.get("labels", []),
                "absolute_quality_score": absolute_quality.get("score"),
                "absolute_quality_subscores": absolute_quality.get("subscores", {}),
                "absolute_quality_fail_reasons": absolute_quality.get("fail_reasons", []),
                "origin": "etl_fallback",  # 区分 gdr 主动 reject
            },
            "session": session.model_dump(mode="json"),
        }
        path = Path(getattr(cfg, "scoring_reject_output_path", "./audit/scoring_reject.jsonl"))
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
        log.warning(
            "scoring-reject (etl fallback) appended: %s (session %s, "
            "redline_violation=%s, score=%s)",
            path, session.session_id,
            redline.get("violation", False),
            absolute_quality.get("score"),
        )
    except Exception as e:
        log.warning("failed to append scoring_reject (etl fallback): %s", e)


__all__ = [
    "load_refined_session",
    "gate_then_load",
    "append_scoring_reject_fallback",
]
