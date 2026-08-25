from domain import ThinkingBlock, ToolcallBlock, ToolresultBlock, BlockUnion, DefectTag
import json
import re

_NOISE_PATTERN = re.compile(
    r"DEBUG|Traceback|\[API_MISUSE\]|FATAL|ModuleNotFoundError|IndentationError|SyntaxError"
)


def _extract_entities(text: str) -> set[str]:
    entities = set()
    for match in re.finditer(r'"([^"]+)"', text):
        entities.add(match.group(1))
    for match in re.finditer(r"'([^']+)'", text):
        entities.add(match.group(1))
    for match in re.finditer(r"\b(browser|execute_shell_command|write_file|read_file|search_file|list_files|glob|grep|tavily_search)\b", text, re.IGNORECASE):
        entities.add(match.group(1).lower())
    return entities


def _check_thought(original_block: ThinkingBlock, refined_content: dict, max_len: int) -> bool:
    refined = refined_content.get("thinking", "")
    if not refined:
        return False
    if len(refined) > max_len:
        return False
    orig_entities = _extract_entities(original_block.thinking)
    new_entities = _extract_entities(refined)
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


def check(original_block: BlockUnion, refined_content: dict, tool_names: list[str], thought_max_len_l1: int = 2000) -> bool:
    if isinstance(original_block, dict):
        block_type = original_block.get("type", "")
    else:
        block_type = getattr(original_block, "type", "")

    if block_type == "thinking":
        tb = original_block if isinstance(original_block, ThinkingBlock) else ThinkingBlock(**original_block)
        return _check_thought(tb, refined_content, thought_max_len_l1)
    elif block_type == "toolcall":
        tb = original_block if isinstance(original_block, ToolcallBlock) else ToolcallBlock(**original_block)
        return _check_toolcall(tb, refined_content, tool_names)
    elif block_type == "toolresult":
        tb = original_block if isinstance(original_block, ToolresultBlock) else ToolresultBlock(**original_block)
        return _check_toolresult(tb, refined_content)
    return True