"""اختيار المفاتيح وتتبّع صحّتها. المكان الوحيد الذي يعرف الاستراتيجية.

لقطة في الذاكرة عن قصد: `candidates()` يُنادى في كل نداء AI، وقراءة
Mongo هناك تعني استعلامًا إضافيًا لكل رسالة. النسخة واحدة فلا تزامن.
"""
from __future__ import annotations

import logging
import random
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Iterable, Sequence

from database.provider_keys import (
    DEFAULT_COOLDOWN,
    TOUCH_INTERVAL,
    ProviderKey,
    ProviderKeyRepository,
    hint,
)
from services.ai_providers.errors import (
    AuthError,
    ProviderError,
    QuotaError,
    RateLimitError,
)

log = logging.getLogger(__name__)

ROUND_ROBIN = "round_robin"
RANDOM = "random"
STRATEGIES = (ROUND_ROBIN, RANDOM)
DEFAULT_THRESHOLD = 3

# ما يُنسب إلى المفتاح. المهلات و5xx مستثناة عن قصد:
# انقطاع عند المزوّد كان سيُعطّل كل مفاتيحك السليمة في دقيقة.
KEY_ERRORS = (AuthError, QuotaError, RateLimitError)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def is_key_error(error: BaseException) -> bool:
    return isinstance(error, KEY_ERRORS)


@dataclass(frozen=True, slots=True)
class Credential:
    """مفتاح جاهز. key_id فارغ = غير مُدار (بيئة أو بلا مفتاح)."""

    provider: str
    value: str | None
    key_id: str = ""
    label: str | None = None
    stale: bool = False    # يستحق كتابة last_used_at
    dirty: bool = False    # عليه فشل سابق يحتاج تصفيرًا

    @property
    def managed(self) -> bool:
        return bool(self.key_id)

    @property
    def hint(self) -> str:
        return hint(self.value)

    @property
    def display(self) -> str:
        return f"{self.label} ({self.hint})" if self.label else self.hint

    @property
    def cache_key(self) -> str:
        """مخزن العملاء: العميل يجمّد المفتاح في بانيه."""
        return f"{self.provider}:{self.key_id or 'env'}"


@dataclass(frozen=True, slots=True)
class ProviderStatus:
    name: str
    label: str
    total: int
    active: int
    cooling: int
    disabled: int
    env_fallback: bool

    @property
    def healthy(self) -> bool:
        return self.active > 0 or self.env_fallback

    @property
    def summary(self) -> str:
        if self.total:
            text = f"{self.active}/{self.total} مفتاح"
            if self.cooling:
                text += f" · {self.cooling} مبرَّد"
            if self.disabled:
                text += f" · {self.disabled} معطّل"
            return text
        return "من .env" if self.env_fallback else "بلا مفاتيح"


