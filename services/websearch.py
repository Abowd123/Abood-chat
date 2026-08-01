"""بحث الويب عبر Serper أو Brave، كأداة أو كأمر يدوي."""
from __future__ import annotations

import asyncio
import html
import logging
from dataclasses import dataclass
from typing import Any, Sequence

import httpx

from services.ai_providers.base import ToolSpec

log = logging.getLogger(__name__)

SERPER_URL = "https://google.serper.dev/search"
BRAVE_URL = "https://api.search.brave.com/res/v1/web/search"
TIMEOUT = 20.0

WEB_SEARCH_TOOL = ToolSpec(
    name="web_search",
    description=(
        "يبحث في الويب عن معلومات حديثة. استخدمه للأحداث الجارية، "
        "الأسعار، الإصدارات، وأي شيء يتغيّر بمرور الوقت أو لا تعرفه بثقة."
    ),
    parameters={
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "عبارة البحث، مختصرة ومحدّدة.",
            }
        },
        "required": ["query"],
    },
)


class SearchError(RuntimeError):
    """رسالته صالحة للعرض."""


@dataclass(frozen=True, slots=True)
class SearchResult:
    title: str
    url: str
    snippet: str

    def as_line(self, index: int) -> str:
        return (
            f"{index}. <b>{html.escape(self.title)}</b>\n"
            f"{html.escape(self.snippet)}\n"
            f"<a href=\"{html.escape(self.url, quote=True)}\">{html.escape(self.url)}</a>"
        )

    def as_context(self, index: int) -> str:
        return f"[{index}] {self.title}\n{self.snippet}\nالمصدر: {self.url}"


class WebSearchService:
    def __init__(
        self,
        *,
        provider: str | None,
        serper_key: str | None,
        brave_key: str | None,
        results: int = 5,
    ) -> None:
        self._provider = provider
        self._serper = serper_key
        self._brave = brave_key
        self._results = max(1, min(10, results))
        self._client: httpx.AsyncClient | None = None

    @property
    def available(self) -> bool:
        return bool(self._provider)

    @property
    def provider(self) -> str | None:
        return self._provider

    @property
    def provider_label(self) -> str:
        return {"serper": "Serper", "brave": "Brave"}.get(
            self._provider or "", "معطّل"
        )

    def _ensure_client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=TIMEOUT)
        return self._client

    async def search(self, query: str) -> list[SearchResult]:
        cleaned = (query or "").strip()
        if not cleaned:
            raise SearchError("عبارة البحث فارغة.")
        if not self.available:
            raise SearchError(
                "البحث غير مضبوط. أضف SERPER_API_KEY أو BRAVE_API_KEY."
            )
        try:
            if self._provider == "serper":
                return await self._serper_search(cleaned)
            return await self._brave_search(cleaned)
        except SearchError:
            raise
        except httpx.TimeoutException as exc:
            raise SearchError("انتهت المهلة أثناء البحث.") from exc
        except Exception as exc:
            raise SearchError(f"تعذّر البحث: {str(exc)[:180]}") from exc

    async def _serper_search(self, query: str) -> list[SearchResult]:
        response = await self._ensure_client().post(
            SERPER_URL,
            headers={"X-API-KEY": self._serper or "", "Content-Type": "application/json"},
            json={"q": query, "num": self._results},
        )
        self._check(response)
        payload = response.json()
        rows = payload.get("organic") or []
        results = [
            SearchResult(
                title=str(row.get("title") or "بلا عنوان"),
                url=str(row.get("link") or ""),
                snippet=str(row.get("snippet") or ""),
            )
            for row in rows[: self._results]
            if row.get("link")
        ]
        if not results and payload.get("answerBox"):
            box = payload["answerBox"]
            results.append(
                SearchResult(
                    title=str(box.get("title") or "إجابة مباشرة"),
                    url=str(box.get("link") or ""),
                    snippet=str(box.get("answer") or box.get("snippet") or ""),
                )
            )
        return results

    async def _brave_search(self, query: str) -> list[SearchResult]:
        response = await self._ensure_client().get(
            BRAVE_URL,
            headers={
                "X-Subscription-Token": self._brave or "",
                "Accept": "application/json",
            },
            params={"q": query, "count": self._results},
        )
        self._check(response)
        rows = (response.json().get("web") or {}).get("results") or []
        return [
            SearchResult(
                title=str(row.get("title") or "بلا عنوان"),
                url=str(row.get("url") or ""),
                snippet=str(row.get("description") or ""),
            )
            for row in rows[: self._results]
            if row.get("url")
        ]

    @staticmethod
    def _check(response: httpx.Response) -> None:
        if response.status_code in (401, 403):
            raise SearchError("مفتاح البحث مرفوض. راجع SERPER_API_KEY / BRAVE_API_KEY.")
        if response.status_code == 429:
            raise SearchError("تجاوزتَ حصّة البحث المسموحة. جرّب لاحقًا.")
        if response.status_code >= 400:
            raise SearchError(f"خدمة البحث رجّعت خطأ {response.status_code}.")

    # ───────────── الأداة ─────────────

    async def run_tool(self, arguments: dict[str, Any]) -> str:
        """نتيجة يقرأها النموذج. لا يرفع استثناءً: النموذج يحتاج جوابًا."""
        query = str((arguments or {}).get("query") or "").strip()
        if not query:
            return "لم تُحدَّد عبارة بحث."
        try:
            results = await self.search(query)
        except SearchError as exc:
            return f"فشل البحث: {exc}"
        if not results:
            return f"لا نتائج لـ «{query}»."
        blocks = [item.as_context(index) for index, item in enumerate(results, 1)]
        return f"نتائج البحث عن «{query}»:\n\n" + "\n\n".join(blocks)

    @staticmethod
    def format_results(query: str, results: Sequence[SearchResult]) -> str:
        if not results:
            return f"🔍 لا نتائج لـ <code>{html.escape(query)}</code>."
        lines = [f"🔍 <b>نتائج «{html.escape(query)}»</b>", ""]
        lines.extend(item.as_line(index) for index, item in enumerate(results, 1))
        return "\n\n".join(lines)

    async def close(self) -> None:
        if self._client is not None:
            try:
                await self._client.aclose()
            except Exception:
                log.debug("تعذّر إغلاق عميل البحث", exc_info=True)
            self._client = None
