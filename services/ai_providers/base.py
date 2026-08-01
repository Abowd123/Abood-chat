"""العقود المشتركة، وحلقة تبديل المفاتيح في مكان واحد.

`KeyedProvider._run` هي المكان **الوحيد** الذي يعرف
«اختر مفتاحًا → نفّذ → عند فشل المفتاح جرّب التالي».
كل مزوّد يقدّم شيئين فقط: كيف يبني عميلًا من مفتاح، وما اسمه في provider_keys.
"""
from __future__ import annotations

import asyncio
import logging
from abc import ABC, abstractmethod
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Any, AsyncIterator, Awaitable, Callable, ClassVar, Sequence

from services.ai_providers.errors import (
    ModelListingUnsupported,
    ProviderError,
    translate_sdk_error,
)

log = logging.getLogger(__name__)

MAX_CLIENTS = 16   # عميل لكل مفتاح: الـ SDK يجمّد المفتاح في بانيه


# ───────────────────────── العقود ─────────────────────────

@dataclass(frozen=True, slots=True)
class ImagePart:
    media_type: str
    data: str            # base64 بلا بادئة data:

    @property
    def data_url(self) -> str:
        return f"data:{self.media_type};base64,{self.data}"


@dataclass(frozen=True, slots=True)
class ToolCall:
    call_id: str
    name: str
    arguments: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class ToolSpec:
    name: str
    description: str
    parameters: dict[str, Any]

    def as_openai(self) -> dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters,
            },
        }

    def as_anthropic(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "input_schema": self.parameters,
        }


@dataclass(frozen=True, slots=True)
class ChatMessage:
    role: str                                   # system | user | assistant | tool
    content: str = ""
    images: tuple[ImagePart, ...] = ()
    tool_calls: tuple[ToolCall, ...] = ()
    tool_call_id: str = ""


@dataclass(frozen=True, slots=True)
class Usage:
    input_tokens: int = 0
    output_tokens: int = 0

    @property
    def total(self) -> int:
        return self.input_tokens + self.output_tokens


@dataclass(frozen=True, slots=True)
class Completion:
    text: str
    usage: Usage = field(default_factory=Usage)
    tool_calls: tuple[ToolCall, ...] = ()
    model: str = ""
    finish_reason: str = ""

    @property
    def wants_tools(self) -> bool:
        return bool(self.tool_calls)


# ───────────────────────── المزوّد المجرّد ─────────────────────────

class BaseProvider(ABC):
    name: ClassVar[str] = "base"
    supports_streaming: ClassVar[bool] = False
    supports_tools: ClassVar[bool] = False

    @abstractmethod
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
        """ردّ واحد كامل."""

    async def stream(
        self,
        messages: Sequence[ChatMessage],
        model: str,
        *,
        system: str = "",
        temperature: float | None = None,
        max_tokens: int | None = None,
    ) -> AsyncIterator[str]:
        """أجزاء الردّ تدريجيًا. المزوّد الذي لا يدعم البثّ يرفع الخطأ."""
        raise ProviderError(
            f"«{self.name}» لا يدعم البثّ.", provider=self.name
        )
        yield ""   # pragma: no cover — يجعل الدالة مولّدًا

    async def list_models(self) -> list[Any]:
        """قائمة النماذج من الخدمة."""
        raise ModelListingUnsupported(provider=self.name)

    async def close(self) -> None:
        return None


# ───────────────────────── حلقة المفاتيح ─────────────────────────

