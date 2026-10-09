from __future__ import annotations

from simulate_serve.domain.evidence import Evidence, EvidenceStatus
from simulate_serve.domain.run import TaskRun
from simulate_serve.domain.task import AcceptanceCriterion, CompiledTask
from simulate_serve.domain.validation import CriterionResult, Verdict
from simulate_serve.tools.browser.models import BrowserInspectionRequest
from simulate_serve.tools.browser.provider_selector import BrowserProviderSelector
from simulate_serve.tools.browser.policy import sanitize_audit_url
from simulate_serve.tools.registry import ToolRegistry
from simulate_serve.validation.claims import Claim


class BrowserEvidenceCollector:
    def __init__(self, registry: ToolRegistry, repository: object | None = None):
        self.registry = registry
        self.repository = repository

    async def collect(self, task: CompiledTask, run: TaskRun, criterion: AcceptanceCriterion, claims: tuple[Claim, ...]) -> CriterionResult:
        candidates = self.registry.select_all(criterion.required_capabilities, task.task_type)
        provider = candidates[0] if candidates else None
        if provider is None or not hasattr(provider, "inspect_url"):
            return CriterionResult(
                criterion_id=criterion.criterion_id,
                verdict=Verdict.INCONCLUSIVE,
                reason_code="TOOL_UNAVAILABLE",
                message="没有 READY 状态且支持该准则的证据 Provider",
            )
        urls = [item.value for item in claims if item.kind == "url"]
        if not urls:
            return CriterionResult(
                criterion_id=criterion.criterion_id,
                verdict=Verdict.FAIL,
                reason_code="URL_MISSING",
                message="回复中没有可用于取证的 URL",
                retryable=True,
            )
        errors: list[str] = []
        barriers: list[str] = []
        invalid_http: list[str] = []
        missing_media: list[str] = []
        playback_uncertain: list[str] = []
        confidence_uncertain: list[str] = []
        passed_evidence: list[str] = []
        minimum = max(1, int(criterion.parameters.get("min_urls", 1)))
        maximum = max(1, int(criterion.parameters.get("max_urls", 5)))
        for url in urls[:maximum]:
            try:
                browser = BrowserProviderSelector(provider, candidates[1] if len(candidates) > 1 else None)
                actions = {"navigate", "snapshot"}
                if criterion.parameters.get("require_playback_progress"):
                    actions.add("playback_probe")
                result = await browser.inspect_url(
                    BrowserInspectionRequest(
                        url=url,
                        criterion_id=criterion.criterion_id,
                        allowed_actions=frozenset(actions),
                        timeout_seconds=provider.descriptor.call_timeout_seconds,
                        evidence_depth="status_required" if criterion.parameters.get("allow_supported") else "standard",
                    )
                )
            except Exception as exc:
                errors.append(str(exc))
                continue
            if self.repository and hasattr(self.repository, "save_evidence"):
                page = result.page
                evidence = Evidence(
                    evidence_id=result.evidence_id,
                    source=result.provider,
                    tool_name="browser.inspect_url",
                    capability="browser.snapshot",
                    status=EvidenceStatus.FAILED if result.error_code else EvidenceStatus.SUCCESS,
                    summary=result.summary,
                    confidence=result.confidence,
                    metadata={
                        "final_url": sanitize_audit_url(page.final_url if page else url),
                        "status": page.status if page else None,
                        "title": page.title if page else "",
                        "media_count": result.media_count,
                        "barrier": result.barriers.blocked,
                    },
                )
                self.repository.save_evidence(run.run_id, evidence)
                if result.evidence_id not in run.evidence_ids:
                    run.evidence_ids.append(result.evidence_id)
            if result.error_code:
                errors.append(f"{result.provider}: {result.error_code}")
                continue
            if result.page and result.page.status is not None and not 200 <= result.page.status < 400:
                invalid_http.append(f"{url} ({result.page.status})")
                continue
            if result.barriers.blocked:
                barriers.append(url)
                continue
            if criterion.parameters.get("require_media") and result.media_count < 1:
                missing_media.append(url)
                continue
            if criterion.parameters.get("require_playback_progress") and not result.media_progress_observed:
                playback_uncertain.append(url)
                continue
            supported_with_status = (
                criterion.parameters.get("allow_supported")
                and result.confidence.value == "supported"
                and result.page is not None
                and result.page.status is not None
                and 200 <= result.page.status < 400
            )
            if result.confidence.value == "confirmed" or supported_with_status:
                passed_evidence.append(result.evidence_id)
                if len(passed_evidence) >= minimum:
                    return CriterionResult(
                        criterion_id=criterion.criterion_id,
                        verdict=Verdict.PASS,
                        reason_code="EVIDENCE_CONFIRMED",
                        message=f"已确认 {len(passed_evidence)} 个候选结果",
                        evidence_ids=tuple(passed_evidence),
                    )
                continue
            confidence_uncertain.append(url)
        if invalid_http:
            return CriterionResult(
                criterion_id=criterion.criterion_id,
                verdict=Verdict.FAIL,
                reason_code="HTTP_STATUS_INVALID",
                message=f"候选页面返回失败状态：{'; '.join(invalid_http[:3])}",
                retryable=True,
            )
        if passed_evidence or len(urls) < minimum:
            return CriterionResult(
                criterion_id=criterion.criterion_id,
                verdict=Verdict.FAIL,
                reason_code="EVIDENCE_COUNT_LOW",
                message=f"已确认的候选结果不足：{len(passed_evidence)}/{minimum}",
                evidence_ids=tuple(passed_evidence),
                retryable=True,
            )
        if errors:
            return CriterionResult(
                criterion_id=criterion.criterion_id,
                verdict=Verdict.ERROR,
                reason_code="TOOL_ERROR",
                message="; ".join(errors[:3]),
            )
        if playback_uncertain:
            return CriterionResult(
                criterion_id=criterion.criterion_id,
                verdict=Verdict.INCONCLUSIVE,
                reason_code="MEDIA_PLAYBACK_UNCONFIRMED",
                message="页面可访问，但未观察到媒体播放进度",
            )
        if confidence_uncertain:
            return CriterionResult(
                criterion_id=criterion.criterion_id,
                verdict=Verdict.INCONCLUSIVE,
                reason_code="EVIDENCE_NOT_CONFIRMED",
                message="页面可访问，但证据置信度未达到该准则要求",
            )
        if missing_media:
            return CriterionResult(
                criterion_id=criterion.criterion_id,
                verdict=Verdict.FAIL,
                reason_code="MEDIA_MISSING",
                message="候选页面可访问，但未发现要求的媒体元素",
                retryable=True,
            )
        if barriers:
            return CriterionResult(
                criterion_id=criterion.criterion_id,
                verdict=Verdict.FAIL,
                reason_code="BROWSER_BARRIER",
                message="候选页面存在登录、会员、付费或其他访问门槛",
                retryable=True,
            )
        return CriterionResult(
            criterion_id=criterion.criterion_id,
            verdict=Verdict.INCONCLUSIVE,
            reason_code="EVIDENCE_EMPTY",
            message="浏览器未返回足以判定该准则的证据",
        )
