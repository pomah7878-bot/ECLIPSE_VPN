"""Админ-раздел: нарушения лимита IP и статистика по платформам."""
import logging

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.types import CallbackQuery, InlineKeyboardButton, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder

from bot.utils.admin import is_admin
from bot.utils.ru_format import format_rub
from bot.utils.text import escape_html, safe_edit_or_send

logger = logging.getLogger(__name__)

router = Router()
router.message.filter(lambda m: m.from_user is not None and is_admin(m.from_user.id))
router.callback_query.filter(lambda c: is_admin(c.from_user.id))

PLATFORM_WINDOW_DAYS = 30


def _back_home():
    from bot.keyboards.admin_misc import back_button, home_button
    return back_button('admin_panel'), home_button()


def _abuse_status(row: dict) -> str:
    if row.get('permanent'):
        return '⛔ навсегда'
    if row.get('block_active'):
        return f"⏳ до {str(row.get('blocked_until') or '')[:16]}"
    return '⚪️ без блокировки'


def _abuse_text() -> str:
    from bot.services import ip_abuse
    from database.requests import (
        get_ip_limit_permanent_strikes, get_ip_limit_temp_hours, is_ip_limit_enforce_enabled,
    )
    st = ip_abuse.stats()
    rows = ip_abuse.list_offenders(6)
    enforce = is_ip_limit_enforce_enabled()
    lines = [
        "🛡 <b>Нарушения лимита IP</b>",
        "",
        f"📌 Автонаказания: {'🟢 включены' if enforce else '⚪️ выключены'}",
        f"⏳ Временная блокировка: <b>{get_ip_limit_temp_hours()} ч</b>",
        f"⛔ Постоянный бан: после <b>{get_ip_limit_permanent_strikes()}</b> эпизодов",
        "",
        f"📊 Всего нарушителей: <b>{st['total']}</b>",
        f"⏳ Временно заблокированы: <b>{st['blocked']}</b>",
        f"⛔ Забанены навсегда: <b>{st['permanent']}</b>",
        "",
        "Правило: 1-е превышение — предупреждение, 2-е — временная блокировка, "
        "3-е — постоянный бан. Считается не чаще раза в 12 ч, старые эпизоды "
        "сбрасываются через 30 дней.",
    ]
    if not rows:
        lines += ["", "Нарушений пока нет."]
        return "\n".join(lines)

    lines += ["", "<b>Последние нарушители:</b>"]
    for row in rows:
        who = f"@{row['username']}" if row.get('username') else f"tg {row.get('telegram_id') or '—'}"
        lines.append(
            f"• {escape_html(who)} · ключ #{row.get('key_id') or '—'} · "
            f"{int(row.get('last_used') or 0)}/{int(row.get('last_limit') or 0)} IP · "
            f"эпизодов: {int(row.get('strikes') or 0)} · {_abuse_status(row)}"
        )
    lines += ["", "Действия — кнопками ниже. /devlimit — уведомления о лимите."]
    return "\n".join(lines)


def _abuse_keyboard() -> object:
    from bot.services import ip_abuse
    from database.requests import is_ip_limit_enforce_enabled
    kb = InlineKeyboardBuilder()
    kb.row(InlineKeyboardButton(
        text='⚪️ Выключить автонаказания' if is_ip_limit_enforce_enabled() else '🟢 Включить автонаказания',
        callback_data='admin_ip_toggle_enforce',
    ))
    for row in ip_abuse.list_offenders(3):
        uid = int(row['user_id'])
        who = f"@{row['username']}" if row.get('username') else f"tg {row.get('telegram_id') or uid}"
        kb.row(InlineKeyboardButton(text=f"👤 {who}"[:60], callback_data=f"admin_ip_show:{uid}"))
        kb.row(
            InlineKeyboardButton(text='⏳ 24 ч', callback_data=f"admin_ip_temp:{uid}:24"),
            InlineKeyboardButton(text='⏳ 7 дн', callback_data=f"admin_ip_temp:{uid}:168"),
        )
        kb.row(
            InlineKeyboardButton(text='⛔ Навсегда', callback_data=f"admin_ip_perm:{uid}"),
            InlineKeyboardButton(text='✅ Разблокировать', callback_data=f"admin_ip_unblock:{uid}"),
        )
    kb.row(InlineKeyboardButton(text='🔄 Обновить', callback_data='admin_ipabuse'))
    kb.row(*_back_home())
    return kb.as_markup()


async def _render_abuse(callback: CallbackQuery) -> None:
    await safe_edit_or_send(callback.message, _abuse_text(), reply_markup=_abuse_keyboard())
    await callback.answer()


@router.message(Command('ipabuse'))
async def ipabuse_cmd(message: Message):
    from aiogram.types import InlineKeyboardMarkup
    await message.answer(_abuse_text(), reply_markup=_abuse_keyboard(), parse_mode='HTML')


@router.callback_query(F.data == 'admin_ipabuse')
async def ipabuse_screen(callback: CallbackQuery):
    await _render_abuse(callback)


@router.callback_query(F.data == 'admin_ip_toggle_enforce')
async def ipabuse_toggle_enforce(callback: CallbackQuery):
    from database.requests import is_ip_limit_enforce_enabled, set_ip_limit_enforce_enabled
    set_ip_limit_enforce_enabled(not is_ip_limit_enforce_enabled())
    await _render_abuse(callback)


