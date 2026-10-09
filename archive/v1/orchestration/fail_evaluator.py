"""orchestration.fail_evaluator — simulate 验证不通过的 LLM 归因评价.

背景 (CLAUDE.md "数据保留原则")
--------------------------------
``guide_exhausted`` / ``inconclusive`` 已从 ``task_pipeline._SIMULATE_FAIL_STATES``
移出: 轨迹结构完整、含 assistant 实质回复 (含拒答), 不进死信, 继续走 gdr → etl。

但"验证不通过"这个质量信号不能随死信一起消失 —— 人工复核需要知道每条失败轨迹
**失败在哪、是不是有代表性、该改任务还是该改模型**。本模块把「验证不通过原因」
与「agent 轨迹结果内容」一起发给 LLM 做一次归因, 产出结构化评价。

分层: 分数由 simulate 端确定性校验决定, LLM 不参与打分
------------------------------------------------------
``score`` **恒为 0.0**, 来自 ``simulate`` 端的确定性 ValidationReport ——
这是既定事实, 不是 LLM 的裁量。LLM 只负责归因分类与可读说明:

* 失败属于哪一类 (拒答 / 能力不足 / 工具缺失 / 任务歧义 / 格式违约 ...)
* 根因是什么
* 该不该改任务本身 (换个更可判定的任务) 还是改模型/提示
* 人工复核优先级

把打分交给 LLM 会让同一批数据每次评价分数漂移, 且与 ValidationReport 的
fail-closed 判定脱钩; 所以这里刻意只让 LLM 做**定性归因**, 定分由 L0
``criterion_coverage`` 读 ``final_verdict`` 决定。

fail-soft
---------
LLM 不可用 / 超时 / 返回不可解析时, 返回 ``None`` 并让主流程继续 ——
评价是增强信息, 不是前置条件。绝不让评价失败阻断 gdr → etl。
"""

from __future__ import annotations

import json
import logging
import re
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

#: raw CoT 外传红线 (CLAUDE.md 隐私红线): ``<think>...</think>`` 是 QwenPaw 的
#: **原始**推理链，未经 ``thought_refactor`` 精修，禁止发给任何外部 LLM。
#: 与 ``gdr.prompts._THINK_RE`` 同模式 —— 这里的 text block 来自 C1 trajectory，
#: 一定带 raw CoT，所以每次调用本模块的评估都必须在入 prompt 前剥离。
_THINK_BLOCK_RE = re.compile(r"<think>.*?</think>", re.DOTALL)

#: 未闭合的 ``<think>`` (截断的 trajectory) —— 连同其后全部内容一并丢弃，
#: 否则会把半截推理链当正文送出去。
_UNCLOSED_THINK_RE = re.compile(r"<think>.*\Z", re.DOTALL)

#: 评价结果的 schema 版本 (落盘契约)
SCHEMA_VERSION = "fail_evaluation.v1"

#: 注入 ``session.metadata`` 的键名 (C3 meta.json 里的字段)
FAIL_EVALUATION_METADATA_KEY = "fail_evaluation"

#: LLM 归因的重试上限 (第 2 次会带明确字段名提示)
_MAX_ATTEMPTS = 2

#: 失败归因分类。闭集 —— 便于人工按类聚合统计。
FAILURE_CATEGORIES = (
    "refusal",           # agent 主动拒答 (版权 / 道德 / 越权)
    "capability_gap",    # 想做但做不到 (推理错 / 知识缺失)
    "tool_missing",      # 缺工具 / 工具报错, 无法取证
    "task_ambiguity",    # 任务描述歧义, 多种合理解
    "format_violation",  # 内容对但格式违约
    "incomplete",        # 明显没做完
    "unknown",
)

