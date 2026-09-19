"""F1 fix: tools 字段透传到 ``save_session`` / ``write_refined_session`` 产物.

覆盖:

- ``save_session`` 把 ``session.metadata["tools"]`` 写入 ``.openai.json``
  和 ``.messages.json`` 顶层, 保留 schema (description + parameters).
- 配置项 ``tools_payload_max`` 控制截断.
- 配置项 ``include_tools_in_payloads=False`` 关闭时不写入.
- ``write_refined_session`` (usage_prune 路径) 同样行为.
- qwenjina.txt 由 ``transform.render_sample_text`` 渲染时已传 tools,
  不需单独验证注入 (qf_text 含 tools 文本化由 transform.py 自身测试覆盖).
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from domain import Message, Session
from domain.schema import _current_settings, _extract_tools_payload, save_session


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _tool(name: str, *, with_schema: bool = True) -> dict:
    spec = {
        "type": "function",
        "function": {
            "name": name,
            "description": f"Test tool {name}",
            "parameters": {"type": "object", "properties": {"q": {"type": "string"}}},
        },
    }
    if not with_schema:
        spec["function"].pop("description")
        spec["function"]["parameters"] = {"type": "object"}
    return spec


def _make_session(tools: list[dict], openai_msgs: list[dict] | None = None) -> Session:
    msgs = [
        Message(role="user", id="m0", blocks=[
            {"type": "text", "id": "b0", "text": "hi"},
        ]),
        Message(role="assistant", id="m1", blocks=[
            {"type": "text", "id": "b1", "text": "hello"},
        ]),
    ]
    return Session(
        session_id="s-tools-test",
        messages=msgs,
        metadata={
            "openai_messages": openai_msgs or [{"role": "user", "content": "hi"}],
            "tools": tools,
            "qf_text": "<system>placeholder</system>\n<|user|>hi<|end|>\n",
        },
    )


def _patch_settings(monkeypatch, **overrides):
    """替换 schema._current_settings 返回的 cfg, 避免真的去加载根配置."""
    defaults = dict(include_tools_in_payloads=True, tools_payload_max=64)
    defaults.update(overrides)
    fake = SimpleNamespace(**defaults)
    monkeypatch.setattr("domain.schema._current_settings", lambda: fake)


# ---------------------------------------------------------------------------
# _extract_tools_payload
# ---------------------------------------------------------------------------


class TestExtractToolsPayload:
    def test_returns_tools_when_metadata_present(self, monkeypatch):
        _patch_settings(monkeypatch)
        sess = _make_session([_tool("web_search"), _tool("browser")])
        out = _extract_tools_payload(sess)
        assert out is not None
        assert [t["function"]["name"] for t in out] == ["web_search", "browser"]

    def test_returns_none_when_disabled(self, monkeypatch):
        _patch_settings(monkeypatch, include_tools_in_payloads=False)
        sess = _make_session([_tool("web_search")])
        assert _extract_tools_payload(sess) is None

    def test_returns_none_when_metadata_empty(self, monkeypatch):
        _patch_settings(monkeypatch)
        sess = _make_session([])
        assert _extract_tools_payload(sess) is None

    def test_truncates_to_cap(self, monkeypatch):
        _patch_settings(monkeypatch, tools_payload_max=2)
        sess = _make_session([_tool("a"), _tool("b"), _tool("c"), _tool("d")])
        out = _extract_tools_payload(sess)
        assert out is not None
        assert len(out) == 2
        assert [t["function"]["name"] for t in out] == ["a", "b"]

    def test_cap_zero_means_no_truncate(self, monkeypatch):
        _patch_settings(monkeypatch, tools_payload_max=0)
        sess = _make_session([_tool("a"), _tool("b"), _tool("c")])
        out = _extract_tools_payload(sess)
        assert out is not None and len(out) == 3


# ---------------------------------------------------------------------------
# save_session: openai.json
# ---------------------------------------------------------------------------


class TestSaveSessionOpenAITools:
    def test_openai_payload_contains_tools(self, tmp_path, monkeypatch):
        _patch_settings(monkeypatch)
        sess = _make_session([
            _tool("web_search"),
            _tool("execute_shell_command"),
        ])
        save_session(sess, tmp_path / "S001")
        openai_path = tmp_path / "S001.openai.json"
        assert openai_path.exists()
        payload = json.loads(openai_path.read_text("utf-8"))
        assert "openai_messages" in payload
        assert "tools" in payload
        names = [t["function"]["name"] for t in payload["tools"]]
        assert names == ["web_search", "execute_shell_command"]

    def test_openai_payload_preserves_schema(self, tmp_path, monkeypatch):
        """description + parameters 必须随 tools 一起落地."""
        _patch_settings(monkeypatch)
        sess = _make_session([_tool("browser")])
        save_session(sess, tmp_path / "S002")
        payload = json.loads((tmp_path / "S002.openai.json").read_text("utf-8"))
        fn = payload["tools"][0]["function"]
        assert fn["name"] == "browser"
        assert fn["description"] == "Test tool browser"
        assert "properties" in fn["parameters"]

    def test_openai_payload_omits_tools_when_disabled(self, tmp_path, monkeypatch):
        _patch_settings(monkeypatch, include_tools_in_payloads=False)
        sess = _make_session([_tool("web_search")])
        save_session(sess, tmp_path / "S003")
        payload = json.loads((tmp_path / "S003.openai.json").read_text("utf-8"))
        assert "openai_messages" in payload
        assert "tools" not in payload

    def test_openai_payload_omits_tools_when_metadata_empty(self, tmp_path, monkeypatch):
        _patch_settings(monkeypatch)
        sess = _make_session([])
        save_session(sess, tmp_path / "S004")
        payload = json.loads((tmp_path / "S004.openai.json").read_text("utf-8"))
        assert "tools" not in payload

    def test_openai_payload_respects_cap(self, tmp_path, monkeypatch):
        _patch_settings(monkeypatch, tools_payload_max=1)
        sess = _make_session([_tool("a"), _tool("b"), _tool("c")])
        save_session(sess, tmp_path / "S005")
        payload = json.loads((tmp_path / "S005.openai.json").read_text("utf-8"))
        assert len(payload["tools"]) == 1
        assert payload["tools"][0]["function"]["name"] == "a"


# ---------------------------------------------------------------------------
# save_session: messages.json
# ---------------------------------------------------------------------------


class TestSaveSessionMessagesTools:
    def test_messages_payload_contains_tools(self, tmp_path, monkeypatch):
        _patch_settings(monkeypatch)
        sess = _make_session([_tool("web_search"), _tool("browser")])
        save_session(sess, tmp_path / "M001")
        payload = json.loads((tmp_path / "M001.messages.json").read_text("utf-8"))
        assert "messages" in payload
        assert "tools" in payload
        assert isinstance(payload["messages"], list)
        names = [t["function"]["name"] for t in payload["tools"]]
        assert names == ["web_search", "browser"]

    def test_messages_payload_omits_tools_when_disabled(self, tmp_path, monkeypatch):
        _patch_settings(monkeypatch, include_tools_in_payloads=False)
        sess = _make_session([_tool("web_search")])
        save_session(sess, tmp_path / "M002")
        payload = json.loads((tmp_path / "M002.messages.json").read_text("utf-8"))
        assert "tools" not in payload

    def test_messages_blocks_structure_unchanged(self, tmp_path, monkeypatch):
        """F1 不改 messages 内的 blocks 结构, 只在顶层加 tools."""
        _patch_settings(monkeypatch)
        sess = _make_session([_tool("a")])
        save_session(sess, tmp_path / "M003")
        payload = json.loads((tmp_path / "M003.messages.json").read_text("utf-8"))
        for msg in payload["messages"]:
            assert "role" in msg
            assert "blocks" in msg
            assert isinstance(msg["blocks"], list)


# ---------------------------------------------------------------------------
# save_session: qwenjina.txt + meta.json
# ---------------------------------------------------------------------------


class TestSaveSessionQwenjinaAndMeta:
    def test_qwenjina_written_when_qf_text_present(self, tmp_path, monkeypatch):
        _patch_settings(monkeypatch)
        sess = _make_session([_tool("web_search")])
        out = save_session(sess, tmp_path / "Q001")
        assert out.qwenjina is not None and out.qwenjina.exists()

    def test_qwenjina_skipped_when_qf_text_missing(self, tmp_path, monkeypatch):
        _patch_settings(monkeypatch)
        sess = _make_session([_tool("web_search")])
        sess.metadata.pop("qf_text", None)
        out = save_session(sess, tmp_path / "Q002")
        assert out.qwenjina is None
        assert not (tmp_path / "Q002.qwenjina.txt").exists()

    def test_meta_always_contains_tools(self, tmp_path, monkeypatch):
        """meta.json 始终包含完整 tools (审计需要), 与 include_tools_in_payloads 无关."""
        _patch_settings(monkeypatch, include_tools_in_payloads=False)
        sess = _make_session([_tool("web_search"), _tool("browser")])
        save_session(sess, tmp_path / "META")
        payload = json.loads((tmp_path / "META.meta.json").read_text("utf-8"))
        assert "tools" in payload
        names = [t["function"]["name"] for t in payload["tools"]]
        assert names == ["web_search", "browser"]


# ---------------------------------------------------------------------------
# write_refined_session (usage_prune 路径)
# ---------------------------------------------------------------------------


class TestWriteRefinedSessionTools:
    @pytest.fixture(autouse=True)
    def _patch_prune_settings(self, monkeypatch):
        defaults = dict(include_tools_in_payloads=True, tools_payload_max=64)

        class _FakeSettings:
            pass

        # 同时 patch etl 路径下的 _current_settings_for_prune 和 gdr 路径下的 _current_settings
        monkeypatch.setattr(
            "etl.qwenformat.usage_prune._current_settings_for_prune",
            lambda: SimpleNamespace(**defaults),
        )
        yield

    def test_write_refined_session_openai_includes_tools(self, tmp_path):
        from etl.qwenformat.usage_prune import write_refined_session

        sess = {
            "session_id": "u1",
            "messages": [{"role": "user", "blocks": [{"type": "text", "text": "hi"}]}],
            "metadata": {
                "openai_messages": [{"role": "user", "content": "hi"}],
                "tools": [_tool("web_search"), _tool("browser")],
                "qf_text": "<system>placeholder</system>",
            },
        }
        write_refined_session(sess, str(tmp_path / "u1.messages.json"))
        payload = json.loads((tmp_path / "u1.openai.json").read_text("utf-8"))
        assert "openai_messages" in payload
        assert "tools" in payload
        names = [t["function"]["name"] for t in payload["tools"]]
        assert names == ["web_search", "browser"]

    def test_write_refined_session_messages_includes_tools(self, tmp_path):
        from etl.qwenformat.usage_prune import write_refined_session

        sess = {
            "session_id": "u2",
            "messages": [{"role": "user", "blocks": [{"type": "text", "text": "hi"}]}],
            "metadata": {
                "openai_messages": [],
                "tools": [_tool("a"), _tool("b")],
                "qf_text": "x",
            },
        }
        write_refined_session(sess, str(tmp_path / "u2.messages.json"))
        payload = json.loads((tmp_path / "u2.messages.json").read_text("utf-8"))
        assert "messages" in payload
        assert "tools" in payload
        assert [t["function"]["name"] for t in payload["tools"]] == ["a", "b"]