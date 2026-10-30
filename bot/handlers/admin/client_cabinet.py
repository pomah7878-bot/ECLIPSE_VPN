"""Админка: «🎨 Новый вид меню» — включатель лицензионной функции client_cabinet."""
from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery

from bot.services import client_cabinet as cab
from bot.utils.admin import is_admin as _is_admin_guard

router = Router()
router.callback_query.filter(lambda c: _is_admin_guard(c.from_user.id))

_LOCK = "🔒 Эта функция доступна по лицензии"


@router.callback_query(F.data == "admin_client_cabinet")
async def cabinet_admin_open(callback: CallbackQuery, state: FSMContext):
    from bot.handlers.admin.integrations import show_cabinet_theme_menu
    await show_cabinet_theme_menu(callback, state)


@router.callback_query(F.data == "admin_cabinet_toggle")
async def cabinet_admin_toggle(callback: CallbackQuery, state: FSMContext):
    if not cab.licensed():
        await callback.answer(_LOCK, show_alert=True)
        return
    cab.set_enabled(not cab.enabled())
    from bot.handlers.admin.integrations import show_cabinet_theme_menu
    await show_cabinet_theme_menu(callback, state)
