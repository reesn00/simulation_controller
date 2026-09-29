"""etl.writers.render_chain — C2 Session → C3 渲染链 (F1 接线).

背景
----
新架构 ``simulation server → gdr → etl`` 下，gdr step 22 已经完成
**路径泛化 + system 段裁剪 + tools 裁剪**（``gdr.refiners.usage_prune``），
但 **qf_text 渲染**是 etl 专属（用 etl 自己的 ``chat_template.jinja``），
历史上一直没人调用 —— 导致：

* ``*.openai.json`` 恒为 ``{"openai_messages": []}``
* ``*.qwenjina.txt`` 因缺 ``qf_text`` 而根本不写文件

本模块把这一步接上。``save_session_v2`` 读
``session.metadata["openai_messages"]`` / ``["qf_text"]`` /
``_extract_tools_payload`` 读 ``session.metadata["tools"]``，
所以渲染结果统一并入 ``session.metadata``。

步骤边界（方案 etl-prune-frontload.md 的前移决策）
------------------------------------------------
| 步骤 | 归属 | 状态 |
|---|---|---|
| 路径泛化 / system 裁剪 / tools 裁剪 | **gdr step 22** | 已完成，etl **不重跑** |
| qf_text 渲染 (本模块) | **etl** | 本模块补上 |
| system prompt 重排 (``render_cleaned_system``) | 未接线 | 见 ``NOTE_SYSTEM_RECOMPOSE`` |
| tool 模板落盘 (``save_tool_templates``) | 未接线 | 见 ``NOTE_TOOL_TEMPLATES`` |
| tool_output_summarizer | 未接线 | 见 ``NOTE_OUTPUT_SUMMARIZER`` |

三个未接线步骤都**不是** F1 验收项所必需（见方案 §12.1），且各自有副作用，
不默认开启 —— 详见对应常量处的说明。
"""

from __future__ import annotations

import logging
from functools import lru_cache
from pathlib import Path
from typing import Any

from gdr.domain.schema import Session

log = logging.getLogger(__name__)

#: etl 专属 chat_template（Qwen3 模板）。与 gdr 阶段无关 —— gdr 不渲 qf_text。
DEFAULT_TEMPLATE_PATH = (
    Path(__file__).resolve().parents[1] / "qwenformat" / "chat_template.jinja"
)

#: 本地 section/tool 模板根目录（供未来的 system 重排 / tool 模板落盘使用）。
DEFAULT_TEMPLATES_DIR = Path(__file__).resolve().parents[1] / "qwenformat" / "templates"

#: transform 写入 session.metadata 的键。前三个是 C3 契约字段，其余为审计信息。
RENDER_METADATA_KEYS = (
    "openai_messages",
    "tools",
    "qf_text",
    "qf_rendered_at",
    "qf_stats",
)

# --- 未接线步骤说明 -------------------------------------------------------
#
# NOTE_SYSTEM_RECOMPOSE: ``etl.qwenformat.system_prompt.render_cleaned_system``
#   会**改写 system 段内容**并注入 tool schema 文本。它必须在 qf_text 渲染
#   **之前**执行（否则渲出来的是旧 system），而 F1 的目标只是补上缺失的
#   渲染步骤。gdr 已在 step 22 做过段级裁剪，因此这里的增量收益需要用
#   真实批次对比 ``qf_text`` 差异后再决定，不在 F1 范围内。
#
# NOTE_TOOL_TEMPLATES: ``save_tool_templates`` 会往
#   ``etl/qwenformat/templates/tools/`` **写文件**。这是跨 session 的共享
#   目录，写入时机与并发策略需要单独设计。
#
# NOTE_OUTPUT_SUMMARIZER: ``summarize_record`` 的 LLMAnchored 实现会调
#   LLM 做 tool result 精简。**绝不能**默认接进生产链路：每 session 一次
#   LLM 调用，成本与失败率都不可控，且会改变训练数据语义。


class RenderChainError(RuntimeError):
    """渲染链失败（模板缺失 / 渲染异常）。调用方应按可重试处理。"""


@lru_cache(maxsize=4)
def _load_template(template_path: str | None) -> str:
    """读 chat_template；结果按路径缓存（批处理下避免每 session 一次磁盘读）。"""
    path = Path(template_path) if template_path else DEFAULT_TEMPLATE_PATH
    if not path.exists():
        raise RenderChainError(f"chat_template 不存在: {path}")
    return path.read_text(encoding="utf-8")


@lru_cache(maxsize=1)
def _build_env() -> Any:
    """Jinja sandbox 环境；全局复用（构造开销不小且无状态差异）。"""
    from etl.qwenformat.transform import build_chat_env

    return build_chat_env()


