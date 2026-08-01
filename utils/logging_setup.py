"""إعداد السجلّ، وإرسال الأخطاء الحرجة إليك عبر تليجرام.

الحلقة المفرغة محسوبة: إن فشل الإرسال نستخدم log.warning لا log.error،
وإلا لأعاد المعالج استدعاء نفسه بلا نهاية.
"""
from __future__ import annotations

import asyncio
import logging
import sys
from typing import Any

FORMAT = "%(asctime)s %(levelname)-8s %(name)s — %(message)s"
DATE_FORMAT = "%Y-%m-%d %H:%M:%S"

NOISY = (
    "pyrogram.session",
    "pyrogram.connection",
    "pyrogram.crypto",
    "httpx",
    "httpcore",
    "openai._base_client",
    "anthropic._base_client",
)

MAX_TRACE = 1200


class TelegramErrorHandler(logging.Handler):
    """يرسل ERROR وما فوقه إلى المالك، بلا أن يُغرق محادثته."""

    def __init__(self, notifier: Any, level: int = logging.ERROR) -> None:
        super().__init__(level)
        self._notifier = notifier
        self._loop: asyncio.AbstractEventLoop | None = None

    def bind_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        self._loop = loop

    def emit(self, record: logging.LogRecord) -> None:
        if record.name.startswith("services.notifier"):
            return   # يمنع الحلقة المفرغة
        try:
            text = self._render(record)
        except Exception:
            return
        loop = self._loop
        if loop is None or loop.is_closed():
            return
        try:
            loop.call_soon_threadsafe(
                lambda: loop.create_task(
                    self._notifier.send(
                        text, tag=f"log:{record.name}:{record.lineno}", cooldown=300
                    )
                )
            )
        except Exception:
            pass

    def _render(self, record: logging.LogRecord) -> str:
        import html

        head = "🚨" if record.levelno >= logging.CRITICAL else "⚠️"
        body = (
            f"{head} <b>{html.escape(record.levelname)}</b>\n"
            f"<code>{html.escape(record.name)}</code>\n\n"
            f"{html.escape(str(record.getMessage())[:900])}"
        )
        if record.exc_info:
            trace = self.formatException(record.exc_info)
            body += f"\n\n<pre>{html.escape(trace[-MAX_TRACE:])}</pre>"
        return body


def setup_logging(level: str = "INFO") -> None:
    numeric = getattr(logging, level.upper(), logging.INFO)
    root = logging.getLogger()
    root.setLevel(numeric)
    for existing in list(root.handlers):
        root.removeHandler(existing)

    stream = logging.StreamHandler(sys.stdout)
    stream.setFormatter(logging.Formatter(FORMAT, datefmt=DATE_FORMAT))
    root.addHandler(stream)

    for name in NOISY:
        logging.getLogger(name).setLevel(logging.WARNING)


def attach_notifier(notifier: Any) -> TelegramErrorHandler:
    handler = TelegramErrorHandler(notifier)
    handler.setFormatter(logging.Formatter(FORMAT, datefmt=DATE_FORMAT))
    try:
        handler.bind_loop(asyncio.get_running_loop())
    except RuntimeError:
        pass
    logging.getLogger().addHandler(handler)
    return handler
