"""تقطيع النصوص وتهيئتها لتليجرام."""
from __future__ import annotations

import html
import re

TELEGRAM_LIMIT = 4096
SAFE_LIMIT = 3900        # هامش للتنسيق والتذييل
CAPTION_LIMIT = 1024

_UNCLOSED_FENCE = re.compile(r"```")


def escape(text: str) -> str:
    return html.escape(text or "")


def split_message(text: str, limit: int = SAFE_LIMIT) -> list[str]:
    """يقطّع على حدود طبيعية: فقرة، سطر، ثم مسافة."""
    if not text:
        return []
    if len(text) <= limit:
        return [text]

    chunks: list[str] = []
    remaining = text
    while len(remaining) > limit:
        window = remaining[:limit]
        cut = window.rfind("\n\n")
        if cut < limit // 3:
            cut = window.rfind("\n")
        if cut < limit // 3:
            cut = window.rfind(" ")
        if cut < limit // 3:
            cut = limit
        chunks.append(remaining[:cut].rstrip())
        remaining = remaining[cut:].lstrip()
    if remaining:
        chunks.append(remaining)
    return chunks


def balance_code_fences(text: str) -> str:
    """يغلق سياج كود مفتوحًا: البثّ يقطع الردّ في منتصفه أحيانًا."""
    if len(_UNCLOSED_FENCE.findall(text)) % 2:
        return text + "\n```"
    return text


def as_html_code(text: str) -> str:
    return f"<pre>{escape(text)}</pre>"


def truncate(text: str, limit: int = 200) -> str:
    flat = " ".join((text or "").split())
    return flat if len(flat) <= limit else flat[: limit - 1] + "…"
