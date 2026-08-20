"""
Настройка обязательной подписки на Telegram-канал перед использованием бота.
"""
import logging

from aiogram import Router, F
from aiogram.types import Message, CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup
from aiogram.fsm.context import FSMContext
from aiogram.utils.keyboard import InlineKeyboardBuilder

from bot.utils.admin import is_admin
from bot.utils.text import safe_edit_or_send, get_message_text_for_storage
from bot.states.user_states import GateSettings
from bot.keyboards.admin_misc import back_button, home_button

logger = logging.getLogger(__name__)
router = Router()


def channel_gate_menu_kb(enabled: bool) -> InlineKeyboardMarkup:
    """Клавиатура меню настройки обязательной подписки."""
    builder = InlineKeyboardBuilder()
    toggle_label = '✅ Требовать подписку' if enabled else '❌ Требовать подписку'
    builder.row(InlineKeyboardButton(text=toggle_label, callback_data='admin_toggle_channel_gate'))
    builder.row(InlineKeyboardButton(text='✏️ Изменить канал', callback_data='admin_channel_gate_set'))
    builder.row(back_button('admin_bot_settings'), home_button())
    return builder.as_markup()


@router.callback_query(F.data == 'admin_channel_gate')
async def show_channel_gate_menu(callback: CallbackQuery, state: FSMContext):
    """Главное меню настройки обязательной подписки."""
    if not is_admin(callback.from_user.id):
        await callback.answer('⛔ Доступ запрещён', show_alert=True)
        return
    await state.clear()

    from database.requests import is_channel_gate_enabled, get_gate_channel_id
    enabled = is_channel_gate_enabled()
    channel_id = get_gate_channel_id()
    channel_text = channel_id if channel_id else '⚠️ не настроен'

    await safe_edit_or_send(
        callback.message,
        '🔒 <b>Обязательная подписка на канал</b>\n\n'
        f'Текущий канал: {channel_text}\n\n'
        'Если включено — пользователь должен быть подписан на указанный канал, '
        'чтобы пользоваться ботом (кроме подтверждения уже начатой оплаты).',
        reply_markup=channel_gate_menu_kb(enabled),
    )
    await callback.answer()


@router.callback_query(F.data == 'admin_toggle_channel_gate')
async def toggle_channel_gate(callback: CallbackQuery, state: FSMContext):
    """Включает/выключает требование обязательной подписки."""
    if not is_admin(callback.from_user.id):
        await callback.answer('⛔ Доступ запрещён', show_alert=True)
        return

    from database.requests import is_channel_gate_enabled, set_channel_gate_enabled, get_gate_channel_id
    current = is_channel_gate_enabled()
    if not current and not get_gate_channel_id():
        await callback.answer('⚠️ Сначала настройте канал', show_alert=True)
        return
    set_channel_gate_enabled(not current)

    await show_channel_gate_menu(callback, state)


@router.callback_query(F.data == 'admin_channel_gate_set')
async def start_channel_gate_input(callback: CallbackQuery, state: FSMContext):
    """Запрашивает username/ID канала для обязательной подписки."""
    if not is_admin(callback.from_user.id):
        await callback.answer('⛔ Доступ запрещён', show_alert=True)
        return
    await state.set_state(GateSettings.waiting_for_channel)
    await safe_edit_or_send(
        callback.message,
        '✏️ <b>Канал для обязательной подписки</b>\n\n'
        'Отправьте username канала (например, @eclipse_unlimited_news) или его ID.\n\n'
        'Бот должен быть администратором этого канала, чтобы проверять подписку.',
        reply_markup=None,
        force_new=True,
    )
    await callback.answer()


@router.message(GateSettings.waiting_for_channel, F.text, ~F.text.startswith('/'))
async def process_channel_gate_input(message: Message, state: FSMContext):
    """Сохраняет канал для обязательной подписки."""
    if not is_admin(message.from_user.id):
        return
    from database.requests import set_gate_channel_id
    text = get_message_text_for_storage(message, 'plain').strip()
    if not text.startswith('@') and not text.lstrip('-').isdigit():
        text = f'@{text}'
    set_gate_channel_id(text)
    await state.clear()
    logger.info(f"Админ {message.from_user.id} настроил канал для гейта подписки: {text}")

    from database.requests import is_channel_gate_enabled
    enabled = is_channel_gate_enabled()
    await safe_edit_or_send(
        message,
        f'✅ Канал сохранён: {text}',
        reply_markup=channel_gate_menu_kb(enabled),
        force_new=True,
    )
