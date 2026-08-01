"""OpenRouter: متوافق مع OpenAI، لكنه يعلن القدرات والأسعار فنقرأها."""
from __future__ import annotations

import logging
from typing import Any, Mapping

from services.ai_providers.openai_provider import OpenAIProvider
from services.model_directory import ModelInfo, family_of

log = logging.getLogger(__name__)


def _per_million(raw: Mapping[str, Any], field: str) -> float | None:
    """OpenRouter يسعّر بالتوكن الواحد نصًا؛ نحوّله إلى دولار/مليون."""
    pricing = raw.get("pricing") or {}
    value = pricing.get(field)
    if value in (None, "", "-1"):
        return None
    try:
        return float(value) * 1_000_000
    except (TypeError, ValueError):
        return None


class OpenRouterProvider(OpenAIProvider):
    name = "openrouter"
    key_provider = "openrouter"
    base_url = "https://openrouter.ai/api/v1"

    async def _fetch_models(self, client: Any) -> list[ModelInfo]:
        listing = await client.models.list()
        rows = getattr(listing, "data", None) or []
        models: list[ModelInfo] = []
        for row in rows:
            if isinstance(row, dict):
                raw: Mapping[str, Any] = row
            elif hasattr(row, "model_dump"):
                raw = row.model_dump()
            else:
                raw = {"id": getattr(row, "id", "")}

            model_id = str(raw.get("id") or "")
            if not model_id:
                continue
            architecture = raw.get("architecture") or {}
            modalities = architecture.get("input_modalities") or []
            parameters = raw.get("supported_parameters") or []
            models.append(
                ModelInfo(
                    id=model_id,
                    label=str(raw.get("name") or "") or model_id,
                    family=family_of(model_id),
                    context=raw.get("context_length"),
                    input_price=_per_million(raw, "prompt"),
                    output_price=_per_million(raw, "completion"),
                    # مقروء من الخدمة لا مخمَّن
                    vision="image" in modalities,
                    tools="tools" in parameters or "tool_choice" in parameters,
                    created=raw.get("created"),
                )
            )
        models.sort(key=lambda item: (item.family, item.id))
        return models
