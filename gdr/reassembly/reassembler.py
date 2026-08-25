import json
import logging
from domain import Session, BlockRefineRecord, MessageHealth, DefectTag
from policy import RefinementPolicy

log = logging.getLogger(__name__)


def _prune_repetitive_blocks(
    blocks: list,
    refine_records: list[BlockRefineRecord],
    cfg,
) -> tuple[list, list[BlockRefineRecord]]:
    """改进3: 对 REPETITIVE_CALL 标记的连续重复块，保留第一个，删除其余。

    同时删除对应的 toolresult 块（与 toolcall 共享相同 id）。
    """
    ids_to_remove: set[str] = set()
    i = 0
    while i < len(blocks):
        b = blocks[i]
        if isinstance(b, dict):
            t = b.get("type", "")
        else:
            t = getattr(b, "type", "")

        if t != "toolcall":
            i += 1
            continue

        # 找到连续相同 name 的 toolcall 组
        name = b.get("name") if isinstance(b, dict) else getattr(b, "name", "")
        group_start = i
        j = i + 1
        while j < len(blocks):
            nb = blocks[j]
            if isinstance(nb, dict):
                nt = nb.get("type", "")
                nname = nb.get("name", "")
            else:
                nt = getattr(nb, "type", "")
                nname = getattr(nb, "name", "")
            if nt == "toolcall" and nname == name:
                j += 1
            else:
                break

        group_size = j - group_start
        if group_size >= cfg.repetitive_call_threshold:
            # 检查 inputs 是否高度相似
            inputs = []
            for k in range(group_start, j):
                bk = blocks[k]
                inputs.append(bk.get("input", "") if isinstance(bk, dict) else getattr(bk, "input", ""))
            all_similar = True
            for k in range(len(inputs) - 1):
                from difflib import SequenceMatcher
                if SequenceMatcher(None, inputs[k], inputs[k + 1]).ratio() <= 0.9:
                    all_similar = False
                    break
            if all_similar:
                # 保留第一个 toolcall，删除后续重复的 + 对应的 toolresult
                for k in range(group_start + 1, j):
                    bid = blocks[k].get("id", "") if isinstance(blocks[k], dict) else getattr(blocks[k], "id", "")
                    ids_to_remove.add(bid)
                log.info(
                    "pruned %d repetitive %s calls in blocks[%d:%d], kept first",
                    group_size - 1, name, group_start, j,
                )
        i = j

    if not ids_to_remove:
        return blocks, refine_records

    # 过滤 blocks
    pruned_blocks = []
    for b in blocks:
        bid = b.get("id", "") if isinstance(b, dict) else getattr(b, "id", "")
        if bid not in ids_to_remove:
            pruned_blocks.append(b)

    # 过滤 refine_records
    pruned_records = [r for r in refine_records if r.block_index.block_id not in ids_to_remove]

    return pruned_blocks, pruned_records


def _prune_context_switch_blocks(
    blocks: list,
    refine_records: list[BlockRefineRecord],
    cfg,
) -> tuple[list, list[BlockRefineRecord]]:
    """改进3: 对 CONTEXT_SWITCH_LOOP 标记的 browser↔shell 切换循环，
    保留第一次出现的一对有效调用，删除后续无意义的切换对。

    策略：保留前 2 次 browser↔shell 切换（供 Agent 探索），
    删除第 3 次及以后的切换对。
    """
    # 找到所有 browser↔shell 切换的 toolcall 索引
    switch_pairs: list[tuple[int, int]] = []  # (browser_idx, shell_idx) 或 (shell_idx, browser_idx)
    toolcall_indices = []
    for i, b in enumerate(blocks):
        t = b.get("type", "") if isinstance(b, dict) else getattr(b, "type", "")
        if t == "toolcall":
            toolcall_indices.append(i)

    for j in range(1, len(toolcall_indices)):
        prev_idx = toolcall_indices[j - 1]
        curr_idx = toolcall_indices[j]
        prev_b = blocks[prev_idx]
        curr_b = blocks[curr_idx]
        prev_name = prev_b.get("name") if isinstance(prev_b, dict) else getattr(prev_b, "name", "")
        curr_name = curr_b.get("name") if isinstance(curr_b, dict) else getattr(curr_b, "name", "")
        if (prev_name, curr_name) in [("browser", "execute_shell_command"), ("execute_shell_command", "browser")]:
            switch_pairs.append((prev_idx, curr_idx))

    if len(switch_pairs) < cfg.context_switch_threshold:
        return blocks, refine_records

    # 保留前 (threshold - 1) 对，删除后面的
    keep_pairs = switch_pairs[:cfg.context_switch_threshold - 1]
    remove_pairs = switch_pairs[cfg.context_switch_threshold - 1:]

    ids_to_remove: set[str] = set()
    for prev_idx, curr_idx in remove_pairs:
        for idx in [prev_idx, curr_idx]:
            b = blocks[idx]
            bid = b.get("id", "") if isinstance(b, dict) else getattr(b, "id", "")
            ids_to_remove.add(bid)
            # 同时删除对应 toolresult（toolcall 和 toolresult 共享相同 id）
            # 在 toolcall 后面找同 id 的 toolresult
            for k in range(idx + 1, min(idx + 3, len(blocks))):
                nb = blocks[k]
                nbid = nb.get("id", "") if isinstance(nb, dict) else getattr(nb, "id", "")
                ntype = nb.get("type", "") if isinstance(nb, dict) else getattr(nb, "type", "")
                if nbid == bid and ntype == "toolresult":
                    ids_to_remove.add(bid)
                    break

    if ids_to_remove:
        log.info(
            "pruned %d context-switch pairs (kept %d), removing %d blocks",
            len(remove_pairs), len(keep_pairs), len(ids_to_remove),
        )

    pruned_blocks = [b for b in blocks if (b.get("id", "") if isinstance(b, dict) else getattr(b, "id", "")) not in ids_to_remove]
    pruned_records = [r for r in refine_records if r.block_index.block_id not in ids_to_remove]

    return pruned_blocks, pruned_records