#: 结构化输出 schema (OpenAI 兼容 ``response_format.json_schema``)
_EVALUATION_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "failure_category": {
            "type": "string",
            "enum": list(FAILURE_CATEGORIES),
            "description": "失败归因分类",
        },
        "root_cause": {
            "type": "string",
            "description": "一句话根因, 中文, 不超过 60 字",
        },
        "agent_intent": {
            "type": "string",
            "description": "agent 实际想做什么 / 说了什么, 中文, 不超过 60 字",
        },
        "should_revise_task": {
            "type": "boolean",
            "description": "该修任务定义本身(如歧义/不可判定)而非修 agent",
        },
        "review_priority": {
            "type": "string",
            "enum": ["high", "medium", "low"],
            "description": "人工复核优先级",
        },
        "review_note": {
            "type": "string",
            "description": "给人工复核者的一句话提示, 中文, 不超过 80 字",
        },
    },
    "required": [
        "failure_category",
        "root_cause",
        "agent_intent",
        "should_revise_task",
        "review_priority",
        "review_note",
    ],
    "additionalProperties": False,
}

_SYSTEM_PROMPT = (
    "你是 Agent 轨迹质量审核员。给定一次任务执行的**验证失败原因**与"
    "**agent 的实际回复内容**, 判断这次失败属于哪一类, 并给出根因与复核建议。\n"
    "只做定性归因, 不要给数字分数 (分数由验证环节决定)。\n"
    "注意: agent 拒答不等于 agent 做错了 —— 若 agent 出于版权/道德/安全理由"
    "明确拒绝, 应归类为 refusal 并把 should_revise_task 设为 true"
    "(该任务本身可能不适合作为可成功判定的评测项)。"
)


# ---------------------------------------------------------------------------
# 输入组装 (fail-soft)
# ---------------------------------------------------------------------------


def extract_final_reply(trajectory_path: Path) -> str:
    """从 C1 trajectory JSONL 里取 agent 的最后一段 text 回复。

    只读最后一条 ``model_response`` 的 ``text`` block —— 那是给人看的最终答复。

    **raw CoT 已剥离**: C1 trajectory 的 text block 内嵌 QwenPaw 的原始
    ``<think>`` 推理链。CLAUDE.md 隐私红线禁止 raw CoT 外传, 而本模块的产物
    就是"发给 LLM 的 prompt", 所以必须在此处剥离 —— 剥离发生在任何 LLM
    调用之前, 之后的上游 (prompt 组装 / 落盘) 拿到的已是干净正文。

    读不到返回空串 (调用方 fail-soft)。
    """
    path = Path(trajectory_path)
    if not path.is_file():
        return ""
    latest = ""
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(event, dict):
                continue
            if event.get("event_type") not in ("model_response", "final_reply"):
                continue
            payload = event.get("payload")
            if not isinstance(payload, dict):
                continue
            # final_reply.payload.content 是冗余快照; text block 同样取
            for block in payload.get("content") or ():
                if isinstance(block, dict) and block.get("type") == "text":
                    text = block.get("text")
                    if isinstance(text, str) and text.strip():
                        latest = text
    except (OSError, UnicodeDecodeError) as exc:
        log.warning("fail_evaluator: 读取 %s 失败: %s", path, exc)
        return ""
    return strip_raw_cot(latest)


def strip_raw_cot(text: str) -> str:
    """剥离 ``<think>...</think>`` 原始推理链 (CLAUDE.md 隐私红线).

    已闭合块整段删除; 未闭合的 ``<think>`` (trajectory 截断时) 连同其后内容
    一并丢弃 —— 半截推理链比没有推理链更危险。
    """
    if not text:
        return ""
    cleaned = _THINK_BLOCK_RE.sub("", text)
    cleaned = _UNCLOSED_THINK_RE.sub("", cleaned)
    return cleaned.strip()


