import re

from pydantic import BaseModel, ConfigDict

from simulate_serve.domain.evidence import EvidenceConfidence


class BrowserInspectionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    url: str
    criterion_id: str
    allowed_actions: frozenset[str] = frozenset({"navigate", "snapshot"})
    timeout_seconds: float = 30
    evidence_depth: str = "standard"


class PageObservation(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    final_url: str
    status: int | None = None
    title: str = ""
    text_summary: str = ""


class BarrierObservation(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    login: bool = False
    membership: bool = False
    paywall: bool = False
    captcha: bool = False
    region_restricted: bool = False

    @property
    def blocked(self) -> bool:
        return any((self.login, self.membership, self.paywall, self.captcha, self.region_restricted))


def detect_barriers(lower_text: str) -> BarrierObservation:
    return BarrierObservation(
        login=bool(re.search(r"登录|注册|sign\s*in|log\s*in", lower_text)),
        membership=bool(re.search(r"会员|vip|subscription", lower_text)),
        paywall=bool(re.search(r"付费|购买|paywall|purchase", lower_text)),
        captcha=bool(re.search(r"验证码|captcha", lower_text)),
        region_restricted=bool(re.search(r"地区限制|not available in your region", lower_text)),
    )


class BrowserInspectionResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    provider: str
    evidence_id: str
    page: PageObservation | None = None
    links: tuple[str, ...] = ()
    media_count: int = 0
    media_progress_observed: bool = False
    barriers: BarrierObservation = BarrierObservation()
    confidence: EvidenceConfidence = EvidenceConfidence.NONE
    summary: str = ""
    error_code: str | None = None
    retryable: bool = False
