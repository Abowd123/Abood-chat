"""التصدير والحذف الشامل.

قاعدة ثابتة: قيم مفاتيح API لا تخرج في التصدير أبدًا — الملف يمرّ
بسحابة تليجرام. نسخة `scripts/backup.py` تحملها (مشفّرة إن فعّلت
التشفير) لأن نسخة بلا مفاتيح لا تُستعاد.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

log = logging.getLogger(__name__)

EXPORT_VERSION = 1


@dataclass(frozen=True, slots=True)
class DataStats:
    messages: int = 0
    personas: int = 0
    memories: int = 0
    custom_providers: int = 0
    provider_keys: int = 0


@dataclass(slots=True)
class WipeReport:
    messages: int = 0
    memories: int = 0
    personas: int = 0
    custom_providers: int = 0
    provider_keys: int = 0
    errors: list[str] = field(default_factory=list)

    def rows(self) -> list[tuple[str, int]]:
        return [
            ("الرسائل", self.messages),
            ("عناصر الذاكرة", self.memories),
            ("الشخصيات المخصّصة", self.personas),
            ("المزوّدات المخصّصة", self.custom_providers),
            ("مفاتيح API", self.provider_keys),
        ]


class DataService:
    def __init__(
        self,
        *,
        settings_repo: Any,
        conversation: Any,
        personas: Any,
        memories: Any,
        custom_repo: Any,
        key_repo: Any,
        keyring: Any,
        registry: Any,
    ) -> None:
        self._settings = settings_repo
        self._conversation = conversation
        self._personas = personas
        self._memories = memories
        self._custom = custom_repo
        self._keys = key_repo
        self._keyring = keyring
        self._registry = registry

    async def stats(self) -> DataStats:
        return DataStats(
            messages=await self._conversation.count(),
            personas=await self._personas.count(),
            memories=await self._memories.count(),
            custom_providers=await self._custom.count(),
            provider_keys=len(await self._keys.list_all()),
        )

    async def export_json(self) -> bytes:
        payload = {
            "version": EXPORT_VERSION,
            "exported_at": datetime.now(timezone.utc).isoformat(),
            "settings": await self._settings.export(),
            "personas": await self._personas.export(),
            "messages": await self._conversation.export(),
            "memories": await self._memories.export(),
            "custom_providers": await self._custom.export(),
            # بلا قيم: هذا الملف يمرّ بتليجرام
            "provider_keys": await self._keys.export(),
        }
        return json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")

    async def wipe_all(self) -> WipeReport:
        report = WipeReport()

        async def attempt(label: str, coro: Any) -> int:
            try:
                return int(await coro)
            except Exception as exc:
                log.exception("فشل حذف %s", label)
                report.errors.append(f"{label}: {exc}")
                return 0

        report.messages = await attempt("الرسائل", self._conversation.clear())
        report.memories = await attempt("الذاكرة", self._memories.clear())
        report.personas = await attempt("الشخصيات", self._personas.delete_all())
        report.custom_providers = await attempt(
            "المزوّدات المخصّصة", self._custom.delete_all()
        )
        report.provider_keys = await attempt(
            "مفاتيح API", self._keys.delete_all()
        )

        # اللقطات في الذاكرة تبقى تحمل ما حُذف حتى إعادة التشغيل بلا هذا
        try:
            await self._keyring.refresh()
            await self._registry.refresh()
            await self._personas.ensure_default()
            await self._settings.reset()
        except Exception as exc:
            log.exception("فشل إعادة التهيئة بعد الحذف")
            report.errors.append(f"إعادة التهيئة: {exc}")
        return report
