"""كل لوحات الأزرار. مسار واحد لبناء callback_data ولفكّه.

حدّ تليجرام 64 بايتًا لـ callback_data، فالمعرّفات قصيرة عن قصد
(12 حرفًا للمفاتيح، 10 لبصمة النموذج) وليس المعرّف الكامل.
"""
from __future__ import annotations

from typing import Any, Collection, Mapping, Sequence

from pyrogram.types import InlineKeyboardButton as Btn
from pyrogram.types import InlineKeyboardMarkup as Markup

SEP = ":"
CB_LIMIT = 64

CURATED_PER_PAGE = 8
MODELS_PER_PAGE = 8
FAMILIES_PER_PAGE = 10
PERSONAS_PER_PAGE = 8

KEY_STATE = {
    "active": "🟢",
    "shaky": "🟡",
    "cooling": "🧊",
    "disabled": "🔴",
}

BACK = Btn("⬅️ رجوع", "home")
CLOSE = Btn("✖️ إغلاق", "close")


def cb(*parts: Any) -> str:
    """يبني callback_data ويتحقق من الحد قبل أن يرفضه تليجرام صامتًا."""
    data = SEP.join(str(part) for part in parts if str(part) != "")
    if len(data.encode()) > CB_LIMIT:
        raise ValueError(f"callback_data أطول من {CB_LIMIT} بايتًا: {data!r}")
    return data


def parse_cb(data: str) -> tuple[str, ...]:
    return tuple(data.split(SEP))


def state_label(value: bool) -> str:
    return "🟢 مفعّل" if value else "⚪️ معطّل"


