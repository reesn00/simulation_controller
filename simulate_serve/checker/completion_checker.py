"""simulate_serve.checker.completion_checker — trajectory completeness verification.

判定逻辑（5 维正向/反向混合）:
  1. 末尾 event_type ∈ {final_reply, error, cancel}
     - 不在 → incomplete(no_terminal_event)
     - error/cancel → aborted
     - final_reply → 进入维度 2
  2. 末尾 toolcall 未配对 → incomplete
  3. toolcall / toolresult 数量不匹配（尾部缺失） → incomplete
  4. 末尾 text 被截断（启发式） → incomplete
  5. 末段仅 thinking（>= 200 字符）且无 text → incomplete

与 gdr 侧 ``_detect_incomplete_session`` 的语义差异:
  - gdr 是反向判定（默认完整, 命中 4 维才标 incomplete）, 只作用于
    Session 层末段文本, 输出 dict 或 None.
  - 本模块是正向输出 ``CompletionCheck``, 先在 trajectory 事件层确认
    终态事件已到达, 再叠加 Session 层启发式. 用于决定是否在
    simulate_serve 阶段原地重投远端 Agent.

边界约束: 本模块**不依赖 gdr / etl**, 通过本地简化解析与启发式
避免反向依赖（``simulation server → gdr → etl`` 是单向架构）。
"""
from __future__ import annotations

import json
import logging
import shutil
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Iterator

from simulate_serve.domain.completion import CompletionCheck

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------


_TERMINAL_EVENT_TYPES = frozenset({"final_reply", "error", "cancel"})

# 末段 text 启发式: "出现即不完整" 标记 (前缀 marker)
_INCOMPLETE_TEXT_MARKERS = (
    "我先说清楚一点",
    "继续",
    "再",
    "等",
    "我先",
    "让我",
    "查一下",
    "继续看",
    "稍后整理",
    "稍后汇总",
    "我先把",
    "等下",
    "稍等",
    "等一下",
    "稍等一下",
)

# 末段 text 启发式: "出现即完整" 强信号 (尾部 100 字符)
_COMPLETE_TAIL_TOKENS = (
    "总结", "结论", "已核实", "已完成", "下一步", "锚点",
    "报告完毕", "报告完", "汇报完毕", "汇总完毕", "排查完", "结束",
    "completion complete", "task complete", "done",
    "就这样", "以上", "完毕", "结束", "结果如上",
)

# 末段仅 thinking 阈值（与 gdr.config.settings.incomplete_thinking_only_min_chars 默认一致）
_THINKING_ONLY_MIN_CHARS = 200


# ---------------------------------------------------------------------------
# 公共 API
# ---------------------------------------------------------------------------


def check_completion(trajectory_path: Path | str | None) -> CompletionCheck:
    """判定 trajectory 是否完整结束任务。

    Args:
        trajectory_path: agent_trajectory 目录下的 JSONL 文件路径
                         (e.g. ``<run_id>__<session_id>.json``); 为 None
                         时按缺失处理, 返 incomplete(retryable=True)。
    """
    detected_at = datetime.now(UTC).isoformat()

    if trajectory_path is None:
        return _incomplete(
            reasons=("trajectory_path_missing",),
            detected_at=detected_at,
            summary="未指定 trajectory 路径",
        )

    path = Path(trajectory_path)
    if not path.exists():
        return _incomplete(
            reasons=("trajectory_file_missing",),
            detected_at=detected_at,
            summary=f"trajectory 文件不存在: {path}",
        )

    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as exc:
        return _incomplete(
            reasons=(f"trajectory_read_error:{type(exc).__name__}",),
            detected_at=detected_at,
            summary=f"trajectory 读取失败: {exc}",
        )

    events = list(_iter_json_objects(raw))
    if not events:
        return _incomplete(
            reasons=("trajectory_empty",),
            detected_at=detected_at,
            summary="trajectory 事件流为空",
        )

    # 维度 1: 末尾 event_type
    last_event_type = str(events[-1].get("event_type", "") or "")
    has_final_reply = any(str(e.get("event_type", "") or "") == "final_reply" for e in events)
    terminal_event_seen = last_event_type in _TERMINAL_EVENT_TYPES

    if last_event_type in ("error", "cancel"):
        return CompletionCheck(
            status="aborted",
            reasons=(f"terminal_event:{last_event_type}",),
            last_event_type=last_event_type,
            has_final_reply=has_final_reply,
            terminal_event_seen=True,
            last_block_type="",
            final_text_preview="",
            summary=f"trajectory 终态事件为 {last_event_type} (agent 未正常结束)",
            retryable=False,
            detected_at=detected_at,
        )

    if not terminal_event_seen:
        return _incomplete(
            reasons=(f"no_terminal_event:last_event={last_event_type or '<missing>'}",),
            detected_at=detected_at,
            summary=f"trajectory 末尾事件不是终态事件 ({last_event_type or '<missing>'})",
            last_event_type=last_event_type,
            has_final_reply=has_final_reply,
            terminal_event_seen=False,
        )

    # 维度 2-5: 简化重放末段
    last_block_type, text_blocks, toolcall_ids, toolresult_ids, final_text, thinking_chars = (
        _replay_last_assistant(events)
    )

    reasons: list[str] = []

    if last_block_type in ("toolcall", "tool_call"):
        last_name = toolcall_ids[-1] if toolcall_ids else "?"
        reasons.append(f"last_assistant_block_is_toolcall:{last_name}")

    pending_toolcall = len(toolcall_ids) - len(toolresult_ids)
    if pending_toolcall > 0 and not _has_complete_close_signal(final_text):
        reasons.append(
            f"toolcall_result_mismatch:{len(toolcall_ids)}_vs_{len(toolresult_ids)}"
        )

    if last_block_type == "text" and final_text and _is_text_incomplete(final_text):
        reasons.append("last_text_incomplete")

    # F2 修复: 末段仅 thinking 且无 text、未配对 toolcall、字符数超阈值 → incomplete.
    # 只看最后一轮 (last_block_type / text_blocks / thinking_chars 已是最后轮状态),
    # 不引入全局 toolcall_ids (会误把上一轮已配对的 toolcall 算进来).
    if (
        last_block_type == "thinking"
        and not text_blocks
        and thinking_chars >= _THINKING_ONLY_MIN_CHARS
    ):
        reasons.append(
            f"last_assistant_no_final_text:thinking_only:{thinking_chars}chars"
        )

    preview = final_text[:200] if final_text else ""

    if reasons:
        return CompletionCheck(
            status="incomplete",
            reasons=tuple(reasons),
            last_event_type=last_event_type,
            has_final_reply=has_final_reply,
            terminal_event_seen=True,
            last_block_type=last_block_type,
            final_text_preview=preview,
            summary=f"trajectory 不完整: {'; '.join(reasons)}",
            retryable=True,
            detected_at=detected_at,
        )

    return CompletionCheck(
        status="complete",
        reasons=("all_trajectory_complete",),
        last_event_type=last_event_type,
        has_final_reply=has_final_reply,
        terminal_event_seen=True,
        last_block_type=last_block_type,
        final_text_preview=preview,
        summary=(
            f"任务已完整结束 (末段 {len(final_text)} 字, {len(toolcall_ids)} 个工具调用)"
            if final_text
            else f"任务已完整结束 ({len(toolcall_ids)} 个工具调用)"
        ),
        retryable=False,
        detected_at=detected_at,
    )


