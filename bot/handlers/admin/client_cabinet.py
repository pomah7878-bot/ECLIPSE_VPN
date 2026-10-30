"""Админка: «🎨 Новый вид меню» — включатель лицензионной функции client_cabinet."""
from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup

from bot.keyboards.admin_misc import back_button, home_button
from bot.services import client_cabinet as cab
from bot.utils.admin import is_admin as _is_admin_guard
from bot.utils.text import safe_edit_or_send

router = Router()
router.callback_query.filter(lambda c: _is_admin_guard(c.from_user.id))

_LOCK = "🔒 Эта функция доступна по лицензии"


async def _show(callback: CallbackQuery) -> None:
    if not cab.licensed():
        await callback.answer(_LOCK, show_alert=True)
        return
    on = cab.enabled()
    text = ("🎨 <b>Новый вид меню</b>\n\n"
            "Новый вид личного кабинета: таблица с балансом, ключами, трафиком, оплатами и рефералами, "
            "полоса прогресса и цветные кнопки. Клиенты открывают его командой /cabinet и кнопкой «👤 Кабинет» "
            "на главной (если она не скрыта в редакторе страниц).\n\n"
            "Работает в новых версиях Telegram; если сообщение не принято, кабинет покажется обычным текстом.")
    rows = [[InlineKeyboardButton(text="🟢 Включено" if on else "⚪ Выключено", callback_data="admin_cabinet_toggle")]]
    if on:
        rows.append([InlineKeyboardButton(text="👁 Показать как у клиента", callback_data="cabinet", style="primary")])
    rows.append([back_button("admin_bot_settings"), home_button()])
    await safe_edit_or_send(callback.message, text, reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))
    await callback.answer()


@router.callback_query(F.data == "admin_client_cabinet")
async def cabinet_admin_open(callback: CallbackQuery, state: FSMContext):
    await _show(callback)


@router.callback_query(F.data == "admin_cabinet_toggle")
async def cabinet_admin_toggle(callback: CallbackQuery, state: FSMContext):
    if not cab.licensed():
        await callback.answer(_LOCK, show_alert=True)
        return
    cab.set_enabled(not cab.enabled())
    await _show(callback)
