import logging
from aiogram import Router, F
from aiogram.types import (
    CallbackQuery, InlineKeyboardButton, Message, Contact,
    ReplyKeyboardMarkup, KeyboardButton, ReplyKeyboardRemove,
)
from aiogram.utils.keyboard import InlineKeyboardBuilder
from aiogram.fsm.context import FSMContext
from bot.utils.text import safe_edit_or_send
from bot.states.user_states import TrialPhoneVerification

logger = logging.getLogger(__name__)

router = Router()


@router.callback_query(F.data == 'trial_subscription')
async def show_trial_subscription(callback: CallbackQuery):
    """Показывает пробник: страницу тарифа (режим 'account') или выбор
    группы, если пробников несколько (режим 'per_group')."""
    from database.requests import (
        is_trial_enabled, get_trial_tariff_id, has_used_trial, get_trial_mode,
        get_groups_with_trial, get_user_internal_id, get_eligible_trial_group_ids,
    )
    from bot.utils.page_renderer import render_page

    user_id = callback.from_user.id

    if not is_trial_enabled():
        await callback.answer('❌ Пробная подписка недоступна', show_alert=True)
        return

    mode = get_trial_mode()

    if mode == 'account':
        if get_trial_tariff_id() is None:
            await callback.answer('❌ Тариф не настроен', show_alert=True)
            return
        if has_used_trial(user_id):
            await callback.answer('ℹ️ Вы уже использовали пробный период', show_alert=True)
            return
        await render_page(callback, page_key='trial')
        await callback.answer()
        return

    # Режим 'per_group'
    groups = get_groups_with_trial()
    if not groups:
        await callback.answer('❌ Пробники не настроены', show_alert=True)
        return

    internal_id = get_user_internal_id(user_id)
    if not internal_id:
        await callback.answer('❌ Ошибка профиля', show_alert=True)
        return
    eligible_ids = get_eligible_trial_group_ids(internal_id, [g['id'] for g in groups])
    available = [g for g in groups if g['id'] in eligible_ids]

    if not available:
        await callback.answer('ℹ️ Пробный период доступен только новым пользователям без активных или прошлых ключей', show_alert=True)
        return

    if len(available) == 1:
        await _show_group_trial_details(callback, available[0]['id'])
        return

    builder = InlineKeyboardBuilder()
    for g in available:
        builder.row(InlineKeyboardButton(text=f"🎁 {g['name']}", callback_data=f"trial_subscription_group:{g['id']}"))
    builder.row(InlineKeyboardButton(text='⬅️ Назад', callback_data='start'))
    await safe_edit_or_send(
        callback.message,
        "🎁 <b>Выберите пробную группу</b>\n\n"
        "В каждой из этих групп тарифов доступен свой пробный период — можно взять по одному из каждой.",
        reply_markup=builder.as_markup(),
    )
    await callback.answer()


@router.callback_query(F.data.startswith('trial_subscription_group:'))
async def show_trial_subscription_group(callback: CallbackQuery):
    """Показывает детали пробника конкретной группы (режим 'per_group')."""
    group_id = int(callback.data.split(':')[1])
    await _show_group_trial_details(callback, group_id)


async def _show_group_trial_details(callback: CallbackQuery, group_id: int):
    """Экран подтверждения активации пробника конкретной группы тарифов."""
    from database.requests import get_group_by_id, get_tariff_by_id

    group = get_group_by_id(group_id)
    if not group or not group.get('trial_tariff_id'):
        await callback.answer('❌ Пробник для этой группы недоступен', show_alert=True)
        return
    tariff = get_tariff_by_id(group['trial_tariff_id'])
    if not tariff:
        await callback.answer('❌ Тариф не найден', show_alert=True)
        return

    traffic_limit_gb = tariff.get('traffic_limit_gb') or 0
    traffic_text = f"{traffic_limit_gb} ГБ" if traffic_limit_gb > 0 else 'Безлимит'
    text = (
        f"🎁 <b>Пробная подписка — {group['name']}</b>\n\n"
        f"📋 Тариф: {tariff['name']}\n"
        f"📅 Срок: {tariff['duration_days']} дн.\n"
        f"📊 Трафик: {traffic_text}\n\n"
        f"Активировать пробный доступ?"
    )
    builder = InlineKeyboardBuilder()
    builder.row(InlineKeyboardButton(text='✅ Активировать', callback_data=f'trial_activate_group:{group_id}'))
    builder.row(InlineKeyboardButton(text='⬅️ Назад', callback_data='trial_subscription'))
    await safe_edit_or_send(callback.message, text, reply_markup=builder.as_markup())
    await callback.answer()


