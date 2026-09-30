"""F1 fix: tools 字段透传到 ``save_session_v2`` 的 4 视图产物 + meta 去重.

覆盖:

- ``save_session_v2`` 把 ``session.metadata["tools"]`` 写入 ``.openai.json``
  和 ``.messages.json`` 顶层, 保留 schema (description + parameters).
- 配置项 ``tools_payload_max`` 控制截断.
- 配置项 ``include_tools_in_payloads=False`` 关闭时不写入.
- 2026-09-30 去重: ``meta.json`` 不再内嵌 ``openai_messages`` / ``tools`` /
  ``qf_text`` 三个视图副本, 改由 ``tools_declared`` + ``views`` 承担审计.
- qwenjina.txt 由 ``transform.render_sample_text`` 渲染时已传 tools,
  不需单独验证注入 (qf_text 含 tools 文本化由 transform.py 自身测试覆盖).
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

from domain import Message, Session
from domain.schema import _current_settings, _extract_tools_payload, save_session_v2


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
# save_session_v2: openai.json
# ---------------------------------------------------------------------------


class TestSaveSessionOpenAITools:
    def test_openai_payload_contains_tools(self, tmp_path, monkeypatch):
        _patch_settings(monkeypatch)
        sess = _make_session([
            _tool("web_search"),
            _tool("execute_shell_command"),
        ])
        save_session_v2(sess, tmp_path / "S001")
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
        save_session_v2(sess, tmp_path / "S002")
        payload = json.loads((tmp_path / "S002.openai.json").read_text("utf-8"))
        fn = payload["tools"][0]["function"]
        assert fn["name"] == "browser"
        assert fn["description"] == "Test tool browser"
        assert "properties" in fn["parameters"]

    def test_openai_payload_omits_tools_when_disabled(self, tmp_path, monkeypatch):
        _patch_settings(monkeypatch, include_tools_in_payloads=False)
        sess = _make_session([_tool("web_search")])
        save_session_v2(sess, tmp_path / "S003")
        payload = json.loads((tmp_path / "S003.openai.json").read_text("utf-8"))
        assert "openai_messages" in payload
        assert "tools" not in payload

    def test_openai_payload_omits_tools_when_metadata_empty(self, tmp_path, monkeypatch):
        _patch_settings(monkeypatch)
        sess = _make_session([])
        save_session_v2(sess, tmp_path / "S004")
        payload = json.loads((tmp_path / "S004.openai.json").read_text("utf-8"))
        assert "tools" not in payload

    def test_openai_payload_respects_cap(self, tmp_path, monkeypatch):
        _patch_settings(monkeypatch, tools_payload_max=1)
        sess = _make_session([_tool("a"), _tool("b"), _tool("c")])
        save_session_v2(sess, tmp_path / "S005")
        payload = json.loads((tmp_path / "S005.openai.json").read_text("utf-8"))
        assert len(payload["tools"]) == 1
        assert payload["tools"][0]["function"]["name"] == "a"


# ---------------------------------------------------------------------------
# save_session_v2: messages.json
# ---------------------------------------------------------------------------


class TestSaveSessionMessagesTools:
    def test_messages_payload_contains_tools(self, tmp_path, monkeypatch):
        _patch_settings(monkeypatch)
        sess = _make_session([_tool("web_search"), _tool("browser")])
        save_session_v2(sess, tmp_path / "M001")
        payload = json.loads((tmp_path / "M001.messages.json").read_text("utf-8"))
        assert "messages" in payload
        assert "tools" in payload
        assert isinstance(payload["messages"], list)
        names = [t["function"]["name"] for t in payload["tools"]]
        assert names == ["web_search", "browser"]

    def test_messages_payload_omits_tools_when_disabled(self, tmp_path, monkeypatch):
        _patch_settings(monkeypatch, include_tools_in_payloads=False)
        sess = _make_session([_tool("web_search")])
        save_session_v2(sess, tmp_path / "M002")
        payload = json.loads((tmp_path / "M002.messages.json").read_text("utf-8"))
        assert "tools" not in payload

    def test_messages_blocks_structure_unchanged(self, tmp_path, monkeypatch):
        """F1 不改 messages 内的 blocks 结构, 只在顶层加 tools."""
        _patch_settings(monkeypatch)
        sess = _make_session([_tool("a")])
        save_session_v2(sess, tmp_path / "M003")
        payload = json.loads((tmp_path / "M003.messages.json").read_text("utf-8"))
        for msg in payload["messages"]:
            assert "role" in msg
            assert "blocks" in msg
            assert isinstance(msg["blocks"], list)


# ---------------------------------------------------------------------------
# save_session_v2: qwenjina.txt + meta.json
# ---------------------------------------------------------------------------


class TestSaveSessionQwenjinaAndMeta:
    def test_qwenjina_written_when_qf_text_present(self, tmp_path, monkeypatch):
        _patch_settings(monkeypatch)
        sess = _make_session([_tool("web_search")])
        out = save_session_v2(sess, tmp_path / "Q001")
        assert out.qwenjina is not None and out.qwenjina.exists()

    def test_qwenjina_skipped_when_qf_text_missing(self, tmp_path, monkeypatch):
        _patch_settings(monkeypatch)
        sess = _make_session([_tool("web_search")])
        sess.metadata.pop("qf_text", None)
        out = save_session_v2(sess, tmp_path / "Q002")
        assert out.qwenjina is None
        assert not (tmp_path / "Q002.qwenjina.txt").exists()

    def test_meta_declares_tools_when_payloads_off(self, tmp_path, monkeypatch):
        """meta.json 始终记录全部工具名 (审计需要), 与 include_tools_in_payloads 无关.

        2026-09-30 起是 ``tools_declared`` 名清单而非完整 tools schema ——
        完整 schema 已落在 3 份视图里, 再存一份是双份存储。详见
        :class:`TestMetaExcludesViewPayloads`。
        """
        _patch_settings(monkeypatch, include_tools_in_payloads=False)
        sess = _make_session([_tool("web_search"), _tool("browser")])
        save_session_v2(sess, tmp_path / "META")
        payload = json.loads((tmp_path / "META.meta.json").read_text("utf-8"))
        assert payload["tools_declared"] == ["web_search", "browser"]


# ---------------------------------------------------------------------------
# save_session_v2: meta 去重 (2026-09-30)
# ---------------------------------------------------------------------------


class TestMetaExcludesViewPayloads:
    """meta.json 不再内嵌 3 份视图的内容副本（2026-09-30 去重）。

    这三个键是 ``.messages.json`` / ``.openai.json`` / ``.qwenjina.txt`` 的
    **内容副本**，写进 meta 等于同一份数据存两遍（实测占 meta 体积约一半）。
    审计由 ``tools_declared``（未截断工具名）+ ``views``（尺寸 + sha256）接替。
    """

    def test_meta_drops_view_payload_keys(self, tmp_path, monkeypatch):
        _patch_settings(monkeypatch)
        sess = _make_session([_tool("web_search")])
        sess.metadata["openai_messages"] = [{"role": "user", "content": "hi"}]
        sess.metadata["qf_text"] = "<text>hi</text>"
        save_session_v2(sess, tmp_path / "D001")
        payload = json.loads((tmp_path / "D001.meta.json").read_text("utf-8"))
        for key in ("openai_messages", "tools", "qf_text"):
            assert key not in payload, f"{key} 是视图副本, 不该进 meta"

    def test_meta_keeps_tools_declared_when_payloads_off(self, tmp_path, monkeypatch):
        """tools_declared 承担原 meta["tools"] 的审计职责, 与 payload 开关无关."""
        _patch_settings(monkeypatch, include_tools_in_payloads=False)
        sess = _make_session([_tool("web_search"), _tool("browser")])
        save_session_v2(sess, tmp_path / "D002")
        payload = json.loads((tmp_path / "D002.meta.json").read_text("utf-8"))
        assert payload["tools_declared"] == ["web_search", "browser"]
        # 开关关闭时视图确实没写 tools, 但名清单照样在
        for suffix in (".messages.json", ".openai.json"):
            view = json.loads((tmp_path / f"D002{suffix}").read_text("utf-8"))
            assert "tools" not in view

    def test_tools_declared_not_truncated_by_payload_max(self, tmp_path, monkeypatch):
        """名清单取未截断全量 —— 这正是原 meta["tools"] 的审计价值所在."""
        _patch_settings(monkeypatch, tools_payload_max=2)
        sess = _make_session([_tool(f"t{i}") for i in range(5)])
        save_session_v2(sess, tmp_path / "D003")
        payload = json.loads((tmp_path / "D003.meta.json").read_text("utf-8"))
        assert payload["tools_declared"] == ["t0", "t1", "t2", "t3", "t4"]
        view = json.loads((tmp_path / "D003.openai.json").read_text("utf-8"))
        assert len(view["tools"]) == 2, "视图侧仍应受 tools_payload_max 约束"

    def test_views_pointer_matches_disk(self, tmp_path, monkeypatch):
        import hashlib

        _patch_settings(monkeypatch)
        sess = _make_session([_tool("web_search")])
        sess.metadata["qf_text"] = "<text>hi</text>"
        save_session_v2(sess, tmp_path / "D004")
        views = json.loads((tmp_path / "D004.meta.json").read_text("utf-8"))["views"]
        assert set(views) == {"messages", "openai", "qwenjina"}
        for name, pointer in views.items():
            path = tmp_path / f"D004.{name if name != 'qwenjina' else 'qwenjina.txt'}"
            if name in ("messages", "openai"):
                path = tmp_path / f"D004.{name}.json"
            raw = path.read_bytes()
            assert pointer["file"] == path.name
            assert pointer["bytes"] == len(raw)
            assert pointer["sha256"] == hashlib.sha256(raw).hexdigest()

    def test_views_omits_qwenjina_when_qf_text_missing(self, tmp_path, monkeypatch):
        """qf_text 缺失 → 不写 qwenjina.txt, views 也不列它 (不写悬空指针)."""
        _patch_settings(monkeypatch)
        sess = _make_session([_tool("web_search")])
        sess.metadata.pop("qf_text", None)
        save_session_v2(sess, tmp_path / "D005")
        payload = json.loads((tmp_path / "D005.meta.json").read_text("utf-8"))
        assert set(payload["views"]) == {"messages", "openai"}
        assert not (tmp_path / "D005.qwenjina.txt").exists()

    def test_meta_keeps_qf_rendered_at_and_stats(self, tmp_path, monkeypatch):
        """这两个键刻意不剥 —— qf_rendered_at 是 LS 标注页时间线唯一数据源."""
        _patch_settings(monkeypatch)
        sess = _make_session([_tool("web_search")])
        sess.metadata["qf_text"] = "<text>hi</text>"
        sess.metadata["qf_rendered_at"] = "2026-09-30T00:00:00.000000Z"
        sess.metadata["qf_stats"] = {"openai_messages_emitted": 2, "tools_unique": 1}
        save_session_v2(sess, tmp_path / "D006")
        payload = json.loads((tmp_path / "D006.meta.json").read_text("utf-8"))
        assert payload["qf_rendered_at"] == "2026-09-30T00:00:00.000000Z"
        assert payload["qf_stats"] == {"openai_messages_emitted": 2, "tools_unique": 1}
