"""Клавиатуры для инструментов диагностики сервера в админке."""
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from aiogram.utils.keyboard import InlineKeyboardBuilder
from bot.keyboards.admin_misc import back_button, home_button


def server_tools_menu_kb(server_id: int) -> InlineKeyboardMarkup:
    """Меню инструментов диагностики конкретного сервера."""
    builder = InlineKeyboardBuilder()
    builder.row(InlineKeyboardButton(text='📋 Логи подключений', callback_data=f'admin_srv_logs:{server_id}'))
    builder.row(InlineKeyboardButton(text='📊 Состояние сервера', callback_data=f'admin_srv_status:{server_id}'))
    builder.row(InlineKeyboardButton(text='🔄 Перезапустить Xray', callback_data=f'admin_srv_restart_ask:{server_id}'))
    builder.row(back_button(f'admin_server_view:{server_id}'), home_button())
    return builder.as_markup()


def server_tools_back_kb(server_id: int) -> InlineKeyboardMarkup:
    """Возврат в меню инструментов."""
    builder = InlineKeyboardBuilder()
    builder.row(back_button(f'admin_server_tools:{server_id}'), home_button())
    return builder.as_markup()


def server_restart_confirm_kb(server_id: int) -> InlineKeyboardMarkup:
    """Подтверждение перезапуска Xray."""
    builder = InlineKeyboardBuilder()
    builder.row(InlineKeyboardButton(text='✅ Да, перезапустить', callback_data=f'admin_srv_restart_do:{server_id}'))
    builder.row(InlineKeyboardButton(text='❌ Отмена', callback_data=f'admin_server_tools:{server_id}'))
    return builder.as_markup()
