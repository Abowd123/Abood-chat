#!/usr/bin/env python3
"""نسخ احتياطي قابل للجدولة (cron أو Railway cron).

⚠️ هذه النسخة **تحمل قيم مفاتيح API** (مشفّرة إن ضُبط
CONTENT_ENCRYPTION_KEY). مقصود: نسخة بلا مفاتيح لا تُستعاد.
احفظها في مكان آمن، ولا ترفعها إلى مستودع.

الاستخدام:
    python scripts/backup.py
    python scripts/backup.py --out /data/backups --keep 14
    python scripts/backup.py --no-secrets     # بلا قيم المفاتيح
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from config import load_settings                        # noqa: E402
from database.mongo import Mongo                        # noqa: E402

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s %(levelname)-8s %(message)s"
)
log = logging.getLogger("backup")

COLLECTIONS = (
    "settings",
    "personas",
    "messages",
    "memories",
    "custom_providers",
    "provider_keys",
    "flows",
)

SECRET_FIELDS = {
    "provider_keys": ("key_value", "fingerprint"),
    "custom_providers": ("api_key",),
}

# المتجهات تضخّم الملف عشرات الأضعاف بلا قيمة للقارئ
HEAVY_FIELDS = {"memories": ("vector",)}


def _serialize(value):
    from bson import ObjectId

    if isinstance(value, ObjectId):
        return str(value)
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, dict):
        return {key: _serialize(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_serialize(item) for item in value]
    return value


async def dump(
    mongo: Mongo, *, include_secrets: bool, include_vectors: bool
) -> dict:
    existing = set(await mongo.collection_names())
    payload: dict = {
        "version": 1,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "database": mongo.db.name,
        "includes_secrets": include_secrets,
        "collections": {},
    }
    for name in COLLECTIONS:
        if name not in existing:
            payload["collections"][name] = []
            continue
        projection: dict[str, int] = {}
        if not include_secrets:
            for field in SECRET_FIELDS.get(name, ()):
                projection[field] = 0
        if not include_vectors:
            for field in HEAVY_FIELDS.get(name, ()):
                projection[field] = 0
        cursor = mongo.collection(name).find({}, projection or None)
        docs = await cursor.to_list(length=None)
        payload["collections"][name] = [_serialize(doc) for doc in docs]
        log.info("%-18s %s مستندًا", name, len(docs))
    return payload


def prune(folder: Path, keep: int) -> None:
    if keep <= 0:
        return
    files = sorted(
        folder.glob("backup-*.json"), key=lambda p: p.stat().st_mtime, reverse=True
    )
    for stale in files[keep:]:
        stale.unlink(missing_ok=True)
        log.info("حُذفت نسخة قديمة: %s", stale.name)


async def main() -> int:
    parser = argparse.ArgumentParser(description="نسخ احتياطي لقاعدة البوت")
    parser.add_argument("--out", default="backups", help="مجلد الحفظ")
    parser.add_argument("--keep", type=int, default=7, help="عدد النسخ المحفوظة")
    parser.add_argument(
        "--no-secrets", action="store_true", help="بلا قيم المفاتيح"
    )
    parser.add_argument(
        "--with-vectors", action="store_true", help="تضمين متجهات الذاكرة"
    )
    args = parser.parse_args()

    settings = load_settings()
    mongo = Mongo(settings.mongo_uri, settings.mongo_db)
    try:
        await mongo.connect()
        payload = await dump(
            mongo,
            include_secrets=not args.no_secrets,
            include_vectors=args.with_vectors,
        )
    except Exception:
        log.exception("فشل النسخ الاحتياطي")
        return 1
    finally:
        await mongo.close()

    folder = Path(args.out)
    folder.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    target = folder / f"backup-{stamp}.json"
    target.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    # المفاتيح داخل الملف: قصر الصلاحيات على المالك
    try:
        target.chmod(0o600)
    except OSError:
        pass

    size = target.stat().st_size
    log.info("حُفظت النسخة: %s (%s KB)", target, size // 1024)
    if not args.no_secrets:
        log.warning(
            "الملف يحمل قيم مفاتيح API%s — احفظه في مكان آمن",
            " (مشفّرة)" if settings.content_encryption_key else " نصًا صريحًا",
        )
    prune(folder, args.keep)
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
