"""أي نقطة نهاية متوافقة مع OpenAI API (Ollama, LM Studio, vLLM, LiteLLM).

يرث OpenAIProvider لا BaseProvider مباشرة: البروتوكول واحد، فبناء الحِمل
وكتل الصور وتسلسل الأدوات وتحليل الرد وقائمة النماذج منطق مشترك
لا يجوز تفريعه. لا يُتجاوَز إلا ما يختلف فعلًا.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from services.ai_providers.errors import (
    EmptyResponseError,
    ProviderError,
    ProviderUnavailableError,
)
from services.ai_providers.openai_provider import OpenAIProvider

log = logging.getLogger(__name__)

PLACEHOLDER_KEY = "not-required"
DEFAULT_TIMEOUT = 180.0   # الموديلات المحلية على CPU أبطأ من أي سحابة


@dataclass(frozen=True, slots=True)
class CustomConfig:
    """إعدادات ديناميكية من Mongo، لا ثوابت في الكود."""

    key: str
    name: str
    base_url: str
    model_name: str
    api_key: str | None = None
    timeout: float = DEFAULT_TIMEOUT

    @property
    def key_hint(self) -> str:
        if not self.api_key:
            return "بلا مفتاح"
        return f"…{self.api_key[-4:]}"


class CustomProvider(OpenAIProvider):
    name = "custom"
    allow_keyless = True      # Ollama محلي بلا مفتاح

    def __init__(self, config: CustomConfig, keyring: Any) -> None:
        super().__init__(keyring)
        self._config = config
        self.base_url = config.base_url
        self.timeout = config.timeout
        # مفتاح المستند يبقى احتياطيًا بعد بذره في provider_keys
        keyring.register_fallback(config.key, config.api_key)
        log.info(
            "مزوّد مخصّص جاهز: %s (%s، نموذج %s، %s)",
            config.name, config.base_url, config.model_name, config.key_hint,
        )

    @property
    def config(self) -> CustomConfig:
        return self._config

    @property
    def key_provider(self) -> str:   # type: ignore[override]
        """يظلّل ClassVar: كل مزوّد مخصّص له مجموعة مفاتيحه."""
        return self._config.key

    @property
    def label(self) -> str:
        return self._config.name

    # ───────────── ما يختلف فعلًا ─────────────

    def _params(
        self, *, temperature: float | None, max_tokens: int | None
    ) -> dict[str, Any]:
        """حِمل أدنى: خوادم محلية ترجع 400 على معاملات لا تعرفها."""
        params: dict[str, Any] = {}
        if temperature is not None:
            params["temperature"] = float(temperature)
        if max_tokens is not None:
            params["max_tokens"] = int(max_tokens)
        return params

    def _validate_response(self, response: Any, model: str) -> Any:
        """تحقّق متساهل: الخوادم المحلية تخرج عن المخطط أحيانًا."""
        choices = getattr(response, "choices", None)
        if not choices:
            raise EmptyResponseError(
                f"لم يرجع «{self._config.name}» أي اختيار. "
                "تأكّد أن النموذج محمَّل على الخادم.",
                provider=self.name, model=model,
            )
        choice = choices[0]
        if getattr(choice, "message", None) is None:
            raise EmptyResponseError(provider=self.name, model=model)
        return choice

    # ───────────── الفحص ─────────────

    async def probe(self) -> tuple[bool, str]:
        """يتحقق من الوصول ووجود النموذج. لا يرفع استثناءً."""
        try:
            models = await self.list_models()
        except Exception as exc:
            return False, self._diagnose(exc)

        available = [item.id for item in models]
        wanted = self._config.model_name
        if not available:
            return True, "الخادم يستجيب، لكنه لا يعرض قائمة نماذج."
        if wanted in available:
            return True, f"الخادم يستجيب، والنموذج «{wanted}» متاح."

        # Ollama يسمّي النماذج بلاحقة وسم: llama3.2 مقابل llama3.2:latest
        near = [
            item for item in available
            if item.split(":", 1)[0] == wanted.split(":", 1)[0]
        ]
        if near:
            return True, f"النموذج غير مطابق حرفيًا. المشابه: {', '.join(near[:3])}"
        return False, (
            f"النموذج «{wanted}» غير موجود على الخادم.\n"
            f"المتاح: {', '.join(available[:6])}"
        )

    def _diagnose(self, exc: BaseException) -> str:
        """رسائل أدقّ من نصّ الاستثناء الخام."""
        text = str(exc)
        lowered = text.lower()
        if "connect" in lowered:
            hint = "تأكّد أن الخادم يعمل وأن العنوان صحيح."
            if "localhost" in self._config.base_url or "127.0.0.1" in self._config.base_url:
                hint += (
                    "\nإن كان البوت داخل حاوية، فـ localhost يشير إلى الحاوية "
                    "نفسها لا إلى جهازك — استخدم host.docker.internal."
                )
            return f"تعذّر الاتصال. {hint}"
        if "401" in text or "unauthorized" in lowered:
            return "المفتاح مرفوض (401). راجع API key."
        if "404" in text:
            return "المسار غير موجود (404). أكثر سبب شائع: نسيان /v1 في نهاية العنوان."
        if "timeout" in lowered or "المهلة" in text:
            return "انتهت المهلة. الخادم بطيء أو النموذج يُحمَّل الآن."
        return f"فشل الفحص: {text[:200]}"


def build_custom_provider(config: CustomConfig, keyring: Any) -> CustomProvider:
    try:
        return CustomProvider(config, keyring)
    except ProviderError:
        raise
    except Exception as exc:
        raise ProviderUnavailableError(
            f"تعذّرت تهيئة «{config.name}»: {exc}", provider="custom", cause=exc
        ) from exc
