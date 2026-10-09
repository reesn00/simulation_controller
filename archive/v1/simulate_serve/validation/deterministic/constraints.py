import re

from simulate_serve.domain.task import AcceptanceCriterion
from simulate_serve.domain.validation import CriterionResult, Verdict

from .common import normalize, passed

_NEGATION = re.compile(r"(?:不推荐|不要|排除|不符合|无法|不可用|拒绝|避免)")
_RECOMMEND_TOKENS = ("推荐", "可以", "链接", "网址", "在线观看", "可播放", "播放", "http")
# 强推荐信号: 仅在「短窗内未命中 recommendations」时, 由长窗 fallback 单独检测.
# 把 http / 链接 / 网址 单列出来是为了让「列表项 + URL」形态 (URL 距平台名 17+ 字符,
# 原 16 字符窗看不到) 能被识别为推荐, 同时避免长窗跨段吞掉前一段 negation 的局部主导.
_SHORT_WINDOW = 16
_LONG_WINDOW = 64
_STRONG_RECOMMEND_TOKENS = ("http", "链接", "网址")


class ConstraintValidator:
    def validate(self, criterion: AcceptanceCriterion, text: str) -> CriterionResult:
        excluded = [normalize(str(item)) for item in criterion.parameters.get("excluded_platforms", [])]
        normalized = normalize(text)
        violations: list[str] = []
        ambiguous: list[str] = []
        for platform in excluded:
            if not platform:
                continue
            saw_ambiguous = False
            for match in re.finditer(re.escape(platform), normalized):
                start = match.start()
                short_ctx = normalized[max(0, start - _SHORT_WINDOW) : match.end() + _SHORT_WINDOW]
                short_recommends = any(t in short_ctx for t in _RECOMMEND_TOKENS)
                short_negated = bool(_NEGATION.search(short_ctx))
                if short_recommends and not short_negated:
                    violations.append(platform)
                    break
                if short_recommends:
                    # 短窗同时含 recommendations 与 negation (例如「不要优酷...推荐优酷链接」)
                    # 短窗内本身已是「推荐+否定」并存, 不再扩窗去重判; 跳过即可.
                    continue
                # 短窗内无 recommendations → 长窗 fallback 仅看强推荐信号 (URL/链接/网址)
                long_ctx = normalized[max(0, start - _LONG_WINDOW) : match.end() + _LONG_WINDOW]
                if any(t in long_ctx for t in _STRONG_RECOMMEND_TOKENS):
                    long_negated = bool(_NEGATION.search(long_ctx))
                    if not long_negated:
                        violations.append(platform)
                        break
                    continue
                if not short_negated:
                    saw_ambiguous = True
                continue
            if platform not in violations and saw_ambiguous:
                ambiguous.append(platform)
        violations = list(dict.fromkeys(violations))
        ambiguous = [item for item in dict.fromkeys(ambiguous) if item not in violations]
        if violations:
            return CriterionResult(
                criterion_id=criterion.criterion_id,
                verdict=Verdict.FAIL,
                reason_code="SOURCE_EXCLUDED",
                message=f"结果使用了被排除的平台：{' / '.join(violations)}",
                retryable=True,
            )
        if ambiguous:
            return CriterionResult(
                criterion_id=criterion.criterion_id,
                verdict=Verdict.INCONCLUSIVE,
                reason_code="SOURCE_CONTEXT_AMBIGUOUS",
                message=f"无法确定是否在推荐被排除平台：{' / '.join(ambiguous)}",
            )
        return passed(criterion, "未使用被排除平台")
