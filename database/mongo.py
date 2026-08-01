"""عميل MongoDB. مجموعة واحدة لكل غرض، بلا user_id: البوت لمالك واحد."""
from __future__ import annotations

import logging
from typing import Any

from motor.motor_asyncio import AsyncIOMotorClient, AsyncIOMotorDatabase

log = logging.getLogger(__name__)


class Mongo:
    def __init__(self, uri: str, database: str) -> None:
        self._uri = uri
        self._name = database
        self._client: AsyncIOMotorClient | None = None
        self._db: AsyncIOMotorDatabase | None = None

    async def connect(self) -> None:
        self._client = AsyncIOMotorClient(
            self._uri,
            serverSelectionTimeoutMS=8000,
            connectTimeoutMS=8000,
            tz_aware=True,
        )
        # ping صريح: بلا نداء فعلي لا يظهر خطأ الاتصال إلا عند أول استعلام
        await self._client.admin.command("ping")
        self._db = self._client[self._name]
        log.info("MongoDB متصل — قاعدة %s", self._name)

    @property
    def db(self) -> AsyncIOMotorDatabase:
        if self._db is None:
            raise RuntimeError("MongoDB غير متصل — نادِ connect() أولًا")
        return self._db

    def collection(self, name: str) -> Any:
        return self.db[name]

    async def collection_names(self) -> list[str]:
        return await self.db.list_collection_names()

    async def close(self) -> None:
        if self._client is not None:
            self._client.close()
            self._client = None
            self._db = None
            log.info("أُغلق اتصال MongoDB")
