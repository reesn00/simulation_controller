"""etl.qwenformat.usage_prune: 按真实调用裁剪 system prompt / tools, 并泛化本机路径.

设计原则 (与全量压缩相对):
    - 删除是无损的, 压缩是有损的 —— 优先"按使用情况整段删除";
      保留下来的段 **原样保留**, 不做任何改写, 避免失真.
    - 段级拆复用 ``system_prompt.partition_system_prompt`` 的职责分类:
        - ``RETRIEVAL HEADLINE`` / ``Conversation Persistence``:
          仅当 assistant 最终文本出现 ``⟦...⟧`` headline 时保留;
        - ``THE MAP`` / ``DISCIPLINE``: 仅当调用过 ``recall_history`` 时保留;
        - ``长期记忆``: 仅当调用过 ``memory_search`` 时保留;
        - ``agent-skills``: 只保留被 ``Skill`` 工具真实调用的 skill 条目
          (条目原文保留, 仅去掉 ``<dir>`` 本机路径行);
        - ``identity`` / ``Directories`` / ``unknown``: 始终保留.
    - ``metadata.tools`` 裁剪为实际调用的工具集合.
    - 本机路径泛化: 全文件内 ``<盘符>:\\Users\\<name>`` 根路径统一替换为
      按 session_id 种子从 persona 池采样的用户名, 并顺带变化 workspace 目录名;
      文件内映射一致, 跨文件多样; 保持 Windows 风格 (与轨迹中的 shell 证据一致).

主入口 ``prune_session_in_place``: 直接在 refined session dict 上完成
system 裁剪 + tools 裁剪 + 路径泛化, 并复用
``transform.trajectory_to_session_with_openai_metadata`` 重渲染
``metadata.openai_messages`` / ``metadata.tools`` / ``metadata.qf_text``
三处副本, 保证一致.
"""

from __future__ import annotations

import hashlib
import json
import random
import re
from pathlib import Path
from typing import Any

from etl.qwenformat.system_prompt import partition_system_prompt
from etl.qwenformat.transform import trajectory_to_session_with_openai_metadata


# ---------------------------------------------------------------------------
# 使用情况采集
# ---------------------------------------------------------------------------

_HEADLINE_RE = re.compile("⟦")


def collect_usage(session: dict[str, Any]) -> dict[str, Any]:
    """扫描 session messages, 返回真实使用情况.

    Returns:
        ``{"called_tools": set[str], "called_skills": set[str],
          "has_headline": bool}``
    """
    called_tools: set[str] = set()
    called_skills: set[str] = set()
    has_headline = False

    for msg in session.get("messages", []) or []:
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
    if title == "RETRIEVAL HEADLINE" or title == "Conversation Persistence":
        return usage["has_headline"]
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


def _prune_skills_section(content: str, called_skills: set[str]) -> tuple[str, list[str], list[str]]:
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
    tools: list[dict[str, Any]], called_tools: set[str]
) -> tuple[list[dict[str, Any]], list[str]]:
    """把 tools 列表裁剪为实际调用的集合 (保序).

    被调用但不在原列表中的工具补一个最小 schema, 保证 toolcall 必有定义.
    返回 (pruned_tools, dropped_tool_names).
    """
    pruned: list[dict[str, Any]] = []
    dropped: list[str] = []
    seen: set[str] = set()
    for tdef in tools or []:
        name = _tool_name(tdef)
        if not name:
            continue
        if name in called_tools and name not in seen:
            seen.add(name)
            pruned.append(tdef)
        elif name not in called_tools:
            dropped.append(name)
    for name in called_tools - seen:
        pruned.append({
            "type": "function",
            "function": {
                "name": name,
                "description": "",
                "parameters": {"type": "object"},
            },
        })
    return pruned, dropped


# ---------------------------------------------------------------------------
# 本机路径泛化 (文件内一致, 跨文件多样)
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
    """为文件中发现的每个本机用户根路径生成确定性替换映射.

    同一逻辑路径的各转义形态 (canonical 相同) 映射到同一用户名, 各自
    保持原有分隔符风格; 同一 ``session_id`` 每次得到相同映射 (可复跑);
    不同 session 落到池内不同用户名, 达到跨文件多样化.
    """
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


def generalize_local_paths(session: dict[str, Any]) -> dict[str, str]:
    """泛化 session 全字段中的本机路径; 返回应用的根路径映射.

    - ``<盘符>:\\Users\\<name>`` 根 (含 JSON 内嵌双反斜杠形态) → persona
      池采样的新根 (按 session_id 种子);
    - ``workspaces\\default`` 目录名也从池中采样变化, 保持原分隔符风格.
    """
    session_id = session.get("session_id") or ""
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

    _collect(session)
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

    replaced = _replace_in_strings(session, replacements)
    session.clear()
    session.update(replaced)
    return mapping


# ---------------------------------------------------------------------------
# refined 文件读写 (仅新拆分四视图, 见 docs/refined_split_plan.md; 不兼容旧单文件)
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
    """
    path = Path(path)
    if not path.name.endswith(_MESSAGES_SUFFIX):
        raise ValueError(f"expect <stem>{_MESSAGES_SUFFIX}, got: {path.name}")
    base = _split_base(path)
    metadata = dict(session.get("metadata") or {})

    path.write_text(
        json.dumps({"messages": session.get("messages", [])}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    base.with_name(base.name + ".openai.json").write_text(
        json.dumps(
            {"openai_messages": metadata.get("openai_messages", [])},
            ensure_ascii=False, indent=2,
        ),
        encoding="utf-8",
    )
    qf_text = metadata.get("qf_text")
    if qf_text:
        base.with_name(base.name + ".qwenjina.txt").write_text(
            str(qf_text), encoding="utf-8"
        )
    metadata["session_id"] = session.get("session_id")
    base.with_name(base.name + ".meta.json").write_text(
        json.dumps(metadata, ensure_ascii=False, indent=2), encoding="utf-8"
    )


# ---------------------------------------------------------------------------
# 主入口
# ---------------------------------------------------------------------------

def prune_session_in_place(
    session: dict[str, Any],
    template_str: str,
    env,
) -> dict[str, Any]:
    """在 refined session dict 上执行: 路径泛化 → system 裁剪 → tools 裁剪 → 重渲染.

    ``messages`` / ``summary`` / ``metadata.openai_messages`` /
    ``metadata.tools`` / ``metadata.qf_text`` 全部同步更新; metadata 的
    其他键 (refine_history / validation_summary 等) 原样保留.

    Returns:
        stats dict (裁剪前后长度 / 丢弃的段与工具 / 路径映射).
    """
    stats: dict[str, Any] = {}

    # 1. 本机路径泛化 (先于 system 裁剪, 让新 system 直接落在泛化后的文本上)
    path_mapping = generalize_local_paths(session)
    # 不把原始根路径写进产物 (否则等于泄漏本机路径); 只记录替换后的新根
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

    # 3. tools 裁剪
    old_tools = (session.get("metadata") or {}).get("tools") or []
    new_tools, dropped_tools = prune_tools(old_tools, usage["called_tools"])
    stats["tools_before"] = len(old_tools)
    stats["tools_after"] = len(new_tools)
    stats["dropped_tools"] = dropped_tools

    # 4. 复用 qf transform 重渲染三处副本
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
    if usage["has_headline"]:
        assert "RETRIEVAL HEADLINE" in new_system, (
            "headline marker present but instruction section dropped"
        )

    stats["qf_text_chars_after"] = len(metadata["qf_text"])
    return stats
