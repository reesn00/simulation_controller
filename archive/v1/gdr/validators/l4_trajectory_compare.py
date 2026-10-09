"""轨迹级对比式评分 (方案 trajectory-scoring-two-layer.md §2.1).

整合三维度:
  - fidelity: 任务意图保真度 (LLM 抽取三要素并比对)
  - instruction_adherence: 指令遵循度 (复用 l4_diff_classifier)
  - coherence_delta: 轨迹逻辑增益 (复用 reassembly 配对扫描做对齐检查)

输出 TrajectoryCompareResult: overall=pass → 进入独立终审, fail → 精修管道.
"""
from __future__ import annotations

import json
import logging
from typing import Any

from domain import (
    Session,
    BlockRefineRecord,
    TrajectoryCompareResult,
    FidelityVerdict,
    InstructionAdherence,
    Breakpoint,
)
from validators.l4_diff_classifier import classify_batch, adherence_score, has_regression

log = logging.getLogger(__name__)


def _extract_elements(session: Session, max_chars: int = 2000) -> dict[str, str]:
    """抽取任务目标 + 关键操作链 + 最终结果集."""
    meta = session.metadata or {}
    goal = ""
    if isinstance(meta.get("user_intent"), dict):
        goal = str(meta["user_intent"].get("text", "") or meta["user_intent"].get("summary", ""))
    if not goal:
        for msg in session.messages:
            if msg.role == "user":
                for blk in msg.blocks:
                    btype = blk.get("type", "") if isinstance(blk, dict) else getattr(blk, "type", "")
                    if btype == "text":
                        goal = blk.get("text", "") if isinstance(blk, dict) else getattr(blk, "text", "")
                        break
                if goal:
                    break
    goal = goal[:max_chars]

    actions: list[str] = []
    for msg in session.messages:
        if msg.role != "assistant":
            continue
        for blk in msg.blocks:
            btype = blk.get("type", "") if isinstance(blk, dict) else getattr(blk, "type", "")
            if btype == "toolcall":
                name = blk.get("name", "") if isinstance(blk, dict) else getattr(blk, "name", "")
                inp = blk.get("input", "") if isinstance(blk, dict) else getattr(blk, "input", "")
                actions.append(f"{name}({inp[:200]})")
    action_chain = " -> ".join(actions)[:max_chars]

    results: list[str] = []
    for msg in reversed(session.messages):
        if msg.role != "assistant":
            continue
        for blk in reversed(msg.blocks):
            btype = blk.get("type", "") if isinstance(blk, dict) else getattr(blk, "type", "")
            if btype == "text":
                text = blk.get("text", "") if isinstance(blk, dict) else getattr(blk, "text", "")
                if text:
                    results.append(text)
                    break
        if results:
            break
    result_set = "\n".join(results)[:max_chars]

    return {"goal": goal, "actions": action_chain, "results": result_set}


def _fidelity_compare(
    original_elements: dict[str, str],
    refined_elements: dict[str, str],
    cfg: Any,
) -> FidelityVerdict:
    """LLM 比对两版要素, 输出保真/失真判断."""
    if not getattr(cfg, "compare_fidelity_llm", True):
        return _fidelity_heuristic(original_elements, refined_elements)
    from prompts import load_and_render, parse_json_object
    from infrastructure import LlamaCppClient
    try:
        system_prompt = load_and_render("trajectory_compare", "system")
        user_prompt = load_and_render(
            "trajectory_compare", "user",
            original_goal=original_elements["goal"],
            original_actions=original_elements["actions"],
            original_results=original_elements["results"],
            refined_goal=refined_elements["goal"],
            refined_actions=refined_elements["actions"],
            refined_results=refined_elements["results"],
        )
        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ]
        client = LlamaCppClient.get(cfg.judge_model, cfg=cfg, timeout=60)
        text, _meta = client.chat(messages, max_tokens=cfg.judge_max_tokens, temperature=0.0)
        result = parse_json_object(text)
        verdict = result.get("verdict", "faithful")
        if verdict not in ("faithful", "degraded"):
            verdict = "faithful"
        return FidelityVerdict(
            verdict=verdict,
            lost_elements=list(result.get("lost_elements", [])),
            preserved_core=list(result.get("preserved_core", [])),
        )
    except Exception as e:
        log.warning("fidelity LLM compare failed: %s; falling back to heuristic", e)
        return _fidelity_heuristic(original_elements, refined_elements)


