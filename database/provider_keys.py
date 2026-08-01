"""مفاتيح API: قائمة لكل مزوّد بدل مفتاح واحد.

⚠️ هذه أخطر مجموعة في المشروع: تحمل كل مفاتيح فاتورتك.
القيمة مشفَّرة عبر ContentCipher، ولا تُعرض ولا تُسجَّل ولا تُصدَّر أبدًا.
"""
from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Mapping

from bson import ObjectId
from pymongo import ASCENDING, DESCENDING
from pymongo.errors import DuplicateKeyError

from database.mongo import Mongo
from services.crypto import ContentCipher

log = logging.getLogger(__name__)

COLLECTION = "provider_keys"
ID_LENGTH = 12
MAX_PER_PROVIDER = 15
VALUE_MIN, VALUE_MAX = 8, 400
LABEL_MAX = 30

DEFAULT_COOLDOWN = 60
MAX_COOLDOWN = 900
TOUCH_INTERVAL = 120   # لا نكتب last_used_at في كل نداء


class ProviderKeyError(ValueError):
    """خطأ إدخال، رسالته صالحة للعرض."""


def _now() -> datetime:
    return datetime.now(timezone.utc)


def make_id(oid: ObjectId) -> str:
    return str(oid)[-ID_LENGTH:]


def fingerprint(value: str) -> str:
    """يمنع إضافة نفس المفتاح مرتين: تكراره يُبطل التوزيع بصمت."""
    return hashlib.sha256(value.strip().encode()).hexdigest()[:32]


def hint(value: str | None) -> str:
    """آخر 4 رموز فقط، للتمييز."""
    if not value:
        return "بلا مفتاح"
    cleaned = value.strip()
    return f"…{cleaned[-4:]}" if len(cleaned) > 4 else "…" * len(cleaned)


def validate_value(raw: str) -> str:
    value = (raw or "").strip()
    if not (VALUE_MIN <= len(value) <= VALUE_MAX):
        raise ProviderKeyError(
            f"قيمة المفتاح يجب أن تكون بين {VALUE_MIN} و{VALUE_MAX} حرفًا."
        )
    if any(ch.isspace() for ch in value):
        raise ProviderKeyError("المفتاح لا يحتوي مسافات — تأكّد من النسخ.")
    return value


def validate_label(raw: str | None) -> str | None:
    if raw is None:
        return None
    label = " ".join(raw.split())
    if not label or label in ("-", "تخطي", "skip"):
        return None
    if len(label) > LABEL_MAX:
        raise ProviderKeyError(f"الوسم أطول من {LABEL_MAX} حرفًا.")
    return label


@dataclass(frozen=True, slots=True)
class ProviderKey:
    key_id: str
    provider_name: str
    key_value: str
    label: str | None
    is_active: bool
    last_used_at: datetime | None
    failure_count: int
    created_at: datetime
    cooldown_until: datetime | None = None
    disabled_reason: str | None = None
    success_count: int = 0
    total_failures: int = 0

    @property
    def hint(self) -> str:
        return hint(self.key_value)

    @property
    def display(self) -> str:
        return f"{self.label} ({self.hint})" if self.label else self.hint

    def cooling(self, at: datetime | None = None) -> bool:
        if self.cooldown_until is None:
            return False
        return self.cooldown_until > (at or _now())

    @property
    def usable(self) -> bool:
        return self.is_active and not self.cooling()

    @property
    def state(self) -> str:
        if not self.is_active:
            return "disabled"
        if self.cooling():
            return "cooling"
        if self.failure_count:
            return "shaky"
        return "active"

    def public(self) -> dict[str, Any]:
        """بلا القيمة."""
        return {
            "key_id": self.key_id,
            "provider_name": self.provider_name,
            "label": self.label,
            "hint": self.hint,
            "is_active": self.is_active,
            "failure_count": self.failure_count,
            "success_count": self.success_count,
            "last_used_at": self.last_used_at.isoformat() if self.last_used_at else None,
            "created_at": self.created_at.isoformat(),
        }


