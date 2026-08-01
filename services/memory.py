"""ذاكرة RAG: فهرسة كل رسالة، واسترجاع ما يشبه السؤال الحالي."""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Sequence

from database.memories import Memory, MemoryRepository
from services.embeddings import EmbeddingService

log = logging.getLogger(__name__)

MIN_INDEX_CHARS = 12    # «نعم» و«تمام» لا تستحق متجهًا
MAX_BLOCK_CHARS = 1200


@dataclass(frozen=True, slots=True)
class MemoryStatus:
    available: bool
    enabled: bool
    count: int
    backend: str

    @property
    def active(self) -> bool:
        return self.available and self.enabled


class MemoryService:
    def __init__(
        self,
        repo: MemoryRepository,
        embeddings: EmbeddingService,
        *,
        top_k: int = 5,
        min_score: float = 0.25,
    ) -> None:
        self._repo = repo
        self._embeddings = embeddings
        self._top_k = max(1, top_k)
        self._min_score = min_score

    @property
    def available(self) -> bool:
        return self._embeddings.available

    @property
    def backend(self) -> str:
        return "Atlas Vector Search" if self._repo.uses_atlas else "حساب محلي"

    async def status(self, *, enabled: bool) -> MemoryStatus:
        return MemoryStatus(
            available=self.available,
            enabled=enabled,
            count=await self._repo.count(),
            backend=self.backend,
        )

    # ───────────── الفهرسة ─────────────

    async def remember(self, role: str, text: str) -> bool:
        if not self.available:
            return False
        cleaned = (text or "").strip()
        if len(cleaned) < MIN_INDEX_CHARS:
            return False
        vector = await self._embeddings.embed(cleaned)
        if not vector:
            return False
        await self._repo.add(role, cleaned[:MAX_BLOCK_CHARS], vector)
        return True

    def remember_later(self, role: str, text: str) -> None:
        """فهرسة في الخلفية: لا تؤخّر ظهور الرد للمستخدم."""
        if not self.available:
            return
        task = asyncio.create_task(self._safe_remember(role, text))
        task.add_done_callback(lambda _: None)

    async def _safe_remember(self, role: str, text: str) -> None:
        try:
            await self.remember(role, text)
        except Exception:
            log.warning("تعذّرت الفهرسة في الخلفية", exc_info=True)

    async def reindex(self, rows: Sequence[tuple[str, str]]) -> int:
        """يعيد بناء الفهرس من سجلّ المحادثة."""
        if not self.available:
            return 0
        candidates = [
            (role, text.strip()[:MAX_BLOCK_CHARS])
            for role, text in rows
            if text and len(text.strip()) >= MIN_INDEX_CHARS
        ]
        if not candidates:
            return 0
        vectors = await self._embeddings.embed_many(
            [text for _, text in candidates]
        )
        if not vectors:
            return 0
        pairs = [
            (role, text, vector)
            for (role, text), vector in zip(candidates, vectors)
        ]
        return await self._repo.add_many(pairs)

    # ───────────── الاسترجاع ─────────────

    async def retrieve_relevant_memories(
        self, query: str, *, limit: int | None = None
    ) -> list[Memory]:
        if not self.available:
            return []
        cleaned = (query or "").strip()
        if len(cleaned) < 3:
            return []
        vector = await self._embeddings.embed(cleaned)
        if not vector:
            return []
        try:
            return await self._repo.search(
                vector, limit=limit or self._top_k, min_score=self._min_score
            )
        except Exception:
            log.warning("تعذّر استرجاع الذاكرة", exc_info=True)
            return []

    @staticmethod
    def as_context_block(memories: Sequence[Memory]) -> str:
        """نصّ يُدمج في رسالة النظام."""
        if not memories:
            return ""
        lines = ["من محادثات سابقة (قد تكون ذات صلة):"]
        for item in memories:
            speaker = "أنت" if item.role == "assistant" else "المستخدم"
            lines.append(f"- [{speaker}] {item.text[:300]}")
        return "\n".join(lines)

    async def latest(self, limit: int = 10) -> list[Memory]:
        return await self._repo.latest(limit)

    async def forget_all(self) -> int:
        return await self._repo.clear()
