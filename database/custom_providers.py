"""المزوّدات المخصّصة المتوافقة مع OpenAI API (Ollama, LM Studio, vLLM...)."""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Mapping
from urllib.parse import urlparse

from bson import ObjectId
from pymongo import ASCENDING
from pymongo.errors import DuplicateKeyError

from database.mongo import Mongo
from services.crypto import ContentCipher

log = logging.getLogger(__name__)

COLLECTION = "custom_providers"
KEY_PREFIX = "cx-"
KEY_LENGTH = 12
MAX_PROVIDERS = 20

NAME_MIN, NAME_MAX = 2, 40
MODEL_MAX = 120
URL_MAX = 300

LOCAL_HOSTS = frozenset(
    {"localhost", "127.0.0.1", "::1", "0.0.0.0", "host.docker.internal"}
)
PRIVATE_NET = re.compile(r"^(10\.|192\.168\.|172\.(1[6-9]|2\d|3[01])\.)")


class CustomProviderError(ValueError):
    """خطأ إدخال، رسالته صالحة للعرض."""


def _now() -> datetime:
    return datetime.now(timezone.utc)


def make_key(oid: ObjectId) -> str:
    return f"{KEY_PREFIX}{str(oid)[-KEY_LENGTH:]}"


def is_custom_provider(name: str) -> bool:
    return isinstance(name, str) and name.startswith(KEY_PREFIX)


def validate_name(raw: str) -> str:
    name = " ".join((raw or "").split())
    if not (NAME_MIN <= len(name) <= NAME_MAX):
        raise CustomProviderError(
            f"الاسم يجب أن يكون بين {NAME_MIN} و{NAME_MAX} حرفًا."
        )
    return name


def validate_base_url(raw: str) -> str:
    """يفرض https لغير المحلي: المفتاح يُرسل في نفس الطلب."""
    url = (raw or "").strip().rstrip("/")
    if not url or len(url) > URL_MAX:
        raise CustomProviderError("عنوان غير صالح.")
    if "://" not in url:
        url = f"http://{url}"

    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        raise CustomProviderError("العنوان يجب أن يبدأ بـ http:// أو https://")
    host = (parsed.hostname or "").lower()
    if not host:
        raise CustomProviderError("العنوان بلا مضيف.")

    local = (
        host in LOCAL_HOSTS
        or bool(PRIVATE_NET.match(host))
        or host.endswith(".local")
    )
    if parsed.scheme == "http" and not local:
        raise CustomProviderError(
            "http الصريح مسموح للمضيفات المحلية فقط — "
            "مفتاحك سيمرّ بلا تشفير. استخدم https."
        )
    if not parsed.path.rstrip("/").endswith("/v1"):
        log.info("عنوان بلا /v1: %s — قد يرجع 404", url)
    return url


def validate_model_name(raw: str) -> str:
    model = (raw or "").strip()
    if not model or len(model) > MODEL_MAX or "\n" in model:
        raise CustomProviderError("اسم موديل غير صالح.")
    return model


def validate_api_key(raw: str | None) -> str | None:
    if raw is None:
        return None
    key = raw.strip()
    if not key or key in ("-", "تخطي", "skip", "لا"):
        return None
    if len(key) > 400:
        raise CustomProviderError("المفتاح أطول من المعقول.")
    return key


@dataclass(frozen=True, slots=True)
class CustomProviderDoc:
    key: str
    name: str
    base_url: str
    model_name: str
    api_key: str | None
    created_at: datetime

    @property
    def key_hint(self) -> str:
        if not self.api_key:
            return "بلا مفتاح"
        return f"…{self.api_key[-4:]}"

    def public(self) -> dict[str, Any]:
        """بلا المفتاح: صالح للتصدير عبر تليجرام."""
        return {
            "key": self.key,
            "name": self.name,
            "base_url": self.base_url,
            "model_name": self.model_name,
            "has_key": self.api_key is not None,
            "created_at": self.created_at.isoformat(),
        }


class CustomProviderRepository:
    def __init__(self, mongo: Mongo, cipher: ContentCipher | None = None) -> None:
        self._mongo = mongo
        self._cipher = cipher or ContentCipher(None)

    @property
    def _col(self) -> Any:
        return self._mongo.collection(COLLECTION)

    async def ensure_indexes(self) -> None:
        await self._col.create_index([("key", ASCENDING)], unique=True, name="key")
        await self._col.create_index(
            [("name_lower", ASCENDING)], unique=True, name="name_unique"
        )

    def _decode(self, doc: Mapping[str, Any]) -> CustomProviderDoc:
        sealed = doc.get("api_key")
        api_key = self._cipher.open(sealed) if isinstance(sealed, str) and sealed else None
        return CustomProviderDoc(
            key=str(doc["key"]),
            name=str(doc.get("name") or ""),
            base_url=str(doc.get("base_url") or ""),
            model_name=str(doc.get("model_name") or ""),
            api_key=api_key or None,
            created_at=doc.get("created_at") or _now(),
        )

    async def count(self) -> int:
        return await self._col.count_documents({})

    async def list_all(self) -> list[CustomProviderDoc]:
        cursor = self._col.find({}).sort("created_at", ASCENDING)
        return [self._decode(doc) for doc in await cursor.to_list(length=None)]

    async def get(self, key: str) -> CustomProviderDoc | None:
        doc = await self._col.find_one({"key": key})
        return self._decode(doc) if doc else None

    async def create(
        self,
        name: str,
        base_url: str,
        model_name: str,
        api_key: str | None = None,
    ) -> CustomProviderDoc:
        name = validate_name(name)
        base_url = validate_base_url(base_url)
        model_name = validate_model_name(model_name)
        api_key = validate_api_key(api_key)

        if await self.count() >= MAX_PROVIDERS:
            raise CustomProviderError(
                f"بلغتَ الحد الأقصى ({MAX_PROVIDERS} مزوّدًا). احذف واحدًا أولًا."
            )

        oid = ObjectId()
        doc = {
            "_id": oid,
            "key": make_key(oid),
            "name": name,
            "name_lower": name.casefold(),
            "base_url": base_url,
            "model_name": model_name,
            # التشفير هنا لا في الطبقة الأعلى: لا نسخة صريحة تصل Mongo
            "api_key": self._cipher.seal(api_key) if api_key else None,
            "created_at": _now(),
        }
        try:
            await self._col.insert_one(doc)
        except DuplicateKeyError as exc:
            raise CustomProviderError(f"يوجد مزوّد بالاسم «{name}» بالفعل.") from exc
        log.info("أُضيف مزوّد مخصّص: %s (%s)", name, doc["key"])
        return self._decode(doc)

    async def delete(self, key: str) -> bool:
        result = await self._col.delete_one({"key": key})
        if result.deleted_count:
            log.info("حُذف مزوّد مخصّص: %s", key)
        return bool(result.deleted_count)

    async def delete_all(self) -> int:
        result = await self._col.delete_many({})
        return result.deleted_count

    async def export(self) -> list[dict[str, Any]]:
        return [doc.public() for doc in await self.list_all()]
