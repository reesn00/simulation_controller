"""gdr/refiners/retry_loop_clip: LLM 判断式同函数连续重试剪枝.

设计原则 (方案 ②):
- 触发严格: rule-based 预筛通过的连续同函数 429 段才进入 LLM 评估.
- LLM 失败 / 解析失败 / 字段缺失 → **整段保留**, 不做任何剪枝 (保守 fallback).
- LLM 决定保留哪几条: 长度 1-3, 且至少 1 条对应失败结果.

API:
    - ``is_rate_limited_text(text)``                — 文本是否含 429 / rate limit 标记
    - ``group_consecutive_same_function(blocks)``    — 把 blocks 切成同名 toolcall 连续段
    - ``find_retry_loop_segments(blocks, min_consecutive)`` — rule 预筛
    - ``llm_judge_retry_loop(segment, client)``      — LLM 决策, 返回 keep_indices 或 None
    - ``apply_clip(blocks, keep_indices)``           — 按 keep_indices 剪枝
    - ``clip_session(session, client, ...)``         — session 级入口, 写 retry_loop_clip 元数据
"""
from __future__ import annotations

import json
import logging
from typing import Any

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# rate-limit 标记
# ---------------------------------------------------------------------------

_RATE_LIMIT_MARKERS = (
    "429", "rate limit", "too many requests", "rate_limited", "timeout",
)


def is_rate_limited_text(text: str) -> bool:
    """文本中是否含 429 / rate-limit / timeout 标记 (大小写不敏感)."""
    if not text:
        return False
    lower = text.lower()
    return any(m in lower for m in _RATE_LIMIT_MARKERS)


# ---------------------------------------------------------------------------
# blocks 解析: dict / Pydantic 模型统一
# ---------------------------------------------------------------------------


def _btype(b: Any) -> str:
    return b.get("type", "") if isinstance(b, dict) else getattr(b, "type", "")


def _bid(b: Any) -> str:
    return b.get("id", "") if isinstance(b, dict) else getattr(b, "id", "")


def _bname(b: Any) -> str:
    return b.get("name", "") if isinstance(b, dict) else getattr(b, "name", "")


def _bstate(b: Any) -> str | None:
    return b.get("state", None) if isinstance(b, dict) else getattr(b, "state", None)


def _boutput(b: Any) -> str:
    return b.get("output_text", "") if isinstance(b, dict) else getattr(b, "output_text", "") or ""


# ---------------------------------------------------------------------------
# group_consecutive_same_function
# ---------------------------------------------------------------------------


def group_consecutive_same_function(
    blocks: list,
) -> list[list[tuple[int, Any, Any]]]:
    """把 blocks 切分为"同名 toolcall 连续段".

    同段条件:
        - 连续 toolcall 之间只能出现 toolresult;
        - toolcall.name 必须相同;
        - 出现 text / thinking 等非工具块即打断, 开启新段.

    Returns:
        list of runs, 每个 run 是 ``[(tc_block_index, call_block, result_block | None), ...]``
    """
    runs: list[list[tuple[int, Any, Any]]] = []
    current: list[tuple[int, Any, Any]] = []
    current_name: str | None = None

    i = 0
    n = len(blocks)
    while i < n:
        b = blocks[i]
        t = _btype(b)
        if t == "toolcall":
            name = _bname(b)
            if current_name is not None and name != current_name:
                # 同段要求同名; 切分
                runs.append(current)
                current = []
                current_name = None
            current_name = name
            # 找配对的 toolresult (按 id 跨过其它 toolcall 扫描, 与 reassembler 一致)
            result = None
            for k in range(i + 1, n):
                nb = blocks[k]
                nt = _btype(nb)
                if nt == "toolresult" and _bid(nb) == _bid(b):
                    result = nb
                    break
                if nt != "toolcall" and nt != "toolresult":
                    break
            current.append((i, b, result))
            i += 1
        elif t == "toolresult":
            # 不开新段; 若 current 已收尾同一段 toolcall, 这里跳过;
            # 但若 result 散落 (没有配对 call), 视为孤立 toolresult, 忽略
            i += 1
        else:
            # text / thinking 等非工具块打断
            if current:
                runs.append(current)
                current = []
                current_name = None
            i += 1

    if current:
        runs.append(current)
    return runs


