"""供应商注册表：按模态解析主供应商，未配置密钥时回退 Mock。"""

from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.security import decrypt_secret
from app.db.models import ProviderSetting
from app.providers.base import ProviderKind
from app.providers.fallback import wrap_with_fallback
from app.services import provider_log
from app.providers.http_providers import (
    ArkImageProvider,
    ArkTextProvider,
    ArkVideoProvider,
    KlingVideoProvider,
    MinimaxSpeechProvider,
    OpenAICompatSpeechProvider,
    OpenAIImageProvider,
    OpenAITextProvider,
)
from app.providers.custom import (
    DeepSeekTextProvider,
    QwenTtsProvider,
    ToapisImageProvider,
    ToapisVideoProvider,
)
from app.providers.mock import (
    MockImageProvider,
    MockSpeechProvider,
    MockTextProvider,
    MockVideoProvider,
)

TEXT_FACTORIES = {
    "mock": lambda **kw: MockTextProvider(),
    "ark": lambda **kw: ArkTextProvider(**kw),
    "openai": lambda **kw: OpenAITextProvider(**kw),
    "deepseek": lambda **kw: DeepSeekTextProvider(**kw),
}
IMAGE_FACTORIES = {
    "mock": lambda **kw: MockImageProvider(),
    "ark": lambda **kw: ArkImageProvider(**kw),
    "openai": lambda **kw: OpenAIImageProvider(**kw),
    "gemini": lambda **kw: ToapisImageProvider(**kw),
}
VIDEO_FACTORIES = {
    "mock": lambda **kw: MockVideoProvider(),
    "ark": lambda **kw: ArkVideoProvider(**kw),
    "kling": lambda **kw: KlingVideoProvider(**kw),
    "grok": lambda **kw: ToapisVideoProvider(**kw),
}
SPEECH_FACTORIES = {
    "mock": lambda **kw: MockSpeechProvider(),
    "minimax": lambda **kw: MinimaxSpeechProvider(**kw),
    "openai": lambda **kw: OpenAICompatSpeechProvider(**kw),
    "qwen": lambda **kw: QwenTtsProvider(**kw),
}

FACTORIES = {
    ProviderKind.TEXT: TEXT_FACTORIES,
    ProviderKind.IMAGE: IMAGE_FACTORIES,
    ProviderKind.VIDEO: VIDEO_FACTORIES,
    ProviderKind.SPEECH: SPEECH_FACTORIES,
}

DEFAULT_BY_KIND = {
    ProviderKind.TEXT: lambda: settings.text_provider,
    ProviderKind.IMAGE: lambda: settings.image_provider,
    ProviderKind.VIDEO: lambda: settings.video_provider,
    ProviderKind.SPEECH: lambda: settings.speech_provider,
}


def _setting_for(session: Session | None, kind: ProviderKind) -> ProviderSetting | None:
    if session is None:
        return None
    return session.scalar(select(ProviderSetting).where(ProviderSetting.kind == kind.value))


def _build(
    kind: ProviderKind,
    provider_id: str,
    setting: ProviderSetting | None,
    *,
    fallback: bool = False,
) -> Any:
    factory = FACTORIES[kind].get(provider_id)
    if factory is None:
        factory = FACTORIES[kind]["mock"]
        provider_id = "mock"
    if provider_id == "mock" or setting is None:
        return factory()
    if fallback:
        model = setting.fallback_model or ""
        base_url = setting.fallback_base_url or ""
        encrypted = setting.fallback_api_key_encrypted
    else:
        model = setting.model or ""
        base_url = setting.base_url or ""
        encrypted = setting.api_key_encrypted
    api_key = decrypt_secret(encrypted) if encrypted else ""
    return factory(model=model, api_key=api_key, base_url=base_url)


def _configured(provider: Any) -> bool:
    return bool(getattr(provider, "configured", lambda: True)())


def _primary_id(kind: ProviderKind, setting: ProviderSetting | None) -> str:
    if setting is not None and setting.is_primary and setting.provider:
        return setting.provider
    return DEFAULT_BY_KIND[kind]()


def _resolve_fallback(kind: ProviderKind, setting: ProviderSetting | None) -> Any:
    """构造备用供应商（FR-090）；未配置或未配密钥时返回 None。"""
    if setting is None or not setting.fallback_provider:
        return None
    if setting.fallback_provider == setting.provider:
        # 主备同款没有意义，避免自己重试自己
        return None
    provider = _build(kind, setting.fallback_provider, setting, fallback=True)
    if not _configured(provider):
        return None
    return provider_log.instrument(provider, kind.value)


def resolve(kind: ProviderKind, session: Session | None = None, *, allow_fallback: bool = True) -> Any:
    """解析指定模态的主供应商实例（带调用记录代理，主备切换见 FR-090）。

    - 主供应商未配置密钥时回退 Mock，保证闭环可跑通（异常降级见需求 §11）；
    - 配置了备用供应商时，返回一个代理：主供应商出现超时/限流/不可用等
      可重试失败时，自动改用备用供应商重试一次。
    """
    setting = _setting_for(session, kind)
    provider = _build(kind, _primary_id(kind, setting), setting)
    if not _configured(provider) and allow_fallback:
        provider = FACTORIES[kind]["mock"]()
    primary = provider_log.instrument(provider, kind.value)
    fallback = _resolve_fallback(kind, setting)
    if fallback is None:
        return primary
    return wrap_with_fallback(primary, fallback, kind)


def resolve_by_id(kind: ProviderKind, provider_id: str, session: Session | None = None) -> Any:
    setting = _setting_for(session, kind)
    if setting is not None and setting.provider != provider_id:
        setting = None
    return provider_log.instrument(_build(kind, provider_id, setting), kind.value)


def describe_all() -> list[dict[str, Any]]:
    """给前端 /config/providers 的清单（不回传密钥）。"""
    out: list[dict[str, Any]] = []
    for kind, factories in FACTORIES.items():
        for provider_id, factory in factories.items():
            try:
                instance = factory()
            except Exception:  # pragma: no cover - 构造失败不应影响清单
                continue
            info = instance.describe()
            info["configured"] = bool(getattr(instance, "configured", lambda: True)())
            info["kind"] = kind.value
            out.append(info)
    return out
