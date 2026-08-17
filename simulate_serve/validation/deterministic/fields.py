import json

from simulate_serve.domain.task import AcceptanceCriterion
from simulate_serve.domain.validation import CriterionResult

from .common import failed, normalize, passed
from .format import markdown_table


class FieldValidator:
    def validate(self, criterion: AcceptanceCriterion, text: str) -> CriterionResult:
        fields = [str(item) for item in criterion.parameters.get("fields", [])]
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError:
            parsed = None
        if isinstance(parsed, dict):
            missing = [field for field in fields if not self._has_path(parsed, field)]
        else:
            table = markdown_table(text)
            if table:
                headers = {normalize(item) for item in table[0]}
                missing = [field for field in fields if normalize(field) not in headers]
            else:
                haystack = normalize(text)
                missing = [field for field in fields if normalize(field) not in haystack]
        return failed(criterion, "FIELD_MISSING", f"缺少字段：{' / '.join(missing)}") if missing else passed(criterion, "必需字段完整")

    @staticmethod
    def _has_path(value: dict, path: str) -> bool:
        current: object = value
        for part in path.split("."):
            if not isinstance(current, dict) or part not in current:
                return False
            current = current[part]
        return True
