"""القائمة المنسّقة + النماذج المُكتشَفة + resolve_model.

القائمة المنسّقة قدراتها متحقَّق منها، فهي المسار الذي لا يفشل.
الاكتشاف (`dyn:*`) يوسّع الخيارات ولا يستبدل المعرفة المؤكَّدة.

⚠️ أسماء النماذج أدناه قد تكون قديمة — استخدم زرّ «الخدمات» لجلب
ما هو متاح فعلًا من الـ API.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Iterable, Mapping, Sequence

from services.ai_providers.errors import ProviderError, ProviderUnavailableError

log = logging.getLogger(__name__)

DYN_PREFIX = "dyn:"
BUILTIN_PROVIDERS = ("openrouter", "anthropic", "openai")


@dataclass(frozen=True, slots=True)
class ModelSpec:
    key: str
    label: str
    provider: str
    model_id: str
    streaming: bool = True
    vision: bool = False
    tools: bool = False
    discovered: bool = False

    @property
    def custom(self) -> bool:
        return self.provider.startswith("cx-")


# القائمة المنسّقة: قدراتها متحقَّق منها يدويًا
MODELS: dict[str, ModelSpec] = {
    "or-sonnet": ModelSpec(
        key="or-sonnet", label="Claude Sonnet 4.5 (OpenRouter)",
        provider="openrouter", model_id="anthropic/claude-sonnet-4.5",
        vision=True, tools=True,
    ),
    "or-haiku": ModelSpec(
        key="or-haiku", label="Claude Haiku 4.5 (OpenRouter)",
        provider="openrouter", model_id="anthropic/claude-haiku-4.5",
        vision=True, tools=True,
    ),
    "or-gpt4o": ModelSpec(
        key="or-gpt4o", label="GPT-4o (OpenRouter)",
        provider="openrouter", model_id="openai/gpt-4o",
        vision=True, tools=True,
    ),
    "or-gemini-flash": ModelSpec(
        key="or-gemini-flash", label="Gemini 2.5 Flash (OpenRouter)",
        provider="openrouter", model_id="google/gemini-2.5-flash",
        vision=True, tools=True,
    ),
    "or-llama70b": ModelSpec(
        key="or-llama70b", label="Llama 3.3 70B (OpenRouter)",
        provider="openrouter", model_id="meta-llama/llama-3.3-70b-instruct",
        tools=True,
    ),
    "an-sonnet": ModelSpec(
        key="an-sonnet", label="Claude Sonnet 4.5 (Anthropic)",
        provider="anthropic", model_id="claude-sonnet-4-5",
        vision=True, tools=True,
    ),   "an-haiku": ModelSpec(
        key="an-haiku", label="Claude Haiku 4.5 (Anthropic)",
        provider="anthropic", model_id="claude-haiku-4-5",
        vision=True, tools=True,
    ),
    "oa-gpt4o": ModelSpec(
        key="oa-gpt4o", label="GPT-4o (OpenAI)",
        provider="openai", model_id="gpt-4o",
        vision=True, tools=True,
    ),
    "oa-gpt4o-mini": ModelSpec(
        key="oa-gpt4o-mini", label="GPT-4o mini (OpenAI)",
        provider="openai", model_id="gpt-4o-mini",
        vision=True, tools=True,
    ),
}

DEFAULT_MODEL = "or-sonnet"

# سجلّ المزوّدات المخصّصة (يُضبط في الإقلاع)
_custom: Any = None

# قدرات النماذج المُكتشَفة المستخدَمة: محفوظة عند الاختيار لا مخمّنة
_caps: dict[str, dict[str, Any]] = {}


# ───────────────────────── التسجيل ─────────────────────────

def register_custom(registry: Any) -> None:
    """يُنادى في الإقلاع بعد refresh(). بدونه لا تُحلّ مفاتيح cx-*."""
    global _custom
    _custom = registry


def custom_registry() -> Any:
    return _custom


def remember_caps(caps: Sequence[Any]) -> None:
    """يُنادى من SettingsRepository: يُبقي القدرات متاحة للفحص المتزامن."""
    for item in caps:
        _caps[item.key] = {
            "label": item.label,
            "vision": item.vision,
            "tools": item.tools,
            "context": item.context,
        }


def remember_one(key: str, *, label: str, vision: bool, tools: bool,
                 context: int | None = None) -> None:
    _caps[key] = {
        "label": label, "vision": vision, "tools": tools, "context": context
    }


# ───────────────────────── المفاتيح المُكتشَفة ─────────────────────────

def dynamic_key(provider: str, model_id: str) -> str:
    return f"{DYN_PREFIX}{provider}:{model_id}"


def parse_dynamic(key: Any) -> tuple[str, str] | None:
    """`dyn:openrouter:anthropic/claude-4.5` → (openrouter, anthropic/claude-4.5)

    maxsplit=2 مقصود: معرّفات Ollama تحمل نقطتين (`llama3.2:3b`).
    """
    if not isinstance(key, str) or not key.startswith(DYN_PREFIX):
        return None
    parts = key.split(":", 2)
    if len(parts) != 3 or not parts[1] or not parts[2]:
        return None
    return parts[1], parts[2]


def is_dynamic(key: Any) -> bool:
    return parse_dynamic(key) is not None


def dynamic_spec(key: str) -> ModelSpec | None:
    parsed = parse_dynamic(key)
    if parsed is None:
        return None
    provider, model_id = parsed
    caps = _caps.get(key, {})
    return ModelSpec(
        key=key,
        label=str(caps.get("label") or model_id),
        provider=provider,
        model_id=model_id,
        streaming=True,
        vision=bool(caps.get("vision")),
        tools=bool(caps.get("tools")),
        discovered=True,
    )


# ───────────────────────── القراءة الموحّدة ─────────────────────────

def custom_specs() -> dict[str, ModelSpec]:
    """المزوّدات المخصّصة كنموذج جاهز بنموذجه الافتراضي."""
    if _custom is None:
        return {}
    return _custom.specs()


def all_specs() -> dict[str, ModelSpec]:
    """المنسّقة أولًا ثم المخصّصة. لا تكرار: بادئة cx- تمنعه."""
    return {**MODELS, **custom_specs()}


def model_spec(key: str) -> ModelSpec | None:
    direct = all_specs().get(key)
    if direct is not None:
        return direct
    return dynamic_spec(key)


def is_valid_model(key: Any) -> bool:
    """فحص بنيوي لا يلمس الشبكة: نداء تحقّق لكل رسالة ثمن باهظ."""
    if not isinstance(key, str) or not key:
        return False
    if key in all_specs():
        return True
    parsed = parse_dynamic(key)
    if parsed is None:
        return False
    provider, _ = parsed
    if provider.startswith("cx-"):
        return bool(_custom is not None and _custom.knows(provider))
    return provider in BUILTIN_PROVIDERS


def normalize_model(key: Any) -> str:
    candidate = (key or "").strip() if isinstance(key, str) else ""
    return candidate if is_valid_model(candidate) else DEFAULT_MODEL


def model_label(key: str) -> str:
    spec = model_spec(key)
    return spec.label if spec else key


def model_labels() -> dict[str, str]:
    return {key: spec.label for key, spec in all_specs().items()}


def provider_of(key: str) -> str:
    parsed = parse_dynamic(key)
    if parsed is not None:
        return parsed[0]
    spec = model_spec(key)
    return spec.provider if spec else ""


def supports_vision(key: str) -> bool:
    spec = model_spec(key)
    return bool(spec and spec.vision)


def supports_tools(key: str) -> bool:
    spec = model_spec(key)
    return bool(spec and spec.tools)


def supports_streaming(key: str) -> bool:
    spec = model_spec(key)
    return bool(spec and spec.streaming)


def known_providers() -> list[str]:
    """الجاهزة ثم المخصّصة — ترتيب شاشة الخدمات."""
    custom = list(custom_specs().values()) if _custom is not None else []
    return [*BUILTIN_PROVIDERS, *(spec.provider for spec in custom)]


def _provider_has_key(provider: str) -> bool:
    """Mongo هو المصدر بعد البذر لا .env."""
    if provider.startswith("cx-"):
        return True   # المحلي يعمل بلا مفتاح
    try:
        from services.ai_providers import keyring

        return keyring().has_usable(provider)
    except Exception:
        return False


def locked_models() -> set[str]:
    """نموذج مزوّده بلا مفتاح صالح — أصدق من إخفاء المشكلة."""
    return {
        key for key, spec in MODELS.items() if not _provider_has_key(spec.provider)
    }


def model_badges() -> dict[str, str]:
    badges: dict[str, str] = {}
    for key, spec in all_specs().items():
        marks = []
        if spec.vision:
            marks.append("👁")
        if spec.tools:
            marks.append("🛠")
        if marks:
            badges[key] = "".join(marks)
    return badges


# ───────────────────────── الحلّ ─────────────────────────

@dataclass(frozen=True, slots=True)
class Resolved:
    provider: Any
    model_id: str
    spec: ModelSpec


def resolve_model(key: str) -> Resolved:
    """يحوّل مفتاح الإعدادات إلى مزوّد جاهز للنداء."""
    from services.ai_providers import resolve_provider_instance

    normalized = normalize_model(key)

    parsed = parse_dynamic(normalized)
    if parsed is not None:
        provider_name, model_id = parsed
        instance = resolve_provider_instance(provider_name)
        if instance is None:
            raise ProviderUnavailableError(
                f"المزوّد «{provider_name}» لم يعد موجودًا. "
                "اختر نموذجًا آخر من ⚙️ الإعدادات → 🤖 النموذج.",
                provider=provider_name,
            )
        return Resolved(
            provider=instance,
            model_id=model_id,
            spec=dynamic_spec(normalized) or ModelSpec(
                key=normalized, label=model_id, provider=provider_name,
                model_id=model_id, discovered=True,
            ),
        )

    spec = model_spec(normalized)
    if spec is None:
        raise ProviderError(f"نموذج غير معروف: {normalized!r}")

    instance = resolve_provider_instance(spec.provider)
    if instance is None:
        raise ProviderUnavailableError(
            f"المزوّد «{spec.provider}» غير متاح. "
            "راجع ⚙️ الإعدادات → 🔑 المفاتيح.",
            provider=spec.provider,
        )
    return Resolved(provider=instance, model_id=spec.model_id, spec=spec)
