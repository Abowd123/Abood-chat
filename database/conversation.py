"""سجلّ المحادثة: Mongo مصدر الحقيقة، وRedis كاش آخر 20 رسالة.

لا `user_id` في أي مستند: البوت لمالك واحد ومحادثة واحدة.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Mapping, Sequence

from pymongo import ASCENDING, DESCENDING

from database.mongo import Mongo
from database.redis_client import RedisClient

log = logging.getLogger(__name__)

COLLECTION = "messages"
CACHE_KEY = "conversation:recent"
CACHE_TTL = 86400
CACHE_KEEP = 20


def _now() -> datetime:
    return datetime.now(timezone.utc)


@dataclass(frozen=True, slots=True)
class Message:
    role: str
    content: str
    created_at: datetime
    model_used: str = ""
    media_type: str = ""
    tokens_in: int = 0
    tokens_out: int = 0

    def to_cache(self) -> dict[str, Any]:
        return {
            "role": self.role,
            "content": self.content,
            "at": self.created_at.timestamp(),
            "model": self.model_used,
            "media": self.media_type,
        }

    @classmethod
    def from_cache(cls, raw: Mapping[str, Any]) -> "Message":
        return cls(
            role=str(raw.get("role") or "user"),
            content=str(raw.get("content") or ""),
            created_at=datetime.fromtimestamp(
                float(raw.get("at") or 0), tz=timezone.utc
            ),
            model_used=str(raw.get("model") or ""),
            media_type=str(raw.get("media") or ""),
        )

    @classmethod
    def from_doc(cls, raw: Mapping[str, Any]) -> "Message":
        return cls(
            role=str(raw.get("role") or "user"),
            content=str(raw.get("content") or ""),
            created_at=raw.get("created_at") or _now(),
            model_used=str(raw.get("model_used") or ""),
            media_type=str(raw.get("media_type") or ""),
            tokens_in=int(raw.get("tokens_in") or 0),
            tokens_out=int(raw.get("tokens_out") or 0),
        )

    def public(self) -> dict[str, Any]:
        return {
            "role": self.role,
            "content": self.content,
            "created_at": self.created_at.isoformat(),
            "model_used": self.model_used,
            "media_type": self.media_type,
            "tokens_in": self.tokens_in,
            "tokens_out": self.tokens_out,
        }


class ConversationRepository:
    def __init__(
        self, mongo: Mongo, redis: RedisClient, *, window: int = CACHE_KEEP
    ) -> None:
        self._mongo = mongo
        self._redis = redis
        self._window = max(2, window)

    @property
    def _col(self) -> Any:
        return self._mongo.collection(COLLECTION)

    async def ensure_indexes(self) -> None:
        await self._col.create_index([("created_at", ASCENDING)], name="chronological")

    async def append(
        self,
        role: str,
        content: str,
        *,
        model_used: str = "",
        media_type: str = "",
        tokens_in: int = 0,
        tokens_out: int = 0,
    ) -> Message:
        message = Message(
            role=role,
            content=content,
            created_at=_now(),
            model_used=model_used,
            media_type=media_type,
            tokens_in=tokens_in,
            tokens_out=tokens_out,
        )
        await self._col.insert_one(
            {
                "role": message.role,
                "content": message.content,
                "created_at": message.created_at,
                "model_used": message.model_used,
                "media_type": message.media_type,
                "tokens_in": message.tokens_in,
                "tokens_out": message.tokens_out,
            }
        )
        await self._redis.push_trim(
            CACHE_KEY,
            json.dumps(message.to_cache(), ensure_ascii=False),
            keep=self._window,
            ttl=CACHE_TTL,
        )
        return message

    async def recent(self, limit: int | None = None) -> list[Message]:
        """يقرأ من الكاش، ويعود إلى Mongo عند غيابه ثم يُعيد بناءه."""
        limit = limit or self._window
        cached = await self._redis.list_range(CACHE_KEY)
        if cached:
            messages = []
            for row in cached:
                try:
                    messages.append(Message.from_cache(json.loads(row)))
                except (ValueError, TypeError):
                    continue
            if messages:
                return messages[-limit:]

        cursor = self._col.find({}).sort("created_at", DESCENDING).limit(limit)
        docs = await cursor.to_list(length=limit)
        messages = [Message.from_doc(doc) for doc in reversed(docs)]
        await self._rebuild_cache(messages)
        return messages

    async def _rebuild_cache(self, messages: Sequence[Message]) -> None:
        if not messages or self._redis.client is None:
            return
        await self._redis.delete(CACHE_KEY)
        for message in messages[-self._window :]:
            await self._redis.push_trim(
                CACHE_KEY,
                json.dumps(message.to_cache(), ensure_ascii=False),
                keep=self._window,
                ttl=CACHE_TTL,
            )

    async def count(self) -> int:
        return await self._col.count_documents({})

    async def all_messages(self) -> list[Message]:
        cursor = self._col.find({}).sort("created_at", ASCENDING)
        return [Message.from_doc(doc) for doc in await cursor.to_list(length=None)]

    async def clear(self) -> int:
        result = await self._col.delete_many({})
        await self._redis.delete(CACHE_KEY)
        log.info("أُعيد تعيين المحادثة (%s رسالة)", result.deleted_count)
        return result.deleted_count

    async def export(self) -> list[dict[str, Any]]:
        return [message.public() for message in await self.all_messages()]
