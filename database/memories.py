"""متجهات الذاكرة الدلالية.

مسارا بحث:
  1) Atlas Vector Search عبر $vectorSearch إن ضُبط MEMORY_VECTOR_INDEX.
  2) حساب تشابه جيبي محلي على آخر N مستندًا — يعمل على أي Mongo.
المسار الثاني افتراضي كي لا يعتمد التشغيل على ميزة سحابية.
"""
from __future__ import annotations

import logging
import math
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Mapping, Sequence

from pymongo import ASCENDING, DESCENDING

from database.mongo import Mongo

log = logging.getLogger(__name__)

COLLECTION = "memories"


def _now() -> datetime:
    return datetime.now(timezone.utc)


def cosine(left: Sequence[float], right: Sequence[float]) -> float:
    if not left or not right or len(left) != len(right):
        return 0.0
    dot = 0.0
    norm_left = 0.0
    norm_right = 0.0
    for a, b in zip(left, right):
        dot += a * b
        norm_left += a * a
        norm_right += b * b
    if norm_left <= 0.0 or norm_right <= 0.0:
        return 0.0
    return dot / math.sqrt(norm_left * norm_right)


@dataclass(frozen=True, slots=True)
class Memory:
    role: str
    text: str
    created_at: datetime
    score: float = 0.0

    @property
    def preview(self) -> str:
        flat = " ".join(self.text.split())
        return flat if len(flat) <= 70 else flat[:69] + "…"

    def public(self) -> dict[str, Any]:
        return {
            "role": self.role,
            "text": self.text,
            "created_at": self.created_at.isoformat(),
        }


class MemoryRepository:
    def __init__(
        self,
        mongo: Mongo,
        *,
        vector_index: str | None = None,
        max_scan: int = 1500,
    ) -> None:
        self._mongo = mongo
        self._index = vector_index
        self._max_scan = max(100, max_scan)
        self._vector_search_failed = False

    @property
    def _col(self) -> Any:
        return self._mongo.collection(COLLECTION)

    @property
    def uses_atlas(self) -> bool:
        return bool(self._index) and not self._vector_search_failed

    async def ensure_indexes(self) -> None:
        await self._col.create_index([("created_at", DESCENDING)], name="recent")
        await self._col.create_index([("role", ASCENDING)], name="role")

    async def add(self, role: str, text: str, vector: Sequence[float]) -> None:
        if not text.strip() or not vector:
            return
        await self._col.insert_one(
            {
                "role": role,
                "text": text,
                "vector": [float(value) for value in vector],
                "dim": len(vector),
                "created_at": _now(),
            }
        )

    async def add_many(self, rows: Sequence[tuple[str, str, Sequence[float]]]) -> int:
        docs = [
            {
                "role": role,
                "text": text,
                "vector": [float(value) for value in vector],
                "dim": len(vector),
                "created_at": _now(),
            }
            for role, text, vector in rows
            if text.strip() and vector
        ]
        if not docs:
            return 0
        await self._col.insert_many(docs)
        return len(docs)

    async def search(
        self, vector: Sequence[float], *, limit: int, min_score: float
    ) -> list[Memory]:
        if not vector:
            return []
        if self.uses_atlas:
            found = await self._atlas_search(vector, limit=limit, min_score=min_score)
            if found is not None:
                return found
        return await self._local_search(vector, limit=limit, min_score=min_score)

    async def _atlas_search(
        self, vector: Sequence[float], *, limit: int, min_score: float
    ) -> list[Memory] | None:
        pipeline = [
            {
                "$vectorSearch": {
                    "index": self._index,
                    "path": "vector",
                    "queryVector": [float(value) for value in vector],
                    "numCandidates": max(50, limit * 20),
                    "limit": limit,
                }
            },
            {
                "$project": {
                    "role": 1,
                    "text": 1,
                    "created_at": 1,
                    "score": {"$meta": "vectorSearchScore"},
                }
            },
        ]
        try:
            docs = await self._col.aggregate(pipeline).to_list(length=limit)
        except Exception as exc:
            # فهرس غير موجود أو Mongo غير Atlas: نسقط للحساب المحلي مرة واحدة
            self._vector_search_failed = True
            log.warning(
                "تعذّر $vectorSearch (%s) — استُخدم الحساب المحلي بدلًا منه", exc
            )
            return None
        return [
            Memory(
                role=str(doc.get("role") or ""),
                text=str(doc.get("text") or ""),
                created_at=doc.get("created_at") or _now(),
                score=float(doc.get("score") or 0.0),
            )
            for doc in docs
            if float(doc.get("score") or 0.0) >= min_score
        ]

    async def _local_search(
        self, vector: Sequence[float], *, limit: int, min_score: float
    ) -> list[Memory]:
        cursor = (
            self._col.find({"dim": len(vector)})
            .sort("created_at", DESCENDING)
            .limit(self._max_scan)
        )
        scored: list[Memory] = []
        for doc in await cursor.to_list(length=self._max_scan):
            score = cosine(vector, doc.get("vector") or [])
            if score < min_score:
                continue
            scored.append(
                Memory(
                    role=str(doc.get("role") or ""),
                    text=str(doc.get("text") or ""),
                    created_at=doc.get("created_at") or _now(),
                    score=score,
                )
            )
        scored.sort(key=lambda item: item.score, reverse=True)
        return scored[:limit]

    async def count(self) -> int:
        return await self._col.count_documents({})

    async def latest(self, limit: int = 10) -> list[Memory]:
        cursor = self._col.find({}).sort("created_at", DESCENDING).limit(limit)
        return [
            Memory(
                role=str(doc.get("role") or ""),
                text=str(doc.get("text") or ""),
                created_at=doc.get("created_at") or _now(),
            )
            for doc in await cursor.to_list(length=limit)
        ]

    async def clear(self) -> int:
        result = await self._col.delete_many({})
        log.info("مُسحت الذاكرة (%s عنصرًا)", result.deleted_count)
        return result.deleted_count

    async def export(self) -> list[dict[str, Any]]:
        """بلا المتجهات: حجمها هائل وبلا قيمة للقارئ البشري."""
        cursor = self._col.find({}, {"vector": 0}).sort("created_at", ASCENDING)
        return [
            {
                "role": str(doc.get("role") or ""),
                "text": str(doc.get("text") or ""),
                "created_at": (doc.get("created_at") or _now()).isoformat(),
            }
            for doc in await cursor.to_list(length=None)
        ]
