"""كل الأزرار والتنقّل. التنقّل بـ edit_message لا برسائل جديدة."""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from io import BytesIO
from typing import Any

from pyrogram import Client, filters
from pyrogram.types import CallbackQuery

from services import screens
from services.ai_providers import BUILTIN_PROVIDERS
from services.catalog import (
    DEFAULT_MODEL,
    dynamic_key,
    is_valid_model,
    normalize_model,
    provider_of,
    remember_one,
)
from services.flows import FLOW_CUSTOM, FLOW_KEY, FLOW_PERSONA, prompt_for
from services.keyring import RANDOM, ROUND_ROBIN
from utils import keyboards as kb
from utils.text import escape

log = logging.getLogger(__name__)

LOADING_ROUTES = ("svc", "svcp", "svcf")


def register(app: Client, owner_only: Any) -> None:
    @app.on_callback_query(owner_only)
    async def on_callback(client: Client, query: CallbackQuery) -> None:
        route = kb.parse_cb(query.data or "")
        head = route[0] if route else ""

        try:
            if head == "noop":
                await query.answer()
                return
            if head == "close":
                await query.answer()
                await query.message.delete()
                return

            # الجلب يحتاج مؤشّر تحميل ثم نتيجة: مساران للتعديل
            if head in LOADING_ROUTES and len(route) > 1:
                if await _service_flow(client, query, route):
                    return

            if head == "do":
                await _do(client, query, route[1:])
                return
            if head == "set":
                await _set(client, query, route[1:])
                return
            if head in ("kdel", "ktog", "pdel", "cxdel", "wipeask", "forgetask"):
                await _confirmations(client, query, head, route)
                return

            await query.answer()
            await _render(client, query, route)
        except Exception as exc:
            log.exception("فشل في معالجة الزرّ %s", query.data)
            try:
                await query.answer("حدث خطأ — راجع السجلّ", show_alert=True)
            except Exception:
                pass


# ───────────────────────── الرسم ─────────────────────────

async def _render(client: Client, query: CallbackQuery, route: tuple[str, ...]) -> None:
    screen = await _screen_for(client, route)
    await _apply(query, screen)


async def _apply(query: CallbackQuery, screen: screens.Screen) -> None:
    try:
        await query.message.edit_text(
            screen.text, reply_markup=screen.markup, disable_web_page_preview=True
        )
    except Exception as exc:
        if "MESSAGE_NOT_MODIFIED" in str(exc):
            return
        log.debug("تعذّر تعديل الرسالة: %s", exc)


def _int_at(route: tuple[str, ...], index: int) -> int:
    if len(route) > index and route[index].lstrip("-").isdigit():
        return max(0, int(route[index]))
    return 0


def service_entries(client: Client) -> list[screens.ServiceEntry]:
    ring = client.ctx.keyring
    entries: list[screens.ServiceEntry] = []
    for name in BUILTIN_PROVIDERS:
        status = ring.status(name)
        entries.append(
            screens.ServiceEntry(
                name=name, label=status.label, custom=False,
                healthy=status.healthy, keys=status.summary,
            )
        )
    for doc in client.ctx.custom.docs():
        status = ring.status(doc.key)
        entries.append(
            screens.ServiceEntry(
                name=doc.key, label=doc.name, custom=True,
                healthy=True,   # المحلي يعمل بلا مفتاح
                keys=status.summary if status.total else "بلا مفتاح",
            )
        )
    return entries


async def _screen_for(client: Client, route: tuple[str, ...]) -> screens.Screen:
    ctx = client.ctx
    s = await ctx.bot_settings()
    head = route[0] if route else "home"
    arg = route[1] if len(route) > 1 else ""

    if head in ("home", ""):
        return screens.main_menu(
            s,
            messages=await ctx.conversation.count(),
            personas=await ctx.personas.count(),
        )

    if head == "settings":
        return screens.settings_menu(
            s,
            search_available=ctx.search.available,
            memory_available=ctx.memory.available,
        )

    if head == "svc" and not arg:
        return screens.services(s, service_entries(client))

    if head == "models":
        return screens.curated_models(s, page=_int_at(route, 1))

    if head == "personas":
        items = await ctx.personas.list_all()
        return screens.personas(
            items, s.selected_persona_id, page=_int_at(route, 1)
        )

    if head == "persona":
        item = await ctx.personas.get(arg)
        if item is None:
            return await _screen_for(client, ("personas",))
        return screens.persona_detail(item, active=s.selected_persona_id == arg)

    if head == "memory":
        status = await ctx.memory.status(enabled=s.memory_enabled)
        return screens.memory(status, await ctx.memory.latest(5))

    if head == "search":
        return screens.search_screen(
            s, provider=ctx.search.provider_label, available=ctx.search.available
        )

    if head == "cxlist":
        return screens.custom_providers(ctx.custom.docs())

    if head == "cx":
        doc = ctx.custom.doc(arg)
        if doc is None:
            return await _screen_for(client, ("cxlist",))
        return screens.custom_detail(doc)

    if head == "keys":
        ring = ctx.keyring
        if not arg:
            providers = [*BUILTIN_PROVIDERS, *(d.key for d in ctx.custom.docs())]
            return screens.keys_overview(
                ring.statuses(providers),
                strategy=ring.strategy,
                encrypted=ctx.key_repo.encrypted,
            )
        return screens.keys_for_provider(
            ring.status(arg), ring.keys_for(arg), threshold=ring.threshold
        )

    if head == "key":
        key = await ctx.key_repo.get(arg)
        if key is None:
            return await _screen_for(client, ("keys",))
        return screens.key_detail(key, ctx.keyring.status(key.provider_name))

    if head == "data":
        return screens.data_menu(await ctx.data.stats())

    if head == "reset":
        return screens.confirm_reset()

    return await _screen_for(client, ("home",))


