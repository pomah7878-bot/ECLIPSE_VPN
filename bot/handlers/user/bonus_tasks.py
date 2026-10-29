"""Бонусные дни за задания — отзыв и сторис (платная функция bonus_tasks).

Как работает:
• клиент открывает «🎁 Бонусные дни» и выбирает задание;
• на экране задания — пошаговая инструкция, ссылка и кнопка «📎 Отправить скриншот»;
• админы получают скриншот с кнопками «✅ Начислить» / «❌ Отклонить»;
• после одобрения дни добавляются к активному ключу клиента.

Каждое задание — один раз на клиента. Отклонённую заявку можно прислать снова.
Где оставлять отзыв: ссылка из настроек, иначе новостной канал бота; если нет
ни того, ни другого — задание «Отзыв» клиентам не показывается.
Админам кнопка на главной не показывается (для проверки — «👁 Как видит клиент»).
Управление: «📣 Маркетинг» → «🎁 Бонусные дни». По умолчанию выключено.
"""
import html
import logging

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder

logger = logging.getLogger(__name__)
router = Router()

FEATURE = "bonus_tasks"
TASKS = {
    "story": {"icon": "📸", "title": "Сторис", "setting": "bonus_story_days"},
    "review": {"icon": "⭐", "title": "Отзыв", "setting": "bonus_review_days"},
}
DEFAULT_DAYS = 7
DAYS_CHOICES = (0, 1, 3, 5, 7, 14, 30)


class BonusStates(StatesGroup):
    waiting_screenshot = State()
    admin_review_url = State()


# ── настройки ────────────────────────────────────────────────────────────────

def _setting(key: str, default: str) -> str:
    from database.requests import get_setting
    value = get_setting(key, default)
    return default if value is None else str(value)


def is_licensed() -> bool:
    try:
        from bot.services.license import is_feature_available
        return is_feature_available(FEATURE)
    except Exception:
        return False


def is_enabled() -> bool:
    try:
        return is_licensed() and _setting("bonus_tasks_enabled", "0") == "1"
    except Exception:
        return False


def task_days(task: str) -> int:
    try:
        return max(0, min(365, int(_setting(TASKS[task]["setting"], str(DEFAULT_DAYS)))))
    except (ValueError, KeyError):
        return DEFAULT_DAYS


def review_url() -> str:
    """Ссылка из настроек (куда писать отзыв)."""
    url = _setting("bonus_review_url", "").strip()
    return url if url.startswith(("https://", "http://", "tg://")) else ""


def review_target() -> str:
    """Куда клиент пишет отзыв: ссылка из настроек, иначе новостной канал бота."""
    url = review_url()
    if url:
        return url
    try:
        from database.requests import get_marketing_channel_id
        channel = (get_marketing_channel_id() or "").strip().lstrip("@")
        if channel:
            from bot.utils.telegram_links import build_telegram_link
            return build_telegram_link(channel)
    except Exception:
        pass
    return ""


def active_tasks() -> list:
    tasks = [t for t in TASKS if task_days(t) > 0]
    if "review" in tasks and not review_target():
        tasks.remove("review")  # некуда оставлять отзыв — задание не показываем
    return tasks


def is_visible(telegram_id: int | None = None) -> bool:
    """Показывать ли кнопку «🎁 Бонусные дни» на главной (админам — нет)."""
    if telegram_id is not None and _is_admin(telegram_id):
        return False
    return is_enabled() and bool(active_tasks())


def pending_count() -> int:
    """Сколько заявок ждут проверки (для значка 🔔 в меню админа). Без лицензии — 0."""
    try:
        if not is_licensed():
            return 0
        from database.db_bonus_tasks import stats
        return int(stats().get("pending", 0))
    except Exception:
        return 0


def pending_badge() -> str:
    n = pending_count()
    return f" · 🔔 {n}" if n else ""


def _plural_days(n: int) -> str:
    if 11 <= n % 100 <= 14:
        return "дней"
    return {1: "день", 2: "дня", 3: "дня", 4: "дня"}.get(n % 10, "дней")


def _days(n: int) -> str:
    return f"+{n} {_plural_days(n)}"


def _nav_row(back: str = "start") -> list:
    return [InlineKeyboardButton(text="⬅️ Назад", callback_data=back),
            InlineKeyboardButton(text="🈴 На главную", callback_data="start")]