@router.callback_query(F.data.startswith('admin_ip_show:'))
async def ipabuse_show(callback: CallbackQuery):
    try:
        uid = int(callback.data.split(':', 1)[1])
    except (ValueError, IndexError):
        await callback.answer()
        return
    from database.requests import get_ip_abuse, get_user_by_id
    from bot.services.platforms import platform_label
    row = get_ip_abuse(uid)
    user = get_user_by_id(uid)
    if not row or not user:
        await callback.answer('Нарушитель не найден', show_alert=True)
        return
    who = f"@{user['username']}" if user.get('username') else f"tg {user.get('telegram_id') or uid}"
    text = (
        f"👤 <b>{escape_html(who)}</b>\n\n"
        f"🌐 Платформа: {escape_html(platform_label(user.get('platform')))}\n"
        f"🚫 Бан: {'да' if user.get('is_banned') else 'нет'}\n"
        f"⚠️ Эпизодов: {int(row.get('strikes') or 0)}\n"
        f"📅 Последний: {str(row.get('last_strike_at') or '—')[:16]}\n"
        f"📊 Последнее превышение: {int(row.get('last_used') or 0)} при лимите {int(row.get('last_limit') or 0)}\n"
        f"📍 Статус: {_abuse_status(row)}\n"
    )
    kb = InlineKeyboardBuilder()
    kb.row(
        InlineKeyboardButton(text='⏳ 24 ч', callback_data=f"admin_ip_temp:{uid}:24"),
        InlineKeyboardButton(text='⏳ 7 дн', callback_data=f"admin_ip_temp:{uid}:168"),
    )
    kb.row(
        InlineKeyboardButton(text='⛔ Навсегда', callback_data=f"admin_ip_perm:{uid}"),
        InlineKeyboardButton(text='✅ Разблокировать', callback_data=f"admin_ip_unblock:{uid}"),
    )
    kb.row(*_back_home())
    await safe_edit_or_send(callback.message, text, reply_markup=kb.as_markup())
    await callback.answer()


@router.callback_query(F.data.startswith('admin_ip_temp:'))
async def ipabuse_temp(callback: CallbackQuery):
    from bot.services import ip_abuse
    try:
        _, uid, hours = callback.data.split(':', 2)
        uid, hours = int(uid), int(hours)
    except (ValueError, IndexError):
        await callback.answer()
        return
    result = await ip_abuse.temp_block(uid, hours, by=f"admin:{callback.from_user.id}")
    await callback.answer('Временная блокировка установлена' if result.get('ok') else result.get('message', 'Ошибка'), show_alert=True)
    await _render_abuse(callback)


@router.callback_query(F.data.startswith('admin_ip_perm:'))
async def ipabuse_perm(callback: CallbackQuery):
    from bot.services import ip_abuse
    try:
        uid = int(callback.data.split(':', 1)[1])
    except (ValueError, IndexError):
        await callback.answer()
        return
    result = await ip_abuse.permanent_ban(uid, by=f"admin:{callback.from_user.id}")
    await callback.answer('Забанен навсегда' if result.get('ok') else result.get('message', 'Ошибка'), show_alert=True)
    await _render_abuse(callback)


@router.callback_query(F.data.startswith('admin_ip_unblock:'))
async def ipabuse_unblock(callback: CallbackQuery):
    from bot.services import ip_abuse
    try:
        uid = int(callback.data.split(':', 1)[1])
    except (ValueError, IndexError):
        await callback.answer()
        return
    result = await ip_abuse.unblock(uid, by=f"admin:{callback.from_user.id}")
    await callback.answer('Разблокирован' if result.get('ok') else result.get('message', 'Ошибка'), show_alert=True)
    await _render_abuse(callback)


# ------------------------------------------------------------------ платформы

def _platforms_text() -> str:
    from database.requests import get_platform_stats
    from bot.services.platforms import platform_label
    rows = get_platform_stats(PLATFORM_WINDOW_DAYS)
    total_users = sum(int(r['users'] or 0) for r in rows)
    total_orders = sum(int(r['orders'] or 0) for r in rows)
    total_revenue = sum(float(r['revenue_rub'] or 0) for r in rows)
    lines = [
        "📊 <b>Платформы клиентов</b>",
        "",
        f"Заказы и выручка — за {PLATFORM_WINDOW_DAYS} дней.",
        "",
        "<b>Пользователи:</b>",
    ]
    shown = 0
    for row in rows:
        users = int(row['users'] or 0)
        if users <= 0:
            continue
        shown += 1
        lines.append(f"• {platform_label(row['platform'])}: <b>{users}</b>")
    if not shown:
        lines.append("• пока нет данных")
    lines += ["", "<b>Заказы и выручка:</b>"]
    shown = 0
    for row in rows:
        orders = int(row['orders'] or 0)
        revenue = float(row['revenue_rub'] or 0)
        if orders <= 0 and revenue <= 0:
            continue
        shown += 1
        lines.append(
            f"• {platform_label(row['platform'])}: <b>{orders}</b> заказ(ов) · "
            f"{format_rub(revenue)}"
        )
    if not shown:
        lines.append("• пока нет оплат")
    lines += [
        "",
        f"Итого: <b>{total_users}</b> пользователей · <b>{total_orders}</b> заказов · "
        f"<b>{format_rub(total_revenue)}</b> за {PLATFORM_WINDOW_DAYS} дней.",
        "",
        "Платформа определяется по заголовкам приложения/браузера; "
        "Telegram-бот учитывается отдельно.",
    ]
    return "\n".join(lines)


@router.message(Command('platforms'))
async def platforms_cmd(message: Message):
    kb = InlineKeyboardBuilder()
    kb.row(*_back_home())
    await message.answer(_platforms_text(), reply_markup=kb.as_markup(), parse_mode='HTML')


@router.callback_query(F.data == 'admin_platforms')
async def platforms_screen(callback: CallbackQuery):
    kb = InlineKeyboardBuilder()
    kb.row(*_back_home())
    await safe_edit_or_send(callback.message, _platforms_text(), reply_markup=kb.as_markup())
    await callback.answer()
