"""الأوامر النصّية كاختصارات فقط. كل شيء متاح عبر الأزرار."""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from io import BytesIO
from typing import Any

from pyrogram import Client, filters
from pyrogram.types import BotCommand, Message

from services import screens
from services.websearch import SearchError
from utils.text import escape

log = logging.getLogger(__name__)

COMMANDS = [
    BotCommand("start", "القائمة الرئيسية"),
    BotCommand("menu", "القائمة الرئيسية"),
    BotCommand("model", "اختيار الخدمة والنموذج"),
    BotCommand("persona", "الشخصيات"),
    BotCommand("memory", "الذاكرة الدلالية"),
    BotCommand("search", "بحث ويب يدوي"),
    BotCommand("reset", "إعادة تعيين المحادثة"),
    BotCommand("export", "تصدير البيانات JSON"),
    BotCommand("forget_all", "مسح كل الذاكرة"),
    BotCommand("status", "حالة البوت"),
    BotCommand("cancel", "إلغاء الإدخال المعلَّق"),
]


def register(app: Client, owner_only: Any) -> None:
    base = filters.private & owner_only

    async def _send(client: Client, message: Message, screen: screens.Screen) -> None:
        await message.reply_text(screen.text, reply_markup=screen.markup, quote=False)

    @app.on_message(base & filters.command(["start", "menu"]), group=0)
    async def start(client: Client, message: Message) -> None:
        s = await client.ctx.bot_settings()
        await _send(
            client, message,
            screens.main_menu(
                s,
                messages=await client.ctx.conversation.count(),
                personas=await client.ctx.personas.count(),
            ),
        )

    @app.on_message(base & filters.command("model"), group=0)
    async def model(client: Client, message: Message) -> None:
        from handlers.menu import service_entries

        s = await client.ctx.bot_settings()
        await _send(client, message, screens.services(s, service_entries(client)))

    @app.on_message(base & filters.command("persona"), group=0)
    async def persona(client: Client, message: Message) -> None:
        s = await client.ctx.bot_settings()
        items = await client.ctx.personas.list_all()
        await _send(client, message, screens.personas(items, s.selected_persona_id))

    @app.on_message(base & filters.command("memory"), group=0)
    async def memory(client: Client, message: Message) -> None:
        s = await client.ctx.bot_settings()
        status = await client.ctx.memory.status(enabled=s.memory_enabled)
        latest = await client.ctx.memory.latest(5)
        await _send(client, message, screens.memory(status, latest))

    @app.on_message(base & filters.command("reset"), group=0)
    async def reset(client: Client, message: Message) -> None:
        await _send(client, message, screens.confirm_reset())

    @app.on_message(base & filters.command("forget_all"), group=0)
    async def forget_all(client: Client, message: Message) -> None:
        await _send(client, message, screens.confirm_forget())

    @app.on_message(base & filters.command("export"), group=0)
    async def export(client: Client, message: Message) -> None:
        notice = await message.reply_text("⏳ <i>جاري التصدير...</i>", quote=True)
        try:
            payload = await client.ctx.data.export_json()
        except Exception as exc:
            log.exception("فشل التصدير")
            await notice.edit_text(f"❌ تعذّر التصدير: {escape(str(exc)[:200])}")
            return
        stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M")
        document = BytesIO(payload)
        document.name = f"backup-{stamp}.json"
        await message.reply_document(
            document,
            caption=(
                "📦 <b>نسخة احتياطية</b>\n"
                f"<i>{len(payload) // 1024}KB · بلا قيم المفاتيح</i>"
            ),
            quote=True,
        )
        await notice.delete()

    @app.on_message(base & filters.command("search"), group=0)
    async def search(client: Client, message: Message) -> None:
        parts = (message.text or "").split(maxsplit=1)
        if len(parts) < 2:
            await message.reply_text(
                "الاستخدام: <code>/search عبارة البحث</code>", quote=True
            )
            return
        query = parts[1].strip()
        notice = await message.reply_text("🔍 <i>جاري البحث...</i>", quote=True)
        try:
            results = await client.ctx.search.search(query)
        except SearchError as exc:
            await notice.edit_text(f"❌ {escape(str(exc))}")
            return
        await notice.edit_text(
            client.ctx.search.format_results(query, results),
            disable_web_page_preview=True,
        )

    @app.on_message(base & filters.command("status"), group=0)
    async def status(client: Client, message: Message) -> None:
        ctx = client.ctx
        s = await ctx.bot_settings()
        stats = await ctx.data.stats()
        memory_status = await ctx.memory.status(enabled=s.memory_enabled)
        from services.catalog import model_label, normalize_model

        lines = [
            "<b>📊 حالة البوت</b>",
            "",
            f"النموذج: <code>{escape(model_label(normalize_model(s.selected_model)))}</code>",
            f"الرسائل: <code>{stats.messages}</code>",
            f"الشخصيات: <code>{stats.personas}</code>",
            f"الذاكرة: <code>{stats.memories}</code> عنصرًا "
            f"({escape(memory_status.backend)})",
            f"المزوّدات المخصّصة: <code>{stats.custom_providers}</code>",
            f"مفاتيح API: <code>{stats.provider_keys}</code>",
            "",
            f"Redis: {'🟢 متصل' if ctx.redis.available else '🔴 غير متاح'}",
            f"تشفير المفاتيح: {'🟢 مفعّل' if ctx.key_repo.encrypted else '🔴 معطّل'}",
            f"البحث: <code>{escape(ctx.search.provider_label)}</code>",
            f"تحويل الصوت: "
            f"{'🟢 متاح' if ctx.transcription.available else '🔴 غير مضبوط'}",
            f"كاش النماذج: <code>{ctx.models.cache_hits}</code> إصابة · "
            f"<code>{ctx.models.fetches}</code> جلب",
        ]
        await message.reply_text("\n".join(lines), quote=True)
