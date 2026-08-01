"""تشفير متماثل للمفاتيح المخزَّنة (Fernet).

قرار صريح: بلا مفتاح تشفير لا يتوقّف البوت، بل يخزّن القيم صريحة
ويسجّل تحذيرًا. تعطيل البوت كليًا كان سيدفعك لحلول أسوأ.
لكن اضبط CONTENT_ENCRYPTION_KEY: هذه المجموعة تحمل كل فاتورتك.
"""
from __future__ import annotations

import logging

from cryptography.fernet import Fernet, InvalidToken

log = logging.getLogger(__name__)

PREFIX = "enc:v1:"


def generate_key() -> str:
    return Fernet.generate_key().decode()


class ContentCipher:
    def __init__(self, key: str | None) -> None:
        self._fernet: Fernet | None = None
        if key:
            try:
                self._fernet = Fernet(key.encode() if isinstance(key, str) else key)
            except Exception as exc:
                raise ValueError(
                    "CONTENT_ENCRYPTION_KEY غير صالح — أنشئه بـ "
                    "python -c \"from cryptography.fernet import Fernet;"
                    "print(Fernet.generate_key().decode())\""
                ) from exc

    @property
    def enabled(self) -> bool:
        return self._fernet is not None

    def is_sealed(self, value: str | None) -> bool:
        return isinstance(value, str) and value.startswith(PREFIX)

    def seal(self, value: str | None) -> str | None:
        if value is None:
            return None
        if self._fernet is None:
            return value
        token = self._fernet.encrypt(value.encode()).decode()
        return f"{PREFIX}{token}"

    def open(self, value: str | None) -> str | None:
        """يفكّ المشفَّر ويمرّر الصريح كما هو.

        التمرير مقصود: قيمة كُتبت قبل تفعيل التشفير تبقى مقروءة.
        """
        if value is None:
            return None
        if not self.is_sealed(value):
            return value
        if self._fernet is None:
            log.error(
                "قيمة مشفَّرة موجودة بلا CONTENT_ENCRYPTION_KEY — "
                "اضبط المفتاح نفسه الذي شُفّرت به"
            )
            return None
        token = value[len(PREFIX) :]
        try:
            return self._fernet.decrypt(token.encode()).decode()
        except InvalidToken:
            log.error("تعذّر فكّ التشفير — المفتاح مختلف عن الذي شُفّرت به القيمة")
            return None
