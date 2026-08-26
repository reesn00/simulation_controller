from domain import BlockUnion, ValidationResult
from validators import l1_rules
from validators import l2_semantic
from infrastructure.http_embed import HttpEmbedder

import logging
log = logging.getLogger(__name__)


def validate_block(
    original_block: BlockUnion,
    refined_content: dict,
    tool_names: list[str],
    cfg,
    embedder: HttpEmbedder | None = None,
) -> tuple[bool, list[ValidationResult]]:
    """Run L1 -> L2 -> (L3) pipeline.

    The caller may pass a pre-built ``embedder`` to share one HTTP client across
    many blocks; otherwise we lazily build (and singleton-cache) one from cfg.
    """
    results: list[ValidationResult] = []

    if cfg.enable_l1:
        l1_passed = l1_rules.check(original_block, refined_content, tool_names, cfg.thought_max_len_l1)
        results.append(ValidationResult(level="L1", passed=l1_passed))
        if not l1_passed:
            return (False, results)

    if cfg.enable_l2:
        if embedder is None:
            from infrastructure.http_embed import get_embedder
            embedder = get_embedder(cfg)

        try:
            l2_passed, l2_sim, l2_threshold = l2_semantic.check(
                original_block, refined_content, embedder,
            )
        except Exception as e:
            # fail-close: any L2 crash must NOT auto-pass.
            detail = f"L2 semantic validator crashed: {e}"
            log.error(detail)
            results.append(ValidationResult(level="L2", passed=False, score=0.0, detail=detail))
            if cfg.enable_l3:
                _run_l3(results, original_block, refined_content, cfg)
                return (results[-1].passed, results)
            return (False, results)

        results.append(ValidationResult(
            level="L2", passed=l2_passed, score=l2_sim,
            detail=f"threshold={l2_threshold}",
        ))

        if not l2_passed:
            if cfg.enable_l3:
                _run_l3(results, original_block, refined_content, cfg)
                return (results[-1].passed, results)
            return (False, results)

    return (True, results)


def _run_l3(results, original_block, refined_content, cfg) -> None:
    """Run L3 judge and append its result. Mutates ``results`` in place."""
    from validators import l3_judge
    l3_result = l3_judge.check(original_block, refined_content, cfg)
    l3_passed = l3_result.get("verdict") == "pass" and l3_result.get("score", 0) >= 7
    results.append(ValidationResult(
        level="L3", passed=l3_passed,
        score=l3_result.get("score"),
        detail=l3_result.get("reason"),
    ))