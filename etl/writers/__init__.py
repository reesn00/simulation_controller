"""etl.writers — C3 4 视图写入层.

新架构 ``simulation server → gdr → etl`` 下，etl 在尾部统一做格式整理：
usage_prune (前移到 gdr step 22, C2 已脱敏) + transform + system prompt
partition + tool templates + tool output summarizer + save_session_v2 拆 4 视图.

本包是 etl 写入 C3 文件的唯一入口；具体拆 4 视图的实现来自
``gdr.domain.schema.save_session_v2``（gdr 拥有 pydantic 类型与拆 4 视图逻辑），
etl 只编排调用链。

方案 etl-prune-frontload.md §5.2: 入口先调 ``etl.parsers.gate_then_load``
做训练集准入门控, 通过门控后才进入 transform/summarizer 链.
"""
from __future__ import annotations

import logging
from pathlib import Path

from etl.parsers import gate_then_load
from gdr.domain.schema import SessionOutputs, save_session_v2

log = logging.getLogger(__name__)


def render_to_4_views(
    c2_path: Path,
    base_path: Path,
    *,
    settings: dict | None = None,
) -> dict[str, Path | None] | None:
    """C2 refined Session → etl 处理链 → C3 4 视图文件.

    调用链：
      1. ``etl.parsers.gate_then_load`` 门控 (reject → audit 旁路, 不入流程);
         compare_fail → meta 标 compare_warn
      2. ``etl.qwenformat.usage_prune.prune_session_in_place`` (前移到 gdr 后,
         C2 已是已精简 + 已脱敏形态, etl 不再调; 该步为兼容性占位, 实际 noop)
      3. ``etl.qwenformat.transform.trajectory_to_session_with_openai_metadata``
         写 ``metadata.openai_messages`` / ``qf_rendered_at`` / ``qf_stats``
      4. ``etl.qwenformat.system_prompt.partition_system_prompt`` 切分 system 段
      5. ``etl.qwenformat.tool_templates.save_tool_templates`` tool schema 持久化
      6. ``etl.qwenformat.tool_output_summarizer.summarize_record`` tool result 精简
      7. ``gdr.domain.schema.save_session_v2`` 拆 4 视图落盘

    Args:
        c2_path: 输入 C2 refined Session 路径
        base_path: 输出 4 视图文件的 stem（无扩展名；如 ``.../xxx_refined``）
        settings: etl/qwenformat 配置（chat_template_path 等）；None 时走默认

    Returns:
        dict 含 4 路径键值 ``{"messages": Path, "openai": Path,
        "qwenjina": Path | None, "meta": Path}``——与
        ``SessionOutputs`` 字段一一对应。
        返回 None 表示门控 reject (已落 audit 旁路), 调用方无需后续处理.
    """
    # step 1: 门控 (reject → return None, audit 旁路已在 gate_then_load 内部处理)
    session = gate_then_load(c2_path)
    if session is None:
        log.warning(
            "render_to_4_views: %s rejected by gate; audit path populated, "
            "skipping 4-views render",
            c2_path.name,
        )
        return None

    # step 2-7: transform + summarizer + 4 视图拆分
    # TODO(etl-migration): 实现步骤 2-7 的完整调用链.
    # 当前 etl.writers 仍处于 migration-plan §2 step 1 阶段, 详见
    # docs/设计方案/etl-prune-frontload.md §6 P1.
    raise NotImplementedError(
        "etl.writers.render_to_4_views 步骤 2-7 仍在 migration-plan §2 step 1; "
        "当前实现确保 step 1 (gate_then_load) 接入, 门控 reject 时正确返回 None. "
        "完整实现见后续 PR."
    )


__all__ = ["render_to_4_views", "SessionOutputs", "save_session_v2", "gate_then_load"]
