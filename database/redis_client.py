"""كاش Redis متسامح مع الفشل.

الكاش ليس مصدر حقيقة: انقطاع Redis يجب أن يُبطئ البوت لا أن يُسقطه.
لذلك كل عملية قراءة/كتابة تُمسك استثناءها وتُسجّله مرة واحدة.
"""
from __future__ import annotations

import json
import logging
from typing import Any

import redis.asyncio as aioredis

log = logging.getLogger(__name__)


class RedisClient:
    def __init__(self, url: str) -> None:
        self._url = url
        self._client: aioredis.Redis | None = None
        self._warned = False

    async def connect(self) -> bool:
        try:
            self._client = aioredis.from_url(
                self._url,
                encoding="utf-8",
                decode_responses=True,
                socket_connect_timeout=5,
                socket_timeout=5,
            )
            await self._client.ping()
            log.info("Redis متصل")
            return True
        except Exception as exc:
            log.warning("تعذّر الاتصال بـ Redis (%s) — يعمل البوت بلا كاش", exc)
            self._client = None
            return False

    @property
    def available(self) -> bool:
        return self._client is not None

    @property
    def client(self) -> aioredis.Redis | None:
        return self._client

    def _degrade(self, action: str, exc: Exception) -> None:
        if not self._warned:
            log.warning("عطل في Redis أثناء %s (%s) — الكاش معطّل مؤقتًا", action, exc)
            self._warned = True

    # ---- نصوص ----

    async def get(self, key: str) -> str | None:
        if self._client is None:
            return None
        try:
            return await self._client.get(key)
        except Exception as exc:
            self._degrade("القراءة", exc)
            return None

    async def set(self, key: str, value: str, *, ttl: int | None = None) -> None:
        if self._client is None:
            return
        try:
            if ttl:
                await self._client.setex(key, ttl, value)
            else:
                await self._client.set(key, value)
        except Exception as exc:
            self._degrade("الكتابة", exc)

    async def delete(self, *keys: str) -> None:
        if self._client is None or not keys:
            return
        try:
            await self._client.delete(*keys)
        except Exception as exc:
            self._degrade("الحذف", exc)

    # ---- JSON ----

    async def get_json(self, key: str) -> Any | None:
        raw = await self.get(key)
        if raw is None:
            return None
        try:
            return json.loads(raw)
        except (ValueError, TypeError):
            log.warning("قيمة JSON تالفة في %s — تُحذف", key)
            await self.delete(key)
            return None

    async def set_json(self, key: str, value: Any, *, ttl: int | None = None) -> None:
        try:
            payload = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
        except (TypeError, ValueError) as exc:
            log.warning("تعذّر تحويل القيمة إلى JSON لـ %s: %s", key, exc)
            return
        await self.set(key, payload, ttl=ttl)

    # ---- قوائم (كاش المحادثة) ----

    async def push_trim(self, key: str, value: str, *, keep: int, ttl: int) -> None:
        """يُضيف إلى نهاية القائمة ويقصّها على آخر `keep` عنصرًا."""
        if self._client is None:
            return
        try:
            pipe = self._client.pipeline()
            pipe.rpush(key, value)
            pipe.ltrim(key, -keep, -1)
            pipe.expire(key, ttl)
            await pipe.execute()
        except Exception as exc:
            self._degrade("الإضافة للقائمة", exc)

    async def list_range(self, key: str) -> list[str]:
        if self._client is None:
            return []
        try:
            return await self._client.lrange(key, 0, -1)
        except Exception as exc:
            self._degrade("قراءة القائمة", exc)
            return []

    async def close(self) -> None:
        if self._client is not None:
            try:
                await self._client.close()
            except Exception:
                log.debug("تعذّر إغلاق Redis", exc_info=True)
            self._client = None
