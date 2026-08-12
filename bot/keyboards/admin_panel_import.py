"""Клавиатуры для импорта вручную созданных клиентов панели в бота."""
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from aiogram.utils.keyboard import InlineKeyboardBuilder
from bot.keyboards.admin_misc import back_button, home_button


def orphan_list_kb(server_id: int, emails: list) -> InlineKeyboardMarkup:
    """Список email вручную созданных клиентов, которых можно импортировать."""
    builder = InlineKeyboardBuilder()
    for email in emails:
        builder.row(InlineKeyboardButton(
            text=f'👤 {email}',
            callback_data=f'admin_import_orphan_pick:{server_id}:{email}',
        ))
    builder.row(back_button(f'admin_server_view:{server_id}'), home_button())
    return builder.as_markup()


def orphan_import_cancel_kb(server_id: int) -> InlineKeyboardMarkup:
    """Клавиатура отмены на шаге ввода Telegram ID."""
    builder = InlineKeyboardBuilder()
    builder.row(InlineKeyboardButton(text='❌ Отмена', callback_data=f'admin_import_orphans:{server_id}'))
    return builder.as_markup()


def orphan_import_confirm_kb(server_id: int) -> InlineKeyboardMarkup:
    """Клавиатура подтверждения импорта."""
    builder = InlineKeyboardBuilder()
    builder.row(InlineKeyboardButton(text='✅ Привязать', callback_data='admin_import_orphan_confirm'))
    builder.row(InlineKeyboardButton(text='❌ Отмена', callback_data=f'admin_import_orphans:{server_id}'))
    return builder.as_markup()
