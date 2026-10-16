"""Админ-раздел «🌐 AI на сайте»: включение помощника и настройка всех его возможностей.

Путь: «Интеграции → AI-суппорт → AI на сайте». Платная функция (лицензия «site_ai»).
Помощник работает на публичных страницах сайта для гостей и для клиентов, вошедших
в кабинет. Каждая возможность включается и выключается отдельно."""
import asyncio
import logging
import re

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder

from bot.keyboards.admin_misc import back_button, home_button
from bot.services import site_ai
from bot.states.admin_states import AdminStates
from bot.utils.admin import is_admin
from bot.utils.text import escape_html, get_message_text_for_storage, safe_edit_or_send

logger = logging.getLogger(__name__)
router = Router()
router.callback_query.filter(lambda c: is_admin(c.from_user.id))
router.message.filter(lambda m: m.from_user is not None and is_admin(m.from_user.id))

MAIN = "admin_site_ai"
BACK_TO = "admin_integrations_apikeys"

# раздел -> (заголовок, пояснение, ключи переключателей)
SECTIONS = {
    "who": ("👥 Кому и где",
            "Для кого работает помощник и на каких страницах он появляется.",
            ["guest", "client", "page_welcome", "page_shop", "escalate"]),
    "guest": ("🛒 Помощь гостям",
              "Что помощник умеет для посетителя, который ещё не вошёл в кабинет. "
              "Данных клиентов и действий с ключами у гостя нет.",
              ["g_tariffs", "g_trial", "g_buy_link", "g_kb"]),
    "client": ("👤 Помощь клиентам",
               "Что помощник умеет для клиента, вошедшего в кабинет через Telegram. "
               "Действия с ключами по умолчанию выключены, включайте только то, что нужно.",
               ["c_kb", "c_tariffs", "c_status", "c_devices", "c_devices_manage", "c_autorenew", "c_payment", "c_promo", "c_web"]),
}


def _nav(back: str):
    return back_button(back), home_button()


def _dot(on: bool) -> str:
    return "🟢" if on else "⚪"


async def _licensed(callback: CallbackQuery) -> bool:
    if not site_ai.is_licensed():
        await callback.answer("🌐 AI на сайте доступен только по лицензии", show_alert=True)
        return False
    return True


# ------------------------------------------------------------------ главный экран --

def _main_text() -> str:
    enabled = site_ai.get_bool("enabled")
    lines = [
        "🌐 <b>AI на сайте</b>",
        "",
        "Помощник в чате на сайте: советует тариф и помогает купить, "
        "объясняет подключение, решает вопросы клиентов по подписке.",
        "",
        f"<b>Состояние:</b> {'включён' if site_ai.is_active() else 'выключен'}",
        f"<b>Вопросов сегодня:</b> {site_ai.requests_today()} из {site_ai.get_int('daily_total')}",
        f"<b>Гости:</b> {'да' if site_ai.get_bool('guest') else 'нет'} · "
        f"<b>клиенты:</b> {'да' if site_ai.get_bool('client') else 'нет'}",
    ]
    if enabled and not site_ai.is_active():
        lines += ["", "⚠️ Включён, но не работает: проверьте, что включён «AI-суппорт» и заданы ключи Groq или Gemini."]
    lines += ["", "<i>Сначала включите помощника, потом при необходимости настройте, что он умеет.</i>"]
    return "\n".join(lines)


def _main_kb() -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.row(InlineKeyboardButton(text=f"{_dot(site_ai.get_bool('enabled'))} AI на сайте: "
                                    f"{'включён' if site_ai.get_bool('enabled') else 'выключен'}",
                               callback_data="admin_site_ai_main_toggle"))
    for key, (title, _hint, _keys) in SECTIONS.items():
        b.row(InlineKeyboardButton(text=title, callback_data=f"admin_site_ai_s:{key}"))
    b.row(InlineKeyboardButton(text="🛡 Лимиты и защита", callback_data="admin_site_ai_limits"))
    b.row(InlineKeyboardButton(text="✍️ Тексты и правила", callback_data="admin_site_ai_texts"))
    b.row(InlineKeyboardButton(text="🧪 Проверить помощника", callback_data="admin_site_ai_selftest"))
    b.row(*_nav(BACK_TO))
    return b.as_markup()


async def _show_main(callback: CallbackQuery, state: FSMContext):
    await state.set_state(AdminStates.admin_menu)
    await safe_edit_or_send(callback.message, _main_text(), reply_markup=_main_kb())


@router.callback_query(F.data == MAIN)
async def admin_site_ai(callback: CallbackQuery, state: FSMContext):
    if not await _licensed(callback):
        return
    await callback.answer()
    await _show_main(callback, state)


