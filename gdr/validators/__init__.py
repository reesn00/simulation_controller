from domain import BlockUnion, ValidationResult, BlockRefineRecord
from validators import l1_rules

try:
    from validators import l2_semantic
except Exception:
    l2_semantic = None


def validate_block(
    original_block: BlockUnion,
    refined_content: dict,
    tool_names: list[str],
    cfg,
) -> tuple[bool, list[ValidationResult]]:
    results: list[ValidationResult] = []

    if cfg.enable_l1:
        l1_passed = l1_rules.check(original_block, refined_content, tool_names, cfg.thought_max_len_l1)
        results.append(ValidationResult(level="L1", passed=l1_passed))
        if not l1_passed:
            return (False, results)

    if cfg.enable_l2:
        if l2_semantic is None:
            # fail-close: L2 依赖缺失时不应默认通过
            detail = "L2 semantic validator unavailable: missing sentence-transformers or scikit-learn"
            log.error(detail)
            results.append(ValidationResult(level="L2", passed=False, score=0.0, detail=detail))
            if cfg.enable_l3:
                log.info("falling back to L3 judge because L2 validator is unavailable")
                from validators import l3_judge
                l3_result = l3_judge.check(original_block, refined_content, cfg)
                l3_passed = l3_result.get("verdict") == "pass" and l3_result.get("score", 0) >= 7
                results.append(ValidationResult(
                    level="L3", passed=l3_passed,
                    score=l3_result.get("score"),
                    detail=l3_result.get("reason"),
                ))
                return (l3_passed, results)
            return (False, results)

        l2_passed, l2_sim, l2_threshold = l2_semantic.check(
            original_block, refined_content, cfg.embedding_model_name,
        )
        results.append(ValidationResult(level="L2", passed=l2_passed, score=l2_sim, detail=f"threshold={l2_threshold}"))
        if not l2_passed:
            if cfg.enable_l3:
                from validators import l3_judge
                l3_result = l3_judge.check(original_block, refined_content, cfg)
                l3_passed = l3_result.get("verdict") == "pass" and l3_result.get("score", 0) >= 7
                results.append(ValidationResult(
                    level="L3", passed=l3_passed,
                    score=l3_result.get("score"),
                    detail=l3_result.get("reason"),
                ))
                return (l3_passed, results)
            return (False, results)

    return (True, results)