class KeyRing:
    def __init__(
        self,
        repo: ProviderKeyRepository,
        *,
        strategy: str = ROUND_ROBIN,
        threshold: int = DEFAULT_THRESHOLD,
        env_keys: dict[str, str | None] | None = None,
        notifier: Any = None,
        labels: dict[str, str] | None = None,
    ) -> None:
        self._repo = repo
        self._strategy = strategy if strategy in STRATEGIES else ROUND_ROBIN
        self._threshold = max(1, threshold)
        self._env = {name: value for name, value in (env_keys or {}).items() if value}
        self._notifier = notifier
        self._labels = dict(labels or {})
        self._keys: dict[str, list[ProviderKey]] = {}
        self._cursor: dict[str, int] = {}
        self._fallbacks: dict[str, str] = {}

    @property
    def repo(self) -> ProviderKeyRepository:
        return self._repo

    @property
    def strategy(self) -> str:
        return self._strategy

    @property
    def threshold(self) -> int:
        return self._threshold

    def label_for(self, provider: str) -> str:
        if provider in self._labels:
            return self._labels[provider]
        from services.ai_providers import provider_label

        return provider_label(provider)

    def set_labels(self, labels: dict[str, str]) -> None:
        self._labels.update(labels)

    def set_strategy(self, name: str) -> str:
        if name in STRATEGIES:
            self._strategy = name
            log.info("استراتيجية المفاتيح: %s", name)
        return self._strategy

    def register_fallback(self, provider: str, value: str | None) -> None:
        """مفتاح المزوّد المخصّص من مستنده، احتياطيًا."""
        if value:
            self._fallbacks[provider] = value
        else:
            self._fallbacks.pop(provider, None)

    # ───────────── اللقطة ─────────────

    async def refresh(self) -> int:
        keys = await self._repo.list_all()
        grouped: dict[str, list[ProviderKey]] = {}
        for key in keys:
            grouped.setdefault(key.provider_name, []).append(key)
        self._keys = grouped
        log.info(
            "حلقة المفاتيح: %s مفتاحًا عبر %s مزوّدًا (%s)",
            len(keys), len(grouped), self._strategy,
        )
        return len(keys)

    async def seed_from_env(self) -> int:
        """ينقل مفاتيح .env إلى Mongo مرة واحدة، فيصير المسار موحّدًا.

        للمزوّد الذي لا يملك أي مفتاح مُدار فقط: لا يكرّر ولا يكتب فوق.
        """
        added = 0
        sources = [(name, value, ".env") for name, value in self._env.items()]
        sources += [
            (name, value, "من إعداد المزوّد")
            for name, value in self._fallbacks.items()
        ]
        for provider, value, label in sources:
            if self._keys.get(provider):
                continue
            try:
                await self._repo.add(provider, value, label=label)
                added += 1
                log.info("بُذر مفتاح %s (%s)", provider, label)
            except Exception as exc:
                log.warning("تعذّر بذر مفتاح %s: %s", provider, exc)
        if added:
            await self.refresh()
        return added

    # ───────────── الاختيار ─────────────

    def candidates(
        self, provider: str, *, allow_keyless: bool = False
    ) -> list[Credential]:
        """القائمة كاملة ومرتّبة: الحلقة تحتاج البقية للتبديل عند الفشل."""
        now = _now()
        pool = self._keys.get(provider) or []
        usable = [key for key in pool if key.is_active and not key.cooling(now)]

        if not usable:
            # التهدئة تقدير لا حقيقة: مفتاح مبرَّد أفضل من رفض الرد
            usable = [key for key in pool if key.is_active]
            if usable:
                log.info("كل مفاتيح %s مبرَّدة — محاولة رغم ذلك", provider)

        if usable:
            return [
                Credential(
                    provider=provider,
                    value=key.key_value,
                    key_id=key.key_id,
                    label=key.label,
                    stale=self._is_stale(key, now),
                    dirty=key.failure_count > 0 or key.cooldown_until is not None,
                )
                for key in self._order(provider, usable)
                if key.key_value
            ]

        fallback = self._fallbacks.get(provider) or self._env.get(provider)
        if fallback:
            log.info("لا مفاتيح مُدارة لـ %s — استخدام الاحتياطي", provider)
            return [Credential(provider=provider, value=fallback)]

        if allow_keyless:
            return [Credential(provider=provider, value=None)]
        return []

    def _order(self, provider: str, keys: Sequence[ProviderKey]) -> list[ProviderKey]:
        if len(keys) == 1:
            return list(keys)
        if self._strategy == RANDOM:
            shuffled = list(keys)
            random.shuffle(shuffled)
            return shuffled
        start = self._cursor.get(provider, 0) % len(keys)
        self._cursor[provider] = (start + 1) % len(keys)
        return [*keys[start:], *keys[:start]]

    @staticmethod
    def _is_stale(key: ProviderKey, now: datetime) -> bool:
        if key.last_used_at is None:
            return True
        return (now - key.last_used_at).total_seconds() > TOUCH_INTERVAL

    def missing_key_error(self, provider: str) -> ProviderError:
        label = self.label_for(provider)
        pool = self._keys.get(provider) or []
        if pool:
            return AuthError(
                f"كل مفاتيح «{label}» معطّلة ({len(pool)} مفتاحًا). "
                "أعِد تفعيل واحدًا أو أضف جديدًا من ⚙️ الإعدادات → 🔑 المفاتيح.",
                provider=provider,
            )
        return AuthError(
            f"لا يوجد مفتاح لـ «{label}». "
            "أضفه من ⚙️ الإعدادات → 🔑 المفاتيح → ➕ إضافة مفتاح.",
            provider=provider,
        )

    # ───────────── التقرير ─────────────

    async def report_success(self, credential: Credential) -> None:
        if not credential.managed:
            return
        if not (credential.dirty or credential.stale):
            return   # لا كتابة Mongo لكل رد بلا فائدة
        await self._repo.mark_success(credential.key_id, reset=credential.dirty)
        if credential.dirty:
            await self.refresh()

    async def report_failure(
        self, credential: Credential, error: BaseException
    ) -> None:
        if not credential.managed:
            log.warning("فشل مفتاح غير مُدار لـ %s: %s", credential.provider, error)
            return

        cooldown = None
        if isinstance(error, RateLimitError):
            # 429 يعني ازدحامًا لا مفتاحًا تالفًا: نبرّده ولا نُعطّله فورًا
            cooldown = getattr(error, "retry_after", None) or DEFAULT_COOLDOWN

        count, disabled = await self._repo.mark_failure(
            credential.key_id,
            reason=type(error).__name__,
            threshold=self._threshold,
            cooldown=cooldown,
        )
        await self.refresh()

        if disabled:
            await self._alert_disabled(credential, error, count)
        else:
            log.warning(
                "فشل المفتاح %s لـ %s (%s/%s): %s",
                credential.hint, credential.provider, count, self._threshold,
                type(error).__name__,
            )

    async def _alert_disabled(
        self, credential: Credential, error: BaseException, count: int
    ) -> None:
        if self._notifier is None:
            return
        label = self.label_for(credential.provider)
        remaining = len(
            [k for k in (self._keys.get(credential.provider) or []) if k.usable]
        )
        tail = (
            f"\nمفاتيح صالحة متبقّية: <code>{remaining}</code>"
            if remaining
            else "\n<b>⚠️ لم يبقَ أي مفتاح صالح لهذا المزوّد.</b>"
        )
        await self._notifier.send(
            "🔑 <b>عُطِّل مفتاح تلقائيًا</b>\n"
            f"المزوّد: <code>{label}</code>\n"
            f"المفتاح: <code>{credential.display}</code>\n"
            f"السبب: <code>{type(error).__name__}</code> بعد {count} فشل متتالٍ"
            f"{tail}",
            tag=f"key-disabled:{credential.key_id}",
            cooldown=0,
        )

    # ───────────── العرض ─────────────

    def status(self, provider: str) -> ProviderStatus:
        now = _now()
        pool = self._keys.get(provider) or []
        return ProviderStatus(
            name=provider,
            label=self.label_for(provider),
            total=len(pool),
            active=len([k for k in pool if k.is_active and not k.cooling(now)]),
            cooling=len([k for k in pool if k.is_active and k.cooling(now)]),
            disabled=len([k for k in pool if not k.is_active]),
            env_fallback=bool(
                not pool and (provider in self._env or provider in self._fallbacks)
            ),
        )

    def statuses(self, providers: Iterable[str]) -> list[ProviderStatus]:
        return [self.status(name) for name in providers]

    def keys_for(self, provider: str) -> list[ProviderKey]:
        return list(self._keys.get(provider) or [])

    def total_keys(self) -> int:
        return sum(len(pool) for pool in self._keys.values())

    def has_usable(self, provider: str) -> bool:
        return self.status(provider).healthy

    # ───────────── تعديلات تمرّ بالحلقة كي تُحدَّث اللقطة ─────────────

    async def add(
        self, provider: str, value: str, label: str | None = None
    ) -> ProviderKey:
        key = await self._repo.add(provider, value, label)
        await self.refresh()
        return key

    async def remove(self, key_id: str) -> bool:
        removed = await self._repo.delete(key_id)
        if removed:
            await self.refresh()
        return removed

    async def set_active(self, key_id: str, active: bool) -> ProviderKey | None:
        key = await self._repo.set_active(key_id, active)
        await self.refresh()
        return key

    async def clear_provider(self, provider: str) -> int:
        removed = await self._repo.delete_for_provider(provider)
        self.register_fallback(provider, None)
        await self.refresh()
        return removed