@router.callback_query(F.data == "admin_site_ai_main_toggle")
async def admin_site_ai_main_toggle(callback: CallbackQuery, state: FSMContext):
    if not await _licensed(callback):
        return
    new = not site_ai.get_bool("enabled")
    site_ai.set_bool("enabled", new)
    await callback.answer("✅ Помощник включён на сайте" if new else "⚪ Помощник выключен")
    await _show_main(callback, state)


# --------------------------------------------------------------------- переключатели --

def _section_text(section: str) -> str:
    title, hint, keys = SECTIONS[section]
    lines = [f"<b>{title}</b>", "", hint, ""]
    for key in keys:
        _default, label, tip = site_ai.BOOL_SETTINGS[key]
        lines.append(f"{_dot(site_ai.get_bool(key))} <b>{escape_html(label)}</b>\n{escape_html(tip)}")
    return "\n".join(lines)


def _section_kb(section: str) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    for key in SECTIONS[section][2]:
        b.row(InlineKeyboardButton(text=f"{_dot(site_ai.get_bool(key))} {site_ai.BOOL_SETTINGS[key][1]}",
                                   callback_data=f"admin_site_ai_t:{section}:{key}"))
    b.row(*_nav(MAIN))
    return b.as_markup()


@router.callback_query(F.data.startswith("admin_site_ai_s:"))
async def admin_site_ai_section(callback: CallbackQuery, state: FSMContext):
    if not await _licensed(callback):
        return
    section = callback.data.split(":", 1)[1]
    if section not in SECTIONS:
        await callback.answer()
        return
    await callback.answer()
    await safe_edit_or_send(callback.message, _section_text(section), reply_markup=_section_kb(section))


@router.callback_query(F.data.startswith("admin_site_ai_t:"))
async def admin_site_ai_toggle(callback: CallbackQuery, state: FSMContext):
    if not await _licensed(callback):
        return
    try:
        _, section, key = callback.data.split(":")
    except ValueError:
        await callback.answer()
        return
    if section not in SECTIONS or key not in SECTIONS[section][2]:
        await callback.answer()
        return
    new = not site_ai.get_bool(key)
    site_ai.set_bool(key, new)
    await callback.answer(f"{'🟢 Включено' if new else '⚪ Выключено'}: {site_ai.BOOL_SETTINGS[key][1]}")
    await safe_edit_or_send(callback.message, _section_text(section), reply_markup=_section_kb(section))


# ---------------------------------------------------------------------------- лимиты --

def _limits_text() -> str:
    lines = ["🛡 <b>Лимиты и защита</b>", "",
             "Публичный чат не должен тратить ваши лимиты Groq и Gemini впустую. Нажмите на параметр, чтобы изменить.", ""]
    for key, (_d, lo, hi, label, tip) in site_ai.INT_SETTINGS.items():
        lines.append(f"<b>{escape_html(label)}:</b> {site_ai.get_int(key)}\n{escape_html(tip)} (от {lo} до {hi})")
    return "\n".join(lines)


def _limits_kb() -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    for key, spec in site_ai.INT_SETTINGS.items():
        b.row(InlineKeyboardButton(text=f"{spec[3]}: {site_ai.get_int(key)}", callback_data=f"admin_site_ai_e:i:{key}"))
    b.row(*_nav(MAIN))
    return b.as_markup()


@router.callback_query(F.data == "admin_site_ai_limits")
async def admin_site_ai_limits(callback: CallbackQuery, state: FSMContext):
    if not await _licensed(callback):
        return
    await callback.answer()
    await state.set_state(AdminStates.admin_menu)
    await safe_edit_or_send(callback.message, _limits_text(), reply_markup=_limits_kb())


# ---------------------------------------------------------------------------- тексты --

def _texts_text() -> str:
    lines = ["✍️ <b>Тексты и правила</b>", "", "Нажмите на пункт, чтобы изменить.", ""]
    for key, (_d, label, tip, _mx) in site_ai.TEXT_SETTINGS.items():
        value = site_ai.get_text(key).strip()
        shown = escape_html(value.replace("\n", " / ")[:80] + ("…" if len(value) > 80 else "")) if value else "<i>не задано</i>"
        lines.append(f"<b>{escape_html(label)}:</b> {shown}")
    return "\n".join(lines)


def _texts_kb() -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    for key, spec in site_ai.TEXT_SETTINGS.items():
        b.row(InlineKeyboardButton(text=f"✏️ {spec[1]}", callback_data=f"admin_site_ai_e:t:{key}"))
    b.row(*_nav(MAIN))
    return b.as_markup()


@router.callback_query(F.data == "admin_site_ai_texts")
async def admin_site_ai_texts(callback: CallbackQuery, state: FSMContext):
    if not await _licensed(callback):
        return
    await callback.answer()
    await state.set_state(AdminStates.admin_menu)
    await safe_edit_or_send(callback.message, _texts_text(), reply_markup=_texts_kb())


