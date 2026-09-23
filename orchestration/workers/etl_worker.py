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

from orchestration.observability.langfuse_config import LangfuseConfig
from orchestration.workers.base_worker import _output_filename

# 工厂 + helpers 全部走 simulate_serve.observability.langfuse_client;
# 不要在本地重新实现 stage_trace/step_span/snapshot/_to_jsonable 等.
from simulate_serve.observability.langfuse_client import (
    _to_jsonable,
    get_client,
    snapshot,
    stage_trace,
    step_span,
)

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


def _capture_save_payload(out: EtlOutputs | None) -> dict[str, Any] | None:
    """构造 outer / save 子 span 的 ``output`` 字典.

    qwenjina_path 为 None 时显式传 None (不写库 vs 缺省 failed 0 字节 不可分).
    bytes 是 size stat, 落盘后才有; 失败时也是 None.
    """
    if out is None:
        return None

    def _size(p: Path | None) -> int | None:
        if p is None:
            return None
        try:
            return p.stat().st_size if p.exists() else None
        except Exception:
            return None

    return {
        "messages": str(out.messages_path),
        "openai": str(out.openai_path),
        "qwenjina": str(out.qwenjina_path) if out.qwenjina_path else None,
        "meta": str(out.meta_path),
        "messages_bytes": _size(out.messages_path),
        "openai_bytes": _size(out.openai_path),
        "qwenjina_bytes": _size(out.qwenjina_path),
        "meta_bytes": _size(out.meta_path),
    }


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
    langfuse_cfg: LangfuseConfig | None = None,
) -> EtlOutputs:
    """单个 C2 refined Session JSON → 4 视图文件.

    流程 (契约 §3.4):
      1. ``load_refined_session(c2_path)`` 读 + 校验 schema_version
      2. ``save_session_v2(session, base_path)`` 写 4 视图
      3. 返 ``EtlOutputs(messages_path=..., openai_path=..., ...)``

    Parameters
    ----------
    c2_path:
        gdr 阶段产出的 C2 refined Session 单文件路径.
    etl_outputs_dir:
        C3 4 视图文件输出目录.
    task_id:
        Catalog 内的 task_id, 用于产物文件名前缀 + ``EtlOutputs.task_id``.
    session_id:
        远端 session_id, 用于产物文件名前缀 + ``EtlOutputs.session_id``,
        同时跨进程串 Langfuse 三阶段 trace.
    attempt:
        ``_safe_run_etl`` 重试下标, 0..max_retry_etl. 默认 0 (旧调用兼容).
    langfuse_cfg:
        ``orchestration.observability.langfuse_config.LangfuseConfig``;
        ``None`` 时退化为不创建任何 span (旧调用兼容).

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
        JSON 损坏) / ``session_id`` 与 C2 内 session_id 不一致.
        调用方应直接标 dead.
    Exception:
        ``save_session_v2`` 写盘失败 (IO 抖动) 等未显式分类异常,
        由 PipelineExecutor 兜底 (本函数不重新分类 ——
        etl 阶段重试成本极低, PipelineExecutor 直接走 attempts_etl 计数).

    Notes
    -----
    Langfuse 接入 (PR 4, 2026-09-23):
      - outer ``stage_trace`` 名 ``etl:{task_id}``, input=None,
        output=``_capture_save_payload(outputs)`` (延迟闭包),
        tags=[``stage:etl``, ``task:{task_id}``, ``attempt:N``].
      - 子 span ``etl.load_refined_session`` (input=C2 dict / output=Session dump).
      - 子 span ``etl.save_c3_4views`` (input=Session dump / output=4视图路径dict).
      - ``per_step_span=False`` 时跳过两个子 span (outer 仍开).
      - finally 同步 ``client.flush()`` (主路径 + 业务异常覆盖).
    """
    c2_path = Path(c2_path)
    etl_outputs_dir = Path(etl_outputs_dir)

    t0 = time.perf_counter()

    # Langfuse 客户端工厂: cfg=None / enabled=False / SDK 缺失 / 凭据缺失
    # → 返 None, 全部 span 创建 path 退化为 no-op.
    client = get_client(langfuse_cfg)
    payload_mode = (
        getattr(langfuse_cfg, "upload_payload", "full") if langfuse_cfg else "none"
    )
    max_payload_bytes = (
        getattr(langfuse_cfg, "max_payload_bytes", 0) if langfuse_cfg else 0
    )
    per_step_span = (
        getattr(langfuse_cfg, "per_step_span", True) if langfuse_cfg else True
    )

    # save 子 span 的 output 闭包需要 in-flight `outputs`; 提前定义占位,
    # finally 阶段 flush 兜底.
    outputs: EtlOutputs | None = None
    session: Any = None

    # --- 1. C2 文件存在性 + schema 校验 ---
    if not c2_path.exists():
        # 提前 raise → 外层还没 stage_trace → 这里直接抛 EtlNonRetryableError,
        # 不开 outer span (外层 span 仅覆盖业务主路径, 缺少 C2 / 参数错由调用方
        # 标 dead 即可). 不吞, 不消化.
        raise EtlNonRetryableError(
            f"run_etl_once: C2 refined session missing for task {task_id}: {c2_path}"
        )

    try:
        with stage_trace(
            client,
            session_id=session_id,
            name=f"etl:{task_id}",
            user_id=session_id,
            task_id=task_id,
            tags=["stage:etl", f"task:{task_id}", f"attempt:{attempt}"],
            metadata={
                "stage": "etl",
                "task_id": task_id,
                "session_id": session_id,
                "attempt": attempt,
                "c2_path": str(c2_path),
                "etl_outputs_dir": str(etl_outputs_dir),
            },
            input_data=None,
            # 延迟闭包: outer span __exit__ 时取 `outputs` 的当前值.
            output_capture=lambda: _capture_save_payload(outputs) if outputs else None,
            payload_mode=payload_mode,
            max_payload_bytes=max_payload_bytes,
        ):
            # ---- C2 -> Session (sub-span) ----
            # C2 raw 提前读 + 深拷贝, 防止后续 load 失败时 input 还能取证.
            c2_raw_dict: dict | None
            try:
                import json as _json
                c2_raw_dict = _json.loads(c2_path.read_text(encoding="utf-8"))
            except Exception as exc:
                c2_raw_dict = {"_raw_read_error": f"{type(exc).__name__}: {exc}"}
            c2_raw_snap = snapshot(c2_raw_dict) if c2_raw_dict else None

            if per_step_span:
                try:
                    with step_span(
                        client,
                        name="etl.load_refined_session",
                        input_data=c2_raw_snap,
                        # 闭包陷阱: lambda 自己的 locals() 拿不到外层
                        # `session`; 必须靠闭包变量, 不要写
                        # `if 'session' in locals()`.
                        output_capture=(
                            lambda: _to_jsonable(session) if session is not None else None
                        ),
                        payload_mode=payload_mode,
                        session_id=session_id,
                        task_id=task_id,
                        max_payload_bytes=max_payload_bytes,
                        metadata={"c2_path": str(c2_path)},
                    ):
                        session = load_refined_session(c2_path)
                except (ValueError, UnicodeDecodeError, FileNotFoundError) as exc:
                    # schema 不匹配 / JSON 损坏 → 永久性错误, 重试不会改变结果.
                    # 异常穿透 stage_trace (标 outer ERROR + status_message),
                    # 此处仅做错误分类重抛.
                    raise EtlNonRetryableError(
                        f"run_etl_once: load_refined_session error "
                        f"({type(exc).__name__}): {exc}"
                    ) from exc
            else:
                try:
                    session = load_refined_session(c2_path)
                except (ValueError, UnicodeDecodeError, FileNotFoundError) as exc:
                    raise EtlNonRetryableError(
                        f"run_etl_once: load_refined_session error "
                        f"({type(exc).__name__}): {exc}"
                    ) from exc

            # ---- session_id 跨层校验 (C2 内 session_id 必须等于入参) ----
            # session_id 串联三阶段 trace 的唯一字段; 不一致 = 数据被串错,
            # 直接 dead, 不重试.
            session_file_id = getattr(session, "session_id", None)
            if session_file_id != session_id:
                raise EtlNonRetryableError(
                    f"run_etl_once: session_id mismatch: arg={session_id!r} "
                    f"c2={session_file_id!r} for task {task_id}"
                )

            # ---- Session -> 4 视图 (sub-span) ----
            # base_path 是无扩展名 stem; save_session_v2 会自动追加
            # ``.messages.json`` / ``.openai.json`` / ``.qwenjina.txt`` /
            # ``.meta.json`` 4 个尾缀. stem = ``<safe_task_id>__<safe_session_id>``
            # (与 gdr 写 C2 时同一前缀, 便于 C2 与 C3 归并到同一 prefix).
            base_path = etl_outputs_dir / _output_filename(
                task_id, session_id, suffix=""
            )
            base_path.parent.mkdir(parents=True, exist_ok=True)

            duration_so_far = time.perf_counter() - t0
            if per_step_span:
                with step_span(
                    client,
                    name="etl.save_c3_4views",
                    input_data=_to_jsonable(session),
                    output_capture=lambda: _capture_save_payload(outputs),
                    payload_mode=payload_mode,
                    session_id=session_id,
                    task_id=task_id,
                    max_payload_bytes=max_payload_bytes,
                    metadata={"base_path": str(base_path)},
                ):
                    session_outputs = save_session_v2(session, base_path)
                    outputs = EtlOutputs.from_session_v2(
                        session_outputs,
                        task_id=task_id,
                        session_id=session_id,
                        duration_seconds=duration_so_far,
                    )
            else:
                session_outputs = save_session_v2(session, base_path)
                outputs = EtlOutputs.from_session_v2(
                    session_outputs,
                    task_id=task_id,
                    session_id=session_id,
                    duration_seconds=duration_so_far,
                )

            # outer span 退出前: ``outputs`` 必须已绑定 (成功路径必走).
            assert outputs is not None  # pragma: no cover
    finally:
        # 主路径 flush: 每 task 边界强制把 SDK 缓冲 batch 上传.
        # fail-safe: flush 抛错不要影响业务返回值.
        if client is not None:
            try:
                client.flush()
            except Exception as exc:  # pragma: no cover
                _log.warning("Langfuse flush failed during run_etl_once: %s", exc)

    assert outputs is not None  # pragma: no cover
    return outputs