def story_link(telegram_id: int) -> str:
    """Ссылка для сторис: личная реферальная (если рефералка включена), иначе ссылка на бота."""
    from bot.utils.runtime_state import get_bot_username
    from bot.utils.telegram_links import build_telegram_link
    username = get_bot_username()
    if not username:
        return ""
    try:
        from database.requests import ensure_user_referral_code, get_user_internal_id, is_referral_enabled
        if is_referral_enabled():
            uid = get_user_internal_id(telegram_id)
            if uid:
                return build_telegram_link(username, f"ref_{ensure_user_referral_code(uid)}")
    except Exception:
        pass
    return build_telegram_link(username)


# ── клиент ───────────────────────────────────────────────────────────────────

def _claim_status(user_id: int | None, task: str):
    from database.db_bonus_tasks import get_active_claim
    claim = get_active_claim(user_id, task) if user_id else None
    return claim["status"] if claim else None


def _list_screen(user_id: int | None):
    lines = [
        "🎁 <b>Бонусные дни</b>",
        "",
        "Получите дни подписки бесплатно — выполните задание и пришлите скриншот.",
        "",
        "<b>Как это работает</b>",
        "1️⃣ Выберите задание ниже",
        "2️⃣ Выполните его по инструкции",
        "3️⃣ Пришлите скриншот — мы проверим и начислим дни",
        "",
        "<b>Задания</b>",
    ]
    b = InlineKeyboardBuilder()
    for task in active_tasks():
        meta, days = TASKS[task], task_days(task)
        status = _claim_status(user_id, task)
        mark = {"approved": "✅ получено", "pending": "⏳ на проверке"}.get(status, _days(days))
        lines.append(f"{meta['icon']} {meta['title']} — {mark}")
        b.row(InlineKeyboardButton(text=f"{meta['icon']} {meta['title']} — {mark}", callback_data=f"bonus_task:{task}"))
    lines += ["", "ℹ️ Каждое задание — один раз. Дни добавятся к вашему активному ключу."]
    b.row(*_nav_row())
    return "\n".join(lines), b.as_markup()


def _task_screen(task: str, telegram_id: int, user_id: int | None):
    meta, days = TASKS[task], task_days(task)
    status = _claim_status(user_id, task)
    b = InlineKeyboardBuilder()
    lines = [f"{meta['icon']} <b>{meta['title']}</b> — {_days(days)} к подписке", ""]
    if task == "story":
        link = story_link(telegram_id)
        lines += [
            "<b>Что сделать</b>",
            "1️⃣ Опубликуйте сторис в Telegram или Instagram и расскажите о нас.",
            "2️⃣ Добавьте в сторис ссылку на бота" + (" (ниже)." if link else "."),
            "3️⃣ Не удаляйте сторис хотя бы 24 часа.",
            "4️⃣ Сделайте скриншот опубликованной сторис и пришлите его сюда.",
        ]
        if link:
            lines += ["", "🔗 Ссылка для сторис (нажмите, чтобы скопировать):", f"<code>{html.escape(link)}</code>"]
    else:
        target = review_target()
        lines += [
            "<b>Что сделать</b>",
            "1️⃣ Нажмите «⭐ Оставить отзыв» — откроется место для отзывов.",
            "2️⃣ Напишите пару предложений: скорость, стабильность, удобство.",
            "3️⃣ Сделайте скриншот своего отзыва и пришлите его сюда.",
        ]
        if target:
            b.row(InlineKeyboardButton(text="⭐ Оставить отзыв", url=target))
    lines.append("")
    if status == "approved":
        lines.append("✅ Вы уже получили дни за это задание. Спасибо!")
    elif status == "pending":
        lines.append("⏳ Скриншот на проверке — дни начислим после проверки.")
    else:
        lines.append("Когда всё готово — нажмите «📎 Отправить скриншот».")
        b.row(InlineKeyboardButton(text="📎 Отправить скриншот", callback_data=f"bonus_send:{task}"))
    b.row(*_nav_row("bonus_tasks"))
    return "\n".join(lines), b.as_markup()


@router.callback_query(F.data == "bonus_tasks")
async def cb_bonus_tasks(callback: CallbackQuery, state: FSMContext):
    from bot.utils.text import safe_edit_or_send
    from database.requests import get_user_internal_id
    await state.clear()
    if not (is_enabled() and active_tasks()):
        await callback.answer("Сейчас бонусных заданий нет", show_alert=True)
        return
    text, kb = _list_screen(get_user_internal_id(callback.from_user.id))
    await safe_edit_or_send(callback.message, text, reply_markup=kb)
    await callback.answer()


