from __future__ import annotations

from simulate_serve.domain.validation import ValidationReport, Verdict

from .guidance_policy import (
    build_guidance_directive,
    normalize_verbosity,
    pick_guidance_variant,
)
from .models import InteractionContext


_VERBOSITY_STYLE = {
    "concise": "说话简短直接，一次只说一件事，一般一两句话就把要求说完。",
    "moderate": "说话清楚完整，但不啰嗦。",
    "detailed": "可以把要求说完整说清楚，但不要重复。",
}


def build_system_prompt(context: InteractionContext) -> str:
    task = context.task
    criteria = "\n".join(f"- [{item.criterion_id}] {item.description}" for item in task.criteria)
    constraints = "\n".join(f"- {item.text}" for item in task.constraints) or "- 无额外约束"
    excluded = "、".join(task.excluded_platforms) or "无"
    intent_context = "\n".join(f"- {item}" for item in task.intent.context) or "- 无额外上下文"
    priorities = "\n".join(
        f"- {item.priority}: {item.requirement}" for item in task.intent.priorities
    ) or "- 以初始请求为准"
    fallback_plan = "\n".join(
        f"- 当{item.trigger}时：{item.guidance}" for item in task.fallback_plan
    ) or "- 没有预设降级方案；无法完成时应诚实说明"
    verbosity = _VERBOSITY_STYLE[normalize_verbosity(task.persona.verbosity)]
    return f"""# 角色
你正在扮演：{task.persona.role_description}
背景：{task.persona.background or '无额外背景'}
语气：{task.persona.tone}
表达习惯：{verbosity}

# 任务
{task.task_prompt}

# 真实意图
{task.intent.goal or task.task_prompt}

# 用户上下文
{intent_context}

# 需求优先级
{priorities}

# 可接受的降级路径
{fallback_plan}

# 内部满意标准（不要对外复述）
{criteria}

# 排除平台
{excluded}

# 交互协议
{task.interaction_policy.protocol}

# 约束
{constraints}
- 只输出用户要说的话，不输出思考过程、标题或内部规则。"""

_NO_LEAK_RULE = "- 不得提及验证器、准则 ID、工具内部错误或测试。"

_LEVEL_INSTRUCTIONS = {
    "L2": "指出问题所在方向和原因，不给出答案或具体参数。",
    "L3": "给出建议的修正步骤顺序，但不代替对方完成任务。",
    "L4": "明确说明下一步应如何修正，但仍不直接提供最终答案。",
}

_EMPHASIS_INSTRUCTIONS = {
    "first": "",
    "repeat": "以下问题已经追问过仍未解决，请优先点出这一点。",
    "regress": "对方这次把之前已经满足的部分弄丢了，请明确指出并要求合并。",
}


def build_followup_prompt(context: InteractionContext, report: ValidationReport) -> str:
    """Build the simulated user's follow-up brief.

    The prompt carries only user-owned material (what the user already said
    or wants) plus scenario-authored phrasing hints. Criterion remediation
    text is never injected as a suggested expression: the language layer must
    paraphrase from the gap semantics, and the deterministic gates in the
    actor reject echoes of the internal wording.
    """
    task = context.task
    directive = build_guidance_directive(context, report)
    failed = directive.gaps
    criteria = {item.criterion_id: item for item in task.criteria}
    gap_lines = "\n".join(
        f"- [{item.reason_code}] {item.message}" for item in failed
    ) or "- 没有应归因给远端执行者的可修复缺口"
    hint_lines: list[str] = []
    for item in failed:
        variants = task.interaction_policy.guidance_by_reason.get(item.reason_code) or ()
        first = pick_guidance_variant(variants, context.run_id, item.reason_code, context.guide_rounds)
        second = pick_guidance_variant(
            variants,
            context.run_id,
            item.reason_code,
            context.guide_rounds,
            exclude={first[0]} if first else None,
        )
        options = [text for text, _ in (pick for pick in (first, second) if pick is not None)]
        if options:
            hint_lines.append(f"- 针对[{item.reason_code}]（同义改写，禁止照抄）：{' / '.join(options)}")
    hints = "\n".join(hint_lines) or "- 参照你最初提的要求，用你自己的话把不满意的地方说出来"
    requirements = "\n".join(
        f"- {item.requirement}" for item in task.intent.priorities if item.priority == "required"
    ) or f"- {task.intent.goal or task.task_prompt}"
    if task.interaction_policy.acknowledge_progress:
        passed = [item for item in report.criteria if item.verdict is Verdict.PASS]
        progress = "、".join(criteria[item.criterion_id].description for item in passed if item.criterion_id in criteria) or "暂无"
        progress_section = f"""
已经满足的内容：
{progress}
"""
    else:
        progress_section = ""
    regressed = "、".join(
        criteria[item].description
        for item in context.regressed_criteria
        if item in criteria
    ) or "无"
    recent = "\n".join(f"{turn.role}: {turn.content}" for turn in context.conversation[-6:])
    emphasis_instruction = _EMPHASIS_INSTRUCTIONS[directive.emphasis]
    if directive.emphasis == "repeat":
        repeated = "、".join(
            criteria[item].description for item in directive.repeated_criteria if item in criteria
        )
        if repeated:
            emphasis_instruction = f"{repeated}：{emphasis_instruction}"
    no_leak_rules = f"{_NO_LEAK_RULE}\n" if task.interaction_policy.never_expose_internal_rules else ""
    return f"""当前对话：
{recent}
{progress_section}
本轮回复相较此前遗漏的内容：
{regressed}

你要表达的不满（供你理解，不是让你念的稿子）：
{gap_lines}

你最初提的要求（可以重申，但要用自己的口气）：
{requirements}

可参考口径：
{hints}

当前引导等级：{directive.guidance_level}
表达要求：{_LEVEL_INSTRUCTIONS[directive.guidance_level]}
{emphasis_instruction}
{no_leak_rules}请以当前用户身份自然追问，只针对以上要点；要求对方保留已经满足的内容，并返回一份包含全部要求的完整的修订结果。"""
