"""Админка: «📊 Аналитика» (лицензионная функция admin_insights, включается админом отдельно)."""
import logging

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup

from bot.keyboards.admin_misc import back_button, home_button
from bot.services import admin_insights as ai
from bot.utils.admin import is_admin as _is_admin_guard
from bot.utils.text import safe_edit_or_send

logger = logging.getLogger(__name__)

router = Router()
router.callback_query.filter(lambda c: _is_admin_guard(c.from_user.id))

_LOCK = "🔒 Эта функция доступна по лицензии"


def _kb(on: bool, details: bool = True) -> InlineKeyboardMarkup:
    rows = [[InlineKeyboardButton(text="🟢 Включено" if on else "⚪ Выключено", callback_data="admin_insights_toggle")]]
    if on and details:
        rows.append([InlineKeyboardButton(text="💳 Лента оплат", callback_data="admin_insights_pays"),
                     InlineKeyboardButton(text="⏳ Истекающие", callback_data="admin_insights_exp")])
        rows.append([InlineKeyboardButton(text="🔄 Обновить", callback_data="admin_insights")])
    rows.append([back_button("admin_payments"), home_button()])
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def _show(callback: CallbackQuery) -> None:
    if not ai.licensed():
        await callback.answer(_LOCK, show_alert=True)
        return
    if not ai.enabled():
        text = ("📊 <b>Аналитика</b>\n\nВыключена. Включите кнопкой ниже: последние оплаты, "
                "неоплаченные заказы, пробники, истекающие ключи, шлюзы, бэкап и инциденты на одном экране.")
        await safe_edit_or_send(callback.message, text, reply_markup=_kb(False))
    else:
        rich_html = None
        try:
            from bot.services import client_cabinet
            if client_cabinet.enabled():
                rich_html = ai.build_html()
        except Exception as e:  # noqa: BLE001
            logger.debug("Новый вид аналитики: %s", e)
        if rich_html:
            from bot.utils.rich_view import send as rich_send
            await rich_send(callback.message, rich_html, ai.build_text(), _kb(True))
        else:
            await safe_edit_or_send(callback.message, ai.build_text(), reply_markup=_kb(True))
    await callback.answer()


@router.callback_query(F.data == "admin_insights")
async def insights_open(callback: CallbackQuery, state: FSMContext):
    await _show(callback)


@router.callback_query(F.data == "admin_insights_toggle")
async def insights_toggle(callback: CallbackQuery, state: FSMContext):
    if not ai.licensed():
        await callback.answer(_LOCK, show_alert=True)
        return
    ai.set_enabled(not ai.enabled())
    await _show(callback)


@router.callback_query(F.data.in_({"admin_insights_pays", "admin_insights_exp"}))
async def insights_detail(callback: CallbackQuery, state: FSMContext):
    if not ai.enabled():
        await callback.answer(_LOCK if not ai.licensed() else "Аналитика выключена", show_alert=True)
        return
    text = ai.payments_list_text() if callback.data.endswith("pays") else ai.expiring_list_text()
    await safe_edit_or_send(callback.message, text, reply_markup=InlineKeyboardMarkup(
        inline_keyboard=[[back_button("admin_insights"), home_button()]]))
    await callback.answer()