#: Qwen3 chat_template 硬要求的 role —— 没有 user 轮次时模板直接抛
#: ``No user query found in messages``。``No messages provided`` 则是空列表。
#: 两者都是**数据形态问题**，不是环境问题，不应让 etl 阶段失败。
_TEMPLATE_REQUIRES_USER = True


def _can_render_qf_text(session: Session) -> bool:
    """Qwen3 模板能否渲染：至少要有 1 条 user 消息。

    ``Message.role`` 被 pydantic 约束为 system / user / assistant，
    因此"只有 assistant / 只有 system"的 session 会触发模板的
    ``No user query found``。这类 session 本来也构不成训练样本。
    """
    for msg in session.messages or []:
        role = getattr(msg, "role", None)
        if role is None and isinstance(msg, dict):
            role = msg.get("role")
        if role == "user":
            return True
    return not _TEMPLATE_REQUIRES_USER


def apply_render_chain(
    session: Session,
    *,
    template_path: Path | str | None = None,
) -> Session:
    """给 C2 Session 补齐 C3 渲染产物（**就地**改 ``session.metadata``）。

    调用 :func:`etl.qwenformat.transform.trajectory_to_session_with_openai_metadata`
    并把结果 **merge** 进 ``session.metadata``。注意 transform 返回的是一个
    *全新的* metadata dict（只含渲染相关键），**不能**直接赋值替换 ——
    那会抹掉 ``validation_summary`` / ``training_value_score`` /
    ``refine_history`` 等全部 gdr 评分信号，而它们是评分卡 L3/L4/L5 的数据源。

    Args:
        session: gdr 产出的 C2 refined Session（pydantic）。
        template_path: chat_template 路径；None 用
            :data:`DEFAULT_TEMPLATE_PATH`。

    Returns:
        同一个 session 对象（就地修改），便于链式调用。

    Raises:
        RenderChainError: 模板缺失或渲染失败。
    """
    # 空 session / 无 user 轮次的 session：chat_template 会抛
    # "No messages provided" 或 "No user query found in messages"。这是
    # **数据问题**不是环境问题 —— 让整个 etl 阶段失败会误标 dead，跳过渲染即可
    # （qf_text 缺失 → save_session_v2 不写 qwenjina.txt，这是诚实信号）。
    if not _can_render_qf_text(session):
        log.warning(
            "etl render_chain: session=%s 无 user 轮次，跳过 qf_text 渲染",
            session.session_id,
        )
        return session

    # transform 吃 dict；输出只用于取 metadata，messages 保持原样不回写。
    trajectory: dict[str, Any] = session.model_dump(mode="json")

    # transform 把 ``trajectory["tools"]`` 当作 OpenAI tools schema 的权威来源
    # （含 description / parameters）。gdr 把它存在 metadata 里，这里提上来。
    # 缺失时 transform 会回退到"从 toolcall 名推导"的空 schema 兜底。
    existing_tools = (session.metadata or {}).get("tools") or []
    if existing_tools:
        trajectory["tools"] = existing_tools

    from etl.qwenformat.transform import trajectory_to_session_with_openai_metadata

    try:
        rendered = trajectory_to_session_with_openai_metadata(
            trajectory,
            _load_template(str(template_path) if template_path else None),
            _build_env(),
        )
    except RenderChainError:
        raise
    except Exception as exc:  # noqa: BLE001 — 统一包装，调用方按 RenderChainError 处理
        raise RenderChainError(
            f"qf_text 渲染失败 (session={session.session_id!r}): "
            f"{type(exc).__name__}: {exc}"
        ) from exc

    rendered_meta = rendered.get("metadata") or {}
    meta = session.metadata if session.metadata is not None else {}
    for key in RENDER_METADATA_KEYS:
        if key in rendered_meta:
            meta[key] = rendered_meta[key]
    session.metadata = meta

    openai_count = len(meta.get("openai_messages") or [])
    qf_len = len(meta.get("qf_text") or "")
    log.info(
        "etl render_chain: session=%s openai_messages=%d qf_text_chars=%d tools=%d",
        session.session_id,
        openai_count,
        qf_len,
        len(meta.get("tools") or []),
    )
    if openai_count == 0:
        # 不 raise：空轨迹是数据问题不是渲染问题，交由调用方按 incomplete 处理。
        log.warning(
            "etl render_chain: session=%s 渲染出 0 条 openai_message；"
            "qf_text 可能为空（检查上游 messages 是否有 assistant 块）",
            session.session_id,
        )

    return session
