from simulate_serve.domain.task import AcceptanceCriterion
from simulate_serve.domain.validation import CriterionResult

from .common import failed, normalize, passed


class KeywordValidator:
    def validate(self, criterion: AcceptanceCriterion, text: str) -> CriterionResult:
        keywords = [normalize(str(item)) for item in criterion.parameters.get("keywords", []) if str(item).strip()]
        mode = str(criterion.parameters.get("mode", "all"))
        haystack = normalize(text)
        present = [item for item in keywords if item in haystack]
        if mode == "all" and len(present) != len(keywords):
            missing = [item for item in keywords if item not in present]
            return failed(criterion, "KEYWORD_MISSING", f"缺少关键信息：{' / '.join(missing)}")
        if mode == "any" and keywords and not present:
            return failed(criterion, "KEYWORD_ANY_MISSING", "未包含任一要求的关键信息")
        if mode == "none" and present:
            return failed(criterion, "KEYWORD_FORBIDDEN", f"包含禁止信息：{' / '.join(present)}")
        return passed(criterion, "关键信息检查通过")
