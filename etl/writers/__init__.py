"""etl.writers — C3 4 视图写入层.

新架构 ``simulation server → gdr → etl`` 下，etl 在尾部统一做格式整理：
usage_prune + transform + system prompt partition + tool templates +
tool output summarizer + save_session_v2 拆 4 视图。

本包是 etl 写入 C3 文件的唯一入口；具体拆 4 视图的实现来自
``gdr.domain.schema.save_session_v2``（gdr 拥有 pydantic 类型与拆 4 视图逻辑），
etl 只编排调用链。
"""
from __future__ import annotations

from pathlib import Path

from gdr.domain.schema import SessionOutputs, save_session_v2


def render_to_4_views(
    c2_path: Path,
    base_path: Path,
    *,
    settings: dict | None = None,
) -> dict[str, Path | None]:
    """C2 refined Session → etl 处理链 → C3 4 视图文件.

    调用链：
      1. ``etl.parsers.load_refined_session`` 读 C2
      2. ``etl.qwenformat.usage_prune.collect_usage`` 取真实调用的 tools/skills
      3. ``etl.qwenformat.usage_prune.prune_session_in_place`` 裁
         system/tools + 重渲染 ``metadata.qf_text``
      4. ``etl.qwenformat.transform.trajectory_to_session_with_openai_metadata``
         写 ``metadata.openai_messages`` / ``qf_rendered_at`` / ``qf_stats``
      5. ``etl.qwenformat.system_prompt.partition_system_prompt`` 切分 system 段
      6. ``etl.qwenformat.tool_templates.save_tool_templates`` tool schema 持久化
      7. ``etl.qwenformat.tool_output_summarizer.summarize_record`` tool result 精简
      8. ``gdr.domain.schema.save_session_v2`` 拆 4 视图落盘

    Args:
        c2_path: 输入 C2 refined Session 路径
        base_path: 输出 4 视图文件的 stem（无扩展名；如 ``.../xxx_refined``）
        settings: etl/qwenformat 配置（chat_template_path 等）；None 时走默认

    Returns:
        dict 含 4 路径键值 ``{"messages": Path, "openai": Path,
        "qwenjina": Path | None, "meta": Path}``——与
        ``SessionOutputs`` 字段一一对应。
    """
    raise NotImplementedError(
        "etl.writers.render_to_4_views 将在 migration-plan §2 step 1 实现"
    )


__all__ = ["render_to_4_views", "SessionOutputs"]