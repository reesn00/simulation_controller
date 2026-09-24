"""gdr.refiners.usage_prune: 按真实调用裁剪 session (系统 / 工具 / 路径).

从 etl.qwenformat.usage_prune 迁移 (方案 etl-prune-frontload.md §2.1).
在 gdr 末尾 step 22 调用, 让写出的 C2 refined Session 天然是已精简 + 已脱敏
形态; etl 不再承担结构裁剪, 仅做格式整理 + 4 视图拆分.

设计要点:
    - 接受 gdr pydantic Session, 内部转 dict 操作后写回 Session;
      与 etl dict 实现保持行为等价, 便于审计与回归.
    - 本机路径泛化是 CLAUDE.md 红线级别隐私脱敏, 与 F3-D meta_tag_strip
      同级, 在 C2 落盘前必须完成.
    - tools 列表裁剪已读 gdr.config.settings.tools_prune_*, 配置与实现
      同阶段, 消除跨阶段配置依赖.
"""
from __future__ import annotations

import hashlib
import json
import logging
import random
import re
from typing import Any, Optional

from domain import Message, Session

from gdr.refiners.system_prompt import partition_system_prompt

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# 使用情况采集
# ---------------------------------------------------------------------------

_HEADLINE_RE = re.compile("⟦")


def collect_usage(session_dict: dict[str, Any]) -> dict[str, Any]:
    """扫描 session dict, 返回真实使用情况.

    Returns:
        ``{"called_tools": set[str], "called_skills": set[str],
          "has_headline": bool}``
    """
    called_tools: set[str] = set()
    called_skills: set[str] = set()
    has_headline = False

    for msg in session_dict.get("messages", []) or []:
        for block in msg.get("blocks", []) or []:
            if not isinstance(block, dict):
                continue
            btype = block.get("type")
            if btype == "toolcall":
                name = block.get("name") or ""
                if name:
                    called_tools.add(name)
                if name == "Skill":
                    try:
                        skill = json.loads(block.get("input") or "{}").get("skill")
                    except (json.JSONDecodeError, TypeError):
                        skill = None
                    if skill:
                        called_skills.add(skill)
            elif btype == "text" and msg.get("role") == "assistant":
                if _HEADLINE_RE.search(block.get("text") or ""):
                    has_headline = True

    return {
        "called_tools": called_tools,
        "called_skills": called_skills,
        "has_headline": has_headline,
    }


# ---------------------------------------------------------------------------
# system prompt 段级裁剪
# ---------------------------------------------------------------------------

def _keep_section(title: str, kind: str, usage: dict[str, Any]) -> bool:
    """按使用情况决定某个 partition 段是否保留."""
    called_tools = usage["called_tools"]
    if title == "Conversation Persistence":
        return usage["has_headline"]
    # F3-D: RETRIEVAL HEADLINE 段已下线 (模板与 boundary 已删除).
    # 历史 qf_out 含此段时, partition 会归为 unknown; 落到 default True 分支
    # 保留 (与其它 unknown 段一致行为), 由 gdr.save_session 后续 ⟦⟧ 剥离
    # 处理训练数据污染.
    if title in ("THE MAP", "DISCIPLINE"):
        return "recall_history" in called_tools
    if title == "长期记忆":
        return "memory_search" in called_tools
    # identity / Directories / Image Understanding / agent-skills / unknown: 保留
    # (agent-skills 的内容在 _prune_skills_section 内按 skill 裁剪)
    return True


_SKILL_BLOCK_RE = re.compile(r"<skill>.*?</skill>", re.DOTALL)
_SKILL_NAME_RE = re.compile(r"<name>\s*(.*?)\s*</name>", re.DOTALL)
_SKILL_DIR_LINE_RE = re.compile(r"^<dir>.*?</dir>\s*\n?", re.MULTILINE)


def _prune_skills_section(
    content: str, called_skills: set[str]
) -> tuple[str, list[str], list[str]]:
    """只保留被真实调用的 <skill> 条目, 并删掉保留条目中的 <dir> 本机路径行.

    Returns:
        (pruned_content, kept_skill_names, dropped_skill_names)
    """
    kept: list[str] = []
    dropped: list[str] = []

    def _replace(m: re.Match[str]) -> str:
        block = m.group(0)
        name_m = _SKILL_NAME_RE.search(block)
        name = name_m.group(1) if name_m else ""
        if name not in called_skills:
            dropped.append(name)
            return ""
        kept.append(name)
        return _SKILL_DIR_LINE_RE.sub("", block)

    pruned = _SKILL_BLOCK_RE.sub(_replace, content)
    # 若一个 skill 都没留, 整个 <agent-skills> 框架也没有存在意义
    if not kept:
        return "", kept, dropped
    # 清理删除条目后留下的多余空行
    pruned = re.sub(r"\n{3,}", "\n\n", pruned)
    return pruned.strip(), kept, dropped


