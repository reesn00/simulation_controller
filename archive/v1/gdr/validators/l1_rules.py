from domain import ThinkingBlock, ToolcallBlock, ToolresultBlock, BlockUnion, DefectTag
import json
import re

_NOISE_PATTERN = re.compile(
    r"DEBUG|Traceback|\[API_MISUSE\]|FATAL|ModuleNotFoundError|IndentationError|SyntaxError"
)


def _extract_entities(text: str, cfg=None) -> set[str]:
    """复用 core.context_understanding._extract_entities 的多语种分发 + jieba 集成.

    原因: 旧实现只抽双引号串 / 单引号串 / 硬编码 10 个工具名, 中文 thinking
    里大量关键术语 (如 "正则表达式" / "分词器" / 平台名 / 项目名) 全部漏掉,
    L1 的 `orig_entities.issubset(new_entities)` 对中文几乎是摆设, refiner
    改写时丢中文术语 L1 看不到.

    CU 实现已覆盖: 引号串 / CamelCase / 工具名白名单 / 字段名 / 数字 /
    日文/韩文过滤 / 中文 jieba.extract_tags(top_k=10) + 缺包降级 1~4 字窗口 +
    `enable_jieba_entity_extraction=False` 显式禁用开关. 这里直接复用, 不再
    维护两份抽取逻辑.

    行为兼容: cfg=None 时 CU 走默认 (use_jieba=True, 缺包自动降级), 与原行为
    相比只会**增加**抽到的实体, 不会减少 — 严格包含原集合, 对既有英文 fixture
    不会破坏. 新增中文术语约束是软增强, refiner 改写时丢中文术语会被 L1 拦下.
    """
    from core.context_understanding import _extract_entities as _cu_extract_entities
    return _cu_extract_entities(text, cfg=cfg)


def _check_thought(original_block: ThinkingBlock, refined_content: dict, max_len: int, cfg=None) -> bool:
    refined = refined_content.get("thinking", "")
    if not refined:
        return False
    if len(refined) > max_len:
        return False
    orig_entities = _extract_entities(original_block.thinking, cfg=cfg)
    new_entities = _extract_entities(refined, cfg=cfg)
    if not orig_entities.issubset(new_entities):
        return False
    return True


def _check_toolcall(original_block: ToolcallBlock, refined_content: dict, tool_names: list[str]) -> bool:
    name = refined_content.get("name", "")
    inp = refined_content.get("input", "")
    if name not in tool_names:
        return False
    try:
        json.loads(inp)
    except Exception:
        return False
    return True


def _check_toolresult(original_block: ToolresultBlock, refined_content: dict) -> bool:
    output_text = refined_content.get("output_text", "")
    if not output_text:
        return False
    if _NOISE_PATTERN.search(output_text):
        return False
    return True


def check(original_block: BlockUnion, refined_content: dict, tool_names: list[str], thought_max_len_l1: int = 2000, cfg=None) -> bool:
    if isinstance(original_block, dict):
        block_type = original_block.get("type", "")
    else:
        block_type = getattr(original_block, "type", "")

    if block_type == "thinking":
        tb = original_block if isinstance(original_block, ThinkingBlock) else ThinkingBlock(**original_block)
        return _check_thought(tb, refined_content, thought_max_len_l1, cfg=cfg)
    elif block_type == "toolcall":
        tb = original_block if isinstance(original_block, ToolcallBlock) else ToolcallBlock(**original_block)
        return _check_toolcall(tb, refined_content, tool_names)
    elif block_type == "toolresult":
        tb = original_block if isinstance(original_block, ToolresultBlock) else ToolresultBlock(**original_block)
        return _check_toolresult(tb, refined_content)
    return True