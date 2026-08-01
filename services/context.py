"""حاوية التبعيات. تُعلَّق على عميل Pyrofork فتصل كل الهاندلرات."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(slots=True)
class Context:
    settings: Any                 # config.Settings
    mongo: Any
    redis: Any
    settings_repo: Any
    conversation: Any
    personas: Any
    memories_repo: Any
    custom_repo: Any
    key_repo: Any
    flows: Any
    keyring: Any
    custom: Any                   # CustomRegistry
    models: Any                   # ModelDirectory
    embeddings: Any
    memory: Any                   # MemoryService
    transcription: Any
    search: Any
    chat: Any                     # ChatService
    data: Any                     # DataService
    notifier: Any

    async def bot_settings(self) -> Any:
        return await self.settings_repo.get()
