"""مستند إعدادات واحد لكل البوت، مع كاش Redis.

`_id` ثابت لأن المالك واحد: لا حاجة لمفتاح مستخدم في أي مكان.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Mapping

from database.mongo import Mongo
from database.redis_client import RedisClient

log = logging.getLogger(__name__)

COLLECTION = "settings"
SETTINGS_ID = "global"
CACHE_KEY = "settings:global"
CACHE_TTL = 300
MAX_REMEMBERED_CAPS = 60


@dataclass(frozen=True, slots=True)
class ModelCap:
    """قدرات نموذج مُكتشَف، محفوظة عند اختياره لا مخمّنة."""

    key: str
    label: str = ""
    vision: bool = False
    tools: bool = False
    context: int | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "label": self.label,
            "vision": self.vision,
            "tools": self.tools,
            "context": self.context,
        }

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "ModelCap":
        return cls(
            key=str(raw.get("key") or ""),
            label=str(raw.get("label") or ""),
            vision=bool(raw.get("vision")),
            tools=bool(raw.get("tools")),
            context=raw.get("context"),
        )


@dataclass(frozen=True, slots=True)
class BotSettings:
    selected_model: str = ""
    selected_provider: str = ""
    selected_persona_id: str = ""
    memory_enabled: bool = True
    web_search_enabled: bool = False
    streaming_enabled: bool = True
    key_strategy: str = "round_robin"
    model_show_all: bool = False
    model_caps: tuple[ModelCap, ...] = ()

    def cap(self, key: str) -> ModelCap | None:
        return next((item for item in self.model_caps if item.key == key), None)

    def to_dict(self) -> dict[str, Any]:
        return {
            "selected_model": self.selected_model,
            "selected_provider": self.selected_provider,
            "selected_persona_id": self.selected_persona_id,
            "memory_enabled": self.memory_enabled,
            "web_search_enabled": self.web_search_enabled,
            "streaming_enabled": self.streaming_enabled,
            "key_strategy": self.key_strategy,
            "model_show_all": self.model_show_all,
            "model_caps": [item.to_dict() for item in self.model_caps],
        }

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any] | None) -> "BotSettings":
        raw = raw or {}
        caps = tuple(
            ModelCap.from_dict(item)
            for item in (raw.get("model_caps") or [])
            if isinstance(item, Mapping) and item.get("key")
        )
        return cls(
            selected_model=str(raw.get("selected_model") or ""),
            selected_provider=str(raw.get("selected_provider") or ""),
            selected_persona_id=str(raw.get("selected_persona_id") or ""),
            memory_enabled=bool(raw.get("memory_enabled", True)),
            web_search_enabled=bool(raw.get("web_search_enabled", False)),
            streaming_enabled=bool(raw.get("streaming_enabled", True)),
            key_strategy=str(raw.get("key_strategy") or "round_robin"),
            model_show_all=bool(raw.get("model_show_all", False)),
            model_caps=caps,
        )


class SettingsRepository:
    def __init__(
        self,
        mongo: Mongo,
        redis: RedisClient,
        *,
        default_model: str = "",
        default_strategy: str = "round_robin",
        default_streaming: bool = True,
    ) -> None:
        self._mongo = mongo
        self._redis = redis
        self._defaults = {
            "selected_model": default_model,
            "selected_provider": "",
            "selected_persona_id": "",
            "memory_enabled": True,
            "web_search_enabled": False,
            "streaming_enabled": default_streaming,
            "key_strategy": default_strategy,
            "model_show_all": False,
            "model_caps": [],
        }
        self._cached: BotSettings | None = None

    @property
    def _col(self) -> Any:
        return self._mongo.collection(COLLECTION)

    async def ensure(self) -> BotSettings:
        """ينشئ المستند إن لم يوجد، بلا كتابة فوق ما هو موجود."""
        await self._col.update_one(
            {"_id": SETTINGS_ID}, {"$setOnInsert": self._defaults}, upsert=True
        )
        return await self.get(force=True)

    async def get(self, *, force: bool = False) -> BotSettings:
        if not force and self._cached is not None:
            return self._cached
        if not force:
            cached = await self._redis.get_json(CACHE_KEY)
            if isinstance(cached, dict):
                self._cached = BotSettings.from_dict(cached)
                self._sync_catalog(self._cached)
                return self._cached

        doc = await self._col.find_one({"_id": SETTINGS_ID})
        settings = BotSettings.from_dict(doc)
        if not settings.selected_model:
            settings = BotSettings.from_dict(
                {**(doc or {}), "selected_model": self._defaults["selected_model"]}
            )
        self._cached = settings
        await self._redis.set_json(CACHE_KEY, settings.to_dict(), ttl=CACHE_TTL)
        self._sync_catalog(settings)
        return settings

    @staticmethod
    def _sync_catalog(settings: BotSettings) -> None:
        """يُبقي قدرات النماذج المُكتشَفة متاحة للفحص المتزامن في catalog."""
        from services import catalog

        catalog.remember_caps(settings.model_caps)

    async def _invalidate(self) -> None:
        self._cached = None
        await self._redis.delete(CACHE_KEY)

    async def _apply(self, updates: dict[str, Any]) -> BotSettings:
        await self._col.update_one(
            {"_id": SETTINGS_ID}, {"$set": updates}, upsert=True
        )
        await self._invalidate()
        return await self.get(force=True)

    # ---- النموذج ----

    async def set_selection(
        self, model_key: str, provider: str, cap: ModelCap | None = None
    ) -> BotSettings:
        """يكتب النموذج والخدمة معًا: لا يمكن أن يتضاربا.

        `selected_model` هو الحاكم؛ `selected_provider` مشتقّ منه للعرض.
        """
        from services.catalog import is_valid_model

        if not is_valid_model(model_key):
            raise ValueError(f"نموذج غير صالح: {model_key!r}")

        updates: dict[str, Any] = {
            "selected_model": model_key,
            "selected_provider": provider,
        }
        if cap is not None:
            current = await self.get()
            remaining = [
                item.to_dict() for item in current.model_caps if item.key != cap.key
            ]
            updates["model_caps"] = [cap.to_dict(), *remaining][:MAX_REMEMBERED_CAPS]
        return await self._apply(updates)

    async def set_model(self, model_key: str) -> BotSettings:
        from services.catalog import provider_of

        return await self.set_selection(model_key, provider_of(model_key))

    async def update_cap(self, cap: ModelCap) -> BotSettings:
        current = await self.get()
        remaining = [
            item.to_dict() for item in current.model_caps if item.key != cap.key
        ]
        return await self._apply(
            {"model_caps": [cap.to_dict(), *remaining][:MAX_REMEMBERED_CAPS]}
        )

    async def set_model_show_all(self, value: bool) -> BotSettings:
        return await self._apply({"model_show_all": bool(value)})

    # ---- بقية المفاتيح ----

    async def set_persona(self, persona_id: str) -> BotSettings:
        return await self._apply({"selected_persona_id": persona_id})

    async def set_memory(self, enabled: bool) -> BotSettings:
        return await self._apply({"memory_enabled": bool(enabled)})

    async def set_web_search(self, enabled: bool) -> BotSettings:
        return await self._apply({"web_search_enabled": bool(enabled)})

    async def set_streaming(self, enabled: bool) -> BotSettings:
        return await self._apply({"streaming_enabled": bool(enabled)})

    async def set_key_strategy(self, strategy: str) -> BotSettings:
        if strategy not in ("round_robin", "random"):
            raise ValueError(f"استراتيجية غير صالحة: {strategy!r}")
        return await self._apply({"key_strategy": strategy})

    async def reset(self) -> BotSettings:
        await self._col.update_one(
            {"_id": SETTINGS_ID}, {"$set": self._defaults}, upsert=True
        )
        await self._invalidate()
        log.info("أُعيدت الإعدادات إلى الافتراضي")
        return await self.get(force=True)

    async def export(self) -> dict[str, Any]:
        return (await self.get(force=True)).to_dict()