# ───────────────────────── جلب النماذج ─────────────────────────

async def _service_flow(
    client: Client, query: CallbackQuery, route: tuple[str, ...]
) -> bool:
    """يرجع True إن تولّى الرد. مؤشّر تحميل ثم نتيجة."""
    head = route[0]
    provider = route[1]
    family = route[2] if head == "svcf" and len(route) > 2 else ""
    page = _int_at(route, 3 if head == "svcf" else 2)

    directory = client.ctx.models
    cached = await directory.peek(provider)
    await query.answer()
    if cached is None:
        # المؤشّر يظهر عند جلب فعلي فقط، لا عند تصفّح كاش
        try:
            await query.message.edit_text(screens.LOADING_MODELS)
        except Exception:
            log.debug("تعذّر عرض مؤشّر التحميل", exc_info=True)

    listing = await directory.fetch(provider)
    s = await client.ctx.bot_settings()
    screen = (
        screens.model_fetch_failed(provider, listing.error or "سبب غير معروف")
        if listing.failed
        else screens.discovered_models(listing, s, family=family, page=page)
    )
    await _apply(query, screen)
    return True


# ───────────────────────── الاختيار ─────────────────────────

async def _set(
    client: Client, query: CallbackQuery, args: tuple[str, ...]
) -> None:
    kind = args[0] if args else ""
    value = args[1] if len(args) > 1 else ""

    if kind == "model":
        if not is_valid_model(value):
            await query.answer("نموذج غير صالح", show_alert=True)
            return
        await client.ctx.settings_repo.set_selection(value, provider_of(value))
        await query.answer("✅ حُفظ النموذج")
        await _render(client, query, ("settings",))
        return

    if kind == "persona":
        item = await client.ctx.personas.get(value)
        if item is None:
            await query.answer("غير موجودة", show_alert=True)
            return
        await client.ctx.settings_repo.set_persona(value)
        await query.answer(f"✅ {item.name}")
        await _render(client, query, ("persona", value))
        return

    await query.answer()


async def _pick_model(
    client: Client, query: CallbackQuery, provider: str, digest: str
) -> None:
    await query.answer("جارٍ الحفظ...")
    info = await client.ctx.models.resolve(provider, digest)
    if info is None:
        listing = await client.ctx.models.fetch(provider, force=True)
        await _apply(
            query,
            screens.model_fetch_failed(
                provider,
                "لم أجد هذا النموذج في القائمة — قد يكون سُحب من الخدمة.",
                has_stale=listing.ok,
            ),
        )
        return

    from database.settings import ModelCap

    key = dynamic_key(provider, info.id)
    cap = ModelCap(
        key=key, label=info.name, vision=info.vision,
        tools=info.tools, context=info.context,
    )
    remember_one(
        key, label=info.name, vision=info.vision,
        tools=info.tools, context=info.context,
    )
    try:
        await client.ctx.settings_repo.set_selection(key, provider, cap)
    except ValueError:
        await query.answer("النموذج غير صالح", show_alert=True)
        return
    log.info("النموذج المختار: %s (%s)", info.id, provider)
    await _apply(query, screens.model_selected(info, provider))


# ───────────────────────── التأكيدات ─────────────────────────

