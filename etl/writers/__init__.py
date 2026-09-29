"""etl.writers — C3 4 视图写入层.

新架构 ``simulation server → gdr → etl`` 下，etl 在尾部统一做格式整理：
usage_prune (前移到 gdr step 22, C2 已脱敏) + transform + system prompt
partition + tool templates + tool output summarizer + save_session_v2 拆 4 视图.

本包是 etl 写入 C3 文件的唯一入口；具体拆 4 视图的实现来自
``gdr.domain.schema.save_session_v2``（gdr 拥有 pydantic 类型与拆 4 视图逻辑），
etl 只编排调用链。

方案 etl-prune-frontload.md §5.2: 入口先调 ``etl.parsers.gate_then_load``
做训练集准入门控, 通过门控后才进入渲染链.

**2026-09-28 (F1)**: 步骤 2-7 骨架已补齐 —— 实际只需接 ``apply_render_chain``
(gdr step 22 已完成路径泛化 / system 裁剪 / tools 裁剪, etl 唯一缺的是
etl 专属 ``qf_text`` 渲染)。接通后 ``*.openai.json`` 不再是空数组,
``*.qwenjina.txt`` 开始生成。
"""
from __future__ import annotations

import logging
from pathlib import Path

from etl.parsers import gate_then_load
from etl.writers.render_chain import RenderChainError, apply_render_chain
from gdr.domain.schema import SessionOutputs, save_session_v2

log = logging.getLogger(__name__)


def render_to_4_views(
    c2_path: Path,
    base_path: Path,
    *,
    settings: dict | None = None,
    template_path: Path | str | None = None,
) -> dict[str, Path | None] | None:
    """C2 refined Session → etl 处理链 → C3 4 视图文件.

    调用链：
      1. ``etl.parsers.gate_then_load`` 门控 (reject → audit 旁路, 不入流程);
         compare_fail → meta 标 compare_warn
      2. ``etl.writers.render_chain.apply_render_chain`` — 路径泛化 / system 裁剪 /
         tools 裁剪已前移到 **gdr step 22**, etl 不重跑; 本步只补 etl 专属的
         ``qf_text`` 渲染, 写 ``metadata.openai_messages`` / ``tools`` /
         ``qf_text`` / ``qf_rendered_at`` / ``qf_stats``
      3. ``gdr.domain.schema.save_session_v2`` 拆 4 视图落盘

    未接线的三步 (system prompt 重排 / tool 模板落盘 / tool_output_summarizer)
    各自有副作用, 不在 F1 范围, 理由见 ``etl.writers.render_chain`` 顶部注释。

    Args:
        c2_path: 输入 C2 refined Session 路径
        base_path: 输出 4 视图文件的 stem（无扩展名；如 ``.../xxx_refined``）
        settings: etl/qwenformat 配置（chat_template_path 等）；None 时走默认
        template_path: chat_template 覆盖路径；None 用
            :data:`etl.writers.render_chain.DEFAULT_TEMPLATE_PATH`

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

    # step 2: qf_text 渲染 (补 openai_messages / tools / qf_text 到 metadata)
    apply_render_chain(session, template_path=template_path)

    # step 3: 拆 4 视图落盘
    outputs = save_session_v2(session, base_path)
    return {
        "messages": outputs.messages,
        "openai": outputs.openai,
        "qwenjina": outputs.qwenjina,
        "meta": outputs.meta,
    }


__all__ = [
    "render_to_4_views",
    "apply_render_chain",
    "RenderChainError",
    "SessionOutputs",
    "save_session_v2",
    "gate_then_load",
]
