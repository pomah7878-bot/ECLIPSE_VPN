"""Экран «📡 Статус серверов» в боте (кнопка в «Справке»)."""
from aiogram import F, Router
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup

router = Router()


def _kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔄 Обновить", callback_data="server_status")],
        [InlineKeyboardButton(text="⬅️ Назад", callback_data="help"),
         InlineKeyboardButton(text="🈴 На главную", callback_data="start")],
    ])


@router.callback_query(F.data == "server_status")
async def show_server_status(callback: CallbackQuery):
    from bot.services import server_status
    from bot.utils.text import safe_edit_or_send
    if not server_status.is_enabled():
        await callback.answer("Статус серверов сейчас недоступен", show_alert=True)
        return
    try:
        text = server_status.bot_text()
    except Exception:
        text = "📡 <b>Статус серверов</b>\n\nНе удалось получить данные, попробуйте позже."
    try:
        await safe_edit_or_send(callback.message, text, reply_markup=_kb())
    except Exception as e:  # «message is not modified» при повторном «Обновить»
        if "not modified" not in str(e):
            raise
    await callback.answer()
