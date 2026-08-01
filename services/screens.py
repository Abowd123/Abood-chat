"""نصوص الشاشات ولوحاتها. كل شاشة دالة ترجع Screen.

الفصل مقصود: الهاندلر يقرّر أي شاشة، والشاشة تعرف كيف تُرسم.
"""
from __future__ import annotations

import html
from dataclasses import dataclass
from typing import Any, Sequence

import utils.keyboards as kb
from database.settings import BotSettings
from services.catalog import (
    locked_models,
    model_badges,
    model_label,
    model_labels,
    model_spec,
    normalize_model,
)
from services.model_directory import FAMILY_THRESHOLD, Listing

LOADING_MODELS = "⏳ <i>جاري جلب النماذج...</i>"


@dataclass(frozen=True, slots=True)
class Screen:
    text: str
    markup: Any = None


@dataclass(frozen=True, slots=True)
class ServiceEntry:
    name: str
    label: str
    custom: bool
    healthy: bool
    keys: str


def _age(seconds: float) -> str:
    if seconds < 90:
        return "أقل من دقيقة"
    minutes = int(seconds // 60)
    if minutes < 60:
        return f"{minutes} دقيقة"
    hours = minutes // 60
    return f"{hours} ساعة" if hours < 24 else f"{hours // 24} يوم"


def _esc(value: Any) -> str:
    return html.escape(str(value))


# ───────────────────────── الرئيسية ─────────────────────────

def main_menu(s: BotSettings, *, messages: int, personas: int) -> Screen:
    current = normalize_model(s.selected_model)
    return Screen(
        "<b>🤖 لوحة التحكّم</b>\n\n"
        f"النموذج: <code>{_esc(model_label(current))}</code>\n"
        f"الرسائل المحفوظة: <code>{messages}</code>\n"
        f"الشخصيات: <code>{personas}</code>\n"
        f"الذاكرة: {kb.state_label(s.memory_enabled)} · "
        f"البحث: {kb.state_label(s.web_search_enabled)}\n\n"
        "<i>أرسل أي رسالة لتبدأ المحادثة، أو اختر من الأزرار.</i>",
        kb.main_menu(),
    )


def settings_menu(s: BotSettings, *, search_available: bool,
                  memory_available: bool) -> Screen:
    current = normalize_model(s.selected_model)
    spec = model_spec(current)
    lines = ["<b>⚙️ الإعدادات</b>", ""]
    if spec:
        lines += [
            f"النموذج: <code>{_esc(spec.label)}</code>",
            f"المعرّف: <code>{_esc(spec.model_id)}</code>",
            f"الصور: {kb.state_label(spec.vision)} · "
            f"الأدوات: {kb.state_label(spec.tools)}",
        ]
    lines += [
        "",
        f"الذاكرة: {kb.state_label(s.memory_enabled)}"
        + ("" if memory_available else " <i>(غير مضبوطة)</i>"),
        f"بحث الويب: {kb.state_label(s.web_search_enabled)}"
        + ("" if search_available else " <i>(غير مضبوط)</i>"),
        f"البثّ التدريجي: {kb.state_label(s.streaming_enabled)}",
        f"استراتيجية المفاتيح: <code>{_esc(s.key_strategy)}</code>",
    ]
    return Screen("\n".join(lines), kb.settings_menu(s))


# ───────────────────────── النموذج والخدمات ─────────────────────────

def services(s: BotSettings, entries: Sequence[ServiceEntry]) -> Screen:
    current = normalize_model(s.selected_model)
    spec = model_spec(current)
    lines = ["<b>🤖 اختيار الخدمة</b>", ""]
    if spec:
        source = "مُكتشَف" if spec.discovered else "منسّق"
        lines += [
            f"النموذج الحالي: <code>{_esc(spec.label)}</code>",
            f"المعرّف: <code>{_esc(spec.model_id)}</code>",
            f"المصدر: <i>{source}</i>",
        ]
    lines.append("")
    for entry in entries:
        icon = "🟢" if entry.healthy else "🔒"
        lines.append(f"{icon} <b>{_esc(entry.label)}</b> — {_esc(entry.keys)}")
    lines += [
        "",
        "<i>اختر خدمة لجلب نماذجها المتاحة، أو ⭐ المختارة "
        "لقائمة قدراتها متحقَّق منها.</i>",
    ]
    return Screen("\n".join(lines), kb.services_menu(entries, s.selected_provider))


def curated_models(s: BotSettings, *, page: int = 0) -> Screen:
    current = normalize_model(s.selected_model)
    locked = locked_models()
    return Screen(
        "<b>⭐ النماذج المختارة</b>\n\n"
        f"الحالي: <code>{_esc(model_label(current))}</code>\n\n"
        "<i>👁 صور · 🛠 أدوات · 🔒 مزوّده بلا مفتاح صالح</i>\n"
        "<i>قدرات هذه النماذج متحقَّق منها يدويًا.</i>",
        kb.curated_models_menu(
            model_labels(), current, locked=locked,
            badges=model_badges(), page=page,
        ),
    )


def discovered_models(
    listing: Listing, s: BotSettings, *, family: str = "", page: int = 0
) -> Screen:
    show_all = bool(s.model_show_all)
    label = _service_label(listing.provider)
    models = (
        listing.in_family(family, show_all=show_all)
        if family
        else listing.visible(show_all=show_all)
    )
    current_id = _current_model_id(s, listing.provider)

    head = [f"<b>🤖 {_esc(label)}</b>", ""]
    if family:
        head.append(f"العائلة: <code>{_esc(family)}</code>")
    head.append(f"النماذج: <code>{len(models)}</code>")
    if listing.stale:
        head.append(
            f"⚠️ <i>قائمة محفوظة (عمرها {_age(listing.age)}) — "
            f"فشل التحديث: {_esc(listing.error or '')}</i>"
        )
    elif listing.cached:
        head.append(f"<i>من الكاش · عمرها {_age(listing.age)}</i>")
    else:
        head.append("<i>محدَّثة الآن</i>")

    if not family and len(models) > FAMILY_THRESHOLD:
        head += ["", "<i>القائمة طويلة، فهي مجموعة بالعائلة. اختر عائلة.</i>"]
        return Screen(
            "\n".join(head),
            kb.model_families_menu(
                listing.provider, listing.families(show_all=show_all),
                page=page, show_all=show_all, hidden=listing.hidden_count(),
            ),
        )

    head += ["", "<i>👁 صور · 🛠 أدوات · 🆓 مجاني</i>"]
    if not listing.provider.startswith("cx-"):
        head.append(
            "<i>القدرات غير المعلَنة من الخدمة تبقى مطفأة — "
            "فعّلها بعد الاختيار.</i>"
        )
    return Screen(
        "\n".join(head),
        kb.discovered_models_menu(
            listing.provider, models, current_id, family=family,
            page=page, show_all=show_all, hidden=listing.hidden_count(),
        ),
    )


def model_fetch_failed(
    provider: str, error: str, *, has_stale: bool = False
) -> Screen:
    return Screen(
        f"<b>❌ تعذّر جلب نماذج {_esc(_service_label(provider))}</b>\n\n"
        f"<code>{_esc(error)}</code>\n\n"
        "<i>الأسباب الشائعة: مفتاح مرفوض · الخدمة لا توفّر قائمة · "
        "انقطاع شبكة · خادم محلي مطفأ.</i>\n\n"
        "<i>النموذج الحالي لم يتغيّر، والبوت يعمل كما هو.</i>",
        kb.model_fetch_failed_menu(provider, has_stale=has_stale),
    )


def model_selected(info: Any, provider: str) -> Screen:
    price = (
        f"<code>${info.input_price:.2f}</code> / "
        f"<code>${info.output_price or 0:.2f}</code> لكل مليون توكن"
        if info.priced
        else "<i>غير معلوم</i>"
    )
    lines = [
        "<b>✅ حُفظ النموذج</b>",
        "",
        f"النموذج: <code>{_esc(info.name)}</code>",
        f"المعرّف: <code>{_esc(info.id)}</code>",
        f"الخدمة: <code>{_esc(_service_label(provider))}</code>",
    ]
    if info.context:
        lines.append(f"السياق: <code>{info.context}</code> توكن")
    lines += [
        f"السعر: {price}",
        f"الصور: {kb.state_label(info.vision)} · "
        f"الأدوات: {kb.state_label(info.tools)}",
        "",
        "<i>المحادثات والشخصيات والذاكرة تعمل كما هي بلا تغيير.</i>",
    ]
    return Screen("\n".join(lines), kb.model_saved_menu(provider))


def _service_label(provider: str) -> str:
    from services.ai_providers import provider_label

    return provider_label(provider)


def _current_model_id(s: BotSettings, provider: str) -> str:
    spec = model_spec(normalize_model(s.selected_model))
    if spec is None or spec.provider != provider:
        return ""
    return spec.model_id


# ───────────────────────── الشخصيات ─────────────────────────

def personas(items: Sequence[Any], current_id: str, *, page: int = 0) -> Screen:
    lines = ["<b>🎭 الشخصيات</b>", ""]
    for item in items:
        mark = "✅ " if item.persona_id == current_id else "· "
        lines.append(f"{mark}<b>{_esc(item.name)}</b>")
        lines.append(f"   <i>{_esc(item.preview)}</i>")
    lines += ["", "<i>اضغط شخصية لعرضها وتفعيلها.</i>"]
    return Screen("\n".join(lines), kb.personas_menu(items, current_id, page=page))


def persona_detail(item: Any, *, active: bool) -> Screen:
    return Screen(
        f"<b>🎭 {_esc(item.name)}</b>\n\n"
        f"<code>{_esc(item.prompt)}</code>\n\n"
        f"مُفعَّلة: {kb.state_label(active)}"
        + ("\n<i>شخصية افتراضية — لا تُحذف.</i>" if item.built_in else ""),
        kb.persona_detail_menu(item.persona_id, active=active, built_in=item.built_in),
    )


def confirm_persona_delete(item: Any) -> Screen:
    return Screen(
        f"<b>🗑️ حذف «{_esc(item.name)}»</b>\n\n"
        "<i>المحادثات السابقة تبقى كما هي.</i>",
        kb.confirm_menu(
            "prm", item.persona_id, cancel_route=("persona", item.persona_id)
        ),
    )


# ───────────────────────── الذاكرة والبحث ─────────────────────────

def memory(status: Any, latest: Sequence[Any]) -> Screen:
    lines = ["<b>🧠 الذاكرة الدلالية</b>", ""]
    if not status.available:
        lines += [
            "⚠️ <i>غير مضبوطة.</i>",
            "<i>أضف EMBEDDING_API_KEY، أو استخدم Ollama محليًا "
            "بـ EMBEDDING_BASE_URL.</i>",
            "",
        ]
    lines += [
        f"الحالة: {kb.state_label(status.enabled)}",
        f"العناصر المفهرسة: <code>{status.count}</code>",
        f"المحرّك: <code>{_esc(status.backend)}</code>",
    ]
    if latest:
        lines += ["", "<b>أحدث ما فُهرس:</b>"]
        for item in latest[:5]:
            speaker = "أنت" if item.role == "assistant" else "المستخدم"
            lines.append(f"· <i>[{speaker}] {_esc(item.preview)}</i>")
    lines += [
        "",
        "<i>الذاكرة تُستدعى تلقائيًا حين يشبه سؤالك محادثة سابقة.</i>",
    ]
    return Screen("\n".join(lines), kb.memory_menu(status.enabled, status.count))


def confirm_forget() -> Screen:
    return Screen(
        "<b>🗑️ مسح كل الذاكرة</b>\n\n"
        "<i>سيُحذف كل ما فُهرس. سجلّ المحادثة نفسه لا يتأثر، "
        "ويمكنك إعادة الفهرسة منه لاحقًا.</i>",
        kb.confirm_menu("forget", "", cancel_route=("memory",)),
    )


def search_screen(s: BotSettings, *, provider: str, available: bool) -> Screen:
    lines = ["<b>🔍 بحث الويب</b>", ""]
    if not available:
        lines += [
            "⚠️ <i>غير مضبوط.</i>",
            "<i>أضف SERPER_API_KEY أو BRAVE_API_KEY في البيئة.</i>",
            "",
        ]
    lines += [
        f"الحالة: {kb.state_label(s.web_search_enabled)}",
        f"المزوّد: <code>{_esc(provider)}</code>",
        "",
        "<i>عند التفعيل، يستدعي النموذج البحث تلقائيًا حين يحتاج "
        "معلومة حديثة. أو استخدم <code>/search عبارة</code> يدويًا.</i>",
    ]
    return Screen("\n".join(lines), kb.search_menu(s.web_search_enabled, available))


# ───────────────────────── المزوّدات المخصّصة ─────────────────────────

def custom_providers(docs: Sequence[Any]) -> Screen:
    lines = ["<b>🔧 المزوّدات المخصّصة</b>", ""]
    if not docs:
        lines += [
            "<i>لا مزوّدات بعد.</i>",
            "",
            "<i>أضف Ollama أو LM Studio أو vLLM أو أي نقطة نهاية "
            "متوافقة مع OpenAI API.</i>",
        ]
    for doc in docs:
        lines.append(f"🔧 <b>{_esc(doc.name)}</b>")
        lines.append(f"   <code>{_esc(doc.base_url)}</code>")
        lines.append(f"   النموذج: <code>{_esc(doc.model_name)}</code>")
    return Screen("\n".join(lines), kb.custom_providers_menu(docs))


def custom_detail(doc: Any) -> Screen:
    return Screen(
        f"<b>🔧 {_esc(doc.name)}</b>\n\n"
        f"العنوان: <code>{_esc(doc.base_url)}</code>\n"
        f"النموذج: <code>{_esc(doc.model_name)}</code>\n"
        f"المفتاح: <code>{_esc(doc.key_hint)}</code>\n"
        f"أُضيف: <code>{doc.created_at.strftime('%Y-%m-%d')}</code>",
        kb.custom_detail_menu(doc.key),
    )


def custom_saved(doc: Any, *, probe_ok: bool, probe_detail: str) -> Screen:
    icon = "✅" if probe_ok else "⚠️"
    return Screen(
        "<b>➕ حُفظ المزوّد المخصّص</b>\n\n"
        f"الاسم: <code>{_esc(doc.name)}</code>\n"
        f"العنوان: <code>{_esc(doc.base_url)}</code>\n"
        f"النموذج: <code>{_esc(doc.model_name)}</code>\n"
        f"المفتاح: <code>{_esc(doc.key_hint)}</code>\n\n"
        f"{icon} <i>{_esc(probe_detail)}</i>\n\n"
        "<i>ظهر الآن في قائمة الخدمات.</i>",
        kb.custom_detail_menu(doc.key),
    )


def confirm_custom_delete(doc: Any, *, selected: bool) -> Screen:
    warning = (
        "\n\n<b>⚠️ هذا مزوّد النموذج المُستخدَم حاليًا</b> — "
        "سيعود الاختيار إلى الافتراضي."
        if selected
        else ""
    )
    return Screen(
        f"<b>🗑️ حذف «{_esc(doc.name)}»</b>\n\n"
        f"العنوان: <code>{_esc(doc.base_url)}</code>"
        f"{warning}\n\n"
        "<i>ستُحذف مفاتيحه أيضًا. المحادثات السابقة تبقى كما هي.</i>",
        kb.confirm_menu("cxrm", doc.key, cancel_route=("cx", doc.key)),
    )


# ───────────────────────── المفاتيح ─────────────────────────

def keys_overview(
    statuses: Sequence[Any], *, strategy: str, encrypted: bool
) -> Screen:
    lines = ["<b>🔑 مفاتيح المزوّدات</b>", ""]
    for status in statuses:
        icon = "🟢" if status.healthy else "🔴"
        lines.append(f"{icon} <b>{_esc(status.label)}</b> — {_esc(status.summary)}")
    lines += [
        "",
        "الاستراتيجية: "
        + (
            "<code>دوري (round robin)</code>"
            if strategy == "round_robin"
            else "<code>عشوائي (random)</code>"
        ),
    ]
    if not encrypted:
        lines += [
            "",
            "<b>⚠️ المفاتيح مخزَّنة نصًا صريحًا.</b> "
            "<i>اضبط CONTENT_ENCRYPTION_KEY لتشفيرها.</i>",
        ]
    lines += ["", "<i>🟢 نشط · 🟡 عليه فشل · 🧊 مبرَّد مؤقتًا · 🔴 معطّل</i>"]
    return Screen("\n".join(lines), kb.keys_providers_menu(statuses, strategy))


def keys_for_provider(
    status: Any, keys: Sequence[Any], *, threshold: int
) -> Screen:
    lines = [f"<b>🔑 {_esc(status.label)}</b>", ""]
    if not keys:
        lines.append("<i>لا مفاتيح مُدارة لهذا المزوّد.</i>")
        if status.env_fallback:
            lines.append("<i>يعمل حاليًا بمفتاح من .env أو من إعداد المزوّد.</i>")
    for key in keys:
        icon = kb.KEY_STATE.get(key.state, "⚪️")
        lines.append(f"{icon} <code>{_esc(key.display)}</code>")
        bits = []
        if key.success_count:
            bits.append(f"{key.success_count} نجاح")
        if key.failure_count:
            bits.append(f"{key.failure_count}/{threshold} فشل متتالٍ")
        if key.cooling():
            bits.append("مبرَّد")
        if not key.is_active and key.disabled_reason:
            bits.append(f"عُطِّل: {key.disabled_reason}")
        if key.last_used_at:
            bits.append(f"آخر استخدام {key.last_used_at.strftime('%m-%d %H:%M')}")
        if bits:
            lines.append(f"   <i>{_esc(' · '.join(bits))}</i>")
    lines += [
        "",
        f"<i>يُعطَّل المفتاح تلقائيًا بعد {threshold} فشل متتالٍ في المصادقة "
        "أو الحصة. الـ 429 يبرّده مؤقتًا فقط، وأي نجاح يصفّر العدّاد.</i>",
    ]
    return Screen("\n".join(lines), kb.keys_menu(status.name, keys))


def key_detail(key: Any, status: Any) -> Screen:
    return Screen(
        f"<b>🔑 {_esc(key.display)}</b>\n\n"
        f"المزوّد: <code>{_esc(status.label)}</code>\n"
        f"الحالة: {kb.KEY_STATE.get(key.state, '⚪️')} <code>{_esc(key.state)}</code>\n"
        f"نجاحات: <code>{key.success_count}</code> · "
        f"فشل متتالٍ: <code>{key.failure_count}</code> · "
        f"إجمالي: <code>{key.total_failures}</code>\n"
        f"أُضيف: <code>{key.created_at.strftime('%Y-%m-%d')}</code>\n\n"
        "<i>القيمة الكاملة لا تُعرض أبدًا. للتبديل احذف وأضف غيره.</i>",
        kb.key_detail_menu(key.key_id, status.name, active=key.is_active),
    )


def confirm_key_delete(key: Any, status: Any) -> Screen:
    remaining = status.active - (1 if key.usable else 0)
    warning = (
        "\n\n<b>⚠️ لن يبقَ أي مفتاح صالح لهذا المزوّد</b> — نماذجه ستتوقّف."
        if remaining <= 0 and not status.env_fallback
        else f"\n\nمفاتيح صالحة بعد الحذف: <code>{max(0, remaining)}</code>"
    )
    return Screen(
        f"<b>🗑️ حذف مفتاح</b>\n\n"
        f"المزوّد: <code>{_esc(status.label)}</code>\n"
        f"المفتاح: <code>{_esc(key.display)}</code>"
        f"{warning}\n\n"
        "<i>الحذف نهائي. المفتاح نفسه لا يتأثر عند المزوّد.</i>",
        kb.confirm_menu("krm", key.key_id, cancel_route=("keys", status.name)),
    )


# ───────────────────────── البيانات ─────────────────────────

def data_menu(stats: Any) -> Screen:
    return Screen(
        "<b>📦 البيانات والنسخ الاحتياطي</b>\n\n"
        f"الرسائل: <code>{stats.messages}</code>\n"
        f"الشخصيات: <code>{stats.personas}</code>\n"
        f"عناصر الذاكرة: <code>{stats.memories}</code>\n"
        f"المزوّدات المخصّصة: <code>{stats.custom_providers}</code>\n"
        f"مفاتيح API: <code>{stats.provider_keys}</code>\n\n"
        "<i>التصدير يشمل كل شيء عدا قيم المفاتيح — "
        "لا تمرّ عبر تليجرام أبدًا.</i>",
        kb.data_menu(),
    )


def confirm_wipe(stats: Any) -> Screen:
    return Screen(
        "<b>⚠️ حذف كل البيانات</b>\n\n"
        "سيُحذف نهائيًا:\n"
        f"· <code>{stats.messages}</code> رسالة\n"
        f"· <code>{stats.memories}</code> عنصر ذاكرة\n"
        f"· <code>{stats.personas}</code> شخصية مخصّصة\n"
        f"· <code>{stats.custom_providers}</code> مزوّد مخصّص\n"
        f"· <code>{stats.provider_keys}</code> مفتاح API\n\n"
        "<b>لا رجوع في هذا.</b>\n"
        "<i>إن كان KEY_SEED_FROM_ENV=true ومفاتيحك في .env، "
        "ستُبذر عند التشغيل التالي. وإلا فستحتاج إضافتها يدويًا.</i>\n\n"
        "<i>خذ نسخة احتياطية أولًا (📤 تصدير).</i>",
        kb.confirm_wipe_menu(),
    )


def wipe_done(report: Any) -> Screen:
    lines = ["<b>🗑️ حُذفت البيانات</b>", ""]
    for label, count in report.rows():
        lines.append(f"· {label}: <code>{count}</code>")
    if report.errors:
        lines += ["", "<b>أخطاء:</b>"]
        lines.extend(f"· <i>{_esc(item)}</i>" for item in report.errors)
    lines += ["", "<i>أُعيدت الإعدادات إلى الافتراضي.</i>"]
    return Screen("\n".join(lines), kb.back_only())


def confirm_reset() -> Screen:
    return Screen(
        "<b>🔄 إعادة تعيين المحادثة</b>\n\n"
        "<i>سيُحذف سجلّ المحادثة الحالي. الذاكرة الدلالية والشخصيات "
        "والإعدادات تبقى كما هي.</i>",
        kb.confirm_menu("reset", "", cancel_route=("home",)),
    )


# ───────────────────────── الأخطاء ─────────────────────────

def error(message: str, *, hint: str = "") -> Screen:
    body = f"<b>❌ خطأ</b>\n\n<code>{_esc(message)}</code>"
    if hint:
        body += f"\n\n<i>{_esc(hint)}</i>"
    return Screen(body, kb.back_only())
