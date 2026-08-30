import json
import re
import logging
from concurrent.futures import ThreadPoolExecutor
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

# 投票只会追加一个语义标签 (thinking→BROKEN_LOGIC / toolcall→WRONG_SELECTION /
# toolresult→OBS_NOISE)。当块已有的规则标签命中的决策分支先于/等价于该语义分支时,
# 追加语义标签不改变任何下游决策 (core/policy.py 决策表), 投票纯属浪费 → 跳过。
#   thinking: TOO_SHORT 与 BROKEN_LOGIC 的策略等价; CONTEXT_SWITCH 分支更早;
#             仅 THOUGHT_TOO_LONG + BROKEN_LOGIC 会把 PRUNE 翻成 REPAIR/DEFER → 保留投票
#   toolcall: REPETITIVE/CONTEXT_SWITCH 分支更早, JSON_INVALID/HALLUCINATED/API_HALLU
#             与 WRONG_SELECTION 同分支
#   toolresult: DEBUG_LEAK 与 OBS_NOISE 同分支, CONTEXT_SWITCH 分支更早
_VOTE_REDUNDANT_TAGS: dict[str, set[DefectTag]] = {
    "thinking": {DefectTag.THOUGHT_TOO_SHORT, DefectTag.CONTEXT_SWITCH_LOOP},
    "toolcall": {
        DefectTag.REPETITIVE_CALL, DefectTag.CONTEXT_SWITCH_LOOP,
        DefectTag.TOOL_JSON_INVALID, DefectTag.TOOL_HALLUCINATED,
        DefectTag.API_HALLUCINATION,
    },
    "toolresult": {DefectTag.OBS_DEBUG_LEAK, DefectTag.CONTEXT_SWITCH_LOOP},
}

