"""الإدخال النصّي المعلَّق: شخصية، مزوّد مخصّص، مفتاح.

يُسجَّل في group=1 ليسبق هاندلر المحادثة (group=2)، ويوقف الانتشار
حين يكون هناك تدفّق فعلي فقط.
"""
from __future__ import annotations

import logging
from typing import Any

from pyrogram import Client, filters
from pyrogram.types import Message

from database.custom_providers import (
    CustomProviderError,
    validate_api_key,
    validate_base_url,
    validate_model_name,
)
from database.custom_providers import validate_name as validate_provider_name
from database.personas import PersonaError, validate_name, validate_prompt
from database.provider_keys import (
    ProviderKeyError,
    validate_label,
    validate_value,
)
from services import screens
from services.flows import (
    CUSTOM_PROMPTS,
    FLOW_CUSTOM,
    FLOW_KEY,
    FLOW_PERSONA,
    KEY_PROMPTS,
    KEY_STEPS,
    PERSONA_PROMPTS,
    next_step,
)
from utils.text import escape

log = logging.getLogger(__name__)

PERSONA_VALIDATORS = {"name": validate_name, "prompt": validate_prompt}

CUSTOM_VALIDATORS = {
    "name": validate_provider_name,
    "base_url": validate_base_url,
    "api_key": validate_api_key,
    "model_name": validate_model_name,
}


def register(app: Client, owner_only: Any) -> None:
    base = filters.private & filters.text & owner_only

    @app.on_message(base & filters.command("cancel"), group=1)
    async def cancel(client: Client, message: Message) -> None:
        cleared = await client.ctx.flows.clear()
        await message.reply_text(
            "أُلغي الإدخال." if cleared else "لا يوجد إدخال معلَّق.", quote=True
        )
        message.stop_propagation()

    @app.on_message(base & ~filters.regex(r"^/"), group=1)
    async def on_input(client: Client, message: Message) -> None:
        flow = await client.ctx.flows.get()
        if flow is None:
            return   # يمرّ إلى هاندلر المحادثة

        message.stop_propagation()
        kind = flow.get("kind")
        if kind == FLOW_KEY:
            await _key_step(client, message, flow)
        elif kind == FLOW_CUSTOM:
            await _custom_step(client, message, flow)
        elif kind == FLOW_PERSONA:
            await _persona_step(client, message, flow)
        else:
            await client.ctx.flows.clear()
            await message.reply_text("أُلغي إدخال غير معروف.", quote=True)


# ───────────────────────── الشخصية ─────────────────────────

async def _persona_step(client: Client, message: Message, flow: dict) -> None:
    step = flow.get("step") or "name"
    data = dict(flow.get("data") or {})
    try:
        value = PERSONA_VALIDATORS[step](message.text or "")
    except PersonaError as exc:
        await message.reply_text(
            f"❌ {escape(str(exc))}\n\nأعد الإرسال أو /cancel.", quote=True
        )
        return

    data[step] = value
    following = next_step(FLOW_PERSONA, step)
    if following:
        await client.ctx.flows.update(step=following, data=data)
        await message.reply_text(PERSONA_PROMPTS[following], quote=True)
        return

    await client.ctx.flows.clear()
    try:
        persona = await client.ctx.personas.create(data["name"], data["prompt"])
    except PersonaError as exc:
        await message.reply_text(f"❌ {escape(str(exc))}", quote=True)
        return

    await client.ctx.settings_repo.set_persona(persona.persona_id)
    screen = screens.persona_detail(persona, active=True)
    await message.reply_text(
        f"✅ أُضيفت الشخصية <b>{escape(persona.name)}</b> وفُعِّلت.", quote=True
    )
    await message.reply_text(screen.text, reply_markup=screen.markup)


# ───────────────────────── المزوّد المخصّص ─────────────────────────

async def _custom_step(client: Client, message: Message, flow: dict) -> None:
    step = flow.get("step") or "name"
    data = dict(flow.get("data") or {})
    try:
        value = CUSTOM_VALIDATORS[step](message.text or "")
    except CustomProviderError as exc:
        await message.reply_text(
            f"❌ {escape(str(exc))}\n\nأعد الإرسال أو /cancel.", quote=True
        )
        return

    data[step] = value
    following = next_step(FLOW_CUSTOM, step)
    if following:
        await client.ctx.flows.update(step=following, data=data)
        await message.reply_text(CUSTOM_PROMPTS[following], quote=True)
        return

    await client.ctx.flows.clear()
    notice = await message.reply_text("⏳ <i>جاري الحفظ والفحص...</i>", quote=True)
    try:
        doc = await client.ctx.custom.add(
            name=data["name"],
            base_url=data["base_url"],
            model_name=data["model_name"],
            api_key=data.get("api_key"),
        )
    except CustomProviderError as exc:
        await notice.edit_text(f"❌ {escape(str(exc))}")
        return
    except Exception:
        log.exception("فشل حفظ المزوّد المخصّص")
        await notice.edit_text("❌ تعذّر الحفظ. راجع السجلات.")
        return

    # الحفظ يسبق الفحص: خادم مطفأ لا يجب أن يمحو أربع خطوات إدخال
    ok, detail = await client.ctx.custom.probe(doc.key)
    screen = screens.custom_saved(doc, probe_ok=ok, probe_detail=detail)
    await notice.edit_text(screen.text, reply_markup=screen.markup)


# ───────────────────────── المفتاح ─────────────────────────

async def _key_step(client: Client, message: Message, flow: dict) -> None:
    step = flow.get("step") or KEY_STEPS[0]
    data = dict(flow.get("data") or {})
    provider = str(data.get("provider") or "")

    if step == "value":
        # الحذف أولًا: أي مسار خروج بعده يترك المفتاح ظاهرًا في المحادثة
        try:
            await message.delete()
        except Exception:
            log.warning("تعذّر حذف رسالة المفتاح — احذفها يدويًا")

        try:
            value = validate_value(message.text or "")
        except ProviderKeyError as exc:
            await client.send_message(
                message.chat.id,
                f"❌ {escape(str(exc))}\n\nأعد الإرسال أو /cancel.",
            )
            return

        if await client.ctx.key_repo.has_fingerprint(provider, value):
            await client.ctx.flows.clear()
            await client.send_message(
                message.chat.id,
                "❌ هذا المفتاح مضاف بالفعل لهذا المزوّد.\n"
                "<i>تكراره يُبطل التوزيع بلا أن يظهر ذلك.</i>",
            )
            return

        data["value"] = value
        await client.ctx.flows.update(step="label", data=data)
        await client.send_message(message.chat.id, KEY_PROMPTS["label"])
        return

    try:
        label = validate_label(message.text or "")
    except ProviderKeyError as exc:
        await message.reply_text(f"❌ {escape(str(exc))}", quote=True)
        return

    await client.ctx.flows.clear()
    try:
        key = await client.ctx.keyring.add(provider, data["value"], label)
    except ProviderKeyError as exc:
        await message.reply_text(f"❌ {escape(str(exc))}", quote=True)
        return

    ring = client.ctx.keyring
    screen = screens.keys_for_provider(
        ring.status(provider), ring.keys_for(provider), threshold=ring.threshold
    )
    await message.reply_text(
        f"✅ أُضيف المفتاح <code>{escape(key.display)}</code>", quote=True
    )
    await message.reply_text(screen.text, reply_markup=screen.markup)