# ---------------------------------------------------------------------------
# find_retry_loop_segments: rule 预筛
# ---------------------------------------------------------------------------


def find_retry_loop_segments(
    blocks: list,
    min_consecutive: int = 5,
) -> list[list[tuple[int, Any, Any]]]:
    """rule 预筛: 找出连续 ≥ min_consecutive 次同函数调用且结果全部 429 的段."""
    out: list[list[tuple[int, Any, Any]]] = []
    for run in group_consecutive_same_function(blocks):
        if len(run) < min_consecutive:
            continue
        all_429 = all(
            is_rate_limited_text(_boutput(result) if result is not None else "")
            for _, _, result in run
        )
        if all_429:
            out.append(run)
    return out


# ---------------------------------------------------------------------------
# llm_judge_retry_loop
# ---------------------------------------------------------------------------


_CLIP_PROMPT = """你是一名 SFT 训练数据质量评估员. 以下是一段连续 {count} 次的同函数调用记录,
全部返回 429 / 限流 / 超时错误.

任务: 判断这 {count} 次调用是否属于"同一意图反复重试". 仅在判断为真时, 才给出保留方案.

要求:
1. "同一意图反复重试" = 各次调用的输入参数语义高度一致, 差异仅为:
   - 标点 / 大小写 / 同义改写
   - 增加少量限定词 (如 "正版", "免费", "2024")
   - 重复字符 / 关键词顺序调整
   但核心搜索目标 / 输入 schema 不允许发生本质变化.

2. 如果判定为否 (例如用户主动换了搜索方向), 返回 {{"is_retry_loop": false, "reason": "..."}},
   不要给保留方案.

3. 如果判定为真, 给出 keep_indices (list[int], 从 0 开始的下标), 满足:
   - 长度 >= 1, <= {max_keep}
   - 至少 1 个是含失败结果的下标
   - 优先保留最早与最晚的几次

4. 必须返回合法 JSON.

【调用记录】:
{calls}

【输出 JSON 格式】:
{{"is_retry_loop": bool, "reason": str, "keep_indices": [int, ...] | null}}
"""


def _summarize_call(call: Any, result: Any | None) -> dict[str, Any]:
    output = _boutput(result) if result is not None else ""
    state = _bstate(result) if result is not None else None
    return {
        "function": _bname(call),
        "input": (call.get("input", "") if isinstance(call, dict) else getattr(call, "input", "") or "")[:200],
        "error": output[:200],
        "state": state,
    }


def llm_judge_retry_loop(
    segment: list[tuple[int, Any, Any]],
    llm_client: Any,
    max_keep: int = 3,
    timeout_s: int | None = None,
) -> list[int] | None:
    """用 LLM 判断 segment 是否为重试循环, 返回保留的下标或 None.

    严格 fallback: 任何异常 / 解析失败 / 字段缺失 → 返回 None (不动数据).
    """
    if not segment:
        return None

    calls_payload = json.dumps(
        [_summarize_call(call, result) for _, call, result in segment],
        ensure_ascii=False, indent=2,
    )
    prompt = _CLIP_PROMPT.format(
        count=len(segment), max_keep=max_keep, calls=calls_payload,
    )

    try:
        raw, _meta = llm_client.generate(prompt, max_tokens=400, timeout_s=timeout_s)
        parsed = json.loads(raw)
    except Exception as e:
        log.warning("llm_judge_retry_loop: LLM call/parse failed (%s) — fallback to keep", e)
        return None

    if not isinstance(parsed, dict):
        return None
    if not parsed.get("is_retry_loop"):
        return None
    keep = parsed.get("keep_indices")
    if not isinstance(keep, list) or not keep:
        return None

    # 过滤掉越界下标
    keep = [int(i) for i in keep if isinstance(i, (int, float)) and 0 <= int(i) < len(segment)]
    keep = list(dict.fromkeys(keep))  # 去重保序
    if not keep:
        return None
    keep = keep[:max_keep]

    # 必须至少 1 个对应失败结果 (state in {"error", "timeout", ...})
    def _is_failure(result: Any) -> bool:
        if result is None:
            return False
        state = _bstate(result) or ""
        if state in ("error", "timeout", "denied", "rate_limited", "network"):
            return True
        # 兜底: 即便 state 不是显式 error, output_text 含 429 标记也算失败
        return is_rate_limited_text(_boutput(result))

    if not any(_is_failure(segment[i][2]) for i in keep):
        log.info("llm_judge_retry_loop: keep_indices lack failure → reject")
        return None

    return keep


