import os
import time
import json
import logging
import multiprocessing as mp
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Iterable
from tqdm import tqdm

from config import Settings, load_tools
from infrastructure import setup_logger
from infrastructure.llm_client import set_generation_concurrency
from domain import (
    Session, save_refined_session,
    BlockIndex, BlockRefineRecord, DefectTag,
    ThinkingBlock, ToolcallBlock, ToolresultBlock, Message,
)
from parsers import from_trajectory
from routing import Router
from routing.health import light_health_score_for_session
from refiners import thought_refactor, tool_fixer, obs_denoiser
from validators import validate_block
from reassembly import reassemble, fold_failed_toolresults, fold_repeated_thinking
from reassembly.reassembler import _attach_metadata
from core.context_understanding import build_context_for_session
from core.policy import decide_policy, policy_reason, RefinementPolicy

# PR 3: Langfuse 可观测性 (Commit 2 — _process_one_file outer span).
# 工厂位于 simulate_serve/observability/langfuse_client.py (PR 1 冻结签名);
# gdr 是 workspace 成员, 仅 import 工厂公开 API, 不耦合 simulate_serve 业务。
from simulate_serve.observability.langfuse_client import (
    get_client as _lf_get_client,
    stage_trace as _lf_stage_trace,
)
from gdr.observability.runner_helpers import (
    _current_task_id,
    _gdr_step_span_ctx,
    _payload_mode,
    set_current_task_id as _lf_set_current_task_id,
)

log = logging.getLogger(__name__)


def _has_successful_terminal(session) -> bool:
    """判断 session 是否以成功的最终动作收尾（用于硬过滤豁免）。"""
    for msg in reversed(session.messages):
        if msg.role != "assistant":
            continue
        for b in reversed(msg.blocks):
            if isinstance(b, dict):
                bt = b.get("type", "")
                state = b.get("state", "")
            else:
                bt = getattr(b, "type", "")
                state = getattr(b, "state", "")
            if bt == "toolresult" and state == "success":
                return True
        return False
    return False


def _session_structurally_unusable(session) -> bool:
    """结构严重不可用判定（零 LLM）。True = 没有任何处理与审核价值，硬丢弃。

    保守原则（用户主旨）：只命中三类真正不可用的形态——
      1. 完全没有 assistant 消息（只有 user）；
      2. 所有 assistant 消息都是空壳（无任何 block）；
      3. assistant 消息极少（≤1 条）且没有任何成功信号
         （无 success toolresult、无非空 thinking/text）。
    只要存在可恢复内容就放行；judge 低分不在此列（走 judge_low.jsonl 审核通道）。
    """
    assistants = [m for m in session.messages if getattr(m, "role", "") == "assistant"]
    if not assistants:
        log.warning("hard filter: session %s has no assistant message at all", session.session_id)
        return True
    if all(len(m.blocks) == 0 for m in assistants):
        log.warning(
            "hard filter: session %s assistant messages are all empty shells", session.session_id
        )
        return True
    if len(assistants) <= 1:
        has_signal = False
        for m in assistants:
            for b in m.blocks:
                btype = _block_text_field(b, "type")
                if btype == "toolresult" and _block_text_field(b, "state") == "success":
                    has_signal = True
                    break
                if btype in ("thinking", "text") and _block_text_field(b, btype).strip():
                    has_signal = True
                    break
            if has_signal:
                break
        if not has_signal:
            log.warning(
                "hard filter: session %s has a lone assistant message without any usable "
                "content (no success toolresult, no thinking/text)",
                session.session_id,
            )
            return True
    return False


def _hard_filter_session(session, cfg: Settings) -> bool:
    """Session 级零 LLM 硬过滤（方案 §5.1）。返回 True 表示通过。

    修复 F（用户主旨）：仅按 block 数上限丢弃，不再因"无 successful terminal"丢弃。
    即便 agent 最终失败跑路，只要数据完整（user≥2、assistant 有成功 toolresult），
    都应进入 refine + reassemble 处理并导出。

    结构严重不可用判定（用户主旨补充）：只有数据本身严重不可用才在这里硬丢弃
    （原始输入文件仍在 origindata，可事后修复重跑）：
      * 完全没有 assistant 消息（只有 user）；
      * 所有 assistant 消息都是空壳（无任何 block）；
      * assistant 消息极少（≤1）且没有任何可用内容信号
        （无成功 toolresult、无非空 thinking/text）。
    judge 低分不属于硬丢弃——那部分走 judge_low.jsonl 审核通道，不丢数据。
    """
    if not getattr(cfg, "session_hard_filter_enabled", True):
        return True
    total_blocks = sum(len(m.blocks) for m in session.messages)
    if total_blocks > cfg.session_max_blocks:
        log.warning(
            "hard filter: session %s too many blocks (%d > %d)",
            session.session_id, total_blocks, cfg.session_max_blocks,
        )
        return False
    if _session_structurally_unusable(session):
        return False
    if getattr(session, "error", None):
        log.warning("hard filter: session %s has error=%s", session.session_id, session.error)
        return False
    for mi, m in enumerate(session.messages):
        if getattr(m, "error", None):
            log.warning(
                "hard filter: session %s msg[%d] has error=%s",
                session.session_id, mi, m.error,
            )
            return False
    return True


def _block_text_field(block, key: str, default: str = "") -> str:
    if isinstance(block, dict):
        return block.get(key, default) or default
    return getattr(block, key, default) or default


def _prepare_repair_item(
    block, block_type: str, block_id: str,
    defects: list[DefectTag], bi: BlockIndex, context: dict,
) -> dict | None:
    """决策层判定 REPAIR 后选择 refiner 模块并打包待修 item。

    返回 None 表示该块无需/无法精修 (与原实现中的 continue 语义一致)。
    """
    defect_values = [d.value for d in defects if isinstance(d, DefectTag)]

    if block_type == "thinking" and any(
        d in [DefectTag.THOUGHT_TOO_SHORT, DefectTag.THOUGHT_TOO_LONG, DefectTag.THOUGHT_BROKEN_LOGIC]
        for d in defects
    ):
        if isinstance(block, dict):
            tb = ThinkingBlock(**{k: v for k, v in block.items() if k in ("type", "id", "thinking")})
        else:
            tb = block
        return {
            "bi": bi, "module": "thought_refactor", "block": block, "tb": tb,
            "original": {"thinking": tb.thinking}, "context": context,
            "defect_values": defect_values,
        }

    if block_type == "toolcall" and any(
        d in [
            DefectTag.TOOL_JSON_INVALID, DefectTag.TOOL_HALLUCINATED,
            DefectTag.API_HALLUCINATION, DefectTag.TOOL_WRONG_SELECTION,
            DefectTag.REPETITIVE_CALL,
        ] for d in defects
    ):
        if DefectTag.CONTEXT_SWITCH_LOOP in defects:
            return None
        if isinstance(block, dict):
            tb = ToolcallBlock(**{k: v for k, v in block.items() if k in ("type", "id", "name", "input", "state")})
        else:
            tb = block
        return {
            "bi": bi, "module": "tool_fixer", "block": block, "tb": tb,
            "original": {"name": tb.name, "input": tb.input}, "context": context,
            "defect_values": defect_values,
        }

    if block_type == "toolresult" and any(
        d in [DefectTag.OBS_NOISE, DefectTag.OBS_DEBUG_LEAK] for d in defects
    ):
        if isinstance(block, dict):
            tb = ToolresultBlock(**{k: v for k, v in block.items() if k in ("type", "id", "name", "output_text", "state")})
        else:
            tb = block
        return {
            "bi": bi, "module": "obs_denoiser", "block": block, "tb": tb,
            "original": {"output_text": tb.output_text}, "context": context,
            "defect_values": defect_values,
        }

    if block_type == "text" and DefectTag.TEXT_FACT_HALLUCINATION in defects:
        log.warning(
            "text block %s contains TEXT_FACT_HALLUCINATION, "
            "marking as failed (requires manual review)",
            block_id,
        )
        return {
            "bi": bi, "module": "text_fact_check", "block": block, "tb": None,
            "original": {"text": _block_text_field(block, "text")[:500]},
            "context": context, "defect_values": defect_values,
        }

    if DefectTag.CONTEXT_SWITCH_LOOP in defects:
        return None

    # 无匹配模块: 与原实现一致, 产出一条 module="" 的 failed 记录
    return {
        "bi": bi, "module": "", "block": block, "tb": None,
        "original": {}, "context": context, "defect_values": defect_values,
    }


