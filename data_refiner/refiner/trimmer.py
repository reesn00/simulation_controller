"""连续工具调用失败段识别与剪裁 (R1)。

失败判定: toolresult.state == "error"。
连续失败段 = 相邻的 tool 调用全部失败。段内删除前 n-1 次失败 (含其紧邻前方的
thinking 块)，保留最后一次失败；成功调用一律不动。
注意: 只处理连续段, 绝不做全局"删所有失败只留最后一次"。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from refiner.validity import is_tool_result_error

logger = logging.getLogger("data_refiner.trimmer")


@dataclass
class FailureSegment:
    """一个连续失败段。members 为 (message_index, block_index) 对，按顺序排列。"""

    segment_id: int
    members: list[tuple[int, int]] = field(default_factory=list)

    def __len__(self) -> int:
        return len(self.members)


def _find_failure_segments(
    flat: list[tuple[int, int, dict]],
) -> list[FailureSegment]:
    """在展平的块序列上找最大连续 tool 失败段。

    flat: (message_index, block_index, block) 按原顺序排列的 assistant 块。
    段的连续性只被成功的 toolresult 或消息边界中断; thinking/toolcall 不中断
    (失败调用之间的 thinking 重试推理属于同一失败段)。
    """
    segments: list[FailureSegment] = []
    current: FailureSegment | None = None
    last_mi: int | None = None
    for mi, bi, block in flat:
        if mi != last_mi:
            # 消息边界中断连续段
            current = None
            last_mi = mi
        if block.get("type") == "toolresult" and is_tool_result_error(block):
            if current is None:
                current = FailureSegment(segment_id=len(segments) + 1)
                segments.append(current)
            current.members.append((mi, bi))
        elif block.get("type") == "toolresult":
            # 成功的 toolresult 中断连续段
            current = None
    return [s for s in segments if s]


def _thinking_before(flat: list[tuple[int, int, dict]], pos: int) -> int | None:
    """返回 flat 中 pos 位置之前紧邻的 thinking 块下标（仅当中间没有其他块）。"""
    if pos == 0:
        return None
    mi, bi, block = flat[pos]
    pmi, pbi, pblock = flat[pos - 1]
    if pblock.get("type") == "thinking" and pmi == mi:
        return pos - 1
    return None


def trim(
    data: dict,
) -> tuple[dict, list[dict]]:
    """剪裁连续失败段。返回 (新 data, 轨迹事件列表)。

    不修改原 data（深拷贝后修改）。事件记录被删除与被保留块的说明及原块内容。
    """
    import copy

    new_data = copy.deepcopy(data)
    events: list[dict] = []

    # 展平所有 assistant 消息的块
    flat: list[tuple[int, int, dict]] = []
    for mi, msg in enumerate(new_data.get("messages") or []):
        if msg.get("role") != "assistant":
            continue
        for bi, block in enumerate(msg.get("blocks") or []):
            flat.append((mi, bi, block))

    segments = _find_failure_segments(flat)
    if not segments:
        return new_data, events

    # 收集要删除的 (mi, bi) 集合，以及每个段保留的最后一次失败
    to_remove: dict[tuple[int, int], dict] = {}  # (mi, bi) -> event
    kept_last: dict[tuple[int, int], dict] = {}

    for seg in segments:
        logger.info("失败段 #%d: %d 次连续失败", seg.segment_id, len(seg))
        # 段内成员是 toolresult 的位置；对应的 toolcall 在其前一个块
        for order, (mi, bi) in enumerate(seg.members):
            is_last = order == len(seg.members) - 1
            # 定位配对 toolcall（通常紧邻 toolresult 之前）
            call_mi, call_bi = _find_pairing_toolcall(flat, mi, bi)
            if call_mi is None:
                logger.warning(
                    "失败段 #%d 成员 (msg %d, block %d) 找不到配对 toolcall, 仅按 toolresult 处理",
                    seg.segment_id, mi, bi,
                )
            result_block = flat[_flat_index(flat, mi, bi)][2]

            if is_last:
                kept_last[(mi, bi)] = {
                    "message_index": mi,
                    "block_index": bi,
                    "block_type": "toolresult",
                    "tool": result_block.get("name"),
                    "action": "KEPT_LAST_FAILURE",
                    "segment_id": seg.segment_id,
                    "segment_size": len(seg),
                    "reason": f"连续失败段(共{len(seg)}次)的最后一次失败, 保留作为轨迹",
                    "original_block": result_block,
                }
                if call_mi is not None:
                    call_block = flat[_flat_index(flat, call_mi, call_bi)][2]
                    kept_last[(call_mi, call_bi)] = {
                        "message_index": call_mi,
                        "block_index": call_bi,
                        "block_type": "toolcall",
                        "tool": call_block.get("name"),
                        "action": "KEPT_LAST_FAILURE",
                        "segment_id": seg.segment_id,
                        "segment_size": len(seg),
                        "reason": "连续失败段最后一次失败调用的 toolcall, 随结果保留",
                        "original_block": call_block,
                    }
            else:
                # 删除该失败尝试的 toolresult + toolcall + 紧邻前方的 thinking
                to_remove[(mi, bi)] = {
                    "message_index": mi,
                    "block_index": bi,
                    "block_type": "toolresult",
                    "tool": result_block.get("name"),
                    "action": "REMOVED_EARLY_FAILURE",
                    "segment_id": seg.segment_id,
                    "segment_size": len(seg),
                    "reason": f"连续失败段(共{len(seg)}次)的第{order + 1}次失败, 删除并保留段内最后一次",
                    "original_block": result_block,
                }
                if call_mi is not None:
                    call_block = flat[_flat_index(flat, call_mi, call_bi)][2]
                    to_remove[(call_mi, call_bi)] = {
                        "message_index": call_mi,
                        "block_index": call_bi,
                        "block_type": "toolcall",
                        "tool": call_block.get("name"),
                        "action": "REMOVED_EARLY_FAILURE",
                        "segment_id": seg.segment_id,
                        "segment_size": len(seg),
                        "reason": "被删除失败尝试的 toolcall",
                        "original_block": call_block,
                    }
                    # thinking 块: 在 toolcall 之前紧邻的, 一并删除
                    tpos = _flat_index(flat, call_mi, call_bi)
                    tidx = _thinking_before(flat, tpos)
                    if tidx is not None:
                        tmi, tbi, tblock = flat[tidx]
                        to_remove[(tmi, tbi)] = {
                            "message_index": tmi,
                            "block_index": tbi,
                            "block_type": "thinking",
                            "tool": None,
                            "action": "REMOVED_EARLY_FAILURE",
                            "segment_id": seg.segment_id,
                            "segment_size": len(seg),
                            "reason": "被删除失败尝试的紧邻 thinking, 一并删除避免孤立推理块",
                            "original_block": tblock,
                        }

    if to_remove:
        # 从后往前按 (mi, bi) 删除, 避免索引位移
        for mi, bi in sorted(to_remove.keys(), key=lambda k: (k[0], k[1]), reverse=True):
            blocks = new_data["messages"][mi].get("blocks") or []
            if bi < len(blocks):
                blocks.pop(bi)
        logger.info("共删除 %d 个块 (含 thinking), 保留 %d 次段末失败", len(to_remove), len(kept_last) // 2)
        # 更新 sft_stats 中与消息内容相关的统计
        _refresh_stats(new_data)

    events = sorted(
        list(to_remove.values()) + list(kept_last.values()),
        key=lambda e: (e["message_index"], e["block_index"]),
    )
    return new_data, events


def _flat_index(flat: list[tuple[int, int, dict]], mi: int, bi: int) -> int:
    for idx, (fmi, fbi, _) in enumerate(flat):
        if fmi == mi and fbi == bi:
            return idx
    raise ValueError(f"flat 中不存在 (msg {mi}, block {bi})")


def _find_pairing_toolcall(
    flat: list[tuple[int, int, dict]], mi: int, bi: int,
) -> tuple[int | None, int | None]:
    """toolresult 按 id 回溯配对的 toolcall（同一 assistant 消息内）。"""
    result_block = flat[_flat_index(flat, mi, bi)][2]
    target_id = result_block.get("id")
    for fmi, fbi, block in flat:
        if fmi != mi:
            continue
        if block.get("type") == "toolcall" and block.get("id") == target_id:
            return fmi, fbi
    return None, None


def _refresh_stats(new_data: dict) -> None:
    """删除块后同步 sft_stats 的 tool_calls / tool_results / output_messages。"""
    stats = new_data.get("sft_stats")
    if not isinstance(stats, dict):
        return
    tool_calls = 0
    tool_results = 0
    output_messages = 0
    for msg in new_data.get("messages") or []:
        output_messages += 1
        for block in msg.get("blocks") or []:
            if block.get("type") == "toolcall":
                tool_calls += 1
            elif block.get("type") == "toolresult":
                tool_results += 1
    stats["tool_calls"] = tool_calls
    stats["tool_results"] = tool_results
    stats["output_messages"] = output_messages