# ---------------------------------------------------------------------------
# apply_clip
# ---------------------------------------------------------------------------


def apply_clip(blocks: list, keep_indices: list[int]) -> list:
    """按 keep_indices 保留 toolcall 及其配对 toolresult, 其余删除.

    keep_indices 是 toolcall 在 blocks 列表中的下标 (与
    ``group_consecutive_same_function`` 返回的 tc_block_index 一致).

    空 keep_indices → 原样返回 (保守).
    """
    if not keep_indices:
        return blocks

    keep_set = set(keep_indices)
    # 同时保留配对 toolresult: 先扫描一遍建立 toolcall_id -> toolcall_index
    tc_id_to_idx: dict[str, int] = {}
    for i, b in enumerate(blocks):
        if _btype(b) == "toolcall":
            tc_id_to_idx[_bid(b)] = i

    kept_tc_ids: set[str] = set()
    for idx in keep_set:
        b = blocks[idx]
        if _btype(b) == "toolcall":
            kept_tc_ids.add(_bid(b))

    out: list = []
    for i, b in enumerate(blocks):
        t = _btype(b)
        if t == "toolcall":
            if i in keep_set:
                out.append(b)
            # 否则跳过 (即使被保留的 result 引用了它, result 已不被保留)
        elif t == "toolresult":
            # 配对的 toolcall 在 keep_set → 保留; 否则丢弃
            if _bid(b) in kept_tc_ids:
                out.append(b)
        else:
            out.append(b)
    return out


# ---------------------------------------------------------------------------
# clip_session: session 级入口
# ---------------------------------------------------------------------------


def clip_session(
    session: Any,
    llm_client: Any,
    *,
    min_consecutive: int = 5,
    max_keep: int = 3,
    enabled: bool = True,
    llm_timeout_s: int | None = None,
) -> int:
    """扫描 session.messages[*].blocks, 对每段连续同函数 429 调用走 LLM 判剪枝.

    Returns:
        删除的 block 数量.
    """
    if not enabled:
        return 0

    total_removed = 0
    audit_entries: list[dict[str, Any]] = []

    for msg_idx, msg in enumerate(session.messages):
        blocks = getattr(msg, "blocks", [])
        segs = find_retry_loop_segments(blocks, min_consecutive=min_consecutive)
        if not segs:
            continue

        for seg in segs:
            decision = llm_judge_retry_loop(
                seg, llm_client, max_keep=max_keep, timeout_s=llm_timeout_s,
            )
            if decision is None:
                audit_entries.append({
                    "msg_idx": msg_idx,
                    "function": _bname(seg[0][1]) if seg else "",
                    "segment_len": len(seg),
                    "action": "kept",
                    "reason": "llm_judge_returned_none",
                })
                continue

            # decision 是 segment-internal 下标 (0..len(seg)-1);
            # apply_clip 需要的是 blocks 列表中的绝对下标, 从 seg[idx] 取 (block_idx, ...)
            block_keep = [seg[i][0] for i in decision]
            before_count = len(blocks)
            new_blocks = apply_clip(blocks, block_keep)
            removed = before_count - len(new_blocks)
            if removed > 0:
                msg.blocks = new_blocks
                blocks = new_blocks
                total_removed += removed
                audit_entries.append({
                    "msg_idx": msg_idx,
                    "function": _bname(seg[0][1]) if seg else "",
                    "segment_len": len(seg),
                    "kept_segment_indices": decision,
                    "kept_block_indices": block_keep,
                    "removed": removed,
                    "action": "clipped",
                })

    # 写 retry_loop_clip 审计字段到 session.metadata
    if audit_entries:
        meta = dict(getattr(session, "metadata", None) or {})
        meta["retry_loop_clip"] = {
            "enabled": enabled,
            "min_consecutive": min_consecutive,
            "max_keep": max_keep,
            "total_removed": total_removed,
            "decisions": audit_entries,
        }
        session.metadata = meta

    return total_removed