async def _activate_trial(
    *,
    bot,
    from_user,
    state: FSMContext,
    tariff_id: int,
    mark_used_callback,
    reply_target: Message,
    answer_fn=None,
    delete_message=None,
):
    """Общая логика активации пробника — создание ключа по стандартному
    механизму. mark_used_callback(internal_user_id) вызывается ПОСЛЕ
    успешной проверки, чтобы отметить использование (глобально или по
    группе, в зависимости от режима).

    Принимает разложенные параметры вместо целого callback — вызывается
    как из callback-хендлеров (кнопка "Активировать"), так и из
    message-хендлера после получения контакта (там callback'а нет)."""
    from database.requests import get_tariff_by_id, get_or_create_user, create_initial_vpn_key, create_pending_order, complete_order
    from bot.handlers.user.payments.keys_config import start_new_key_config

    user_id = from_user.id

    tariff = get_tariff_by_id(tariff_id)
    if not tariff:
        if answer_fn:
            await answer_fn('❌ Тариф не найден', show_alert=True)
        return

    (user, _) = get_or_create_user(
        user_id,
        from_user.username,
        first_name=getattr(from_user, 'first_name', None),
        last_name=getattr(from_user, 'last_name', None),
    )
    internal_user_id = user['id']
    mark_used_callback(internal_user_id)
    logger.info(f'Пользователь {user_id} активировал пробный период (тариф ID={tariff_id})')

    duration_days = tariff['duration_days']
    traffic_limit_bytes = (tariff.get('traffic_limit_gb', 0) or 0) * 1024 ** 3
    key_id = create_initial_vpn_key(internal_user_id, tariff_id, duration_days, traffic_limit=traffic_limit_bytes)
    (_, order_id) = create_pending_order(user_id=internal_user_id, tariff_id=tariff_id, payment_type='trial', vpn_key_id=key_id)
    complete_order(order_id)
    try:
        from bot.services.key_lifecycle import emit_key_lifecycle_event_safe

        await emit_key_lifecycle_event_safe(
            'key_created',
            {
                'key_id': key_id,
                'user_id': internal_user_id,
                'tariff_id': tariff_id,
                'days': duration_days,
                'traffic_limit': traffic_limit_bytes,
                'order_id': order_id,
                'payment_type': 'trial',
                'source': 'trial',
            },
        )
    except Exception as hook_err:
        logger.warning(f"Не удалось вызвать lifecycle hooks trial-ключа {key_id}: {hook_err}")

    try:
        from bot.services.notifications import notify_admins_payment
        from database.requests import find_order_by_order_id
        trial_order = find_order_by_order_id(order_id)
        if trial_order:
            await notify_admins_payment(bot, trial_order)
    except Exception as notify_err:
        logger.warning(f'Ошибка уведомления о trial: {notify_err}')

    await state.update_data(
        new_key_order_id=order_id,
        new_key_id=key_id,
        new_key_owner_telegram_id=user_id,
        new_key_owner_username=from_user.username,
    )
    if answer_fn:
        await answer_fn()
    if delete_message:
        try:
            await delete_message.delete()
        except Exception:
            pass
    await start_new_key_config(
        reply_target,
        state,
        order_id,
        key_id,
        owner_telegram_id=user_id,
        owner_username=from_user.username,
    )


def _phone_verification_kb() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text='📱 Поделиться номером телефона', request_contact=True)],
            [KeyboardButton(text='❌ Отмена')],
        ],
        resize_keyboard=True,
        one_time_keyboard=True,
    )


async def _request_trial_phone_verification(
    message_to_edit: Message,
    state: FSMContext,
    *,
    tariff_id: int,
    group_id=None,
):
    """Запрашивает у пользователя подтверждение номера телефона перед
    выдачей пробного периода — единственный практически надёжный способ
    не дать получить пробник дважды (в боте и на сайте) под разными
    аккаунтами. Номер передаётся через встроенный механизм Telegram
    (кнопка "Поделиться контактом"), уже верифицированный самим Telegram
    — никаких SMS с нашей стороны не требуется."""
    await state.update_data(trial_tariff_id=tariff_id, trial_group_id=group_id)
    await state.set_state(TrialPhoneVerification.waiting_for_contact)
    try:
        await message_to_edit.delete()
    except Exception:
        pass
    await message_to_edit.answer(
        "📱 <b>Подтверждение номера телефона</b>\n\n"
        "Чтобы получить пробный период, поделись своим номером телефона — "
        "это защита от повторного получения одним и тем же человеком "
        "(в том числе через сайт). Номер передаётся напрямую через Telegram, "
        "мы не увидим ничего лишнего.\n\n"
        "Нажми кнопку ниже:",
        reply_markup=_phone_verification_kb(),
    )