class ProviderKeyRepository:
    def __init__(self, mongo: Mongo, cipher: ContentCipher | None = None) -> None:
        self._mongo = mongo
        self._cipher = cipher or ContentCipher(None)

    @property
    def _col(self) -> Any:
        return self._mongo.collection(COLLECTION)

    @property
    def encrypted(self) -> bool:
        return self._cipher.enabled

    async def ensure_indexes(self) -> None:
        await self._col.create_index(
            [("key_id", ASCENDING)], unique=True, name="key_id"
        )
        await self._col.create_index(
            [("provider_name", ASCENDING), ("fingerprint", ASCENDING)],
            unique=True,
            name="no_duplicate_key",
        )
        await self._col.create_index(
            [("provider_name", ASCENDING), ("is_active", ASCENDING)], name="lookup"
        )
        await self._col.create_index([("last_used_at", DESCENDING)], name="recent")

    def _decode(self, doc: Mapping[str, Any]) -> ProviderKey:
        sealed = doc.get("key_value")
        value = self._cipher.open(sealed) if isinstance(sealed, str) else ""
        return ProviderKey(
            key_id=str(doc["key_id"]),
            provider_name=str(doc.get("provider_name") or ""),
            key_value=value or "",
            label=doc.get("label") or None,
            is_active=bool(doc.get("is_active", True)),
            last_used_at=doc.get("last_used_at"),
            failure_count=int(doc.get("failure_count") or 0),
            created_at=doc.get("created_at") or _now(),
            cooldown_until=doc.get("cooldown_until"),
            disabled_reason=doc.get("disabled_reason") or None,
            success_count=int(doc.get("success_count") or 0),
            total_failures=int(doc.get("total_failures") or 0),
        )

    async def list_all(self) -> list[ProviderKey]:
        cursor = self._col.find({}).sort("created_at", ASCENDING)
        return [self._decode(doc) for doc in await cursor.to_list(length=None)]

    async def list_for(self, provider_name: str) -> list[ProviderKey]:
        cursor = self._col.find({"provider_name": provider_name}).sort(
            "created_at", ASCENDING
        )
        return [self._decode(doc) for doc in await cursor.to_list(length=None)]

    async def get(self, key_id: str) -> ProviderKey | None:
        doc = await self._col.find_one({"key_id": key_id})
        return self._decode(doc) if doc else None

    async def count_for(self, provider_name: str) -> int:
        return await self._col.count_documents({"provider_name": provider_name})

    async def has_fingerprint(self, provider_name: str, value: str) -> bool:
        doc = await self._col.find_one(
            {"provider_name": provider_name, "fingerprint": fingerprint(value)},
            {"_id": 1},
        )
        return doc is not None

    async def add(
        self, provider_name: str, key_value: str, label: str | None = None
    ) -> ProviderKey:
        provider_name = (provider_name or "").strip()
        if not provider_name:
            raise ProviderKeyError("اسم المزوّد مطلوب.")
        value = validate_value(key_value)
        label = validate_label(label)

        if await self.count_for(provider_name) >= MAX_PER_PROVIDER:
            raise ProviderKeyError(
                f"بلغتَ الحد ({MAX_PER_PROVIDER} مفتاحًا لهذا المزوّد)."
            )

        oid = ObjectId()
        doc = {
            "_id": oid,
            "key_id": make_id(oid),
            "provider_name": provider_name,
            "key_value": self._cipher.seal(value),
            "fingerprint": fingerprint(value),
            "label": label,
            "is_active": True,
            "last_used_at": None,
            "failure_count": 0,
            "cooldown_until": None,
            "disabled_reason": None,
            "success_count": 0,
            "total_failures": 0,
            "created_at": _now(),
        }
        try:
            await self._col.insert_one(doc)
        except DuplicateKeyError as exc:
            raise ProviderKeyError(
                "هذا المفتاح مضاف بالفعل لهذا المزوّد. "
                "تكراره يُبطل التوزيع بلا أن يظهر ذلك."
            ) from exc
        log.info("أُضيف مفتاح لـ %s (%s)", provider_name, hint(value))
        return self._decode(doc)

    async def mark_success(self, key_id: str, *, reset: bool) -> None:
        updates: dict[str, Any] = {"last_used_at": _now()}
        if reset:
            updates |= {
                "failure_count": 0,
                "cooldown_until": None,
                "disabled_reason": None,
            }
        try:
            await self._col.update_one(
                {"key_id": key_id},
                {"$set": updates, "$inc": {"success_count": 1}},
            )
        except Exception:
            log.debug("تعذّر تحديث نجاح المفتاح %s", key_id, exc_info=True)

    async def mark_failure(
        self,
        key_id: str,
        *,
        reason: str,
        threshold: int,
        cooldown: int | None = None,
    ) -> tuple[int, bool]:
        """يرجع (عدد الفشل المتتالي، هل عُطِّل الآن)."""
        updates: dict[str, Any] = {"last_used_at": _now(), "disabled_reason": reason}
        if cooldown:
            seconds = min(max(1, cooldown), MAX_COOLDOWN)
            updates["cooldown_until"] = _now() + timedelta(seconds=seconds)

        try:
            doc = await self._col.find_one_and_update(
                {"key_id": key_id},
                {"$inc": {"failure_count": 1, "total_failures": 1}, "$set": updates},
                return_document=True,
            )
        except Exception:
            log.warning("تعذّر تسجيل فشل المفتاح %s", key_id, exc_info=True)
            return 0, False
        if not doc:
            return 0, False

        count = int(doc.get("failure_count") or 0)
        if count < threshold or not doc.get("is_active", True):
            return count, False

        await self._col.update_one(
            {"key_id": key_id},
            {"$set": {"is_active": False, "cooldown_until": None}},
        )
        log.warning(
            "عُطِّل المفتاح %s لـ %s بعد %s فشل: %s",
            key_id, doc.get("provider_name"), count, reason,
        )
        return count, True

    async def set_active(self, key_id: str, active: bool) -> ProviderKey | None:
        updates: dict[str, Any] = {"is_active": active}
        if active:
            # بلا تصفير، المفتاح يُعطَّل من أول فشل بعد إعادة التفعيل
            updates |= {
                "failure_count": 0,
                "cooldown_until": None,
                "disabled_reason": None,
            }
        await self._col.update_one({"key_id": key_id}, {"$set": updates})
        return await self.get(key_id)

    async def delete(self, key_id: str) -> bool:
        result = await self._col.delete_one({"key_id": key_id})
        return bool(result.deleted_count)

    async def delete_for_provider(self, provider_name: str) -> int:
        result = await self._col.delete_many({"provider_name": provider_name})
        if result.deleted_count:
            log.info("حُذفت %s مفاتيح لـ %s", result.deleted_count, provider_name)
        return result.deleted_count

    async def delete_all(self) -> int:
        result = await self._col.delete_many({})
        log.warning("حُذفت كل مفاتيح المزوّدات (%s)", result.deleted_count)
        return result.deleted_count

    async def export(self) -> list[dict[str, Any]]:
        """بلا قيم: التصدير يمرّ بتليجرام."""
        return [key.public() for key in await self.list_all()]
