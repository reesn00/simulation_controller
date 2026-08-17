from urllib.parse import urlsplit

from simulate_serve.domain.task import AcceptanceCriterion
from simulate_serve.domain.validation import CriterionResult
from simulate_serve.validation.claims import extract_claims

from .common import failed, passed


class UrlSyntaxValidator:
    def validate(self, criterion: AcceptanceCriterion, text: str) -> CriterionResult:
        urls = [item.value for item in extract_claims(text) if item.kind == "url"]
        minimum = int(criterion.parameters.get("min_items", 1))
        valid = []
        for url in urls:
            parsed = urlsplit(url)
            if parsed.scheme in {"http", "https"} and parsed.hostname and not parsed.username and not parsed.password:
                valid.append(url)
        if len(set(valid)) < minimum:
            return failed(criterion, "URL_MISSING", f"可解析的 HTTP(S) URL 不足：{len(set(valid))}/{minimum}")
        return passed(criterion, f"发现 {len(set(valid))} 个 URL（仅校验语法）")