@router.callback_query(F.data.startswith("bonus_task:"))
async def cb_bonus_task(callback: CallbackQuery, state: FSMContext):
    from bot.utils.text import safe_edit_or_send
    from database.requests import get_user_internal_id
    await state.clear()
    task = callback.data.split(":", 1)[1]
    if task not in active_tasks() or not is_enabled():
        await callback.answer("Задание недоступно", show_alert=True)
        return
    text, kb = _task_screen(task, callback.from_user.id, get_user_internal_id(callback.from_user.id))
    await safe_edit_or_send(callback.message, text, reply_markup=kb)
    await callback.answer()


@router.callback_query(F.data.startswith("bonus_send:"))
async def cb_bonus_send(callback: CallbackQuery, state: FSMContext):
    from bot.utils.text import safe_edit_or_send
    from database.db_bonus_tasks import get_active_claim
    from database.requests import get_first_active_key_for_user, get_user_internal_id

    task = callback.data.split(":", 1)[1]
    if task not in active_tasks() or not is_enabled():
        await callback.answer("Задание недоступно", show_alert=True)
        return
    user_id = get_user_internal_id(callback.from_user.id)
    if not user_id:
        await callback.answer("Сначала нажмите /start", show_alert=True)
        return
    if not get_first_active_key_for_user(user_id):
        await callback.answer("Нужен активный ключ: бонусные дни добавляются к нему", show_alert=True)
        return
    if get_active_claim(user_id, task):
        await callback.answer("Заявка по этому заданию уже есть", show_alert=True)
        return
    meta = TASKS[task]
    await state.set_state(BonusStates.waiting_screenshot)
    await state.update_data(bonus_task=task)
    await safe_edit_or_send(
        callback.message,
        f"{meta['icon']} <b>{meta['title']}: скриншот</b>\n\n"
        "📎 Пришлите скриншот <b>одним фото</b> следующим сообщением.\n\n"
        "<i>Скрепка → Фото → выберите скриншот.</i>",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[_nav_row(f"bonus_task:{task}")]),
    )
    await callback.answer()


@router.message(BonusStates.waiting_screenshot, F.photo)
async def msg_bonus_screenshot(message: Message, state: FSMContext):
    from bot.services.user_locks import user_locks
    from database.db_bonus_tasks import create_claim
    from database.requests import get_user_internal_id

    data = await state.get_data()
    task = data.get("bonus_task")
    await state.clear()
    kb = InlineKeyboardMarkup(inline_keyboard=[_nav_row("bonus_tasks")])
    if task not in TASKS or not is_enabled() or task_days(task) <= 0:
        await message.answer("Задание сейчас недоступно.", reply_markup=kb)
        return
    user_id = get_user_internal_id(message.from_user.id)
    if not user_id:
        await message.answer("Сначала нажмите /start.", reply_markup=kb)
        return
    days = task_days(task)
    async with user_locks[user_id]:
        claim_id = create_claim(user_id, message.from_user.id, task, message.photo[-1].file_id, days)
    if claim_id is None:
        await message.answer("Заявка по этому заданию уже есть.", reply_markup=kb)
        return
    await message.answer(
        "✅ <b>Скриншот получен</b>\n\n"
        f"Проверим и начислим {_days(days)} к подписке. Придёт уведомление.",
        reply_markup=kb, parse_mode="HTML",
    )
    await _send_claim_to_admins(message.bot, claim_id)


@router.message(BonusStates.waiting_screenshot)
async def msg_bonus_not_photo(message: Message, state: FSMContext):
    if message.text and message.text.startswith("/"):
        await state.clear()
        return
    data = await state.get_data()
    await message.answer(
        "📎 Нужен скриншот <b>одним фото</b> (не файлом). Скрепка → Фото → выберите скриншот.",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[_nav_row(f"bonus_task:{data.get('bonus_task', 'story')}")]),
        parse_mode="HTML",
    )


# ── админы: проверка заявок ─────────────────────────────────────────────────