@router.message(TrialPhoneVerification.waiting_for_contact, F.contact.as_('contact'))
async def handle_trial_phone_contact(message: Message, state: FSMContext, contact: Contact):
    if contact.user_id != message.from_user.id:
        await message.answer(
            "❌ Это не твой собственный номер телефона. Поделись именно своим контактом.",
            reply_markup=_phone_verification_kb(),
        )
        return

    from bot.services.trial_phone_registry import has_phone_used_trial, mark_phone_trial_used

    if has_phone_used_trial(contact.phone_number):
        await state.clear()
        await message.answer(
            "ℹ️ Этот номер телефона уже использовался для получения пробного периода "
            "(в боте или на сайте) — повторно получить его нельзя.",
            reply_markup=ReplyKeyboardRemove(),
        )
        return

    data = await state.get_data()
    tariff_id = data.get('trial_tariff_id')
    group_id = data.get('trial_group_id')
    if not tariff_id:
        await state.clear()
        await message.answer("❌ Сессия устарела, попробуй ещё раз через меню.", reply_markup=ReplyKeyboardRemove())
        return

    marked = mark_phone_trial_used(contact.phone_number, telegram_id=message.from_user.id)
    if not marked:
        # Кто-то успел использовать этот номер за долю секунды до нас
        await state.clear()
        await message.answer(
            "ℹ️ Этот номер телефона уже использовался для получения пробного периода.",
            reply_markup=ReplyKeyboardRemove(),
        )
        return

    await message.answer("✅ Номер подтверждён, активирую пробный период...", reply_markup=ReplyKeyboardRemove())

    if group_id:
        from database.requests import mark_group_trial_used
        mark_used_callback = lambda uid: mark_group_trial_used(uid, group_id)
    else:
        from database.requests import mark_trial_used
        mark_used_callback = mark_trial_used

    await _activate_trial(
        bot=message.bot,
        from_user=message.from_user,
        state=state,
        tariff_id=tariff_id,
        mark_used_callback=mark_used_callback,
        reply_target=message,
    )


@router.message(TrialPhoneVerification.waiting_for_contact)
async def handle_trial_phone_other_message(message: Message, state: FSMContext):
    """Любое другое сообщение в состоянии ожидания контакта — отмена
    или напоминание нажать кнопку."""
    if (message.text or '').strip() == '❌ Отмена':
        await state.clear()
        await message.answer("Отменено.", reply_markup=ReplyKeyboardRemove())
        return
    await message.answer(
        "Пожалуйста, воспользуйся кнопкой ниже, чтобы поделиться номером телефона, "
        "либо нажми «❌ Отмена».",
        reply_markup=_phone_verification_kb(),
    )


@router.callback_query(F.data == 'trial_activate')
async def activate_trial_subscription(callback: CallbackQuery, state: FSMContext):
    """Запускает верификацию телефона перед активацией пробника в режиме
    'account' (один пробник на весь аккаунт)."""
    from database.requests import is_trial_enabled, get_trial_tariff_id, has_used_trial

    user_id = callback.from_user.id

    if not is_trial_enabled():
        await callback.answer('❌ Пробная подписка недоступна', show_alert=True)
        return
    tariff_id = get_trial_tariff_id()
    if tariff_id is None:
        await callback.answer('❌ Тариф не настроен', show_alert=True)
        return
    if has_used_trial(user_id):
        await callback.answer('ℹ️ Вы уже использовали пробный период', show_alert=True)
        return

    await callback.answer()
    await _request_trial_phone_verification(callback.message, state, tariff_id=tariff_id)


@router.callback_query(F.data.startswith('trial_activate_group:'))
async def activate_trial_subscription_group(callback: CallbackQuery, state: FSMContext):
    """Запускает верификацию телефона перед активацией пробника
    конкретной группы в режиме 'per_group'."""
    from database.requests import (
        is_trial_enabled, get_group_by_id, get_user_internal_id,
        get_eligible_trial_group_ids,
    )

    if not is_trial_enabled():
        await callback.answer('❌ Пробная подписка недоступна', show_alert=True)
        return

    group_id = int(callback.data.split(':')[1])
    group = get_group_by_id(group_id)
    if not group or not group.get('trial_tariff_id'):
        await callback.answer('❌ Пробник для этой группы недоступен', show_alert=True)
        return

    user_id = callback.from_user.id
    internal_id = get_user_internal_id(user_id)
    if not internal_id or group_id not in get_eligible_trial_group_ids(internal_id, [group_id]):
        await callback.answer('ℹ️ Пробник для этой группы сейчас недоступен', show_alert=True)
        return

    await callback.answer()
    await _request_trial_phone_verification(
        callback.message, state, tariff_id=group['trial_tariff_id'], group_id=group_id,
    )
