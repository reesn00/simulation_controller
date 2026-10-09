from __future__ import annotations

import re


_HIDDEN_TAG_RE = re.compile(
    r"<(?:think|reasoning|analysis)>.*?</(?:think|reasoning|analysis)>",
    re.DOTALL | re.IGNORECASE,
)
_INTERNAL_MARKUP_RE = re.compile(
    r"</?(?:think|reasoning|analysis|tool_call|tool_result)>",
    re.IGNORECASE,
)
_INTERNAL_REASONING_RE = re.compile(
    r"(?:^|\n)\s*(?:"
    r"用户(?:想|希望|要求|需要)我|"
    r"让我(?:先|再|重新|来|做)|"
    r"我(?:需要|应该|决定|意识到)(?:先|再|：|:)"
    r")",
    re.IGNORECASE,
)

INTERNAL_BLOCK_TYPES = frozenset(
    {
        "analysis",
        "reasoning",
        "thinking",
        "tool_call",
        "tool_result",
        "tool_use",
        "data",
        "plugin_call",
        "plugin_call_output",
        "function_call",
        "function_call_output",
        "mcp_tool_call",
        "mcp_tool_call_output",
        "progress",
    }
)


def strip_hidden_markup(value: str) -> str:
    """Remove explicitly delimited non-user-visible model content."""
    return _INTERNAL_MARKUP_RE.sub("", _HIDDEN_TAG_RE.sub("", value)).strip()


def has_internal_reasoning_signals(value: str) -> bool:
    """Conservatively identify responses that should not enter distillation."""
    if _INTERNAL_MARKUP_RE.search(value):
        return True
    return bool(_INTERNAL_REASONING_RE.search(value))


_INTERNAL_LEAK_RE = re.compile(
    r"(验证器|验证准则|验收准则|内部规则|工具内部错误|验证机制|criterion[_ ]?id|validator|准则[\s:]?[a-zA-Z0-9_]{0,8})",
    re.IGNORECASE,
)


def leaks_internal_rules(value: str) -> bool:
    """Detect user-side text that would expose the validation harness to the remote agent."""
    return bool(_INTERNAL_LEAK_RE.search(value))


def _char_ngrams(value: str, n: int) -> set[str]:
    text = re.sub(r"\s+", "", value)
    return {text[i : i + n] for i in range(max(len(text) - n + 1, 0))}


def overlaps_internal_text(
    value: str,
    internal_texts,
    *,
    threshold: float = 0.55,
    n: int = 3,
) -> bool:
    """Content-level leak check: True when `value` substantially echoes one of
    `internal_texts` (criterion descriptions, remediation guidance).

    Keyword checks cannot catch a user turn that reads out the acceptance
    wording itself; char n-gram containment of the *output* against each
    internal text can. The threshold is deliberately below 1.0 so a full
    paraphrase-failure (near-verbatim echo) is caught while short shared
    fragments in a natural reply are not.
    """
    output_grams = _char_ngrams(value, n)
    if not output_grams:
        return False
    for internal in internal_texts:
        internal_grams = _char_ngrams(str(internal), n)
        if not internal_grams:
            continue
        if len(output_grams & internal_grams) / len(output_grams) >= threshold:
            return True
    return False