# ------------------------------------------------------------------------- редактор --

def _edit_back(kind: str) -> str:
    return "admin_site_ai_limits" if kind == "i" else "admin_site_ai_texts"


@router.callback_query(F.data.startswith("admin_site_ai_e:"))
async def admin_site_ai_edit(callback: CallbackQuery, state: FSMContext):
    if not await _licensed(callback):
        return
    try:
        _, kind, key = callback.data.split(":")
    except ValueError:
        await callback.answer()
        return
    if kind == "i" and key in site_ai.INT_SETTINGS:
        _d, lo, hi, label, tip = site_ai.INT_SETTINGS[key]
        text = (f"✏️ <b>{escape_html(label)}</b>\n\nСейчас: <b>{site_ai.get_int(key)}</b>\n{escape_html(tip)}\n\n"
                f"Введите число от {lo} до {hi}.")
        rows = [[InlineKeyboardButton(text="↩️ По умолчанию", callback_data=f"admin_site_ai_d:i:{key}")]]
    elif kind == "t" and key in site_ai.TEXT_SETTINGS:
        _d, label, tip, mx = site_ai.TEXT_SETTINGS[key]
        current = site_ai.get_text(key).strip()
        text = (f"✏️ <b>{escape_html(label)}</b>\n\n{escape_html(tip)}\n\n"
                f"Сейчас:\n<code>{escape_html(current) if current else 'не задано'}</code>\n\n"
                f"Пришлите новый текст (до {mx} символов).")
        if key == "rules":
            text += " Чтобы очистить, пришлите <code>0</code>."
        rows = [[InlineKeyboardButton(text="↩️ По умолчанию", callback_data=f"admin_site_ai_d:t:{key}")]]
    else:
        await callback.answer()
        return
    rows.append(list(_nav(_edit_back(kind))))
    await state.set_state(AdminStates.site_ai_edit_value)
    await state.update_data(site_ai_kind=kind, site_ai_key=key)
    await safe_edit_or_send(callback.message, text, reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))
    await callback.answer()


@router.callback_query(F.data.startswith("admin_site_ai_d:"))
async def admin_site_ai_default(callback: CallbackQuery, state: FSMContext):
    if not await _licensed(callback):
        return
    try:
        _, kind, key = callback.data.split(":")
    except ValueError:
        await callback.answer()
        return
    if kind == "i" and key in site_ai.INT_SETTINGS:
        site_ai.set_int(key, site_ai.INT_SETTINGS[key][0])
    elif kind == "t" and key in site_ai.TEXT_SETTINGS:
        site_ai.reset_text(key)
    else:
        await callback.answer()
        return
    await callback.answer("Вернул значение по умолчанию")
    await state.set_state(AdminStates.admin_menu)
    if kind == "i":
        await safe_edit_or_send(callback.message, _limits_text(), reply_markup=_limits_kb())
    else:
        await safe_edit_or_send(callback.message, _texts_text(), reply_markup=_texts_kb())


@router.message(AdminStates.site_ai_edit_value, F.text, ~F.text.startswith("/"))
async def admin_site_ai_value(message: Message, state: FSMContext):
    try:
        await message.delete()
    except Exception:
        pass
    data = await state.get_data()
    kind, key = data.get("site_ai_kind"), data.get("site_ai_key")
    raw = get_message_text_for_storage(message, "plain").strip()
    back_kb = InlineKeyboardMarkup(inline_keyboard=[list(_nav(_edit_back(kind or "t")))])
    if kind == "i" and key in site_ai.INT_SETTINGS:
        _d, lo, hi, label, _tip = site_ai.INT_SETTINGS[key]
        if not raw.isdigit() or not lo <= int(raw) <= hi:
            await safe_edit_or_send(message, f"❌ Введите целое число от {lo} до {hi}.", reply_markup=back_kb, force_new=True)
            return
        site_ai.set_int(key, int(raw))
        await state.set_state(AdminStates.admin_menu)
        await safe_edit_or_send(message, "✅ <b>Сохранено</b>\n\n" + _limits_text(), reply_markup=_limits_kb(), force_new=True)
        return
    if kind == "t" and key in site_ai.TEXT_SETTINGS:
        mx = site_ai.TEXT_SETTINGS[key][3]
        if len(raw) > mx:
            await safe_edit_or_send(message, f"❌ Слишком длинный текст: максимум {mx} символов.", reply_markup=back_kb, force_new=True)
            return
        if not raw or (raw == "0" and key != "rules"):
            await safe_edit_or_send(message, "❌ Пришлите текст. Чтобы вернуть значение по умолчанию, нажмите «↩️ По умолчанию».",
                                    reply_markup=back_kb, force_new=True)
            return
        site_ai.set_text(key, "" if raw == "0" else raw)
        await state.set_state(AdminStates.admin_menu)
        await safe_edit_or_send(message, "✅ <b>Сохранено</b>\n\n" + _texts_text(), reply_markup=_texts_kb(), force_new=True)
        return
    await state.set_state(AdminStates.admin_menu)


