"""C1 契约入口 ``gdr.parsers.from_trajectory`` 的落点契约回归.

**tools 归一到 ``metadata["tools"]``**（2026-09-30 定案）。

``SessionRecord.to_session_dict()`` 把 C1 的 ``model_request.payload.tools``
放在 dict **顶层**，而 ``gdr.domain.schema.Session`` 没有显式 ``tools`` 字段，
只靠 ``ConfigDict(extra="allow")`` 兜 —— 于是它变成一个没人读的顶层 extra，
``metadata["tools"]`` 恒为 None。下游三处全部按 metadata 读：

- ``gdr.refiners.usage_prune.prune_session_in_place`` → ``tools_before=0``，
  **工具裁剪全程空转**（stats 看起来正常，最难发现的一类 bug）
- ``gdr.domain.schema._extract_tools_payload`` → C3 视图 tools 走空
- ``save_session_v2`` 的 ``tools_declared``（审计字段）→ **恒空**

etl 侧当时侥幸没坏：``render_chain`` 用整份 ``model_dump()``，顶层那份还在，
属于"蒙对"而非设计。归一后 etl 那段「gdr 把它存在 metadata 里，这里提上来」
的提升逻辑正好接上。

归一点集中放在生产者（``_normalize_tools``）而不是让 3 个消费者各写一遍
fallback —— 只有一个生产者、三个消费者时这显然更省。
"""
from __future__ import annotations

from pathlib import Path

import pytest

from gdr.parsers import _normalize_tools, from_trajectory

_TOOLS = [
    {"type": "function", "function": {"name": "web_search", "description": "s",
                                      "parameters": {"type": "object"}}},
    {"type": "function", "function": {"name": "browser", "description": "b",
                                      "parameters": {"type": "object"}}},
]


def _raw(tools=None, metadata=None) -> dict:
    """模拟 ``SessionRecord.to_session_dict()`` 的输出形状。"""
    raw = {
        "session_id": "s1",
        "summary": "",
        "messages": [
            {"role": "user", "name": "user", "id": "u1",
             "blocks": [{"type": "text", "id": "b1", "text": "hi"}], "metadata": {}},
        ],
        "source_file": "T001__s1.json",
        "run_id": "T001",
    }
    if tools is not None:
        raw["tools"] = tools
    if metadata is not None:
        raw["metadata"] = metadata
    return raw


class TestNormalizeTools:
    def test_moves_tools_into_metadata(self):
        out = _normalize_tools(_raw(tools=_TOOLS))
        assert out["metadata"]["tools"] == _TOOLS
        assert "tools" not in out, "顶层 tools 必须被搬走, 不能留成没人读的 extra"

    def test_omits_key_when_no_tools(self):
        """无 tools 时不写空键 —— 下游一律 ``.get("tools") or []``, 等价但更干净."""
        out = _normalize_tools(_raw())
        assert "tools" not in out.get("metadata", {})

    def test_omits_key_when_tools_empty_list(self):
        out = _normalize_tools(_raw(tools=[]))
        assert "tools" not in out.get("metadata", {})

    def test_preserves_existing_metadata(self):
        """归一不能把已有的 metadata 键 (routing_abstentions 等) 冲掉."""
        out = _normalize_tools(_raw(tools=_TOOLS, metadata={"unknown_tool_names": ["x"]}))
        assert out["metadata"]["unknown_tool_names"] == ["x"]
        assert out["metadata"]["tools"] == _TOOLS

    def test_mutates_in_place_by_design(self):
        """就地 pop 是**刻意**的: 唯一调用方 ``from_trajectory`` 传的是
        ``to_session_dict()`` 当场新建的 dict, 没有任何人共享它。"""
        raw = _raw(tools=_TOOLS)
        out = _normalize_tools(raw)
        assert out is raw, "沿用同一个 dict, 不额外拷贝"
        assert "tools" not in raw


class TestFromTrajectoryToolPlacement:
    """端到端一点: 真写一份 C1, 确认 tools 落在 metadata 而不是 extra."""

    def test_tools_land_in_metadata(self, c1_trajectory, tmp_path: Path):
        src = c1_trajectory(
            tmp_path / "T001__s1.json", session_id="s1",
            user_text="hi", assistant_text="你好", tools=_TOOLS,
        )
        session = from_trajectory(src)
        # 刻意**不**写 isinstance(session, Session): gdr 有两套并存的导入姿势,
        # 本文件的 `from domain import Session` 是顶层那套, 而 from_trajectory
        # 产出的是 gdr.domain.schema.Session —— 同一个类被加载成两个对象,
        # isinstance 恒 False。这正是 usage_prune 那两处静默失效的成因
        # (见 gdr/refiners/usage_prune.py 注释), 别在这里重蹈。
        assert type(session).__name__ == "Session"
        assert session.metadata["tools"] == _TOOLS

        dumped = session.model_dump(mode="json")
        assert "tools" not in dumped, (
            "顶层多一个 extra tools 就意味着下游读 metadata 时拿到 None —— "
            "这正是本次修复要消掉的形态"
        )

    def test_session_without_tools_has_clean_metadata(self, c1_trajectory, tmp_path: Path):
        src = c1_trajectory(
            tmp_path / "T001__s2.json", session_id="s2",
            user_text="hi", assistant_text="你好",
        )
        session = from_trajectory(src)
        assert "tools" not in (session.metadata or {})


@pytest.mark.parametrize("n_declared", [1, 6])
def test_declared_tool_count_survives_replay(c1_trajectory, tmp_path: Path, n_declared):
    """声明多少个, 重放后就该有多少个 —— 数量对不上说明归一在中途丢了东西."""
    tools = [
        {"type": "function", "function": {"name": f"t{i}", "parameters": {"type": "object"}}}
        for i in range(n_declared)
    ]
    src = c1_trajectory(
        tmp_path / f"T001__s{n_declared}.json", session_id=f"s{n_declared}",
        user_text="hi", assistant_text="你好", tools=tools,
    )
    got = from_trajectory(src).metadata["tools"]
    assert [t["function"]["name"] for t in got] == [t["function"]["name"] for t in tools]