def _claim_caption(claim: dict, footer: str = "") -> str:
    from database.requests import get_user_by_id
    user = get_user_by_id(claim["user_id"]) or {}
    username = user.get("username")
    who = f"@{html.escape(username)}" if username else "без username"
    meta = TASKS.get(claim["task"], {"icon": "🎁", "title": claim["task"]})
    check = {
        "story": "сторис опубликована, видно наш бот или ссылку на него",
        "review": "отзыв настоящий и оставлен в нужном месте",
    }.get(claim["task"], "задание выполнено")
    text = (
        f"🔔 <b>Нужно проверить заявку на бонусные дни</b> №{claim['id']}\n\n"
        f"Задание: {meta['icon']} {meta['title']}\n"
        f"Клиент: {who} (<code>{claim['telegram_id']}</code>)\n"
        f"Награда: +{claim['days']} {_plural_days(int(claim['days']))}\n\n"
        f"🔎 Проверьте на скриншоте: {check}."
    )
    if footer:
        return text + f"\n\n{footer}"
    return text + "\n\nНажмите «✅ Начислить» или «❌ Отклонить»."


def _claim_kb(claim_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="✅ Начислить", callback_data=f"bonus_ok:{claim_id}"),
        InlineKeyboardButton(text="❌ Отклонить", callback_data=f"bonus_no:{claim_id}"),
    ]])


async def _send_claim_to_admins(bot, claim_id: int, only_admin: int | None = None) -> None:
    from config import ADMIN_IDS
    from database.db_bonus_tasks import get_claim
    claim = get_claim(claim_id)
    if not claim:
        return
    for admin_id in ([only_admin] if only_admin else ADMIN_IDS):
        try:
            await bot.send_photo(admin_id, claim["file_id"], caption=_claim_caption(claim),
                                 reply_markup=_claim_kb(claim_id), parse_mode="HTML")
        except Exception as e:
            logger.warning("Бонусные дни: не удалось отправить заявку %s админу %s: %s", claim_id, admin_id, e)


def _is_admin(user_id: int) -> bool:
    from bot.utils.admin import is_admin
    return is_admin(user_id)


def _admin_name(callback: CallbackQuery) -> str:
    u = callback.from_user
    name = getattr(u, "username", None)
    return f"@{html.escape(name)}" if name else f"админ {u.id}"


async def _finish_admin_message(callback: CallbackQuery, claim: dict, footer: str) -> None:
    """Проверенная заявка: итог в подписи + навигация. v1.344: сообщение больше не висит —
    «На главную» превращает его в главную, а /start и возврат на главную его убирают."""
    msg = callback.message
    try:
        await msg.edit_caption(
            caption=_claim_caption(claim, footer), parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[_nav_row("admin_bonus_tasks")]),
        )
    except Exception:
        pass
    try:
        from bot.services.main_menu_tracker import track_flow_message, track_sensitive
        track_flow_message(msg.chat.id, msg.message_id)
        track_sensitive(msg.chat.id, msg)
    except Exception as e:  # noqa: BLE001
        logger.debug("Бонусные дни: заявка не запомнена для уборки: %s", e)


@router.callback_query(F.data.startswith("bonus_ok:"))
async def cb_bonus_ok(callback: CallbackQuery):
    if not _is_admin(callback.from_user.id):
        await callback.answer("⛔ Доступ запрещён", show_alert=True)
        return
    from bot.services.rewards import grant_days_to_first_active_key
    from database.db_bonus_tasks import decide_claim, get_claim, reopen_claim

    claim_id = int(callback.data.split(":", 1)[1])
    claim = get_claim(claim_id)
    if not claim:
        await callback.answer("Заявка не найдена", show_alert=True)
        return
    if not decide_claim(claim_id, "approved", callback.from_user.id):
        await callback.answer("Заявку уже проверил другой админ", show_alert=True)
        await _finish_admin_message(callback, get_claim(claim_id) or claim, "Уже проверена")
        return
    try:
        result = await grant_days_to_first_active_key(
            int(claim["user_id"]), int(claim["days"]),
            source="bonus_task", reason=f"bonus_{claim['task']}",
            reference_type="bonus_task_claim", reference_id=str(claim_id),
        )
    except Exception as e:
        logger.error("Бонусные дни: ошибка начисления по заявке %s: %s", claim_id, e)
        result = {"ok": False, "reason": "error"}
    if not result.get("ok"):
        reopen_claim(claim_id)
        reason = "у клиента нет активного ключа" if result.get("reason") == "no_active_key" else "ошибка начисления"
        await callback.answer(f"Дни не начислены: {reason}. Заявка осталась на проверке.", show_alert=True)
        return
    days = int(claim["days"])
    await _finish_admin_message(callback, claim, f"✅ Начислено +{days} {_plural_days(days)} — {_admin_name(callback)}")
    await callback.answer("Начислено")
    try:
        meta = TASKS.get(claim["task"], {"title": ""})
        await callback.bot.send_message(
            claim["telegram_id"],
            f"🎁 <b>Бонусные дни начислены!</b>\n\nЗадание «{meta['title']}» проверено: "
            f"+{days} {_plural_days(days)} к вашей подписке. Спасибо!",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="🈴 На главную", callback_data="start")],
            ]),
        )
    except Exception as e:
        logger.warning("Бонусные дни: не удалось уведомить клиента %s: %s", claim["telegram_id"], e)


