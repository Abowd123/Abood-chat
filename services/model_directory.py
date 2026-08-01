"""اكتشاف النماذج: نداء /v1/models، كاش Redis، ومجموعات عائلية.

الكاش على مستويين عن قصد:
  models:fresh:<p>  ساعة  — النتيجة الحاليّة
  models:last:<p>   أسبوع — آخر نتيجة ناجحة، تُعرَض عند فشل الجلب
مستوى واحد يعني شاشة خطأ بدل قائمة عمرها 70 دقيقة.
"""
from __future__ import annotations

import asyncio
import hashlib
import logging
import re
import time
from dataclasses import dataclass
from typing import Any, Callable, Mapping

log = logging.getLogger(__name__)

FRESH_TTL = 3600
LAST_GOOD_TTL = 604800
NEGATIVE_TTL = 90         # يمنع ضرب الـ API عند كل ضغطة أثناء عطل
FETCH_TIMEOUT = 25.0      # أقصر من مهلة تعديل رسالة تليجرام

HASH_LENGTH = 10          # 2^40: تصادم مهمَل في قوائم بالمئات
FAMILY_THRESHOLD = 24     # فوقه تُعرض العائلات أولًا

# نماذج ليست للمحادثة. OpenAI يرجعها في نفس القائمة.
NON_CHAT = (
    "embedding", "whisper", "tts-", "dall-e", "moderation", "davinci",
    "babbage", "audio-", "realtime", "-transcribe", "image-", "codex-mini",
    "computer-use", "rerank", "-tts", "stable-diffusion", "flux",
)


def model_hash(model_id: str) -> str:
    return hashlib.sha256(model_id.encode()).hexdigest()[:HASH_LENGTH]


def family_of(model_id: str) -> str:
    """`anthropic/claude-sonnet-4.5` → anthropic · `llama3.2:3b` → llama3.2"""
    if "/" in model_id:
        return model_id.split("/", 1)[0].split(":", 1)[0]
    head = model_id.split(":", 1)[0]
    parts = re.split(r"[-_.]", head, maxsplit=1)
    return parts[0] or head


def is_chat_model(model_id: str) -> bool:
    lowered = model_id.lower()
    return not any(marker in lowered for marker in NON_CHAT)


@dataclass(frozen=True, slots=True)
class ModelInfo:
    """سطر من /v1/models. الحقول الاختيارية تأتي من OpenRouter فقط."""

    id: str
    label: str = ""
    family: str = ""
    context: int | None = None
    input_price: float | None = None     # دولار/مليون توكن
    output_price: float | None = None
    vision: bool = False
    tools: bool = False
    created: int | None = None

    @property
    def name(self) -> str:
        return self.label or self.id

    @property
    def hash(self) -> str:
        return model_hash(self.id)

    @property
    def priced(self) -> bool:
        return self.input_price is not None

    def badge(self) -> str:
        marks = []
        if self.vision:
            marks.append("👁")
        if self.tools:
            marks.append("🛠")
        if self.input_price == 0:
            marks.append("🆓")
        return "".join(marks)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id, "label": self.label, "context": self.context,
            "in": self.input_price, "out": self.output_price,
            "vision": self.vision, "tools": self.tools, "created": self.created,
        }

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "ModelInfo":
        model_id = str(raw.get("id") or "")
        return cls(
            id=model_id,
            label=str(raw.get("label") or ""),
            family=family_of(model_id),
            context=raw.get("context"),
            input_price=raw.get("in"),
            output_price=raw.get("out"),
            vision=bool(raw.get("vision")),
            tools=bool(raw.get("tools")),
            created=raw.get("created"),
        )


@dataclass(frozen=True, slots=True)
class Listing:
    provider: str
    models: tuple[ModelInfo, ...] = ()
    fetched_at: float = 0.0
    cached: bool = False
    stale: bool = False        # نتيجة قديمة لأن الجلب فشل
    error: str | None = None

    @property
    def ok(self) -> bool:
        return bool(self.models)

    @property
    def failed(self) -> bool:
        return self.error is not None and not self.models

    @property
    def age(self) -> float:
        return max(0.0, time.time() - self.fetched_at) if self.fetched_at else 0.0

    def chat_only(self) -> tuple[ModelInfo, ...]:
        return tuple(item for item in self.models if is_chat_model(item.id))

    def visible(self, *, show_all: bool) -> tuple[ModelInfo, ...]:
        # قائمة كلها "غير محادثة" تُعرض كما هي بدل شاشة فارغة
        return self.models if show_all else (self.chat_only() or self.models)

    def hidden_count(self) -> int:
        return len(self.models) - len(self.chat_only())

    def families(self, *, show_all: bool = False) -> list[tuple[str, int]]:
        counts: dict[str, int] = {}
        for item in self.visible(show_all=show_all):
            counts[item.family] = counts.get(item.family, 0) + 1
        return sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))

    def in_family(self, family: str, *, show_all: bool = False) -> tuple[ModelInfo, ...]:
        return tuple(
            item for item in self.visible(show_all=show_all) if item.family == family
        )

    def by_hash(self, digest: str) -> ModelInfo | None:
        return next((item for item in self.models if item.hash == digest), None)

    def find(self, model_id: str) -> ModelInfo | None:
        return next((item for item in self.models if item.id == model_id), None)


