"""تحميل الإعدادات من البيئة والتحقّق منها قبل أي شيء آخر.

الفلسفة: يفشل الإقلاع فورًا وبرسالة واضحة عند نقص متغيّر إلزامي،
بدل أن يعمل البوت ثم يسقط عند أول رسالة.
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field

from dotenv import load_dotenv

log = logging.getLogger(__name__)

load_dotenv(override=False)

TRUE_VALUES = {"1", "true", "yes", "on", "y", "نعم"}


class ConfigError(RuntimeError):
    """نقص أو خطأ في متغيّرات البيئة."""


def _raw(name: str) -> str:
    return (os.getenv(name) or "").strip()


def _text(name: str, *, required: bool = False, default: str = "") -> str:
    value = _raw(name)
    if not value:
        if required:
            raise ConfigError(f"المتغيّر {name} مطلوب ولم يُضبط في البيئة.")
        return default
    return value


def _optional(name: str) -> str | None:
    return _raw(name) or None


def _integer(name: str, *, required: bool = False, default: int = 0) -> int:
    value = _raw(name)
    if not value:
        if required:
            raise ConfigError(f"المتغيّر {name} مطلوب ولم يُضبط في البيئة.")
        return default
    try:
        return int(value)
    except ValueError as exc:
        raise ConfigError(f"المتغيّر {name} يجب أن يكون رقمًا صحيحًا، وصل: {value!r}") from exc


def _decimal(name: str, *, default: float) -> float:
    value = _raw(name)
    if not value:
        return default
    try:
        return float(value)
    except ValueError as exc:
        raise ConfigError(f"المتغيّر {name} يجب أن يكون رقمًا، وصل: {value!r}") from exc


def _flag(name: str, *, default: bool = False) -> bool:
    value = _raw(name)
    return value.lower() in TRUE_VALUES if value else default


@dataclass(frozen=True, slots=True)
class Settings:
    # تليجرام
    api_id: int
    api_hash: str
    bot_token: str
    owner_id: int

    # قواعد البيانات
    mongo_uri: str
    mongo_db: str
    redis_url: str

    # مفاتيح المزوّدات (احتياطية — Mongo هو المصدر بعد البذر)
    openrouter_api_key: str | None
    openai_api_key: str | None
    anthropic_api_key: str | None

    # التشفير
    content_encryption_key: str | None

    # النموذج
    default_model: str
    max_output_tokens: int
    temperature: float
    context_messages: int
    streaming: bool

    # تعدّد المفاتيح
    key_strategy: str
    key_failure_threshold: int
    key_seed_from_env: bool

    # كاش النماذج
    model_cache_ttl: int

    # الذاكرة
    embedding_base_url: str
    embedding_api_key: str | None
    embedding_model: str
    memory_top_k: int
    memory_min_score: float
    memory_max_scan: int
    memory_vector_index: str | None

    # الصوت
    whisper_base_url: str
    whisper_api_key: str | None
    whisper_model: str

    # البحث
    serper_api_key: str | None
    brave_api_key: str | None
    search_provider: str
    search_results: int

    # التشغيل
    log_level: str
    timezone: str

    # ---- خصائص مشتقّة ----

    @property
    def memory_available(self) -> bool:
        """الذاكرة تحتاج مزوّد embeddings؛ المحلي لا يحتاج مفتاحًا."""
        if not self.embedding_base_url or not self.embedding_model:
            return False
        return bool(self.embedding_api_key) or self._is_local(self.embedding_base_url)

    @property
    def transcription_available(self) -> bool:
        if not self.whisper_base_url or not self.whisper_model:
            return False
        return bool(self.whisper_api_key) or self._is_local(self.whisper_base_url)

    @property
    def search_available(self) -> bool:
        return bool(self.serper_api_key or self.brave_api_key)

    @property
    def active_search_provider(self) -> str | None:
        if self.search_provider == "serper":
            return "serper" if self.serper_api_key else None
        if self.search_provider == "brave":
            return "brave" if self.brave_api_key else None
        if self.serper_api_key:
            return "serper"
        if self.brave_api_key:
            return "brave"
        return None

    @property
    def env_provider_keys(self) -> dict[str, str | None]:
        return {
            "openrouter": self.openrouter_api_key,
            "openai": self.openai_api_key,
            "anthropic": self.anthropic_api_key,
        }

    @staticmethod
    def _is_local(url: str) -> bool:
        lowered = url.lower()
        markers = ("localhost", "127.0.0.1", "::1", "host.docker.internal", ".local")
        return any(marker in lowered for marker in markers)

    def summary(self) -> dict[str, object]:
        """ملخّص للسجلّ. لا يحتوي أي قيمة سرّية."""
        return {
            "owner_id": self.owner_id,
            "mongo_db": self.mongo_db,
            "default_model": self.default_model,
            "streaming": self.streaming,
            "encryption": bool(self.content_encryption_key),
            "env_keys": [name for name, value in self.env_provider_keys.items() if value],
            "memory": self.memory_available,
            "transcription": self.transcription_available,
            "search": self.active_search_provider or "معطّل",
            "key_strategy": self.key_strategy,
        }


def load_settings() -> Settings:
    strategy = _text("KEY_STRATEGY", default="round_robin").lower()
    if strategy not in ("round_robin", "random"):
        log.warning("KEY_STRATEGY غير معروف (%s) — استُخدم round_robin", strategy)
        strategy = "round_robin"

    search_provider = _text("SEARCH_PROVIDER", default="auto").lower()
    if search_provider not in ("auto", "serper", "brave"):
        search_provider = "auto"

    settings = Settings(
        api_id=_integer("API_ID", required=True),
        api_hash=_text("API_HASH", required=True),
        bot_token=_text("BOT_TOKEN", required=True),
        owner_id=_integer("OWNER_ID", required=True),
        mongo_uri=_text("MONGO_URI", required=True),
        mongo_db=_text("MONGO_DB", default="tgbot"),
        redis_url=_text("REDIS_URL", default="redis://localhost:6379/0"),
        openrouter_api_key=_optional("OPENROUTER_API_KEY"),
        openai_api_key=_optional("OPENAI_API_KEY"),
        anthropic_api_key=_optional("ANTHROPIC_API_KEY"),
        content_encryption_key=_optional("CONTENT_ENCRYPTION_KEY"),
        default_model=_text("DEFAULT_MODEL", default="or-sonnet"),
        max_output_tokens=_integer("MAX_OUTPUT_TOKENS", default=4096),
        temperature=_decimal("TEMPERATURE", default=0.7),
        context_messages=max(2, _integer("CONTEXT_MESSAGES", default=20)),
        streaming=_flag("STREAMING", default=True),
        key_strategy=strategy,
        key_failure_threshold=max(1, _integer("KEY_FAILURE_THRESHOLD", default=3)),
        key_seed_from_env=_flag("KEY_SEED_FROM_ENV", default=True),
        model_cache_ttl=max(0, _integer("MODEL_CACHE_TTL", default=3600)),
        embedding_base_url=_text(
            "EMBEDDING_BASE_URL", default="https://api.openai.com/v1"
        ).rstrip("/"),
        embedding_api_key=_optional("EMBEDDING_API_KEY") or _optional("OPENAI_API_KEY"),
        embedding_model=_text("EMBEDDING_MODEL", default="text-embedding-3-small"),
        memory_top_k=max(1, _integer("MEMORY_TOP_K", default=5)),
        memory_min_score=_decimal("MEMORY_MIN_SCORE", default=0.25),
        memory_max_scan=max(100, _integer("MEMORY_MAX_SCAN", default=1500)),
        memory_vector_index=_optional("MEMORY_VECTOR_INDEX"),
        whisper_base_url=_text(
            "WHISPER_BASE_URL", default="https://api.openai.com/v1"
        ).rstrip("/"),
        whisper_api_key=_optional("WHISPER_API_KEY") or _optional("OPENAI_API_KEY"),
        whisper_model=_text("WHISPER_MODEL", default="whisper-1"),
        serper_api_key=_optional("SERPER_API_KEY"),
        brave_api_key=_optional("BRAVE_API_KEY"),
        search_provider=search_provider,
        search_results=max(1, min(10, _integer("SEARCH_RESULTS", default=5))),
        log_level=_text("LOG_LEVEL", default="INFO").upper(),
        timezone=_text("TZ", default="UTC"),
    )

    if not settings.content_encryption_key:
        log.warning(
            "CONTENT_ENCRYPTION_KEY غير مضبوط — "
            "مفاتيح API ستُخزَّن نصًا صريحًا في Mongo"
        )
    return settings
