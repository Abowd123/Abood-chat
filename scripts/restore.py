#!/usr/bin/env python3
"""استعادة من نسخة `scripts/backup.py`.

⚠️ يكتب فوق المجموعات المستعادة. يطلب تأكيدًا صريحًا.

الاستخدام:
    python scripts/restore.py backups/backup-20260801-034100.json
    python scripts/restore.py file.json --only messages,personas
    python scripts/restore.py file.json --yes
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from config import load_settings                        # noqa: E402
from database.mongo import Mongo                        # noqa: E402

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s %(levelname)-8s %(message)s"
)
log = logging.getLogger("restore")

DATE_FIELDS = (
    "created_at", "last_used_at", "cooldown_until", "started_at", "exported_at"
)


def _revive(value):
    if isinstance(value, dict):
        return {key: _revive(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_revive(item) for item in value]
    return value


def _restore_dates(doc: dict) -> dict:
    for field in DATE_FIELDS:
        raw = doc.get(field)
        if isinstance(raw, str):
            try:
                doc[field] = datetime.fromisoformat(raw)
            except ValueError:
                pass
    return doc


async def main() -> int:
    parser = argparse.ArgumentParser(description="استعادة نسخة احتياطية")
    parser.add_argument("file", help="ملف النسخة")
    parser.add_argument("--only", default="", help="مجموعات محدّدة مفصولة بفاصلة")
    parser.add_argument("--yes", action="store_true", help="بلا سؤال تأكيد")
    args = parser.parse_args()

    path = Path(args.file)
    if not path.exists():
        log.error("الملف غير موجود: %s", path)
        return 1

    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except ValueError as exc:
        log.error("ملف غير صالح: %s", exc)
        return 1

    collections = payload.get("collections") or {}
    if not collections:
        log.error("لا مجموعات في الملف — قد يكون تصدير /export لا نسخة كاملة")
        return 1

    wanted = (
        [name.strip() for name in args.only.split(",") if name.strip()]
        if args.only
        else list(collections)
    )
    plan = {name: collections.get(name) or [] for name in wanted}

    log.info("النسخة من: %s", payload.get("created_at", "غير معروف"))
    log.info("تحمل مفاتيح: %s", payload.get("includes_secrets", False))
    for name, docs in plan.items():
        log.info("  %-18s %s مستندًا", name, len(docs))

    if not args.yes:
        print("\n⚠️  ستُحذف المجموعات أعلاه ويُكتب محتوى النسخة مكانها.")
        if input("اكتب 'yes' للمتابعة: ").strip().lower() != "yes":
            log.info("أُلغيت الاستعادة")
            return 0

    settings = load_settings()
    mongo = Mongo(settings.mongo_uri, settings.mongo_db)
    try:
        await mongo.connect()
        for name, docs in plan.items():
            collection = mongo.collection(name)
            await collection.delete_many({})
            if not docs:
                log.info("%-18s فارغة", name)
                continue
            prepared = [_restore_dates(_revive(dict(doc))) for doc in docs]
            for doc in prepared:
                doc.pop("_id", None)   # يُعاد توليده
            await collection.insert_many(prepared)
            log.info("%-18s استُعيد %s مستندًا", name, len(prepared))
    except Exception:
        log.exception("فشلت الاستعادة")
        return 1
    finally:
        await mongo.close()

    log.info("تمّت الاستعادة — أعد تشغيل البوت")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
