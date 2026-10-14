"""v1.301: бонусные дни за задания — отзыв и сторис.

Как работает:
• клиент открывает «🎁 Бонусные дни», выбирает задание и присылает скриншот;
• админы получают скриншот с кнопками «✅ Начислить» / «❌ Отклонить»;
• после одобрения дни добавляются к активному ключу клиента.

Каждое задание — один раз на клиента. Отклонённую заявку можно прислать снова.
Нужен активный ключ (дни добавляются к нему).
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

TASKS = {
    "story": {
        "icon": "📸", "title": "Сторис",
        "how": "Опубликуйте сторис с рассказом о нас и пришлите скриншот.",
        "setting": "bonus_story_days",
    },
    "review": {
        "icon": "⭐", "title": "Отзыв",
        "how": "Оставьте отзыв о сервисе и пришлите скриншот.",
        "setting": "bonus_review_days",
    },
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


def is_enabled() -> bool:
    try:
        return _setting("bonus_tasks_enabled", "0") == "1"
    except Exception:
        return False


def task_days(task: str) -> int:
    try:
        return max(0, min(365, int(_setting(TASKS[task]["setting"], str(DEFAULT_DAYS)))))
    except (ValueError, KeyError):
        return DEFAULT_DAYS


def review_url() -> str:
    url = _setting("bonus_review_url", "").strip()
    return url if url.startswith(("https://", "http://", "tg://")) else ""


def active_tasks() -> list:
    return [t for t in TASKS if task_days(t) > 0]


def is_visible() -> bool:
    """Показывать ли кнопку «🎁 Бонусные дни» на главной."""
    return is_enabled() and bool(active_tasks())


def _plural_days(n: int) -> str:
    if 11 <= n % 100 <= 14:
        return "дней"
    return {1: "день", 2: "дня", 3: "дня", 4: "дня"}.get(n % 10, "дней")


def _nav_row(back: str = "start") -> list:
    return [InlineKeyboardButton(text="⬅️ Назад", callback_data=back),
            InlineKeyboardButton(text="🈴 На главную", callback_data="start")]


# ── клиент ───────────────────────────────────────────────────────────────────

def _user_screen(user_id: int | None):
    from database.db_bonus_tasks import get_active_claim
    lines = [
        "🎁 <b>Бонусные дни</b>",
        "",
        "Выполните задание и получите дни подписки бесплатно.",
        "",
    ]
    b = InlineKeyboardBuilder()
    for task in active_tasks():
        meta, days = TASKS[task], task_days(task)
        claim = get_active_claim(user_id, task) if user_id else None
        lines.append(f"{meta['icon']} <b>{meta['title']}</b> — +{days} {_plural_days(days)}")
        lines.append(meta["how"])
        lines.append("")
        if claim and claim["status"] == "approved":
            b.row(InlineKeyboardButton(text=f"✅ {meta['title']}: дни начислены", callback_data="noop"))
        elif claim:
            b.row(InlineKeyboardButton(text=f"⏳ {meta['title']}: на проверке", callback_data="noop"))
        else:
            if task == "review" and review_url():
                b.row(InlineKeyboardButton(text="⭐ Оставить отзыв", url=review_url()))
            b.row(InlineKeyboardButton(text=f"{meta['icon']} {meta['title']}: прислать скриншот",
                                       callback_data=f"bonus_task:{task}"))
    lines += [
        "ℹ️ <b>Условия</b>",
        "• каждое задание — один раз",
        "• дни добавятся к активному ключу после проверки",
    ]
    b.row(*_nav_row())
    return "\n".join(lines), b.as_markup()


@router.callback_query(F.data == "bonus_tasks")
async def cb_bonus_tasks(callback: CallbackQuery, state: FSMContext):
    from bot.utils.text import safe_edit_or_send
    from database.requests import get_user_internal_id
    await state.clear()
    if not is_visible():
        await callback.answer("Сейчас бонусных заданий нет", show_alert=True)
        return
    text, kb = _user_screen(get_user_internal_id(callback.from_user.id))
    await safe_edit_or_send(callback.message, text, reply_markup=kb)
    await callback.answer()


@router.callback_query(F.data.startswith("bonus_task:"))
async def cb_bonus_task(callback: CallbackQuery, state: FSMContext):
    from bot.utils.text import safe_edit_or_send
    from database.db_bonus_tasks import get_active_claim
    from database.requests import get_first_active_key_for_user, get_user_internal_id

    task = callback.data.split(":", 1)[1]
    if task not in TASKS or not is_enabled() or task_days(task) <= 0:
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
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="❌ Отмена", callback_data="bonus_tasks")],
        _nav_row("bonus_tasks"),
    ])
    await safe_edit_or_send(
        callback.message,
        f"{meta['icon']} <b>{meta['title']}</b>\n\n"
        f"{meta['how']}\n\n"
        "📎 Пришлите скриншот <b>одним фото</b> в этот чат.",
        reply_markup=kb,
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
        "✅ <b>Скриншот отправлен на проверку</b>\n\n"
        f"После проверки начислим +{days} {_plural_days(days)} и пришлём уведомление.",
        reply_markup=kb, parse_mode="HTML",
    )
    await _send_claim_to_admins(message.bot, claim_id)


@router.message(BonusStates.waiting_screenshot)
async def msg_bonus_not_photo(message: Message, state: FSMContext):
    if message.text and message.text.startswith("/"):
        await state.clear()
        return
    await message.answer(
        "📎 Нужен скриншот <b>одним фото</b>. Пришлите картинку, не файл.",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="❌ Отмена", callback_data="bonus_tasks")],
        ]),
        parse_mode="HTML",
    )


# ── админы: проверка заявок ─────────────────────────────────────────────────

def _claim_caption(claim: dict, footer: str = "") -> str:
    from database.requests import get_user_by_id
    user = get_user_by_id(claim["user_id"]) or {}
    username = user.get("username")
    who = f"@{html.escape(username)}" if username else "без username"
    meta = TASKS.get(claim["task"], {"icon": "🎁", "title": claim["task"]})
    text = (
        f"🎁 <b>Заявка на бонусные дни</b> №{claim['id']}\n\n"
        f"Задание: {meta['icon']} {meta['title']}\n"
        f"Клиент: {who} (<code>{claim['telegram_id']}</code>)\n"
        f"Награда: +{claim['days']} {_plural_days(int(claim['days']))}"
    )
    return text + (f"\n\n{footer}" if footer else "")


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


async def _finish_admin_message(callback: CallbackQuery, claim: dict, footer: str) -> None:
    try:
        await callback.message.edit_caption(caption=_claim_caption(claim, footer), parse_mode="HTML", reply_markup=None)
    except Exception:
        pass


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
    await _finish_admin_message(callback, claim, f"✅ Начислено +{days} {_plural_days(days)}")
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
    await _finish_admin_message(callback, claim, "❌ Отклонено")
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

def _admin_text() -> str:
    from database.db_bonus_tasks import stats
    st = stats()
    url = review_url()
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
    lines.append("🔗 Ссылка для отзыва: " + (html.escape(url) if url else "не задана"))
    lines += [
        "",
        "📊 <b>Заявки</b>",
        f"• на проверке: {st['pending']}",
        f"• одобрено: {st['approved']} (выдано дней: {st['days']})",
        f"• отклонено: {st['rejected']}",
        "",
        "ℹ️ Каждое задание клиент выполняет один раз. Заявки приходят всем админам с кнопками проверки.",
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
    b.row(InlineKeyboardButton(text="📥 Показать заявки на проверке", callback_data="adm_bt_pending"))
    b.row(InlineKeyboardButton(text="⬅️ Назад", callback_data="admin_marketing"),
          InlineKeyboardButton(text="🈴 На главную", callback_data="start"))
    return b.as_markup()


async def _show_admin(callback: CallbackQuery) -> None:
    from bot.utils.text import safe_edit_or_send
    await safe_edit_or_send(callback.message, _admin_text(), reply_markup=_admin_kb())


@router.callback_query(F.data == "admin_bonus_tasks")
async def cb_admin_bonus(callback: CallbackQuery, state: FSMContext):
    if not _is_admin(callback.from_user.id):
        await callback.answer("⛔ Доступ запрещён", show_alert=True)
        return
    await state.clear()
    await _show_admin(callback)
    await callback.answer()


@router.callback_query(F.data == "adm_bt_toggle")
async def cb_admin_bonus_toggle(callback: CallbackQuery):
    if not _is_admin(callback.from_user.id):
        await callback.answer("⛔ Доступ запрещён", show_alert=True)
        return
    from database.requests import set_setting
    set_setting("bonus_tasks_enabled", "0" if is_enabled() else "1")
    await _show_admin(callback)
    await callback.answer("Готово")


@router.callback_query(F.data.startswith("adm_bt_days:"))
async def cb_admin_bonus_days(callback: CallbackQuery):
    if not _is_admin(callback.from_user.id):
        await callback.answer("⛔ Доступ запрещён", show_alert=True)
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
    if not _is_admin(callback.from_user.id):
        await callback.answer("⛔ Доступ запрещён", show_alert=True)
        return
    from database.requests import set_setting
    _, task, raw = callback.data.split(":", 2)
    if task in TASKS and raw.isdigit() and int(raw) in DAYS_CHOICES:
        set_setting(TASKS[task]["setting"], raw)
    await _show_admin(callback)
    await callback.answer("Сохранено")


@router.callback_query(F.data == "adm_bt_url")
async def cb_admin_bonus_url(callback: CallbackQuery, state: FSMContext):
    if not _is_admin(callback.from_user.id):
        await callback.answer("⛔ Доступ запрещён", show_alert=True)
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
        "🔗 <b>Ссылка для отзыва</b>\n\n"
        "Куда клиент пишет отзыв: пост в канале, чат отзывов, страница каталога.\n"
        "У клиента появится кнопка «⭐ Оставить отзыв».\n\n"
        "Пришлите ссылку, она должна начинаться с https://",
        reply_markup=b.as_markup(),
    )
    await callback.answer()


@router.callback_query(F.data == "adm_bt_url_clear")
async def cb_admin_bonus_url_clear(callback: CallbackQuery, state: FSMContext):
    if not _is_admin(callback.from_user.id):
        await callback.answer("⛔ Доступ запрещён", show_alert=True)
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
    if not _is_admin(callback.from_user.id):
        await callback.answer("⛔ Доступ запрещён", show_alert=True)
        return
    from database.db_bonus_tasks import list_pending
    pending = list_pending(10)
    if not pending:
        await callback.answer("Заявок на проверке нет", show_alert=True)
        return
    for claim in pending:
        await _send_claim_to_admins(callback.bot, claim["id"], only_admin=callback.from_user.id)
    await callback.answer(f"Отправлено заявок: {len(pending)}")