@router.callback_query(F.data.startswith("bonus_no:"))
async def cb_bonus_no(callback: CallbackQuery):
    if not _is_admin(callback.from_user.id):
        await callback.answer("⛔ Доступ запрещён", show_alert=True)
        return
    from database.db_bonus_tasks import decide_claim, get_claim

    claim_id = int(callback.data.split(":", 1)[1])
    claim = get_claim(claim_id)
    if not claim:
        await callback.answer("Заявка не найдена", show_alert=True)
        return
    if not decide_claim(claim_id, "rejected", callback.from_user.id):
        await callback.answer("Заявку уже проверил другой админ", show_alert=True)
        await _finish_admin_message(callback, get_claim(claim_id) or claim, "Уже проверена")
        return
    await _finish_admin_message(callback, claim, f"❌ Отклонено — {_admin_name(callback)}")
    await callback.answer("Отклонено")
    try:
        meta = TASKS.get(claim["task"], {"title": ""})
        await callback.bot.send_message(
            claim["telegram_id"],
            f"🎁 <b>Заявка не принята</b>\n\nСкриншот по заданию «{meta['title']}» не прошёл проверку. "
            "Можно выполнить задание и прислать скриншот ещё раз.",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="🎁 Бонусные дни", callback_data="bonus_tasks")],
                [InlineKeyboardButton(text="🈴 На главную", callback_data="start")],
            ]),
        )
    except Exception as e:
        logger.warning("Бонусные дни: не удалось уведомить клиента %s: %s", claim["telegram_id"], e)


# ── админы: настройки ───────────────────────────────────────────────────────

async def _guard(callback: CallbackQuery) -> bool:
    """Только админ и только при наличии лицензии на функцию."""
    if not _is_admin(callback.from_user.id):
        await callback.answer("⛔ Доступ запрещён", show_alert=True)
        return False
    if not is_licensed():
        await callback.answer("🎁 Бонусные дни доступны только по лицензии", show_alert=True)
        return False
    return True


def _admin_text() -> str:
    from database.db_bonus_tasks import stats
    st = stats()
    url = review_url()
    target = review_target()
    lines = [
        "🎁 <b>Бонусные дни</b>",
        "",
        "Клиент выполняет задание, присылает скриншот, вы проверяете и одобряете одной кнопкой. "
        "Дни добавляются к активному ключу клиента.",
        "",
        f"Статус: {'✅ включено' if is_enabled() else '⛔ выключено'}",
    ]
    for task, meta in TASKS.items():
        d = task_days(task)
        lines.append(f"{meta['icon']} {meta['title']}: " + (f"<b>+{d} {_plural_days(d)}</b>" if d else "выключено"))
    if url:
        lines.append("🔗 Отзывы оставляют: " + html.escape(url))
    elif target:
        lines.append("🔗 Отзывы оставляют: в новостном канале (ссылка не задана)")
    else:
        lines.append("⚠️ Отзыв: не задано, куда его оставлять — задание скрыто от клиентов. Укажите ссылку.")
    lines += [
        "",
        "📊 <b>Заявки</b>",
        f"• на проверке: {st['pending']}" + (" 🔔" if st["pending"] else ""),
        f"• одобрено: {st['approved']} (выдано дней: {st['days']})",
        f"• отклонено: {st['rejected']}",
        "",
        "<b>Как это работает</b>",
        "1️⃣ Клиент жмёт «🎁 Бонусные дни» на главной и выбирает задание",
        "2️⃣ Читает пошаговую инструкцию и присылает скриншот",
        "3️⃣ Вам приходит скриншот с кнопками «✅ Начислить» / «❌ Отклонить»",
        "4️⃣ После одобрения дни добавляются к ключу клиента",
        "",
        "ℹ️ Каждое задание — один раз на клиента. У админов кнопки на главной нет — посмотреть экран клиента можно кнопкой «👁 Как видит клиент».",
    ]
    return "\n".join(lines)


