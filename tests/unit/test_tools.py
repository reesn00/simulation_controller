from __future__ import annotations

from types import SimpleNamespace

import pytest

from simulate_serve.config import ToolProviderConfig, ToolsConfig
from simulate_serve.domain.evidence import EvidenceConfidence
from simulate_serve.domain.validation import Verdict
from simulate_serve.tools.browser.models import BrowserInspectionRequest, BrowserInspectionResult
from simulate_serve.tools.browser.models import BarrierObservation, PageObservation, detect_barriers
from simulate_serve.tools.browser.policy import UrlPolicyError, sanitize_audit_url, validate_public_url
from simulate_serve.tools.browser.playwright_mcp import PlaywrightMCPProvider
from simulate_serve.tools.browser.provider_selector import BrowserProviderSelector
from simulate_serve.tools.descriptor import ToolDescriptor, ToolStatus
from simulate_serve.tools.evidence_adapter import BrowserEvidenceCollector
from simulate_serve.tools.registry import RequiredToolUnavailableError, ToolRegistry
from simulate_serve.validation.claims import Claim
from simulate_serve.validation.deterministic.constraints import ConstraintValidator


class FakeProvider:
    def __init__(self, descriptor: ToolDescriptor):
        self.descriptor = descriptor
        self.tools = []
        self.closed = False

    async def start(self):
        self.tools = [SimpleNamespace(name="fake.inspect")]
        return self.tools

    async def close(self):
        self.closed = True


@pytest.mark.asyncio
async def test_registry_reports_disabled_and_ready() -> None:
    registry = ToolRegistry({"fake": FakeProvider})
    report = await registry.start(
        ToolsConfig(
            providers=[
                ToolProviderConfig(name="off", type="fake", enabled=False),
                ToolProviderConfig(name="on", type="fake", enabled=True, capabilities=["x"]),
            ]
        )
    )
    assert [item.status for item in report.tools] == [ToolStatus.DISABLED, ToolStatus.READY]
    assert registry.select(frozenset({"x"})) is not None
    assert len(registry.select_all(frozenset({"x"}))) == 1
    await registry.close()


@pytest.mark.asyncio
async def test_required_missing_provider_fails_after_report() -> None:
    registry = ToolRegistry()
    with pytest.raises(RequiredToolUnavailableError) as caught:
        await registry.start(ToolsConfig(providers=[ToolProviderConfig(name="missing", type="missing", enabled=True, required=True)]))
    assert caught.value.report.tools[0].status is ToolStatus.DEPENDENCY_MISSING


@pytest.mark.asyncio
async def test_optional_missing_provider_reports_and_continues() -> None:
    registry = ToolRegistry()
    report = await registry.start(ToolsConfig(providers=[ToolProviderConfig(name="missing", type="missing", enabled=True)]))
    assert report.tools[0].status is ToolStatus.DEPENDENCY_MISSING
    assert report.required_failures == ()


@pytest.mark.asyncio
async def test_url_policy_rejects_local_targets() -> None:
    with pytest.raises(UrlPolicyError):
        await validate_public_url("http://localhost/test")
    with pytest.raises(UrlPolicyError):
        await validate_public_url("file:///tmp/a")


def test_audit_url_drops_query_and_fragment_by_default() -> None:
    assert sanitize_audit_url("https://example.com/watch?token=secret&id=1#part") == "https://example.com/watch"
    assert sanitize_audit_url("https://example.com/watch?token=secret&id=1", {"id"}) == "https://example.com/watch?id=1"


class ResultProvider:
    def __init__(self, result: BrowserInspectionResult):
        self.result = result
        self.calls = 0

    async def inspect_url(self, request: BrowserInspectionRequest) -> BrowserInspectionResult:
        self.calls += 1
        return self.result


@pytest.mark.asyncio
async def test_browser_fallback_only_for_allowed_failure_codes() -> None:
    primary = ResultProvider(BrowserInspectionResult(provider="p", evidence_id="1", error_code="bot_blocked"))
    fallback = ResultProvider(BrowserInspectionResult(provider="f", evidence_id="2", summary="ok"))
    selector = BrowserProviderSelector(primary, fallback)
    result = await selector.inspect_url(BrowserInspectionRequest(url="https://example.com", criterion_id="c"))
    assert result.provider == "f"
    blocked = ResultProvider(BrowserInspectionResult(provider="p", evidence_id="3", error_code="paywall"))
    fallback.calls = 0
    result = await BrowserProviderSelector(blocked, fallback).inspect_url(BrowserInspectionRequest(url="https://example.com", criterion_id="c"))
    assert result.provider == "p"
    assert fallback.calls == 0


