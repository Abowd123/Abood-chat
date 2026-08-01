"""مزوّد Anthropic. بروتوكوله مختلف، لكن حلقة المفاتيح واحدة."""
from __future__ import annotations

import logging
from typing import Any, AsyncIterator, Sequence

from services.ai_providers.base import (
    ChatMessage,
    Completion,
    KeyedProvider,
    ToolCall,
    ToolSpec,
    Usage,
)
from services.ai_providers.errors import EmptyResponseError, ModelListingUnsupported
from services.model_directory import ModelInfo, family_of

log = logging.getLogger(__name__)

DEFAULT_MAX_TOKENS = 4096   # Anthropic يطلبه إلزاميًا


class AnthropicProvider(KeyedProvider):
    name = "anthropic"
    key_provider = "anthropic"
    supports_streaming = True
    supports_tools = True
    timeout: float = 120.0

    def __init__(self, keyring: Any) -> None:
        super().__init__(keyring)
        import anthropic

        self._sdk = anthropic

    def _build_client(self, credential: Any) -> Any:
        return self._sdk.AsyncAnthropic(
            api_key=credential.value or "not-required",
            timeout=self.timeout,
            max_retries=0,
        )

    # ───────────── التسلسل ─────────────

    @staticmethod
    def _blocks(message: ChatMessage) -> list[dict[str, Any]]:
        blocks: list[dict[str, Any]] = []
        for image in message.images:
            blocks.append(
                {
                    "type": "image",
                    "source": {
                        "type": "base64",
                        "media_type": image.media_type,
                        "data": image.data,
                    },
                }
            )
        if message.content:
            blocks.append({"type": "text", "text": message.content})
        for call in message.tool_calls:
            blocks.append(
                {
                    "type": "tool_use",
                    "id": call.call_id,
                    "name": call.name,
                    "input": call.arguments,
                }
            )
        return blocks or [{"type": "text", "text": ""}]

    @classmethod
    def _serialize(cls, message: ChatMessage) -> dict[str, Any]:
        if message.role == "tool":
            # نتيجة الأداة تُرسل كرسالة user تحمل كتلة tool_result
            return {
                "role": "user",
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": message.tool_call_id,
                        "content": message.content or "",
                    }
                ],
            }
        role = "assistant" if message.role == "assistant" else "user"
        return {"role": role, "content": cls._blocks(message)}

    def _payload(
        self,
        messages: Sequence[ChatMessage],
        model: str,
        *,
        system: str,
        tools: Sequence[ToolSpec],
        temperature: float | None,
        max_tokens: int | None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "model": model,
            "max_tokens": int(max_tokens or DEFAULT_MAX_TOKENS),
            "messages": [
                self._serialize(message)
                for message in messages
                if message.role != "system"
            ],
        }
        # النظام معامل مستقلّ عند Anthropic لا رسالة في القائمة
        merged_system = "\n\n".join(
            [system, *[m.content for m in messages if m.role == "system" and m.content]]
        ).strip()
        if merged_system:
            payload["system"] = merged_system
        if temperature is not None:
            payload["temperature"] = float(temperature)
        if tools:
            payload["tools"] = [tool.as_anthropic() for tool in tools]
        return payload

    # ───────────── التحليل ─────────────

    def _parse(self, response: Any, model: str) -> Completion:
        blocks = getattr(response, "content", None) or []
        texts: list[str] = []
        calls: list[ToolCall] = []
        for block in blocks:
            kind = getattr(block, "type", "")
            if kind == "text":
                texts.append(str(getattr(block, "text", "") or ""))
            elif kind == "tool_use":
                arguments = getattr(block, "input", None)
                calls.append(
                    ToolCall(
                        call_id=str(getattr(block, "id", "") or ""),
                        name=str(getattr(block, "name", "") or ""),
                        arguments=arguments if isinstance(arguments, dict) else {},
                    )
                )

        text = "".join(texts).strip()
        if not text and not calls:
            raise EmptyResponseError(provider=self.name, model=model)

        usage_raw = getattr(response, "usage", None)
        usage = Usage(
            input_tokens=int(getattr(usage_raw, "input_tokens", 0) or 0),
            output_tokens=int(getattr(usage_raw, "output_tokens", 0) or 0),
        )
        return Completion(
            text=text,
            usage=usage,
            tool_calls=tuple(calls),
            model=str(getattr(response, "model", "") or model),
            finish_reason=str(getattr(response, "stop_reason", "") or ""),
        )

    # ───────────── الواجهة ─────────────

    async def generate(
        self,
        messages: Sequence[ChatMessage],
        model: str,
        *,
        system: str = "",
        tools: Sequence[ToolSpec] = (),
        temperature: float | None = None,
        max_tokens: int | None = None,
    ) -> Completion:
        payload = self._payload(
            messages, model, system=system, tools=tools,
            temperature=temperature, max_tokens=max_tokens,
        )
        response = await self._run(
            lambda client: client.messages.create(**payload), model
        )
        return self._parse(response, model)

    async def stream(
        self,
        messages: Sequence[ChatMessage],
        model: str,
        *,
        system: str = "",
        temperature: float | None = None,
        max_tokens: int | None = None,
    ) -> AsyncIterator[str]:
        payload = self._payload(
            messages, model, system=system, tools=(),
            temperature=temperature, max_tokens=max_tokens,
        )

        def factory(client: Any) -> AsyncIterator[str]:
            return self._deltas(client, payload)

        async for chunk in self._run_stream(factory, model):
            yield chunk

    @staticmethod
    async def _deltas(client: Any, payload: dict[str, Any]) -> AsyncIterator[str]:
        async with client.messages.stream(**payload) as stream:
            async for piece in stream.text_stream:
                if piece:
                    yield piece

    # ───────────── قائمة النماذج ─────────────

    async def _fetch_models(self, client: Any) -> list[ModelInfo]:
        endpoint = getattr(client, "models", None)
        if endpoint is None or not hasattr(endpoint, "list"):
            raise ModelListingUnsupported(
                "نسخة مكتبة anthropic لا توفّر قائمة نماذج. "
                "استخدم ⭐ المختارة أو حدّث المكتبة.",
                provider=self.name,
            )
        try:
            listing = await endpoint.list(limit=100)
        except Exception as exc:
            if getattr(exc, "status_code", None) == 404:
                raise ModelListingUnsupported(provider=self.name) from exc
            raise

        models: list[ModelInfo] = []
        for row in getattr(listing, "data", None) or []:
            model_id = str(getattr(row, "id", "") or "")
            if not model_id:
                continue
            models.append(
                ModelInfo(
                    id=model_id,
                    label=str(getattr(row, "display_name", "") or ""),
                    family=family_of(model_id),
                )
            )
        models.sort(key=lambda item: item.id, reverse=True)   # الأحدث أولًا
        return models
