"""
Handlers for the “Trial subscription” section in the admin panel.

Trial feature management:
- On/off
- Editing page text
- Select a tariff (including inactive ones, except Admin Tariff)
"""
import logging
from aiogram import Router, F
from aiogram.types import CallbackQuery, Message
from aiogram.fsm.context import FSMContext
from aiogram.filters import StateFilter

from bot.states.admin_states import AdminStates
from bot.utils.admin import is_admin
from bot.utils.text import escape_html, safe_edit_or_send

logger = logging.getLogger(__name__)

from bot.utils.text import safe_edit_or_send

router = Router()
from bot.utils.admin import is_admin as _is_admin_guard
router.callback_query.filter(lambda c: _is_admin_guard(c.from_user.id))
router.message.filter(lambda m: m.from_user is not None and _is_admin_guard(m.from_user.id))


# ============================================================================
# AUXILIARY FUNCTION: DISPLAYING MENU
# ============================================================================

async def show_trial_menu(callback: CallbackQuery):
    """Shows the trial subscription settings menu."""
    from database.requests import (
        get_setting, is_trial_enabled, get_trial_tariff_id, get_tariff_by_id, get_trial_mode
    )
    from bot.keyboards.admin import trial_settings_kb

    enabled = is_trial_enabled()
    tariff_id = get_trial_tariff_id()
    tariff_name = None
    mode = get_trial_mode()

    if tariff_id:
        tariff = get_tariff_by_id(tariff_id)
        if tariff:
            status = "🟢" if tariff['is_active'] else "⚪"
            tariff_name = f"{status} {tariff['name']} ({tariff['duration_days']} дн.)"

    status_text = "🟢 Включена" if enabled else "⚪ Выключена"
    tariff_text = tariff_name if tariff_name else "_не задан_"
    mode_text = "🎯 Один пробник на весь аккаунт" if mode == 'account' else "📂 По одному пробнику в каждой группе тарифов"

    text = (
        "🎁 <b>Пробная подписка</b>\n\n"
        "Управление функцией пробного доступа для новых пользователей.\n\n"
        f"📌 <b>Статус:</b> {escape_html(status_text)}\n"
        f"📋 <b>Общий тариф:</b> {tariff_text}\n"
        f"⚙️ <b>Режим:</b> {mode_text}\n\n"
        "❓ <b>Как работает:</b>\n"
        "• Режим «на весь аккаунт» — этот общий тариф выдаётся один раз, и всё.\n"
        "• Режим «по группам» — общий тариф игнорируется, вместо него используются пробные тарифы, заданные ОТДЕЛЬНО у каждой группы тарифов (раздел «Группы тарифов»). Пользователь может взять по одному пробнику в каждой такой группе."
    )

    await safe_edit_or_send(callback.message, 
        text,
        reply_markup=trial_settings_kb(enabled, tariff_name, mode, _device_guard_label())
    )
    await callback.answer()


# ============================================================================
# MAIN SCREEN FOR TRIAL SUBSCRIPTION
# ============================================================================

@router.callback_query(F.data == "admin_trial")
async def admin_trial_menu(callback: CallbackQuery):
    """Shows the trial subscription management menu."""
    if not is_admin(callback.from_user.id):
        return
    await show_trial_menu(callback)


# ============================================================================
# ON/OFF
# ============================================================================

async def _set_trial_enabled(callback: CallbackQuery, target_enabled: bool):
    """Sets the trial subscription status."""
    if not is_admin(callback.from_user.id):
        return

    from database.requests import set_setting, is_trial_enabled

    current = is_trial_enabled()
    if current == target_enabled:
        status = "уже включена" if target_enabled else "уже выключена"
        await callback.answer(f"Пробная подписка {status}")
        return

    new_value = '1' if target_enabled else '0'
    set_setting('trial_enabled', new_value)

    action = "включена" if new_value == '1' else "выключена"
    logger.info(f"Пробная подписка {action} (admin: {callback.from_user.id})")

    await show_trial_menu(callback)