def _admin_kb() -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.row(InlineKeyboardButton(text="⛔ Выключить" if is_enabled() else "✅ Включить", callback_data="adm_bt_toggle"))
    for task, meta in TASKS.items():
        d = task_days(task)
        b.row(InlineKeyboardButton(
            text=f"{meta['icon']} {meta['title']}: " + (f"+{d} дн." if d else "выкл"),
            callback_data=f"adm_bt_days:{task}",
        ))
    b.row(InlineKeyboardButton(text="🔗 Ссылка для отзыва", callback_data="adm_bt_url"))
    n = pending_count()
    b.row(InlineKeyboardButton(text=f"📥 Заявки на проверке: {n}" + (" 🔔" if n else ""), callback_data="adm_bt_pending"))
    b.row(InlineKeyboardButton(text="🧾 Последние, кто делился", callback_data="adm_bt_log"))
    b.row(InlineKeyboardButton(text="👁 Как видит клиент", callback_data="bonus_tasks"))
    b.row(InlineKeyboardButton(text="⬅️ Назад", callback_data="admin_marketing"),
          InlineKeyboardButton(text="🈴 На главную", callback_data="start"))
    return b.as_markup()


async def _show_admin(callback: CallbackQuery) -> None:
    from bot.utils.text import safe_edit_or_send
    await safe_edit_or_send(callback.message, _admin_text(), reply_markup=_admin_kb())


@router.callback_query(F.data == "admin_bonus_tasks")
async def cb_admin_bonus(callback: CallbackQuery, state: FSMContext):
    if not await _guard(callback):
        return
    await state.clear()
    await _show_admin(callback)
    await callback.answer()


@router.callback_query(F.data == "adm_bt_toggle")
async def cb_admin_bonus_toggle(callback: CallbackQuery):
    if not await _guard(callback):
        return
    from database.requests import set_setting
    set_setting("bonus_tasks_enabled", "0" if is_enabled() else "1")
    await _show_admin(callback)
    await callback.answer("Готово")


@router.callback_query(F.data.startswith("adm_bt_days:"))
async def cb_admin_bonus_days(callback: CallbackQuery):
    if not await _guard(callback):
        return
    from bot.utils.text import safe_edit_or_send
    task = callback.data.split(":", 1)[1]
    if task not in TASKS:
        await callback.answer()
        return
    cur = task_days(task)
    b = InlineKeyboardBuilder()
    buttons = [InlineKeyboardButton(text=("✅ " if n == cur else "") + (f"{n} дн." if n else "Выкл"),
                                    callback_data=f"adm_bt_set:{task}:{n}") for n in DAYS_CHOICES]
    b.row(*buttons[:4])
    b.row(*buttons[4:])
    b.row(InlineKeyboardButton(text="⬅️ Назад", callback_data="admin_bonus_tasks"),
          InlineKeyboardButton(text="🈴 На главную", callback_data="start"))
    meta = TASKS[task]
    await safe_edit_or_send(
        callback.message,
        f"{meta['icon']} <b>{meta['title']}</b>\n\nСколько дней подписки начислять за это задание.\n"
        "«Выкл» — задание не показывается клиентам.",
        reply_markup=b.as_markup(),
    )
    await callback.answer()


@router.callback_query(F.data.startswith("adm_bt_set:"))
async def cb_admin_bonus_set(callback: CallbackQuery):
    if not await _guard(callback):
        return
    from database.requests import set_setting
    _, task, raw = callback.data.split(":", 2)
    if task in TASKS and raw.isdigit() and int(raw) in DAYS_CHOICES:
        set_setting(TASKS[task]["setting"], raw)
    await _show_admin(callback)
    await callback.answer("Сохранено")


