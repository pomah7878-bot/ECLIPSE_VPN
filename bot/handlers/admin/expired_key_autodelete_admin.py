"""
Handlers for the "Автоудаление истекших ключей" section in the admin panel.

Management:
- On/off
- Number of days after expiration before deletion
- Editing notification text (via universal editor)
"""
import logging
from aiogram import Router, F
from aiogram.types import CallbackQuery, Message
from aiogram.fsm.context import FSMContext

from bot.states.admin_states import AdminStates
from bot.utils.admin import is_admin
from bot.utils.text import safe_edit_or_send

logger = logging.getLogger(__name__)
router = Router()


async def show_autodelete_menu(callback: CallbackQuery):
    """Shows the expired key autodeletion settings menu."""
    from bot.services.expired_key_autodelete import is_autodelete_enabled, get_autodelete_days
    from bot.keyboards.admin_settings import expired_key_autodelete_settings_kb

    enabled = is_autodelete_enabled()
    days = get_autodelete_days()
    status_text = "🟢 Включено" if enabled else "⚪ Выключено"

    text = (
        "🗑 <b>Автоудаление истекших ключей</b>\n\n"
        "Ключи, срок действия которых истёк более N дней назад, автоматически "
        "удаляются из базы и с VPN-панели, а владельцу отправляется уведомление "
        "с напоминанием о сервисе.\n\n"
        f"📌 <b>Статус:</b> {status_text}\n"
        f"📅 <b>Дней до удаления:</b> {days}\n\n"
        "Проверка выполняется раз в сутки вместе с остальными ежедневными задачами."
    )

    await safe_edit_or_send(
        callback.message,
        text,
        reply_markup=expired_key_autodelete_settings_kb(enabled, days)
    )
    await callback.answer()


@router.callback_query(F.data == "admin_autodelete")
async def admin_autodelete_menu(callback: CallbackQuery):
    """Shows the autodeletion settings menu."""
    if not is_admin(callback.from_user.id):
        return
    await show_autodelete_menu(callback)


@router.callback_query(F.data.startswith("admin_autodelete_set:"))
async def admin_autodelete_set(callback: CallbackQuery):
    """Enables or disables autodeletion."""
    if not is_admin(callback.from_user.id):
        return

    from database.requests import set_setting
    from bot.services.expired_key_autodelete import SETTING_ENABLED, is_autodelete_enabled

    target_enabled = callback.data.rsplit(":", 1)[1] == "1"
    current = is_autodelete_enabled()
    if current == target_enabled:
        status = "уже включено" if target_enabled else "уже выключено"
        await callback.answer(f"Автоудаление {status}")
        return

    set_setting(SETTING_ENABLED, '1' if target_enabled else '0')
    action = "включено" if target_enabled else "выключено"
    logger.info(f"Автоудаление истекших ключей {action} (admin: {callback.from_user.id})")

    await show_autodelete_menu(callback)


@router.callback_query(F.data == "admin_autodelete_edit_days")
async def admin_autodelete_edit_days_start(callback: CallbackQuery, state: FSMContext):
    """Requests the number of days as text."""
    if not is_admin(callback.from_user.id):
        return

    from bot.services.expired_key_autodelete import get_autodelete_days
    from bot.keyboards.admin import back_and_home_kb

    await state.set_state(AdminStates.waiting_for_autodelete_days)
    current_days = get_autodelete_days()
    await safe_edit_or_send(
        callback.message,
        f"📅 <b>Дней до удаления</b>\n\n"
        f"Сейчас: <b>{current_days}</b>\n\n"
        f"Введите новое число дней (сколько времени должно пройти после истечения "
        f"ключа, прежде чем он будет автоматически удалён):",
        reply_markup=back_and_home_kb('admin_autodelete'),
    )
    await callback.answer()


@router.message(AdminStates.waiting_for_autodelete_days, F.text, ~F.text.startswith("/"))
async def admin_autodelete_days_input(message: Message, state: FSMContext):
    """Processes the entered number of days."""
    if not is_admin(message.from_user.id):
        return

    text = (message.text or "").strip()
    try:
        days = int(text)
    except ValueError:
        await message.answer("❌ Введите целое число, например: 30")
        return

    if days < 1 or days > 365:
        await message.answer("❌ Число дней должно быть от 1 до 365.")
        return

    from database.requests import set_setting
    from bot.services.expired_key_autodelete import SETTING_DAYS

    set_setting(SETTING_DAYS, str(days))
    logger.info(f"Дней до автоудаления изменено на {days} (admin: {message.from_user.id})")
    await state.clear()

    from bot.services.expired_key_autodelete import is_autodelete_enabled
    from bot.keyboards.admin_settings import expired_key_autodelete_settings_kb

    enabled = is_autodelete_enabled()
    status_text = "🟢 Включено" if enabled else "⚪ Выключено"
    await message.answer(
        f"✅ <b>Изменено:</b> {days} дней\n\n"
        f"🗑 <b>Автоудаление истекших ключей</b>\n\n"
        f"📌 <b>Статус:</b> {status_text}\n"
        f"📅 <b>Дней до удаления:</b> {days}",
        parse_mode="HTML",
        reply_markup=expired_key_autodelete_settings_kb(enabled, days),
    )


@router.callback_query(F.data == "admin_autodelete_edit_text")
async def admin_autodelete_edit_text_start(callback: CallbackQuery, state: FSMContext):
    """Starts editing the notification text through the universal editor."""
    if not is_admin(callback.from_user.id):
        return

    from bot.handlers.admin.message_editor import show_message_editor

    await show_message_editor(
        callback.message, state,
        key='expired_key_deletion_notice',
        back_callback='admin_autodelete',
        allowed_types=['text', 'photo', 'video', 'animation'],
    )
    await callback.answer()