# ------------------------------------------------------------------ самопроверка --

_ICON = {"ok": "✅", "retried": "🟡", "blocked": "🔴", "bad": "🔴", "error": "⚪"}


async def _ask_assistant(question: str) -> dict:
    """Один вопрос в режиме гостя напрямую в AI-сервис (без лимитов сайта)."""
    import uuid
    import aiohttp
    from config import SUPPORT_API_TOKEN
    async with aiohttp.ClientSession() as session:
        async with session.post(
            "http://127.0.0.1:8086/consult_site",
            json={"mode": "guest", "message": question, "session_id": "selftest" + uuid.uuid4().hex[:12], "user_id": None},
            headers={"X-Support-Token": SUPPORT_API_TOKEN},
            timeout=aiohttp.ClientTimeout(total=70),
        ) as resp:
            if resp.status != 200:
                raise RuntimeError(f"AI-сервис ответил {resp.status}")
            return await resp.json()


def _selftest_text(done: list, total: int, finished: bool) -> str:
    lines = ["🧪 <b>Проверка помощника</b>", ""]
    if not finished:
        lines += [f"⏳ Задаю вопросы-ловушки: {len(done)} из {total}…", ""]
    for status, question, reply, notes in done:
        plain = re.sub(r"<[^>]+>", "", (reply or "")).replace("\n", " ")
        excerpt = escape_html(plain[:160] + ("…" if len(plain) > 160 else ""))
        extra = f"\n   ⚠️ {escape_html(', '.join(notes))}" if notes else ""
        lines.append(f"{_ICON[status]} <b>{escape_html(question)}</b>\n   <i>{excerpt}</i>{extra}")
    if finished:
        counts = {k: sum(1 for d in done if d[0] == k) for k in ("ok", "retried", "blocked", "bad", "error")}
        lines += ["", f"✅ чисто: {counts['ok']} · 🟡 исправлено защитой: {counts['retried']} · 🔴 проблемы: {counts['blocked'] + counts['bad']}"
                      + (f" · ⚪ ошибки: {counts['error']}" if counts["error"] else ""), ""]
        lines += ["✅ — ответ опирается на реальные данные.",
                  "🟡 — модель сначала назвала то, чего нет в данных, защита заставила её переписать ответ.",
                  "🔴 — модель выдумывала и после повтора; клиент увидит безопасный ответ «уточните на странице покупки».",
                  "Если много 🟡 или 🔴, добавьте в «✍️ Тексты и правила» точные формулировки, например про устройства."]
    return "\n".join(lines)[:4000]


def _selftest_kb(finished: bool) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    if finished:
        b.row(InlineKeyboardButton(text="🔄 Проверить ещё раз", callback_data="admin_site_ai_selftest"))
    b.row(*_nav(MAIN))
    return b.as_markup()


@router.callback_query(F.data == "admin_site_ai_selftest")
async def admin_site_ai_selftest(callback: CallbackQuery, state: FSMContext):
    if not await _licensed(callback):
        return
    if not site_ai.is_active() or not site_ai.mode_enabled(site_ai.MODE_GUEST):
        await callback.answer("Сначала включите помощника и режим для гостей", show_alert=True)
        return
    await callback.answer("⏳ Проверяю, это займёт около минуты")
    total, done = len(site_ai.TEST_QUESTIONS), []
    await safe_edit_or_send(callback.message, _selftest_text(done, total, False), reply_markup=_selftest_kb(False))
    for kind, question in site_ai.TEST_QUESTIONS:
        try:
            data = await _ask_assistant(question)
            if data.get("error"):          # помощник упал (например, лимит модели) — одна повторная попытка
                await asyncio.sleep(6)
                data = await _ask_assistant(question)
            if data.get("error"):
                raise RuntimeError(data["error"])
            reply = data.get("reply") or ""
            notes = list(data.get("violations") or []) + site_ai.trap_flags(kind, reply)
            status = data.get("guard", "ok")
            if status == "ok" and notes:
                status = "bad"
            if status == "blocked":
                notes = ["ответ заменён безопасным"]
            done.append((status, question, reply, notes))
        except Exception as e:
            logger.warning(f"site_ai selftest: {e}")
            done.append(("error", question, f"не удалось получить ответ: {str(e)[:120]}", []))
        await asyncio.sleep(2)             # пауза между вопросами, чтобы не упереться в лимит модели
        await safe_edit_or_send(callback.message, _selftest_text(done, total, len(done) == total),
                                reply_markup=_selftest_kb(len(done) == total))
