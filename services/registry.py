"""سجلّ المزوّدات المخصّصة في الذاكرة.

السبب: `resolve_model()` متزامنة ويُنادى عليها في كل رسالة، وقراءة Mongo
هناك تعني نداءً إضافيًا لكل رد. السجلّ يُحمَّل في الإقلاع ويُحدَّث عند
الإضافة والحذف فقط.
"""
from __future__ import annotations

import logging
from typing import Any

from database.custom_providers import CustomProviderDoc, CustomProviderRepository
from services.ai_providers.custom_provider import (
    CustomConfig,
    CustomProvider,
    build_custom_provider,
)
from services.catalog import ModelSpec

log = logging.getLogger(__name__)


class CustomRegistry:
    def __init__(
        self,
        repo: CustomProviderRepository,
        keyring: Any,
        directory: Any = None,
    ) -> None:
        self._repo = repo
        self._keyring = keyring
        self._directory = directory
        self._docs: dict[str, CustomProviderDoc] = {}
        self._specs: dict[str, ModelSpec] = {}
        self._instances: dict[str, CustomProvider] = {}

    @property
    def repo(self) -> CustomProviderRepository:
        return self._repo

    def __len__(self) -> int:
        return len(self._specs)

    def bind_directory(self, directory: Any) -> None:
        self._directory = directory

    # ───────────── قراءة متزامنة ─────────────

    def specs(self) -> dict[str, ModelSpec]:
        return dict(self._specs)

    def docs_as_specs(self) -> list[ModelSpec]:
        return list(self._specs.values())

    def spec(self, key: str) -> ModelSpec | None:
        return self._specs.get(key)

    def doc(self, key: str) -> CustomProviderDoc | None:
        return self._docs.get(key)

    def docs(self) -> list[CustomProviderDoc]:
        return list(self._docs.values())

    def knows(self, key: str) -> bool:
        return key in self._docs

    def labels(self) -> dict[str, str]:
        return {key: doc.name for key, doc in self._docs.items()}

    def provider(self, key: str) -> CustomProvider | None:
        """يبني العميل عند أول طلب ويُعيد استخدامه (مجمّع اتصالات httpx)."""
        cached = self._instances.get(key)
        if cached is not None:
            return cached
        doc = self._docs.get(key)
        if doc is None:
            return None
        instance = build_custom_provider(self._config(doc), self._keyring)
        self._instances[key] = instance
        return instance

    @staticmethod
    def _config(doc: CustomProviderDoc) -> CustomConfig:
        return CustomConfig(
            key=doc.key,
            name=doc.name,
            base_url=doc.base_url,
            model_name=doc.model_name,
            api_key=doc.api_key,
        )

    # ───────────── التحديث ─────────────

    async def refresh(self) -> int:
        docs = await self._repo.list_all()
        self._docs = {doc.key: doc for doc in docs}
        self._specs = {
            doc.key: ModelSpec(
                key=doc.key,
                label=doc.name,
                provider=doc.key,
                model_id=doc.model_name,
                streaming=True,
                # القدرات مطفأة: التوافق مع OpenAI API شكل طلب لا مجموعة قدرات
                vision=False,
                tools=False,
            )
            for doc in docs
        }
        for key in list(self._instances):
            if key not in self._docs:
                await self._drop_instance(key)
        for doc in docs:
            self._keyring.register_fallback(doc.key, doc.api_key)
        self._keyring.set_labels(self.labels())
        log.info("سجلّ المزوّدات المخصّصة: %s مزوّد", len(self._specs))
        return len(self._specs)

    async def add(
        self, name: str, base_url: str, model_name: str, api_key: str | None = None
    ) -> CustomProviderDoc:
        doc = await self._repo.create(name, base_url, model_name, api_key)
        await self.refresh()
        return doc

    async def remove(self, key: str) -> bool:
        removed = await self._repo.delete(key)
        if not removed:
            return False
        await self._drop_instance(key)
        # مفاتيح مزوّد محذوف تبقى بلا مالك ولا واجهة تصل إليها
        await self._keyring.clear_provider(key)
        if self._directory is not None:
            # قائمة خادم آخر لا تصلح لمزوّد جديد بنفس المفتاح
            await self._directory.drop(key)
        await self.refresh()
        return True

    async def probe(self, key: str) -> tuple[bool, str]:
        instance = self.provider(key)
        if instance is None:
            return False, "المزوّد غير موجود."
        return await instance.probe()

    async def _drop_instance(self, key: str) -> None:
        instance = self._instances.pop(key, None)
        if instance is not None:
            try:
                await instance.close()
            except Exception:
                log.debug("تعذّر إغلاق مزوّد مخصّص", exc_info=True)

    async def close(self) -> None:
        for key in list(self._instances):
            await self._drop_instance(key)