def _execute_repair_item(item: dict, cfg, tool_names: list[str], hallu_apis: set[str]):
    """执行单个块的精修 + 验证。线程安全: 只依赖 item 内数据与无状态模块函数。"""
    module = item["module"]
    refined = None
    if module == "thought_refactor":
        val = thought_refactor.refine(
            item["tb"], item["context"], item["defect_values"], cfg,
        )
        refined = {"thinking": val} if val else None
    elif module == "tool_fixer":
        val = tool_fixer.refine(
            item["tb"], item["context"], tool_names, hallu_apis,
            item["defect_values"], cfg,
        )
        refined = val or None
    elif module == "obs_denoiser":
        val = obs_denoiser.refine(
            item["tb"], item["context"], item["defect_values"], cfg,
        )
        refined = {"output_text": val} if val else None
    # module == "" / "text_fact_check": refined 保持 None

    if refined:
        passed, val_results = validate_block(item["block"], refined, tool_names, cfg)
        return refined, val_results, ("success" if passed else "failed")
    return None, [], "failed"


def _run_repairs(
    repair_items: list[dict], cfg, tool_names: list[str], hallu_apis: set[str],
) -> list[BlockRefineRecord]:
    """并发执行精修 (块间独立), 按输入顺序产出 refine_records。"""
    if not repair_items:
        return []

    workers = max(1, min(int(getattr(cfg, "llm_concurrency", 4)), len(repair_items)))

    def _run(item: dict):
        return _execute_repair_item(item, cfg, tool_names, hallu_apis)

    if workers > 1:
        with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="gdr-refine") as pool:
            outcomes = list(pool.map(_run, repair_items))
    else:
        outcomes = [_run(it) for it in repair_items]

    records: list[BlockRefineRecord] = []
    for item, (refined, val_results, result) in zip(repair_items, outcomes):
        records.append(BlockRefineRecord(
            block_index=item["bi"],
            module=item["module"],
            original_content=item["original"],
            refined_content=refined,
            attempts=cfg.max_retries_9b + 1,
            result=result,
            validation_results=val_results,
        ))
    return records


