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


def _fold_msg_failed_toolresults(blocks: list, cu=None) -> tuple[list, set[str]]:
    """单条消息: 折叠同一工具的失败尝试。

    规则:
      - toolcall 与其后紧跟的同 id toolresult 构成一次尝试对 (name, state)。
      - 按 name 连续分组 (被其他 name 打断即开新组)。
      - 组内存在 success 时: 删除组内全部 state=error 的 (toolcall, toolresult),
        保留所有 success 尝试 (含更早的成功, 如环境自检等有效信息)。
      - 组内全为 error 时: 保守保留整组 (无成功结果可保, 不做删除)。
    返回 (新 blocks, 被删除的 block id 集合)。
    """
    pairs: list[tuple[str, str, int, int | None]] = []
    i = 0
    n = len(blocks)
    while i < n:
        b = blocks[i]
        if isinstance(b, dict):
            t = b.get("type", "")
            bid = b.get("id", "")
            name = b.get("name", "")
        else:
            t = getattr(b, "type", "")
            bid = getattr(b, "id", "")
            name = getattr(b, "name", "")
        if t != "toolcall":
            i += 1
            continue

        tr_idx: int | None = None
        state: str | None = None
        for k in range(i + 1, n):
            nb = blocks[k]
            if isinstance(nb, dict):
                nt = nb.get("type", "")
                nbid = nb.get("id", "")
                nstate = nb.get("state", "")
            else:
                nt = getattr(nb, "type", "")
                nbid = getattr(nb, "id", "")
                nstate = getattr(nb, "state", "")
            if nt == "toolcall":
                break
            if nt == "toolresult" and nbid == bid:
                tr_idx = k
                state = nstate
                break
        pairs.append((name, state or "", i, tr_idx))
        i += 1

    groups: list[list[tuple[str, str, int, int | None]]] = []
    for p in pairs:
        if groups and p[0] == groups[-1][-1][0]:
            groups[-1].append(p)
        else:
            groups.append([p])

    ids_to_remove: set[str] = set()
    cu_enabled = getattr(cu.cfg if cu else None, "fold_use_cu", False) if cu else False

    def _is_referenced(bid: str) -> bool:
        if not cu_enabled or cu is None:
            return False
        view = cu.get_view(bid)
        return bool(view and view.referenced_by)

    for g in groups:
        success_count = sum(1 for p in g if p[1] == "success")
        if success_count == 0:
            log.info(
                "keep all-failed tool group name=%s tries=%d (no success to retain)",
                g[0][0], len(g),
            )
            continue
        removed_in_group = 0
        for _, state, tc_idx, tr_idx in g:
            if state != "error":
                continue
            b = blocks[tc_idx]
            bid = b.get("id", "") if isinstance(b, dict) else getattr(b, "id", "")
            if _is_referenced(bid):
                log.debug("fold_failed_toolresults keeps referenced toolcall %s", bid)
                continue
            ids_to_remove.add(bid)
            removed_in_group += 1
            if tr_idx is not None:
                nb = blocks[tr_idx]
                nbid = nb.get("id", "") if isinstance(nb, dict) else getattr(nb, "id", "")
                if _is_referenced(nbid):
                    log.debug("fold_failed_toolresults keeps referenced toolresult %s", nbid)
                    # 回退删除 tc，因为 result 被引用意味着 tc 也应保留
                    ids_to_remove.discard(bid)
                    removed_in_group -= 1
                    continue
                ids_to_remove.add(nbid)
                removed_in_group += 1
        if removed_in_group:
            log.info(
                "folded %d failed %s try block(s), kept %d success(es)",
                removed_in_group, g[0][0], success_count,
            )

    if not ids_to_remove:
        return blocks, set()

    pruned = [b for idx, b in enumerate(blocks)
              if (b.get("id", "") if isinstance(b, dict) else getattr(b, "id", "")) not in ids_to_remove]
    return pruned, ids_to_remove


