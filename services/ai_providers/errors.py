"""أخطاء موحّدة لكل المزوّدات.

كل خطأ يحمل `user_message` صالحة للعرض مباشرة: المستخدم هو المالك،
فرسالة مفيدة أنفع من نصّ استثناء خام.
"""
from __future__ import annotations

from typing import Any


class ProviderError(RuntimeError):
    default_message = "تعذّر إكمال الطلب مع مزوّد الذكاء الاصطناعي."

    def __init__(
        self,
        message: str | None = None,
        *,
        provider: str = "",
        model: str | None = None,
        retry_after: int | None = None,
        cause: BaseException | None = None,
    ) -> None:
        self.user_message = message or self.default_message
        self.provider = provider
        self.model = model
        self.retry_after = retry_after
        self.cause = cause
        super().__init__(self.user_message)

    def __str__(self) -> str:
        return self.user_message


class AuthError(ProviderError):
    default_message = (
        "المفتاح مرفوض من الخدمة (401/403). "
        "راجع ⚙️ الإعدادات → 🔑 المفاتيح."
    )


class QuotaError(ProviderError):
    default_message = "رصيد المفتاح منتهٍ أو الحصة مستنفدة."


class RateLimitError(ProviderError):
    default_message = "تجاوزتَ حدّ الطلبات مؤقتًا (429)."


class ProviderTimeoutError(ProviderError):
    default_message = "انتهت المهلة قبل أن تستجيب الخدمة."


class ProviderUnavailableError(ProviderError):
    default_message = "الخدمة غير متاحة حاليًا."


class BadRequestError(ProviderError):
    default_message = "الطلب مرفوض من الخدمة."


class EmptyResponseError(ProviderError):
    default_message = "رجع ردّ فارغ من الخدمة."


class VisionUnsupportedError(ProviderError):
    default_message = (
        "النموذج الحالي لا يقرأ الصور. "
        "اختر نموذجًا بصريًا من 🤖 النموذج، أو فعّل «الصور» لهذا النموذج."
    )


class ToolsUnsupportedError(ProviderError):
    default_message = "النموذج الحالي لا يدعم الأدوات."


class ModelListingUnsupported(ProviderError):
    default_message = "هذه الخدمة لا توفّر قائمة نماذج."


def parse_retry_after(value: Any) -> int | None:
    if value in (None, ""):
        return None
    try:
        return max(1, int(float(value)))
    except (TypeError, ValueError):
        return None


def translate_sdk_error(
    exc: BaseException, *, provider: str, model: str | None = None
) -> ProviderError:
    """ترجمة موحّدة: SDKs الثلاثة تتطابق أسماء استثناءاتها."""
    if isinstance(exc, ProviderError):
        return exc

    name = type(exc).__name__
    status = getattr(exc, "status_code", None)
    response = getattr(exc, "response", None)
    if status is None and response is not None:
        status = getattr(response, "status_code", None)

    retry_after = None
    headers = getattr(response, "headers", None)
    if headers:
        try:
            retry_after = parse_retry_after(headers.get("retry-after"))
        except Exception:
            retry_after = None

    text = str(exc)
    lowered = text.lower()

    if name in ("AuthenticationError", "PermissionDeniedError") or status in (401, 403):
        return AuthError(provider=provider, model=model, cause=exc)
    if status == 402 or "insufficient" in lowered or "credit balance" in lowered:
        return QuotaError(provider=provider, model=model, cause=exc)
    if name == "RateLimitError" or status == 429:
        return RateLimitError(
            provider=provider, model=model, retry_after=retry_after, cause=exc
        )
    if name in ("APITimeoutError", "APIConnectionError") or isinstance(
        exc, (TimeoutError, ConnectionError)
    ):
        return ProviderTimeoutError(
            f"تعذّر الوصول إلى «{provider}»: {text[:160]}",
            provider=provider, model=model, cause=exc,
        )
    if status == 404:
        return BadRequestError(
            f"النموذج أو المسار غير موجود عند «{provider}» (404). "
            "تأكّد من اسم النموذج ومن انتهاء العنوان بـ /v1.",
            provider=provider, model=model, cause=exc,
        )
    if status in (400, 422):
        return BadRequestError(
            f"الطلب مرفوض ({status}): {text[:200]}",
            provider=provider, model=model, cause=exc,
        )
    if status is not None and 500 <= int(status) < 600:
        return ProviderUnavailableError(
            f"عطل عند «{provider}» ({status}). جرّب بعد قليل.",
            provider=provider, model=model, retry_after=retry_after, cause=exc,
        )
    return ProviderUnavailableError(
        f"خطأ من «{provider}»: {text[:200]}",
        provider=provider, model=model, cause=exc,
    )