def process_one(
    session: Session, cfg: Settings, tool_names: list[str], hallu_apis: set[str],
    *,
    tool_descriptions: dict[str, str] | None = None,
    off_topic_blacklist: set[str] | None = None,
) -> Session | None:
    t0 = time.perf_counter()
    try:
        # 保留原始 session 深拷贝, 供两层评分系统对比式评分使用
        # (方案 trajectory-scoring-two-layer.md §3). 仅启用时才拷贝 (省内存).
        original_session_for_compare: Session | None = None
        if getattr(cfg, "enable_trajectory_compare", True) or getattr(cfg, "enable_free_quality", True):
            original_session_for_compare = session.model_copy(deep=True)

        # === -1. Session 级硬过滤（方案 §5.1） ===
        with _gdr_step_span_ctx(
            "gdr.hard_filter", session, metadata={"step": "hard_filter"},
        ):
            if not _hard_filter_session(session, cfg):
                log.info("session %s filtered out by hard filter", session.session_id)
                return None

        # === 0. 轻量健康分 (零 LLM) ===
        with _gdr_step_span_ctx(
            "gdr.light_health", session, metadata={"step": "light_health"},
        ):
            light_health = light_health_score_for_session(session, cfg)

        # === 1. 上下文理解·结构层 (引用图/视图/archive, 零 LLM), 供 fold 保护 ===
        context_understanding = None
        if getattr(cfg, "enable_context_understanding", True):
            with _gdr_step_span_ctx(
                "gdr.context_understanding.build", session,
                metadata={"step": "context_understanding_build"},
            ):
                try:
                    context_understanding = build_context_for_session(
                        session, cfg, light_health=light_health, track_state=False,
                    )
                except Exception as e:
                    log.warning("ContextUnderstanding.build failed, falling back: %s", e)
                    context_understanding = None

        # === 2. 会话级折叠 (CU 结构层保护被引用 block) ===
        with _gdr_step_span_ctx(
            "gdr.fold.failed_toolresults", session,
            metadata={"step": "fold_failed_toolresults"},
        ):
            folded = fold_failed_toolresults(session, cfg, cu=context_understanding)
            if folded:
                log.info("folded %d failed toolresult block(s)", folded)

        with _gdr_step_span_ctx(
            "gdr.fold.repeated_thinking", session,
            metadata={"step": "fold_repeated_thinking"},
        ):
            folded_thinking = fold_repeated_thinking(session, cfg, cu=context_understanding)
            if folded_thinking:
                log.info("folded %d consecutive thinking block(s)", folded_thinking)

        # === 2.3 P0 方案 ②: 重试循环 LLM 判剪枝 ===
        # 在 fold 之后、reassembler 之前; 复用 main_model (与 thought_refactor
        # 用的同一个本地 9B 模型, 避免引入新 LLM 依赖). LLM 任何异常/不确定
        # 判定 → 保守 fallback, 不动数据.
        if getattr(cfg, "retry_loop_clip_enabled", True):
            from refiners.retry_loop_clip import clip_session
            from infrastructure import LlamaCppClient
            # PR 3 step 5: gdr.retry_loop_clip outer + 子 generation (在
            # llm_judge_retry_loop 内 step_span(as_type="generation"))
            with _gdr_step_span_ctx(
                "gdr.retry_loop_clip", session,
                metadata={"tool": "retry_loop_clip"},
            ):
                try:
                    clip_client = LlamaCppClient.get(
                        cfg.main_model, cfg=cfg,
                        timeout=int(getattr(cfg, "retry_loop_clip_llm_timeout_s", 60)),
                    )
                    removed_clip = clip_session(
                        session, clip_client,
                        min_consecutive=int(getattr(cfg, "retry_loop_clip_min_consecutive", 5)),
                        max_keep=int(getattr(cfg, "retry_loop_clip_max_keep", 3)),
                        llm_timeout_s=int(getattr(cfg, "retry_loop_clip_llm_timeout_s", 60)),
                    )
                    if removed_clip:
                        log.info("retry_loop_clip removed %d block(s)", removed_clip)
                except Exception as e:
                    log.warning(
                        "retry_loop_clip failed for session %s, skipping: %s",
                        session.session_id, e,
                    )

        # === 2.5 fold 后重切 chunk + 增量状态追踪 (唯一一次 LLM 状态追踪) ===
        # chunk 划分反映折叠后的 session, 避免 fold 掉的块虚增一致性校验的重算长度
        if context_understanding is not None:
            # PR 3 step 6: gdr.cu.retrack_state outer; 子 generation 由
            # commit 9 per_llm_span hook 在 _track_state 内部调用 LLM 时
            # 自动包 (此处不重复加, 避免双 span)。
            with _gdr_step_span_ctx(
                "gdr.cu.retrack_state", session,
                metadata={"step": "retrack_state"},
            ):
                try:
                    context_understanding.retrack_state(session)
                except Exception as e:
                    log.warning(
                        "retrack_state failed for session %s, CU state unavailable: %s",
                        session.session_id, e,
                    )

        # === P0-1.3 + P0-1.1 共享: 启发式 user_intent（截断首条 user, 零 LLM）===
        # router 阶段在 judge / reassembler 之前, 不能调 LLM 抽取更精确的
        # user_intent; 这里用启发式 (截断首条 user 消息前 N 字符) 给
        # TOOL_OFF_TOPIC 嵌入层做意图参考. reassembler 阶段会调 LLM 抽取
        # 更精确版本 (覆盖 metadata.user_intent), 这里的结果仅作为
        # metadata.user_intent_heuristic 留存审计.
        from core.user_intent import heuristic_user_intent
        # PR 3 step 7: gdr.user_intent.heuristic
        with _gdr_step_span_ctx(
            "gdr.user_intent.heuristic", session,
            metadata={"step": "user_intent_heuristic"},
        ):
            user_intent_heuristic = heuristic_user_intent(
                session,
                max_chars=int(getattr(cfg, "user_intent_max_chars", 1500)),
                min_chars=int(getattr(cfg, "user_intent_min_chars_for_extract", 20)),
            )

        # === 3. Router.tag 使用 CU 作为 LLM 评审上下文 ===
        router = Router()
        # PR 3 step 8: gdr.router.tag — metadata 记 candidate_blocks 数量 +
        # vote_concurrency (LLM 投票层 fan-out 上限, 便于 UI 看并发度)
        with _gdr_step_span_ctx(
            "gdr.router.tag", session,
            metadata={
                "tool": "router_tag",
                "candidate_blocks": len([
                    blk for msg in session.messages
                    if msg.role == "assistant"
                    for blk in msg.blocks
                ]),
                "vote_concurrency": int(getattr(cfg, "llm_concurrency", 4)),
            },
        ):
            defects_index, health_scores, routing_abstentions = router.tag(
                session, tool_names, hallu_apis, cfg,
                context_understanding=context_understanding,
                tool_descriptions=tool_descriptions or {},
                off_topic_blacklist=off_topic_blacklist or set(),
                user_intent_heuristic=user_intent_heuristic,
            )
        if user_intent_heuristic:
            session.metadata = session.metadata or {}
            session.metadata["user_intent_heuristic"] = user_intent_heuristic
        # 修复 P1.3: routing 弃权审计挂到 session.metadata, 让后续 judge /
        # reassembler 看到哪些 block 被丢, 而不是只看一行聚合 WARNING 盲猜。
        if routing_abstentions:
            session.metadata = session.metadata or {}
            session.metadata["routing_abstentions"] = {
                "count": len(routing_abstentions),
                "block_ids": sorted({a["block_id"] for a in routing_abstentions if a.get("block_id")}),
                "block_types": sorted({a["block_type"] for a in routing_abstentions if a.get("block_type")}),
            }

        # === 3.2 白名单漂移报告 (只告警+记录, 不参与判定) ===
        # 名单来源自动化后仅剩的过期风险: QwenPaw 新增动态工具而 extra_tools
        # 没跟上。让漂移自己浮出, 而不是以 "sanity check failed" 的迷惑形式出现。
        unknown_tools = _collect_unknown_tool_names(session, tool_names)
        if unknown_tools:
            session.metadata = session.metadata or {}
            session.metadata["unknown_tool_names"] = sorted(unknown_tools)
            log.warning(
                "session %s uses tool(s) outside the whitelist: %s — real QwenPaw "
                "dynamic tools should be added to tools.yaml extra_tools (auto "
                "source: check qwenpaw_agent_json / tool_source)",
                session.session_id, ", ".join(sorted(unknown_tools)),
            )

        unhealthy_msg_indices = {h.msg_idx for h in health_scores if not h.is_healthy}

        policy_decisions: list[dict] = []
        prune_block_ids: set[str] = set()
        deferred_block_ids: set[str] = set()
        repair_items: list[dict] = []

        # === 3.5 决策层 (零 LLM, 串行; 保持块序) ===
        # PR 3 step 9: gdr.policy.decide — metadata 含 defect_total /
        # policy 分布计数 (便于 audit 看剪枝/修复决策密度)
        _defect_total = sum(len(v) for v in defects_index.values())
        with _gdr_step_span_ctx(
            "gdr.policy.decide", session,
            metadata={
                "step": "policy_decide",
                "defect_total": _defect_total,
                "message_count": len(session.messages),
            },
        ):
            for msg_idx, msg in enumerate(session.messages):
                if msg.role != "assistant":
                    continue

                msg_health = next((h for h in health_scores if h.msg_idx == msg_idx), None)
                if msg_health and not msg_health.is_healthy:
                    # 不健康消息整体短路：不再扫描其 block 缺陷，避免无意义精修
                    log.info(
                        "skipping unhealthy msg[%d] entirely (score=%.2f)",
                        msg_idx, msg_health.health_score,
                    )
                    continue

                for blk_idx, block in enumerate(msg.blocks):
                    if isinstance(block, dict):
                        block_type = block.get("type", "")
                        block_id = block.get("id", "")
                    else:
                        block_type = getattr(block, "type", "")
                        block_id = getattr(block, "id", "")

                    defects = defects_index.get(block_id, [])
                    if not defects:
                        continue

                    bi = BlockIndex(msg_idx=msg_idx, block_idx=blk_idx, block_id=block_id, block_type=block_type)
                    context = _build_context(msg.blocks, blk_idx)
                    view = context_understanding.get_view(block_id) if context_understanding else None
                    policy = decide_policy(block, defects, view, retry_exhausted=False, cfg=cfg)
                    reason = policy_reason(policy, defects, view)

                    decision = {
                        "block_id": block_id,
                        "msg_idx": msg_idx,
                        "defects": [d.value for d in defects],
                        "policy": policy.value,
                        "reason": reason,
                        "context_relevance": view.relevance_to_active if view else 0.0,
                    }

                    # PRUNE 策略: 不调用 refiner, 仅记录 + 标记
                    if policy in (RefinementPolicy.PRUNE_BLOCK, RefinementPolicy.PRUNE_WITH_PAIR):
                        prune_block_ids.add(block_id)
                        policy_decisions.append(decision)
                        log.info("policy=PRUNE block_id=%s reason=%s", block_id, reason)
                        continue
                    if policy == RefinementPolicy.PRUNE_MESSAGE:
                        # 整条消息级删除由 reassembler 通过 health_scores 处理, 此处仅标记决策
                        policy_decisions.append(decision)
                        log.info("policy=PRUNE_MESSAGE block_id=%s reason=%s", block_id, reason)
                        continue
                    if policy == RefinementPolicy.DEFER_TO_HUMAN:
                        deferred_block_ids.add(block_id)
                        policy_decisions.append(decision)
                        log.info("policy=DEFER block_id=%s reason=%s", block_id, reason)
                        continue

                    # policy == REPAIR_IN_PLACE
                policy_decisions.append(decision)
                item = _prepare_repair_item(block, block_type, block_id, defects, bi, context)
                if item is not None:
                    repair_items.append(item)

        # PR 3 step 9 收尾: policy.decide span 在循环结束处 __exit__
        # (上下文管理器自动关闭), 此处无须显式 close. 之后进入 step 10
        # _run_repairs 的 span.

        # === 4. 并发精修 + 验证 (块间独立) ===
        # PR 3 step 10: gdr.refine.run_repairs 外层 span. 内层 ThreadPoolExecutor
        # 不为每条 repair_item 单独起 span (避免几千子节点), 仅 metadata 聚合
        # success_count / failure_count. 高级模式 ``langfuse_gdr_per_refine_span``
        # 启用时由 _run_repairs 内部逐条起 (后续可扩展, 默认 false)。
        with _gdr_step_span_ctx(
            "gdr.refine.run_repairs", session,
            metadata={
                "step": "refine_run_repairs",
                "repair_item_count": len(repair_items),
                "per_refine_span": bool(
                    getattr(cfg, "langfuse_gdr_per_refine_span", False)
                ),
            },
        ):
            refine_records = _run_repairs(
                repair_items, cfg, tool_names, hallu_apis,
            )

        # 完全无缺陷且无决策时早退, 并挂上统一 metadata (此前该路径输出无 refine_history/
        # validation_summary)。有 policy_decisions 时 (如全部 PRUNE) 必须继续走
        # reassemble —— 否则剪枝决策会被静默丢弃。
        if not refine_records and not policy_decisions:
            # PR 3 step 12: gdr.early_exit — 无缺陷且 L1 sanity 通过时短路径.
            with _gdr_step_span_ctx(
                "gdr.early_exit", session,
                metadata={"step": "early_exit"},
            ):
                if _l1_sanity_check(session, tool_names, cfg.thought_max_len_l1):
                    log.info("no defects found in session %s", session.session_id)
                    _attach_metadata(session, [], policy_decisions, deferred_block_ids, cfg=cfg)
                    return session
            log.warning(
                "session %s has no defect tags but failed L1 sanity check; "
                "falling back to original session to preserve audit trail",
                session.session_id,
            )
            _attach_metadata(session, [], policy_decisions, deferred_block_ids, cfg=cfg)
            return session

        elapsed = time.perf_counter() - t0
        # PR 3 step 13: gdr.reassemble outer span. 内嵌 3 个 generation 子 span
        # (user_intent_llm / consistency_check / l3_judge) 由 reassembler 内部
        # 调用 step_span(as_type="generation") 自起, 这里只起外层. metadata 含
        # refine_records_count 便于 audit 看精修密度.
        with _gdr_step_span_ctx(
            "gdr.reassemble", session,
            metadata={
                "step": "reassemble",
                "refine_records_count": len(refine_records),
                "policy_decisions_count": len(policy_decisions),
            },
        ):
            result = reassemble(
                session,
                refine_records,
                health_scores,
                cfg,
                policy_decisions=policy_decisions,
                prune_block_ids=prune_block_ids,
                deferred_block_ids=deferred_block_ids,
                cu=context_understanding,
            )
        # 用户主旨: 数据完整即处理并导出. reassembler 内部已有 budget 守护 (一致性前
        # /judge 前), 但中间仍可能耗时; reassembler 已返回时不丢弃, 仅在返回 None
        # (reassembler 完全失败) 且接近超时上限时回退到 original session.
        # 例外: judge 主动判死 (低分 / strict 一致性失败) 的 session 标记了
        # judge_discard, 属于质量决策而非超时失败, 不允许被兜底复活。
        if (
            result is None
            and not session.metadata.get("judge_discard")
            and elapsed > cfg.session_timeout_s * 0.8
        ):
            # PR 3 step 14: gdr.timeout_fallback — 仅当 result is None 且超时时
            # 才进入. metadata 含 elapsed vs session_timeout_s 比例, audit 排查.
            with _gdr_step_span_ctx(
                "gdr.timeout_fallback", session,
                metadata={
                    "step": "timeout_fallback",
                    "elapsed_s": round(elapsed, 1),
                    "timeout_s": int(cfg.session_timeout_s),
                    "judge_discard": bool(
                        session.metadata.get("judge_discard")
                    ),
                },
            ):
                log.warning(
                    "reassembler returned None for session %s after %.1fs (>80%% of %ds); "
                    "falling back to original session to preserve data",
                    session.session_id, elapsed, cfg.session_timeout_s,
                )
                session.metadata = session.metadata or {}
                session.metadata["timeout_partial_save"] = True
                session.metadata["timeout_elapsed_s"] = round(elapsed, 1)
                return session
        # === 两层评分系统 (方案 trajectory-scoring-two-layer.md) ===
        # 第一层对比式 + 第二层独立式, 结果写入 metadata 供下游门控.
        if result is not None:
            result.metadata = result.metadata or {}
            if (
                getattr(cfg, "enable_trajectory_compare", True)
                and original_session_for_compare is not None
            ):
                with _gdr_step_span_ctx(
                    "gdr.trajectory_compare", result,
                    metadata={"step": "trajectory_compare"},
                ):
                    try:
                        from validators.l4_trajectory_compare import compare as traj_compare
                        compare_result = traj_compare(
                            original_session_for_compare, result, refine_records, cfg,
                        )
                        result.metadata["trajectory_compare"] = compare_result.model_dump(mode="json")
                    except Exception as e:
                        log.warning("trajectory compare failed for %s: %s", result.session_id, e)
            if getattr(cfg, "enable_free_quality", True):
                with _gdr_step_span_ctx(
                    "gdr.free_quality", result,
                    metadata={"step": "free_quality"},
                ):
                    try:
                        from validators.free_quality import evaluate as free_eval
                        free_result = free_eval(result, cfg)
                        result.metadata["trajectory_free"] = free_result.model_dump(mode="json")
                        if free_result.decision == "reject":
                            result.metadata["scoring_reject"] = True
                    except Exception as e:
                        log.warning("free quality eval failed for %s: %s", result.session_id, e)

            # === step 22: usage_prune 前移到 gdr (方案 etl-prune-frontload.md) ===
            # 结构裁剪 + 本机路径泛化 (CLAUDE.md 隐私红线), 让 C2 天然是
            # 已精简 + 已脱敏形态. etl 不再做结构裁剪.
            if getattr(cfg, "usage_prune_enabled", True):
                with _gdr_step_span_ctx(
                    "gdr.usage_prune", result,
                    metadata={"step": "usage_prune"},
                ):
                    try:
                        from gdr.refiners.usage_prune import prune_session_in_place
                        usage_prune_stats = prune_session_in_place(result, cfg)
                        result.metadata["usage_prune"] = usage_prune_stats
                    except Exception as e:
                        log.warning(
                            "usage_prune failed for %s: %s; "
                            "continuing without structural pruning",
                            result.session_id, e,
                        )

            # === step 23: 独立式 reject 门控 (方案 etl-prune-frontload.md §5.2) ===
            # 红线违规 / 总分 < 4 → 不写 C2, 转 audit/scoring_reject.jsonl.
            # 让评分真正生效, 而非悬空写 metadata.
            free_decision = (
                (result.metadata.get("trajectory_free") or {}).get("decision")
            )
            if free_decision == "reject":
                with _gdr_step_span_ctx(
                    "gdr.scoring_reject_gate", result,
                    metadata={
                        "step": "scoring_reject_gate",
                        "audit_path": str(
                            getattr(cfg, "scoring_reject_output_path", "")
                        ),
                        "enabled": bool(
                            getattr(cfg, "scoring_reject_audit_enabled", True)
                        ),
                    },
                ):
                    result.metadata["scoring_reject"] = True
                    _append_scoring_reject_queue(result, cfg)
                    log.warning(
                        "session %s rejected by free_quality; "
                        "redirecting to %s, skipping C2 write",
                        result.session_id,
                        getattr(cfg, "scoring_reject_output_path", "?"),
                    )
                # 评分低 (free_quality reject) → status="scoring_reject",
                # 走 audited 终态, 不进 dead (CLAUDE.md "数据保留原则" —
                # 结构合格但评分低的轨迹保留供人工复核).
                _lf_final_result = {
                    "input": str(input_path),
                    "status": "scoring_reject",
                }
                return _lf_final_result

        log.debug(
            "session %s processed in %.2fs",
            session.session_id, elapsed,
            extra={"session_id": session.session_id, "latency_s": round(elapsed, 3)},
        )
        return result

    except Exception as e:
        # PR 3 step 15: gdr.unhandled_error — 顶层异常单独 span 记录,
        # 让运维能定位"pipeline 崩在 step 几".
        try:
            with _gdr_step_span_ctx(
                "gdr.unhandled_error", session,
                metadata={
                    "step": "unhandled_error",
                    "exception_type": type(e).__name__,
                },
            ):
                pass  # 仅 span; 实际异常继续往外传
        except Exception:
            pass  # 二度防护: span 失败不能掩盖原始异常
        log.exception("pipeline error for session %s: %s", session.session_id, e)
        return None


