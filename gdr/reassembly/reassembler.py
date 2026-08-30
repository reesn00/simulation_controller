import json
import logging
import time
from domain import Session, BlockRefineRecord, MessageHealth, DefectTag, StepEditStatus
from core.policy import RefinementPolicy
from core.context_understanding import GlobalState

log = logging.getLogger(__name__)

# 修复 A: reassemble 内部 timeout 守护（防止 consistency check / judge 卡死导致 30+ 分钟）
_REASSEMBLE_T0: float = 0.0


def _over_session_budget(cfg, t0: float) -> bool:
    """reassemble 内部判断：是否已超过 session_timeout_s 预算。"""
    if cfg is None or t0 <= 0:
        return False
    budget = getattr(cfg, "session_timeout_s", 180)
    return (time.perf_counter() - t0) > budget


def _is_session_too_bad(session) -> bool:
    """判断原 session 是否过于糟糕（应立刻丢弃，不值得保留 refine 成果）。

    触发任一条件即返回 True:
      1. 用户消息 ≤ 1 条（无后续交互可学习）
      2. assistant 消息 ≤ 1 条，且该条无任何成功 toolresult（agent 第一次失败就跑路）
    """
    if session is None or not getattr(session, "messages", None):
        return True

    user_count = sum(1 for m in session.messages if m.role == "user")
    assistant_msgs = [m for m in session.messages if m.role == "assistant"]

    if user_count <= 1:
        return True

    if len(assistant_msgs) <= 1:
        if not assistant_msgs:
            return True
        first = assistant_msgs[0]
        has_success = False
        for b in first.blocks:
            if isinstance(b, dict):
                bt = b.get("type", "")
                state = b.get("state", "")
            else:
                bt = getattr(b, "type", "")
                state = getattr(b, "state", "")
            if bt == "toolresult" and state == "success":
                has_success = True
                break
        if not has_success:
            return True

    return False


def _lost_critical_fields(before, after) -> list[str]:
    """关键字段丢失/冲突检测 (方案 §5.4): 关键实体、约束、任务目标。"""
    lost = []
    before_keys = set(before.key_entities) - set(before.archived_actions)
    after_keys = set(after.key_entities) - set(after.archived_actions)
    if before_keys - after_keys:
        lost.append("key_entities")
    if set(before.critical_constraints) - set(after.critical_constraints):
        lost.append("critical_constraints")
    if before.task_goal and before.task_goal != after.task_goal:
        lost.append("task_goal")
    return lost


def _validate_edit_consistency(
    session: Session,
    refine_records: list[BlockRefineRecord],
    cu,
    cfg,
) -> list[BlockRefineRecord]:
    """编辑后复用状态追踪器校验全局一致性 (方案 §5.4)。

    真增量实现: 从最早被编辑的 chunk 起, 携带状态逐 chunk 只更新一次,
    复杂度 O(N - first_edited) —— 旧实现每次 state_after(ci) 都从 chunk 0
    重放, 是 O(N²) 的 LLM 调用爆炸点。

    起点取 first_edited 前一个 chunk 的编辑前快照: 其之前的 chunk 未被编辑,
    快照仍然有效。first_edited=0 时从空状态开始, 与初始追踪等价。

      - 关键字段丢失/冲突 → 回滚该 Chunk 内所有编辑 (result=rollback, 恢复原文),
        并把当前状态重置为该 chunk 的编辑前快照后继续
      - 状态重算失败 / LLM 调用预算耗尽 → 该 chunk 及之后的成功编辑标记 needs_review

    注意: 调用时编辑已写回 session.blocks, 因此回滚必须把 block 恢复为 original_content。
    """
    edited_chunks: set[int] = set()
    for r in refine_records:
        if r.result != "success":
            continue
        ci = cu.chunk_of_block(r.block_index.block_id)
        if ci is not None:
            edited_chunks.add(ci)
    if not edited_chunks:
        return refine_records

    first_edited = min(edited_chunks)
    budget = max(0, int(getattr(cfg, "consistency_max_llm_calls", 40)))

    if first_edited > 0:
        current = cu.snapshot_at(first_edited - 1)
    else:
        current = None
    if current is None:
        current = GlobalState()

    for ci in range(first_edited, cu.num_chunks):
        if budget <= 0:
            log.warning(
                "consistency LLM budget (%d) exhausted at chunk %d, "
                "marking remaining edits needs_review",
                getattr(cfg, "consistency_max_llm_calls", 40), ci,
            )
            _mark_needs_review_from(refine_records, cu, ci)
            return refine_records

        calls_before = cu.state_tracking_calls
        new_state = cu.update_state_chunk(session, current, ci, cfg)
        budget -= cu.state_tracking_calls - calls_before
        if new_state is None:
            log.warning("state re-track failed at chunk %d, mark needs_review", ci)
            _mark_needs_review_from(refine_records, cu, ci)
            return refine_records

        before = cu.state_snapshots.get(ci)
        if before is not None:
            lost_keys = _lost_critical_fields(before, new_state)
            if lost_keys and getattr(cfg, "consistency_rollback_on_entity_loss", True):
                log.warning(
                    "consistency conflict at chunk %d: %s -> rollback edits in chunk",
                    ci, lost_keys,
                )
                for r in refine_records:
                    if r.block_index.block_id in cu.chunk_blocks.get(ci, []):
                        r.refined_content = None
                        r.result = "rollback"
                        r.edit_status = StepEditStatus.ROLLBACK
                        _restore_block_content(session, r)
                # 回滚后该 chunk 回到编辑前状态, 后续 chunk 以此为起点
                cu.state_snapshots[ci] = before
                current = before
                continue
        current = new_state
    return refine_records