async def _confirmations(
    client: Client, query: CallbackQuery, head: str, route: tuple[str, ...]
) -> None:
    ctx = client.ctx
    arg = route[1] if len(route) > 1 else ""
    s = await ctx.bot_settings()

    if head == "wipeask":
        await query.answer()
        await _apply(query, screens.confirm_wipe(await ctx.data.stats()))
        return

    if head == "forgetask":
        await query.answer()
        await _apply(query, screens.confirm_forget())
        return

    if head == "pdel":
        item = await ctx.personas.get(arg)
        if item is None:
            await query.answer("غير موجودة", show_alert=True)
            return
        if item.built_in:
            await query.answer("الشخصية الافتراضية لا تُحذف", show_alert=True)
            return
        await query.answer()
        await _apply(query, screens.confirm_persona_delete(item))
        return

    if head == "cxdel":
        doc = ctx.custom.doc(arg)
        if doc is None:
            await query.answer("غير موجود", show_alert=True)
            return
        await query.answer()
        await _apply(
            query,
            screens.confirm_custom_delete(
                doc, selected=provider_of(normalize_model(s.selected_model)) == arg
            ),
        )
        return

    if head == "kdel":
        key = await ctx.key_repo.get(arg)
        if key is None:
            await query.answer("غير موجود", show_alert=True)
            return
        await query.answer()
        await _apply(
            query,
            screens.confirm_key_delete(key, ctx.keyring.status(key.provider_name)),
        )
        return

    if head == "ktog":
        key = await ctx.key_repo.get(arg)
        if key is None:
            await query.answer("غير موجود", show_alert=True)
            return
        updated = await ctx.keyring.set_active(arg, not key.is_active)
        await query.answer(
            "أُعيد التفعيل والعدّاد صُفِّر" if updated.is_active else "عُطِّل المفتاح"
        )
        await _apply(
            query,
            screens.key_detail(updated, ctx.keyring.status(updated.provider_name)),
        )
        return


# ───────────────────────── الأفعال ─────────────────────────

