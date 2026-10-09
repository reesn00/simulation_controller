from simulate_serve.domain.task import AcceptanceCriterion
from simulate_serve.domain.validation import CriterionResult
from simulate_serve.validation.claims import extract_claims

from .common import failed, passed
from .format import list_items, markdown_table


class CountValidator:
    def validate(self, criterion: AcceptanceCriterion, text: str) -> CriterionResult:
        if "min_chars" in criterion.parameters:
            minimum = int(criterion.parameters["min_chars"])
            actual = len(text.strip())
            return passed(criterion, f"字符数 {actual}") if actual >= minimum else failed(criterion, "CHAR_COUNT_LOW", f"回复长度不足：{actual}/{minimum}")
        minimum = int(criterion.parameters.get("min_items", 1))
        unit = str(criterion.parameters.get("unit", "auto"))
        table = markdown_table(text)
        if unit == "table_rows":
            actual = len(table[1]) if table else 0
        elif unit == "list_items":
            actual = len(list_items(text))
        elif unit == "urls":
            actual = len({item.value for item in extract_claims(text) if item.kind == "url"})
        else:
            actual = len(table[1]) if table else len(list_items(text))
        return passed(criterion, f"结果项 {actual}") if actual >= minimum else failed(criterion, "ITEM_COUNT_LOW", f"结果项不足：{actual}/{minimum}")