def _mark_needs_review_from(
    refine_records: list[BlockRefineRecord], cu, from_chunk: int,
) -> None:
    """把 from_chunk 及之后的成功编辑标记为 needs_review。"""
    for r in refine_records:
        if r.result != "success":
            continue
        r_ci = cu.chunk_of_block(r.block_index.block_id)
        if r_ci is not None and r_ci >= from_chunk:
            r.edit_status = StepEditStatus.NEEDS_REVIEW


def _find_block_by_id(blocks: list, block_id: str):
    """在块列表中按 id 定位块 (块可能是 dict 或 Pydantic 模型); 未找到返回 None。"""
    for b in blocks:
        bid = b.get("id", "") if isinstance(b, dict) else getattr(b, "id", "")
        if bid == block_id:
            return b
    return None


def _restore_block_content(session: Session, record: BlockRefineRecord) -> None:
    """把 block 恢复为 original_content（用于一致性回滚）。

    按 block_id 定位 (prune 后 block_idx 可能错位, 不能直接用索引)。
    """
    bid = record.block_index.block_id
    for msg in session.messages:
        for block in msg.blocks:
            cur_id = block.get("id", "") if isinstance(block, dict) else getattr(block, "id", "")
            if cur_id != bid:
                continue
            orig = record.original_content or {}
            for key, value in orig.items():
                if isinstance(block, dict):
                    block[key] = value
                else:
                    setattr(block, key, value)
            return


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
    """单条消息: 折叠连续工具调用段中的失败尝试。

    规则:
      - toolcall 与其后紧跟的同 id toolresult 构成一次尝试对 (name, state)。
      - 按"连续工具块"分段, 不看工具名; 出现 toolcall/toolresult 以外的块
        (text/thinking 等) 即打断, 开启新段。
      - 段内以每次 success 为切点划分子段: 子段 = 若干失败 + 结尾的 success,
        删除子段内的失败, 保留该 success (所有 success 都保留)。
      - 段尾若为一串没有 success 收尾的失败, 只保留其中最后一次尝试。
      - 因此整段的最后一次工具调用必定保留。
      - 若 cu 提供, 仅当 block 被 active window 中 thinking/text 引用时, 才豁免删除
        (受 fold_protect_active_text_only 控制; 关闭后回退到任意 referenced_by)。
    返回 (新 blocks, 被删除的 block id 集合)。
    """
    groups: list[list[tuple[str, str, int, int | None]]] = []
    current: list[tuple[str, str, int, int | None]] = []
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
            if t != "toolresult" and current:
                # 非工具块打断连续段
                groups.append(current)
                current = []
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
        current.append((name, state or "", i, tr_idx))
        i += 1
    if current:
        groups.append(current)

    ids_to_remove: set[str] = set()
    cu_enabled = getattr(cu.cfg if cu else None, "fold_use_cu", False) if cu else False
    protect_active_only = (
        getattr(cu.cfg, "fold_protect_active_text_only", True) if cu else True
    )

    def _is_protected(bid: str) -> bool:
        if not cu_enabled or cu is None:
            return False
        view = cu.get_view(bid)
        if view is None:
            return False
        if protect_active_only:
            return bool(view.referenced_by_active_text)
        return bool(view.referenced_by)

    for g in groups:
        # 以每次 success 为切点划分子段, 计算需要保留的下标
        keep_idx: set[int] = set()
        seg_start = 0
        for i, p in enumerate(g):
            if p[1] == "success":
                keep_idx.add(i)  # 子段: [seg_start, i], 保留结尾的 success
                seg_start = i + 1
        if seg_start < len(g):
            # 段尾无 success 收尾的失败串: 只保留最后一次尝试
            keep_idx.add(len(g) - 1)

        removed_in_group = 0
        for i, p in enumerate(g):
            if i in keep_idx:
                continue
            _, state, tc_idx, tr_idx = p
            b = blocks[tc_idx]
            bid = b.get("id", "") if isinstance(b, dict) else getattr(b, "id", "")
            if _is_protected(bid):
                log.debug("fold_failed_toolresults keeps referenced toolcall %s", bid)
                continue
            ids_to_remove.add(bid)
            removed_in_group += 1
            if tr_idx is not None:
                nb = blocks[tr_idx]
                nbid = nb.get("id", "") if isinstance(nb, dict) else getattr(nb, "id", "")
                if _is_protected(nbid):
                    log.debug("fold_failed_toolresults keeps referenced toolresult %s", nbid)
                    # 回退删除 tc，因为 result 被引用意味着 tc 也应保留
                    ids_to_remove.discard(bid)
                    removed_in_group -= 1
                    continue
                ids_to_remove.add(nbid)
                removed_in_group += 1
        if removed_in_group:
            log.info(
                "folded %d failed try block(s) in tool segment of %d "
                "(kept %d: all successes + last attempt)",
                removed_in_group, len(g), len(keep_idx),
            )

    if not ids_to_remove:
        return blocks, set()

    pruned = [b for idx, b in enumerate(blocks)
              if (b.get("id", "") if isinstance(b, dict) else getattr(b, "id", "")) not in ids_to_remove]
    return pruned, ids_to_remove


