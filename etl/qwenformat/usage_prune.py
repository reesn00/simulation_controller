"""etl.qwenformat.usage_prune: etl 阶段的 usage_prune 入口与 4 视图 I/O.

方案 etl-prune-frontload.md §6 P0 实施后:
    - 底层裁剪函数 (collect_usage / prune_system_text / prune_tools /
      generalize_local_paths / build_path_mapping) 已迁到
      ``gdr.refiners.usage_prune`` (C1→C2 阶段统一实现).
    - ``partition_system_prompt`` 已迁到 ``gdr.refiners.system_prompt``.
    - etl 本包保留:
        1. ``load_refined_session`` / ``write_refined_session`` — 4 视图 I/O
        2. ``prune_session_in_place`` — etl 阶段的完整入口 (含 qf_text 重渲染);
           scripts/prune_refined_system.py 等存量重跑工具使用此入口.
    - 实际裁剪逻辑委托给 gdr.refiners.usage_prune 的底层函数, 避免双源真值.

本文件不再含独立裁剪逻辑; 单实现真值在 gdr.refiners.usage_prune.
"""
from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, Optional

from etl.qwenformat.transform import trajectory_to_session_with_openai_metadata
from gdr.refiners.usage_prune import (
    collect_usage,
    generalize_local_paths,
    prune_session_in_place as _gdr_prune_session_in_place,
    prune_system_text,
    prune_tools,
)

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# 4 视图 I/O (拆分视图读 / 写, 仅 etl 使用)
# ---------------------------------------------------------------------------

_MESSAGES_SUFFIX = ".messages.json"


def _split_base(path: Path) -> Path:
    """``<stem>.messages.json`` → 无扩展名 stem 路径 ``<stem>``."""
    return path.with_name(path.name[: -len(_MESSAGES_SUFFIX)])


def load_refined_session(path: Path | str) -> dict[str, Any]:
    """从拆分四视图重组 session dict.

    ``path`` 为 ``<stem>.messages.json``; 读同 stem 的 messages.json +
    meta.json (openai.json / qwenjina.txt 是 meta 内字段的副本视图,
    以 meta.json 为准).
    """
    path = Path(path)
    if not path.name.endswith(_MESSAGES_SUFFIX):
        raise ValueError(f"expect <stem>{_MESSAGES_SUFFIX}, got: {path.name}")
    base = _split_base(path)
    messages_payload = json.loads(path.read_text(encoding="utf-8"))
    meta = json.loads(
        base.with_name(base.name + ".meta.json").read_text(encoding="utf-8")
    )
    session_id = meta.pop("session_id", base.name)
    return {
        "session_id": session_id,
        "messages": messages_payload.get("messages", []),
        "metadata": meta,
    }


