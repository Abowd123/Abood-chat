"""الرسائل والوسائط: نصّ، صورة، صوت. كلها تمرّ بنفس ChatService."""
from __future__ import annotations

import base64
import logging
import time
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any

from pyrogram import Client, filters
from pyrogram.enums import ChatAction, ParseMode
from pyrogram.types import Message

from services.ai_providers import ImagePart, ProviderError
from services.transcription import TranscriptionError
from utils.text import balance_code_fences, escape, split_message

log = logging.getLogger(__name__)

MAX_IMAGE_BYTES = 5 * 1024 * 1024
STREAM_INTERVAL = 1.2        # أقل من هذا يصطدم بحدّ تعديل الرسائل
STREAM_MIN_DELTA = 60


class StreamSink:
    """يحدّث رسالة واحدة تدريجيًا بلا أن يصطدم بحدّ تليجرام."""

    def __init__(self, message: Message) -> None:
        self._message = message
        self._last_push = 0.0
        self._last_length = 0
        self._sent = ""

    async def push(self, text: str) -> None:
        now = time.monotonic()
        if now - self._last_push < STREAM_INTERVAL:
            return
        if len(text) - self._last_length < STREAM_MIN_DELTA:
            return
        self._last_push = now
        self._last_length = len(text)
        await self._edit(balance_code_fences(text) + " ▌")

    async def finish(self, text: str) -> None:
        chunks = split_message(text)
        await self._edit(chunks[0])
        for extra in chunks[1:]:
            await self._message.reply_text(
                extra, parse_mode=ParseMode.MARKDOWN, quote=False
            )

    async def _edit(self, text: str) -> None:
        if text == self._sent:
            return
        self._sent = text
        try:
            await self._message.edit_text(text, parse_mode=ParseMode.MARKDOWN)
        except Exception:
            # Markdown غير متوازن أثناء البثّ: نعيد كنصّ خام
            try:
                await self._message.edit_text(text)
            except Exception:
                log.debug("تعذّر تعديل رسالة البثّ", exc_info=True)


def register(app: Client, owner_only: Any) -> None:
    base = filters.private & owner_only

    @app.on_message(base & filters.text & ~filters.regex(r"^/"), group=2)
    async def on_text(client: Client, message: Message) -> None:
        await _handle(client, message, prompt=message.text or "")

    @app.on_message(base & filters.photo, group=2)
    async def on_photo(client: Client, message: Message) -> None:
        prompt = (message.caption or "").strip() or "صِف هذه الصورة بالتفصيل."
        with TemporaryDirectory() as folder:
            path = await message.download(file_name=f"{folder}/image")
            if not path:
                await message.reply_text("❌ تعذّر تنزيل الصورة.", quote=True)
                return
            data = Path(path).read_bytes()
        if len(data) > MAX_IMAGE_BYTES:
            await message.reply_text(
                f"❌ الصورة كبيرة ({len(data) // (1024 * 1024)}MB). "
                f"الحدّ {MAX_IMAGE_BYTES // (1024 * 1024)}MB.",
                quote=True,
            )
            return
        image = ImagePart(
            media_type="image/jpeg",
            data=base64.b64encode(data).decode(),
        )
        await _handle(
            client, message, prompt=prompt, images=[image], media_type="photo"
        )

    @app.on_message(base & (filters.voice | filters.audio), group=2)
    async def on_voice(client: Client, message: Message) -> None:
        service = client.ctx.transcription
        if not service.available:
            await message.reply_text(
                "🎙 تحويل الصوت غير مضبوط.\n"
                "<i>أضف WHISPER_API_KEY، أو استخدم خادمًا محليًا "
                "بـ WHISPER_BASE_URL.</i>",
                quote=True,
            )
            return

        notice = await message.reply_text("🎙 <i>جاري تحويل الصوت...</i>", quote=True)
        with TemporaryDirectory() as folder:
            path = await message.download(file_name=f"{folder}/audio")
            if not path:
                await notice.edit_text("❌ تعذّر تنزيل الملف الصوتي.")
                return
            try:
                transcript = await service.transcribe(path)
            except TranscriptionError as exc:
                await notice.edit_text(f"❌ {escape(str(exc))}")
                return

        await notice.edit_text(
            f"🎙 <b>النصّ المستخرج:</b>\n<i>{escape(transcript)}</i>"
        )
        await _handle(client, message, prompt=transcript, media_type="voice")


async def _handle(
    client: Client,
    message: Message,
    *,
    prompt: str,
    images: list[ImagePart] | None = None,
    media_type: str = "",
) -> None:
    prompt = (prompt or "").strip()
    if not prompt:
        return

    # إدخال معلَّق يعالجه handlers/flow في group=1؛ هذا احتياط
    if await client.ctx.flows.has_pending():
        return

    settings = await client.ctx.bot_settings()
    placeholder = await message.reply_text("💭 <i>جاري التفكير...</i>", quote=True)
    sink = StreamSink(placeholder)

    try:
        await client.send_chat_action(message.chat.id, ChatAction.TYPING)
        result = await client.ctx.chat.send(
            prompt,
            settings,
            images=images or (),
            media_type=media_type,
            stream_to=sink,
        )
    except ProviderError as exc:
        await placeholder.edit_text(
            f"❌ <b>تعذّر الحصول على رد</b>\n\n{escape(exc.user_message)}"
        )
        return
    except Exception as exc:
        log.exception("فشل غير متوقّع في مسار المحادثة")
        await placeholder.edit_text(
            f"❌ <b>خطأ غير متوقّع</b>\n<code>{escape(str(exc)[:300])}</code>\n\n"
            "<i>وصلتني نسخة من التفاصيل.</i>"
        )
        return

    # البثّ كتب الردّ بنفسه؛ غيره يحتاج كتابةً الآن
    if placeholder.text in ("💭 جاري التفكير...", None) or not sink._sent:
        chunks = split_message(result.text)
        try:
            await placeholder.edit_text(chunks[0], parse_mode=ParseMode.MARKDOWN)
        except Exception:
            await placeholder.edit_text(chunks[0])
        for extra in chunks[1:]:
            await message.reply_text(
                extra, parse_mode=ParseMode.MARKDOWN, quote=False
            )

    log.info("ردّ عبر %s (%s)", result.model_label, result.footer)
