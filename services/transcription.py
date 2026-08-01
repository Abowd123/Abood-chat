"""تحويل الصوت إلى نصّ عبر Whisper (أو أي نقطة نهاية متوافقة)."""
from __future__ import annotations

import asyncio
import logging
from pathlib import Path

log = logging.getLogger(__name__)

TIMEOUT = 180.0
MAX_BYTES = 24 * 1024 * 1024   # حدّ OpenAI 25MB؛ نتركه هامشًا


class TranscriptionError(RuntimeError):
    """رسالته صالحة للعرض."""


class TranscriptionService:
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

    @property
    def available(self) -> bool:
        return self._enabled

    @property
    def model(self) -> str:
        return self._model

    def _ensure_client(self) -> object:
        if self._client is None:
            import openai

            self._client = openai.AsyncOpenAI(
                api_key=self._api_key or "not-required",
                base_url=self._base_url,
                timeout=TIMEOUT,
                max_retries=1,
            )
        return self._client

    async def transcribe(self, path: str | Path) -> str:
        if not self.available:
            raise TranscriptionError(
                "تحويل الصوت غير مضبوط. أضف WHISPER_API_KEY في البيئة "
                "أو استخدم خادمًا محليًا."
            )
        file_path = Path(path)
        if not file_path.exists():
            raise TranscriptionError("تعذّر العثور على الملف الصوتي.")
        size = file_path.stat().st_size
        if size > MAX_BYTES:
            raise TranscriptionError(
                f"الملف كبير ({size // (1024 * 1024)}MB). الحد {MAX_BYTES // (1024 * 1024)}MB."
            )

        client = self._ensure_client()
        try:
            with file_path.open("rb") as handle:
                response = await asyncio.wait_for(
                    client.audio.transcriptions.create(
                        model=self._model, file=handle
                    ),
                    timeout=TIMEOUT,
                )
        except asyncio.TimeoutError as exc:
            raise TranscriptionError("انتهت المهلة أثناء تحويل الصوت.") from exc
        except Exception as exc:
            raise TranscriptionError(f"تعذّر تحويل الصوت: {str(exc)[:180]}") from exc

        text = str(getattr(response, "text", "") or "").strip()
        if not text:
            raise TranscriptionError("لم أستخرج أي نصّ من التسجيل.")
        return text

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
            log.debug("تعذّر إغلاق عميل التحويل", exc_info=True)