class KeyedProvider(BaseProvider):
    key_provider: ClassVar[str] = ""
    allow_keyless: ClassVar[bool] = False

    def __init__(self, keyring: Any) -> None:
        self._keyring = keyring
        self._clients: OrderedDict[str, Any] = OrderedDict()

    # ---- ما يقدّمه الفرعي ----

    def _build_client(self, credential: Any) -> Any:
        raise NotImplementedError

    async def _fetch_models(self, client: Any) -> list[Any]:
        raise ModelListingUnsupported(provider=self.name)

    # ---- مخزن العملاء ----

    def _client_for(self, credential: Any) -> Any:
        cache_key = credential.cache_key
        client = self._clients.get(cache_key)
        if client is not None:
            self._clients.move_to_end(cache_key)
            return client
        client = self._build_client(credential)
        self._clients[cache_key] = client
        while len(self._clients) > MAX_CLIENTS:
            _, evicted = self._clients.popitem(last=False)
            self._schedule_close(evicted)
        return client

    @staticmethod
    def _schedule_close(client: Any) -> None:
        closer = getattr(client, "close", None)
        if closer is None:
            return
        try:
            asyncio.get_running_loop().create_task(closer())
        except Exception:
            log.debug("تعذّر إغلاق عميل مُستبعَد", exc_info=True)

    async def close(self) -> None:
        for client in list(self._clients.values()):
            closer = getattr(client, "close", None)
            if closer is None:
                continue
            try:
                await closer()
            except Exception:
                log.debug("تعذّر إغلاق عميل %s", self.name, exc_info=True)
        self._clients.clear()

    # ---- الترجمة ----

    def _translate(self, exc: BaseException, model: str) -> ProviderError:
        return translate_sdk_error(exc, provider=self.name, model=model)

    def _candidates(self) -> list[Any]:
        return self._keyring.candidates(
            self.key_provider, allow_keyless=self.allow_keyless
        )

    # ---- الحلقة: نداء واحد ----

    async def _run(self, factory: Callable[[Any], Awaitable[Any]], model: str) -> Any:
        from services.keyring import is_key_error

        candidates = self._candidates()
        if not candidates:
            raise self._keyring.missing_key_error(self.key_provider)

        last: ProviderError | None = None
        total = len(candidates)
        for index, credential in enumerate(candidates):
            try:
                result = await factory(self._client_for(credential))
            except Exception as exc:
                error = self._translate(exc, model)
                if not is_key_error(error):
                    raise error          # عطل المزوّد: لا تحرق بقية المفاتيح
                await self._keyring.report_failure(credential, error)
                last = error
                if index + 1 >= total:
                    raise error
                log.info(
                    "تبديل مفتاح %s: %s → المحاولة %s/%s",
                    self.key_provider, type(error).__name__, index + 2, total,
                )
                continue
            await self._keyring.report_success(credential)
            return result

        raise last or self._keyring.missing_key_error(self.key_provider)

    # ---- الحلقة: بثّ ----

    async def _run_stream(
        self, factory: Callable[[Any], AsyncIterator[str]], model: str
    ) -> AsyncIterator[str]:
        """التبديل ممكن حتى أول قطعة فقط.

        بعد إرسال أول جزء للمستخدم، التبديل يعني ردًّا مشوّهًا يخلط
        مخرجات مفتاحين. عمليًا 401 و429 يصلان قبل أول بايت.
        """
        from services.keyring import is_key_error

        candidates = self._candidates()
        if not candidates:
            raise self._keyring.missing_key_error(self.key_provider)

        last: ProviderError | None = None
        total = len(candidates)
        for index, credential in enumerate(candidates):
            try:
                stream = factory(self._client_for(credential)).__aiter__()
                first = await stream.__anext__()
            except StopAsyncIteration:
                await self._keyring.report_success(credential)
                return
            except Exception as exc:
                error = self._translate(exc, model)
                if not is_key_error(error):
                    raise error
                await self._keyring.report_failure(credential, error)
                last = error
                if index + 1 >= total:
                    raise error
                continue

            await self._keyring.report_success(credential)
            if first:
                yield first
            async for chunk in stream:      # ما بعد أول قطعة: بلا تبديل
                if chunk:
                    yield chunk
            return

        raise last or self._keyring.missing_key_error(self.key_provider)

    # ---- القائمة تمرّ بنفس الحلقة ----

    async def list_models(self) -> list[Any]:
        return await self._run(self._fetch_models, model="models.list")
