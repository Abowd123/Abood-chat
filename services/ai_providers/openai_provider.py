"""مزوّد OpenAI، وأساس كل ما هو متوافق مع OpenAI API."""
from __future__ import annotations

import json
import logging
from typing import Any, AsyncIterator, Mapping, Sequence

from services.ai_providers.base import (
    ChatMessage,
    Completion,
    KeyedProvider,
    ToolCall,
    ToolSpec,
    Usage,
)
from services.ai_providers.errors import EmptyResponseError
from services.model_directory import ModelInfo, family_of

log = logging.getLogger(__name__)


class OpenAIProvider(KeyedProvider):
    name = "openai"
    key_provider = "openai"
    supports_streaming = True
    supports_tools = True

    base_url: str | None = None
    timeout: float = 120.0

    def __init__(self, keyring: Any) -> None:
        super().__init__(keyring)
        import openai

        self._sdk = openai

    def _build_client(self, credential: Any) -> Any:
        return self._sdk.AsyncOpenAI(
            api_key=credential.value or "not-required",
            base_url=self.base_url,
            timeout=self.timeout,
            # إعادة المحاولة عندنا: تكرار الـ SDK على نفس المفتاح يؤخّر التبديل
            max_retries=0,
        )

    # ───────────── التسلسل ─────────────

    @staticmethod
    def _content(message: ChatMessage) -> Any:
        if not message.images:
            return message.content
        blocks: list[dict[str, Any]] = []
        if message.content:
            blocks.append({"type": "text", "text": message.content})
        for image in message.images:
            blocks.append(
                {"type": "image_url", "image_url": {"url": image.data_url}}
            )
        return blocks

    @classmethod
    def _serialize(cls, message: ChatMessage) -> dict[str, Any]:
        if message.role == "tool":
            return {
                "role": "tool",
                "tool_call_id": message.tool_call_id,
                "content": message.content or "",
            }
        payload: dict[str, Any] = {
            "role": message.role,
            "content": cls._content(message),
        }
        if message.tool_calls:
            payload["content"] = message.content or None
            payload["tool_calls"] = [
                {
                    "id": call.call_id,
                    "type": "function",
                    "function": {
                        "name": call.name,
                        "arguments": json.dumps(call.arguments, ensure_ascii=False),
                    },
                }
                for call in message.tool_calls
            ]
        return payload

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
        rows: list[dict[str, Any]] = []
        if system:
            rows.append({"role": "system", "content": system})
        rows.extend(self._serialize(message) for message in messages)

        payload: dict[str, Any] = {"model": model, "messages": rows}
        payload |= self._params(temperature=temperature, max_tokens=max_tokens)
        if tools:
            payload["tools"] = [tool.as_openai() for tool in tools]
            payload["tool_choice"] = "auto"
        return payload

    def _params(
        self, *, temperature: float | None, max_tokens: int | None
    ) -> dict[str, Any]:
        params: dict[str, Any] = {}
        if temperature is not None:
            params["temperature"] = float(temperature)
        if max_tokens is not None:
            params["max_tokens"] = int(max_tokens)
        return params

    # ───────────── التحليل ─────────────

    @staticmethod
    def _parse_tool_calls(message: Any) -> tuple[ToolCall, ...]:
        raw_calls = getattr(message, "tool_calls", None) or []
        calls: list[ToolCall] = []
        for raw in raw_calls:
            function = getattr(raw, "function", None)
            if function is None:
                continue
            try:
                arguments = json.loads(getattr(function, "arguments", "") or "{}")
            except (ValueError, TypeError):
                arguments = {}
            if not isinstance(arguments, dict):
                arguments = {}
            calls.append(
                ToolCall(
                    call_id=str(getattr(raw, "id", "") or ""),
                    name=str(getattr(function, "name", "") or ""),
                    arguments=arguments,
                )
            )
        return tuple(calls)

    def _validate_response(self, response: Any, model: str) -> Any:
        choices = getattr(response, "choices", None)
        if not choices:
            raise EmptyResponseError(provider=self.name, model=model)
        choice = choices[0]
        if getattr(choice, "message", None) is None:
            raise EmptyResponseError(provider=self.name, model=model)
        return choice

    def _parse(self, response: Any, model: str) -> Completion:
        choice = self._validate_response(response, model)
        message = choice.message
        usage_raw = getattr(response, "usage", None)
        usage = Usage(
            input_tokens=int(getattr(usage_raw, "prompt_tokens", 0) or 0),
            output_tokens=int(getattr(usage_raw, "completion_tokens", 0) or 0),
        )
        calls = self._parse_tool_calls(message)
        text = str(getattr(message, "content", "") or "")
        if not text and not calls:
            raise EmptyResponseError(provider=self.name, model=model)
        return Completion(
            text=text,
            usage=usage,
            tool_calls=calls,
            model=str(getattr(response, "model", "") or model),
            finish_reason=str(getattr(choice, "finish_reason", "") or ""),
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
            lambda client: client.chat.completions.create(**payload), model
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
        ) | {"stream": True}

        def factory(client: Any) -> AsyncIterator[str]:
            return self._deltas(client.chat.completions.create(**payload))

        async for chunk in self._run_stream(factory, model):
            yield chunk

    @staticmethod
    async def _deltas(awaitable_stream: Any) -> AsyncIterator[str]:
        stream = await awaitable_stream
        async for event in stream:
            choices = getattr(event, "choices", None) or []
            if not choices:
                continue
            delta = getattr(choices[0], "delta", None)
            piece = getattr(delta, "content", None) if delta else None
            if piece:
                yield piece

    # ───────────── قائمة النماذج ─────────────

    async def _fetch_models(self, client: Any) -> list[ModelInfo]:
        listing = await client.models.list()
        rows = getattr(listing, "data", None) or []
        models: list[ModelInfo] = []
        for row in rows:
            model_id = str(getattr(row, "id", "") or "")
            if not model_id:
                continue
            models.append(
                ModelInfo(
                    id=model_id,
                    family=family_of(model_id),
                    created=getattr(row, "created", None),
                )
            )
        models.sort(key=lambda item: item.id)
        return models
