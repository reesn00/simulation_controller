import os
import time
import json
import logging
import multiprocessing as mp
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Iterable
from tqdm import tqdm

from config import Settings, load_tools
from infrastructure import setup_logger
from infrastructure.llm_client import set_generation_concurrency
from domain import (
    Session, load_session, save_session,
    BlockIndex, BlockRefineRecord, DefectTag,
    ThinkingBlock, ToolcallBlock, ToolresultBlock,
)
from routing import Router
from routing.health import light_health_score_for_session
from refiners import thought_refactor, tool_fixer, obs_denoiser
from validators import validate_block
from reassembly import reassemble, fold_failed_toolresults, fold_repeated_thinking
from reassembly.reassembler import _attach_metadata
from core.context_understanding import build_context_for_session
from core.policy import decide_policy, policy_reason, RefinementPolicy

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


def _hard_filter_session(session, cfg: Settings) -> bool:
    """Session 级零 LLM 硬过滤（方案 §5.1）。返回 True 表示通过。

    修复 F（用户主旨）：仅按 block 数上限丢弃，不再因"无 successful terminal"丢弃。
    即便 agent 最终失败跑路，只要数据完整（user≥2、assistant 有成功 toolresult），
    都应进入 refine + reassemble 处理并导出。
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
) -> Session | None:
    t0 = time.perf_counter()
    try:
        # === -1. Session 级硬过滤（方案 §5.1） ===
        if not _hard_filter_session(session, cfg):
            log.info("session %s filtered out by hard filter", session.session_id)
            return None

        # === 0. 轻量健康分 (零 LLM) ===
        light_health = light_health_score_for_session(session, cfg)

        # === 1. 上下文理解·结构层 (引用图/视图/archive, 零 LLM), 供 fold 保护 ===
        context_understanding = None
        if getattr(cfg, "enable_context_understanding", True):
            try:
                context_understanding = build_context_for_session(
                    session, cfg, light_health=light_health, track_state=False,
                )
            except Exception as e:
                log.warning("ContextUnderstanding.build failed, falling back: %s", e)
                context_understanding = None

        # === 2. 会话级折叠 (CU 结构层保护被引用 block) ===
        folded = fold_failed_toolresults(session, cfg, cu=context_understanding)
        if folded:
            log.info("folded %d failed toolresult block(s)", folded)

        folded_thinking = fold_repeated_thinking(session, cfg, cu=context_understanding)
        if folded_thinking:
            log.info("folded %d consecutive thinking block(s)", folded_thinking)

        # === 2.5 fold 后重切 chunk + 增量状态追踪 (唯一一次 LLM 状态追踪) ===
        # chunk 划分反映折叠后的 session, 避免 fold 掉的块虚增一致性校验的重算长度
        if context_understanding is not None:
            try:
                context_understanding.retrack_state(session)
            except Exception as e:
                log.warning(
                    "retrack_state failed for session %s, CU state unavailable: %s",
                    session.session_id, e,
                )

        # === 3. Router.tag 使用 CU 作为 LLM 评审上下文 ===
        router = Router()
        defects_index, health_scores = router.tag(
            session, tool_names, hallu_apis, cfg,
            context_understanding=context_understanding,
        )

        unhealthy_msg_indices = {h.msg_idx for h in health_scores if not h.is_healthy}

        policy_decisions: list[dict] = []
        prune_block_ids: set[str] = set()
        deferred_block_ids: set[str] = set()
        repair_items: list[dict] = []

        # === 3.5 决策层 (零 LLM, 串行; 保持块序) ===
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

        # === 4. 并发精修 + 验证 (块间独立) ===
        refine_records = _run_repairs(repair_items, cfg, tool_names, hallu_apis)

        # 完全无缺陷且无决策时早退, 并挂上统一 metadata (此前该路径输出无 refine_history/
        # validation_summary)。有 policy_decisions 时 (如全部 PRUNE) 必须继续走
        # reassemble —— 否则剪枝决策会被静默丢弃。
        if not refine_records and not policy_decisions:
            if _l1_sanity_check(session, tool_names, cfg.thought_max_len_l1):
                log.info("no defects found in session %s", session.session_id)
                _attach_metadata(session, [], policy_decisions, deferred_block_ids)
                return session
            log.warning(
                "session %s has no defect tags but failed L1 sanity check; "
                "falling back to original session to preserve audit trail",
                session.session_id,
            )
            _attach_metadata(session, [], policy_decisions, deferred_block_ids)
            return session

        elapsed = time.perf_counter() - t0
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
        if result is None and elapsed > cfg.session_timeout_s * 0.8:
            log.warning(
                "reassembler returned None for session %s after %.1fs (>80%% of %ds); "
                "falling back to original session to preserve data",
                session.session_id, elapsed, cfg.session_timeout_s,
            )
            session.metadata = session.metadata or {}
            session.metadata["timeout_partial_save"] = True
            session.metadata["timeout_elapsed_s"] = round(elapsed, 1)
            return session
        log.debug(
            "session %s processed in %.2fs",
            session.session_id, elapsed,
            extra={"session_id": session.session_id, "latency_s": round(elapsed, 3)},
        )
        return result

    except Exception as e:
        log.exception("pipeline error for session %s: %s", session.session_id, e)
        return None


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
                if name not in tool_names:
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


def _process_one_file(input_path: Path, output_path: Path, cfg: Settings) -> dict:
    """单文件处理: load → refine → save。返回 per-file 状态 dict (供 worker 收集)。"""
    log.info("loading session from %s", input_path)
    try:
        session = load_session(input_path)
    except Exception as e:
        log.error("failed to load %s: %s", input_path, e)
        return {"input": str(input_path), "status": "load_error", "error": str(e)}

    tool_names, hallu_apis = load_tools(cfg.tools_config_path)
    result = process_one(session, cfg, tool_names, hallu_apis)

    if result is not None:
        try:
            output_path.parent.mkdir(parents=True, exist_ok=True)
            save_session(result, output_path)
            log.info("saved refined session to %s", output_path)
            # 方案 §5.5: 人工审核队列独立输出 (deferred blocks 追加到 jsonl)
            _append_deferred_queue(result, cfg)
            return {"input": str(input_path), "output": str(output_path), "status": "success"}
        except Exception as e:
            log.error("failed to save %s: %s", output_path, e)
            return {"input": str(input_path), "status": "save_error", "error": str(e)}
    log.error("session discarded (input=%s)", input_path)
    return {"input": str(input_path), "status": "discard"}


# === 多进程 worker 入口 ===
def _worker_init(log_dir: Path, llm_concurrency: int) -> None:
    """Pool worker 初始化: 每个 worker 进程独立 setup_logger + 并发上限 + 模型缓存。"""
    setup_logger(log_dir)
    set_generation_concurrency(llm_concurrency)
    log.info("worker pid=%d initialized (llm_concurrency=%d)", os.getpid(), llm_concurrency)


def _worker_process_file(args: tuple) -> dict:
    """Pool worker 入口: 从 dict 重建 Settings, 然后走单文件流程。"""
    input_path_str, output_path_str, cfg_dict = args
    cfg = Settings(**cfg_dict)
    return _process_one_file(Path(input_path_str), Path(output_path_str), cfg)


def _aggregate(results: Iterable[dict]) -> dict:
    """汇总 per-file 结果为单次运行的统计 dict。"""
    total = 0
    success = 0
    discard = 0
    error = 0
    for r in results:
        if r is None:
            continue
        total += 1
        s = r.get("status")
        if s == "success":
            success += 1
        elif s == "discard":
            discard += 1
        else:
            error += 1
    return {
        "total": total,
        "success": success,
        "discard": discard,
        "error": error,
        "kept_ratio": round(success / max(total, 1), 4),
    }


def _discover_inputs(cfg: Settings) -> list[Path]:
    """根据 cfg 决定输入文件列表, 支持 max_files 截断。"""
    if cfg.batch_input_dir:
        inputs = sorted(p for p in cfg.batch_input_dir.glob("*.json") if p.is_file())
    else:
        inputs = [cfg.input_path]
    if cfg.max_files is not None:
        inputs = inputs[: cfg.max_files]
    return inputs


def _resolve_output(cfg: Settings, input_path: Path) -> Path:
    if cfg.batch_input_dir and cfg.batch_output_dir:
        return cfg.batch_output_dir / f"{input_path.stem}_refined.json"
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