# 改进1: 从 toolresult 中提取事实实体（数值、价格、平台名等）
_FACT_VALUE_PATTERN = re.compile(
    r"(\d+(?:\.\d+)?)\s*(元|块|月|天|小时|年)",
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
        context_understanding=None,
    ) -> dict[str, list[DefectTag]]:
        """级联投票 + 线程池并发。

        - 每个候选块先投"首票" (CU 全局状态上下文); 判无缺陷直接放行 (1 次调用)。
        - 判有缺陷时用局部窗口上下文 (强制 surrounding, 绕过 CU 状态捷径) 补一票确认,
          两票一致才标记; 分歧保守取无缺陷。假阳性会触发 PRUNE/多余精修, 代价高于假阴性。
        - 块间投票相互独立, 用 ThreadPoolExecutor 并发; 实际 LLM 并发由
          llm_client 的生成信号量 (llm_concurrency) 兜底。
        - 解析失败 / 超时 视为弃权; 弃权不标记。
        """
        result: dict[str, list[DefectTag]] = {}
        if not cfg.enable_llm_layer or not blocks_info:
            return result

        # 取有效策略 (不够 3 个则补 "none", 多了截断); 无效策略降级为 "none"
        strategies = list(cfg.llm_vote_context_strategies or [])
        while len(strategies) < 3:
            strategies.append("none")
        strategies = strategies[:3]
        strategies = [
            s if s in _VOTE_STRATEGY_SPAN else "none" for s in strategies
        ]

        max_workers = max(1, min(int(getattr(cfg, "llm_concurrency", 4)), len(blocks_info)))
        with ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="gdr-vote") as pool:
            votes = list(pool.map(
                lambda info: self._vote_block(
                    info, session, cfg, context_understanding, strategies,
                ),
                blocks_info,
            ))

        for block_id, tag in votes:
            if tag is not None:
                result.setdefault(block_id, []).append(tag)
        return result

    def _vote_block(
        self, info: dict, session, cfg, context_understanding,
        strategies: list[str],
    ) -> tuple[str, Optional[DefectTag]]:
        """单个候选块的级联投票。返回 (block_id, 语义标签或 None)。"""
        block_id = info["block_id"]
        block_type = info.get("block_type", "")
        try:
            first = self._single_vote(
                info, session, cfg, context_understanding,
                strategy=strategies[0], force_surrounding=False,
            )
            if first is not True:
                return block_id, None
            # 确认票: 选一个非 "none" 策略并强制 surrounding 上下文,
            # 保证确认票看到的是局部原文而非同一份 CU 状态摘要
            confirm_strategy = next(
                (s for s in strategies[1:] if s != "none"), "±1",
            )
            second = self._single_vote(
                info, session, cfg, context_understanding,
                strategy=confirm_strategy, force_surrounding=True,
            )
            if second is not True:
                log.debug(
                    "block %s vote not confirmed (v1=defect, v2=%s), skip tag",
                    block_id, second,
                )
                return block_id, None
        except Exception as e:
            log.warning("LLM vote failed for block %s: %s", block_id, e)
            return block_id, None

        tag = {
            "thinking": DefectTag.THOUGHT_BROKEN_LOGIC,
            "toolcall": DefectTag.TOOL_WRONG_SELECTION,
            "toolresult": DefectTag.OBS_NOISE,
        }.get(block_type)
        return block_id, tag

    def _single_vote(
        self, info: dict, session, cfg, context_understanding,
        *, strategy: str, force_surrounding: bool,
    ) -> Optional[bool]:
        """投一票。返回 True/False; 解析失败/请求异常返回 None (弃权)。"""
        block_id = info["block_id"]
        block_type = info.get("block_type", "")
        content = info.get("content", "")
        context_text = self._build_review_context(
            session, info.get("msg_idx"), info.get("block_idx"), block_id, strategy,
            context_understanding=context_understanding, cfg=cfg,
            force_surrounding=force_surrounding,
        )
        try:
            from infrastructure import LlamaCppClient
            llm = LlamaCppClient.get(cfg.main_model, cfg=cfg, timeout=cfg.llm_timeout_s)
            prompt = self._build_llm_review_prompt(block_type, content, context_text)
            messages = [{"role": "user", "content": prompt}]
            text, _ = llm.chat(
                messages,
                # reasoning 模型的思考 token 计入 max_tokens, 预算过小 → content 为空弃权
                max_tokens=1024,
                temperature=0.3,
                timeout_s=cfg.llm_timeout_s,
            )
            from prompts import parse_json_object
            parsed = parse_json_object(text)
            if "has_defect" not in parsed:
                log.warning(
                    "LLM review unparseable for block %s (strategy=%s)",
                    block_id, strategy,
                )
                return None
            return bool(parsed["has_defect"])
        except Exception as e:
            log.warning(
                "LLM review error for block %s (strategy=%s): %s",
                block_id, strategy, e,
            )
            return None

    def _build_review_context(
        self,
        session, msg_idx, block_idx, block_id: str, strategy: str,
        context_understanding=None, cfg=None, force_surrounding: bool = False,
    ) -> str:
        """组装 Router LLM 评审所需的上下文。

        当 ``cfg.llm_vote_use_cu=True`` 且 ``context_understanding`` 可用时,
        使用 CU archive/view 作为注入上下文; 否则回退到旧 ±N surrounding。
        ``force_surrounding=True`` 跳过 CU 捷径, 直接用 ±N 局部原文 ——
        用于确认票, 保证与首票 (CU 全局状态) 输入不同源。
        """
        cfg = cfg or self.cfg if hasattr(self, "cfg") else None
        if cfg is None:
            return self._build_surrounding_context(
                session, msg_idx, block_idx, strategy, max_chars=4000,
            )

        if (
            not force_surrounding
            and getattr(cfg, "llm_vote_use_cu", False)
            and context_understanding is not None
        ):
            try:
                # 优先注入增量状态追踪的最新快照（方案 §3.2）
                if getattr(cfg, "context_state_tracker_enabled", True):
                    state = context_understanding.latest_state()
                    if state and (state.task_goal or state.key_entities or state.completed_actions):
                        state_text = state.render()
                        if state_text:
                            return state_text
                # 回退到 archive 渲染
                cu_text = context_understanding.render_archive_for_block(
                    block_id,
                    max_chars=cfg.cu_prompt_max_chars,
                    strategy=cfg.cu_prompt_archive_strategy,
                )
                if cu_text:
                    return cu_text
            except Exception as e:
                log.warning("CU prompt rendering failed for block %s: %s", block_id, e)

        return self._build_surrounding_context(
            session, msg_idx, block_idx, strategy,
            max_chars=cfg.llm_vote_max_context_chars,
        )

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

    def _build_llm_review_prompt(self, block_type: str, content: str, context_text: str = "") -> str:
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

        ctx_part = f"[会话上下文]\n{context_text}\n" if context_text else ""
        return (
            f"{role}\n"
            f"{ctx_part}"
            f"[输入] {content}\n"
            f"[任务] {task}。输出JSON: {{\"has_defect\": true|false}}"
        )

    def tag(
        self, session: Session,
        tool_names: list[str], hallu_apis: set[str], cfg,
        *, context_understanding=None,
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
        # 对"规则层已命中缺陷"的 thinking/toolcall/toolresult block 做级联投票
        # (首票 + 确认票), 不健康消息的 block 不进入投票 (健康分已覆盖)。
        # 语义标签不改变决策的块 (llm_vote_skip_rule_decidable, 默认开) 直接跳过。
        candidate_blocks: list[dict] = []
        skip_decidable = getattr(cfg, "llm_vote_skip_rule_decidable", True)
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
                block_defects = defects_index.get(bid) or []
                if not block_defects:
                    continue
                if skip_decidable:
                    redundant = _VOTE_REDUNDANT_TAGS.get(btype, set())
                    if redundant and redundant.intersection(block_defects):
                        log.debug(
                            "block %s skips LLM vote: tags %s cannot change policy",
                            bid, [d.value for d in block_defects],
                        )
                        continue
                candidate_blocks.append({
                    "block_id": bid,
                    "block_type": btype,
                    "content": _block_text_content(btype, block),
                    "msg_idx": msg_idx,
                    "block_idx": blk_idx,
                })

        if candidate_blocks:
            llm_tags = self._llm_layer(
                candidate_blocks, session, cfg,
                context_understanding=context_understanding,
            )
            for bid, tags in llm_tags.items():
                for tag in tags:
                    if tag not in defects_index.get(bid, []):
                        defects_index.setdefault(bid, []).append(tag)

        return defects_index, health_scores