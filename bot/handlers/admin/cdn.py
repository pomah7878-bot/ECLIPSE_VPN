"""Админ: CDN-пакеты ключей и настройки CDN (⚙️ Настройки бота → 🌐 CDN, а также /cdn)."""
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

# поле -> (ключ настройки, название, подробное объяснение, пример)
SETTING_FIELDS = {
    'ids': (
        'cdn_inbound_ids', 'ID CDN-инбаунда',
        'Номер инбаунда в панели 3x-ui, через который идёт трафик клиентов через CDN.\n'
        'Где взять: панель 3x-ui → Inbounds, число в колонке ID.\n'
        'Можно несколько через запятую. Бот перестаёт добавлять в этот инбаунд обычных '
        'клиентов: туда попадают только те, у кого есть CDN-пакет.\n'
        'Номера инбаундов у разных серверов могут совпадать. Если у вас несколько серверов, '
        'удобнее написать в панели в названии инбаунда слово <code>CDN</code> (например «DE CDN»): '
        'тогда бот узнает его сам на любом сервере.',
        '301',
    ),
    'price': (
        'cdn_price_cents', 'Цена пакета, ₽',
        'Сколько клиент платит с баланса бота за один пакет.\n'
        'Считайте от своих расходов: CDN тарифицируется по исходящему трафику '
        '(цена за ГБ у Yandex × объём пакета), плюс ваша наценка.\n'
        '0 — покупка выключена, кнопка клиентам не показывается (админ по-прежнему '
        'может выдавать пакеты бесплатно).',
        '150',
    ),
    'gb': (
        'cdn_pack_gb', 'Объём пакета, ГБ',
        'Сколько трафика получает клиент в одном пакете. Когда объём выбран, CDN-ключ '
        'отключается, а обычные ключи подписки работают как раньше.\n'
        'Для отдельного клиента админ может задать другой объём в карточке его ключа.',
        '10',
    ),
    'days': (
        'cdn_pack_days', 'Срок пакета, дней',
        'Через сколько дней пакет заканчивается, даже если объём не выбран. '
        'Новая покупка заменяет текущий пакет: остаток объёма и срока не суммируется.',
        '30',
    ),
}


def _stats_line() -> str:
    from database import db_cdn
    packs = db_cdn.list_packs()
    if not packs:
        return 'Пакетов пока нет.'
    active = sum(1 for p in packs if p['status'] == db_cdn.STATUS_ACTIVE)
    free = sum(1 for p in packs if p['status'] == db_cdn.STATUS_ACTIVE and p['is_free'])
    return f'Пакетов всего: {len(packs)}, активных: {active} (из них бесплатных: {free}).'


def _settings_text() -> str:
    from bot.services import cdn
    ids = ', '.join(str(i) for i in sorted(cdn.get_cdn_inbound_ids())) or 'не задан'
    price = cdn.get_cdn_price_cents()
    price_text = cdn.format_price(price) if price > 0 else 'не задана'
    ready_ids = '✅' if cdn.get_cdn_inbound_ids() else '❌'
    ready_price = '✅' if price > 0 else '❌'
    enabled = cdn.is_cdn_enabled()
    status = ('✅ <b>включён</b>' if enabled else
              '⏸ <b>выключен</b>: тарифы без CDN, продаж нет, пакеты из тарифов не выдаются. '
              'Настройки тарифов сохранены, действующие пакеты клиентов работают до конца срока.')
    return (
        f'🌐 <b>CDN-пакеты (обход белых списков)</b>\n\nСтатус: {status}\n\n'
        '<b>Что это.</b> Дополнительный ключ в подписке клиента, который идёт через CDN. '
        'Он нужен там, где мобильный интернет пропускает только «белые» адреса. '
        'Клиент сам решает, нужен ли ему CDN, и покупает пакет с баланса бота.\n\n'
        '<b>Как работает.</b> Покупка или выдача создаёт отдельного клиента в CDN-инбаунде '
        'панели с лимитом ГБ и сроком. Закончился объём или срок: CDN-ключ отключается, '
        'остальные ключи подписки не затрагиваются. CDN платный для вас, поэтому в обычную '
        'подписку он по умолчанию не попадает.\n\n'
        '<b>Настройки</b>\n'
        f'{ready_ids} CDN-инбаунд (ID): <b>{escape_html(ids)}</b>\n'
        f'{ready_price} Цена пакета: <b>{price_text}</b>\n'
        f'• Объём пакета: <b>{cdn.get_cdn_pack_gb()} ГБ</b>\n'
        f'• Срок пакета: <b>{cdn.get_cdn_pack_days()} дн.</b>\n\n'
        'Клиенты видят кнопку «🌐 CDN» в карточке ключа, когда отмечены оба пункта ✅.\n\n'
        '<b>Выдать бесплатно или изменить объём</b> для одного клиента: '
        'Пользователи → ключ → «🌐 CDN-пакет».\n\n'
        f'📊 {_stats_line()}'
    )


