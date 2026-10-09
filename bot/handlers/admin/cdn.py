"""Админ: CDN-пакеты ключей и настройки CDN (/cdn)."""
import logging

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from bot.services.panel_sync_coordinator import regular_panel_operation
from bot.states.admin_states import AdminStates
from bot.utils.admin import is_admin as _is_admin_guard
from bot.utils.text import escape_html, safe_edit_or_send

logger = logging.getLogger(__name__)

router = Router()
router.callback_query.filter(lambda c: _is_admin_guard(c.from_user.id))
router.message.filter(lambda m: m.from_user is not None and _is_admin_guard(m.from_user.id))

SETTING_FIELDS = {
    'ids': ('cdn_inbound_ids', 'ID CDN-инбаунда (через запятую, например 301)'),
    'price': ('cdn_price_cents', 'Цена пакета в рублях (0 — покупка выключена)'),
    'gb': ('cdn_pack_gb', 'Объём пакета по умолчанию, ГБ'),
    'days': ('cdn_pack_days', 'Срок пакета по умолчанию, дней'),
}


def _settings_text() -> str:
    from bot.services import cdn
    ids = ', '.join(str(i) for i in sorted(cdn.get_cdn_inbound_ids())) or 'не задан'
    price = cdn.get_cdn_price_cents()
    price_text = cdn.format_price(price) if price > 0 else 'не задана (покупка выключена)'
    return (
        '🌐 <b>Настройки CDN-пакетов</b>\n\n'
        f'• CDN-инбаунд (ID): <b>{escape_html(ids)}</b>\n'
        f'• Цена пакета: <b>{price_text}</b>\n'
        f'• Объём по умолчанию: <b>{cdn.get_cdn_pack_gb()} ГБ</b>\n'
        f'• Срок по умолчанию: <b>{cdn.get_cdn_pack_days()} дн.</b>\n\n'
        'Клиенты видят кнопку «🌐 CDN» в карточке ключа, когда задан ID инбаунда и цена. '
        'Инбаунд можно также пометить в панели меткой <code>[CDN]</code> в начале названия. '
        'Выдать пакет бесплатно или изменить объём: карточка ключа → «🌐 CDN-пакет».'
    )


def _settings_kb() -> InlineKeyboardMarkup:
    rows = [[InlineKeyboardButton(text=f'✏️ {label}', callback_data=f'admin_cdn_set:{field}')]
            for field, (_, label) in SETTING_FIELDS.items()]
    rows.append([InlineKeyboardButton(text='🈴 На главную', callback_data='start')])
    return InlineKeyboardMarkup(inline_keyboard=rows)


@router.message(Command('cdn'))
async def cdn_settings_command(message: Message, state: FSMContext):
    await state.clear()
    await message.answer(_settings_text(), reply_markup=_settings_kb(), parse_mode='HTML')


@router.callback_query(F.data == 'admin_cdn_settings')
async def cdn_settings_callback(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    await safe_edit_or_send(callback.message, _settings_text(), reply_markup=_settings_kb())
    await callback.answer()


@router.callback_query(F.data.startswith('admin_cdn_set:'))
async def cdn_setting_edit(callback: CallbackQuery, state: FSMContext):
    field = callback.data.split(':')[1]
    if field not in SETTING_FIELDS:
        await callback.answer('Неизвестная настройка', show_alert=True)
        return
    await state.set_state(AdminStates.cdn_setting_value)
    await state.update_data(cdn_field=field)
    await safe_edit_or_send(
        callback.message,
        f'✏️ <b>{SETTING_FIELDS[field][1]}</b>\n\nОтправьте новое значение сообщением.',
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text='❌ Отмена', callback_data='admin_cdn_settings')]]),
    )
    await callback.answer()


@router.message(AdminStates.cdn_setting_value, F.text, ~F.text.startswith('/'))
async def cdn_setting_save(message: Message, state: FSMContext):
    from bot.services import cdn
    data = await state.get_data()
    field = data.get('cdn_field')
    if field not in SETTING_FIELDS:
        await state.clear()
        return
    raw = (message.text or '').strip().replace(',', '.') if field == 'price' else (message.text or '').strip()
    key = SETTING_FIELDS[field][0]
    try:
        if field == 'ids':
            parts = [p.strip() for p in raw.replace(';', ',').split(',') if p.strip()]
            if not all(p.isdigit() for p in parts):
                raise ValueError('ID — только числа через запятую')
            value = ','.join(str(int(p)) for p in parts)
        elif field == 'price':
            value = str(int(round(float(raw) * 100)))
            if int(value) < 0:
                raise ValueError('Цена не может быть отрицательной')
        else:
            value = str(int(raw))
            if int(value) <= 0:
                raise ValueError('Значение должно быть больше нуля')
    except ValueError as e:
        msg = str(e)
        if not msg or 'literal' in msg or 'convert' in msg:
            msg = 'Введите корректное число'
        await message.answer(f'❌ {escape_html(msg)}')
        return
    cdn.set_cdn_setting(key, value)
    await state.clear()
    await message.answer('✅ Сохранено.\n\n' + _settings_text(), reply_markup=_settings_kb(), parse_mode='HTML')


