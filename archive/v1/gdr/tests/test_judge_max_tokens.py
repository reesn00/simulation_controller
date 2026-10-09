"""回归测试: judge 调用预算 + reasoning_content 兜底.

覆盖两类问题:
  1. L3 judge (reassembler + validators/l3_judge) 过去硬编码 max_tokens=2048,
     reasoning 模型 (Qwen3.5 / DeepSeek / o1 类) 的思考链计入 max_tokens, 思考
     吃掉预算 → content 只剩半截 JSON → parse 失败 → score=0 → 误判 discard.
     现改为读 cfg.judge_max_tokens (默认 36000, 不在代码侧硬截)。
  2. llm_client.chat 过去只读 message.content, 端点把思考放在
     message.reasoning_content (DeepSeek / 部分 vLLM 配置) 时, content 为空
     直接报 empty, 不再尝试 reasoning_content. 现加 fallback, 并在 meta 里
     标记 used_reasoning_content_fallback 供审计。
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from config import Settings
from infrastructure import LlamaCppClient
from validators import l3_judge


def test_settings_default_judge_max_tokens_is_36000(tmp_path, monkeypatch):
    """默认值 = 36000, 不在代码侧硬截, 后端按自己的 n_ctx 自然截断."""
    fp = tmp_path / "root.yaml"
    fp.write_text(
        "llm:\n  base_url: http://llm/v1\n  model: mm\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("GDR_CONFIG_FILE", str(fp))
    s = Settings()
    assert s.judge_max_tokens == 36000
    assert s.llm_vote_max_tokens == 36000


def test_settings_judge_max_tokens_overridable_via_env(tmp_path, monkeypatch):
    fp = tmp_path / "root.yaml"
    fp.write_text(
        "llm:\n  base_url: http://llm/v1\n  model: mm\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("GDR_CONFIG_FILE", str(fp))
    monkeypatch.setenv("GDR_JUDGE_MAX_TOKENS", "24000")
    s = Settings()
    assert s.judge_max_tokens == 24000


def test_settings_llm_vote_max_tokens_overridable_via_env(tmp_path, monkeypatch):
    fp = tmp_path / "root.yaml"
    fp.write_text(
        "llm:\n  base_url: http://llm/v1\n  model: mm\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("GDR_CONFIG_FILE", str(fp))
    monkeypatch.setenv("GDR_LLM_VOTE_MAX_TOKENS", "16000")
    s = Settings()
    assert s.llm_vote_max_tokens == 16000


def test_llm_chat_falls_back_to_reasoning_content_when_content_empty():
    """content 为空但 reasoning_content 非空 → 用 reasoning_content 作为 text,
    并在 meta 标记 used_reasoning_content_fallback=True."""
    client = LlamaCppClient.__new__(LlamaCppClient)  # 绕过 .get 单例
    client.model = "fake-reasoning"
    client.base_url = "http://fake/v1"
    client._semaphore = MagicMock()
    client._semaphore.__enter__ = lambda s: s
    client._semaphore.__exit__ = lambda s, *a: None

    payload_response = {
        "choices": [{
            "message": {
                "content": "",
                "reasoning_content": "让我想想...\n{\"score\": 8, \"reason\": \"ok\"}",
            },
            "finish_reason": "stop",
        }],
        "usage": {"prompt_tokens": 100, "completion_tokens": 50},
    }

    with patch.object(client, "_post_chat_completions", return_value=payload_response):
        text, meta = client.chat(
            [{"role": "user", "content": "x"}], max_tokens=2048, temperature=0.0,
        )

    # reasoning_content 末尾的 JSON 应被采纳为 text
    assert text == "让我想想...\n{\"score\": 8, \"reason\": \"ok\"}"
    assert meta["used_reasoning_content_fallback"] is True


def test_llm_chat_prefers_content_over_reasoning_content():
    """content 非空时, reasoning_content 兜底不触发 (避免覆盖真实答案)."""
    client = LlamaCppClient.__new__(LlamaCppClient)
    client.model = "fake"
    client.base_url = "http://fake/v1"
    client._semaphore = MagicMock()
    client._semaphore.__enter__ = lambda s: s
    client._semaphore.__exit__ = lambda s, *a: None

    payload_response = {
        "choices": [{
            "message": {
                "content": "{\"score\": 9}",
                "reasoning_content": "internal thought",
            },
            "finish_reason": "stop",
        }],
        "usage": {"prompt_tokens": 10, "completion_tokens": 5},
    }

    with patch.object(client, "_post_chat_completions", return_value=payload_response):
        text, meta = client.chat(
            [{"role": "user", "content": "x"}], max_tokens=2048, temperature=0.0,
        )

    assert text == "{\"score\": 9}"
    assert meta["used_reasoning_content_fallback"] is False


def test_llm_chat_both_empty_logs_warning_and_marks_no_fallback():
    """content + reasoning_content 都空 → 仍报 empty, meta 标记 fallback=False."""
    client = LlamaCppClient.__new__(LlamaCppClient)
    client.model = "fake"
    client.base_url = "http://fake/v1"
    client._semaphore = MagicMock()
    client._semaphore.__enter__ = lambda s: s
    client._semaphore.__exit__ = lambda s, *a: None

    payload_response = {
        "choices": [{
            "message": {"content": "", "reasoning_content": ""},
            "finish_reason": "length",
        }],
        "usage": {"prompt_tokens": 10, "completion_tokens": 0},
    }

    with patch.object(client, "_post_chat_completions", return_value=payload_response):
        text, meta = client.chat(
            [{"role": "user", "content": "x"}], max_tokens=2048, temperature=0.0,
        )

    assert text == ""
    assert meta["used_reasoning_content_fallback"] is False


def test_l3_judge_validator_uses_cfg_judge_max_tokens(cfg):
    """validators/l3_judge.py 的 chat 调用应读 cfg.judge_max_tokens,
    不再硬编码 2048."""
    cfg = cfg.model_copy(update={"judge_max_tokens": 12345})
    captured = {}

    def fake_chat(self, messages, **kwargs):
        captured["max_tokens"] = kwargs.get("max_tokens")
        # 返回可直接 parse 的 JSON
        return '{"verdict": "pass", "score": 8, "reason": "ok"}', {}

    with patch.object(LlamaCppClient, "chat", fake_chat):
        out = l3_judge.check(
            {"type": "thinking", "thinking": "原思考"},
            {"thinking": "新思考"},
            cfg,
        )

    assert captured["max_tokens"] == 12345
    assert out["verdict"] == "pass"
    assert out["score"] == 8


def test_l3_judge_validator_default_max_tokens_is_36000(cfg):
    """未显式配置 judge_max_tokens 时, 默认 36000 而不是 2048."""
    captured = {}

    def fake_chat(self, messages, **kwargs):
        captured["max_tokens"] = kwargs.get("max_tokens")
        return '{"verdict": "pass", "score": 10, "reason": ""}', {}

    with patch.object(LlamaCppClient, "chat", fake_chat):
        l3_judge.check(
            {"type": "thinking", "thinking": "原"},
            {"thinking": "新"},
            cfg,
        )

    assert captured["max_tokens"] == 36000


def test_reassembler_judge_uses_cfg_judge_max_tokens(cfg):
    """reassembly/reassembler.py 终检 judge 也应读 cfg.judge_max_tokens,
    不再硬编码 2048."""
    from reassembly.reassembler import reassemble

    cfg = cfg.model_copy(update={
        "judge_max_tokens": 9999,
        "judge_min_score": 7,
        "judge_min_score_relaxed": 0,
        "judge_min_modified_for_relaxation": 5,
        "strict_consistency": True,
    })
    captured = {}

    def fake_chat(self, messages, **kwargs):
        captured["max_tokens"] = kwargs.get("max_tokens")
        return '{"score": 9, "reason": "ok"}', {}

    session, refine_records = _build_min_session_with_refine()

    with patch.object(LlamaCppClient, "chat", fake_chat):
        out = reassemble(session, refine_records, cfg=cfg)

    assert captured["max_tokens"] == 9999
    # judge 通过, session 应被保留, 不进 judge_discard
    assert out is not None
    assert "judge_discard" not in out.metadata


def test_router_llm_vote_uses_cfg_llm_vote_max_tokens(cfg):
    """router LLM 投票也应读 cfg.llm_vote_max_tokens (默认 36000),
    不再硬编码 1024. 同根问题: reasoning 模型的思考计入 max_tokens."""
    from routing import Router
    from core.context_understanding import build_context_for_session
    from routing.health import light_health_score_for_session
    from domain import Session, Message

    cfg = cfg.model_copy(update={
        "enable_llm_layer": True,
        "llm_vote_max_tokens": 8888,
    })
    session = Session(session_id="rt-mt-test", messages=[
        Message(role="assistant", id="m0", blocks=[
            # 600 字符 thinking → 触发 thought_too_long → 进入 LLM 投票
            {"type": "thinking", "id": "th1", "thinking": "a" * 600},
        ]),
    ])
    light = light_health_score_for_session(session, cfg)
    cu = build_context_for_session(session, cfg, light_health=light)

    captured = {}

    def fake_chat(self, messages, **kwargs):
        captured["max_tokens"] = kwargs.get("max_tokens")
        return '{"has_defect": false}', None

    with patch.object(LlamaCppClient, "chat", fake_chat):
        Router().tag(session, ["browser"], set(), cfg, context_understanding=cu)

    assert captured["max_tokens"] == 8888


def test_router_llm_vote_default_max_tokens_is_36000(cfg):
    """未显式配置 llm_vote_max_tokens 时, 默认 36000 而不是 1024."""
    from routing import Router
    from core.context_understanding import build_context_for_session
    from routing.health import light_health_score_for_session
    from domain import Session, Message

    cfg = cfg.model_copy(update={"enable_llm_layer": True})
    session = Session(session_id="rt-mt-default", messages=[
        Message(role="assistant", id="m0", blocks=[
            {"type": "thinking", "id": "th1", "thinking": "a" * 600},
        ]),
    ])
    light = light_health_score_for_session(session, cfg)
    cu = build_context_for_session(session, cfg, light_health=light)

    captured = {}

    def fake_chat(self, messages, **kwargs):
        captured["max_tokens"] = kwargs.get("max_tokens")
        return '{"has_defect": false}', None

    with patch.object(LlamaCppClient, "chat", fake_chat):
        Router().tag(session, ["browser"], set(), cfg, context_understanding=cu)

    assert captured["max_tokens"] == 36000


# === minimal helpers ===

def _build_min_session_with_refine():
    """构造一条足以触发 reassemble → judge 调用链的最小 session.
    返回 (session, refine_records) — reassemble 把 refine_records 作为直接参数接收,
    不从 session.metadata 取."""
    from domain import Session, Message, BlockRefineRecord, BlockIndex
    # 单条 assistant 消息, 1 长 thinking (触发 thought_too_long → 走 refiner
    # → 产生 refine_records) + 1 toolcall + 1 toolresult。
    blocks = [
        {
            "type": "thinking", "id": "th1",
            "thinking": "a" * 600,
        },
        {
            "type": "toolcall", "id": "tc1",
            "name": "browser", "input": '{"q":"x"}', "state": "finished",
        },
        {
            "type": "toolresult", "id": "tc1",
            "name": "browser", "output_text": "ok", "state": "success",
        },
    ]
    msg = Message(role="assistant", id="msg-0", blocks=blocks)
    session = Session(session_id="judge-mt-test", messages=[msg])
    # success 计 modified_count=1, 触发 judge。
    refine_records = [
        BlockRefineRecord(
            block_index=BlockIndex(
                msg_idx=0, block_idx=0, block_id="th1", block_type="thinking",
            ),
            module="thought_refactor",
            original_content={"thinking": "x" * 600},
            refined_content={"thinking": "y" * 50},
            result="success",
        ),
    ]
    return session, refine_records