def fold_failed_toolresults(session: Session, cfg, cu=None) -> int:
    """会话级折叠: 对所有 assistant 消息删除连续工具段中的失败尝试,
    保留所有成功的 (toolcall, toolresult) 以及每段最后一次尝试。

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
    cu=None,
) -> Session | None:
    """组装 + 一致性终检 + 元数据落盘。

    新增参数 (P0 决策层接入):
      policy_decisions     决策层输出, 每个 dict 至少含 block_id / policy / reason / defects
      prune_block_ids      决策为 PRUNE_* 的 block ID 集合 (在元数据落盘前从 blocks 中移除)
      deferred_block_ids   决策为 DEFER_TO_HUMAN 的 block ID 集合 (不修改, 仅标记)
      cu                   ContextUnderstanding 实例; 提供时执行方案 §5.4 编辑前后状态快照校验

    兼容性: 新参数均为可选; 旧调用方式 (只传前 3 个) 仍可用, 等价于关闭决策层。
    """
    policy_decisions = policy_decisions or []
    prune_block_ids = prune_block_ids or set()
    deferred_block_ids = deferred_block_ids or set()

    # 修复 A: 启动内部超时计时器（仿 runner.py:306 风格）
    _REASSEMBLE_T0 = time.perf_counter()

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

    # 按记录写回 refined content。
    # 修复: 必须按 block_id 定位块, 不能用 record.block_index.block_idx 位置索引 ——
    # 上面的 repetitive/context-switch/policy 剪枝会删除块, 使记录中的位置索引失效,
    # 轻则 IndexError 整 session 白跑被丢弃, 重则把内容错写到相邻块。
    for record in refine_records:
        idx = record.block_index

        # 改进2: 跳过不健康消息中的所有 block
        if idx.msg_idx in unhealthy_msg_indices:
            log.info(
                "skipping block %s in unhealthy msg[%d]",
                idx.block_id, idx.msg_idx,
            )
            continue

        if not (0 <= idx.msg_idx < len(session.messages)):
            log.warning(
                "record msg_idx %d out of range for session %s, skip writeback",
                idx.msg_idx, session.session_id,
            )
            continue
        block = _find_block_by_id(session.messages[idx.msg_idx].blocks, idx.block_id)
        if block is None:
            log.warning(
                "block %s pruned/folded before writeback, skip (no content to edit)",
                idx.block_id,
            )
            continue

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

    # === 方案 §5.4: 编辑前后状态快照一致性校验 + 自动回滚 ===
    # 必须在编辑写回 blocks 之后执行; 回滚会把 block 恢复为 original_content
    if cu is not None and cfg is not None and getattr(cfg, "enable_edit_consistency_check", True):
        if _over_session_budget(cfg, _REASSEMBLE_T0):
            log.warning(
                "session %s: over session budget (%.1fs) before consistency check, skipping",
                session.session_id, cfg.session_timeout_s,
            )
        else:
            had_success_before = any(r.result == "success" for r in refine_records)
            try:
                refine_records = _validate_edit_consistency(session, refine_records, cu, cfg)
            except Exception as e:
                log.warning("edit consistency check failed, proceeding without rollback: %s", e)
            # 修复 B: 所有成功编辑都被一致性回滚 → blocks 已恢复为原文（语义安全），
            # 短路跳过终 judge，避免 judge 把"无编辑痕迹"误判 0 分 → 双重丢弃。
            still_success = sum(1 for r in refine_records if r.result == "success")
            if had_success_before and still_success == 0:
                log.warning(
                    "session %s: all %d success edits rolled back by consistency check, "
                    "skipping end-of-pipeline judge (blocks already restored to safe original)",
                    session.session_id, had_success_before,
                )
                _attach_metadata(session, refine_records, policy_decisions, deferred_block_ids)
                return session

    # 方案 §5.5: 标记成功编辑的 edit_status
    for r in refine_records:
        if r.result == "success" and r.edit_status == StepEditStatus.UNTOUCHED:
            r.edit_status = StepEditStatus.EDITED
        elif r.result == "failed" and r.edit_status == StepEditStatus.UNTOUCHED:
            r.edit_status = StepEditStatus.PRESERVED

    from infrastructure import LlamaCppClient
    from prompts import load_and_render

    strict = bool(getattr(cfg, "strict_consistency", True))
    # 修复 A: judge 调用前再判一次 budget，超时则跳过 judge、保留当前已修复的 session
    if _over_session_budget(cfg, _REASSEMBLE_T0):
        log.warning(
            "session %s: over session budget (%.1fs) before judge, returning without final score",
            session.session_id, cfg.session_timeout_s,
        )
        _attach_metadata(session, refine_records, policy_decisions, deferred_block_ids)
        return session
    # 修复 I: judge 调用前再判一次 budget（防止单次 LLM 调用耗时把 budget 耗光）
    # 用户主旨: 数据完整即处理并导出. 一次 LLM 卡顿不应让整 session 丢失.
    try:
        messages_detail = _build_messages_detail(session)
        system_prompt = load_and_render("reassembler", "system")
        user_prompt = load_and_render(
            "reassembler", "user",
            session_summary=session.summary,
            messages_detail=messages_detail,
        )
        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ]
        client = LlamaCppClient.get(cfg.judge_model, cfg=cfg, timeout=cfg.l3_timeout_s)
        # reasoning 模型的思考 token 计入 max_tokens, 预算过小 → content 为空
        text, meta = client.chat(
            messages, max_tokens=2048, temperature=0.0, timeout_s=cfg.l3_timeout_s,
        )
        from prompts import parse_json_object
        result = parse_json_object(text)
        score = result.get("score", 0)
        if score < 7:
            # 修复 E: judge 评分低 ≠ refine 错；仅当原 session 本身过于糟糕时才丢弃。
            if _is_session_too_bad(session):
                log.error(
                    "discard session %s, reason=consistency_score=%s (session too bad)",
                    session.session_id, score,
                )
                return None
            log.warning(
                "judge score %s < 7 for session %s, but session has usable content "
                "(user>=2 + assistant has successes); returning session with refined blocks",
                score, session.session_id,
            )
            _attach_metadata(session, refine_records, policy_decisions, deferred_block_ids)
            return session
    except Exception as e:
        # 修复 I: 网络/LLM 临时超时（TimeoutError + httpx.TimeoutException）一律降级保留。
        # 用户主旨: 数据完整即处理并导出. 一次 LLM 卡顿不应让整 session 丢失.
        # 其他异常按 strict_consistency 处理.
        from infrastructure.llm_client import TimeoutError as _LLMTimeout
        _is_transient = isinstance(e, _LLMTimeout) or e.__class__.__name__ in (
            "TimeoutException", "ReadTimeout", "ConnectTimeout", "RemoteProtocolError",
        )
        if strict and not _is_transient:
            log.error(
                "discard session %s, reason=consistency_check_failed: %s",
                session.session_id, e,
            )
            return None
        if _is_transient:
            log.warning(
                "judge/judge transient failure for session %s (%s); returning session "
                "without final consistency score per 'preserve data' policy",
                session.session_id, e,
            )
        else:
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

    # === 方案 §5.5: edit_status 汇总与版本血缘 ===
    edit_summary: dict[str, int] = {}
    for r in refine_records:
        key = r.edit_status.value if hasattr(r.edit_status, "value") else str(r.edit_status)
        edit_summary[key] = edit_summary.get(key, 0) + 1
    session.metadata["edit_status_summary"] = edit_summary
    session.metadata.setdefault("original_session_id", session.session_id)  # 血缘追溯
    session.metadata.setdefault("refined_version", "v2")

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