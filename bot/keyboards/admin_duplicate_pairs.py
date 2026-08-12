"""Клавиатуры для уведомлений о найденных потенциальных дубликатах клиентов."""
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from aiogram.utils.keyboard import InlineKeyboardBuilder


def duplicate_pair_notification_kb(pair_id: int) -> InlineKeyboardMarkup:
    """Клавиатура под уведомлением о найденном дубликате."""
    builder = InlineKeyboardBuilder()
    builder.row(InlineKeyboardButton(
        text='🔗 Объединить (продлить и удалить дубликат)',
        callback_data=f'admin_duppair_merge:{pair_id}',
    ))
    builder.row(InlineKeyboardButton(
        text='🗑️ Просто удалить дубликат',
        callback_data=f'admin_duppair_delete:{pair_id}',
    ))
    builder.row(InlineKeyboardButton(
        text='➡️ Игнорировать (не дубликат)',
        callback_data=f'admin_duppair_ignore:{pair_id}',
    ))
    return builder.as_markup()
