"""حساب المتجهات عبر أي نقطة نهاية متوافقة مع OpenAI.

معطّل بصمت إن لم يُضبط مزوّد: الذاكرة ميزة إضافية، وغيابها لا يمنع
المحادثة. `available` يُخبر بقية الطبقات بلا استثناءات.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Sequence

log = logging.getLogger(__name__)

BATCH_SIZE = 32
TIMEOUT = 60.0
MAX_CHARS = 8000   # قصّ آمن: النموذج له حدّ توكنات


class EmbeddingService:
    def __init__(
        self,
        *,
        base_url: str,
        api_key: str | None,
        model: str,
        enabled: bool = True,
    ) -> None:
        self._base_url = base_url
        self._api_key = api_key
        self._model = model
        self._enabled = enabled and bool(base_url and model)
        self._client: object | None = None
        self._failed = False

    @property
    def available(self) -> bool:
        return self._enabled and not self._failed

    @property
    def model(self) -> str:
        return self._model

    def _ensure_client(self) -> object | None:
        if self._client is not None:
            return self._client
        if not self._enabled:
            return None
        try:
            import openai

            self._client = openai.AsyncOpenAI(
                api_key=self._api_key or "not-required",
                base_url=self._base_url,
                timeout=TIMEOUT,
                max_retries=1,
            )
        except Exception as exc:
            log.warning("تعذّرت تهيئة مزوّد المتجهات: %s", exc)
            self._failed = True
            return None
        return self._client

    async def embed(self, text: str) -> list[float]:
        vectors = await self.embed_many([text])
        return vectors[0] if vectors else []

    async def embed_many(self, texts: Sequence[str]) -> list[list[float]]:
        cleaned = [item.strip()[:MAX_CHARS] for item in texts if item and item.strip()]
        if not cleaned or not self.available:
            return []
        client = self._ensure_client()
        if client is None:
            return []

        results: list[list[float]] = []
        for start in range(0, len(cleaned), BATCH_SIZE):
            chunk = cleaned[start : start + BATCH_SIZE]
            try:
                response = await asyncio.wait_for(
                    client.embeddings.create(model=self._model, input=chunk),
                    timeout=TIMEOUT,
                )
            except Exception as exc:
                # فشل الذاكرة لا يُسقط الرد: نسجّل ونكمل بلا متجهات
                log.warning("تعذّر حساب المتجهات: %s", exc)
                return results
            for row in getattr(response, "data", None) or []:
                vector = getattr(row, "embedding", None) or []
                results.append([float(value) for value in vector])
        return results

    async def close(self) -> None:
        client = self._client
        if client is None:
            return
        closer = getattr(client, "close", None)
        if closer is None:
            return
        try:
            await closer()
        except Exception:
            log.debug("تعذّر إغلاق عميل المتجهات", exc_info=True)
