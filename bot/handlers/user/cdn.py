"""Экран клиента «CDN — обход белых списков»: статус пакета и покупка (баланс и платёжные системы)."""
import logging

from aiogram import F, Router
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup

from bot.services.panel_sync_coordinator import regular_panel_operation
from bot.utils.text import escape_html, safe_edit_or_send

logger = logging.getLogger(__name__)

router = Router()


def _back_row(key_id: int):
    return [
        InlineKeyboardButton(text='⬅️ Назад', callback_data=f'key:{key_id}'),
        InlineKeyboardButton(text='🈴 На главную', callback_data='start'),
    ]


async def _load_key(callback: CallbackQuery, key_id: int):
    from database.requests import get_key_details_for_user
    key = get_key_details_for_user(key_id, callback.from_user.id)
    if not key or not key.get('sub_id'):
        await callback.answer('Ключ не найден или у него нет подписки', show_alert=True)
        return None
    return key


def _payment_rows(key_id: int):
    """Кнопки оплаты пакета теми же способами, что и подписка (через служебный тариф CDN)."""
    from bot.services import cdn
    try:
        tariff_id = cdn.ensure_cdn_tariff()
    except Exception as e:  # noqa: BLE001
        logger.warning('CDN: служебный тариф недоступен: %s', e)
        return []
    if not tariff_id:
        return []
    try:
        from database.db_keys import get_vpn_key_by_id
        if cdn.validate_key_for_purchase(get_vpn_key_by_id(key_id)):
            return []
    except Exception:  # noqa: BLE001
        return []
    from database import db_settings as st
    methods = [
        (st.is_yookassa_qr_configured, 'renew_pay_qr', '📱 СБП / банк (ЮKassa)'),
        (st.is_cards_configured, 'renew_pay_cards', '💳 Банковской картой'),
        (st.is_stars_enabled, 'renew_pay_stars', '⭐ Telegram Stars'),
        (st.is_wata_configured, 'renew_pay_wata', '🌊 WATA'),
        (st.is_platega_configured, 'renew_pay_platega', '💠 Platega'),
        (st.is_cardlink_configured, 'renew_pay_cardlink', '🔗 Cardlink'),
    ]
    rows = []
    for check, prefix, text in methods:
        try:
            enabled = bool(check())
        except Exception:  # noqa: BLE001
            enabled = False
        if enabled:
            rows.append([InlineKeyboardButton(text=text, callback_data=f'{prefix}:{key_id}:{tariff_id}')])
    return rows


async def _show_cdn_screen(callback: CallbackQuery, key: dict, notice: str = '') -> None:
    from bot.services import cdn
    from database import db_cdn
    from database.requests import get_user_balance

    key_id = int(key['id'])
    pack = db_cdn.get_pack(key_id)
    price = cdn.get_sale_price_cents()
    gb, days = cdn.get_cdn_pack_gb(), cdn.get_cdn_pack_days()

    lines = []
    if notice:
        lines.append(notice + '\n')
    lines.append('🌐 <b>CDN — обход белых списков</b>')
    lines.append(
        '\nДоп. ключ в вашей подписке, который работает через CDN. '
        'Нужен, когда мобильный интернет пропускает только «белые» адреса. '
        'Обычные ключи подписки работают как раньше.'
    )
    if pack:
        lines.append('\n<b>Ваш пакет</b>\n' + cdn.describe_pack(pack))
    if price > 0:
        lines.append(f'\n<b>Пакет:</b> {gb} ГБ на {days} дн. — <b>{cdn.format_price(price)}</b>')
        lines.append(f'💰 Баланс: {cdn.format_price(int(get_user_balance(key["user_id"]) or 0))}')
        if pack and pack['status'] == 'active':
            lines.append('ℹ️ Новый пакет заменит текущий: остаток объёма и срока не суммируется.')
    rows = []
    if price > 0:
        label = '🔄 Купить новый пакет' if pack else '🛒 Купить пакет'
        rows.append([InlineKeyboardButton(text=f'{label} с баланса — {cdn.format_price(price)}', callback_data=f'key_cdn_buy:{key_id}')])
        rows.extend(_payment_rows(key_id))
        rows.append([InlineKeyboardButton(text='💳 Пополнить баланс', callback_data='balance_topup_menu')])
    rows.append(_back_row(key_id))
    await safe_edit_or_send(callback.message, '\n'.join(lines), reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))


@router.callback_query(F.data.startswith('key_cdn:'))
async def key_cdn_handler(callback: CallbackQuery):
    key_id = int(callback.data.split(':')[1])
    key = await _load_key(callback, key_id)
    if not key:
        return
    await _show_cdn_screen(callback, key)
    await callback.answer()


@router.callback_query(F.data.startswith('key_cdn_buy:'))
async def key_cdn_buy_ask(callback: CallbackQuery):
    from bot.services import cdn
    key_id = int(callback.data.split(':')[1])
    key = await _load_key(callback, key_id)
    if not key:
        return
    price = cdn.get_sale_price_cents()
    if price <= 0:
        await callback.answer('Покупка CDN сейчас недоступна', show_alert=True)
        return
    text = (
        '🌐 <b>Подтвердите покупку</b>\n\n'
        f'Пакет CDN: {cdn.get_cdn_pack_gb()} ГБ на {cdn.get_cdn_pack_days()} дн.\n'
        f'Оплата с баланса: <b>{cdn.format_price(price)}</b>'
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text='✅ Купить', callback_data=f'key_cdn_buy_ok:{key_id}')],
        [InlineKeyboardButton(text='❌ Отмена', callback_data=f'key_cdn:{key_id}')],
    ])
    await safe_edit_or_send(callback.message, text, reply_markup=kb)
    await callback.answer()


@router.callback_query(F.data.startswith('key_cdn_buy_ok:'))
@regular_panel_operation
async def key_cdn_buy_confirm(callback: CallbackQuery):
    from bot.services import cdn
    key_id = int(callback.data.split(':')[1])
    key = await _load_key(callback, key_id)
    if not key:
        return
    await callback.answer('⏳ Оформляю пакет…')
    result = await cdn.purchase_pack(key_id, int(key['user_id']))
    if result.get('ok'):
        notice = (
            f"✅ <b>Пакет CDN подключён:</b> {result['gb']} ГБ на {result['days']} дн.\n"
            'Обновите подписку в приложении, чтобы появился CDN-ключ.'
        )
    elif result.get('error') == 'insufficient_funds':
        notice = f"❌ Недостаточно средств на балансе. Нужно {cdn.format_price(result['need'])}."
    else:
        extra = ' Деньги возвращены на баланс.' if result.get('refunded') else ''
        notice = f"❌ {escape_html(str(result.get('error')))}.{extra}"
    await _show_cdn_screen(callback, key, notice=notice)
