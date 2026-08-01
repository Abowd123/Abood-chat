"""الشخصيات. تُنشأ «مساعد عام» تلقائيًا ولا يمكن حذفها."""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Mapping

from bson import ObjectId
from pymongo import ASCENDING
from pymongo.errors import DuplicateKeyError

from database.mongo import Mongo

log = logging.getLogger(__name__)

COLLECTION = "personas"
ID_LENGTH = 12
MAX_PERSONAS = 40

NAME_MIN, NAME_MAX = 2, 40
PROMPT_MIN, PROMPT_MAX = 10, 4000

DEFAULT_SLUG = "general-assistant"
DEFAULT_NAME = "مساعد عام"
DEFAULT_PROMPT = (
    "أنت مساعد شخصي ذكي ومباشر. تجيب بدقّة وبلا حشو، "
    "وتردّ بلغة المستخدم نفسها. إن لم تعرف شيئًا فقل ذلك صراحةً "
    "بدل التخمين. عند كتابة كود، اكتبه كاملًا وقابلًا للتشغيل."
)


class PersonaError(ValueError):
    """خطأ إدخال، رسالته صالحة للعرض."""


def _now() -> datetime:
    return datetime.now(timezone.utc)


def validate_name(raw: str) -> str:
    name = " ".join((raw or "").split())
    if not (NAME_MIN <= len(name) <= NAME_MAX):
        raise PersonaError(f"الاسم يجب أن يكون بين {NAME_MIN} و{NAME_MAX} حرفًا.")
    return name


def validate_prompt(raw: str) -> str:
    prompt = (raw or "").strip()
    if not (PROMPT_MIN <= len(prompt) <= PROMPT_MAX):
        raise PersonaError(
            f"التعليمات يجب أن تكون بين {PROMPT_MIN} و{PROMPT_MAX} حرفًا."
        )
    return prompt


@dataclass(frozen=True, slots=True)
class Persona:
    persona_id: str
    name: str
    prompt: str
    built_in: bool
    created_at: datetime

    @property
    def preview(self) -> str:
        flat = " ".join(self.prompt.split())
        return flat if len(flat) <= 90 else flat[:89] + "…"

    def public(self) -> dict[str, Any]:
        return {
            "persona_id": self.persona_id,
            "name": self.name,
            "prompt": self.prompt,
            "built_in": self.built_in,
            "created_at": self.created_at.isoformat(),
        }

    @classmethod
    def from_doc(cls, raw: Mapping[str, Any]) -> "Persona":
        return cls(
            persona_id=str(raw.get("persona_id") or ""),
            name=str(raw.get("name") or ""),
            prompt=str(raw.get("prompt") or ""),
            built_in=bool(raw.get("built_in")),
            created_at=raw.get("created_at") or _now(),
        )


class PersonaRepository:
    def __init__(self, mongo: Mongo) -> None:
        self._mongo = mongo

    @property
    def _col(self) -> Any:
        return self._mongo.collection(COLLECTION)

    async def ensure_indexes(self) -> None:
        await self._col.create_index(
            [("persona_id", ASCENDING)], unique=True, name="persona_id"
        )
        await self._col.create_index(
            [("name_lower", ASCENDING)], unique=True, name="name_unique"
        )
        await self._col.create_index([("created_at", ASCENDING)], name="created")

    async def ensure_default(self) -> Persona:
        """يُنشئ «مساعد عام» مرة واحدة. لا يكتب فوق تعديلاتك عليه."""
        existing = await self._col.find_one({"persona_id": DEFAULT_SLUG})
        if existing:
            return Persona.from_doc(existing)
        doc = {
            "persona_id": DEFAULT_SLUG,
            "name": DEFAULT_NAME,
            "name_lower": DEFAULT_NAME.casefold(),
            "prompt": DEFAULT_PROMPT,
            "built_in": True,
            "created_at": _now(),
        }
        try:
            await self._col.insert_one(doc)
            log.info("أُنشئت الشخصية الافتراضية")
        except DuplicateKeyError:
            pass
        return Persona.from_doc(doc)

    async def count(self) -> int:
        return await self._col.count_documents({})

    async def list_all(self) -> list[Persona]:
        cursor = self._col.find({}).sort([("built_in", -1), ("created_at", ASCENDING)])
        return [Persona.from_doc(doc) for doc in await cursor.to_list(length=None)]

    async def get(self, persona_id: str) -> Persona | None:
        doc = await self._col.find_one({"persona_id": persona_id})
        return Persona.from_doc(doc) if doc else None

    async def resolve(self, persona_id: str) -> Persona:
        """يسقط إلى الافتراضية إن كانت المُختارة محذوفة."""
        if persona_id:
            found = await self.get(persona_id)
            if found is not None:
                return found
        return await self.ensure_default()

    async def create(self, name: str, prompt: str) -> Persona:
        name = validate_name(name)
        prompt = validate_prompt(prompt)
        if await self.count() >= MAX_PERSONAS:
            raise PersonaError(f"بلغتَ الحد الأقصى ({MAX_PERSONAS} شخصية).")

        doc = {
            "persona_id": str(ObjectId())[-ID_LENGTH:],
            "name": name,
            "name_lower": name.casefold(),
            "prompt": prompt,
            "built_in": False,
            "created_at": _now(),
        }
        try:
            await self._col.insert_one(doc)
        except DuplicateKeyError as exc:
            raise PersonaError(f"توجد شخصية بالاسم «{name}» بالفعل.") from exc
        log.info("أُضيفت شخصية: %s", name)
        return Persona.from_doc(doc)

    async def delete(self, persona_id: str) -> bool:
        persona = await self.get(persona_id)
        if persona is None:
            return False
        if persona.built_in:
            raise PersonaError("الشخصية الافتراضية لا تُحذف.")
        result = await self._col.delete_one({"persona_id": persona_id})
        return bool(result.deleted_count)

    async def delete_all(self) -> int:
        """يحذف المخصّصة فقط، فيبقى للبوت شخصية يعمل بها."""
        result = await self._col.delete_many({"built_in": {"$ne": True}})
        return result.deleted_count

    async def export(self) -> list[dict[str, Any]]:
        return [persona.public() for persona in await self.list_all()]