def fold_failed_toolresults(session: Session, cfg, cu=None) -> int:
    """会话级折叠: 对所有 assistant 消息删除失败/过时的工具尝试,
    保留同一工具组内最后一次成功的 (toolcall, toolresult)。

    Args:
        cu: 可选的 ContextUnderstanding；启用 fold_use_cu 时会保护被引用的 block。

    返回删除的块总数; 并将被删块 id 记录到 metadata, 便于审计。
    """
    total_removed = 0
    removed_ids: set[str] = set()
    for msg in session.messages:
        if msg.role != "assistant":
            continue
        new_blocks, ids = _fold_msg_failed_toolresults(msg.blocks, cu=cu)
        if ids:
            msg.blocks = new_blocks
            total_removed += len(ids)
            removed_ids.update(ids)

    if total_removed:
        session.metadata.setdefault("folded_failed_toolresults", []).extend(sorted(removed_ids))
    return total_removed


def _fold_msg_consecutive_thinking(blocks: list, cu=None) -> tuple[list, set[str]]:
    """单条消息: 折叠连续出现的 thinking 块。

    规则: 相邻的 thinking (中间无其他块) 构成一个连续组,
    组内只保留最后一条 thinking, 删除更早的 thinking。
    删除后同一位置不会残留连续 thinking。
    返回 (新 blocks, 被删除的 block id 集合)。
    """
    cu_enabled = getattr(cu.cfg if cu else None, "fold_use_cu", False) if cu else False

    def _is_referenced(bid: str) -> bool:
        if not cu_enabled or cu is None:
            return False
        view = cu.get_view(bid)
        return bool(view and view.referenced_by)

    ids_to_remove: set[str] = set()
    run_start: int | None = None
    consecutive_ready = False
    for i, b in enumerate(blocks):
        t = b.get("type", "") if isinstance(b, dict) else getattr(b, "type", "")
        if t == "thinking":
            if run_start is None:
                run_start = i
            else:
                consecutive_ready = True
        else:
            if consecutive_ready and run_start is not None:
                for k in range(run_start, i - 1):
                    kb = blocks[k]
                    kid = kb.get("id", "") if isinstance(kb, dict) else getattr(kb, "id", "")
                    if not _is_referenced(kid):
                        ids_to_remove.add(kid)
                    else:
                        log.debug("fold_repeated_thinking keeps referenced thinking %s", kid)
            run_start = None
            consecutive_ready = False

    if consecutive_ready and run_start is not None:
        for k in range(run_start, len(blocks) - 1):
            kb = blocks[k]
            kid = kb.get("id", "") if isinstance(kb, dict) else getattr(kb, "id", "")
            if not _is_referenced(kid):
                ids_to_remove.add(kid)
            else:
                log.debug("fold_repeated_thinking keeps referenced thinking %s", kid)

    if not ids_to_remove:
        return blocks, set()

    pruned = [
        b for b in blocks
        if (b.get("id", "") if isinstance(b, dict) else getattr(b, "id", "")) not in ids_to_remove
    ]
    log.info("folded %d consecutive thinking block(s), kept last per run", len(ids_to_remove))
    return pruned, ids_to_remove


def fold_repeated_thinking(session: Session, cfg, cu=None) -> int:
    """会话级折叠: 对所有 assistant 消息删除连续 thinking 中更早的块,
    每组只保留最后一条 thinking。

    Args:
        cu: 可选的 ContextUnderstanding；启用 fold_use_cu 时会保护被引用的 block。

    返回删除的块总数; 并将被删块 id 记录到 metadata, 便于审计。
    """
    total_removed = 0
    removed_ids: set[str] = set()
    for msg in session.messages:
        if msg.role != "assistant":
            continue
        new_blocks, ids = _fold_msg_consecutive_thinking(msg.blocks, cu=cu)
        if ids:
            msg.blocks = new_blocks
            total_removed += len(ids)
            removed_ids.update(ids)

    if total_removed:
        session.metadata.setdefault("folded_repeated_thinking", []).extend(sorted(removed_ids))
    return total_removed


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