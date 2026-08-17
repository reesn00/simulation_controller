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
