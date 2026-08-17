from __future__ import annotations

from simulate_serve.domain.validation import ValidationReport, Verdict

from .guidance_policy import guidance_level_for_round, select_guidance_gaps
from .models import InteractionContext


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
    return f"""# 角色
你正在扮演：{task.persona.role_description}
背景：{task.persona.background or '无额外背景'}
语气：{task.persona.tone}

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
def build_followup_prompt(context: InteractionContext, report: ValidationReport) -> str:
    criteria = {item.criterion_id: item for item in context.task.criteria}
    failed = select_guidance_gaps(context.task, report)
    gaps = "\n".join(
        f"- 缺口：{item.message}\n  建议表达：{context.task.interaction_policy.guidance_by_reason.get(item.reason_code) or criteria[item.criterion_id].remediation.guidance}"
        for item in failed
    ) or "- 没有应归因给远端执行者的可修复缺口"
    passed = [item for item in report.criteria if item.verdict is Verdict.PASS]
    progress = "、".join(criteria[item.criterion_id].description for item in passed if item.criterion_id in criteria) or "暂无"
    regressed = "、".join(
        criteria[item].description
        for item in context.regressed_criteria
        if item in criteria
    ) or "无"
    recent = "\n".join(f"{turn.role}: {turn.content}" for turn in context.conversation[-6:])
    guidance_level = guidance_level_for_round(context.guide_rounds)
    guidance_instruction = {
        "L2": "指出问题所在方向和原因，不给出答案或具体参数。",
        "L3": "给出建议的修正步骤顺序，但不代替对方完成任务。",
        "L4": "明确说明下一步应如何修正，但仍不直接提供最终答案。",
    }[guidance_level]
    return f"""当前对话：
{recent}

已经满足的内容：
{progress}

本轮回复相较此前遗漏的内容：
{regressed}

尚未满足的项目：
{gaps}

当前引导等级：{guidance_level}
表达要求：{guidance_instruction}

请以当前用户身份自然追问，只针对以上可修复缺口；要求对方保留已经满足的内容，并返回一份包含全部要求的完整的修订结果。不得提及验证器、准则 ID、工具内部错误或测试。"""
