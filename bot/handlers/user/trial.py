import asyncio
import logging
from aiogram import Router, F
from aiogram.types import (
    CallbackQuery, InlineKeyboardButton, Message,
)
from aiogram.utils.keyboard import InlineKeyboardBuilder
from aiogram.fsm.context import FSMContext
from bot.utils.text import safe_edit_or_send

logger = logging.getLogger(__name__)

router = Router()


def _refuse_trial(user_id: int, scope: str, reason: str, detail: str = '') -> None:
    """Пишет отказ в журнал и, если пора, уведомляет админов."""
    try:
        from bot.services import trial_guard
        identities = trial_guard.bot_identities(user_id)
        result = trial_guard.record_refusal(
            identities,
            primary=('telegram', str(user_id)),
            scope=scope,
            channel='bot',
            reason=reason,
            detail=detail,
        )
        if result.get('alert'):
            asyncio.create_task(trial_guard.send_abuse_alert(
                channel='bot',
                identities=identities,
                primary=('telegram', str(user_id)),
                reason=reason,
                count=result.get('count') or 0,
                auto_banned=bool(result.get('auto_banned')),
            ))
    except Exception as e:  # noqa: BLE001
        logger.debug('trial: не удалось записать отказ (%s): %s', reason, e)


@router.callback_query(F.data == 'trial_subscription')
async def show_trial_subscription(callback: CallbackQuery):
    """Показывает пробник: страницу тарифа (режим 'account') или выбор
    группы, если пробников несколько (режим 'per_group')."""
    from database.requests import (
        is_trial_enabled, get_trial_tariff_id, get_trial_mode,
        get_groups_with_trial, get_user_internal_id, get_eligible_trial_group_ids,
        is_user_banned,
    )
    from bot.utils.page_renderer import render_page
    from bot.services import trial_guard

    user_id = callback.from_user.id

    if is_user_banned(user_id):
        await callback.answer('⛔ Доступ заблокирован', show_alert=True)
        return

    from bot.services.license import is_feature_available
    if not is_feature_available("trial_period"):
        await callback.answer('❌ Пробная подписка недоступна', show_alert=True)
        return
    if not is_trial_enabled():
        await callback.answer('❌ Пробная подписка недоступна', show_alert=True)
        return

    mode = get_trial_mode()

    if mode == 'account':
        if get_trial_tariff_id() is None:
            await callback.answer('❌ Тариф не настроен', show_alert=True)
            return
        status = trial_guard.status_bot(user_id, 'account')
        if not status['allowed']:
            await callback.answer(status['message'], show_alert=True)
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
    # Единый реестр: убираем группы, где пробник уже выдан или запрещён.
    available = [
        g for g in available
        if trial_guard.status_bot(user_id, f"group:{g['id']}")['allowed']
    ]

    if not available:
        await callback.answer(
            'ℹ️ Пробный период недоступен: он уже использован, запрещён или вы не новый клиент',
            show_alert=True,
        )
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
    on_success=None,
    reply_target: Message,
    answer_fn=None,
    delete_message=None,
):
    """Общая логика активации пробника — создание ключа по стандартному
    механизму. on_success(internal_user_id) вызывается ТОЛЬКО после того,
    как ключ и заказ реально созданы (иначе при сбое человек потерял бы
    пробник, хотя отметка уже стояла бы)."""
    from database.requests import get_tariff_by_id, get_or_create_user, create_initial_vpn_key, create_pending_order, complete_order
    from bot.handlers.user.payments.keys_config import start_new_key_config

    user_id = from_user.id

    tariff = get_tariff_by_id(tariff_id)
    if not tariff:
        if answer_fn:
            await answer_fn('❌ Тариф не найден', show_alert=True)
        return False

    (user, _) = get_or_create_user(
        user_id,
        from_user.username,
        first_name=getattr(from_user, 'first_name', None),
        last_name=getattr(from_user, 'last_name', None),
    )
    internal_user_id = user['id']

    duration_days = tariff['duration_days']
    traffic_limit_bytes = (tariff.get('traffic_limit_gb', 0) or 0) * 1024 ** 3
    key_id = create_initial_vpn_key(internal_user_id, tariff_id, duration_days, traffic_limit=traffic_limit_bytes)
    (_, order_id) = create_pending_order(user_id=internal_user_id, tariff_id=tariff_id, payment_type='trial', vpn_key_id=key_id)
    complete_order(order_id)
    logger.info(f'Пользователь {user_id} активировал пробный период (тариф ID={tariff_id})')

    if on_success:
        try:
            on_success(internal_user_id)
        except Exception as mark_err:  # noqa: BLE001
            logger.warning(f'Не удалось обновить старый учёт пробника для {internal_user_id}: {mark_err}')

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
    return True