def _collect_unknown_tool_names(session: Session, tool_names: list[str]) -> set[str]:
    """会话中出现但不在白名单里的工具名 (白名单漂移报告, 不参与判定)。

    空白名单 (降级态, 名称校验本来就跳过) 返回空集, 不产生噪声。
    """
    if not tool_names:
        return set()
    unknown: set[str] = set()
    for msg in session.messages:
        if msg.role != "assistant":
            continue
        for block in msg.blocks:
            if isinstance(block, dict):
                block_type = block.get("type", "")
                name = block.get("name", "")
            else:
                block_type = getattr(block, "type", "")
                name = getattr(block, "name", "")
            if block_type == "toolcall" and name and name not in tool_names:
                unknown.add(name)
    return unknown


def _l1_sanity_check(session: Session, tool_names: list[str], thought_max_len_l1: int) -> bool:
    """无 defect tag 时执行的轻量 L1 抽检，防止 router 漏检。

    检查项:
      - 所有 assistant 消息中的 toolcall.input 为合法 JSON
      - toolcall.name 在可用工具列表中
      - thinking 块非空且长度不超过 thought_max_len_l1
    """
    for msg in session.messages:
        if msg.role != "assistant":
            continue
        for block in msg.blocks:
            if isinstance(block, dict):
                block_type = block.get("type", "")
            else:
                block_type = getattr(block, "type", "")

            if block_type == "toolcall":
                if isinstance(block, dict):
                    name = block.get("name", "")
                    inp = block.get("input", "")
                else:
                    name = getattr(block, "name", "")
                    inp = getattr(block, "input", "")
                if tool_names and name not in tool_names:
                    log.warning("sanity check failed: tool name %r not in whitelist", name)
                    return False
                try:
                    json.loads(inp)
                except Exception as e:
                    log.warning("sanity check failed: tool input is not valid JSON: %s", e)
                    return False
            elif block_type == "thinking":
                if isinstance(block, dict):
                    thinking = block.get("thinking", "")
                else:
                    thinking = getattr(block, "thinking", "")
                if not thinking or len(thinking) > thought_max_len_l1:
                    log.warning("sanity check failed: thinking empty or too long (%d)", len(thinking))
                    return False
    return True


def _build_context(blocks: list, current_idx: int) -> dict:
    ctx = {"prev_blocks": [], "next_blocks": []}
    for i in range(max(0, current_idx - 2), current_idx):
        b = blocks[i]
        if isinstance(b, dict):
            ctx["prev_blocks"].append({"type": b.get("type"), "id": b.get("id")})
        else:
            ctx["prev_blocks"].append({"type": getattr(b, "type", ""), "id": getattr(b, "id", "")})
    for i in range(current_idx + 1, min(len(blocks), current_idx + 3)):
        b = blocks[i]
        if isinstance(b, dict):
            ctx["next_blocks"].append({"type": b.get("type"), "id": b.get("id")})
        else:
            ctx["next_blocks"].append({"type": getattr(b, "type", ""), "id": getattr(b, "id", "")})
    return ctx


