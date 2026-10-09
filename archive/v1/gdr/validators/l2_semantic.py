"""L2 semantic validator backed by an HTTP embedding endpoint.

Compares ``cosine_similarity(orig_block_text, refined_block_text)`` against
per-block-type cutoffs:

    thinking   0.85
    toolcall   0.90
    toolresult 0.80

Falls back to "fail" (not "pass") on any infrastructure error so the caller
can escalate to the L3 judge instead of silently letting the block through.
"""
from __future__ import annotations

import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # avoid runtime import cycle in tests
    from infrastructure.http_embed import HttpEmbedder

log = logging.getLogger(__name__)


_THINKING_THRESHOLD = 0.85
_TOOLCALL_THRESHOLD = 0.90
_TOOLRESULT_THRESHOLD = 0.80
# Margin under threshold that still counts as a "soft" fail (L3 may rescue it).
_FAIL_MARGIN = 0.03


def _orig_text(block) -> str:
    if isinstance(block, dict):
        bt = block.get("type", "")
    else:
        bt = getattr(block, "type", "")
    if bt == "thinking":
        return block.get("thinking", "") if isinstance(block, dict) else getattr(block, "thinking", "")
    if bt == "toolcall":
        return block.get("input", "") if isinstance(block, dict) else getattr(block, "input", "")
    if bt == "toolresult":
        return block.get("output_text", "") if isinstance(block, dict) else getattr(block, "output_text", "")
    return ""


def _ref_text(refined: dict, block_type: str) -> str:
    if block_type == "thinking":
        return refined.get("thinking", "") or ""
    if block_type == "toolcall":
        return refined.get("input", "") or ""
    if block_type == "toolresult":
        return refined.get("output_text", "") or ""
    return ""


def check(
    original_block,
    refined_content: dict,
    embedder: "HttpEmbedder",
) -> tuple[bool, float, float]:
    """Run L2 semantic check. Returns (passed, similarity, threshold)."""
    block_type = (
        original_block.get("type", "")
        if isinstance(original_block, dict)
        else getattr(original_block, "type", "")
    )

    if block_type not in ("thinking", "toolcall", "toolresult"):
        return (True, 1.0, 1.0)

    threshold = {
        "thinking": _THINKING_THRESHOLD,
        "toolcall": _TOOLCALL_THRESHOLD,
        "toolresult": _TOOLRESULT_THRESHOLD,
    }[block_type]

    orig_text = _orig_text(original_block)
    ref_text = _ref_text(refined_content, block_type)

    # Empty on either side → cannot judge; treat as fail so L3 / caller decides.
    if not orig_text or not ref_text:
        return (False, 0.0, threshold)

    try:
        orig_emb = embedder.embed(orig_text)
        ref_emb = embedder.embed(ref_text)
    except Exception as e:
        # fail-close: any infrastructure error must NOT silently pass.
        log.error("L2 semantic embed failed for %s block: %s", block_type, e)
        return (False, 0.0, threshold)

    if not orig_emb or not ref_emb:
        return (False, 0.0, threshold)

    sim = embedder.cosine(orig_emb, ref_emb)

    if sim >= threshold:
        return (True, sim, threshold)
    if sim >= threshold - _FAIL_MARGIN:
        # Soft fail: embedder was ambiguous; still let L3 / caller decide.
        return (False, sim, threshold)
    return (False, sim, threshold)