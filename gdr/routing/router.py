import json
import re
import logging
from typing import Optional
from difflib import SequenceMatcher

from domain import (
    Session, DefectTag, MessageHealth,
    ThinkingBlock, ToolcallBlock, ToolresultBlock, TextBlock,
)

log = logging.getLogger(__name__)

_NOISE_PATTERN = re.compile(
    r"DEBUG|Traceback|status:\s*5\d\d|Error:|\[API_MISUSE\]|FATAL|"
    r"ModuleNotFoundError|IndentationError|SyntaxError"
)

# 改进1: 从 toolresult 中提取事实实体（数值、价格、平台名等）
_FACT_VALUE_PATTERN = re.compile(
    r"(\d+(?:\.\d+)?)\s*(?:元|块|月|天|小时|年)",
)
_FACT_PLATFORM_PATTERN = re.compile(
    r"(爱奇艺|优酷|腾讯视频|B站|bilibili|1905电影网|芒果TV|搜狐视频|百度视频|"
    r"iQiyi|Youku|VIP|会员|免费|付费|点播|包月|连续包月)",
)

THOUGHT_DEFECT_TAGS = {DefectTag.THOUGHT_TOO_SHORT, DefectTag.THOUGHT_TOO_LONG}
TOOL_DEFECT_TAGS = {
    DefectTag.TOOL_JSON_INVALID, DefectTag.TOOL_HALLUCINATED,
    DefectTag.API_HALLUCINATION, DefectTag.TOOL_WRONG_SELECTION,
    DefectTag.REPETITIVE_CALL,
}
OBS_DEFECT_TAGS = {DefectTag.OBS_NOISE, DefectTag.OBS_DEBUG_LEAK}


def _input_similarity(a: str, b: str) -> float:
    return SequenceMatcher(None, a, b).ratio()


def _block_text_content(block_type: str, block) -> str:
    """抽取 block 用于 LLM 投票评审的纯文本字段。

    block 可能是 Pydantic 实例或 dict; 字段缺失返回空串。
    """
    def _g(key: str, default: str = "") -> str:
        if isinstance(block, dict):
            return block.get(key, default) or default
        return getattr(block, key, default) or default

    if block_type == "thinking":
        return _g("thinking")
    if block_type == "toolcall":
        name = _g("name")
        inp = _g("input")
        return f"name={name}\ninput={inp}"
    if block_type == "toolresult":
        name = _g("name")
        out = _g("output_text")
        return f"name={name}\noutput_text={out}"
    if block_type == "text":
        return _g("text")
    return _g("thinking") or _g("input") or _g("output_text") or _g("text")


# 投票层上下文策略 → (前向 block 数, 后向 block 数)
_VOTE_STRATEGY_SPAN = {
    "none": (0, 0),
    "±1": (1, 1),
    "±2": (2, 2),
    "pre1_post2": (1, 2),
    "pre2_post1": (2, 1),
}