@pytest.mark.asyncio
async def test_browser_fallback_collects_status_when_primary_cannot() -> None:
    primary = ResultProvider(
        BrowserInspectionResult(
            provider="p",
            evidence_id="1",
            page=PageObservation(final_url="https://example.com"),
            confidence=EvidenceConfidence.SUPPORTED,
        )
    )
    fallback = ResultProvider(
        BrowserInspectionResult(
            provider="f",
            evidence_id="2",
            page=PageObservation(final_url="https://example.com", status=200),
            confidence=EvidenceConfidence.SUPPORTED,
        )
    )

    result = await BrowserProviderSelector(primary, fallback).inspect_url(
        BrowserInspectionRequest(url="https://example.com", criterion_id="c", evidence_depth="status_required")
    )

    assert result.provider == "f"
    assert fallback.calls == 1


class EvidenceRegistry:
    def __init__(self, provider: ResultProvider):
        self.provider = provider

    def select_all(self, capabilities: frozenset[str], task_type: str = ""):
        return (self.provider,)


def evidence_provider(result: BrowserInspectionResult) -> ResultProvider:
    provider = ResultProvider(result)
    provider.descriptor = ToolDescriptor(
        name="browser",
        provider_type="fake",
        enabled=True,
        capabilities=frozenset({"browser.snapshot"}),
    )
    return provider


@pytest.mark.asyncio
async def test_supported_browser_evidence_requires_explicit_policy() -> None:
    provider = evidence_provider(
        BrowserInspectionResult(
            provider="fake",
            evidence_id="e1",
            page=PageObservation(final_url="https://example.com", status=200),
            confidence=EvidenceConfidence.SUPPORTED,
            summary="page reachable",
        )
    )
    collector = BrowserEvidenceCollector(EvidenceRegistry(provider))
    claim = Claim(kind="url", value="https://example.com", start=0, end=19)
    base = {
        "criterion_id": "links",
        "required_capabilities": frozenset({"browser.snapshot"}),
        "parameters": {},
    }
    task = SimpleNamespace(task_type="web")
    run = SimpleNamespace(run_id="run", evidence_ids=[])

    strict = await collector.collect(task, run, SimpleNamespace(**base), (claim,))
    allowed = await collector.collect(
        task,
        run,
        SimpleNamespace(**{**base, "parameters": {"allow_supported": True}}),
        (claim,),
    )

    assert strict.verdict is Verdict.INCONCLUSIVE
    assert strict.reason_code == "EVIDENCE_NOT_CONFIRMED"
    assert allowed.verdict is Verdict.PASS


@pytest.mark.asyncio
async def test_media_playback_requires_observed_progress() -> None:
    provider = evidence_provider(
        BrowserInspectionResult(
            provider="fake",
            evidence_id="e2",
            media_count=1,
            confidence=EvidenceConfidence.SUPPORTED,
            summary="video element found",
        )
    )
    collector = BrowserEvidenceCollector(EvidenceRegistry(provider))
    criterion = SimpleNamespace(
        criterion_id="video.playable",
        required_capabilities=frozenset({"browser.snapshot"}),
        parameters={"require_media": True, "require_playback_progress": True},
    )

    result = await collector.collect(
        SimpleNamespace(task_type="video"),
        SimpleNamespace(run_id="run", evidence_ids=[]),
        criterion,
        (Claim(kind="url", value="https://example.com/video", start=0, end=25),),
    )

    assert result.verdict is Verdict.INCONCLUSIVE
    assert result.reason_code == "MEDIA_PLAYBACK_UNCONFIRMED"


def test_playback_probe_parser_does_not_confuse_media_true_with_progress_false() -> None:
    assert not PlaywrightMCPProvider._playback_progressed("{media: true, progressed: false}")
    assert PlaywrightMCPProvider._playback_progressed("{media: true, progressed: true}")


@pytest.mark.asyncio
async def test_supported_browser_evidence_rejects_http_error_status() -> None:
    provider = evidence_provider(
        BrowserInspectionResult(
            provider="fake",
            evidence_id="e404",
            page=PageObservation(final_url="https://example.com/missing", status=404),
            confidence=EvidenceConfidence.SUPPORTED,
            summary="404 page rendered",
        )
    )
    collector = BrowserEvidenceCollector(EvidenceRegistry(provider))
    criterion = SimpleNamespace(
        criterion_id="links",
        required_capabilities=frozenset({"browser.snapshot"}),
        parameters={"allow_supported": True},
    )

    result = await collector.collect(
        SimpleNamespace(task_type="web"),
        SimpleNamespace(run_id="run", evidence_ids=[]),
        criterion,
        (Claim(kind="url", value="https://example.com/missing", start=0, end=27),),
    )

    assert result.verdict is Verdict.FAIL
    assert result.reason_code == "HTTP_STATUS_INVALID"