def paginate(
    items: Sequence[Any], page: int, per_page: int
) -> tuple[Sequence[Any], int, int]:
    total_pages = max(1, (len(items) + per_page - 1) // per_page)
    page = max(0, min(page, total_pages - 1))
    start = page * per_page
    return items[start : start + per_page], page, total_pages


def _pager(route: tuple[Any, ...] | str, page: int, total_pages: int) -> list[Btn]:
    if total_pages <= 1:
        return []
    base = route if isinstance(route, tuple) else (route,)
    row: list[Btn] = []
    if page > 0:
        row.append(Btn("◀️", cb(*base, page - 1)))
    row.append(Btn(f"{page + 1}/{total_pages}", "noop"))
    if page < total_pages - 1:
        row.append(Btn("▶️", cb(*base, page + 1)))
    return row


def _clip(text: str, limit: int = 34) -> str:
    return text if len(text) <= limit else text[: limit - 1] + "…"


def back_only() -> Markup:
    return Markup([[BACK, CLOSE]])


# ───────────────────────── الرئيسية ─────────────────────────

def main_menu() -> Markup:
    return Markup(
        [
            [Btn("⚙️ الإعدادات", cb("settings")), Btn("🎭 الشخصيات", cb("personas"))],
            [Btn("🧠 الذاكرة", cb("memory")), Btn("🔍 البحث", cb("search"))],
            [Btn("📦 تصدير/نسخ احتياطي", cb("data"))],
            [Btn("🔄 إعادة تعيين المحادثة", cb("reset"))],
            [CLOSE],
        ]
    )


def settings_menu(s: Any) -> Markup:
    return Markup(
        [
            [Btn("🤖 النموذج", cb("svc"))],
            [Btn("🔑 المفاتيح", cb("keys")), Btn("🔧 مزوّد مخصّص", cb("cxlist"))],
            [
                Btn(f"🧠 الذاكرة: {state_label(s.memory_enabled)}", cb("do", "memtog")),
            ],
            [
                Btn(
                    f"🔍 البحث: {state_label(s.web_search_enabled)}",
                    cb("do", "srctog"),
                ),
            ],
            [
                Btn(
                    f"⚡ البثّ: {state_label(s.streaming_enabled)}",
                    cb("do", "strtog"),
                ),
            ],
            [Btn("🎭 الشخصيات", cb("personas"))],
            [BACK, CLOSE],
        ]
    )


# ───────────────────────── الخدمات والنماذج ─────────────────────────

def services_menu(entries: Sequence[Any], current_provider: str) -> Markup:
    rows: list[list[Btn]] = [[Btn("⭐ المختارة (قائمة منسّقة)", cb("models"))]]
    for entry in entries:
        mark = "✅ " if entry.name == current_provider else ""
        lock = "" if entry.healthy else " 🔒"
        icon = "🔧" if entry.custom else "☁️"
        rows.append([Btn(f"{mark}{icon} {entry.label}{lock}", cb("svc", entry.name))])
    rows.append([Btn("➕ إضافة مزوّد مخصّص", cb("do", "cxadd"))])
    rows.append([Btn("⚙️ الإعدادات", cb("settings")), BACK])
    return Markup(rows)


def curated_models_menu(
    models: Mapping[str, str],
    current: str,
    *,
    locked: Collection[str] = (),
    badges: Mapping[str, str] | None = None,
    page: int = 0,
) -> Markup:
    badges = badges or {}
    items, page, total = paginate(list(models.items()), page, CURATED_PER_PAGE)
    rows: list[list[Btn]] = []
    for key, label in items:
        mark = "✅ " if key == current else ""
        lock = "🔒 " if key in locked else ""
        badge = badges.get(key, "")
        suffix = f" {badge}" if badge else ""
        rows.append(
            [Btn(f"{mark}{lock}{_clip(label)}{suffix}", cb("set", "model", key))]
        )
    pager = _pager("models", page, total)
    if pager:
        rows.append(pager)
    rows.append([Btn("🤖 الخدمات", cb("svc")), BACK])
    return Markup(rows)


def model_families_menu(
    provider: str,
    families: Sequence[tuple[str, int]],
    *,
    page: int = 0,
    show_all: bool = False,
    hidden: int = 0,
) -> Markup:
    items, page, total = paginate(list(families), page, FAMILIES_PER_PAGE)
    rows = [
        [Btn(f"{_clip(name, 26)} · {count}", cb("svcf", provider, name))]
        for name, count in items
    ]
    pager = _pager(("svcp", provider), page, total)
    if pager:
        rows.append(pager)
    rows.append(_model_tools(provider, show_all=show_all, hidden=hidden))
    rows.append([Btn("🤖 الخدمات", cb("svc")), BACK])
    return Markup(rows)


def discovered_models_menu(
    provider: str,
    models: Sequence[Any],
    current_model_id: str,
    *,
    family: str = "",
    page: int = 0,
    show_all: bool = False,
    hidden: int = 0,
) -> Markup:
    items, page, total = paginate(list(models), page, MODELS_PER_PAGE)
    rows: list[list[Btn]] = []
    for info in items:
        mark = "✅ " if info.id == current_model_id else ""
        badge = info.badge()
        suffix = f" {badge}" if badge else ""
        # البصمة لا المعرّف: معرّفات OpenRouter تتجاوز حدّ 64 بايتًا
        rows.append([Btn(f"{mark}{_clip(info.name)}{suffix}", cb("pm", provider, info.hash))])
    route = ("svcf", provider, family) if family else ("svcp", provider)
    pager = _pager(route, page, total)
    if pager:
        rows.append(pager)
    rows.append(_model_tools(provider, show_all=show_all, hidden=hidden))
    back = (
        Btn("📂 العائلات", cb("svc", provider))
        if family
        else Btn("🤖 الخدمات", cb("svc"))
    )
    rows.append([back, BACK])
    return Markup(rows)


def _model_tools(provider: str, *, show_all: bool, hidden: int) -> list[Btn]:
    row = [Btn("🔄 تحديث القائمة", cb("do", "mrefresh", provider))]
    if hidden or show_all:
        label = "🙈 المحادثة فقط" if show_all else f"👀 عرض الكل (+{hidden})"
        row.append(Btn(label, cb("do", "mall", provider)))
    return row


def model_fetch_failed_menu(provider: str, *, has_stale: bool = False) -> Markup:
    rows = [[Btn("🔁 إعادة المحاولة", cb("do", "mretry", provider))]]
    if has_stale:
        rows.append([Btn("📋 القائمة المحفوظة", cb("svcp", provider, 0))])
    rows.append([Btn("🔑 المفاتيح", cb("keys", provider))])
    rows.append([Btn("⭐ المختارة", cb("models")), Btn("🤖 الخدمات", cb("svc"))])
    rows.append([BACK, CLOSE])
    return Markup(rows)


def model_saved_menu(provider: str) -> Markup:
    return Markup(
        [
            [Btn("🔁 نموذج آخر من نفس الخدمة", cb("svc", provider))],
            [Btn("🤖 الخدمات", cb("svc")), Btn("⚙️ الإعدادات", cb("settings"))],
            [BACK, CLOSE],
        ]
    )


# ───────────────────────── الشخصيات ─────────────────────────

def personas_menu(items: Sequence[Any], current_id: str, *, page: int = 0) -> Markup:
    page_items, page, total = paginate(list(items), page, PERSONAS_PER_PAGE)
    rows: list[list[Btn]] = []
    for item in page_items:
        mark = "✅ " if item.persona_id == current_id else ""
        icon = "⭐" if item.built_in else "🎭"
        rows.append(
            [Btn(f"{mark}{icon} {_clip(item.name)}", cb("persona", item.persona_id))]
        )
    pager = _pager("personas", page, total)
    if pager:
        rows.append(pager)
    rows.append([Btn("➕ إضافة شخصية", cb("do", "padd"))])
    rows.append([Btn("⚙️ الإعدادات", cb("settings")), BACK])
    return Markup(rows)


def persona_detail_menu(
    persona_id: str, *, active: bool, built_in: bool
) -> Markup:
    rows: list[list[Btn]] = []
    if not active:
        rows.append([Btn("✅ تفعيل هذه الشخصية", cb("set", "persona", persona_id))])
    if not built_in:
        rows.append([Btn("🗑️ حذف", cb("pdel", persona_id))])
    rows.append([Btn("🎭 الشخصيات", cb("personas")), BACK])
    return Markup(rows)


# ───────────────────────── الذاكرة والبحث ─────────────────────────

def memory_menu(enabled: bool, count: int) -> Markup:
    rows = [[Btn(f"الحالة: {state_label(enabled)}", cb("do", "memtog"))]]
    if count:
        rows.append([Btn("🗑️ مسح كل الذاكرة", cb("forgetask"))])
    rows.append([Btn("🔁 إعادة الفهرسة من المحادثة", cb("do", "reindex"))])
    rows.append([Btn("⚙️ الإعدادات", cb("settings")), BACK])
    return Markup(rows)


def search_menu(enabled: bool, available: bool) -> Markup:
    rows = []
    if available:
        rows.append([Btn(f"الحالة: {state_label(enabled)}", cb("do", "srctog"))])
    rows.append([Btn("⚙️ الإعدادات", cb("settings")), BACK])
    return Markup(rows)


# ───────────────────────── المزوّدات المخصّصة ─────────────────────────

def custom_providers_menu(docs: Sequence[Any]) -> Markup:
    rows: list[list[Btn]] = []
    for doc in docs:
        rows.append(
            [
                Btn(f"🔧 {_clip(doc.name)}", cb("cx", doc.key)),
                Btn("🗑️", cb("cxdel", doc.key)),
            ]
        )
    rows.append([Btn("➕ إضافة مزوّد مخصّص", cb("do", "cxadd"))])
    rows.append([Btn("⚙️ الإعدادات", cb("settings")), BACK])
    return Markup(rows)


def custom_detail_menu(key: str) -> Markup:
    return Markup(
        [
            [Btn("📋 نماذج هذا المزوّد", cb("svc", key))],
            [Btn("🧪 فحص الاتصال", cb("do", "cxtest", key))],
            [Btn("🔑 مفاتيحه", cb("keys", key))],
            [Btn("🗑️ حذف", cb("cxdel", key))],
            [Btn("🔧 المزوّدات", cb("cxlist")), BACK],
        ]
    )


# ───────────────────────── المفاتيح ─────────────────────────

def keys_providers_menu(statuses: Sequence[Any], strategy: str) -> Markup:
    rows: list[list[Btn]] = []
    for status in statuses:
        mark = "🟢" if status.healthy else "🔴"
        counter = f"{status.active}/{status.total}" if status.total else "—"
        rows.append(
            [Btn(f"{mark} {_clip(status.label, 24)} · {counter}", cb("keys", status.name))]
        )
    label = "🔁 دوري" if strategy == "round_robin" else "🎲 عشوائي"
    rows.append([Btn(f"الاستراتيجية: {label}", cb("do", "kstrat"))])
    rows.append([Btn("⚙️ الإعدادات", cb("settings")), BACK])
    return Markup(rows)


def keys_menu(provider: str, keys: Sequence[Any]) -> Markup:
    rows: list[list[Btn]] = []
    for key in keys:
        icon = KEY_STATE.get(key.state, "⚪️")
        rows.append(
            [
                Btn(f"{icon} {_clip(key.display, 28)}", cb("key", key.key_id)),
                Btn("🗑️", cb("kdel", key.key_id)),
            ]
        )
    rows.append([Btn("➕ إضافة مفتاح", cb("do", "kadd", provider))])
    rows.append([Btn("🔑 المفاتيح", cb("keys")), BACK])
    return Markup(rows)


def key_detail_menu(key_id: str, provider: str, *, active: bool) -> Markup:
    toggle = "🔴 تعطيل" if active else "🟢 إعادة تفعيل"
    return Markup(
        [
            [Btn(toggle, cb("ktog", key_id))],
            [Btn("🗑️ حذف", cb("kdel", key_id))],
            [Btn("🔑 مفاتيح المزوّد", cb("keys", provider)), BACK],
        ]
    )


# ───────────────────────── البيانات والتأكيد ─────────────────────────

def data_menu() -> Markup:
    return Markup(
        [
            [Btn("📤 تصدير JSON", cb("do", "export"))],
            [Btn("🗑️ حذف كل البيانات", cb("wipeask"))],
            [Btn("⚙️ الإعدادات", cb("settings")), BACK],
        ]
    )


def confirm_menu(
    action: str,
    argument: str = "",
    *,
    cancel_route: tuple[Any, ...] = ("home",),
    confirm_label: str = "✅ تأكيد",
) -> Markup:
    confirm = cb("do", action, argument) if argument else cb("do", action)
    return Markup(
        [
            [Btn(confirm_label, confirm)],
            [Btn("↩️ إلغاء", cb(*cancel_route))],
        ]
    )


def confirm_wipe_menu() -> Markup:
    """زرّان لا زرّ واحد: الحذف الشامل لا رجوع فيه."""
    return Markup(
        [
            [Btn("📤 تصدير أولًا", cb("do", "export"))],
            [Btn("🗑️ نعم، احذف كل شيء", cb("do", "wipe"))],
            [Btn("↩️ إلغاء", cb("data"))],
        ]
    )
