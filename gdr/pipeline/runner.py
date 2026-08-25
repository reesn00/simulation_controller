import os
import time
import json
import logging
import multiprocessing as mp
from pathlib import Path
from typing import Iterable
from tqdm import tqdm

from config import Settings, load_tools
from infrastructure import setup_logger
from domain import (
    Session, load_session, save_session,
    BlockIndex, BlockRefineRecord, DefectTag,
    ThinkingBlock, ToolcallBlock, ToolresultBlock, TextBlock,
)
from routing import Router
from refiners import thought_refactor, tool_fixer, obs_denoiser
from validators import validate_block
from reassembly import reassemble, fold_failed_toolresults, fold_repeated_thinking
from context_understanding import build_context_for_session
from policy import decide_policy, policy_reason, RefinementPolicy

log = logging.getLogger(__name__)


def process_one(
    session: Session, cfg: Settings, tool_names: list[str], hallu_apis: set[str],
) -> Session | None:
    t0 = time.perf_counter()
    try:
        router = Router()
        defects_index, health_scores = router.tag(session, tool_names, hallu_apis, cfg)

        # === 会话级折叠: 删除同一工具组内失败/过时的 toolresult,
        #     保留最后一次成功的 (toolcall, toolresult)。健康与不健康消息均生效。===
        folded = fold_failed_toolresults(session, cfg)
        if folded:
            log.info("folded %d failed toolresult block(s)", folded)

        # === 会话级折叠: 连续 thinking 只保留最后一条, 消除重复思考。===
        folded_thinking = fold_repeated_thinking(session, cfg)
        if folded_thinking:
            log.info("folded %d consecutive thinking block(s)", folded_thinking)

        # === 上下文理解 + 决策层 (P0) ===
        unhealthy_msg_indices = {h.msg_idx for h in health_scores if not h.is_healthy}
        context_understanding = None
        if getattr(cfg, "enable_context_understanding", True):
            try:
                context_understanding = build_context_for_session(
                    session, cfg, unhealthy_msg_indices=unhealthy_msg_indices,
                )
            except Exception as e:
                log.warning("ContextUnderstanding.build failed, falling back: %s", e)
                context_understanding = None

        policy_decisions: list[dict] = []
        prune_block_ids: set[str] = set()
        deferred_block_ids: set[str] = set()

        refine_records: list[BlockRefineRecord] = []

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

                # === 决策层: 在调用 refiner 前选策略 ===
                view = context_understanding.get_view(block_id) if context_understanding else None
                policy = decide_policy(block, defects, view, retry_exhausted=False, cfg=cfg)
                reason = policy_reason(policy, defects, view)

                # PRUNE 策略: 不调用 refiner, 仅记录 + 标记
                if policy in (RefinementPolicy.PRUNE_BLOCK, RefinementPolicy.PRUNE_WITH_PAIR):
                    prune_block_ids.add(block_id)
                    policy_decisions.append({
                        "block_id": block_id,
                        "msg_idx": msg_idx,
                        "defects": [d.value for d in defects],
                        "policy": policy.value,
                        "reason": reason,
                        "context_relevance": view.relevance_to_active if view else 0.0,
                    })
                    log.info("policy=PRUNE block_id=%s reason=%s", block_id, reason)
                    continue
                if policy == RefinementPolicy.PRUNE_MESSAGE:
                    # 整条消息级删除由 reassembler 通过 health_scores 处理, 此处仅标记决策
                    policy_decisions.append({
                        "block_id": block_id,
                        "msg_idx": msg_idx,
                        "defects": [d.value for d in defects],
                        "policy": policy.value,
                        "reason": reason,
                        "context_relevance": view.relevance_to_active if view else 0.0,
                    })
                    log.info("policy=PRUNE_MESSAGE block_id=%s reason=%s", block_id, reason)
                    continue
                if policy == RefinementPolicy.DEFER_TO_HUMAN:
                    deferred_block_ids.add(block_id)
                    policy_decisions.append({
                        "block_id": block_id,
                        "msg_idx": msg_idx,
                        "defects": [d.value for d in defects],
                        "policy": policy.value,
                        "reason": reason,
                        "context_relevance": view.relevance_to_active if view else 0.0,
                    })
                    log.info("policy=DEFER block_id=%s reason=%s", block_id, reason)
                    continue

                # policy == REPAIR_IN_PLACE: 走原 refiner 逻辑
                policy_decisions.append({
                    "block_id": block_id,
                    "msg_idx": msg_idx,
                    "defects": [d.value for d in defects],
                    "policy": policy.value,
                    "reason": reason,
                    "context_relevance": view.relevance_to_active if view else 0.0,
                })

                refined = None
                module_name = ""
                original_content = {}

                if block_type == "thinking" and any(
                    d in [DefectTag.THOUGHT_TOO_SHORT, DefectTag.THOUGHT_TOO_LONG, DefectTag.THOUGHT_BROKEN_LOGIC]
                    for d in defects
                ):
                    module_name = "thought_refactor"
                    if isinstance(block, dict):
                        tb = ThinkingBlock(**{k: v for k, v in block.items() if k in ("type", "id", "thinking")})
                    else:
                        tb = block
                    original_content = {"thinking": tb.thinking}
                    refined_val = thought_refactor.refine(
                        tb, context,
                        [d.value for d in defects if isinstance(d, DefectTag)], cfg,
                    )
                    if refined_val:
                        refined = {"thinking": refined_val}

                elif block_type == "toolcall" and any(
                    d in [
                        DefectTag.TOOL_JSON_INVALID, DefectTag.TOOL_HALLUCINATED,
                        DefectTag.API_HALLUCINATION, DefectTag.TOOL_WRONG_SELECTION,
                        DefectTag.REPETITIVE_CALL,
                    ] for d in defects
                ):
                    if DefectTag.CONTEXT_SWITCH_LOOP in defects:
                        continue

                    module_name = "tool_fixer"
                    if isinstance(block, dict):
                        tb = ToolcallBlock(**{k: v for k, v in block.items() if k in ("type", "id", "name", "input", "state")})
                    else:
                        tb = block
                    original_content = {"name": tb.name, "input": tb.input}
                    refined_val = tool_fixer.refine(
                        tb, context, tool_names, hallu_apis,
                        [d.value for d in defects if isinstance(d, DefectTag)], cfg,
                    )
                    if refined_val:
                        refined = refined_val

                elif block_type == "toolresult" and any(
                    d in [DefectTag.OBS_NOISE, DefectTag.OBS_DEBUG_LEAK] for d in defects
                ):
                    module_name = "obs_denoiser"
                    if isinstance(block, dict):
                        tb = ToolresultBlock(**{k: v for k, v in block.items() if k in ("type", "id", "name", "output_text", "state")})
                    else:
                        tb = block
                    original_content = {"output_text": tb.output_text}
                    refined_val = obs_denoiser.refine(
                        tb, context,
                        [d.value for d in defects if isinstance(d, DefectTag)], cfg,
                    )
                    if refined_val:
                        refined = {"output_text": refined_val}

                elif block_type == "text" and DefectTag.TEXT_FACT_HALLUCINATION in defects:
                    module_name = "text_fact_check"
                    if isinstance(block, dict):
                        tb = TextBlock(**{k: v for k, v in block.items() if k in ("type", "id", "text")})
                    else:
                        tb = block
                    original_content = {"text": tb.text[:500]}
                    log.warning(
                        "text block %s contains TEXT_FACT_HALLUCINATION, "
                        "marking as failed (requires manual review)",
                        block_id,
                    )
                    refined = None

                elif DefectTag.CONTEXT_SWITCH_LOOP in defects:
                    continue

                if refined:
                    passed, val_results = validate_block(block, refined, tool_names, cfg)
                    result = "success" if passed else "failed"
                else:
                    val_results = []
                    result = "failed"

                record = BlockRefineRecord(
                    block_index=bi,
                    module=module_name,
                    original_content=original_content,
                    refined_content=refined,
                    attempts=cfg.max_retries_9b + 1,
                    result=result,
                    validation_results=val_results,
                )
                refine_records.append(record)

        if not refine_records:
            if _l1_sanity_check(session, tool_names, cfg.thought_max_len_l1):
                log.info("no defects found in session %s", session.session_id)
                return session
            log.warning("session %s has no defect tags but failed L1 sanity check", session.session_id)
            return None

        elapsed = time.perf_counter() - t0
        if elapsed > cfg.session_timeout_s:
            log.error("timeout processing session %s (%.1fs)", session.session_id, elapsed)
            return None

        result = reassemble(
            session,
            refine_records,
            health_scores,
            cfg,
            policy_decisions=policy_decisions,
            prune_block_ids=prune_block_ids,
            deferred_block_ids=deferred_block_ids,
        )
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
            return {"input": str(input_path), "output": str(output_path), "status": "success"}
        except Exception as e:
            log.error("failed to save %s: %s", output_path, e)
            return {"input": str(input_path), "status": "save_error", "error": str(e)}
    log.error("session discarded (input=%s)", input_path)
    return {"input": str(input_path), "status": "discard"}


