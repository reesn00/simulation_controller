"""orchestration.workers.etl_worker: ST-3 etl 阶段单文件处理入口.

新架构下 worker 是无状态模块函数::

    result = run_etl_once(
        *, c2_path, etl_outputs_dir, task_id, session_id,
    )

不再有 ``EtlWorker`` 类 / ``BaseWorker`` 抽象 / ``run_forever`` 主循环.
调度 / 重试 / SQLite 写全部归 ST-5 PipelineExecutor.

执行链::

    C2 refined Session 单文件 (JSON)
      -> load_refined_session(c2_path) 校验 schema_version
      -> apply_render_chain(session)  补 metadata.openai_messages / tools / qf_text
      -> save_session_v2(session, base_path) 拆 4 视图
      -> EtlOutputs(messages_path, openai_path, qwenjina_path|None, meta_path, ...)
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from gdr.domain import save_session_v2

from etl.parsers import load_refined_session
from etl.writers import RenderChainError, apply_render_chain

from orchestration.criterion_source import (
    inject_audit_reason,
    inject_criterion_evaluation,
    load_criterion_evaluation,
)
from orchestration.fail_evaluator import (
    evaluate_failed_run,
    inject_fail_evaluation,
)
from orchestration.workers.base_worker import _output_filename

_log = logging.getLogger(__name__)


# ----------------------------------------------------------------------
# 数据契约 (契约 §3.4)
# ----------------------------------------------------------------------


@dataclass(frozen=True)
class EtlOutputs:
    """``run_etl_once`` 成功返回值 (C3 4 视图路径集合)."""

    messages_path: Path
    openai_path: Path
    qwenjina_path: Path | None
    meta_path: Path
    task_id: str
    session_id: str
    duration_seconds: float

    @classmethod
    def from_session_v2(
        cls,
        session_outputs: Any,
        *,
        task_id: str,
        session_id: str,
        duration_seconds: float,
    ) -> "EtlOutputs":
        """把 ``gdr.domain.SessionOutputs`` 转换为 ``EtlOutputs``.

        ``session_outputs`` 暴露 ``messages``/``openai``/``qwenjina``/
        ``meta`` 四个 ``Path`` 属性 (``qwenjina`` 可为 None) ——
        与 gdr 端 ``save_session_v2`` 返回类型一致. 业务字段
        ``task_id``/``session_id``/``duration_seconds`` 由调用方传入.
        """
        return cls(
            messages_path=session_outputs.messages,
            openai_path=session_outputs.openai,
            qwenjina_path=session_outputs.qwenjina,
            meta_path=session_outputs.meta,
            task_id=task_id,
            session_id=session_id,
            duration_seconds=duration_seconds,
        )


# ----------------------------------------------------------------------
# 异常 (契约 §3.4)
# ----------------------------------------------------------------------


class EtlNonRetryableError(Exception):
    """C2 文件 schema 不匹配 / load_refined_session 失败 — 不应重试."""


# ----------------------------------------------------------------------
# 模块函数 (契约 §3.4)
# ----------------------------------------------------------------------


def run_etl_once(
    *,
    c2_path: Path,
    etl_outputs_dir: Path,
    task_id: str,
    session_id: str,
    attempt: int = 0,
    runs_dir: Path | None = None,
    run_id: str | None = None,
    src_path: Path | None = None,
    gdr_settings: Any = None,
    audit_reason: str | None = None,
) -> EtlOutputs:
    """单个 C2 refined Session JSON → 4 视图文件.

    流程 (契约 §3.4):
      1. ``load_refined_session(c2_path)`` 读 + 校验 schema_version
      2. ``apply_render_chain(session)`` 渲染 qf_text, 补 ``openai_messages`` /
         ``tools`` 到 ``session.metadata`` (F1, 2026-09-28; gdr step 22 已完成
         路径泛化 / system 裁剪 / tools 裁剪, etl 不重跑)
      3. ``save_session_v2(session, base_path)`` 写 4 视图
      4. 返 ``EtlOutputs(messages_path=..., openai_path=..., ...)``

    Parameters
    ----------
    c2_path:
        gdr 阶段产出的 C2 refined Session 单文件路径.
    etl_outputs_dir:
        C3 4 视图文件输出目录.
    task_id:
        Catalog 内的 task_id, 用于产物文件名前缀 + ``EtlOutputs.task_id``.
    session_id:
        远端 session_id, 用于产物文件名前缀 + ``EtlOutputs.session_id``.
    attempt:
        ``_safe_run_etl`` 重试下标, 0..max_retry_etl. 默认 0 (旧调用兼容).
    runs_dir:
        ``output/runs`` 目录 (F2)。提供时把 simulate 端的 Criterion 验证结果
        注入 ``session.metadata["criterion_results"]``；``None`` 时跳过注入
        (旧调用兼容)。注入 fail-soft —— 读不到不影响主流程。
    run_id:
        已知 run_id 时省掉按 session_id 的全量扫描 (F2)；``None`` 时回退扫描。
    src_path:
        C1 trajectory 路径。提供时, 若本 run 验证未通过, 读 agent 最终回复做
        LLM 失败归因, 注入 ``session.metadata["fail_evaluation"]``。
        ``None`` 时跳过归因 (旧调用兼容)。
    gdr_settings:
        提供 LLM 端点/模型/超时。``None`` 时跳过失败归因 —— 评价是增强信息,
        不是前置条件。
    audit_reason:
        gdr 判低分的理由 (``judge_discard``)。非 ``None`` 时注入
        ``session.metadata["audit_reason"]``, 评分卡据此打低分标签, 标注员
        不会把被拒收的样本误当成正常样本去标。``None`` 表示正常通过。

    Returns
    -------
    ``EtlOutputs`` with:
      - ``messages_path``: ``<stem>.messages.json``
      - ``openai_path``: ``<stem>.openai.json``
      - ``qwenjina_path``: ``<stem>.qwenjina.txt`` 或 ``None`` (qf_text 缺失)
      - ``meta_path``: ``<stem>.meta.json``
      - ``task_id`` / ``session_id``: 原样回传
      - ``duration_seconds``: 端到端耗时 (秒, float)

    Raises
    ------
    EtlNonRetryableError:
        c2_path 缺失 / ``load_refined_session`` 失败 (schema 不匹配 /
        JSON 损坏) / ``session_id`` 与 C2 内 session_id 不一致 /
        ``apply_render_chain`` 失败 (模板缺失 / 渲染异常).
        调用方应直接标 dead.
    Exception:
        ``save_session_v2`` 写盘失败 (IO 抖动) 等未显式分类异常,
        由 PipelineExecutor 兜底 (本函数不重新分类 ——
        etl 阶段重试成本极低, PipelineExecutor 直接走 attempts_etl 计数).
    """
    c2_path = Path(c2_path)
    etl_outputs_dir = Path(etl_outputs_dir)

    t0 = time.perf_counter()

    session: Any = None

    # --- 1. C2 文件存在性 + schema 校验 ---
    if not c2_path.exists():
        raise EtlNonRetryableError(
            f"run_etl_once: C2 refined session missing for task {task_id}: {c2_path}"
        )

    # schema 不匹配 / JSON 损坏 → 永久性错误, 重试不会改变结果.
    try:
        session = load_refined_session(c2_path)
    except (ValueError, UnicodeDecodeError, FileNotFoundError) as exc:
        raise EtlNonRetryableError(
            f"run_etl_once: load_refined_session error "
            f"({type(exc).__name__}): {exc}"
        ) from exc

    # ---- session_id 跨层校验 (C2 内 session_id 必须等于入参) ----
    # 不一致 = 数据被串错, 直接 dead, 不重试.
    session_file_id = getattr(session, "session_id", None)
    if session_file_id != session_id:
        raise EtlNonRetryableError(
            f"run_etl_once: session_id mismatch: arg={session_id!r} "
            f"c2={session_file_id!r} for task {task_id}"
        )
    # ---- Criterion 注入 (F2, 2026-09-28) ----
    # simulate 端的 ValidationReport 只落在 output/runs/, C1/C2 都不带,
    # 这里在写 C3 之前补进 session.metadata。fail-soft: 读不到就跳过,
    # 评分卡对应维度标 source=missing, 不阻断 etl.
    criterion_evaluation = load_criterion_evaluation(
        runs_dir=runs_dir,
        run_id=run_id,
        session_id=session_id,
    )
    inject_criterion_evaluation(session, criterion_evaluation)
    # ---- 失败归因注入 (2026-09-29) ----
    # 验证不通过的轨迹不进死信 (task_pipeline._SIMULATE_FAIL_STATES), 但
    # 「失败在哪」不能丢: 把验证原因 + agent 回复发给 LLM 做定性归因, 分数
    # 恒为 0。fail-soft + 有条件: 仅 final_verdict != pass 且拿到 LLM 配置
    # 才调, 否则静默跳过, 绝不让评价阻断 etl.
    if criterion_evaluation and gdr_settings is not None:
        try:
            fail_evaluation = evaluate_failed_run(
                run_id=run_id or session_id,
                trajectory_path=src_path,
                criterion_evaluation=criterion_evaluation,
                gdr_settings=gdr_settings,
            )
        except Exception as exc:  # 双保险: evaluate_failed_run 自身已 fail-soft
            _log.warning(
                "etl_worker: 失败归因异常 (跳过注入) task=%s: %s: %s",
                task_id, type(exc).__name__, exc,
            )
            fail_evaluation = None
        inject_fail_evaluation(session, fail_evaluation)
    # ---- 低分理由注入 (2026-09-29) ----
    # gdr 判 judge_discard 的 session 结构合格、只因评分低被拒收, 按 CLAUDE.md
    # "数据保留原则" 仍然推 LS 供人工复核。带上 audit_reason, 评分卡和
    # label_config 才知道要给这条打低分标记 —— 否则标注员看到一堆 0 分却
    # 不知道为什么, 会照常标"可用", 等于把 gdr 拒收过的样本又标回训练集。
    # 正常通过时 audit_reason 是 None, 不写键。
    inject_audit_reason(session, audit_reason)
    # ---- 渲染链 (F1, 2026-09-28) ----
    # gdr step 22 已做路径泛化 / system 裁剪 / tools 裁剪; etl 只需补
    # etl 专属的 qf_text 渲染, 把 openai_messages / tools / qf_text
    # 写进 session.metadata —— save_session_v2 靠这三个键决定
    # openai.json 内容与 qwenjina.txt 是否落盘.
    try:
        apply_render_chain(session)
    except RenderChainError as exc:
        # 模板缺失 / 渲染异常 = 环境问题, 重试不会变好.
        raise EtlNonRetryableError(
            f"run_etl_once: render_chain failed for task {task_id}: {exc}"
        ) from exc

    # ---- Session -> 4 视图 ----
    # base_path 是无扩展名 stem; save_session_v2 会自动追加
    # ``.messages.json`` / ``.openai.json`` / ``.qwenjina.txt`` /
    # ``.meta.json`` 4 个尾缀. stem = ``<safe_task_id>__<safe_session_id>``
    # (与 gdr 写 C2 时同一前缀, 便于 C2 与 C3 归并到同一 prefix).
    base_path = etl_outputs_dir / _output_filename(
        task_id, session_id, suffix=""
    )
    base_path.parent.mkdir(parents=True, exist_ok=True)
    duration_so_far = time.perf_counter() - t0
    session_outputs = save_session_v2(session, base_path)
    outputs = EtlOutputs.from_session_v2(
        session_outputs,
        task_id=task_id,
        session_id=session_id,
        duration_seconds=duration_so_far,
    )
    return outputs