"""供应商层单测：主备切换（FR-090）与能力探测（FR-091）。"""

from __future__ import annotations

import asyncio
from typing import Any

from fastapi.testclient import TestClient

from app.providers.base import (
    ProviderCapabilities,
    ProviderKind,
    ProviderResult,
    TextRequest,
)


class _Stub:
    """最小供应商替身：按预置结果回应，并记录被调用次数。"""

    def __init__(self, pid: str, result: Any) -> None:
        self.id = pid
        self.model = "stub-model"
        self.display_name = pid
        self._result = result
        self.calls = 0

    async def generate_json(self, request: TextRequest) -> Any:
        self.calls += 1
        return self._result

    def capabilities(self) -> ProviderCapabilities:
        return ProviderCapabilities()

    def describe(self) -> dict[str, Any]:
        return {"id": self.id, "display_name": self.display_name, "kind": "text"}

    def configured(self) -> bool:
        return True


def _ok(data: dict[str, Any]) -> ProviderResult:
    return ProviderResult(ok=True, kind=ProviderKind.TEXT, data=data, request_id="req-ok")


def _fail(code: str, *, retryable: bool) -> ProviderResult:
    return ProviderResult.failure(ProviderKind.TEXT, code, "stub 失败", retryable=retryable)


def test_fallback_switches_on_retryable_failure() -> None:
    """FR-090：主供应商可重试失败 → 自动改用备用供应商，并把来源写进 raw_summary。"""
    from app.providers.fallback import FallbackProvider

    primary = _Stub("primary", _fail("PROVIDER_TIMEOUT", retryable=True))
    fallback = _Stub("fallback", _ok({"theme": "备用结果"}))
    provider = FallbackProvider(primary, fallback, ProviderKind.TEXT)

    result = asyncio.run(provider.generate_json(TextRequest(prompt="x")))

    assert result.ok is True
    assert result.data == {"theme": "备用结果"}
    assert primary.calls == 1 and fallback.calls == 1
    assert result.raw_summary.get("fallback_from") == "primary"
    assert provider.last_provider_id == "fallback"
    # 对外元信息仍沿用主供应商
    assert provider.id == "primary"


def test_fallback_not_used_when_primary_succeeds() -> None:
    from app.providers.fallback import FallbackProvider

    primary = _Stub("primary", _ok({"theme": "主结果"}))
    fallback = _Stub("fallback", _ok({"theme": "不该被调用"}))
    provider = FallbackProvider(primary, fallback, ProviderKind.TEXT)

    result = asyncio.run(provider.generate_json(TextRequest(prompt="x")))

    assert result.data == {"theme": "主结果"}
    assert fallback.calls == 0
    assert provider.last_provider_id == "primary"


def test_fallback_not_used_for_non_switchable_errors() -> None:
    """参数错误/鉴权失败/内容拦截换一家也一样，不该多花一次调用。"""
    from app.providers.fallback import FallbackProvider, should_switch

    assert should_switch(_fail("PROVIDER_BAD_REQUEST", retryable=False)) is False
    assert should_switch(_fail("PROVIDER_CONTENT_BLOCKED", retryable=False)) is False
    assert should_switch(_fail("PROVIDER_AUTH", retryable=False)) is False
    assert should_switch(_fail("PROVIDER_TIMEOUT", retryable=True)) is True
    assert should_switch(_fail("PROVIDER_UNAVAILABLE", retryable=True)) is True
    assert should_switch(_ok({})) is False

    primary = _Stub("primary", _fail("PROVIDER_BAD_REQUEST", retryable=False))
    fallback = _Stub("fallback", _ok({"theme": "不该被调用"}))
    result = asyncio.run(
        FallbackProvider(primary, fallback, ProviderKind.TEXT).generate_json(TextRequest(prompt="x"))
    )
    assert result.ok is False
    assert fallback.calls == 0


def test_fallback_handles_primary_exception() -> None:
    """主供应商直接抛异常也算一次失败尝试，切备用。"""
    from app.providers.fallback import FallbackProvider

    class _Raising(_Stub):
        async def generate_json(self, request: TextRequest) -> Any:
            self.calls += 1
            raise RuntimeError("连接被重置")

    primary = _Raising("primary", _ok({}))
    fallback = _Stub("fallback", _ok({"theme": "备用接管"}))
    result = asyncio.run(
        FallbackProvider(primary, fallback, ProviderKind.TEXT).generate_json(TextRequest(prompt="x"))
    )
    assert result.ok is True
    assert result.data == {"theme": "备用接管"}


def test_resolve_wraps_only_when_fallback_configured(client: TestClient) -> None:
    """registry.resolve 只在配置了备用供应商时才返回主备代理。"""
    from app.db.models import ProviderSetting
    from app.db.session import session_scope
    from app.providers import registry
    from app.providers.fallback import FallbackProvider
    from app.services.provider_log import RecordingProvider

    with session_scope() as session:
        assert isinstance(registry.resolve(ProviderKind.TEXT, session), RecordingProvider)

        session.add(
            ProviderSetting(
                kind="text",
                provider="ark",
                is_primary=True,
                fallback_provider="mock",
            )
        )
        session.flush()
        provider = registry.resolve(ProviderKind.TEXT, session)
        assert isinstance(provider, FallbackProvider)
        assert provider.fallback_id == "mock"


def test_resolve_skips_fallback_without_key(client: TestClient) -> None:
    from app.db.models import ProviderSetting
    from app.db.session import session_scope
    from app.providers import registry
    from app.providers.fallback import FallbackProvider
    from app.services.provider_log import RecordingProvider

    with session_scope() as session:
        session.add(
            ProviderSetting(
                kind="text",
                provider="mock",
                is_primary=True,
                fallback_provider="openai",  # 没有密钥
            )
        )
        session.flush()
        provider = registry.resolve(ProviderKind.TEXT, session)
        assert not isinstance(provider, FallbackProvider)
        assert isinstance(provider, RecordingProvider)