# ---------------------------------------------------------------- per key --

def _key_cdn_kb(key_id: int, has_pack: bool) -> InlineKeyboardMarkup:
    rows = [[InlineKeyboardButton(text='🎁 Выдать пакет бесплатно (по умолчанию)', callback_data=f'admin_cdn_grant:{key_id}')],
            [InlineKeyboardButton(text='📦 Задать объём (ГБ)', callback_data=f'admin_cdn_vol:{key_id}')]]
    if has_pack:
        rows.append([InlineKeyboardButton(text='⛔ Отключить CDN', callback_data=f'admin_cdn_off:{key_id}')])
    rows.append([InlineKeyboardButton(text='⚙️ Настройки CDN', callback_data='admin_cdn_settings')])
    rows.append([InlineKeyboardButton(text='⬅️ Назад к ключу', callback_data=f'admin_key_view:{key_id}')])
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def _show_key_cdn(callback_or_message, key_id: int, notice: str = '') -> None:
    from bot.services import cdn
    from database import db_cdn
    pack = db_cdn.get_pack(key_id)
    text = (notice + '\n\n' if notice else '') + f'🌐 <b>CDN-пакет ключа #{key_id}</b>\n\n' + cdn.describe_pack(pack)
    kb = _key_cdn_kb(key_id, bool(pack))
    if isinstance(callback_or_message, CallbackQuery):
        await safe_edit_or_send(callback_or_message.message, text, reply_markup=kb)
    else:
        await callback_or_message.answer(text, reply_markup=kb, parse_mode='HTML')


@router.callback_query(F.data.startswith('admin_key_cdn:'))
async def admin_key_cdn(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    await _show_key_cdn(callback, int(callback.data.split(':')[1]))
    await callback.answer()


@router.callback_query(F.data.startswith('admin_cdn_grant:'))
@regular_panel_operation
async def admin_cdn_grant(callback: CallbackQuery):
    from bot.services import cdn
    key_id = int(callback.data.split(':')[1])
    await callback.answer('⏳ Выдаю…')
    result = await cdn.activate_pack(key_id, is_free=True)
    notice = (f"✅ Пакет выдан: {result['gb']} ГБ на {result['days']} дн."
              if result['ok'] else f"❌ {escape_html(str(result['error']))}")
    await _show_key_cdn(callback, key_id, notice)


@router.callback_query(F.data.startswith('admin_cdn_off:'))
@regular_panel_operation
async def admin_cdn_off(callback: CallbackQuery):
    from bot.services import cdn
    key_id = int(callback.data.split(':')[1])
    await callback.answer('⏳ Отключаю…')
    result = await cdn.deactivate_pack(key_id)
    notice = '✅ CDN отключён' if result['ok'] else f"❌ {escape_html(str(result.get('error')))}"
    await _show_key_cdn(callback, key_id, notice)


@router.callback_query(F.data.startswith('admin_cdn_vol:'))
async def admin_cdn_volume_start(callback: CallbackQuery, state: FSMContext):
    key_id = int(callback.data.split(':')[1])
    await state.set_state(AdminStates.cdn_key_volume)
    await state.update_data(cdn_key_id=key_id)
    await safe_edit_or_send(
        callback.message,
        '📦 <b>Объём CDN-пакета, ГБ</b>\n\nОтправьте целое число. Если пакета нет, он будет выдан '
        'бесплатно на срок по умолчанию. Если пакет есть, срок сохранится, счётчик начнётся заново '
        'на указанный объём.',
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text='❌ Отмена', callback_data=f'admin_key_cdn:{key_id}')]]),
    )
    await callback.answer()


@router.message(AdminStates.cdn_key_volume, F.text, ~F.text.startswith('/'))
@regular_panel_operation
async def admin_cdn_volume_save(message: Message, state: FSMContext):
    from bot.services import cdn
    from database import db_cdn
    data = await state.get_data()
    key_id = int(data.get('cdn_key_id') or 0)
    text = (message.text or '').strip()
    if not key_id or not text.isdigit() or int(text) <= 0:
        await message.answer('❌ Введите целое число ГБ больше нуля')
        return
    gb = int(text)
    pack = db_cdn.get_pack(key_id)
    if pack and pack['status'] in (db_cdn.STATUS_ACTIVE, db_cdn.STATUS_SUSPENDED):
        result = await cdn.set_pack_volume(key_id, gb)
    else:
        result = await cdn.activate_pack(key_id, gb=gb, is_free=True)
    await state.clear()
    notice = f'✅ Объём пакета: {gb} ГБ' if result['ok'] else f"❌ {escape_html(str(result['error']))}"
    await _show_key_cdn(message, key_id, notice)