def _fidelity_heuristic(
    original_elements: dict[str, str],
    refined_elements: dict[str, str],
) -> FidelityVerdict:
    """零 LLM 启发式: 目标非空且修改后目标为空 → degraded; 否则 faithful."""
    lost: list[str] = []
    preserved: list[str] = []
    if original_elements["goal"] and not refined_elements["goal"]:
        lost.append("任务目标丢失")
    else:
        preserved.append("任务目标保留")
    if original_elements["results"] and not refined_elements["results"]:
        lost.append("最终结果集丢失")
    else:
        preserved.append("最终结果集保留")
    return FidelityVerdict(
        verdict="degraded" if lost else "faithful",
        lost_elements=lost,
        preserved_core=preserved,
    )


def check_alignment(session: Session) -> list[Breakpoint]:
    """action-observation 对齐检查: 每 toolcall 必有同 id toolresult 且 state=success.

    跨 toolcall 连续扫描 (复用 reassembly 配对逻辑): 从 toolcall 向后扫,
    跨过后续 toolcall, 仅非工具块打断配对.
    """
    breakpoints: list[Breakpoint] = []
    step = 0
    all_blocks: list[tuple[int, str, str, str]] = []
    for msg in session.messages:
        if msg.role != "assistant":
            step += len(msg.blocks)
            continue
        for blk in msg.blocks:
            btype = blk.get("type", "") if isinstance(blk, dict) else getattr(blk, "type", "")
            bid = blk.get("id", "") if isinstance(blk, dict) else getattr(blk, "id", "")
            bstate = blk.get("state", "") if isinstance(blk, dict) else getattr(blk, "state", "")
            all_blocks.append((step, btype, bid, bstate))
            step += 1

    for i, (s, bt, bid, _bs) in enumerate(all_blocks):
        if bt != "toolcall":
            continue
        paired = False
        for j in range(i + 1, len(all_blocks)):
            ns, nbt, nbid, nbs = all_blocks[j]
            if nbt not in ("toolcall", "toolresult"):
                break
            if nbt == "toolresult" and nbid == bid:
                paired = True
                if nbs != "success":
                    breakpoints.append(Breakpoint(
                        step=s,
                        issue=f"toolcall {bid} 的 toolresult state={nbs} (非 success)",
                    ))
                break
        if not paired:
            breakpoints.append(Breakpoint(
                step=s,
                issue=f"toolcall {bid} 缺少配对 toolresult",
            ))
    return breakpoints


def compare(
    original_session: Session,
    refined_session: Session,
    refine_records: list[BlockRefineRecord],
    cfg: Any,
    pair_id: str = "",
) -> TrajectoryCompareResult:
    """轨迹级对比式评分主入口."""
    if not pair_id:
        pair_id = f"{getattr(original_session, 'session_id', '?')}_vs_{getattr(refined_session, 'session_id', '?')}"

    orig_elements = _extract_elements(original_session)
    ref_elements = _extract_elements(refined_session)

    fidelity = _fidelity_compare(orig_elements, ref_elements, cfg)

    diff_items = classify_batch(refine_records, cfg)
    adherence = InstructionAdherence(
        diff_summary=diff_items,
        score=adherence_score(diff_items),
    )

    orig_breakpoints = check_alignment(original_session)
    ref_breakpoints = check_alignment(refined_session)
    orig_bp_count = len(orig_breakpoints)
    ref_bp_count = len(ref_breakpoints)
    if ref_bp_count < orig_bp_count:
        coherence_delta = "improved"
    elif ref_bp_count == orig_bp_count:
        coherence_delta = "unchanged"
    else:
        coherence_delta = "degraded"

    overall = "pass"
    if fidelity.verdict == "degraded":
        overall = "fail"
    if adherence.score == "fail" or has_regression(diff_items):
        overall = "fail"
    if coherence_delta == "degraded":
        overall = "fail"

    return TrajectoryCompareResult(
        pair_id=pair_id,
        fidelity=fidelity,
        instruction_adherence=adherence,
        coherence_delta=coherence_delta,
        breakpoints=ref_breakpoints,
        overall=overall,
    )
