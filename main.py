"""نقطة الدخول: يبني كل شيء بالترتيب الصحيح ثم يشغّل البوت.

ترتيب الإقلاع ليس اعتباطيًا:
  الإعدادات → Mongo → Redis → التشفير → المستودعات → حلقة المفاتيح →
  طبقة المزوّدات → السجلّ المخصّص → الخدمات → الهاندلرات
حلقة المفاتيح قبل المزوّدات لأن `get_provider` يرفض العمل بدونها.
"""
from __future__ import annotations

import asyncio
import logging
import signal
from typing import Any

from pyrogram import Client, filters

from config import load_settings
from database.conversation import ConversationRepository
from database.custom_providers import CustomProviderRepository
from database.memories import MemoryRepository
from database.mongo import Mongo
from database.personas import PersonaRepository
from database.provider_keys import ProviderKeyRepository
from database.redis_client import RedisClient
from database.settings import SettingsRepository
from handlers import chat as chat_handlers
from handlers import commands as command_handlers
from handlers import flow as flow_handlers
from handlers import menu as menu_handlers
from services import catalog
from services.ai_providers import close_providers, init_providers, set_custom_registry
from services.chat import ChatService
from services.context import Context
from services.crypto import ContentCipher
from services.dataops import DataService
from services.embeddings import EmbeddingService
from services.flows import FlowStore
from services.keyring import KeyRing
from services.memory import MemoryService
from services.model_directory import ModelDirectory
from services.notifier import Notifier
from services.registry import CustomRegistry
from services.transcription import TranscriptionService
from services.websearch import WebSearchService
from utils.logging_setup import attach_notifier, setup_logging

log = logging.getLogger("main")

SESSION_NAME = "bot"