@router.callback_query(F.data.startswith("admin_trial_set:"))
async def admin_trial_set(callback: CallbackQuery):
    """Enables or disables the trial subscription with the selected state."""
    target_enabled = callback.data.rsplit(":", 1)[1] == "1"
    await _set_trial_enabled(callback, target_enabled)


@router.callback_query(F.data == "admin_trial_toggle")
async def admin_trial_toggle(callback: CallbackQuery):
    """Compatible toggle for old posts."""
    from database.requests import is_trial_enabled
    await _set_trial_enabled(callback, not is_trial_enabled())


@router.callback_query(F.data == "admin_trial_toggle_mode")
async def admin_trial_toggle_mode(callback: CallbackQuery):
    """Переключает режим пробника: account <-> per_group."""
    if not is_admin(callback.from_user.id):
        return
    from database.requests import get_trial_mode, set_trial_mode
    new_mode = 'per_group' if get_trial_mode() == 'account' else 'account'
    set_trial_mode(new_mode)
    logger.info(f"Режим пробника изменён на '{new_mode}' (admin: {callback.from_user.id})")
    await show_trial_menu(callback)


# ============================================================================
# TEXT EDITING
# ============================================================================

@router.callback_query(F.data == "admin_trial_edit_text")
async def admin_trial_edit_text_start(callback: CallbackQuery, state: FSMContext):
    """Starts editing the text of the trial subscription through the universal editor."""
    if not is_admin(callback.from_user.id):
        return

    from bot.handlers.admin.message_editor import show_message_editor

    await show_message_editor(
        callback.message, state,
        key='trial',
        back_callback='admin_trial',
        allowed_types=['text', 'photo', 'video', 'animation'],
    )
    await callback.answer()



# ============================================================================
# CHOICE OF TARIFF
# ============================================================================

@router.callback_query(F.data == "admin_trial_select_tariff")
async def admin_trial_select_tariff(callback: CallbackQuery):
    """Shows a list of tariffs for selecting a trial period."""
    if not is_admin(callback.from_user.id):
        return

    from database.requests import get_all_tariffs, get_trial_tariff_id
    from bot.keyboards.admin import trial_tariff_select_kb

    # We receive ALL tariffs including inactive ones
    tariffs = get_all_tariffs(include_hidden=True)
    selected_id = get_trial_tariff_id()

    # Filtering Admin Tariff
    available = [t for t in tariffs if t.get('name') != 'Admin Tariff']

    if not available:
        await callback.answer("❌ Нет доступных тарифов", show_alert=True)
        return

    await safe_edit_or_send(callback.message, 
        "📋 <b>Выбор тарифа для пробной подписки</b>\n\n"
        "Выберите тариф, который будет выдаваться пользователям.\n"
        "Отображаются все тарифы, включая неактивные для покупки.\n\n"
        "🟢 — активный тариф  |  ⚪ — неактивный тариф\n"
        "🔘 — текущий выбор",
        reply_markup=trial_tariff_select_kb(available, selected_id)
    )
    await callback.answer()


@router.callback_query(F.data.startswith("admin_trial_set_tariff:"))
async def admin_trial_set_tariff(callback: CallbackQuery):
    """Sets the selected rate for the trial subscription."""
    if not is_admin(callback.from_user.id):
        return

    from database.requests import set_setting, get_tariff_by_id

    tariff_id = int(callback.data.split(":")[1])
    tariff = get_tariff_by_id(tariff_id)

    if not tariff:
        await callback.answer("❌ Тариф не найден", show_alert=True)
        return

    set_setting('trial_tariff_id', str(tariff_id))
    logger.info(
        f"Тариф пробной подписки изменён на ID={tariff_id} "
        f"({tariff['name']}) (admin: {callback.from_user.id})"
    )

    await callback.answer(f"✅ Тариф «{tariff['name']}» выбран", show_alert=False)
    await show_trial_menu(callback)