def prune_system_text(
    system_text: str, usage: dict[str, Any]
) -> tuple[str, dict[str, Any]]:
    """按使用情况裁剪 system prompt, 保留段原样拼接.

    Returns:
        (new_system_text, stats) — stats 含 dropped_sections / kept_skills /
        dropped_skills.
    """
    stats: dict[str, Any] = {
        "dropped_sections": [],
        "kept_skills": [],
        "dropped_skills": [],
    }
    parts: list[str] = []
    for sec in partition_system_prompt(system_text):
        if sec.kind == "framework":
            stats["dropped_sections"].append(sec.title or "framework")
            continue
        if not _keep_section(sec.title, sec.kind, usage):
            stats["dropped_sections"].append(sec.title)
            continue
        content = sec.content
        if sec.title == "agent-skills":
            content, kept, dropped = _prune_skills_section(
                content, usage["called_skills"]
            )
            stats["kept_skills"] = kept
            stats["dropped_skills"] = dropped
            if not content:
                stats["dropped_sections"].append(sec.title)
                continue
        parts.append(content.strip())
    return "\n\n".join(p for p in parts if p), stats


# ---------------------------------------------------------------------------
# tools 列表裁剪
# ---------------------------------------------------------------------------

def _tool_name(tdef: dict[str, Any]) -> str:
    func = tdef.get("function") if isinstance(tdef.get("function"), dict) else None
    return (func or {}).get("name") or tdef.get("name") or ""


def prune_tools(
    tools: list[dict[str, Any]],
    called_tools: set[str],
    *,
    session_id: str = "",
    keep_unused_min: int = 0,
    keep_unused_max: int = 0,
    keep_unused_ratio: float = 0.0,
    strategy: str = "none",
) -> tuple[list[dict[str, Any]], list[str], dict[str, Any]]:
    """把 tools 列表裁剪为实际调用的集合 (保序) + 可选保留 unused 子集.

    被调用但不在原列表中的工具补一个最小 schema, 保证 toolcall 必有定义.

    P0-R fix: SFT 训练样本若只展示被调工具, 模型会学到"工具列表短 = 该调用"
    的错误相关. 通过 ``keep_unused_min`` / ``keep_unused_max`` /
    ``keep_unused_ratio`` 联合控制从 unused 池随机保留若干个未用工具.

    Returns:
        (pruned_tools, dropped_tool_names, audit) — audit 含 strategy /
        kept_unused / kept_unused_count / sampled_from_pool_size.
    """
    pruned: list[dict[str, Any]] = []
    dropped: list[str] = []
    seen: set[str] = set()
    called_in_order: list[dict[str, Any]] = []
    unused_pool: list[dict[str, Any]] = []

    for tdef in tools or []:
        name = _tool_name(tdef)
        if not name:
            continue
        if name in called_tools and name not in seen:
            seen.add(name)
            called_in_order.append(tdef)
        elif name not in called_tools:
            unused_pool.append(tdef)
            dropped.append(name)

    # P0-R: 按 session_id 种子从 unused 池随机保留 (跨样本多样, 样本内确定)
    audit: dict[str, Any] = {
        "strategy": strategy,
        "kept_unused": [],
        "kept_unused_count": 0,
        "sampled_from_pool_size": len(unused_pool),
    }
    if strategy == "deterministic" and unused_pool:
        ratio_n = int(len(unused_pool) * keep_unused_ratio)
        n_keep = max(keep_unused_min, min(keep_unused_max, ratio_n))
        n_keep = min(n_keep, len(unused_pool))
        if n_keep > 0:
            rng = random.Random(
                _seed_from_session(session_id or "unknown") ^ 0x7E4E
            )
            kept_unused_tdefs = rng.sample(unused_pool, n_keep)
            # 按原 tools 顺序追加 (稳定, 便于审计)
            kept_names = {_tool_name(t) for t in kept_unused_tdefs}
            for tdef in unused_pool:
                if _tool_name(tdef) in kept_names:
                    pruned.append(tdef)
                    kept_names.discard(_tool_name(tdef))
            audit["kept_unused"] = sorted(
                _tool_name(t) for t in kept_unused_tdefs
            )
            audit["kept_unused_count"] = len(audit["kept_unused"])
            # 真正保留的 unused 不再算 dropped
            audit_kept_set = set(audit["kept_unused"])
            dropped = [n for n in dropped if n not in audit_kept_set]

    # called 在前段 (保原 tools 顺序)
    pruned = called_in_order + pruned

    # 缺失的 called 工具补最小 schema
    for name in called_tools - seen:
        pruned.append({
            "type": "function",
            "function": {
                "name": name,
                "description": "",
                "parameters": {"type": "object"},
            },
        })

    return pruned, dropped, audit


