from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable
from typing import Any, Protocol

from simulate_serve.config import ToolProviderConfig, ToolsConfig

from .descriptor import ToolDescriptor, ToolHealth, ToolReadinessReport, ToolStatus

logger = logging.getLogger(__name__)


class ToolProvider(Protocol):
    descriptor: ToolDescriptor

    async def start(self) -> list[Any]: ...

    async def close(self) -> None: ...


ProviderFactory = Callable[[ToolDescriptor], ToolProvider]


class RequiredToolUnavailableError(RuntimeError):
    def __init__(self, report: ToolReadinessReport):
        self.report = report
        super().__init__(report.render())


class ProviderSchemaError(RuntimeError):
    pass


class ProviderProbeError(RuntimeError):
    pass


class ToolRegistry:
    """The single lifecycle owner for configured tool providers."""

    def __init__(self, factories: dict[str, ProviderFactory] | None = None):
        self._factories: dict[str, ProviderFactory] = dict(factories or {})
        self._providers: dict[str, ToolProvider] = {}
        self._health: dict[str, ToolHealth] = {}

    def register_factory(self, provider_type: str, factory: ProviderFactory) -> None:
        if provider_type in self._factories:
            raise ValueError(f"Provider factory already registered: {provider_type}")
        self._factories[provider_type] = factory

    async def start(self, config: ToolsConfig) -> ToolReadinessReport:
        descriptors = tuple(self._descriptor(item) for item in config.providers)
        names = [item.name for item in descriptors]
        if len(names) != len(set(names)):
            raise ValueError("Tool provider names must be unique")
        semaphore = asyncio.Semaphore(4)

        async def initialize(descriptor: ToolDescriptor) -> ToolHealth:
            if not descriptor.enabled:
                return ToolHealth(name=descriptor.name, provider_type=descriptor.provider_type, required=descriptor.required, status=ToolStatus.DISABLED)
            started = time.monotonic()
            factory = self._factories.get(descriptor.provider_type)
            if factory is None:
                return self._health_result(descriptor, ToolStatus.DEPENDENCY_MISSING, started, "provider type is not registered")
            try:
                provider = factory(descriptor)
            except (ImportError, ModuleNotFoundError) as exc:
                return self._health_result(descriptor, ToolStatus.DEPENDENCY_MISSING, started, str(exc))
            except Exception as exc:
                return self._health_result(descriptor, ToolStatus.INIT_FAILED, started, str(exc))
            try:
                async with semaphore:
                    tools = await asyncio.wait_for(provider.start(), timeout=descriptor.startup_timeout_seconds)
                names = [getattr(tool, "name", None) or getattr(tool, "get_function_name", lambda: "")() for tool in tools]
                if any(not name for name in names) or len(names) != len(set(names)):
                    await provider.close()
                    return self._health_result(descriptor, ToolStatus.SCHEMA_INVALID, started, "empty or duplicate tool names")
                self._providers[descriptor.name] = provider
                return self._health_result(descriptor, ToolStatus.READY, started, tool_count=len(tools))
            except (ImportError, ModuleNotFoundError, FileNotFoundError) as exc:
                return self._health_result(descriptor, ToolStatus.DEPENDENCY_MISSING, started, str(exc))
            except ProviderSchemaError as exc:
                await self._safe_close(provider)
                return self._health_result(descriptor, ToolStatus.SCHEMA_INVALID, started, str(exc))
            except ProviderProbeError as exc:
                await self._safe_close(provider)
                return self._health_result(descriptor, ToolStatus.PROBE_FAILED, started, str(exc))
            except asyncio.TimeoutError:
                await self._safe_close(provider)
                return self._health_result(descriptor, ToolStatus.CONNECT_FAILED, started, "startup timeout")
            except Exception as exc:
                await self._safe_close(provider)
                return self._health_result(descriptor, ToolStatus.CONNECT_FAILED, started, str(exc))

        health = await asyncio.gather(*(initialize(item) for item in descriptors))
        report = ToolReadinessReport(tools=tuple(health))
        self._health = {item.name: item for item in health}
        logger.info("%s", report.render())
        if report.required_failures:
            await self.close()
            raise RequiredToolUnavailableError(report)
        return report

    def select(self, capabilities: frozenset[str], task_type: str = "") -> ToolProvider | None:
        candidates = self.select_all(capabilities, task_type)
        return candidates[0] if candidates else None

    def select_all(self, capabilities: frozenset[str], task_type: str = "") -> tuple[ToolProvider, ...]:
        candidates = []
        for provider in self._providers.values():
            descriptor = provider.descriptor
            if not capabilities.issubset(descriptor.capabilities):
                continue
            if descriptor.allowed_task_types and task_type not in descriptor.allowed_task_types:
                continue
            candidates.append(provider)
        return tuple(sorted(candidates, key=lambda item: item.descriptor.priority, reverse=True))

    def camel_tools(self, capabilities: frozenset[str], task_type: str = "") -> list[Any]:
        provider = self.select(capabilities, task_type)
        return list(getattr(provider, "tools", [])) if provider else []

    @property
    def report(self) -> ToolReadinessReport:
        return ToolReadinessReport(tools=tuple(self._health.values()))

    async def close(self) -> None:
        for name, provider in reversed(tuple(self._providers.items())):
            try:
                await asyncio.wait_for(provider.close(), timeout=10)
            except Exception as exc:
                current = self._health.get(name)
                if current:
                    self._health[name] = current.model_copy(update={"status": ToolStatus.SHUTDOWN_FAILED, "reason": str(exc)})
                logger.warning("Tool provider %s failed to close: %s", name, exc)
        self._providers.clear()

    @staticmethod
    def _descriptor(config: ToolProviderConfig) -> ToolDescriptor:
        return ToolDescriptor(
            name=config.name,
            provider_type=config.type,
            enabled=config.enabled,
            required=config.required,
            priority=config.priority,
            capabilities=frozenset(config.capabilities),
            allowed_task_types=frozenset(config.allowed_task_types),
            startup_timeout_seconds=config.startup_timeout_seconds,
            call_timeout_seconds=config.call_timeout_seconds,
            max_concurrency=config.max_concurrency,
            config=config.config,
        )

    @staticmethod
    def _health_result(descriptor: ToolDescriptor, status: ToolStatus, started: float, reason: str = "", tool_count: int = 0) -> ToolHealth:
        return ToolHealth(
            name=descriptor.name,
            provider_type=descriptor.provider_type,
            required=descriptor.required,
            status=status,
            reason=reason,
            tool_count=tool_count,
            duration_ms=int((time.monotonic() - started) * 1000),
        )

    @staticmethod
    async def _safe_close(provider: ToolProvider) -> None:
        try:
            await provider.close()
        except Exception:
            logger.debug("Failed to close partially initialized provider", exc_info=True)