def _append_deferred_queue(session: Session, cfg: Settings) -> None:
    """将 deferred / needs_review block 追加写入人工审核队列 jsonl (方案 §5.5)。

    审核结果可反哺 prompt / 规则更新; 文件不存在时自动创建。
    """
    deferred = session.metadata.get("deferred_blocks") or []
    edit_summary = session.metadata.get("edit_status_summary") or {}
    needs_review_count = edit_summary.get("needs_review", 0)
    if not deferred and not needs_review_count:
        return
    record = {
        "session_id": session.session_id,
        "source_file": getattr(session, "source_file", ""),
        "deferred_blocks": deferred,
        "needs_review_count": needs_review_count,
        "edit_status_summary": edit_summary,
    }
    try:
        path = Path(cfg.deferred_output_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
        log.info(
            "deferred queue appended: %s (%d deferred, %d needs_review)",
            path, len(deferred), needs_review_count,
        )
    except Exception as e:
        log.warning("failed to append deferred queue: %s", e)


def _append_judge_low_queue(session: Session, cfg: Settings) -> None:
    """judge 低分 session 整体导出到审核通道 jsonl（方案 §5.5 的 session 级扩展）。

    用户主旨: judge 分数噪声大, 低分不等于不可用。此类 session 不进主输出
    (训练集), 但完整精修结果落 judge_low.jsonl, 保留后期人工修改/并回的可能。

    Fix A: 把 L3 judge 的 reason / relaxed_kind / modified_blocks 等扁平化到
    judge 字段, 审计时可直接读到 LLM 评语与阶梯档位, 无需再解 metadata.
    """
    if not getattr(cfg, "judge_low_export_enabled", True):
        return
    mark = (session.metadata or {}).get("judge_discard")
    if not mark:
        return
    try:
        # Fix A: 把 mark 展开为顶层字段, 让审计/grep 直接命中
        include_reason = bool(getattr(cfg, "judge_low_include_reason", True))
        judge_payload: dict[str, object] = {
            "score": mark.get("score"),
            "min_score": mark.get("min_score"),
        }
        if include_reason:
            # 兼容旧 judge_discard 中没有 reason / relaxed_kind 字段的情况
            judge_payload["reason"] = mark.get("reason", "")
            judge_payload["relaxed_kind"] = mark.get("relaxed_kind")
            judge_payload["modified_blocks"] = mark.get("modified_blocks")
            if "exception" in mark:
                judge_payload["exception"] = mark.get("exception")
                judge_payload["exception_type"] = mark.get("exception_type")
        record = {
            "session_id": session.session_id,
            "source_file": session.source_file,
            "judge": judge_payload,
            "session": session.model_dump(mode="json"),
        }
        path = Path(cfg.judge_low_output_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
        log.warning(
            "judge-low queue appended: %s (session %s, %s)", path, session.session_id, mark
        )
    except Exception as e:
        log.warning("failed to append judge_low queue: %s", e)


def _append_routing_abstain_queue(session: Session, cfg: Settings) -> None:
    """修复 P1.3: routing 弃权 block 列表 (LLM 解析失败/请求异常) 单独落
    audit jsonl. 不阻塞主流程, 但操作者可按 session_id 复核丢了哪些 block.
    """
    if not getattr(cfg, "routing_abstain_audit_enabled", True):
        return
    abstentions = (session.metadata or {}).get("routing_abstentions")
    if not abstentions or not abstentions.get("count"):
        return
    try:
        record = {
            "session_id": session.session_id,
            "source_file": session.source_file,
            "routing_abstentions": abstentions,
        }
        path = Path(cfg.routing_abstain_audit_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
        log.warning(
            "routing-abstain queue appended: %s (session %s, %d block(s))",
            path, session.session_id, abstentions["count"],
        )
    except Exception as e:
        log.warning("failed to append routing_abstain queue: %s", e)


def _append_scoring_reject_queue(session: Session, cfg: Settings) -> None:
    """独立式评分 reject 旁路 (方案 etl-prune-frontload.md §5.2).

    红线违规 / 总分 < 4 / 子分门槛未达 的 session 不写 C2 refine_data,
    整体转 audit/scoring_reject.jsonl 供事后复核. 与 judge_low / incomplete
    / routing_abstain 同级独立 audit 通道; 记录含 redline.labels 与
    absolute_quality.fail_reasons 便于根因分析.
    """
    if not getattr(cfg, "scoring_reject_audit_enabled", True):
        return
    meta = session.metadata or {}
    free = meta.get("trajectory_free") or {}
    if free.get("decision") != "reject":
        return
    try:
        redline = free.get("redline") or {}
        absolute_quality = free.get("absolute_quality") or {}
        record = {
            "session_id": session.session_id,
            "source_file": session.source_file,
            "scoring_reject": {
                "decision": free.get("decision"),
                "redline_violation": redline.get("violation", False),
                "redline_labels": redline.get("labels", []),
                "absolute_quality_score": absolute_quality.get("score"),
                "absolute_quality_subscores": absolute_quality.get("subscores", {}),
                "absolute_quality_fail_reasons": absolute_quality.get("fail_reasons", []),
            },
            "session": session.model_dump(mode="json"),
        }
        path = Path(getattr(cfg, "scoring_reject_output_path", "./audit/scoring_reject.jsonl"))
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
        log.warning(
            "scoring-reject queue appended: %s (session %s, decision=%s, "
            "redline_violation=%s, score=%s)",
            path, session.session_id,
            free.get("decision"),
            redline.get("violation", False),
            absolute_quality.get("score"),
        )
    except Exception as e:
        log.warning("failed to append scoring_reject queue: %s", e)


# === 未闭合 session 防呆 (方向 #完整性检测) ===
# 复现链 (2026-09-19 T001 useramulation-fc37...): 远端 sim 把"最后还在跑
# shell cleanup 命令" 的 trajectory 当"已完成"提交, gdr 直接处理出
# 159 个 block 的 refine_data, 但末尾 toolcall 缺 toolresult, 最后 text 不
# 构成回复. SFT 用这种半截样本会污染训练. 这里在落盘前做完整性检查.


def _detect_incomplete_session(session: Session) -> dict | None:
    """检测未闭合 session. 返回 None 表示完整, dict 表示未闭合 + 诊断.

    检查维度 (由强到弱):
      1. 末尾 toolcall 缺 toolresult (硬指标 — chain-of-thought 训练数据
         必须三元组配对; 缺失会让模型学到"agent 半截完成任务"模式)
      2. toolcall 总数 > toolresult 总数 (尾部配对缺失, 同 1 的弱化版)
      3. 末尾 assistant text 不构成完整回复 (启发式, 见 _is_text_incomplete)
      4. F2 fix: 末尾 assistant 仅含 thinking (无 final text) 且无未配对 toolcall
         — agent 写了思考但没收口. 复现链 (2026-09-19 T001): 最后一条
         assistant 是 thinking-only 空 content, 原 3 维全部跳过, 误判完整.

    不检测:
      - 单条 user + 单条 assistant (合法短回复)
      - 只有 thinking 无 toolcall (合法纯推理, 但仅限 < threshold)
      - thinking_chars < incomplete_thinking_only_min_chars (快速结尾思考)
    """
    # 找到最后一条 assistant message
    last_asst_idx: int | None = None
    for i in range(len(session.messages) - 1, -1, -1):
        if session.messages[i].role == "assistant":
            last_asst_idx = i
            break
    if last_asst_idx is None:
        # 没有 assistant — 极端异常, 但也不算"未闭合"
        return None

    last_asst = session.messages[last_asst_idx]
    blocks = last_asst.blocks or []
    if not blocks:
        # 空 assistant 不算未闭合, 是 sim 端异常
        return None

    # 全局计数 toolcall / toolresult (fold 后的剩余)
    total_toolcall = 0
    total_toolresult = 0
    for m in session.messages:
        for b in (m.blocks or []):
            t = b.get("type") if isinstance(b, dict) else getattr(b, "type", None)
            if t in ("toolcall", "tool_call"):
                total_toolcall += 1
            elif t in ("toolresult", "tool_result"):
                total_toolresult += 1

    pending_toolcall = max(0, total_toolcall - total_toolresult)

    reasons: list[str] = []

    # 维度 1: 末尾是 toolcall (没等结果回来)
    last_block = blocks[-1]
    last_t = last_block.get("type") if isinstance(last_block, dict) else getattr(last_block, "type", None)
    if last_t in ("toolcall", "tool_call"):
        last_name = last_block.get("name", "?") if isinstance(last_block, dict) else getattr(last_block, "name", "?")
        reasons.append(f"last_assistant_block_is_toolcall:{last_name}")

    # 维度 2: toolcall 总数 ≠ toolresult 总数 (尾部配对缺失)
    # F3-E fix: 当未配对 toolcall 之后紧跟完整收尾的 text 块时, 视为 agent
    # 主动放弃等结果 (例如高危指令被 runtime 拦截后放弃重试), 不算 incomplete.
    # 这种样本是 SFT 想要的"健康妥协"模式, 不应被硬指标误杀.
    # 豁免条件: 末尾是 text, 且 text 含结构闭合/语义收尾信号 (复用 F3-D 启发式).
    if total_toolcall > total_toolresult:
        skip_mismatch_for_complete_close = False
        if last_t == "text":
            text_content = (
                last_block.get("text", "")
                if isinstance(last_block, dict)
                else getattr(last_block, "text", "")
            )
            if _has_complete_close_signal(text_content):
                skip_mismatch_for_complete_close = True
                log.info(
                    "session %s: toolcall/result mismatch (%d vs %d) but last text "
                    "has complete-close signal, treating as agent-intentional closure",
                    getattr(last_asst, "id", "?"),
                    total_toolcall, total_toolresult,
                )
        if not skip_mismatch_for_complete_close:
            reasons.append(
                f"toolcall_result_mismatch:{total_toolcall}_vs_{total_toolresult}"
            )

    # 维度 3: 末尾 assistant text 不构成完整回复 (启发式)
    last_text_block = None
    for b in reversed(blocks):
        t = b.get("type") if isinstance(b, dict) else getattr(b, "type", None)
        if t == "text":
            last_text_block = b
            break
    if last_text_block is not None and last_t != "text":
        # 末尾不是 text, 维度 1/2 已覆盖; 此处跳过
        pass
    elif last_text_block is not None:
        # 末尾是 text, 检查是否像半句话
        text_content = (
            last_text_block.get("text", "")
            if isinstance(last_text_block, dict)
            else getattr(last_text_block, "text", "")
        )
        if _is_text_incomplete(text_content):
            reasons.append("last_text_incomplete")

    # 维度 4 (F2 fix): 末尾 assistant 仅 thinking, 无 final text, 无未配对 toolcall.
    # 复现链: 远端 agent 跑完最后一轮工具后, 写了 reasoning 但没产出面向用户的总结 text,
    # sim 端误标 "已完成". 启发式: 字符阈值由 cfg.incomplete_thinking_only_min_chars 控制.
    if last_text_block is None and pending_toolcall == 0:
        text_chars, thinking_chars, _ = _last_assistant_block_summary(last_asst)
        threshold = int(getattr(_current_runner_cfg(), "incomplete_thinking_only_min_chars", 200))
        if text_chars == 0 and thinking_chars >= threshold:
            reasons.append(
                f"last_assistant_no_final_text:thinking_only:{thinking_chars}chars"
            )

    if not reasons:
        return None
    return {
        "is_incomplete": True,
        "reasons": reasons,
        "last_assistant_msg_idx": last_asst_idx,
        "last_block_type": last_t,
        "total_toolcall": total_toolcall,
        "total_toolresult": total_toolresult,
    }


def _last_assistant_block_summary(last_msg: Message) -> tuple[int, int, int]:
    """返回 (text_chars, thinking_chars, toolcall_count_in_this_msg).

    同时被维度 4 与未来扩展使用; 不读 message 外的全局统计.
    """
    text_chars = 0
    thinking_chars = 0
    tc_count = 0
    for b in (last_msg.blocks or []):
        if isinstance(b, dict):
            btype = b.get("type")
        else:
            btype = getattr(b, "type", None)
        if btype == "text":
            text_chars += len((b.get("text", "") if isinstance(b, dict) else getattr(b, "text", "")) or "")
        elif btype == "thinking":
            thinking_chars += len(
                (b.get("thinking", "") if isinstance(b, dict) else getattr(b, "thinking", "")) or ""
            )
        elif btype in ("toolcall", "tool_call"):
            tc_count += 1
    return text_chars, thinking_chars, tc_count


def _current_runner_cfg() -> Any:
    """从 gdr Settings 取 cfg; 失败时回退到 Namespace 默认值.

    runner 是 gdr 内部模块, 默认应能加载根配置; 单测环境兜底即可.
    """
    try:
        from gdr.config.settings import Settings
        return Settings()
    except Exception:
        from types import SimpleNamespace as _NS
        return _NS(incomplete_thinking_only_min_chars=200)


_INCOMPLETE_TEXT_MARKERS = (
    "我先说清楚一点",
    "继续",  # "继续..." / "继续查"
    "再",  # "再查..." / "再确认"
    "等",  # "等一下" / "等结果"
    "我先",
    "让我",
    "查一下",
    "继续看",
    # F2 fix: 新增自承未出答案的措辞
    "稍后整理",
    "稍后汇总",
    "我先把",  # "我先把结果整理一下"
    "等下",
    "稍等",
    "等一下",
    "稍等一下",
)


# F3-D fix: 完整收尾的结构信号。判定 text 是否"看起来像完整回复", 与
# _is_text_incomplete (判不完整) 是镜像关系。这些都是"出现即完整"的强信号
# — 与 _INCOMPLETE_TEXT_MARKERS (出现即不完整) 对偶。
_COMPLETE_TAIL_TOKENS = (
    "总结", "结论", "已核实", "已完成", "下一步", "锚点", "报告完毕",
    "报告完", "汇报完毕", "汇总完毕", "排查完", "结束",
    "completion complete", "task complete", "done",
    # 中文口语化收尾
    "就这样", "以上", "完毕", "结束", "结果如上",
)


def _has_structural_close(s: str) -> bool:
    """F3-D fix: 尾部是否存在结构性闭合信号 (markdown / bracket pair / etc).

    这些是"出现即完整"的强信号, 优先于字符级标点判断。覆盖:
      - markdown 表格行: 尾部或近尾部含 '|' 行
      - markdown 分隔线: '---' 或 '***' (近尾部即可, 不必在末尾)
      - code fence 闭合: 尾部含 '```'
      - colon-fenced block 闭合: 尾部含 ':::'
      - 中文方括号闭合: 【】、⟦⟧、『』
      - 双重方括号闭合: ]]
    """
    tail = s.rstrip()[-300:] if len(s) > 300 else s.rstrip()
    last_line = tail.splitlines()[-1] if tail else ""
    # markdown 表格行 / 段落收尾的 '|'
    if last_line.strip().startswith("|") and last_line.strip().endswith("|"):
        return True
    # markdown 分隔线 (近尾部即可, 不必在末尾; 否则一段以分隔线开头但
    # 后续还有内容的报告会被漏判)
    for line in tail.splitlines():
        stripped = line.strip()
        if stripped in ("---", "***", "___") or (
            len(stripped) >= 3 and all(c in "-*_" for c in stripped)
        ):
            return True
    # code fence 闭合: 尾部含 '```' 但前面已开 (含 '```' 即可)
    if "```" in tail:
        return True
    # colon-fenced block 闭合
    if tail.rstrip().endswith(":::"):
        return True
    # 中文方括号闭合 (配对出现)
    bracket_pairs = [
        ("⟦", "⟧"), ("【", "】"), ("『", "』"),
        ("「", "」"), ("《", "》"), ("(", ")"),
        ("[", "]"), ("{", "}"),
    ]
    for open_b, close_b in bracket_pairs:
        # open 在文中出现过, close 也出现且出现在 open 之后 → 完整闭合
        oi = s.rfind(open_b)
        ci = s.rfind(close_b)
        if oi != -1 and ci != -1 and ci > oi:
            # 必须末尾或近末尾是 close_b 才算完整收尾
            if tail.endswith(close_b) or close_b in tail[-10:]:
                return True
    return False


def _has_complete_close_signal(s: str) -> bool:
    """F3-E fix: 弱化版的"完整收尾"检测, 用于维度 2 豁免判定.

    与 _has_structural_close 区别: 此函数判断"text 末尾是否有清晰收尾",
    放宽条件 (因为用于豁免一个本来不完全的硬指标, 不能误放真截断).
    比 _is_text_incomplete 更宽松: 任何结构闭合或语义收尾词即视为收口.

    注意: 不设最短长度门槛. 短文本如 "排查完成。下一步: ..." 虽 < 30 字符,
    但含明确语义收尾词, 也应视为完整收口 (因为有结构化的"完成/下一步"信号).
    """
    if not s:
        return False
    if _has_structural_close(s):
        return True
    tail_window = s[-100:]
    if any(tok in tail_window for tok in _COMPLETE_TAIL_TOKENS):
        return True
    return False


def _is_text_incomplete(text: str) -> bool:
    """启发式: text 末尾不像完整回复 (低强度信号, 仅辅助).

    判定的中文半句话/继续词前缀 + 没有结论性结尾 (句号/感叹号/双引号闭合).
    这是非常弱的信号 — 漏判无害 (false negative), 误判只让 session 走
    incomplete.jsonl 旁路 (false positive 由人工复核)。

    F3-D fix: 加入结构性闭合信号 (markdown 表格/分隔线/code fence/bracket
    pair) 与尾部语义收尾词 ("总结"/"结论"/"已完成"/"下一步" 等) — 这些是
    "出现即完整" 的强信号, 弥补单纯看末尾标点对结构化总结报告的盲区。
    """
    s = text.strip()
    if not s:
        return True
    # 末尾是省略号或被截断
    if s.endswith("...") or s.endswith("…"):
        return True
    # 强信号: 结构性闭合 → 视为完整, 不论末尾标点
    if len(s) > 30 and _has_structural_close(s):
        return False
    # 强信号: 尾部含语义收尾词 → 视为完整
    tail_window = s[-100:]
    if len(s) > 30 and any(tok in tail_window for tok in _COMPLETE_TAIL_TOKENS):
        return False
    # 长文本无句末标点 → 大概率被截断
    # 短文本 (<30 chars) 容忍, 可能是 "好的" "OK" 等
    if len(s) > 30:
        sentence_end = set(".!?。！？\"'""''")
        # 末尾 (去除尾部空白) 不在结束标点集 → 不像完整
        if s.rstrip()[-1] not in sentence_end:
            # 容忍明确结尾词
            if not any(s.endswith(w) for w in ("完", "了", "好", "OK", "ok")):
                return True
    # 包含继续性词前缀 + 长文本 (前缀 marker, 仅在文本开头 60 字符)
    if len(s) > 30:
        for marker in _INCOMPLETE_TEXT_MARKERS:
            if marker in s[:60]:
                return True
    return False


def _append_incomplete_queue(session: Session, diagnostic: dict, cfg: Settings) -> None:
    """未闭合 session 完整 dump 到 incomplete.jsonl 旁路 (含 diagnostic).

    与 judge_low 同级但独立; 供远端运维复核, 决定是否补 trajectory / 走
    FOLLOWUP_CREATED 让远端继续跑. 与 judge_low 的关键区别: incomplete 的
    数据更值得保留 (内容质量未知, 不是被判分低), 所以保留完整 session
    而不只是 metadata.
    """
    if not getattr(cfg, "incomplete_detection_enabled", True):
        return
    try:
        record = {
            "session_id": session.session_id,
            "source_file": getattr(session, "source_file", ""),
            "diagnostic": diagnostic,
            "session": session.model_dump(mode="json"),
        }
        path = Path(cfg.incomplete_output_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
        log.warning(
            "incomplete queue appended: %s (session %s, reasons=%s)",
            path, session.session_id, diagnostic["reasons"],
        )
    except Exception as e:
        log.warning("failed to append incomplete queue: %s", e)


# === 多进程 worker 入口 ===

def _process_one_file(input_path: Path, output_path: Path, cfg: Settings) -> dict:
    """单文件处理: load → refine → save。返回 per-file 状态 dict (供 worker 收集)。

    新架构：input 是 trajectory（C1 契约），output 是单 C2 refined Session 文件。
    etl 阶段读 C2 后做格式整理 + 拆 4 视图（C3 契约）。

    PR 3 (Commit 2): 外层包 ``stage_trace`` (``name="gdr.process_one"``),
    ``session_id=""`` 占位 (from_trajectory 后由 21 步骤 helper 通过
    ``session._gdr_cfg`` + ``session.session_id`` 自填; ``session_id`` 由
    propagate_attributes 在子 span 内部重新设, 不依赖 outer 的初始值)。
    """
    log.info("loading trajectory from %s", input_path)
    # === PR 3: outer stage_trace ===
    # session_id 初值 "" 是因为 from_trajectory 之前拿不到 session.session_id;
    # 一旦 from_trajectory 返回, 子步骤 helper 会用 session.session_id 重新
    # 通过 step_span(session_id=...) 起 span. propagate_attributes 仅在子
    # span 期间生效, 不影响 outer trace 的 session_id.
    # input_data=None: C1 trajectory 太大, simulate_serve 已传过; output 由
    # output_capture 闭包从局部变量 ``_lf_final_result`` 拿. 该变量在每条
    # 出口路径上被赋值 (None 表示无结果, 工厂会跳过 output 更新).
    _lf_client = _lf_get_client(cfg)
    _lf_final_result: dict | None = None
    with _lf_stage_trace(
        _lf_client,
        session_id="",
        name="gdr.process_one",
        task_id=_current_task_id(),
        tags=["stage:gdr"],
        metadata={"input_path": str(input_path)},
        input_data=None,
        output_capture=lambda: _lf_final_result,
        payload_mode=_payload_mode(cfg),
    ):
        try:
            session = from_trajectory(input_path)
            session._gdr_cfg = cfg   # 绑定 cfg 给子步骤 helper 读
        except Exception as e:
            log.error("failed to load %s: %s", input_path, e)
            _lf_final_result = {"input": str(input_path), "status": "load_error", "error": str(e)}
            return _lf_final_result

        tool_names, hallu_apis, tool_descriptions, off_topic_blacklist = load_tools(
            cfg.tools_config_path, cfg.qwenpaw_agent_json, cfg.tool_source,
        )
        result = process_one(
            session, cfg, tool_names, hallu_apis,
            tool_descriptions=tool_descriptions,
            off_topic_blacklist=off_topic_blacklist,
        )

        # 修复 P1.3: 任意被处理的 session (含 discard 的 judge_low) 都要把
        # routing 弃权审计落地, 独立于 judge 通道. 单 block 解析失败不再让整
        # session 死, 但失败必须可审计。
        # PR 3 step 16: gdr.audit.routing_abstain — 仅 metadata, 不传 payload
        # (queue 写 jsonl, 避免 input/output 双倍上传)。
        with _gdr_step_span_ctx(
            "gdr.audit.routing_abstain", session,
            metadata={
                "step": "audit_routing_abstain",
                "audit_path": str(getattr(cfg, "routing_abstain_audit_path", "")),
                "enabled": bool(
                    getattr(cfg, "routing_abstain_audit_enabled", True)
                ),
            },
        ):
            _append_routing_abstain_queue(session, cfg)

        if result is not None:
            # 方向 #完整性检测: 在 save_refined_session 之前做未闭合检查. 未闭合
            # session 不写 refine_data (避免 SFT 用"agent 半截完成任务"作为正例),
            # 整体路由到 incomplete.jsonl 旁路, 供运维复核或远端走 FOLLOWUP_CREATED
            # 让远端继续跑. 不阻断主流程 — 只是换个落盘点.
            # PR 3 step 17: gdr.incomplete_check — 纯结构判定, 不传 payload
            if getattr(cfg, "incomplete_detection_enabled", True):
                with _gdr_step_span_ctx(
                    "gdr.incomplete_check", session,
                    metadata={
                        "step": "incomplete_check",
                        "enabled": True,
                    },
                ):
                    diagnostic = _detect_incomplete_session(result)
                if diagnostic is not None:
                    log.warning(
                        "session %s flagged as INCOMPLETE (%s); redirecting to %s, "
                        "skipping refine_data write",
                        result.session_id, diagnostic["reasons"],
                        cfg.incomplete_output_path,
                    )
                    # 把 incomplete.jsonl 落盘也包到 audit span 里 (同 step 17 语义)
                    _append_incomplete_queue(result, diagnostic, cfg)
                    _lf_final_result = {
                        "input": str(input_path),
                        "status": "incomplete",
                        "diagnostic": diagnostic,
                    }
                    return _lf_final_result
            try:
                # PR 3 step 18: gdr.save_refined_session (IO, 只 metadata, 不传 payload;
                # C2 文件已包含完整精修结果, 重复上传浪费带宽)
                with _gdr_step_span_ctx(
                    "gdr.save_refined_session", session,
                    metadata={
                        "step": "save_refined_session",
                        "output_path": str(output_path),
                    },
                ):
                    output = save_refined_session(result, output_path)
                log.info("saved refined session to %s", output)
                # 方案 §5.5: 人工审核队列独立输出 (deferred blocks 追加到 jsonl)
                # PR 3 step 19: gdr.audit.deferred — 仅 metadata
                with _gdr_step_span_ctx(
                    "gdr.audit.deferred", session,
                    metadata={
                        "step": "audit_deferred",
                        "deferred_path": str(
                            getattr(cfg, "deferred_output_path", "")
                        ),
                        "deferred_count": len(deferred_block_ids),
                    },
                ):
                    _append_deferred_queue(result, cfg)
                # P0-1.2: 透传 training_value_score / complexity_tier 到 batch report,
                # 便于训练侧按 tier 抽样 / 监控 quality_scorer 分布.
                meta = result.metadata or {}
                _lf_final_result = {
                    "input": str(input_path),
                    "output": str(output),
                    "status": "success",
                    "complexity_tier": meta.get("complexity_tier"),
                    "training_value_score": meta.get("training_value_score"),
                }
                return _lf_final_result
            except Exception as e:
                log.error("failed to save %s: %s", output_path, e)
                _lf_final_result = {"input": str(input_path), "status": "save_error", "error": str(e)}
                return _lf_final_result
        # result is None 且带 judge 标记 → 不丢数据, 转审核通道
        # PR 3 step 20: gdr.audit.judge_low — 仅 metadata
        with _gdr_step_span_ctx(
            "gdr.audit.judge_low", session,
            metadata={
                "step": "audit_judge_low",
                "judge_low_path": str(
                    getattr(cfg, "judge_low_output_path", "")
                ),
                "enabled": bool(getattr(cfg, "judge_low_export_enabled", True)),
            },
        ):
            _append_judge_low_queue(session, cfg)
        # 评分低 (judge_discard) → status="judge_discard", 走 audited 终态,
        # 不进 dead (CLAUDE.md "数据保留原则" — 结构合格的轨迹保留供人工复核).
        log.warning(
            "session %s discarded by judge (judge_low); "
            "redirecting to audit, not dead",
            input_path,
        )
        _lf_final_result = {
            "input": str(input_path), "status": "judge_discard",
        }
        return _lf_final_result


# === 多进程 worker 入口 ===
def _worker_init(log_dir: Path, llm_concurrency: int) -> None:
    """Pool worker 初始化: 每个 worker 进程独立 setup_logger + 并发上限 + 模型缓存。

    PR 3 (Commit 8): fork-safe 重置 Langfuse singleton. spawn 上下文下子进程
    fork 自 master, 父进程的 Langfuse socket / 后台线程不可跨进程使用;
    通过 ``_reset_for_fork()`` 把 ``_client = None``, 下次 ``get_client`` 自动
    重建. 失败也不抛 (业务兜底).
    """
    setup_logger(log_dir)
    set_generation_concurrency(llm_concurrency)
    try:
        from simulate_serve.observability.langfuse_client import _reset_for_fork
        _reset_for_fork()
    except Exception as exc:
        log.debug("Langfuse _reset_for_fork skipped: %s", exc)
    log.info("worker pid=%d initialized (llm_concurrency=%d)", os.getpid(), llm_concurrency)


def _worker_process_file(args: tuple) -> dict:
    """Pool worker 入口: 从 dict 重建 Settings, 然后走单文件流程。

    PR 3 (Commit 8): try/finally flush Langfuse 客户端. Pool worker 子进程退出
    之前 flush, 否则 21 步骤 spans 可能积压直到 shutdown 才上传, 与多进程并发
    模型不兼容. ``get_client`` 在子进程 ``_worker_init`` 已重置, 这里新建 client.
    """
    input_path_str, output_path_str, cfg_dict = args
    cfg = Settings(**cfg_dict)
    try:
        return _process_one_file(Path(input_path_str), Path(output_path_str), cfg)
    finally:
        # 子进程退出前显式 flush, 不依赖 Langfuse SDK 内部 atexit (multiprocessing
        # spawn 上下文下 atexit 时机不可靠). 失败静默吞, 不影响业务结果返回.
        try:
            from simulate_serve.observability.langfuse_client import (
                get_client as _lf_flush_get_client,
            )
            _lf_client = _lf_flush_get_client(cfg)
            if _lf_client is not None and hasattr(_lf_client, "flush"):
                _lf_client.flush()
        except Exception as exc:
            log.debug("Langfuse flush skipped in worker: %s", exc)


def _aggregate(results: Iterable[dict]) -> dict:
    """汇总 per-file 结果为单次运行的统计 dict。

    P0-1.2: 扩展聚合 tier 分布 + training_value_score 统计 (max/min/avg/
    bucket), 供训练侧按 tier 抽样和回归监控. 仅 success 路径有 tier / score;
    discard / incomplete / error 路径跳过.
    """
    total = 0
    success = 0
    discard = 0
    incomplete = 0
    error = 0
    tier_dist: dict[str, int] = {"easy": 0, "medium": 0, "hard": 0}
    score_sum = 0.0
    score_min: float | None = None
    score_max: float | None = None
    score_buckets = {"[0,0.3)": 0, "[0.3,0.5)": 0, "[0.5,0.7)": 0, "[0.7,1.0]": 0}
    for r in results:
        if r is None:
            continue
        total += 1
        s = r.get("status")
        if s == "success":
            success += 1
            tier = r.get("complexity_tier")
            if tier in tier_dist:
                tier_dist[tier] += 1
            tv = r.get("training_value_score")
            if isinstance(tv, (int, float)):
                score_sum += float(tv)
                if score_min is None or float(tv) < score_min:
                    score_min = float(tv)
                if score_max is None or float(tv) > score_max:
                    score_max = float(tv)
                if tv < 0.3:
                    score_buckets["[0,0.3)"] += 1
                elif tv < 0.5:
                    score_buckets["[0.3,0.5)"] += 1
                elif tv < 0.7:
                    score_buckets["[0.5,0.7)"] += 1
                else:
                    score_buckets["[0.7,1.0]"] += 1
        elif s == "discard":
            discard += 1
        elif s == "incomplete":
            incomplete += 1
        else:
            error += 1
    return {
        "total": total,
        "success": success,
        "discard": discard,
        "incomplete": incomplete,
        "error": error,
        "kept_ratio": round(success / max(total, 1), 4),
        "tier_distribution": tier_dist,
        "training_value_score_summary": {
            "scored_count": sum(tier_dist.values()),
            "avg": round(score_sum / max(sum(tier_dist.values()), 1), 4),
            "min": round(score_min, 4) if score_min is not None else None,
            "max": round(score_max, 4) if score_max is not None else None,
            "buckets": score_buckets,
        },
    }


def _discover_inputs(cfg: Settings) -> list[Path]:
    """根据 cfg 决定输入文件列表, 支持 max_files 截断。

    批量模式消费的是 qf_out Session JSON（单对象，扩展名 ``.json``），
    不是 trajectory JSONL。
    """
    if cfg.batch_input_dir:
        inputs = sorted(p for p in cfg.batch_input_dir.glob("*.json") if p.is_file())
    else:
        inputs = [cfg.input_path]
    if cfg.max_files is not None:
        inputs = inputs[: cfg.max_files]
    return inputs


def _resolve_output(cfg: Settings, input_path: Path) -> Path:
    """新架构 gdr 输出单 C2 refined Session：路径无 ``_refined`` 后缀（etl 阶段加）.

    路径形态：``<batch_output_dir>/<task_id>__<session_id>.json``（直接是单文件，
    不再是 stem；etl 阶段调 ``save_session_v2`` 时再加 ``_refined`` 后缀拆 4 视图）。
    """
    if cfg.batch_input_dir and cfg.batch_output_dir:
        return cfg.batch_output_dir / f"{input_path.stem}.json"
    return cfg.output_path


def run(cfg: Settings) -> dict:
    """主编排入口: 单文件 / 批量目录 / 多进程 Pool。

    根据 cfg.batch_input_dir 是否设置切换批量模式;
    根据 cfg.workers 决定是否用 multiprocessing.Pool。
    """
    inputs = _discover_inputs(cfg)
    if not inputs:
        log.warning("no input files found")
        return _aggregate([])

    set_generation_concurrency(cfg.llm_concurrency)

    if cfg.batch_input_dir:
        cfg.batch_output_dir.mkdir(parents=True, exist_ok=True)

    if cfg.workers <= 1:
        results = []
        for fp in tqdm(inputs, desc="GDR refining"):
            results.append(_process_one_file(fp, _resolve_output(cfg, fp), cfg))
    else:
        log.info(
            "starting multiprocessing.Pool with %d workers (llm_concurrency=%d)",
            cfg.workers, cfg.llm_concurrency,
        )
        ctx = mp.get_context("spawn")  # Windows / Linux 均可用, 模型不跨进程共享
        tasks = [
            (str(fp), str(_resolve_output(cfg, fp)), cfg.model_dump(mode="json"))
            for fp in inputs
        ]
        with ctx.Pool(
            processes=cfg.workers,
            initializer=_worker_init,
            initargs=(cfg.log_dir, cfg.llm_concurrency),
        ) as pool:
            results = list(tqdm(
                pool.imap_unordered(_worker_process_file, tasks),
                total=len(tasks),
                desc=f"GDR refining (workers={cfg.workers})",
            ))

    stats = _aggregate(results)
    log.info(
        "done: kept=%d/%d (%.1f%%), discard=%d, error=%d",
        stats["success"], stats["total"],
        100 * stats["kept_ratio"], stats["discard"], stats["error"],
    )

    # 批量模式下另写一份聚合报告
    if cfg.batch_input_dir:
        report_path = cfg.batch_output_dir / "_batch_report.json"
        report_path.write_text(
            json.dumps(stats, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        log.info("batch report saved to %s", report_path)

    return stats