# ---------------------------------------------------------------------------
# 本机路径泛化 (CLAUDE.md 红线级别隐私脱敏)
# ---------------------------------------------------------------------------

# persona 池: 保持 Windows 风格 (轨迹 shell 证据为 Windows), 只变化用户名
_USERNAME_POOL = [
    "alice", "bzhao", "chengl", "dengyy", "erik", "fangfei", "guotao",
    "huangsi", "ivan", "jliu", "kai", "linmq", "meihan", "nathans",
    "oliver", "pengyu", "qianq", "renj", "suny", "tomas",
]

_WORKSPACE_POOL = ["default", "main", "assist", "ws01", "agent", "primary"]

# 用户根路径: 分隔符可能是 1/2/4 个反斜杠 —— 单反斜杠是正常形态;
# 双反斜杠来自 JSON 内嵌 (toolresult 的 output_text 本身是 JSON 字符串);
# 四反斜杠来自 stdout 里 print 的 dict repr 再被多层 JSON 转义.
# 用反向引用 ``\1`` 保证 ``Users`` 两侧分隔符一致.
_WIN_USER_ROOT_RE = re.compile(r"[A-Za-z]:(\\{1,4})Users\1[^\\/\s\"']+")


def _seed_from_session(session_id: str) -> int:
    return int(hashlib.sha256(session_id.encode("utf-8")).hexdigest()[:16], 16)


def _canon_root(root: str) -> str:
    """把分隔符归一到单反斜杠并小写, 作为同一逻辑路径的键."""
    return re.sub(r"\\{2,}", lambda _m: "\\", root).lower()


def build_path_mapping(session_id: str, roots: list[str]) -> dict[str, str]:
    """为文件中发现的每个本机用户根路径生成确定性替换映射."""
    rng = random.Random(_seed_from_session(session_id or "unknown"))
    by_canon: dict[str, list[str]] = {}
    for root in roots:
        by_canon.setdefault(_canon_root(root), []).append(root)
    name_for: dict[str, str] = {
        canon: rng.choice(_USERNAME_POOL) for canon in sorted(by_canon)
    }
    mapping: dict[str, str] = {}
    for canon, variants in by_canon.items():
        for root in variants:
            m = re.match(r"^([A-Za-z]:)(\\+)", root)
            drive, sep = (m.group(1), m.group(2)) if m else ("C:", "\\")
            mapping[root] = f"{drive}{sep}Users{sep}{name_for[canon]}"
    return mapping


def _replace_in_strings(
    obj: Any, replacements: list[tuple[re.Pattern[str], Any]]
) -> Any:
    """递归替换数据结构里所有字符串值 (repl 可为字面量或 callable)."""
    if isinstance(obj, str):
        for pat, repl in replacements:
            if callable(repl):
                obj = pat.sub(repl, obj)
            else:
                obj = pat.sub(lambda _m, r=repl: r, obj)
        return obj
    if isinstance(obj, dict):
        return {k: _replace_in_strings(v, replacements) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_replace_in_strings(v, replacements) for v in obj]
    return obj


def generalize_local_paths(session_dict: dict[str, Any]) -> dict[str, str]:
    """泛化 session dict 全字段中的本机路径; 返回应用的根路径映射.

    - ``<盘符>:\\Users\\<name>`` 根 (含 JSON 内嵌双反斜杠形态) → persona
      池采样的新根 (按 session_id 种子);
    - ``workspaces\\default`` 目录名也从池中采样变化, 保持原分隔符风格.

    这是 CLAUDE.md 红线级别的隐私脱敏 —— C2 文件绝不能含原始本机路径.
    """
    session_id = session_dict.get("session_id") or ""
    roots: list[str] = []

    def _collect(obj: Any) -> None:
        if isinstance(obj, str):
            roots.extend(m.group(0) for m in _WIN_USER_ROOT_RE.finditer(obj))
        elif isinstance(obj, dict):
            for v in obj.values():
                _collect(v)
        elif isinstance(obj, list):
            for v in obj:
                _collect(v)

    _collect(session_dict)
    mapping = build_path_mapping(session_id, roots)
    if not mapping:
        return {}

    rng = random.Random(_seed_from_session(session_id or "unknown") ^ 0x5EED)
    workspace = rng.choice(_WORKSPACE_POOL)

    replacements: list[tuple[re.Pattern[str], Any]] = [
        (re.compile(re.escape(root), re.IGNORECASE), new_root)
        for root, new_root in mapping.items()
    ]
    # workspace 目录名: 覆盖 1/2/4 反斜杠形态, 保持原分隔符
    replacements.append((
        re.compile(r"workspaces(\\{1,4})default(?=\1|[\"'\s]|$)"),
        lambda m: f"workspaces{m.group(1)}{workspace}",
    ))
    # 长 root 在前, 避免前缀遮蔽
    replacements.sort(key=lambda r: -len(r[0].pattern))

    replaced = _replace_in_strings(session_dict, replacements)
    session_dict.clear()
    session_dict.update(replaced)
    return mapping


