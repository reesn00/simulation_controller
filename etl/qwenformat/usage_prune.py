"""etl.qwenformat.usage_prune: 按真实调用裁剪 system prompt / tools, 并泛化本机路径.

设计原则 (与全量压缩相对):
    - 删除是无损的, 压缩是有损的 —— 优先"按使用情况整段删除";
      保留下来的段 **原样保留**, 不做任何改写, 避免失真.
    - 段级拆复用 ``system_prompt.partition_system_prompt`` 的职责分类:
        - ``Conversation Persistence``:
          仅当 assistant 最终文本出现 ``⟦...⟧`` headline 时保留 (兼容历史
          qf_out, F3-D 后该判定仍可用于过滤其它段);
        - ``THE MAP`` / ``DISCIPLINE``: 仅当调用过 ``recall_history`` 时保留;
        - ``长期记忆``: 仅当调用过 ``memory_search`` 时保留;
        - ``agent-skills``: 只保留被 ``Skill`` 工具真实调用的 skill 条目
          (条目原文保留, 仅去掉 ``<dir>`` 本机路径行);
        - ``identity`` / ``Directories`` / ``unknown``: 始终保留.
        - ``RETRIEVAL HEADLINE`` 段已下线 (F3-D): 模板删除, boundary 移除;
          历史 qf_out 含此段时不再被 partition 识别, 自然归入 unknown 段.
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
    ``keep_unused_ratio`` 联合控制从 unused 池随机保留若干个未用工具. 保留数量
    计算: ``min(len(unused), max) * ratio`` 后与 ``min`` 取 max; 同 session_id
    多次调用结果一致 (按 session_id 种子采样); 不同 session_id 跨样本多样.

    Args:
        tools: 原始 tools 列表 (来自 trajectory).
        called_tools: 真实调用的工具名集合 (来自 collect_usage).
        session_id: 用于 deterministic 采样的种子源.
        keep_unused_min: 至少保留几个 unused 工具 (下界).
        keep_unused_max: 最多保留几个 unused 工具 (上界).
        keep_unused_ratio: 按 unused 池比例采样的上限, 与 max 取 min.
        strategy: ``"none"`` 仅保留 called (旧行为); ``"deterministic"`` 按
            session_id 种子采样.

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
# 主入口
# ---------------------------------------------------------------------------

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
    """在 refined session dict 上执行: 路径泛化 → system 裁剪 → tools 裁剪 → 重渲染.

    ``messages`` / ``summary`` / ``metadata.openai_messages`` /
    ``metadata.tools`` / ``metadata.qf_text`` 全部同步更新; metadata 的
    其他键 (refine_history / validation_summary 等) 原样保留.

    P0-R fix: ``tools_prune_*`` 形参控制 tools 随机保留 unused 子集行为
    (默认从 gdr Settings 取, 失败回退到 ``strategy="deterministic"`` 默认值);
    显式传 ``None`` 等价"用默认", 显式传字符串``"none"`` / 数字 0 等价
    "强制关闭".

    Returns:
        stats dict (裁剪前后长度 / 丢弃的段与工具 / 路径映射 / tools_prune audit).
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

    # 3. tools 裁剪 (含 P0-R 未用工具随机保留)
    old_tools = (session.get("metadata") or {}).get("tools") or []
    _default_cfg = _current_settings_for_prune()
    new_tools, dropped_tools, tools_audit = prune_tools(
        old_tools, usage["called_tools"],
        session_id=session.get("session_id", ""),
        keep_unused_min=(
            tools_prune_keep_unused_min
            if tools_prune_keep_unused_min is not None
            else getattr(_default_cfg, "tools_prune_keep_unused_min", 4)
        ),
        keep_unused_max=(
            tools_prune_keep_unused_max
            if tools_prune_keep_unused_max is not None
            else getattr(_default_cfg, "tools_prune_keep_unused_max", 12)
        ),
        keep_unused_ratio=(
            tools_prune_keep_unused_ratio
            if tools_prune_keep_unused_ratio is not None
            else getattr(_default_cfg, "tools_prune_keep_unused_ratio", 0.3)
        ),
        strategy=(
            tools_prune_strategy
            if tools_prune_strategy is not None
            else getattr(_default_cfg, "tools_prune_strategy", "deterministic")
        ),
    )
    stats["tools_before"] = len(old_tools)
    stats["tools_after"] = len(new_tools)
    stats["dropped_tools"] = dropped_tools
    stats["tools_prune"] = tools_audit

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
    # F3-D: RETRIEVAL HEADLINE 段已下线. 即便 assistant 文本含 ⟦⟧, 也不再
    # 强约束 cleaned system 必须保留该 instruction 段. 训练数据中的 ⟦⟧
    # 由 gdr.refiners.meta_tag_strip 在 save_session 落盘前剥离.

    stats["qf_text_chars_after"] = len(metadata["qf_text"])
    return stats