class Router:
    def _rule_layer_think(self, block: ThinkingBlock, thought_min_len: int, thought_max_len: int) -> list[DefectTag]:
        tags = []
        text = block.thinking
        length = len(text)
        if length < thought_min_len:
            tags.append(DefectTag.THOUGHT_TOO_SHORT)
        elif length > thought_max_len:
            tags.append(DefectTag.THOUGHT_TOO_LONG)
        return tags

    def _rule_layer_toolcall(
        self, block: ToolcallBlock,
        tool_names: list[str], hallu_apis: set[str],
    ) -> list[DefectTag]:
        tags = []
        try:
            json.loads(block.input)
        except Exception:
            tags.append(DefectTag.TOOL_JSON_INVALID)
            return tags

        if block.name not in tool_names:
            tags.append(DefectTag.TOOL_HALLUCINATED)

        input_lower = block.input.lower()
        for api in hallu_apis:
            if api.lower() in input_lower:
                tags.append(DefectTag.API_HALLUCINATION)
                break

        return tags

    def _rule_layer_toolresult(self, block: ToolresultBlock) -> list[DefectTag]:
        tags = []
        if _NOISE_PATTERN.search(block.output_text):
            tags.append(DefectTag.OBS_DEBUG_LEAK)
        return tags

    # 改进1: Text 块事实性校验 —— 检查 text 中的数值/价格/平台名是否来自前面的 toolresult
    def _rule_layer_text(
        self, block: TextBlock, preceding_toolresults: list[ToolresultBlock],
    ) -> list[DefectTag]:
        tags = []
        if not preceding_toolresults:
            return tags

        # 提取 text 中所有事实性断言
        text_values = set()
        for m in _FACT_VALUE_PATTERN.finditer(block.text):
            text_values.add(f"{m.group(1)}|{m.group(2)}")
        text_platforms = set(m.group(1) for m in _FACT_PLATFORM_PATTERN.finditer(block.text))

        # 提取前面所有 toolresult 中的事实
        obs_values = set()
        obs_platforms = set()
        for tr in preceding_toolresults:
            for m in _FACT_VALUE_PATTERN.finditer(tr.output_text):
                obs_values.add(f"{m.group(1)}|{m.group(2)}")
            for m in _FACT_PLATFORM_PATTERN.finditer(tr.output_text):
                obs_platforms.add(m.group(1))

        # 检查 text 中的数值事实是否在 toolresult 中有依据
        unverified_values = text_values - obs_values
        if unverified_values:
            log.warning(
                "text block %s contains %d unverified values: %s",
                block.id, len(unverified_values), list(unverified_values)[:5],
            )
            tags.append(DefectTag.TEXT_FACT_HALLUCINATION)

        return tags

    # 改进2: 宏观轨迹质量评分
    def _message_health_score(
        self, blocks: list, msg_idx: int, msg_id: str, cfg,
    ) -> MessageHealth:
        health = MessageHealth(msg_idx=msg_idx, msg_id=msg_id)

        toolcall_blocks = []
        toolresult_blocks = []
        for b in blocks:
            if isinstance(b, dict):
                t = b.get("type", "")
            else:
                t = getattr(b, "type", "")
            if t == "toolcall":
                toolcall_blocks.append(b)
            elif t == "toolresult":
                toolresult_blocks.append(b)

        health.total_toolcalls = len(toolcall_blocks)
        if health.total_toolcalls == 0:
            health.is_healthy = True
            health.health_score = 1.0
            return health

        # 统计成功/失败
        first_success_idx = -1
        for i, tr in enumerate(toolresult_blocks):
            state = tr.get("state", "") if isinstance(tr, dict) else getattr(tr, "state", "")
            if state == "success":
                health.success_toolcalls += 1
                if first_success_idx == -1:
                    first_success_idx = i
            else:
                health.failed_toolcalls += 1

        health.failures_before_first_success = first_success_idx if first_success_idx >= 0 else health.total_toolcalls

        # 检测 REPETITIVE_CALL
        for i in range(len(toolcall_blocks) - 2):
            b1, b2, b3 = toolcall_blocks[i], toolcall_blocks[i + 1], toolcall_blocks[i + 2]
            n1 = b1.get("name") if isinstance(b1, dict) else getattr(b1, "name", "")
            n2 = b2.get("name") if isinstance(b2, dict) else getattr(b2, "name", "")
            n3 = b3.get("name") if isinstance(b3, dict) else getattr(b3, "name", "")
            if n1 == n2 == n3:
                i1 = b1.get("input", "") if isinstance(b1, dict) else getattr(b1, "input", "")
                i2 = b2.get("input", "") if isinstance(b2, dict) else getattr(b2, "input", "")
                i3 = b3.get("input", "") if isinstance(b3, dict) else getattr(b3, "input", "")
                if (_input_similarity(i1, i2) > 0.9 and
                        _input_similarity(i2, i3) > 0.9):
                    health.has_repetitive_loop = True
                    break

        # 检测 CONTEXT_SWITCH_LOOP
        tool_names_ordered = [
            b.get("name") if isinstance(b, dict) else getattr(b, "name", "")
            for b in toolcall_blocks
        ]
        switch_count = 0
        for j in range(1, len(tool_names_ordered)):
            prev, curr = tool_names_ordered[j - 1], tool_names_ordered[j]
            if (prev, curr) in [("browser", "execute_shell_command"), ("execute_shell_command", "browser")]:
                switch_count += 1
        if switch_count >= cfg.context_switch_threshold:
            health.has_context_switch_loop = True

        # 计算健康分数
        success_ratio = health.success_toolcalls / health.total_toolcalls
        failure_penalty = min(health.failures_before_first_success / cfg.max_failures_before_success, 1.0) * 0.4
        loop_penalty = 0.3 if health.has_repetitive_loop else 0.0
        switch_penalty = 0.3 if health.has_context_switch_loop else 0.0

        health.health_score = max(0.0, success_ratio - failure_penalty - loop_penalty - switch_penalty)
        health.is_healthy = (
            health.health_score >= cfg.message_health_min_ratio
            and health.failures_before_first_success <= cfg.max_failures_before_success
        )

        if not health.is_healthy:
            health.defects.append(DefectTag.MESSAGE_UNHEALTHY.value)

        log.debug(
            "msg[%d] health: score=%.2f, success=%d/%d, failures_before_first=%d, "
            "repetitive=%s, switch=%s, healthy=%s",
            msg_idx, health.health_score, health.success_toolcalls, health.total_toolcalls,
            health.failures_before_first_success, health.has_repetitive_loop,
            health.has_context_switch_loop, health.is_healthy,
        )
        return health

    def _rule_layer_message(
        self, blocks: list, cfg,
    ) -> dict[str, list[DefectTag]]:
        result: dict[str, list[DefectTag]] = {}

        toolcall_blocks = [b for b in blocks if isinstance(b, dict) and b.get("type") == "toolcall"]
        for i in range(len(toolcall_blocks) - (cfg.repetitive_call_threshold - 1)):
            group = toolcall_blocks[i:i + cfg.repetitive_call_threshold]
            names = [b.get("name") for b in group]
            if len(set(names)) == 1:
                inputs = [b.get("input", "") for b in group]
                all_similar = all(
                    _input_similarity(inputs[j], inputs[j + 1]) > 0.9
                    for j in range(len(inputs) - 1)
                )
                if all_similar:
                    for b in group:
                        bid = b.get("id", "")
                        if bid not in result:
                            result[bid] = []
                        result[bid].append(DefectTag.REPETITIVE_CALL)

        tool_names_ordered = [b.get("name") for b in toolcall_blocks if isinstance(b, dict)]
        switch_count = 0
        for j in range(1, len(tool_names_ordered)):
            prev, curr = tool_names_ordered[j - 1], tool_names_ordered[j]
            if (prev, curr) in [("browser", "execute_shell_command"), ("execute_shell_command", "browser")]:
                switch_count += 1
        if switch_count >= cfg.context_switch_threshold:
            for b in blocks:
                if isinstance(b, dict):
                    bid = b.get("id", "")
                    if bid not in result:
                        result[bid] = []
                    if DefectTag.CONTEXT_SWITCH_LOOP not in result[bid]:
                        result[bid].append(DefectTag.CONTEXT_SWITCH_LOOP)

        return result

    def _llm_layer(
        self, blocks_info: list[dict], session, cfg,
    ) -> dict[str, list[DefectTag]]:
        """鲁棒化的 3 票投票, 每次投票使用不同的上下文窗口策略。

        - 3 次请求各自的输入由 ``cfg.llm_vote_context_strategies`` 决定
          (默认 ``["none", "±1", "pre2_post1"]``), 覆盖裸看 / 局部窗口 /
          偏前文三种判断依据, 降低同 prompt 引发的系统性偏差。
        - 解析失败 / 超时 视为弃权 (不计入有效票)
        - 有效票数 < 2 → 不标记 (无足够信号)
        - 有效票中 ≥ 2 票 has_defect=True → 标记

        这样既保留 majority-vote 鲁棒性, 又避免单次解析错误连带全部丢分,
        同时通过输入侧的多样性降低 LLM 系统性偏差的同票放大效应。
        """
        result: dict[str, list[DefectTag]] = {}
        if not cfg.enable_llm_layer:
            return result

        # 取有效策略 (不够 3 个则补 "none", 多了截断); 无效策略降级为 "none"
        strategies = list(cfg.llm_vote_context_strategies or [])
        while len(strategies) < 3:
            strategies.append("none")
        strategies = strategies[:3]
        strategies = [
            s if s in _VOTE_STRATEGY_SPAN else "none" for s in strategies
        ]

        for info in blocks_info:
            block_id = info["block_id"]
            block_type = info.get("block_type", "")
            content = info.get("content", "")
            msg_idx = info.get("msg_idx")
            block_idx = info.get("block_idx")

            votes: list[bool] = []
            parse_errors = 0
            for vote_idx, strategy in enumerate(strategies):
                surrounding = self._build_surrounding_context(
                    session, msg_idx, block_idx, strategy,
                    max_chars=cfg.llm_vote_max_context_chars,
                )
                try:
                    from infrastructure import LlamaCppClient
                    llm = LlamaCppClient.get(cfg.main_model, cfg=cfg, timeout_s=cfg.llm_timeout_s)
                    prompt = self._build_llm_review_prompt(block_type, content, surrounding)
                    text, _ = llm.generate(
                        prompt,
                        max_tokens=256,
                        temperature=0.3,
                        timeout_s=cfg.llm_timeout_s,
                    )
                    parsed = json.loads(text) if text.strip().startswith("{") else {}
                    if "has_defect" not in parsed:
                        parse_errors += 1
                        continue
                    votes.append(bool(parsed["has_defect"]))
                except Exception as e:
                    parse_errors += 1
                    log.warning(
                        "LLM review error for block %s (vote=%d/%d strategy=%s): %s",
                        block_id, vote_idx + 1, len(strategies), strategy, e,
                    )
                    continue

            if len(votes) < 2:
                log.debug(
                    "block %s LLM review abstained (votes=%d, errors=%d, strategies=%s)",
                    block_id, len(votes), parse_errors, strategies,
                )
                continue

            defect_votes = sum(1 for v in votes if v)
            if defect_votes >= 2:
                if block_type == "thinking":
                    if DefectTag.THOUGHT_BROKEN_LOGIC not in result.get(block_id, []):
                        result.setdefault(block_id, []).append(DefectTag.THOUGHT_BROKEN_LOGIC)
                elif block_type == "toolcall":
                    if DefectTag.TOOL_WRONG_SELECTION not in result.get(block_id, []):
                        result.setdefault(block_id, []).append(DefectTag.TOOL_WRONG_SELECTION)
                elif block_type == "toolresult":
                    if DefectTag.OBS_NOISE not in result.get(block_id, []):
                        result.setdefault(block_id, []).append(DefectTag.OBS_NOISE)

        return result

    @staticmethod
    def _build_surrounding_context(
        session, msg_idx, block_idx, strategy: str, max_chars: int = 4000,
    ) -> str:
        """根据策略从 session 中提取当前 block 的相邻内容, 供投票 prompt 使用。

        - 越界/类型未知/策略为 "none" → 返回空串
        - 相邻 block 抽取为 type + id + 文本内容, 拼接为单段
        - 总字符超 ``max_chars`` 时截断尾部, 在末尾追加 "...(truncated)"
        """
        if strategy == "none" or msg_idx is None or block_idx is None:
            return ""
        if not (0 <= msg_idx < len(session.messages)):
            return ""
        if msg_idx is None or block_idx is None:
            return ""
        msg = session.messages[msg_idx]
        blocks = msg.blocks
        if not (0 <= block_idx < len(blocks)):
            return ""

        pre, post = _VOTE_STRATEGY_SPAN.get(strategy, (0, 0))
        if pre == 0 and post == 0:
            return ""

        pieces: list[str] = []
        # 前置: [block_idx - pre, block_idx)
        for i in range(max(0, block_idx - pre), block_idx):
            b = blocks[i]
            if isinstance(b, dict):
                t = b.get("type", "?")
                bid = b.get("id", "")
            else:
                t = getattr(b, "type", "?")
                bid = getattr(b, "id", "")
            content = _block_text_content(t, b)
            pieces.append(f"[前 {block_idx - i} | {t}#{bid}]\n{content}")
        # 后置: (block_idx, block_idx + post]
        for i in range(block_idx + 1, min(len(blocks), block_idx + post + 1)):
            b = blocks[i]
            if isinstance(b, dict):
                t = b.get("type", "?")
                bid = b.get("id", "")
            else:
                t = getattr(b, "type", "?")
                bid = getattr(b, "id", "")
            content = _block_text_content(t, b)
            pieces.append(f"[后 {i - block_idx} | {t}#{bid}]\n{content}")

        joined = "\n\n".join(pieces)
        if len(joined) > max_chars:
            joined = joined[:max_chars] + "\n...(truncated)"
        return joined

    def _build_llm_review_prompt(self, block_type: str, content: str, surrounding: str = "") -> str:
        if block_type == "thinking":
            role = "[角色] 思考链质量判断专家。"
            task = "判断推理链是否存在逻辑断裂"
        elif block_type == "toolcall":
            role = "[角色] 工具调用语义判断专家。"
            task = "判断工具选择是否语义错误"
        elif block_type == "toolresult":
            role = "[角色] 观测质量判断专家。"
            task = "判断观测是否含有大量无关噪声"
        else:
            return f"判断是否存在缺陷: {content}"

        ctx_part = f"[相邻上下文]\n{surrounding}\n" if surrounding else ""
        return (
            f"{role}\n"
            f"{ctx_part}"
            f"[输入] {content}\n"
            f"[任务] {task}。输出JSON: {{\"has_defect\": true|false}}"
        )

    def tag(
        self, session: Session,
        tool_names: list[str], hallu_apis: set[str], cfg,
    ) -> tuple[dict[str, list[DefectTag]], list[MessageHealth]]:
        defects_index: dict[str, list[DefectTag]] = {}
        health_scores: list[MessageHealth] = []

        for msg_idx, msg in enumerate(session.messages):
            if msg.role != "assistant":
                continue
            blocks = msg.blocks

            # 改进2: 计算消息级健康评分
            msg_id = msg.id if hasattr(msg, "id") else ""
            health = self._message_health_score(blocks, msg_idx, msg_id, cfg)
            health_scores.append(health)

            # 收集前面的 toolresult 用于 text 块事实性校验
            preceding_toolresults: list[ToolresultBlock] = []

            for i, block in enumerate(blocks):
                if isinstance(block, dict):
                    block_type = block.get("type", "")
                    block_id = block.get("id", "")
                else:
                    block_type = getattr(block, "type", "")
                    block_id = getattr(block, "id", "")

                if block_type == "thinking":
                    if isinstance(block, dict):
                        tb = ThinkingBlock(**{k: v for k, v in block.items() if k in ("type", "id", "thinking")})
                    else:
                        tb = block
                    tags = self._rule_layer_think(tb, cfg.thought_min_len, cfg.thought_max_len)  # noqa: 保留兼容签名
                elif block_type == "toolcall":
                    if isinstance(block, dict):
                        tb = ToolcallBlock(**{k: v for k, v in block.items() if k in ("type", "id", "name", "input", "state")})
                    else:
                        tb = block
                    tags = self._rule_layer_toolcall(tb, tool_names, hallu_apis)
                elif block_type == "toolresult":
                    if isinstance(block, dict):
                        tb = ToolresultBlock(**{k: v for k, v in block.items() if k in ("type", "id", "name", "output_text", "state")})
                    else:
                        tb = block
                    tags = self._rule_layer_toolresult(tb)
                    preceding_toolresults.append(tb)
                elif block_type == "text":
                    # 改进1: Text 块事实性校验
                    if cfg.enable_text_fact_check:
                        if isinstance(block, dict):
                            tb = TextBlock(**{k: v for k, v in block.items() if k in ("type", "id", "text")})
                        else:
                            tb = block
                        tags = self._rule_layer_text(tb, preceding_toolresults)
                    else:
                        tags = []
                else:
                    tags = []

                if tags:
                    defects_index.setdefault(block_id, []).extend(tags)

            # 改进2: 如果消息不健康，为所有块追加 MESSAGE_UNHEALTHY 标签
            if not health.is_healthy:
                for block in blocks:
                    bid = block.get("id", "") if isinstance(block, dict) else getattr(block, "id", "")
                    if DefectTag.MESSAGE_UNHEALTHY not in defects_index.get(bid, []):
                        defects_index.setdefault(bid, []).append(DefectTag.MESSAGE_UNHEALTHY)

            msg_level = self._rule_layer_message(blocks, cfg)
            for bid, tags in msg_level.items():
                for tag in tags:
                    if tag not in defects_index.get(bid, []):
                        defects_index.setdefault(bid, []).append(tag)

        # === LLM 投票层 ===
        # 对"规则层已命中缺陷"的 thinking/toolcall/toolresult block 做 3 票投票,
        # 3 次请求使用 cfg.llm_vote_context_strategies 配置的不同上下文窗口。
        # 不健康消息的 block 不进入投票 (健康分已覆盖)。
        candidate_blocks: list[dict] = []
        for msg_idx, msg in enumerate(session.messages):
            if msg.role != "assistant":
                continue
            mh = next((h for h in health_scores if h.msg_idx == msg_idx), None)
            if mh and not mh.is_healthy:
                continue
            for blk_idx, block in enumerate(msg.blocks):
                if isinstance(block, dict):
                    btype = block.get("type", "")
                    bid = block.get("id", "")
                else:
                    btype = getattr(block, "type", "")
                    bid = getattr(block, "id", "")
                if btype not in ("thinking", "toolcall", "toolresult"):
                    continue
                if bid not in defects_index or not defects_index[bid]:
                    continue
                candidate_blocks.append({
                    "block_id": bid,
                    "block_type": btype,
                    "content": _block_text_content(btype, block),
                    "msg_idx": msg_idx,
                    "block_idx": blk_idx,
                })

        if candidate_blocks:
            llm_tags = self._llm_layer(candidate_blocks, session, cfg)
            for bid, tags in llm_tags.items():
                for tag in tags:
                    if tag not in defects_index.get(bid, []):
                        defects_index.setdefault(bid, []).append(tag)

        return defects_index, health_scores