def _format_failures(evaluation: dict[str, Any] | None) -> str:
    """把 ``criterion_source`` 的判定渲染成人/模型都读得懂的原因清单。"""
    if not evaluation:
        return "(未取到验证报告)"
    criteria = evaluation.get("criteria") or []
    failed = [c for c in criteria if isinstance(c, dict) and c.get("verdict") != "pass"]
    lines = [
        f"最终判定: {evaluation.get('final_verdict')}",
        f"验证轮数: {evaluation.get('rounds')}",
        f"未通过项: {len(failed)}/{len(criteria)}",
    ]
    for c in failed:
        lines.append(
            f"- [{c.get('criterion_id')}] {c.get('verdict')}: "
            f"{c.get('reason_code')} — {c.get('message')}"
        )
    missing = evaluation.get("missing_items") or []
    if missing:
        lines.append("缺失要点: " + "；".join(str(m) for m in missing))
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# 主入口
# ---------------------------------------------------------------------------


#: LLM 字段名漂移容错。后端不支持 ``response_format.json_schema`` 时
#: ``llm_client`` 会退化成"把 schema 拼进 prompt"的方式, 模型仍会改写键名
#: (实测 ``root_cause`` → ``root__agent``)。这里按顺序取第一个非空候选。
_FIELD_ALIASES: dict[str, tuple[str, ...]] = {
    "failure_category": ("failure_category", "category", "fail_category", "type"),
    "root_cause": ("root_cause", "rootCause", "root__agent", "cause", "reason"),
    "agent_intent": ("agent_intent", "intent", "agent__intent", "agent_action"),
    "should_revise_task": (
        "should_revise_task", "shouldReviseTask", "revise_task", "reviseTask",
    ),
    "review_priority": ("review_priority", "priority", "reviewPriority"),
    "review_note": ("review_note", "note", "reviewNote", "comment"),
}

#: 判定"解析成功"的最小字段集 —— 缺这些说明 LLM 没按 schema 输出, 值得重试。
_REQUIRED_FIELDS = ("failure_category", "root_cause", "review_note")


def _pick(parsed: dict[str, Any], field: str) -> Any:
    """按 ``_FIELD_ALIASES`` 取字段值, 兼容模型改写键名的情况。"""
    for key in _FIELD_ALIASES.get(field, (field,)):
        if key in parsed and parsed[key] not in (None, ""):
            return parsed[key]
    return None


def _is_complete(parsed: dict[str, Any]) -> bool:
    return all(_pick(parsed, f) not in (None, "") for f in _REQUIRED_FIELDS)


#: 解析不完整时的重试提示 (对齐 tool_fixer 的做法)
_RETRY_HINT = (
    "上轮输出不符合要求: 必须是一个 JSON 对象, 键名严格为 "
    + "/".join(_EVALUATION_SCHEMA["required"])
    + "。请立即只输出该 JSON, 不要任何解释文字。"
)