@router.callback_query(F.data == "adm_bt_url")
async def cb_admin_bonus_url(callback: CallbackQuery, state: FSMContext):
    if not await _guard(callback):
        return
    from bot.utils.text import safe_edit_or_send
    await state.set_state(BonusStates.admin_review_url)
    b = InlineKeyboardBuilder()
    if review_url():
        b.row(InlineKeyboardButton(text="🗑 Убрать ссылку", callback_data="adm_bt_url_clear"))
    b.row(InlineKeyboardButton(text="⬅️ Назад", callback_data="admin_bonus_tasks"),
          InlineKeyboardButton(text="🈴 На главную", callback_data="start"))
    await safe_edit_or_send(
        callback.message,
        "🔗 <b>Куда оставлять отзыв</b>\n\n"
        "Пришлите ссылку, куда клиент напишет отзыв:\n"
        "• пост в канале с открытыми комментариями\n"
        "• чат или группа для отзывов\n"
        "• страница сервиса в каталоге\n\n"
        "У клиента появится кнопка «⭐ Оставить отзыв».\n"
        "Если ссылку не задать, отзыв пишут в новостной канал бота (если он настроен).\n\n"
        "Ссылка должна начинаться с https://",
        reply_markup=b.as_markup(),
    )
    await callback.answer()


@router.callback_query(F.data == "adm_bt_url_clear")
async def cb_admin_bonus_url_clear(callback: CallbackQuery, state: FSMContext):
    if not await _guard(callback):
        return
    from database.requests import set_setting
    await state.clear()
    set_setting("bonus_review_url", "")
    await _show_admin(callback)
    await callback.answer("Ссылка убрана")


@router.message(BonusStates.admin_review_url, F.text)
async def msg_admin_bonus_url(message: Message, state: FSMContext):
    if not _is_admin(message.from_user.id):
        return
    from database.requests import set_setting
    raw = message.text.strip()
    if raw.startswith("/"):
        await state.clear()
        return
    if not raw.startswith(("https://", "http://", "tg://")) or len(raw) > 500 or " " in raw:
        await message.answer("❌ Это не похоже на ссылку. Пришлите адрес, начинающийся с https://")
        return
    set_setting("bonus_review_url", raw)
    await state.clear()
    await message.answer(_admin_text(), reply_markup=_admin_kb(), parse_mode="HTML")


@router.callback_query(F.data == "adm_bt_pending")
async def cb_admin_bonus_pending(callback: CallbackQuery):
    if not await _guard(callback):
        return
    from database.db_bonus_tasks import list_pending
    pending = list_pending(10)
    if not pending:
        await callback.answer("Заявок на проверке нет", show_alert=True)
        return
    for claim in pending:
        await _send_claim_to_admins(callback.bot, claim["id"], only_admin=callback.from_user.id)
    await callback.answer(f"Отправлено заявок: {len(pending)}")


_CLAIM_MARK = {"approved": "✅", "pending": "⏳", "rejected": "❌"}


def _claim_line(c: dict) -> str:
    who = f"@{c['username']}" if c.get("username") else (c.get("first_name") or "")
    who = html.escape(who) + " · " if who else ""
    meta = TASKS.get(c["task"], {"icon": "🎁", "title": c["task"]})
    date = str(c["created_at"] or "")[:16]
    result = {"approved": f"+{c['days']} {_plural_days(c['days'] or 0)}",
              "pending": "на проверке", "rejected": "отклонено"}.get(c["status"], c["status"])
    return f"{_CLAIM_MARK.get(c['status'], '▫️')} {date} · {who}<code>{c['telegram_id']}</code>\n     {meta['icon']} {meta['title']}: {result}"


@router.callback_query(F.data == "adm_bt_log")
async def cb_admin_bonus_log(callback: CallbackQuery):
    """v1.424: кто делился (сторис, отзыв), когда и сколько дней получил."""
    if not await _guard(callback):
        return
    from database.db_bonus_tasks import list_recent
    from bot.utils.text import safe_edit_or_send
    rows = list_recent(15)
    body = "\n".join(_claim_line(c) for c in rows) if rows else "Пока никто не присылал заданий."
    b = InlineKeyboardBuilder()
    b.row(InlineKeyboardButton(text="⬅️ Назад", callback_data="admin_bonus_tasks"),
          InlineKeyboardButton(text="🈴 На главную", callback_data="start"))
    await safe_edit_or_send(callback.message, "🧾 <b>Последние, кто делился</b>\n\n" + body, reply_markup=b.as_markup())
    await callback.answer()