async def run() -> None:
    settings = load_settings()
    setup_logging(settings.log_level)
    log.info("بدء التشغيل — %s", settings.summary())

    app = Client(
        SESSION_NAME,
        api_id=settings.api_id,
        api_hash=settings.api_hash,
        bot_token=settings.bot_token,
        parse_mode=__import__(
            "pyrogram.enums", fromlist=["ParseMode"]
        ).ParseMode.HTML,
        in_memory=False,
        workdir=".",
    )

    mongo = Mongo(settings.mongo_uri, settings.mongo_db)
    redis = RedisClient(settings.redis_url)
    notifier: Notifier | None = None
    handler = None

    try:
        await mongo.connect()
        await redis.connect()

        # ── التشفير ──
        cipher = ContentCipher(settings.content_encryption_key)

        # ── المستودعات ──
        settings_repo = SettingsRepository(
            mongo, redis,
            default_model=settings.default_model,
            default_strategy=settings.key_strategy,
            default_streaming=settings.streaming,
        )
        conversation = ConversationRepository(
            mongo, redis, window=settings.context_messages
        )
        personas = PersonaRepository(mongo)
        memories_repo = MemoryRepository(
            mongo,
            vector_index=settings.memory_vector_index,
            max_scan=settings.memory_max_scan,
        )
        custom_repo = CustomProviderRepository(mongo, cipher)
        key_repo = ProviderKeyRepository(mongo, cipher)
        flows = FlowStore(mongo)

        for repo in (conversation, personas, memories_repo, custom_repo, key_repo):
            await repo.ensure_indexes()
        bot_settings = await settings_repo.ensure()
        await personas.ensure_default()
        if not bot_settings.selected_persona_id:
            default_persona = await personas.ensure_default()
            bot_settings = await settings_repo.set_persona(
                default_persona.persona_id
            )

        # ── التنبيهات والسجلّ ──
        notifier = Notifier(app, settings.owner_id)
        handler = attach_notifier(notifier)

        # ── حلقة المفاتيح: قبل أي مزوّد ──
        keyring = KeyRing(
            key_repo,
            strategy=bot_settings.key_strategy or settings.key_strategy,
            threshold=settings.key_failure_threshold,
            env_keys=settings.env_provider_keys,
            notifier=notifier,
        )
        await keyring.refresh()

        # ── طبقة المزوّدات ──
        init_providers(keyring)

        # ── كاش النماذج ──
        directory = ModelDirectory(redis, fresh_ttl=settings.model_cache_ttl)

        # ── المزوّدات المخصّصة ──
        registry = CustomRegistry(custom_repo, keyring, directory)
        await registry.refresh()
        catalog.register_custom(registry)
        set_custom_registry(registry)

        if settings.key_seed_from_env:
            seeded = await keyring.seed_from_env()
            if seeded:
                log.info("بُذرت %s مفاتيح — Mongo صار المصدر", seeded)

        if not key_repo.encrypted and await key_repo.list_all():
            log.critical(
                "مفاتيح API مخزَّنة نصًا صريحًا في Mongo — "
                "اضبط CONTENT_ENCRYPTION_KEY فورًا"
            )

        # ── الخدمات ──
        embeddings = EmbeddingService(
            base_url=settings.embedding_base_url,
            api_key=settings.embedding_api_key,
            model=settings.embedding_model,
            enabled=settings.memory_available,
        )
        memory = MemoryService(
            memories_repo, embeddings,
            top_k=settings.memory_top_k,
            min_score=settings.memory_min_score,
        )
        transcription = TranscriptionService(
            base_url=settings.whisper_base_url,
            api_key=settings.whisper_api_key,
            model=settings.whisper_model,
            enabled=settings.transcription_available,
        )
        search = WebSearchService(
            provider=settings.active_search_provider,
            serper_key=settings.serper_api_key,
            brave_key=settings.brave_api_key,
            results=settings.search_results,
        )
        chat = ChatService(
            conversation, personas, memory, search,
            context_messages=settings.context_messages,
            temperature=settings.temperature,
            max_tokens=settings.max_output_tokens,
        )
        data = DataService(
            settings_repo=settings_repo,
            conversation=conversation,
            personas=personas,
            memories=memories_repo,
            custom_repo=custom_repo,
            key_repo=key_repo,
            keyring=keyring,
            registry=registry,
        )

        app.ctx = Context(
            settings=settings,
            mongo=mongo,
            redis=redis,
            settings_repo=settings_repo,
            conversation=conversation,
            personas=personas,
            memories_repo=memories_repo,
            custom_repo=custom_repo,
            key_repo=key_repo,
            flows=flows,
            keyring=keyring,
            custom=registry,
            models=directory,
            embeddings=embeddings,
            memory=memory,
            transcription=transcription,
            search=search,
            chat=chat,
            data=data,
            notifier=notifier,
        )

        # ── الهاندلرات ──
        # الترتيب: الأوامر (0) → الإدخال المعلَّق (1) → المحادثة (2)
        owner_only = filters.user(settings.owner_id)
        command_handlers.register(app, owner_only)
        flow_handlers.register(app, owner_only)
        chat_handlers.register(app, owner_only)
        menu_handlers.register_model_picker(app, owner_only)
        menu_handlers.register(app, owner_only)

        await app.start()
        try:
            await app.set_bot_commands(command_handlers.COMMANDS)
        except Exception:
            log.warning("تعذّر ضبط قائمة الأوامر", exc_info=True)

        me = await app.get_me()
        log.info("البوت يعمل: @%s (المالك %s)", me.username, settings.owner_id)
        await notifier.send(
            "🟢 <b>البوت يعمل</b>\n"
            f"النموذج: <code>{catalog.model_label(catalog.normalize_model(bot_settings.selected_model))}</code>\n"
            f"المفاتيح: <code>{keyring.total_keys()}</code> · "
            f"الذاكرة: {'🟢' if memory.available else '⚪️'} · "
            f"البحث: {'🟢' if search.available else '⚪️'}",
            tag="startup",
            cooldown=0,
        )

        await _wait_for_stop()
        log.info("إشارة إيقاف — جاري الإغلاق")

    except Exception:
        log.critical("فشل الإقلاع", exc_info=True)
        raise
    finally:
        if handler is not None:
            logging.getLogger().removeHandler(handler)
        for closer in (
            close_providers(),
            registry.close() if "registry" in dir() else None,
            embeddings.close() if "embeddings" in dir() else None,
            transcription.close() if "transcription" in dir() else None,
            search.close() if "search" in dir() else None,
            redis.close(),
            mongo.close(),
        ):
            if closer is None:
                continue
            try:
                await closer
            except Exception:
                log.debug("تعذّر إغلاق مورد", exc_info=True)
        try:
            if app.is_connected:
                await app.stop()
        except Exception:
            log.debug("تعذّر إيقاف عميل تليجرام", exc_info=True)
        log.info("توقّف البوت")


async def _wait_for_stop() -> None:
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for name in ("SIGINT", "SIGTERM"):
        sig = getattr(signal, name, None)
        if sig is None:
            continue
        try:
            loop.add_signal_handler(sig, stop.set)
        except (NotImplementedError, RuntimeError):
            pass   # Windows لا يدعمه
    await stop.wait()


if __name__ == "__main__":
    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        pass