def write_refined_session(session: dict[str, Any], path: Path | str) -> None:
    """把 session dict 按拆分四视图写回 ``<stem>.messages.json`` 同 stem 的 4 份文件.

    qf_text 缺失时跳过 qwenjina.txt (与 ``save_session`` 一致).

    F1 fix: tools 字段同步写入 messages.json 与 openai.json 顶层, 受
    ``include_tools_in_payloads`` 与 ``tools_payload_max`` 控制.
    qwenjina.txt 由 ``transform.render_sample_text`` 渲染时已传 tools,
    自带工具定义文本; 不需再注入. ``usage_prune`` 路径下 metadata["tools"]
    已是裁剪后的子集, ``openai_messages`` 也已重渲染, 同步透传即可.
    """
    path = Path(path)
    if not path.name.endswith(_MESSAGES_SUFFIX):
        raise ValueError(f"expect <stem>{_MESSAGES_SUFFIX}, got: {path.name}")
    base = _split_base(path)
    metadata = dict(session.get("metadata") or {})

    tools_payload = _extract_tools_payload_for_prune(metadata)

    messages_payload: dict[str, Any] = {"messages": session.get("messages", [])}
    if tools_payload is not None:
        messages_payload["tools"] = tools_payload
    path.write_text(
        json.dumps(messages_payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    openai_payload: dict[str, Any] = {
        "openai_messages": metadata.get("openai_messages", []),
    }
    if tools_payload is not None:
        openai_payload["tools"] = tools_payload
    base.with_name(base.name + ".openai.json").write_text(
        json.dumps(openai_payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    qf_text = metadata.get("qf_text")
    if qf_text:
        # qf_text 已由 transform.render_sample_text 渲染时传入 tools,
        # 自带工具定义文本; 不需再注入.
        base.with_name(base.name + ".qwenjina.txt").write_text(
            str(qf_text), encoding="utf-8"
        )
    metadata["session_id"] = session.get("session_id")
    base.with_name(base.name + ".meta.json").write_text(
        json.dumps(metadata, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def _extract_tools_payload_for_prune(
    metadata: dict[str, Any],
) -> Optional[list[dict[str, Any]]]:
    """F1 fix: usage_prune 路径的 tools 透传助手, 与 gdr save_session 一致."""
    cfg = _current_settings_for_prune()
    if not getattr(cfg, "include_tools_in_payloads", True):
        return None
    tools = metadata.get("tools") or []
    if not tools:
        return None
    cap = max(0, int(getattr(cfg, "tools_payload_max", 64)))
    if cap and len(tools) > cap:
        tools = tools[:cap]
    return tools


def _current_settings_for_prune() -> Any:
    """从 gdr Settings 取配置. 失败时回退到 Namespace 默认值.

    etl 与 gdr 同仓库但解耦; etl 单元测试不应强制加载根配置.
    """
    try:
        from gdr.config.settings import Settings
        return Settings()
    except Exception:
        from types import SimpleNamespace as _NS
        return _NS(
            include_tools_in_payloads=True, tools_payload_max=64,
            tools_prune_strategy="deterministic",
            tools_prune_keep_unused_min=4,
            tools_prune_keep_unused_max=12,
            tools_prune_keep_unused_ratio=0.3,
        )


# ---------------------------------------------------------------------------
# etl 阶段 prune_session_in_place (兼容层, 含 qf_text 重渲染)
# ---------------------------------------------------------------------------
#
# 设计: scripts/prune_refined_system.py 等存量工具调用此入口, 与 gdr
# 主流程 (C1→C2) 的 gdr.refiners.usage_prune.prune_session_in_place
# 不同之处在于本入口额外做 qf_text 重渲染 (etl 专属 chat_template).
#
# 底层裁剪委托给 gdr.refiners.usage_prune 的子函数, 单实现真值.

def prune_session_in_place(
    session: dict[str, Any],
    template_str: str,
    env,
    *,
    tools_prune_strategy: str | None = None,
    tools_prune_keep_unused_min: int | None = None,
    tools_prune_keep_unused_max: int | None = None,
    tools_prune_keep_unused_ratio: float | None = None,
) -> dict[str, Any]:
    """etl 阶段 prune 入口: 路径泛化 → system 裁剪 → tools 裁剪 → qf_text 重渲染.

    与 gdr 阶段入口差异: 额外做 qf_text 重渲染 (etl 专属 chat_template)
    并写回 metadata.openai_messages / tools / qf_text / qf_rendered_at / qf_stats.

    Args:
        session: refined session dict (来自 load_refined_session).
        template_str: chat_template 字符串 (etl/qwenformat/chat_template.jinja).
        env: jinja2 sandbox env.
        tools_prune_*: 覆盖 cfg 默认 (None = 走 cfg).

    Returns:
        stats dict (裁剪前后长度 / 丢弃的段与工具 / 路径映射 / qf_text 长度).
    """
    cfg = _current_settings_for_prune()

    # 1. 本机路径泛化 (CLAUDE.md 红线级别)
    path_mapping = generalize_local_paths(session)
    stats: dict[str, Any] = {}
    stats["path_new_roots"] = sorted(set(path_mapping.values()))

    messages = session.get("messages") or []
    usage = collect_usage(session)

    # 2. system 段级裁剪
    old_system = ""
    if messages and messages[0].get("role") == "system":
        old_system = "".join(
            b.get("text", "")
            for b in messages[0].get("blocks", [])
            if isinstance(b, dict) and b.get("type") == "text"
        )
    new_system, sys_stats = prune_system_text(old_system, usage)
    stats.update(sys_stats)
    stats["system_chars_before"] = len(old_system)
    stats["system_chars_after"] = len(new_system)

    if messages and messages[0].get("role") == "system":
        messages[0]["blocks"] = [
            {"type": "text", "id": messages[0].get("id", ""), "text": new_system}
        ]
    if session.get("summary"):
        session["summary"] = new_system

    # 3. tools 裁剪 (含 P0-R 未用工具随机保留)
    old_tools = (session.get("metadata") or {}).get("tools") or []
    new_tools, dropped_tools, tools_audit = prune_tools(
        old_tools, usage["called_tools"],
        session_id=session.get("session_id", ""),
        keep_unused_min=(
            tools_prune_keep_unused_min
            if tools_prune_keep_unused_min is not None
            else getattr(cfg, "tools_prune_keep_unused_min", 4)
        ),
        keep_unused_max=(
            tools_prune_keep_unused_max
            if tools_prune_keep_unused_max is not None
            else getattr(cfg, "tools_prune_keep_unused_max", 12)
        ),
        keep_unused_ratio=(
            tools_prune_keep_unused_ratio
            if tools_prune_keep_unused_ratio is not None
            else getattr(cfg, "tools_prune_keep_unused_ratio", 0.3)
        ),
        strategy=(
            tools_prune_strategy
            if tools_prune_strategy is not None
            else getattr(cfg, "tools_prune_strategy", "deterministic")
        ),
    )
    stats["tools_before"] = len(old_tools)
    stats["tools_after"] = len(new_tools)
    stats["dropped_tools"] = dropped_tools
    stats["tools_prune"] = tools_audit

    # 4. 重渲染 qf_text (etl 专属, C3 阶段的 4 视图之一)
    trajectory = {
        "session_id": session.get("session_id"),
        "summary": session.get("summary", ""),
        "source_file": session.get("source_file", ""),
        "messages": session["messages"],
        "tools": new_tools,
    }
    out = trajectory_to_session_with_openai_metadata(trajectory, template_str, env)
    metadata = session.setdefault("metadata", {})
    metadata["openai_messages"] = out["metadata"]["openai_messages"]
    metadata["tools"] = out["metadata"]["tools"]
    metadata["qf_text"] = out["metadata"]["qf_text"]
    metadata["qf_rendered_at"] = out["metadata"]["qf_rendered_at"]
    metadata["qf_stats"] = out["metadata"]["qf_stats"]
    metadata["usage_prune"] = stats

    # 5. 一致性断言
    rendered_tool_names = {_tool_name(t) for t in metadata["tools"]}
    missing = usage["called_tools"] - rendered_tool_names
    assert not missing, f"called tools missing after prune: {missing}"

    stats["qf_text_chars_after"] = len(metadata["qf_text"])
    return stats


def _tool_name(tdef: dict[str, Any]) -> str:
    """工具名提取 (与 gdr.refiners.usage_prune._tool_name 行为一致)."""
    func = tdef.get("function") if isinstance(tdef.get("function"), dict) else None
    return (func or {}).get("name") or tdef.get("name") or ""
