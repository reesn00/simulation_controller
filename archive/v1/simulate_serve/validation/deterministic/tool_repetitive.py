"""TOOL_REPETITIVE early-stop detector.

The remote executor may end up repeating the same tool call with effectively
identical input over and over (search engines returning the same snippet,
browser re-navigation loops, etc.). When that run meets or exceeds the
scenario's ``tool_repetitive_threshold``, the deterministic post-processor in
``ValidationPipeline`` flips the criterion result to ``TOOL_REPETITIVE`` so the
Interaction Actor can guide the user to ask the executor to stop and
summarise.

The detector sits next to the other deterministic validators because:

* It must keep ``reason_code`` coverage reachable (the contract test in
  ``tests/contract/test_reason_code_coverage.py`` enforces this); the
  ``TOOL_REPETITIVE`` code lives in ``DETERMINISTIC_REASON_CODES`` and is
  emitted by the pipeline's deterministic stage.
* It needs extra inputs that the text-only validators do not have
  (``toolcall_blocks`` and the scenario threshold); the pipeline instantiates
  this validator per ``validate()`` call with those inputs, so the
  ``.validate(criterion, text)`` interface stays consistent.
* It must never block other validators: it is queried after the text-stage
  finishes and only mutates results when it returns a non-``None``
  ``CriterionResult``.
"""
from __future__ import annotations

import json

from difflib import SequenceMatcher

from simulate_serve.domain.task import AcceptanceCriterion
from simulate_serve.domain.validation import CriterionResult

from .common import failed


# QwenPaw's blocks carry ``type`` in snake_case; some legacy / interop
# payloads use ``toolcall`` / ``tool_use``. Match all three so the detector
# is robust to schema drift across trajectory versions.
_TOOLCALL_BLOCK_TYPES = frozenset({"tool_call", "toolcall", "tool_use"})

# Inputs within this similarity ratio are treated as the same prompt —
# keeps the detector from being fooled by trivial whitespace / case drift
# while ignoring genuinely different queries.
_INPUT_SIMILARITY_THRESHOLD = 0.9


class ToolRepetitiveValidator:
    """Emit ``TOOL_REPETITIVE`` FAIL on a same-tool/consecutive-similar-input loop
    that meets or exceeds the configured threshold.

    The validator is constructed per ``ValidationPipeline.validate()`` call so
    the threshold and toolcall snapshot match the most recent round. ``validate``
    returns ``None`` when no consecutive run reaches the threshold, leaving
    any earlier ``PASSED`` / failed verdict untouched; otherwise it returns a
    ``TOOL_REPETITIVE`` ``FAIL`` result that the pipeline applies to the
    criterion.
    """

    def __init__(
        self,
        toolcall_blocks: list[dict] | tuple[dict, ...] = (),
        threshold: int = 5,
    ) -> None:
        self._toolcall_blocks: tuple[dict, ...] = tuple(toolcall_blocks)
        # Clamp to the legal range the schema enforces (2..20). A scenario
        # could in principle still hand us an out-of-range value through a
        # legacy code path; clamp silently so the validator never crashes the
        # pipeline.
        if threshold < 2:
            self._threshold = 2
        elif threshold > 20:
            self._threshold = 20
        else:
            self._threshold = int(threshold)

    def validate(self, criterion: AcceptanceCriterion, text: str) -> CriterionResult | None:
        longest_run = self._longest_consecutive_run()
        if longest_run < self._threshold:
            return None
        return failed(
            criterion,
            "TOOL_REPETITIVE",
            (
                f"远端在同一回复轮次内连续调用同一工具 {longest_run} 次"
                f"（阈值 {self._threshold}），请停止重复动作并整理已有结果。"
            ),
            retryable=True,
        )

    def _longest_consecutive_run(self) -> int:
        """Longest run of same-name+sighly-similar-input toolcalls in the snapshot.

        The comparison is name-equal AND input-similarity above the threshold.
        Different tools between the same name break the run so a recovery
        ``browser.snapshot`` after ``web_search`` does not extend the count.
        """
        signatures: list[str] = []
        for block in self._toolcall_blocks:
            signature = _toolcall_signature(block)
            if signature is not None:
                signatures.append(signature)
        if not signatures:
            return 0
        longest = 1
        current = 1
        for index in range(1, len(signatures)):
            if _is_same_signature(signatures[index - 1], signatures[index]):
                current += 1
                if current > longest:
                    longest = current
            else:
                current = 1
        return longest


def _toolcall_signature(block: dict) -> str | None:
    """Return ``"{name}|{input}"`` for a toolcall block, or ``None`` otherwise."""
    if not isinstance(block, dict):
        return None
    block_type = str(block.get("type") or "").casefold()
    if block_type not in _TOOLCALL_BLOCK_TYPES:
        return None
    name = _extract_name(block)
    if not name:
        # Unknown-name tool calls are not collapse targets: their payload is
        # not comparable, so leaving them out of the run is safer than
        # pretending two distinct calls are the same.
        return None
    payload = _normalize_input(_extract_input(block))
    return f"{name}|{payload}"


def _extract_name(block: dict) -> str:
    name = block.get("name")
    if isinstance(name, dict):
        # Some shapes wrap the function descriptor: ``{"name": "web_search", ...}``
        nested = name.get("name")
        if isinstance(nested, str):
            return nested.strip()
        return ""
    if isinstance(name, str):
        return name.strip()
    function = block.get("function")
    if isinstance(function, dict):
        nested = function.get("name")
        if isinstance(nested, str):
            return nested.strip()
    return ""


def _extract_input(block: dict) -> object:
    for key in ("input", "arguments", "args"):
        value = block.get(key)
        if value is not None:
            return value
    function = block.get("function")
    if isinstance(function, dict) and function.get("arguments") is not None:
        return function["arguments"]
    return ""


def _normalize_input(value: object) -> str:
    if isinstance(value, str):
        # Case-fold + collapse whitespace so trivial drift in the user's
        # phrasing does not break the same-query run. The detector's job is
        # to flag a "same query over and over" loop, and the same query
        # written in different case / spacing is still the same query.
        return " ".join(value.casefold().split())
    try:
        text = json.dumps(value, sort_keys=True, ensure_ascii=False)
    except (TypeError, ValueError):
        text = str(value)
    return " ".join(text.casefold().split())


def _is_same_signature(left: str, right: str) -> bool:
    name_left, _, payload_left = left.partition("|")
    name_right, _, payload_right = right.partition("|")
    if name_left != name_right:
        return False
    if not payload_left or not payload_right:
        # One of the two calls had no recorded payload (typically the first
        # in a sequence that did not echo back its arguments). Treat them as
        # the same only if both name and an empty payload match — that is the
        # "name-only" fallback case which already implies a degenerate loop.
        return payload_left == payload_right
    if payload_left == payload_right:
        return True
    return SequenceMatcher(None, payload_left, payload_right).ratio() > _INPUT_SIMILARITY_THRESHOLD