def snapshot_partial_trajectory(
    source: Path | str,
    run_id: str,
    attempt: int,
) -> Path | None:
    """重试前把 partial trajectory 复制为 ``trajectory_attempt_<N>.json``。

    Args:
        source: 当前 trajectory 文件路径
        run_id: 复用 run_id (用于生成归档名)
        attempt: 当前 attempt 编号 (1-based; 1 表示首次失败前的快照)

    Returns:
        实际写入的归档路径; 源文件缺失或 IO 错误时返回 None。
    """
    src = Path(source)
    if not src.exists():
        return None
    parent = src.parent
    archive_path = parent / f"{run_id}.trajectory_attempt_{attempt}.json"
    try:
        shutil.copy2(src, archive_path)
        logger.info(
            "snapshot partial trajectory: run_id=%s attempt=%d -> %s",
            run_id, attempt, archive_path,
        )
        return archive_path
    except OSError as exc:
        logger.warning(
            "failed to snapshot partial trajectory for run_id=%s attempt=%d: %s",
            run_id, attempt, exc,
        )
        return None


# ---------------------------------------------------------------------------
# 内部 helpers
# ---------------------------------------------------------------------------


def _incomplete(
    *,
    reasons: tuple[str, ...],
    detected_at: str,
    summary: str,
    last_event_type: str = "",
    has_final_reply: bool = False,
    terminal_event_seen: bool = False,
    last_block_type: str = "",
    final_text_preview: str = "",
) -> CompletionCheck:
    return CompletionCheck(
        status="incomplete",
        reasons=reasons,
        last_event_type=last_event_type,
        has_final_reply=has_final_reply,
        terminal_event_seen=terminal_event_seen,
        last_block_type=last_block_type,
        final_text_preview=final_text_preview,
        summary=summary,
        retryable=True,
        detected_at=detected_at,
    )


