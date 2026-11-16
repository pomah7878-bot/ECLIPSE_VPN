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

    try:
        from bot.services import trial_guard as _tg
        _st = _tg.stats()
        abuse_label = f"🛡 Защита от повторных: {_st['events_24h']} за 24 ч"
    except Exception:  # noqa: BLE001
        abuse_label = "🛡 Защита от повторных"

    text = (
        "🎁 <b>Пробная подписка</b>\n\n"
        "Управление функцией пробного доступа для новых пользователей.\n\n"
        f"📌 <b>Статус:</b> {escape_html(status_text)}\n"
        f"📋 <b>Общий тариф:</b> {tariff_text}\n"
        f"⚙️ <b>Режим:</b> {mode_text}\n\n"
        "❓ <b>Как это работает</b>\n\n"
        "🎯 <b>На весь аккаунт</b>\n"
        "• выдаётся общий тариф из этого экрана\n"
        "• один раз на аккаунт\n\n"
        "📂 <b>По группам</b>\n"
        "• общий тариф не используется\n"
        "• у каждой группы тарифов свой пробный тариф (раздел «Группы тарифов»)\n"
        "• пользователь может взять по одному пробнику в каждой группе"
    )

    await safe_edit_or_send(callback.message, 
        text,
        reply_markup=trial_settings_kb(enabled, tariff_name, mode, _device_guard_label(), abuse_label)
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
    available = [t for t in tariffs if t.get('name') != 'Admin Tariff'
                 and not str(t.get('name') or '').startswith('Совместная акция: ')]

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
        "Не даёт получить второй пробный период на одном и том же телефоне под другим аккаунтом.",
        "",
        "<b>Как это работает:</b>",
        "• приложение (Happ, INCY, ECLIPSE TV, v2rayTun и др.) присылает идентификатор устройства",
        "• если устройство уже брало пробник под другим аккаунтом — второй пробный ключ блокируется",
        "• вместо подписки человек видит кнопку покупки",
        "",
        "🔐 Хранится только хеш идентификатора, сам идентификатор не сохраняется.",
        "",
        f"📌 <b>Статус:</b> {'🟢 Включено' if on else '⚪ Выключено'}",
        f"📊 Запомнено устройств: {st['devices']}, заблокировано ключей: {st['blocks']}",
    ]
    if blocks:
        lines.append("")
        lines.append("<b>Последние блокировки:</b>")
        for b in blocks:
            device = (b.get('device_label') or b.get('client') or '').strip()
            device_suffix = f" · {escape_html(device)}" if device else ""
            lines.append(
                f"• ключ #{b['key_id']} (tg {b.get('tg') or '—'}){device_suffix} — устройство уже было у tg "
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


# ============================================================================
# v1.560: ЗАЩИТА ОТ ПОВТОРНОЙ ВЫДАЧИ ПРОБНИКА
# ============================================================================

_AUTOBAN_STEPS = (0, 3, 5, 10)


def _short_ts(value) -> str:
    text = str(value or '')
    return text[5:16] if len(text) >= 16 else text


async def _show_trial_abuse(callback: CallbackQuery):
    """Экран журнала попыток и запретов на пробник."""
    from aiogram.types import InlineKeyboardButton
    from aiogram.utils.keyboard import InlineKeyboardBuilder
    from database.requests import (
        get_trial_abuse_alert_threshold, get_trial_auto_ban_threshold, is_trial_strict_identity,
    )
    from bot.services import trial_guard

    st = trial_guard.stats()
    events = trial_guard.recent_events(6)
    blocks = trial_guard.blocked_list(6)
    strict = is_trial_strict_identity()
    autoban = get_trial_auto_ban_threshold()
    alert_at = get_trial_abuse_alert_threshold()

    lines = [
        "🛡 <b>Защита от повторных пробников</b>",
        "",
        "Один реестр для бота, сайта и приложения: пробник блокируется по Telegram, "
        "сайт-аккаунту и подтверждённому номеру.",
        "",
        f"📌 <b>Строгий режим:</b> {'🟢 включён' if strict else '⚪️ выключен'}",
        "   <i>требует подтверждённый номер перед пробником</i>",
        f"📌 <b>Автобан:</b> {'⚪️ выключен' if autoban <= 0 else f'🟢 после {autoban} попыток за 24 ч'}",
        f"📌 <b>Уведомления:</b> после {alert_at} попыток за 24 ч",
        "",
        f"📊 Выдано по реестру: <b>{st['claims']}</b>",
        f"📊 Отказов за 24 ч: <b>{st['events_24h']}</b>",
        f"📊 Запретов: <b>{st['blocks']}</b>",
    ]

    if events:
        lines += ["", "<b>Последние попытки:</b>"]
        for ev in events:
            who = trial_guard.identity_label(ev['identity_kind'], ev['identity_value'])
            reason = trial_guard.REASON_LABELS.get(ev['reason'], ev['reason'])
            channel = trial_guard.CHANNEL_LABELS.get(ev['channel'], ev['channel'])
            lines.append(
                f"• {escape_html(_short_ts(ev['created_at']))} · {escape_html(who)} · "
                f"{escape_html(reason)} · {escape_html(channel)}"
            )
    else:
        lines += ["", "Попыток повторной выдачи пока нет."]

    if blocks:
        lines += ["", "<b>Запреты на пробник:</b>"]
        for block in blocks:
            who = trial_guard.identity_label(block['identity_kind'], block['identity_value'])
            by = "авто" if block.get('created_by') == 'auto' else "вручную"
            lines.append(f"• {escape_html(who)} · {by}")

    kb = InlineKeyboardBuilder()
    kb.row(InlineKeyboardButton(
        text=('⚪️ Выключить строгий режим' if strict else '🟢 Включить строгий режим'),
        callback_data='admin_trial_abuse_strict',
    ))
    next_step = _AUTOBAN_STEPS[(_AUTOBAN_STEPS.index(autoban) + 1) % len(_AUTOBAN_STEPS)] if autoban in _AUTOBAN_STEPS else 0
    autoban_label = '⚪️ Автобан выключен' if autoban <= 0 else f'⛔️ Автобан: {autoban}'
    kb.row(InlineKeyboardButton(
        text=f'{autoban_label} → {("выкл" if next_step == 0 else next_step)}',
        callback_data='admin_trial_abuse_autoban',
    ))
    for ev in events[:4]:
        if trial_guard.find_block([(ev['identity_kind'], ev['identity_value'])]):
            continue
        who = trial_guard.identity_label(ev['identity_kind'], ev['identity_value'])
        kb.row(InlineKeyboardButton(
            text=f"🚫 Запретить пробник: {who}"[:60],
            callback_data=f"admin_trial_abuse_block:{ev['id']}",
        ))
    for block in blocks[:4]:
        who = trial_guard.identity_label(block['identity_kind'], block['identity_value'])
        kb.row(InlineKeyboardButton(
            text=f"🔓 Снять запрет: {who}"[:60],
            callback_data=f"admin_trial_abuse_unblock:{block['id']}",
        ))
    kb.row(InlineKeyboardButton(text='🔄 Обновить', callback_data='admin_trial_abuse'))
    from bot.keyboards.admin_misc import back_button, home_button
    kb.row(back_button('admin_trial'), home_button())
    await safe_edit_or_send(callback.message, "\n".join(lines), reply_markup=kb.as_markup())
    await callback.answer()


@router.callback_query(F.data == "admin_trial_abuse")
async def admin_trial_abuse(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        return
    await _show_trial_abuse(callback)


@router.callback_query(F.data == "admin_trial_abuse_strict")
async def admin_trial_abuse_strict(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        return
    from database.requests import is_trial_strict_identity, set_trial_strict_identity
    set_trial_strict_identity(not is_trial_strict_identity())
    await _show_trial_abuse(callback)


@router.callback_query(F.data == "admin_trial_abuse_autoban")
async def admin_trial_abuse_autoban(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        return
    from database.requests import get_trial_auto_ban_threshold, set_trial_auto_ban_threshold
    current = get_trial_auto_ban_threshold()
    next_step = _AUTOBAN_STEPS[(_AUTOBAN_STEPS.index(current) + 1) % len(_AUTOBAN_STEPS)] if current in _AUTOBAN_STEPS else 0
    set_trial_auto_ban_threshold(next_step)
    await _show_trial_abuse(callback)


@router.callback_query(F.data.startswith("admin_trial_abuse_block:"))
async def admin_trial_abuse_block(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        return
    from bot.services import trial_guard
    try:
        event_id = int(callback.data.split(':', 1)[1])
    except (ValueError, IndexError):
        await callback.answer()
        return
    event = trial_guard.event_by_id(event_id)
    if event:
        trial_guard.block_identity(
            event['identity_kind'],
            event['identity_value'],
            reason=f"manual:{event['reason']}",
            created_by=f"admin:{callback.from_user.id}",
        )
    await _show_trial_abuse(callback)


@router.callback_query(F.data.startswith("admin_trial_abuse_unblock:"))
async def admin_trial_abuse_unblock(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        return
    from bot.services import trial_guard
    try:
        block_id = int(callback.data.split(':', 1)[1])
    except (ValueError, IndexError):
        await callback.answer()
        return
    trial_guard.unblock(block_id)
    await _show_trial_abuse(callback)
