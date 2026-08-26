"""Test lightweight health scoring used by ContextUnderstanding."""
from __future__ import annotations

import pytest
from config import Settings
from domain import Session, Message
from routing.health import light_health_score, light_health_score_for_session


def _session(blocks_by_msg: list[list[dict]]) -> Session:
    messages = []
    for i, blocks in enumerate(blocks_by_msg):
        role = "assistant" if i % 2 == 0 else "user"
        messages.append(Message(role=role, id=f"msg-{i}", blocks=blocks))
    return Session(session_id="h-test", messages=messages)


def test_light_health_score_all_success(cfg: Settings):
    blocks = [
        {"type": "toolcall", "id": "tc1", "name": "browser", "input": "{}", "state": "finished"},
        {"type": "toolresult", "id": "tc1", "name": "browser", "output_text": "ok", "state": "success"},
    ]
    h = light_health_score(blocks, cfg)
    assert h["is_healthy"] is True
    assert h["health_score"] == 1.0


def test_light_health_score_repetitive_loop(cfg: Settings):
    blocks = [
        {"type": "toolcall", "id": "tc1", "name": "browser", "input": '{"q":"x"}', "state": "finished"},
        {"type": "toolresult", "id": "tc1", "name": "browser", "output_text": "err", "state": "error"},
        {"type": "toolcall", "id": "tc2", "name": "browser", "input": '{"q":"x"}', "state": "finished"},
        {"type": "toolresult", "id": "tc2", "name": "browser", "output_text": "err", "state": "error"},
        {"type": "toolcall", "id": "tc3", "name": "browser", "input": '{"q":"x"}', "state": "finished"},
        {"type": "toolresult", "id": "tc3", "name": "browser", "output_text": "ok", "state": "success"},
    ]
    h = light_health_score(blocks, cfg)
    assert h["has_repetitive_loop"] is True
    assert h["is_healthy"] is False


def test_light_health_score_for_session(cfg: Settings):
    session = _session([
        [{"type": "toolcall", "id": "tc1", "name": "browser", "input": "{}", "state": "finished"},
         {"type": "toolresult", "id": "tc1", "name": "browser", "output_text": "ok", "state": "success"}],
        [{"type": "text", "id": "txt1", "text": "user"}],
    ])
    scores = light_health_score_for_session(session, cfg)
    assert 0 in scores
    assert scores[0] == 1.0
    assert 1 not in scores