def reassemble(
    session: Session,
    refine_records: list[BlockRefineRecord],
    health_scores: list[MessageHealth] = None,
    cfg=None,
    policy_decisions: list[dict] | None = None,
    prune_block_ids: set[str] | None = None,
    deferred_block_ids: set[str] | None = None,
) -> Session | None:
    """组装 + 一致性终检 + 元数据落盘。

    新增参数 (P0 决策层接入):
      policy_decisions     决策层输出, 每个 dict 至少含 block_id / policy / reason / defects
      prune_block_ids      决策为 PRUNE_* 的 block ID 集合 (在元数据落盘前从 blocks 中移除)
      deferred_block_ids   决策为 DEFER_TO_HUMAN 的 block ID 集合 (不修改, 仅标记)

    兼容性: 新参数均为可选; 旧调用方式 (只传前 3 个) 仍可用, 等价于关闭决策层。
    """
    policy_decisions = policy_decisions or []
    prune_block_ids = prune_block_ids or set()
    deferred_block_ids = deferred_block_ids or set()

    if not refine_records and not policy_decisions:
        log.error("discard session %s, reason=all_blocks_invalid", session.session_id)
        return None

    health_scores = health_scores or []

    # 改进2: 对不健康的消息，标记其所有 block 为无效并跳过
    unhealthy_msg_indices: set[int] = set()
    for h in health_scores:
        if not h.is_healthy:
            unhealthy_msg_indices.add(h.msg_idx)
            log.warning(
                "msg[%d] is unhealthy (score=%.2f), all blocks will be skipped",
                h.msg_idx, h.health_score,
            )

    # 改进3: 对每条 assistant 消息执行 block 级剪枝
    for msg_idx, msg in enumerate(session.messages):
        if msg.role != "assistant":
            continue

        # 从 health_scores 中查找本条消息的健康状态
        msg_health = next((h for h in health_scores if h.msg_idx == msg_idx), None)
        has_repetitive = msg_health.has_repetitive_loop if msg_health else False
        has_switch_loop = msg_health.has_context_switch_loop if msg_health else False

        if has_repetitive and cfg:
            msg.blocks, refine_records = _prune_repetitive_blocks(
                msg.blocks, refine_records, cfg,
            )
        if has_switch_loop and cfg:
            msg.blocks, refine_records = _prune_context_switch_blocks(
                msg.blocks, refine_records, cfg,
            )

    # 决策层接入: 应用 policy 层产出的 prune_block_ids (PRUNE_* 决策)
    if prune_block_ids:
        pruned_count = 0
        for msg_idx, msg in enumerate(session.messages):
            if msg.role != "assistant":
                continue
            keep_blocks = []
            for b in msg.blocks:
                bid = b.get("id", "") if isinstance(b, dict) else getattr(b, "id", "")
                if bid in prune_block_ids:
                    pruned_count += 1
                    continue
                keep_blocks.append(b)
            msg.blocks = keep_blocks
        if pruned_count:
            log.info(
                "policy-driven prune: removed %d blocks from session %s",
                pruned_count, session.session_id,
            )

    for record in refine_records:
        idx = record.block_index
        msg = session.messages[idx.msg_idx]

        # 改进2: 跳过不健康消息中的所有 block
        if idx.msg_idx in unhealthy_msg_indices:
            log.info(
                "skipping block %s in unhealthy msg[%d]",
                idx.block_id, idx.msg_idx,
            )
            continue

        block = msg.blocks[idx.block_idx]

        if isinstance(block, dict):
            block_type = block.get("type", "")
        else:
            block_type = getattr(block, "type", "")

        refined = record.refined_content or {}
        if block_type == "thinking":
            if "thinking" in refined:
                if isinstance(block, dict):
                    block["thinking"] = refined["thinking"]
                else:
                    setattr(block, "thinking", refined["thinking"])
        elif block_type == "toolcall":
            if "name" in refined:
                if isinstance(block, dict):
                    block["name"] = refined["name"]
                else:
                    setattr(block, "name", refined["name"])
            if "input" in refined:
                if isinstance(block, dict):
                    block["input"] = refined["input"]
                else:
                    setattr(block, "input", refined["input"])
        elif block_type == "toolresult":
            if "output_text" in refined:
                if isinstance(block, dict):
                    block["output_text"] = refined["output_text"]
                else:
                    setattr(block, "output_text", refined["output_text"])

    from infrastructure import LlamaCppClient
    from prompts import load_and_render

    strict = bool(getattr(cfg, "strict_consistency", True))
    try:
        messages_detail = _build_messages_detail(session)
        prompt = load_and_render("reassembler", "system")
        prompt += "\n\n" + load_and_render(
            "reassembler", "user",
            session_summary=session.summary,
            messages_detail=messages_detail,
        )
        client = LlamaCppClient.get(cfg.judge_model, cfg=cfg, timeout_s=cfg.l3_timeout_s)
        text, meta = client.generate(
            prompt, max_tokens=256, temperature=0.0, timeout_s=cfg.l3_timeout_s,
        )
        result = json.loads(text)
        score = result.get("score", 0)
        if score < 7:
            log.error(
                "discard session %s, reason=consistency_score=%s",
                session.session_id, score,
            )
            return None
    except Exception as e:
        if strict:
            log.error(
                "discard session %s, reason=consistency_check_failed: %s",
                session.session_id, e,
            )
            return None
        log.warning("consistency check failed, proceeding anyway: %s", e)

    _attach_metadata(session, refine_records, policy_decisions, deferred_block_ids)
    return session


