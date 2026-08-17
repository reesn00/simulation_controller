from __future__ import annotations

import re

from pydantic import BaseModel, ConfigDict

_URL_RE = re.compile(r"https?://[^\s<>()\[\]{}\"']+", re.IGNORECASE)


class Claim(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: str
    value: str
    start: int
    end: int


def extract_claims(text: str) -> tuple[Claim, ...]:
    claims: list[Claim] = []
    for match in _URL_RE.finditer(text):
        value = match.group(0).rstrip(".,;:，。；：！？")
        claims.append(Claim(kind="url", value=value, start=match.start(), end=match.start() + len(value)))
    for match in re.finditer(r"(?m)^\s*(?:[-*+]\s+|\d+[.)、]\s+)(.+)$", text):
        claims.append(Claim(kind="list_item", value=match.group(1).strip(), start=match.start(1), end=match.end(1)))
    return tuple(claims)