def evaluate_failed_run(
    *,
    run_id: str,
    trajectory_path: Path | None,
    criterion_evaluation: dict[str, Any] | None,
    gdr_settings: Any,
    max_reply_chars: int = 4000,
) -> dict[str, Any] | None:
    """对一次验证不通过的 run 做 LLM 归因评价。

    Args:
        run_id: simulate 端 run_id (写进结果供审计)。
        trajectory_path: C1 trajectory JSONL; 取不到回复则用空串继续。
        criterion_evaluation: ``criterion_source.load_criterion_evaluation``
            的返回值 (含 final_verdict / criteria / missing_items)。
        gdr_settings: gdr Settings (提供 llm endpoint / model / timeout)。
        max_reply_chars: 送给 LLM 的回复截断长度, 防超长 prompt。

    Returns:
        可直接写进 ``session.metadata`` 的 dict; LLM 不可用时返回 ``None``
        (调用方跳过注入, 主流程继续)。

    """
    if not criterion_evaluation:
        log.debug("fail_evaluator: run=%s 无验证报告, 跳过评价", run_id)
        return None

    final_verdict = criterion_evaluation.get("final_verdict")
    # 只评价"没通过"的; 验证通过的 run 不该带这个字段
    if final_verdict == "pass":
        return None

    reply = extract_final_reply(trajectory_path) if trajectory_path else ""
    if not reply.strip():
        log.warning(
            "fail_evaluator: run=%s 取不到 agent 回复, 仅凭失败原因评价", run_id,
        )
    if len(reply) > max_reply_chars:
        reply = reply[:max_reply_chars] + "\n...(已截断)"

    user_prompt = (
        f"## 验证失败原因\n{_format_failures(criterion_evaluation)}\n\n"
        f"## agent 的实际回复\n{reply or '(无回复内容)'}\n\n"
        "请判断失败类别与根因。"
    )

    try:
        from gdr.infrastructure.llm_client import LlamaCppClient
        from gdr.prompts import parse_json_object

        client = LlamaCppClient.get(
            gdr_settings.main_model,
            cfg=gdr_settings,
            timeout=gdr_settings.llm_timeout_s,
        )
        messages = [
            {"role": "system", "content": _SYSTEM_PROMPT},
            {"role": "user", "content": user_prompt},
        ]
        # 后端不一定支持 json_schema 约束, 模型会漂字段名/加解释文字 ——
        # 解析不完整就重试一次并明确点名字段 (对齐 tool_fixer 的重试范式)。
        parsed: dict[str, Any] = {}
        for attempt in range(_MAX_ATTEMPTS):
            attempt_messages = messages
            if attempt > 0:
                attempt_messages = list(messages) + [
                    {"role": "user", "content": _RETRY_HINT},
                ]
            text, _meta = client.chat(
                attempt_messages,
                grammar_json_schema=_EVALUATION_SCHEMA,
                max_tokens=1024,
            )
            parsed = parse_json_object(text)
            if _is_complete(parsed):
                break
            log.debug(
                "fail_evaluator: run=%s 第 %d 次解析不完整, 重试",
                run_id, attempt + 1,
            )
    except Exception as exc:  # fail-soft: 评价是增强, 不是前置条件
        log.warning(
            "fail_evaluator: run=%s LLM 归因失败 (降级为无评价): %s: %s",
            run_id, type(exc).__name__, exc,
        )
        return None

    category = str(_pick(parsed, "failure_category") or "unknown")
    if category not in FAILURE_CATEGORIES:
        category = "unknown"
    priority = str(_pick(parsed, "review_priority") or "medium")
    if priority not in ("high", "medium", "low"):
        priority = "medium"

    evaluation = {
        "schema_version": SCHEMA_VERSION,
        "run_id": run_id,
        # 分数恒为 0 —— 由 simulate 端确定性校验决定, LLM 不参与打分
        "score": 0.0,
        "score_source": "simulate_validation",
        "final_verdict": final_verdict,
        "failure_category": category,
        "root_cause": str(_pick(parsed, "root_cause") or ""),
        "agent_intent": str(_pick(parsed, "agent_intent") or ""),
        "should_revise_task": bool(_pick(parsed, "should_revise_task") or False),
        "review_priority": priority,
        "review_note": str(_pick(parsed, "review_note") or ""),
    }
    log.info(
        "fail_evaluator: run=%s verdict=%s category=%s priority=%s score=0.0",
        run_id, final_verdict, category, priority,
    )
    return evaluation


def inject_fail_evaluation(session: Any, evaluation: dict[str, Any] | None) -> bool:
    """把评价写进 ``session.metadata``。返回是否真的注入。

    ``evaluation`` 为 None 时**不清空**已有值 —— 与
    ``criterion_source.inject_criterion_evaluation`` 同语义。
    """
    if not evaluation:
        return False
    meta = session.metadata if session.metadata is not None else {}
    meta[FAIL_EVALUATION_METADATA_KEY] = evaluation
    session.metadata = meta
    return True


__all__ = [
    "SCHEMA_VERSION",
    "FAIL_EVALUATION_METADATA_KEY",
    "FAILURE_CATEGORIES",
    "extract_final_reply",
    "strip_raw_cot",
    "evaluate_failed_run",
    "inject_fail_evaluation",
]
