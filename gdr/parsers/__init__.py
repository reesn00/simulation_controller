"""gdr.parsers — C1 契约入口.

新架构 ``simulation server → gdr → etl`` 下，gdr 不再消费 etl/qwenformat 的 qf_out
产物，而是直接读 trajectory 事件流（C1 契约，详见 ``docs/contracts/C1-trajectory-events.md``），
轻量解析为 ``gdr.domain.Session`` 后进入 refine 流水线。

本包是 gdr 对 C1 契约的**唯一入口**：所有 gdr 内部代码必须通过本包加载
trajectory，不得直接 ``import etl.qwenformat.load``。

约定：未来若把 parser 从 etl 迁出，本包内部对 ``etl.qwenformat.load.load_trajectory``
的引用是唯一改动点，其他位置不需调整。
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from gdr.domain.schema import Session


def from_trajectory(path: Path) -> Session:
    """把 trajectory JSONL 事件流（C1 契约）解析为 gdr Session.

    步骤：
      1. ``etl.qwenformat.load.load_trajectory(path)`` 重放事件流为
         ``SessionRecord``（dataclass）
      2. ``SessionRecord.to_session_dict()`` 转 dict
      3. **tools 归一到 ``metadata["tools"]``**（见下方说明）
      4. ``Session.model_validate(...)`` 走 pydantic 校验生成最终对象

    该函数**只解析 blocks**，不做 system prompt partition / tool template /
    tool output summarization / qf_text 渲染——这些是 etl 阶段职责。
    """
    # 局部导入: 避免 gdr.parsers → etl.qwenformat.load 的循环依赖 (gdr domain
    # 也可能被 etl 通过 gdr.domain.schema 反向引用)
    from etl.qwenformat.load import load_trajectory

    record = load_trajectory(path)
    raw = _normalize_tools(record.to_session_dict())
    return Session.model_validate(raw)


def _normalize_tools(raw: dict[str, Any]) -> dict[str, Any]:
    """把 ``tools`` 从 dict 顶层搬进 ``metadata["tools"]``.

    ``SessionRecord.to_session_dict()`` 把 C1 的 ``model_request.payload.tools``
    放在**顶层**。而 ``gdr.domain.schema.Session`` 没有显式的 ``tools`` 字段,
    只靠 ``model_config = ConfigDict(extra="allow")`` 兜住 —— 于是它会变成一个
    **顶层 extra 字段**, ``metadata["tools"]`` 恒为 None。

    下游全部按 metadata 读 tools, 顶层那份没人认:
      - ``gdr.refiners.usage_prune.prune_session_in_place`` 读 metadata["tools"]
        → 实测 ``tools_before=0``, **工具裁剪完全空转**
      - ``gdr.domain.schema._extract_tools_payload`` 同上 → C3 视图 tools 缺失
      - ``gdr.domain.schema.save_session_v2`` 的 ``tools_declared`` 同上 →
        **恒空** (该字段 2026-09-30 才引入, 差点就带着这个 bug 上线)

    etl 侧侥幸没坏: ``etl.writers.render_chain`` 用整份 ``model_dump()``,
    顶层那份恰好还在, 属于"蒙对"而非设计。

    与 ``etl.writers.render_chain`` 里那句注释「gdr 把它存在 metadata 里,
    这里提上来」一致 —— **metadata 才是本仓的 tools 归属地**, C1 解析器没照做。
    归一后 etl 那段提升逻辑正好接上, 顶层不再需要存在。

    2026-09-30 定案。改动点集中在这里是刻意的: 消费者一律读 metadata, 只有
    这一个生产者, 归一就该发生在生产侧而不是让 3 个消费者各写一遍 fallback。
    """
    tools = raw.pop("tools", None)
    if tools:
        # 空 tools 时不写键: 下游一律 ``.get("tools") or []``, 空列表与缺键等价,
        # 但缺键不会在 C2 里留一个永远为空的噪声字段。
        raw.setdefault("metadata", {})["tools"] = tools
    return raw


__all__ = ["from_trajectory"]