def _build_messages_detail(session: Session) -> str:
    parts = []
    for msg in session.messages:
        if msg.role != "assistant":
            continue
        for block in msg.blocks:
            if isinstance(block, dict):
                t = block.get("type", "")
            else:
                t = getattr(block, "type", "")
            if t == "thinking":
                content = block.get("thinking", "") if isinstance(block, dict) else getattr(block, "thinking", "")
                parts.append(f"[thinking] {content[:200]}")
            elif t == "toolcall":
                name = block.get("name", "") if isinstance(block, dict) else getattr(block, "name", "")
                parts.append(f"[toolcall] {name}")
            elif t == "toolresult":
                state = block.get("state", "") if isinstance(block, dict) else getattr(block, "state", "")
                parts.append(f"[toolresult] state={state}")
    return "\n".join(parts)


def _attach_metadata(
    session: Session,
    refine_records: list[BlockRefineRecord],
    policy_decisions: list[dict] | None = None,
    deferred_block_ids: set[str] | None = None,
) -> None:
    total = sum(len(m.blocks) for m in session.messages)
    modified = [r.block_index.block_id for r in refine_records if r.result == "success"]
    l1_total = sum(1 for r in refine_records for v in r.validation_results if v.level == "L1")
    l1_passed = sum(
        1 for r in refine_records
        for v in r.validation_results if v.level == "L1" and v.passed
    )
    l2_total = sum(1 for r in refine_records for v in r.validation_results if v.level == "L2")
    l2_passed = sum(
        1 for r in refine_records
        for v in r.validation_results if v.level == "L2" and v.passed
    )
    l3_total = sum(1 for r in refine_records for v in r.validation_results if v.level == "L3")
    l3_passed = sum(
        1 for r in refine_records
        for v in r.validation_results if v.level == "L3" and v.passed
    )

    session.metadata["refine_history"] = [
        {"module": r.module, "attempts": r.attempts, "model_used": "9B/32B", "result": r.result, "reason": ""}
        for r in refine_records
    ]
    session.metadata["validation_summary"] = {
        "total_blocks": total,
        "modified_blocks": len(modified),
        "passed_L1": l1_passed,
        "passed_L2": l2_passed,
        "passed_L3": l3_passed,
        "failed_L1": l1_total - l1_passed,
        "failed_L2": l2_total - l2_passed,
        "failed_L3": l3_total - l3_passed,
    }
    session.metadata["modified_blocks"] = modified

    # === 新增: 决策层输出 ===
    if policy_decisions:
        session.metadata["policy_decisions"] = policy_decisions
    if deferred_block_ids or policy_decisions:
        deferred_ids = sorted({
            d.get("block_id")
            for d in (policy_decisions or [])
            if d.get("policy") == RefinementPolicy.DEFER_TO_HUMAN.value
        } or list(deferred_block_ids or []))
        if deferred_ids:
            session.metadata["deferred_blocks"] = deferred_ids