def _replay_last_assistant(
    events: list[dict[str, Any]],
) -> tuple[str, list[dict[str, Any]], list[str], list[str], str, int]:
    """简化重放: 找最后一个 assistant message 的末段统计。

    Returns:
        (last_block_type, text_blocks, toolcall_ids, toolresult_ids,
         final_text, thinking_chars_of_last_assistant)
    """
    asst_buf: list[dict[str, Any]] = []
    last_blocks: list[dict[str, Any]] = []
    last_thinking_chars = 0

    all_toolcall_ids: list[str] = []
    all_toolresult_ids: list[str] = []

    def flush() -> None:
        nonlocal last_blocks, last_thinking_chars
        if asst_buf:
            last_blocks = list(asst_buf)
            last_thinking_chars = sum(
                len(str(b.get("thinking", "") or ""))
                for b in asst_buf
                if b.get("type") == "thinking"
            )
            asst_buf.clear()

    for ev in events:
        et = str(ev.get("event_type", "") or "")
        payload = ev.get("payload") or {}
        if et == "model_response":
            content = payload.get("content")
            if isinstance(content, list):
                for b in content:
                    if isinstance(b, dict):
                        asst_buf.append(b)
        elif et == "tool_execution":
            tc_id = str(payload.get("tool_call_id", "") or "")
            if tc_id:
                all_toolresult_ids.append(tc_id)
        elif et in ("final_reply", "error", "cancel"):
            # 累计当前轮已知的 toolcall id (flush 前)
            for b in asst_buf:
                if b.get("type") in ("tool_call", "toolcall"):
                    tc_id = str(b.get("id", "") or "")
                    if tc_id:
                        all_toolcall_ids.append(tc_id)
            flush()
    # 末尾残留的 assistant buffer (无 final_reply 关闭)
    for b in asst_buf:
        if b.get("type") in ("tool_call", "toolcall"):
            tc_id = str(b.get("id", "") or "")
            if tc_id:
                all_toolcall_ids.append(tc_id)
    flush()

    if not last_blocks:
        return "", [], [], [], "", 0

    last_block = last_blocks[-1]
    last_block_type = str(last_block.get("type", "") or "")

    text_blocks = [b for b in last_blocks if b.get("type") == "text"]

    final_text = ""
    for b in reversed(last_blocks):
        if b.get("type") == "text":
            final_text = str(b.get("text", "") or "")
            break

    return (
        last_block_type,
        text_blocks,
        all_toolcall_ids,
        all_toolresult_ids,
        final_text,
        last_thinking_chars,
    )


def _iter_json_objects(raw: str) -> Iterator[dict[str, Any]]:
    """大括号深度计数切分 JSON 对象 (支持字符串边界 / 转义)。

    与 ``etl.qwenformat.load._iter_json_objects`` 等价逻辑。
    本模块自包含实现以避免 ``simulate_serve → etl`` 反向依赖。
    """
    depth = 0
    in_string = False
    escape = False
    obj_start = -1
    for i, ch in enumerate(raw):
        if in_string:
            if escape:
                escape = False
            elif ch == "\\":
                escape = True
            elif ch == '"':
                in_string = False
            continue
        if ch == '"':
            in_string = True
        elif ch == "{":
            if depth == 0:
                obj_start = i
            depth += 1
        elif ch == "}":
            if depth > 0:
                depth -= 1
            if depth == 0 and obj_start >= 0:
                obj_text = raw[obj_start:i + 1]
                obj_start = -1
                try:
                    obj = json.loads(obj_text)
                except json.JSONDecodeError:
                    continue
                if isinstance(obj, dict):
                    yield obj


def _is_text_incomplete(text: str) -> bool:
    """启发式: text 末尾不像完整回复 (低强度信号, 仅辅助)。

    与 gdr._is_text_incomplete 等价逻辑, 自包含实现避免反向依赖。
    """
    s = text.strip()
    if not s:
        return True
    if s.endswith("...") or s.endswith("…"):
        return True
    if len(s) > 30 and _has_structural_close(s):
        return False
    if len(s) > 30:
        tail_window = s[-100:]
        if any(tok in tail_window for tok in _COMPLETE_TAIL_TOKENS):
            return False
        sentence_end = set(".!?。！？\"'""''")
        if s.rstrip()[-1] not in sentence_end:
            if not any(s.endswith(w) for w in ("完", "了", "好", "OK", "ok")):
                return True
    if len(s) > 30:
        for marker in _INCOMPLETE_TEXT_MARKERS:
            if marker in s[:60]:
                return True
    return False


def _has_structural_close(s: str) -> bool:
    """F3-D 等价: 尾部是否存在结构性闭合信号。"""
    tail = s.rstrip()[-300:] if len(s) > 300 else s.rstrip()
    last_line = tail.splitlines()[-1] if tail else ""
    if last_line.strip().startswith("|") and last_line.strip().endswith("|"):
        return True
    for line in tail.splitlines():
        stripped = line.strip()
        if stripped in ("---", "***", "___") or (
            len(stripped) >= 3 and all(c in "-*_" for c in stripped)
        ):
            return True
    if "```" in tail:
        return True
    if tail.rstrip().endswith(":::"):
        return True
    bracket_pairs = [
        ("⟦", "⟧"), ("【", "】"), ("『", "』"),
        ("「", "」"), ("《", "》"), ("(", ")"),
        ("[", "]"), ("{", "}"),
    ]
    for open_b, close_b in bracket_pairs:
        oi = s.rfind(open_b)
        ci = s.rfind(close_b)
        if oi != -1 and ci != -1 and ci > oi:
            if tail.endswith(close_b) or close_b in tail[-10:]:
                return True
    return False


def _has_complete_close_signal(s: str) -> bool:
    """F3-E 等价: 弱化版完整收尾检测, 用于 toolcall/result 不匹配的豁免。"""
    if not s:
        return False
    if _has_structural_close(s):
        return True
    tail_window = s[-100:]
    if any(tok in tail_window for tok in _COMPLETE_TAIL_TOKENS):
        return True
    return False