# ---------------------------------------------------------------------------
# 配置读取 (gdr.config.settings, 失败回退 Namespace 默认)
# ---------------------------------------------------------------------------

def _current_usage_prune_cfg() -> Any:
    """从 gdr Settings 取 usage_prune 配置; 失败回退 Namespace 默认."""
    try:
        from gdr.config.settings import Settings
        return Settings()
    except Exception:
        from types import SimpleNamespace as _NS
        return _NS(
            usage_prune_enabled=True,
            tools_prune_strategy="deterministic",
            tools_prune_keep_unused_min=4,
            tools_prune_keep_unused_max=12,
            tools_prune_keep_unused_ratio=0.3,
        )


# ---------------------------------------------------------------------------
# 主入口
# ---------------------------------------------------------------------------

def prune_session_in_place(
    session: Session | dict[str, Any],
    cfg: Any | None = None,
) -> dict[str, Any]:
    """在 Session / session dict 上执行: 路径泛化 → system 裁剪 → tools 裁剪.

    接受 ``Session`` (pydantic) 或 ``dict``; 内部统一转 dict 操作后写回.
    etl 兼容: dict 输入直接 in-place, 返回 stats.

    Returns:
        stats dict (裁剪前后长度 / 丢弃的段与工具 / 路径映射 / tools_prune audit).
    """
    cfg = cfg if cfg is not None else _current_usage_prune_cfg()
    if not getattr(cfg, "usage_prune_enabled", True):
        return {"skipped": True, "reason": "usage_prune_enabled=False"}

    stats: dict[str, Any] = {}

    # 1. 转 dict 统一操作 (pydantic Session → dict)
    if isinstance(session, Session):
        session_dict: dict[str, Any] = session.model_dump(mode="json")
    else:
        session_dict = dict(session)

    # 2. 本机路径泛化 (CLAUDE.md 红线, 必须先于 system 裁剪)
    path_mapping = generalize_local_paths(session_dict)
    # 不把原始根路径写进产物 (否则等于泄漏本机路径); 只记录替换后的新根
    stats["path_new_roots"] = sorted(set(path_mapping.values()))

    messages = session_dict.get("messages") or []
    usage = collect_usage(session_dict)

    # 3. system 段级裁剪
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
    if session_dict.get("summary"):
        session_dict["summary"] = new_system

    # 4. tools 裁剪 (含 P0-R 未用工具随机保留)
    old_tools = (session_dict.get("metadata") or {}).get("tools") or []
    _default_cfg = _current_usage_prune_cfg()
    new_tools, dropped_tools, tools_audit = prune_tools(
        old_tools, usage["called_tools"],
        session_id=session_dict.get("session_id", ""),
        keep_unused_min=int(getattr(_default_cfg, "tools_prune_keep_unused_min", 4)),
        keep_unused_max=int(getattr(_default_cfg, "tools_prune_keep_unused_max", 12)),
        keep_unused_ratio=float(getattr(_default_cfg, "tools_prune_keep_unused_ratio", 0.3)),
        strategy=str(getattr(_default_cfg, "tools_prune_strategy", "deterministic")),
    )
    stats["tools_before"] = len(old_tools)
    stats["tools_after"] = len(new_tools)
    stats["dropped_tools"] = dropped_tools
    stats["tools_prune"] = tools_audit

    # 5. 写回 metadata.tools + usage_prune stats
    metadata = session_dict.setdefault("metadata", {})
    metadata["tools"] = new_tools
    metadata["usage_prune"] = stats

    # 6. 一致性断言
    rendered_tool_names = {_tool_name(t) for t in new_tools}
    missing = usage["called_tools"] - rendered_tool_names
    assert not missing, f"called tools missing after prune: {missing}"

    # 7. 写回 Session (若 pydantic 输入)
    if isinstance(session, Session):
        # 仅更新受影响的字段; messages 用 Message 解析自动转 pydantic
        session.messages = [Message.model_validate(m) for m in messages]
        session.metadata = metadata
        session.summary = session_dict.get("summary", "")

    return stats
