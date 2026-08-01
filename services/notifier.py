"""تنبيهات المالك: مسار واحد لكل ما يجب أن تعرفه فورًا.

الكتم بالوسم (tag) مقصود: عطل متكرّر لا يجب أن يُغرق محادثتك.
"""
from __future__ import annotations

import asyncio
import logging
import time

log = logging.getLogger(__name__)

DEFAULT_COOLDOWN = 300
MAX_LENGTH = 3500


class Notifier:
    def __init__(self, client, owner_id: int) -> None:
        self._client = client
        self._owner_id = owner_id
        self._last: dict[str, float] = {}
        self._lock = asyncio.Lock()
        self.enabled = True

    async def send(
        self, text: str, *, tag: str | None = None, cooldown: int = DEFAULT_COOLDOWN
    ) -> bool:
        if not self.enabled or not text.strip():
            return False

        if tag is not None and cooldown > 0:
            async with self._lock:
                now = time.monotonic()
                previous = self._last.get(tag)
                if previous is not None and now - previous < cooldown:
                    return False
                self._last[tag] = now

        payload = text if len(text) <= MAX_LENGTH else text[: MAX_LENGTH - 1] + "…"
        try:
            await self._client.send_message(
                self._owner_id, payload, disable_web_page_preview=True
            )
            return True
        except Exception as exc:
            # لا نستخدم log.error هنا: قد يكون مصدر التنبيه هو السجلّ نفسه
            log.warning("تعذّر إرسال التنبيه: %s", exc)
            return False