@router.callback_query(F.data == 'trial_activate')
async def activate_trial_subscription(callback: CallbackQuery, state: FSMContext):
    """Активирует пробник в режиме 'account' (один пробник на весь аккаунт)."""
    from database.requests import is_trial_enabled, get_trial_tariff_id, mark_trial_used, is_user_banned
    from bot.services.license import is_feature_available
    from bot.services import trial_guard

    user_id = callback.from_user.id

    if is_user_banned(user_id):
        await callback.answer('⛔ Доступ заблокирован', show_alert=True)
        return
    if not is_feature_available("trial_period"):
        await callback.answer('❌ Пробная подписка недоступна', show_alert=True)
        return
    if not is_trial_enabled():
        await callback.answer('❌ Пробная подписка недоступна', show_alert=True)
        return
    tariff_id = get_trial_tariff_id()
    if tariff_id is None:
        await callback.answer('❌ Тариф не настроен', show_alert=True)
        return

    status = trial_guard.status_bot(user_id, 'account')
    if not status['allowed']:
        _refuse_trial(user_id, 'account', status['reason'])
        await callback.answer(status['message'], show_alert=True)
        return

    try:
        claim = trial_guard.claim_bot(user_id, 'account', channel='bot')
    except Exception:
        logger.exception('Не удалось зафиксировать выдачу пробника для %s', user_id)
        await callback.answer('❌ Не удалось активировать пробный период. Попробуйте позже.', show_alert=True)
        return
    if not claim.get('ok'):
        _refuse_trial(user_id, 'account', 'already_used', detail='claim_conflict')
        await callback.answer(trial_guard.REASON_MESSAGES['already_used'], show_alert=True)
        return

    try:
        activated = await _activate_trial(
            bot=callback.bot,
            from_user=callback.from_user,
            state=state,
            tariff_id=tariff_id,
            on_success=mark_trial_used,
            reply_target=callback.message,
            answer_fn=callback.answer,
            delete_message=callback.message,
        )
        if not activated:
            trial_guard.release_claim(claim)
    except Exception:
        trial_guard.release_claim(claim)
        logger.exception('Не удалось активировать пробный период для %s', user_id)
        await callback.answer('❌ Не удалось активировать пробный период. Попробуйте позже.', show_alert=True)


@router.callback_query(F.data.startswith('trial_activate_group:'))
async def activate_trial_subscription_group(callback: CallbackQuery, state: FSMContext):
    """Активирует пробник конкретной группы в режиме 'per_group'."""
    from database.requests import (
        is_trial_enabled, get_group_by_id, get_user_internal_id,
        get_eligible_trial_group_ids, mark_group_trial_used, is_user_banned,
    )
    from bot.services.license import is_feature_available
    from bot.services import trial_guard

    user_id = callback.from_user.id
    if is_user_banned(user_id):
        await callback.answer('⛔ Доступ заблокирован', show_alert=True)
        return
    if not is_feature_available("trial_period"):
        await callback.answer('❌ Пробная подписка недоступна', show_alert=True)
        return
    if not is_trial_enabled():
        await callback.answer('❌ Пробная подписка недоступна', show_alert=True)
        return

    group_id = int(callback.data.split(':')[1])
    group = get_group_by_id(group_id)
    if not group or not group.get('trial_tariff_id'):
        await callback.answer('❌ Пробник для этой группы недоступен', show_alert=True)
        return

    internal_id = get_user_internal_id(user_id)
    if not internal_id or group_id not in get_eligible_trial_group_ids(internal_id, [group_id]):
        await callback.answer('ℹ️ Пробник для этой группы сейчас недоступен', show_alert=True)
        return

    scope = f'group:{group_id}'
    status = trial_guard.status_bot(user_id, scope)
    if not status['allowed']:
        _refuse_trial(user_id, scope, status['reason'])
        await callback.answer(status['message'], show_alert=True)
        return

    try:
        claim = trial_guard.claim_bot(user_id, scope, channel='bot')
    except Exception:
        logger.exception('Не удалось зафиксировать выдачу пробника группы %s для %s', group_id, user_id)
        await callback.answer('❌ Не удалось активировать пробник. Попробуйте позже.', show_alert=True)
        return
    if not claim.get('ok'):
        _refuse_trial(user_id, scope, 'already_used', detail='claim_conflict')
        await callback.answer(trial_guard.REASON_MESSAGES['already_used'], show_alert=True)
        return

    try:
        activated = await _activate_trial(
            bot=callback.bot,
            from_user=callback.from_user,
            state=state,
            tariff_id=group['trial_tariff_id'],
            on_success=lambda uid: mark_group_trial_used(uid, group_id),
            reply_target=callback.message,
            answer_fn=callback.answer,
            delete_message=callback.message,
        )
        if not activated:
            trial_guard.release_claim(claim)
    except Exception:
        trial_guard.release_claim(claim)
        logger.exception('Не удалось активировать пробник группы %s для %s', group_id, user_id)
        await callback.answer('❌ Не удалось активировать пробник. Попробуйте позже.', show_alert=True)