def _settings_kb() -> InlineKeyboardMarkup:
    from bot.services import cdn
    values = {
        'ids': ', '.join(str(i) for i in sorted(cdn.get_cdn_inbound_ids())) or 'не задан',
        'price': cdn.format_price(cdn.get_cdn_price_cents()) if cdn.get_cdn_price_cents() > 0 else 'не задана',
        'gb': f'{cdn.get_cdn_pack_gb()} ГБ',
        'days': f'{cdn.get_cdn_pack_days()} дн.',
    }
    rows = [[InlineKeyboardButton(text=f'✏️ {meta[1]}: {values[field]}', callback_data=f'admin_cdn_set:{field}')]
            for field, meta in SETTING_FIELDS.items()]
    toggle_text = '⏸ Выключить CDN' if cdn.is_cdn_enabled() else '▶️ Включить CDN'
    rows.insert(0, [InlineKeyboardButton(text=toggle_text, callback_data='admin_cdn_toggle')])
    rows.append([InlineKeyboardButton(text='🔍 Проверить инбаунд на серверах', callback_data='admin_cdn_check')])
    rows.append([
        InlineKeyboardButton(text='⬅️ Назад', callback_data='admin_bot_settings'),
        InlineKeyboardButton(text='🈴 На главную', callback_data='start'),
    ])
    return InlineKeyboardMarkup(inline_keyboard=rows)


@router.message(Command('cdn'))
async def cdn_settings_command(message: Message, state: FSMContext):
    await state.clear()
    await message.answer(_settings_text(), reply_markup=_settings_kb(), parse_mode='HTML')


@router.callback_query(F.data == 'admin_cdn_toggle')
async def cdn_toggle(callback: CallbackQuery, state: FSMContext):
    from bot.services import cdn
    cdn.set_cdn_setting('cdn_enabled', '0' if cdn.is_cdn_enabled() else '1')
    await callback.answer('CDN включён' if cdn.is_cdn_enabled() else 'CDN выключен')
    await safe_edit_or_send(callback.message, _settings_text(), reply_markup=_settings_kb())


@router.callback_query(F.data == 'admin_cdn_settings')
async def cdn_settings_callback(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    await safe_edit_or_send(callback.message, _settings_text(), reply_markup=_settings_kb())
    await callback.answer()


@router.callback_query(F.data == 'admin_cdn_check')
async def cdn_check_servers(callback: CallbackQuery):
    """Показывает, на каких серверах бот нашёл CDN-инбаунд (чтобы проверить ID/метку)."""
    from bot.services import cdn
    from database.requests import get_active_servers
    await callback.answer('⏳ Проверяю серверы…')
    lines = ['🔍 <b>Проверка CDN-инбаунда</b>\n']
    for server in get_active_servers():
        name = escape_html(str(server.get('name') or server.get('id')))
        try:
            _, _, found = await cdn._server_context(int(server['id']))
            if found:
                items = ', '.join(f"#{i.get('id')} «{escape_html(str(i.get('remark') or ''))}»" for i in found)
                lines.append(f'✅ {name}: {items}')
            else:
                lines.append(f'⚠️ {name}: CDN-инбаунд не найден (проверьте ID или слово CDN в названии)')
        except Exception as e:  # noqa: BLE001
            lines.append(f'❌ {name}: нет связи с панелью ({escape_html(str(e)[:80])})')
    await safe_edit_or_send(
        callback.message, '\n'.join(lines),
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text='⬅️ К настройкам CDN', callback_data='admin_cdn_settings')]]),
    )


@router.callback_query(F.data.startswith('admin_cdn_set:'))
async def cdn_setting_edit(callback: CallbackQuery, state: FSMContext):
    field = callback.data.split(':')[1]
    if field not in SETTING_FIELDS:
        await callback.answer('Неизвестная настройка', show_alert=True)
        return
    _, title, help_text, example = SETTING_FIELDS[field]
    await state.set_state(AdminStates.cdn_setting_value)
    await state.update_data(cdn_field=field)
    await safe_edit_or_send(
        callback.message,
        f'✏️ <b>{title}</b>\n\n{help_text}\n\nПример: <code>{example}</code>\n\n'
        'Отправьте новое значение сообщением.',
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


# ---------------------------------------------------- CDN, входящий в тариф --

@router.callback_query(F.data.startswith('admin_tariff_cdn:'))
async def admin_tariff_cdn_start(callback: CallbackQuery, state: FSMContext):
    from bot.services import cdn
    tariff_id = int(callback.data.split(':')[1])
    await state.set_state(AdminStates.cdn_tariff_gb)
    await state.update_data(cdn_tariff_id=tariff_id)
    current = cdn.get_tariff_cdn_gb(tariff_id)
    await safe_edit_or_send(
        callback.message,
        '🌐 <b>CDN в тарифе</b>\n\n'
        'Сколько ГБ CDN получает клиент вместе с этим тарифом. Пакет подключается сам при покупке '
        'и при каждом продлении, на срок тарифа.\n'
        f'Сейчас: <b>{f"{current} ГБ" if current else "не входит"}</b>\n\n'
        'Отправьте целое число ГБ, либо <code>0</code>, чтобы убрать CDN из тарифа. '
        'Отдельная покупка пакета остаётся доступной.',
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text='❌ Отмена', callback_data=f'admin_tariff_view:{tariff_id}')]]),
    )
    await callback.answer()


@router.message(AdminStates.cdn_tariff_gb, F.text, ~F.text.startswith('/'))
async def admin_tariff_cdn_save(message: Message, state: FSMContext):
    from bot.services import cdn
    data = await state.get_data()
    tariff_id = int(data.get('cdn_tariff_id') or 0)
    text = (message.text or '').strip()
    if not tariff_id or not text.isdigit():
        await message.answer('❌ Введите целое число ГБ (0 — убрать CDN из тарифа)')
        return
    cdn.set_tariff_cdn_gb(tariff_id, int(text))
    await state.clear()
    from bot.handlers.admin.tariffs import render_tariff_view
    await render_tariff_view(message, tariff_id, state)