# ============================================================================
# v1.191: УЧЁТ УСТРОЙСТВ ДЛЯ ПРОБНИКОВ (только с лицензией: функция пробного периода)
# ============================================================================

def _device_guard_label() -> str:
    """Подпись кнопки в меню пробного периода."""
    from bot.services.license import is_feature_available
    from bot.services import trial_device as td
    if not is_feature_available("trial_period"):
        return "📱 Учёт устройств 🔒"
    return f"📱 Учёт устройств: {'✅ вкл' if td.guard_setting_on() else '⬜️ выкл'}"


async def _trial_devices_locked(callback: CallbackQuery) -> bool:
    from bot.services.license import is_feature_available
    if is_feature_available("trial_period"):
        return False
    await callback.answer(
        "🔒 Эта функция недоступна без лицензии. Обратитесь к поставщику лицензии.", show_alert=True
    )
    return True


async def _show_trial_devices(callback: CallbackQuery):
    from aiogram.types import InlineKeyboardButton
    from aiogram.utils.keyboard import InlineKeyboardBuilder
    from bot.services import trial_device as td

    on = td.guard_setting_on()
    st = td.stats()
    blocks = td.recent_blocks(5)
    lines = [
        "📱 <b>Учёт устройств для пробников</b>",
        "",
        "Приложение (Happ, INCY и др.) присылает идентификатор устройства. Если это устройство уже "
        "брало пробный период под другим аккаунтом, второй пробный ключ получает заблокированную "
        "подписку с кнопкой покупки. Хранится только хеш идентификатора.",
        "",
        f"📌 <b>Статус:</b> {'🟢 Включено' if on else '⚪ Выключено'}",
        f"📊 Запомнено устройств: {st['devices']}, заблокировано ключей: {st['blocks']}",
    ]
    if blocks:
        lines.append("")
        lines.append("<b>Последние блокировки:</b>")
        for b in blocks:
            lines.append(
                f"• ключ #{b['key_id']} (tg {b.get('tg') or '—'}) — устройство уже было у tg "
                f"{b.get('owner_tg') or '—'}, запросов: {b['hits']}"
            )
        lines.append("")
        lines.append("Если блокировка ошибочна, нажмите «🔓 Разрешить» — устройство попадёт в исключения.")
    kb = InlineKeyboardBuilder()
    kb.row(InlineKeyboardButton(
        text="⏸ Выключить" if on else "▶️ Включить", callback_data="admin_trial_devices_toggle"
    ))
    for b in blocks:
        kb.row(InlineKeyboardButton(
            text=f"🔓 Разрешить ключ #{b['key_id']}",
            callback_data=f"admin_trial_devices_unblock:{b['key_id']}",
        ))
    kb.row(InlineKeyboardButton(text="⬅️ Назад", callback_data="admin_trial"))
    await safe_edit_or_send(callback.message, "\n".join(lines), reply_markup=kb.as_markup())
    await callback.answer()


@router.callback_query(F.data == "admin_trial_devices")
async def admin_trial_devices(callback: CallbackQuery):
    if not is_admin(callback.from_user.id) or await _trial_devices_locked(callback):
        return
    await _show_trial_devices(callback)


@router.callback_query(F.data == "admin_trial_devices_toggle")
async def admin_trial_devices_toggle(callback: CallbackQuery):
    if not is_admin(callback.from_user.id) or await _trial_devices_locked(callback):
        return
    from bot.services import trial_device as td
    td.set_guard_enabled(not td.guard_setting_on())
    await _show_trial_devices(callback)


@router.callback_query(F.data.startswith("admin_trial_devices_unblock:"))
async def admin_trial_devices_unblock(callback: CallbackQuery):
    if not is_admin(callback.from_user.id) or await _trial_devices_locked(callback):
        return
    from bot.services import trial_device as td
    try:
        key_id = int(callback.data.split(":", 1)[1])
    except ValueError:
        await callback.answer()
        return
    td.unblock_key(key_id)
    await _show_trial_devices(callback)
