"""المسار الكامل للمحادثة: الشخصية → الذاكرة → السياق → المزوّد → الحفظ.

كل المدخلات تمرّ من هنا: نصّ، صورة، صوت مُحوَّل. النموذج المُكتشَف
والمنسّق والمخصّص كلها تمرّ بنفس الخطوات بلا فروع خاصة.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, AsyncIterator, Sequence

from database.conversation import ConversationRepository
from database.personas import PersonaRepository
from database.settings import BotSettings
from services.ai_providers import (
    ChatMessage,
    Completion,
    ImagePart,
    ProviderError,
    ToolCall,
    VisionUnsupportedError,
)
from services.catalog import (
    model_label,
    normalize_model,
    resolve_model,
    supports_tools,
    supports_vision,
)
from services.memory import MemoryService
from services.websearch import WEB_SEARCH_TOOL, WebSearchService

log = logging.getLogger(__name__)

MAX_TOOL_ROUNDS = 3   # حلقة أدوات مفتوحة تحرق فاتورتك بلا سقف


@dataclass(slots=True)
class ChatResult:
    text: str
    model_key: str
    model_label: str
    tokens_in: int = 0
    tokens_out: int = 0
    searched: bool = False
    memories_used: int = 0

    @property
    def footer(self) -> str:
        bits = [self.model_label]
        if self.searched:
            bits.append("🔍 بحث")
        if self.memories_used:
            bits.append(f"🧠 {self.memories_used}")
        if self.tokens_in or self.tokens_out:
            bits.append(f"{self.tokens_in}→{self.tokens_out}")
        return " · ".join(bits)


class ChatService:
    def __init__(
        self,
        conversation: ConversationRepository,
        personas: PersonaRepository,
        memory: MemoryService,
        search: WebSearchService,
        *,
        context_messages: int = 20,
        temperature: float = 0.7,
        max_tokens: int = 4096,
    ) -> None:
        self._conversation = conversation
        self._personas = personas
        self._memory = memory
        self._search = search
        self._window = max(2, context_messages)
        self._temperature = temperature
        self._max_tokens = max_tokens

    # ───────────── السياق ─────────────

    async def build_context(
        self,
        prompt: str,
        settings: BotSettings,
        *,
        images: Sequence[ImagePart] = (),
    ) -> tuple[str, list[ChatMessage], int]:
        """يرجع (نصّ النظام، الرسائل، عدد الذكريات المستخدمة)."""
        persona = await self._personas.resolve(settings.selected_persona_id)

        memories_used = 0
        system_parts = [persona.prompt]
        if settings.memory_enabled and self._memory.available:
            memories = await self._memory.retrieve_relevant_memories(prompt)
            if memories:
                memories_used = len(memories)
                system_parts.append(self._memory.as_context_block(memories))

        history = await self._conversation.recent(self._window)
        messages: list[ChatMessage] = [
            ChatMessage(role=item.role, content=item.content)
            for item in history
            if item.content and item.role in ("user", "assistant")
        ]
        messages.append(
            ChatMessage(role="user", content=prompt, images=tuple(images))
        )
        return "\n\n".join(part for part in system_parts if part), messages, memories_used

    # ───────────── الإرسال ─────────────

    async def send(
        self,
        prompt: str,
        settings: BotSettings,
        *,
        images: Sequence[ImagePart] = (),
        media_type: str = "",
        stream_to: Any = None,
    ) -> ChatResult:
        model_key = normalize_model(settings.selected_model)
        resolved = resolve_model(model_key)

        if images and not supports_vision(model_key):
            raise VisionUnsupportedError(
                f"النموذج «{model_label(model_key)}» لا يقرأ الصور. "
                "اختر نموذجًا بصريًا من ⚙️ الإعدادات → 🤖 النموذج.",
                provider=resolved.spec.provider,
                model=resolved.model_id,
            )

        system, messages, memories_used = await self.build_context(
            prompt, settings, images=images
        )

        use_tools = (
            settings.web_search_enabled
            and self._search.available
            and supports_tools(model_key)
        )
        wants_stream = (
            stream_to is not None
            and settings.streaming_enabled
            and resolved.spec.streaming
            and not use_tools           # الأدوات تحتاج الردّ كاملًا أولًا
        )

        if wants_stream:
            result = await self._stream(
                resolved, model_key, system, messages, stream_to
            )
        else:
            result = await self._complete(
                resolved, model_key, system, messages, use_tools=use_tools
            )
        result.memories_used = memories_used

        # الحفظ بعد النجاح فقط: طلب فاشل لا يُلوّث السجلّ
        await self._conversation.append(
            "user", prompt, media_type=media_type
        )
        await self._conversation.append(
            "assistant",
            result.text,
            model_used=model_key,
            tokens_in=result.tokens_in,
            tokens_out=result.tokens_out,
        )
        if settings.memory_enabled:
            self._memory.remember_later("user", prompt)
            self._memory.remember_later("assistant", result.text)
        return result

    async def _complete(
        self,
        resolved: Any,
        model_key: str,
        system: str,
        messages: list[ChatMessage],
        *,
        use_tools: bool,
    ) -> ChatResult:
        tools = (WEB_SEARCH_TOOL,) if use_tools else ()
        searched = False
        tokens_in = 0
        tokens_out = 0
        working = list(messages)

        for round_index in range(MAX_TOOL_ROUNDS):
            completion: Completion = await resolved.provider.generate(
                working,
                resolved.model_id,
                system=system,
                tools=tools,
                temperature=self._temperature,
                max_tokens=self._max_tokens,
            )
            tokens_in += completion.usage.input_tokens
            tokens_out += completion.usage.output_tokens

            if not completion.wants_tools:
                return ChatResult(
                    text=completion.text.strip(),
                    model_key=model_key,
                    model_label=model_label(model_key),
                    tokens_in=tokens_in,
                    tokens_out=tokens_out,
                    searched=searched,
                )

            searched = True
            working.append(
                ChatMessage(
                    role="assistant",
                    content=completion.text,
                    tool_calls=completion.tool_calls,
                )
            )
            for call in completion.tool_calls:
                output = await self._run_tool(call)
                working.append(
                    ChatMessage(
                        role="tool", content=output, tool_call_id=call.call_id
                    )
                )
            log.info("جولة أدوات %s/%s", round_index + 1, MAX_TOOL_ROUNDS)

        # بلغنا السقف: نطلب ردًّا نهائيًا بلا أدوات
        final = await resolved.provider.generate(
            working,
            resolved.model_id,
            system=system,
            temperature=self._temperature,
            max_tokens=self._max_tokens,
        )
        return ChatResult(
            text=final.text.strip(),
            model_key=model_key,
            model_label=model_label(model_key),
            tokens_in=tokens_in + final.usage.input_tokens,
            tokens_out=tokens_out + final.usage.output_tokens,
            searched=searched,
        )

    async def _run_tool(self, call: ToolCall) -> str:
        if call.name == WEB_SEARCH_TOOL.name:
            return await self._search.run_tool(call.arguments)
        return f"أداة غير معروفة: {call.name}"

    async def _stream(
        self,
        resolved: Any,
        model_key: str,
        system: str,
        messages: list[ChatMessage],
        sink: Any,
    ) -> ChatResult:
        chunks: list[str] = []
        async for piece in resolved.provider.stream(
            messages,
            resolved.model_id,
            system=system,
            temperature=self._temperature,
            max_tokens=self._max_tokens,
        ):
            chunks.append(piece)
            await sink.push("".join(chunks))

        text = "".join(chunks).strip()
        if not text:
            raise ProviderError(
                "رجع ردّ فارغ من الخدمة.", provider=resolved.spec.provider
            )
        await sink.finish(text)
        return ChatResult(
            text=text,
            model_key=model_key,
            model_label=model_label(model_key),
        )

    # ───────────── إعادة التعيين ─────────────

    async def reset(self) -> int:
        return await self._conversation.clear()
