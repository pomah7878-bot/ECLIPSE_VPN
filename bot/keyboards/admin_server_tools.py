"""
Клавиатуры раздела «Инструменты сервера» — диагностика и обслуживание
конкретной ноды через API панели 3x-ui (состояние, логи Xray, рестарт
ядра, уборка исчерпавших трафик клиентов, генерация Reality-ключей).

Все callback_data укладываются в лимит Telegram (64 байта):
внутренние действия используют короткий префикс ``srvtools_``,
вход в раздел из карточки сервера — ``admin_server_tools:{id}``.
"""
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from aiogram.utils.keyboard import InlineKeyboardBuilder


def server_tools_menu_kb(server_id: int) -> InlineKeyboardMarkup:
    """Главное меню раздела инструментов для одного сервера."""
    builder = InlineKeyboardBuilder()
    builder.row(InlineKeyboardButton(
        text='📊 Состояние сервера', callback_data=f'srvtools_status:{server_id}'))
    builder.row(InlineKeyboardButton(
        text='📋 Логи Xray', callback_data=f'srvtools_logs:{server_id}'))
    builder.row(InlineKeyboardButton(
        text='🔄 Перезапуск Xray', callback_data=f'srvtools_restart:{server_id}'))
    builder.row(InlineKeyboardButton(
        text='🧹 Убрать исчерпавших трафик', callback_data=f'srvtools_deplete:{server_id}'))
    builder.row(InlineKeyboardButton(
        text='🔑 Новые Reality-ключи', callback_data=f'srvtools_reality:{server_id}'))
    builder.row(InlineKeyboardButton(
        text='⬅️ Назад', callback_data=f'admin_server_view:{server_id}'))
    return builder.as_markup()


def server_tools_back_kb(server_id: int) -> InlineKeyboardMarkup:
    """Кнопка возврата в меню инструментов сервера."""
    builder = InlineKeyboardBuilder()
    builder.row(InlineKeyboardButton(
        text='⬅️ К инструментам', callback_data=f'admin_server_tools:{server_id}'))
    return builder.as_markup()


def xray_logs_count_kb(server_id: int) -> InlineKeyboardMarkup:
    """Выбор количества последних строк лога."""
    builder = InlineKeyboardBuilder()
    builder.row(
        InlineKeyboardButton(text='50', callback_data=f'srvtools_logs_show:{server_id}:50'),
        InlineKeyboardButton(text='100', callback_data=f'srvtools_logs_show:{server_id}:100'),
        InlineKeyboardButton(text='200', callback_data=f'srvtools_logs_show:{server_id}:200'),
    )
    builder.row(InlineKeyboardButton(
        text='⬅️ К инструментам', callback_data=f'admin_server_tools:{server_id}'))
    return builder.as_markup()


def restart_xray_confirm_kb(server_id: int) -> InlineKeyboardMarkup:
    """Подтверждение рестарта Xray (деструктивное действие)."""
    builder = InlineKeyboardBuilder()
    builder.row(InlineKeyboardButton(
        text='✅ Да, перезапустить', callback_data=f'srvtools_restart_do:{server_id}'))
    builder.row(InlineKeyboardButton(
        text='❌ Отмена', callback_data=f'admin_server_tools:{server_id}'))
    return builder.as_markup()


def delete_depleted_confirm_kb(server_id: int) -> InlineKeyboardMarkup:
    """Подтверждение уборки исчерпавших трафик клиентов по всей панели.

    Панель на clients-профиле (3x-ui 3.6.0) удаляет исчерпавших только
    глобально — эндпоинт /panel/api/clients/delDepleted не принимает
    inbound id, поэтому выбор конкретного inbound здесь не предлагается.
    """
    builder = InlineKeyboardBuilder()
    builder.row(InlineKeyboardButton(
        text='✅ Да, убрать по всей панели',
        callback_data=f'srvtools_deplete_do:{server_id}'))
    builder.row(InlineKeyboardButton(
        text='❌ Отмена', callback_data=f'admin_server_tools:{server_id}'))
    return builder.as_markup()