class ModelDirectory:
    def __init__(
        self,
        redis: Any,
        *,
        fresh_ttl: int = FRESH_TTL,
        timeout: float = FETCH_TIMEOUT,
        on_prices: Callable[[str, dict[str, tuple]], None] | None = None,
    ) -> None:
        self._redis = redis
        self._ttl = fresh_ttl or FRESH_TTL
        self._timeout = timeout
        self._on_prices = on_prices
        self._locks: dict[str, asyncio.Lock] = {}
        self.fetches = 0
        self.cache_hits = 0

    # ---- مفاتيح Redis ----

    @staticmethod
    def _fresh_key(provider: str) -> str:
        return f"models:fresh:{provider}"

    @staticmethod
    def _last_key(provider: str) -> str:
        return f"models:last:{provider}"

    @staticmethod
    def _fail_key(provider: str) -> str:
        return f"models:fail:{provider}"

    def _lock(self, provider: str) -> asyncio.Lock:
        lock = self._locks.get(provider)
        if lock is None:
            lock = self._locks[provider] = asyncio.Lock()
        return lock

    # ---- الجلب ----

    async def fetch(self, provider: str, *, force: bool = False) -> Listing:
        """لا يرفع استثناءً أبدًا: الفشل يرجع Listing بحقل error."""
        async with self._lock(provider):   # ضغطتان سريعتان = نداء واحد
            if not force:
                cached = await self._read(self._fresh_key(provider), provider)
                if cached is not None:
                    self.cache_hits += 1
                    return cached
                blocked = await self._blocked(provider)
                if blocked is not None:
                    return blocked

            try:
                models = await asyncio.wait_for(
                    self._call(provider), timeout=self._timeout
                )
            except Exception as exc:
                return await self._on_failure(provider, exc)

            if not models:
                return await self._on_failure(
                    provider, RuntimeError("الخدمة رجّعت قائمة فارغة.")
                )

            listing = Listing(
                provider=provider, models=tuple(models), fetched_at=time.time()
            )
            await self._write(listing)
            self._publish_prices(listing)
            self.fetches += 1
            log.info("جُلب %s نموذجًا من %s", len(models), provider)
            return listing

    async def _call(self, provider: str) -> list[ModelInfo]:
        from services.ai_providers import list_available_models

        return await list_available_models(provider)

    async def peek(self, provider: str) -> Listing | None:
        """قراءة كاش بلا جلب — تمنع وميض مؤشّر التحميل عند التنقّل."""
        return await self._read(self._fresh_key(provider), provider)

    # ---- الفشل ----

    async def _on_failure(self, provider: str, exc: BaseException) -> Listing:
        message = self._describe(exc)
        log.warning("تعذّر جلب نماذج %s: %s", provider, message)
        await self._redis.set(self._fail_key(provider), message, ttl=NEGATIVE_TTL)

        fallback = await self._read(self._last_key(provider), provider)
        if fallback is not None:
            return Listing(
                provider=provider, models=fallback.models,
                fetched_at=fallback.fetched_at, cached=True,
                stale=True, error=message,
            )
        return Listing(provider=provider, error=message)

    async def _blocked(self, provider: str) -> Listing | None:
        message = await self._redis.get(self._fail_key(provider))
        if not message:
            return None
        fallback = await self._read(self._last_key(provider), provider)
        if fallback is not None:
            return Listing(
                provider=provider, models=fallback.models,
                fetched_at=fallback.fetched_at, cached=True,
                stale=True, error=message,
            )
        return Listing(provider=provider, error=message)

    @staticmethod
    def _describe(exc: BaseException) -> str:
        from services.ai_providers.errors import ProviderError

        if isinstance(exc, asyncio.TimeoutError):
            return "انتهت المهلة قبل أن تستجيب الخدمة."
        if isinstance(exc, ProviderError):
            return exc.user_message
        return f"{type(exc).__name__}: {str(exc)[:160]}"

    # ---- Redis ----

    async def _read(self, key: str, provider: str) -> Listing | None:
        payload = await self._redis.get_json(key)
        if not isinstance(payload, dict):
            return None
        rows = payload.get("models") or []
        if not rows:
            return None
        listing = Listing(
            provider=provider,
            models=tuple(ModelInfo.from_dict(row) for row in rows),
            fetched_at=float(payload.get("at") or 0.0),
            cached=True,
        )
        self._publish_prices(listing)   # إعادة تشغيل تفقد جدول الأسعار
        return listing

    async def _write(self, listing: Listing) -> None:
        payload = {
            "at": listing.fetched_at,
            "models": [item.to_dict() for item in listing.models],
        }
        await self._redis.set_json(
            self._fresh_key(listing.provider), payload, ttl=self._ttl
        )
        await self._redis.set_json(
            self._last_key(listing.provider), payload, ttl=LAST_GOOD_TTL
        )
        await self._redis.delete(self._fail_key(listing.provider))

    async def drop(self, provider: str) -> None:
        """يُنادى عند حذف مزوّد مخصّص: قائمة خادم آخر لا تصلح له."""
        await self._redis.delete(
            self._fresh_key(provider),
            self._last_key(provider),
            self._fail_key(provider),
        )

    # ---- الأسعار ----

    def _publish_prices(self, listing: Listing) -> None:
        if self._on_prices is None:
            return
        priced = {
            item.id: (item.input_price, item.output_price)
            for item in listing.models
            if item.priced
        }
        if priced:
            self._on_prices(listing.provider, priced)

    # ---- المساعدة ----

    async def resolve(self, provider: str, digest: str) -> ModelInfo | None:
        """يفكّ بصمة الزرّ. يجلب من جديد إن انتهى الكاش بين العرض والضغط."""
        listing = await self.fetch(provider)
        found = listing.by_hash(digest)
        if found is not None or not listing.cached:
            return found
        return (await self.fetch(provider, force=True)).by_hash(digest)