@pytest.mark.asyncio
async def test_browser_evidence_requires_configured_number_of_urls() -> None:
    provider = evidence_provider(
        BrowserInspectionResult(
            provider="fake",
            evidence_id="e1",
            page=PageObservation(final_url="https://example.com/one", status=200),
            confidence=EvidenceConfidence.SUPPORTED,
            summary="reachable",
        )
    )
    collector = BrowserEvidenceCollector(EvidenceRegistry(provider))
    criterion = SimpleNamespace(
        criterion_id="links",
        required_capabilities=frozenset({"browser.snapshot"}),
        parameters={"allow_supported": True, "min_urls": 3},
    )

    result = await collector.collect(
        SimpleNamespace(task_type="web"),
        SimpleNamespace(run_id="run", evidence_ids=[]),
        criterion,
        (Claim(kind="url", value="https://example.com/one", start=0, end=23),),
    )

    assert result.verdict is Verdict.FAIL
    assert result.reason_code == "EVIDENCE_COUNT_LOW"


def test_excluded_platform_validator_checks_later_recommendations(source_ref) -> None:
    criterion = SimpleNamespace(
        criterion_id="excluded",
        parameters={"excluded_platforms": ["优酷"]},
    )
    text = "不要优酷，这个平台不符合要求。下面介绍其他选择，最后推荐优酷链接：https://youku.example/video"

    result = ConstraintValidator().validate(criterion, text)

    assert result.verdict is Verdict.FAIL
    assert result.reason_code == "SOURCE_EXCLUDED"


# ── detect_barriers 共享函数测试 ──

def test_detect_barriers_defaults_all_false_for_empty_text() -> None:
    barriers = detect_barriers("")
    assert not barriers.login
    assert not barriers.membership
    assert not barriers.paywall
    assert not barriers.captcha
    assert not barriers.region_restricted
    assert not barriers.blocked


def test_detect_barriers_login_keywords() -> None:
    assert detect_barriers("请登录后查看").login
    assert detect_barriers("sign in to continue").login
    assert detect_barriers("log in required").login
    assert detect_barriers("注册账号").login


def test_detect_barriers_membership_keywords() -> None:
    assert detect_barriers("会员专享").membership
    assert detect_barriers("vip exclusive").membership


def test_detect_barriers_paywall_keywords() -> None:
    assert detect_barriers("付费观看").paywall
    assert detect_barriers("purchase now").paywall


def test_detect_barriers_captcha_keywords() -> None:
    assert detect_barriers("请输入验证码").captcha
    assert detect_barriers("verify captcha").captcha


def test_detect_barriers_region_restricted_keywords() -> None:
    assert detect_barriers("该内容有地区限制").region_restricted
    assert detect_barriers("not available in your region").region_restricted


def test_detect_barriers_blocked_when_any_detected() -> None:
    assert detect_barriers("请登录").blocked
    assert not detect_barriers("hello world").blocked


# ── BrowserUseProvider 单元测试 ──

def test_browser_use_provider_factory_registered() -> None:
    from simulate_serve.tools.factories import create_default_registry

    registry = create_default_registry()
    assert "browser_use" in registry._factories
    from simulate_serve.tools.browser.browser_use import BrowserUseProvider

    factory = registry._factories["browser_use"]
    descriptor = ToolDescriptor(name="bu", provider_type="browser_use")
    provider = factory(descriptor)
    assert isinstance(provider, BrowserUseProvider)
    assert provider.descriptor.name == "bu"


def test_browser_use_provider_implements_protocol() -> None:
    from simulate_serve.tools.browser.browser_use import BrowserUseProvider

    provider = BrowserUseProvider(ToolDescriptor(name="bu", provider_type="browser_use"))
    assert hasattr(provider, "descriptor")
    assert hasattr(provider, "tools")
    assert callable(provider.start)
    assert callable(provider.inspect_url)
    assert callable(provider.close)


def test_build_task_contains_only_read_operations() -> None:
    from simulate_serve.tools.browser.browser_use import BrowserUseProvider

    task = BrowserUseProvider._build_task(
        BrowserInspectionRequest(url="https://example.com/page", criterion_id="test")
    )
    assert "https://example.com/page" in task
    assert "final_url" in task
    assert "title" in task
    assert "text_summary" in task
    assert "Do NOT click" in task
    # 验证没有正向的交互指令（排除否定句中的出现）
    forbidden_commands = ["\nclick ", "\ntype ", "\nsubmit ", "\nfill ", "\ndownload "]
    lower_task = task.casefold()
    for word in forbidden_commands:
        assert word not in lower_task, f"task contains forbidden command: {word}"


