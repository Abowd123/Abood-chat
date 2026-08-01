"""المحادثات التفاعلية القصيرة: شخصية، مزوّد مخصّص، مفتاح.

الحالة في Mongo لا في الذاكرة: إعادة تشغيل وسط الإدخال لا تُفقده.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Mapping

from database.mongo import Mongo

log = logging.getLogger(__name__)

COLLECTION = "flows"
FLOW_ID = "pending"

FLOW_PERSONA = "persona"
FLOW_CUSTOM = "custom_provider"
FLOW_KEY = "provider_key"

PERSONA_STEPS = ("name", "prompt")
CUSTOM_STEPS = ("name", "base_url", "api_key", "model_name")
KEY_STEPS = ("value", "label")

PERSONA_PROMPTS = {
    "name": (
        "<b>🎭 شخصية جديدة (1/2)</b>\n\n"
        "ما اسم الشخصية؟\n"
        "<i>مثال: مبرمج Python · محرّر عربي · مدرّب لياقة</i>\n\n"
        "أرسل /cancel للإلغاء."
    ),
    "prompt": (
        "<b>🎭 شخصية جديدة (2/2)</b>\n\n"
        "أرسل تعليمات النظام (system prompt) لهذه الشخصية.\n"
        "<i>اكتبها بصيغة المخاطبة: «أنت مساعد يفعل كذا...»</i>"
    ),
}

CUSTOM_PROMPTS = {
    "name": (
        "<b>➕ مزوّد مخصّص (1/4)</b>\n\n"
        "ما اسم المزوّد؟ سيظهر في قائمة الخدمات.\n"
        "<i>مثال: Ollama محلي</i>\n\n"
        "أرسل /cancel للإلغاء."
    ),
    "base_url": (
        "<b>➕ مزوّد مخصّص (2/4)</b>\n\n"
        "ما عنوان الـ API؟ ينتهي بـ <code>/v1</code> عادةً.\n\n"
        "<i>أمثلة:</i>\n"
        "<code>http://localhost:11434/v1</code> — Ollama\n"
        "<code>http://localhost:1234/v1</code> — LM Studio\n"
        "<code>https://api.example.com/v1</code> — خادم بعيد\n\n"
        "<i>http الصريح مسموح للمضيفات المحلية فقط.</i>"
    ),
    "api_key": (
        "<b>➕ مزوّد مخصّص (3/4)</b>\n\n"
        "ما الـ API key؟\n"
        "أرسل <code>-</code> إن لم يكن مطلوبًا (Ollama وLM Studio محليًا).\n\n"
        "<i>⚠️ المفتاح سيُرسل إلى العنوان الذي أدخلته. "
        "لا تُدخل مفتاح مزوّد آخر هنا.</i>"
    ),
    "model_name": (
        "<b>➕ مزوّد مخصّص (4/4)</b>\n\n"
        "ما اسم النموذج كما يعرفه الخادم؟\n\n"
        "<i>أمثلة:</i> <code>llama3.2</code> · <code>qwen2.5:14b</code>\n"
        "<i>اعرف المتاح بـ:</i> <code>curl localhost:11434/v1/models</code>"
    ),
}

KEY_PROMPTS = {
    "value": (
        "<b>➕ مفتاح جديد لـ {provider} (1/2)</b>\n\n"
        "أرسل قيمة المفتاح.\n\n"
        "<i>🔒 سأحذف رسالتك فورًا بعد قراءتها. مفتاح يبقى في سجلّ "
        "المحادثة يبقى في سحابة تليجرام بلا حدّ زمني.</i>\n\n"
        "/cancel للإلغاء."
    ),
    "label": (
        "<b>➕ مفتاح جديد (2/2)</b>\n\n"
        "وسم يميّز هذا المفتاح؟ يظهر بجانب آخر 4 رموز.\n"
        "<i>مثال: حساب العمل · تجريبي · الحصة الثانية</i>\n\n"
        "أرسل <code>-</code> للتخطي."
    ),
}

FIRST_STEP = {
    FLOW_PERSONA: PERSONA_STEPS[0],
    FLOW_CUSTOM: CUSTOM_STEPS[0],
    FLOW_KEY: KEY_STEPS[0],
}

STEPS = {
    FLOW_PERSONA: PERSONA_STEPS,
    FLOW_CUSTOM: CUSTOM_STEPS,
    FLOW_KEY: KEY_STEPS,
}

PROMPTS = {
    FLOW_PERSONA: PERSONA_PROMPTS,
    FLOW_CUSTOM: CUSTOM_PROMPTS,
    FLOW_KEY: KEY_PROMPTS,
}


class FlowStore:
    """محادثة واحدة معلَّقة في كل وقت: المالك مستخدم واحد فلا تعارض بين محادثات."""

    def __init__(self, mongo: Mongo) -> None:
        self._col = mongo.db[COLLECTION]

    async def start(self, kind: str, data: dict[str, Any] | None = None) -> None:
        await self._col.update_one(
            {"_id": FLOW_ID},
            {
                "$set": {
                    "kind": kind,
                    "step": FIRST_STEP[kind],
                    "data": data or {},
                    "updated_at": datetime.now(timezone.utc),
                }
            },
            upsert=True,
        )

    async def get(self) -> dict[str, Any] | None:
        doc = await self._col.find_one({"_id": FLOW_ID})
        if not doc or not doc.get("kind"):
            return None
        return {
            "kind": str(doc.get("kind")),
            "step": str(doc.get("step") or ""),
            "data": dict(doc.get("data") or {}),
        }

    async def has_pending(self) -> bool:
        return await self.get() is not None

    async def update(self, *, step: str, data: dict[str, Any]) -> None:
        await self._col.update_one(
            {"_id": FLOW_ID}, {"$set": {"step": step, "data": data}}, upsert=True
        )

    async def clear(self) -> bool:
        result = await self._col.delete_one({"_id": FLOW_ID})
        return bool(result.deleted_count)


def next_step(kind: str, current: str) -> str | None:
    steps = STEPS.get(kind, ())
    if current not in steps:
        return None
    index = steps.index(current)
    return steps[index + 1] if index + 1 < len(steps) else None


def prompt_for(kind: str, step: str, **fields: Any) -> str:
    template = PROMPTS.get(kind, {}).get(step, "")
    return template.format(**fields) if fields else template
