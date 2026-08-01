"""المصنع ونقطة الدخول الوحيدة للاكتشاف.

`init_providers()` يجب أن يُنادى في الإقلاع؛ `get_provider()` قبله يرفع
استثناءً واضحًا بدل أن يعمل بلا مفاتيح.
"""
from __future__ import annotations

import logging
from typing import Any

from services.ai_providers.anthropic_provider import AnthropicProvider
from services.ai_providers.base import (
    BaseProvider,
    ChatMessage,
    Completion,
    ImagePart,
    KeyedProvider,
    ToolCall,
    ToolSpec,
    Usage,
)
from services.ai_providers.custom_provider import (
    CustomConfig,
    CustomProvider,
    build_custom_provider,
)
from services.ai_providers.errors import (
    AuthError,
    BadRequestError,
    EmptyResponseError,
    ModelListingUnsupported,
    ProviderError,
    ProviderTimeoutError,
    ProviderUnavailableError,
    QuotaError,
    RateLimitError,
    ToolsUnsupportedError,
    VisionUnsupportedError,
)
from services.ai_providers.openai_provider import OpenAIProvider
from services.ai_providers.openrouter_provider import OpenRouterProvider

log = logging.getLogger(__name__)

__all__ = [
    "BaseProvider", "KeyedProvider", "ChatMessage", "Completion", "ImagePart",
    "ToolCall", "ToolSpec", "Usage", "CustomConfig", "CustomProvider",
    "ProviderError", "AuthError", "QuotaError", "RateLimitError",
    "ProviderTimeoutError", "ProviderUnavailableError", "BadRequestError",
    "EmptyResponseError", "VisionUnsupportedError", "ToolsUnsupportedError",
    "ModelListingUnsupported", "BUILTIN_PROVIDERS", "PROVIDER_LABELS",
    "init_providers", "get_provider", "resolve_provider_instance",
    "list_available_models", "close_providers", "keyring",
]

BUILTIN_PROVIDERS = ("openrouter", "anthropic", "openai")

PROVIDER_LABELS = {
    "openrouter": "OpenRouter",
    "anthropic": "Anthropic",
    "openai": "OpenAI",
}

_FACTORIES: dict[str, type[KeyedProvider]] = {
    "openrouter": OpenRouterProvider,
    "anthropic": AnthropicProvider,
    "openai": OpenAIProvider,
}

_keyring: Any = None
_registry: Any = None
_instances: dict[str, BaseProvider] = {}


def init_providers(key_ring: Any, custom_registry: Any = None) -> None:
    global _keyring, _registry
    _keyring = key_ring
    _registry = custom_registry
    _instances.clear()
    log.info("طبقة المزوّدات مهيّأة")


def set_custom_registry(custom_registry: Any) -> None:
    global _registry
    _registry = custom_registry


def keyring() -> Any:
    if _keyring is None:
        raise RuntimeError("حلقة المفاتيح غير مهيّأة — نادِ init_providers() أولًا")
    return _keyring


def get_provider(name: str) -> BaseProvider:
    """factory function: يبني المزوّد مرة ويُعيد استخدامه."""
    if _keyring is None:
        raise RuntimeError(
            "طبقة المزوّدات غير مهيّأة — نادِ init_providers(keyring) أولًا"
        )
    cached = _instances.get(name)
    if cached is not None:
        return cached
    factory = _FACTORIES.get(name)
    if factory is None:
        raise ValueError(f"مزوّد غير معروف: {name!r}")
    instance = factory(_keyring)
    _instances[name] = instance
    return instance


def resolve_provider_instance(provider_name: str) -> BaseProvider | None:
    """يوحّد الجاهز والمخصّص: cx-* يمرّ بالسجلّ، وما عداه بالمصنع."""
    if not provider_name:
        return None
    if provider_name.startswith("cx-"):
        if _registry is None:
            return None
        return _registry.provider(provider_name)
    try:
        return get_provider(provider_name)
    except ValueError:
        return None


async def list_available_models(provider_name: str) -> list[Any]:
    """نقطة الدخول للاكتشاف — تعمل للجاهز والمخصّص سواءً."""
    instance = resolve_provider_instance(provider_name)
    if instance is None:
        raise ValueError(f"مزوّد غير معروف: {provider_name!r}")
    return await instance.list_models()


def provider_label(provider_name: str) -> str:
    if provider_name in PROVIDER_LABELS:
        return PROVIDER_LABELS[provider_name]
    if _registry is not None:
        doc = _registry.doc(provider_name)
        if doc is not None:
            return doc.name
    return provider_name


async def close_providers() -> None:
    for instance in list(_instances.values()):
        try:
            await instance.close()
        except Exception:
            log.debug("تعذّر إغلاق مزوّد", exc_info=True)
    _instances.clear()
