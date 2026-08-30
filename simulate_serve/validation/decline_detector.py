from __future__ import annotations

import re

# Honest-limitation declarations. We keep this list narrow and high-precision:
# a false positive stops the run and forfeits distillation. Each entry is a
# regex applied to the visible response text.
_DECLINE_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"我(?:无法|不能|没办法|做不到)"),
    re.compile(r"无法(?:完成|满足|提供|继续|实现)"),
    re.compile(r"需要.{0,4}(?:提供|确认|授权|先做|先告诉我)"),
    re.compile(r"无法访问|访问受限|没有权限|权限不足|超出.{0,4}能力"),
    re.compile(r"\b(?:I (?:can(?:not|'t) |cannot |am unable to)\b|out of (?:my )?scope|no permission)"),
)


def detect_honest_limitation(text: str) -> bool:
    """Return True when the remote agent appears to honestly declare an inability.

    Conservative: only signals a single regex hit in the response text. Callers
    must combine with `blocked_action` policy before terminating the run.
    """
    if not text:
        return False
    return any(pattern.search(text) for pattern in _DECLINE_PATTERNS)