async def _do(
    client: Client, query: CallbackQuery, args: tuple[str, ...]
) -> None:
    ctx = client.ctx
    action = args[0] if args else ""
    arg = args[1] if len(args) > 1 else ""

    # ---- اختيار نموذج مُكتشَف ----
    if action == "pm":   # احتياط: pm يأتي كمسار مستقلّ عادةً
        await _pick_model(client, query, arg, args[2] if len(args) > 2 else "")
        return

    # ---- مفاتيح التبديل ----
    if action == "memtog":
        s = await ctx.bot_settings()
        if not ctx.memory.available and not s.memory_enabled:
            await query.answer(
                "الذاكرة غير مضبوطة — أضف EMBEDDING_API_KEY", show_alert=True
            )
            return
        updated = await ctx.settings_repo.set_memory(not s.memory_enabled)
        await query.answer(
            "🧠 الذاكرة مفعّلة" if updated.memory_enabled else "🧠 الذاكرة معطّلة"
        )
        await _render(client, query, ("settings",))
        return

    if action == "srctog":
        s = await ctx.bot_settings()
        if not ctx.search.available:
            await query.answer(
                "البحث غير مضبوط — أضف SERPER_API_KEY أو BRAVE_API_KEY",
                show_alert=True,
            )
            return
        updated = await ctx.settings_repo.set_web_search(not s.web_search_enabled)
        await query.answer(
            "🔍 البحث مفعّل" if updated.web_search_enabled else "🔍 البحث معطّل"
        )
        await _render(client, query, ("settings",))
        return

    if action == "strtog":
        s = await ctx.bot_settings()
        updated = await ctx.settings_repo.set_streaming(not s.streaming_enabled)
        await query.answer(
            "⚡ البثّ مفعّل" if updated.streaming_enabled else "⚡ البثّ معطّل"
        )
        await _render(client, query, ("settings",))
        return

    if action == "kstrat":
        ring = ctx.keyring
        following = RANDOM if ring.strategy == ROUND_ROBIN else ROUND_ROBIN
        ring.set_strategy(following)
        await ctx.settings_repo.set_key_strategy(following)
        await query.answer(f"الاستراتيجية: {following}")
        await _render(client, query, ("keys",))
        return

    # ---- النماذج ----
    if action in ("mrefresh", "mretry"):
        await query.answer("جارٍ التحديث...")
        try:
            await query.message.edit_text(screens.LOADING_MODELS)
        except Exception:
            pass
        listing = await ctx.models.fetch(arg, force=True)
        s = await ctx.bot_settings()
        screen = (
            screens.model_fetch_failed(arg, listing.error or "")
            if listing.failed
            else screens.discovered_models(listing, s)
        )
        await _apply(query, screen)
        return

    if action == "mall":
        s = await ctx.bot_settings()
        updated = await ctx.settings_repo.set_model_show_all(not s.model_show_all)
        await query.answer("عرض الكل" if updated.model_show_all else "المحادثة فقط")
        listing = await ctx.models.fetch(arg)
        await _apply(query, screens.discovered_models(listing, updated))
        return

    # ---- التدفّقات ----
    if action in ("padd", "cxadd", "kadd"):
        if await ctx.flows.has_pending():
            await query.answer(
                "هناك إدخال معلَّق. أكمله أو أرسل /cancel.", show_alert=True
            )
            return
        if action == "padd":
            await ctx.flows.start(FLOW_PERSONA)
            text = prompt_for(FLOW_PERSONA, "name")
        elif action == "cxadd":
            await ctx.flows.start(FLOW_CUSTOM)
            text = prompt_for(FLOW_CUSTOM, "name")
        else:
            if not arg:
                await query.answer("مزوّد غير محدّد", show_alert=True)
                return
            await ctx.flows.start(FLOW_KEY, {"provider": arg})
            text = prompt_for(
                FLOW_KEY, "value", provider=escape(ctx.keyring.label_for(arg))
            )
        await query.answer()
        await query.message.reply_text(text, quote=False)
        return

    # ---- الفحص ----
    if action == "cxtest":
        await query.answer("جارٍ الفحص...")
        ok, detail = await ctx.custom.probe(arg)
        await query.message.reply_text(
            f"{'✅' if ok else '❌'} <b>فحص المزوّد</b>\n{escape(detail)}", quote=True
        )
        return

    # ---- الحذف والتنفيذ ----
    if action == "prm":
        from database.personas import PersonaError

        s = await ctx.bot_settings()
        try:
            removed = await ctx.personas.delete(arg)
        except PersonaError as exc:
            await query.answer(str(exc), show_alert=True)
            return
        if removed and s.selected_persona_id == arg:
            default = await ctx.personas.ensure_default()
            await ctx.settings_repo.set_persona(default.persona_id)
        await query.answer("حُذفت الشخصية" if removed else "غير موجودة")
        await _render(client, query, ("personas",))
        return

    if action == "cxrm":
        s = await ctx.bot_settings()
        was_selected = provider_of(normalize_model(s.selected_model)) == arg
        removed = await ctx.custom.remove(arg)
        if removed and was_selected:
            # مفتاح معلّق يسقط للافتراضي بصمت في كل رسالة بلا هذا
            await ctx.settings_repo.set_model(DEFAULT_MODEL)
        await query.answer("حُذف المزوّد" if removed else "غير موجود")
        await _render(client, query, ("cxlist",))
        return

    if action == "krm":
        key = await ctx.key_repo.get(arg)
        provider = key.provider_name if key else ""
        removed = await ctx.keyring.remove(arg)
        await query.answer("حُذف المفتاح" if removed else "غير موجود")
        await _render(client, query, ("keys", provider) if provider else ("keys",))
        return

    if action == "reset":
        count = await ctx.chat.reset()
        await query.answer(f"أُعيد التعيين ({count} رسالة)")
        await _render(client, query, ("home",))
        return

    if action == "forget":
        count = await ctx.memory.forget_all()
        await query.answer(f"مُسحت الذاكرة ({count} عنصرًا)")
        await _render(client, query, ("memory",))
        return

    if action == "reindex":
        s = await ctx.bot_settings()
        if not ctx.memory.available:
            await query.answer("الذاكرة غير مضبوطة", show_alert=True)
            return
        await query.answer("جارٍ إعادة الفهرسة...")
        messages = await ctx.conversation.all_messages()
        indexed = await ctx.memory.reindex(
            [(item.role, item.content) for item in messages]
        )
        await query.message.reply_text(
            f"🧠 فُهرس <code>{indexed}</code> عنصرًا من "
            f"<code>{len(messages)}</code> رسالة.",
            quote=True,
        )
        await _render(client, query, ("memory",))
        return

    if action == "export":
        await query.answer("جارٍ التصدير...")
        try:
            payload = await ctx.data.export_json()
        except Exception as exc:
            log.exception("فشل التصدير")
            await query.message.reply_text(
                f"❌ تعذّر التصدير: {escape(str(exc)[:200])}", quote=True
            )
            return
        stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M")
        document = BytesIO(payload)
        document.name = f"backup-{stamp}.json"
        await query.message.reply_document(
            document,
            caption=(
                "📦 <b>نسخة احتياطية</b>\n"
                f"<i>{len(payload) // 1024}KB · بلا قيم المفاتيح</i>"
            ),
            quote=True,
        )
        return

    if action == "wipe":
        await query.answer("جارٍ الحذف...")
        report = await ctx.data.wipe_all()
        await _apply(query, screens.wipe_done(report))
        return

    await query.answer()


# ───────────────────────── مسار اختيار النموذج ─────────────────────────

def register_model_picker(app: Client, owner_only: Any) -> None:
    """`pm:<provider>:<hash>` كمسار مستقلّ: البصمة قد تحمل أحرفًا خاصة."""

    @app.on_callback_query(owner_only & filters.regex(r"^pm:"))
    async def on_pick(client: Client, query: CallbackQuery) -> None:
        route = kb.parse_cb(query.data or "")
        if len(route) < 3:
            await query.answer("زرّ غير صالح", show_alert=True)
            return
        await _pick_model(client, query, route[1], route[2])