def test_failure_result_error_code_mapping() -> None:
    from simulate_serve.tools.browser.browser_use import BrowserUseProvider

    result_bot = BrowserUseProvider._failure_result("access denied for bot detection")
    assert result_bot.error_code == "bot_blocked"
    assert result_bot.retryable is False

    result_crash = BrowserUseProvider._failure_result("renderer crash detected")
    assert result_crash.error_code == "renderer_crash"
    assert result_crash.retryable is True

    result_incompat = BrowserUseProvider._failure_result("unsupported browser version")
    assert result_incompat.error_code == "browser_incompatible"
    assert result_incompat.retryable is False

    result_unknown = BrowserUseProvider._failure_result("unknown error occurred")
    assert result_unknown.error_code == "tool_error"
    assert result_unknown.retryable is True


@pytest.mark.asyncio
async def test_browser_use_provider_start_fails_without_dependency() -> None:
    from simulate_serve.tools.factories import create_default_registry

    registry = create_default_registry()
    report = await registry.start(
        ToolsConfig(
            providers=[
                ToolProviderConfig(
                    name="bu",
                    type="browser_use",
                    enabled=True,
                    required=False,
                    capabilities=["browser.navigate"],
                )
            ]
        )
    )
    assert report.tools[0].status is ToolStatus.DEPENDENCY_MISSING
    assert "browser-use" in report.tools[0].reason.lower()


@pytest.mark.asyncio
async def test_browser_use_provider_missing_model_config_fails() -> None:
    from simulate_serve.config import ModelConfig
    from simulate_serve.tools.factories import create_default_registry

    registry = create_default_registry()
    report = await registry.start(
        ToolsConfig(
            providers=[
                ToolProviderConfig(
                    name="bu",
                    type="browser_use",
                    enabled=True,
                    required=False,
                    capabilities=["browser.navigate"],
                )
            ]
        )
    )
    assert report.tools[0].status is ToolStatus.DEPENDENCY_MISSING
    assert "browser-use is not installed" in report.tools[0].reason.lower()

    registry2 = create_default_registry(ModelConfig(model_name="gpt-4o-mini", api_key="", base_url=""))
    report2 = await registry2.start(
        ToolsConfig(
            providers=[
                ToolProviderConfig(
                    name="bu",
                    type="browser_use",
                    enabled=True,
                    required=False,
                    capabilities=["browser.navigate"],
                )
            ]
        )
    )
    assert report2.tools[0].status is ToolStatus.DEPENDENCY_MISSING


def test_resolve_llm_config_explicit_overrides_global() -> None:
    from simulate_serve.config import ModelConfig
    from simulate_serve.tools.browser.browser_use import BrowserUseProvider

    provider = BrowserUseProvider(
        ToolDescriptor(
            name="bu",
            provider_type="browser_use",
            model=ModelConfig(model_name="gpt-4o-mini", api_key="global-key", base_url="https://global"),
            config={"llm_model": "gpt-4o", "llm_api_key": "explicit-key"},
        )
    )
    model, key, url = provider._resolve_llm_config()
    assert model == "gpt-4o"
    assert key == "explicit-key"
    assert url == "https://global"


def test_resolve_llm_config_global_inheritance() -> None:
    from simulate_serve.config import ModelConfig
    from simulate_serve.tools.browser.browser_use import BrowserUseProvider

    provider = BrowserUseProvider(
        ToolDescriptor(
            name="bu",
            provider_type="browser_use",
            model=ModelConfig(model_name="gpt-4o-mini", api_key="global-key", base_url="https://global"),
            config={},
        )
    )
    model, key, url = provider._resolve_llm_config()
    assert model == "gpt-4o-mini"
    assert key == "global-key"
    assert url == "https://global"


def test_resolve_llm_config_anthropic_not_supported() -> None:
    from simulate_serve.config import ModelConfig
    from simulate_serve.tools.browser.browser_use import BrowserUseProvider

    provider = BrowserUseProvider(
        ToolDescriptor(
            name="bu",
            provider_type="browser_use",
            model=ModelConfig(model_type="ANTHROPIC", model_name="claude", api_key="k"),
            config={},
        )
    )
    with pytest.raises(ConnectionError, match="ANTHROPIC"):
        provider._resolve_llm_config()


def test_resolve_llm_config_all_missing_raises() -> None:
    from simulate_serve.tools.browser.browser_use import BrowserUseProvider

    provider = BrowserUseProvider(ToolDescriptor(name="bu", provider_type="browser_use", config={}))
    with pytest.raises(ConnectionError, match="llm_model"):
        provider._resolve_llm_config()


def test_registry_injects_model_into_descriptor() -> None:
    from simulate_serve.config import ModelConfig
    from simulate_serve.tools.factories import create_default_registry

    registry = create_default_registry(ModelConfig(model_name="gpt-4o-mini", api_key="k"))
    descriptor = registry._descriptor(ToolProviderConfig(name="bu", type="browser_use", enabled=True))
    assert descriptor.model is not None
    assert descriptor.model.model_name == "gpt-4o-mini"
    assert descriptor.model.api_key == "k"