# === 多进程 worker 入口 ===
def _worker_init(log_dir: Path) -> None:
    """Pool worker 初始化: 每个 worker 进程独立 setup_logger + 独立模型缓存。"""
    setup_logger(log_dir)
    log.info("worker pid=%d initialized", os.getpid())


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
    """根据 cfg 决定输入文件列表。"""
    if cfg.batch_input_dir:
        return sorted(p for p in cfg.batch_input_dir.glob("*.json") if p.is_file())
    return [cfg.input_path]


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

    if cfg.batch_input_dir:
        cfg.batch_output_dir.mkdir(parents=True, exist_ok=True)

    if cfg.workers <= 1:
        results = []
        for fp in tqdm(inputs, desc="GDR refining"):
            results.append(_process_one_file(fp, _resolve_output(cfg, fp), cfg))
    else:
        log.info("starting multiprocessing.Pool with %d workers", cfg.workers)
        ctx = mp.get_context("spawn")  # Windows / Linux 均可用, 模型不跨进程共享
        tasks = [
            (str(fp), str(_resolve_output(cfg, fp)), cfg.model_dump(mode="json"))
            for fp in inputs
        ]
        with ctx.Pool(
            processes=cfg.workers,
            initializer=_worker_init,
            initargs=(cfg.log_dir,),
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