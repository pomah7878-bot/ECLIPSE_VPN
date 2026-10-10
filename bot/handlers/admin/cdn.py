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
        'Режим «Доп. функция»: сколько клиент платит за один пакет.\n'
        'Режим «В тарифах»: доплата, которая прибавляется к цене каждого платного тарифа выбранных групп.\n'
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


def _group_names(ids) -> str:
    from database.db_groups import get_all_groups
    return ', '.join(g['name'] for g in get_all_groups() if g['id'] in set(ids))


def _settings_text() -> str:
    from bot.services import cdn
    ids = ', '.join(str(i) for i in sorted(cdn.get_cdn_inbound_ids())) or 'не задан'
    price = cdn.get_cdn_price_cents()
    price_text = cdn.format_price(price) if price > 0 else 'не задана'
    ready_ids = '✅' if cdn.get_cdn_inbound_ids() else '❌'
    ready_price = '✅' if price > 0 else '❌'
    mode = cdn.get_cdn_mode()
    if mode == cdn.MODE_OFF:
        status = ('⏸ <b>выключен</b>: в тарифах CDN нет, платный пакет не продаётся, пакеты из тарифов '
                  'не выдаются. Действующие пакеты клиентов работают до конца срока.')
    elif mode == cdn.MODE_TARIFFS:
        names = _group_names(cdn.get_cdn_group_ids())
        status = ('✅ <b>CDN входит в тарифы</b>. Группы: ' + (escape_html(names) if names else '<b>не выбраны</b>') +
                  '.\nЦена CDN прибавляется к цене каждого платного тарифа этих групп, клиент получает '
                  f'{cdn.get_cdn_pack_gb()} ГБ на срок тарифа. Отдельная кнопка покупки пакета скрыта.')
    else:
        status = ('✅ <b>доп. функция</b>: клиент покупает пакет отдельно кнопкой «🌐 CDN» в карточке ключа. '
                  'В тарифах CDN нет.')
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
        '<b>Режимы</b> не пересекаются: «Доп. функция» — платный пакет отдельной кнопкой; «В тарифах» — '
        'CDN входит в тарифы выбранных групп (цена и объём берутся из настроек выше), кнопка покупки скрыта.\n'
        'В режиме «Доп. функция» клиенты видят кнопку «🌐 CDN» в карточке ключа, когда отмечены оба пункта ✅.\n\n'
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
    mode = cdn.get_cdn_mode()
    mode_rows = [[InlineKeyboardButton(text=f"{'🟢' if mode == m else '⚪'} {title}", callback_data=f'admin_cdn_mode:{m}')]
                 for m, title in ((cdn.MODE_OFF, 'Выключен'), (cdn.MODE_ADDON, 'Доп. функция (платный пакет)'),
                                  (cdn.MODE_TARIFFS, 'В тарифах группы'))]
    if mode == cdn.MODE_TARIFFS:
        chosen = set(cdn.get_cdn_group_ids())
        from database.db_groups import get_all_groups
        for g in get_all_groups():
            mode_rows.append([InlineKeyboardButton(
                text=f"{'🟢' if g['id'] in chosen else '⚪'} Группа: {g['name']}",
                callback_data=f"admin_cdn_group:{g['id']}")])
    rows[0:0] = mode_rows
    rows.append([InlineKeyboardButton(text='📊 CDN у провайдера: трафик и расход', callback_data='admin_cdn_provider')])
    rows.append([InlineKeyboardButton(text='🔍 Проверить инбаунд на серверах', callback_data='admin_cdn_check')])
    rows.append([
        InlineKeyboardButton(text='⬅️ Назад', callback_data='admin_tariffs'),
        InlineKeyboardButton(text='🈴 На главную', callback_data='start'),
    ])
    return InlineKeyboardMarkup(inline_keyboard=rows)


@router.message(Command('cdn'))
async def cdn_settings_command(message: Message, state: FSMContext):
    await state.clear()
    await message.answer(_settings_text(), reply_markup=_settings_kb(), parse_mode='HTML')


@router.callback_query(F.data.startswith('admin_cdn_mode:'))
async def cdn_set_mode(callback: CallbackQuery, state: FSMContext):
    from bot.services import cdn
    mode = callback.data.split(':')[1]
    if mode not in (cdn.MODE_OFF, cdn.MODE_ADDON, cdn.MODE_TARIFFS):
        await callback.answer()
        return
    cdn.set_cdn_setting('cdn_mode', mode)
    cdn.set_cdn_setting('cdn_enabled', '0' if mode == cdn.MODE_OFF else '1')
    note = ''
    if mode == cdn.MODE_TARIFFS and not cdn.get_cdn_group_ids():
        note = ' Отметьте группы тарифов ниже.'
    await callback.answer({'off': 'CDN выключен', 'addon': 'CDN: доп. функция',
                           'tariffs': 'CDN входит в тарифы.' + note}[mode], show_alert=bool(note))
    await safe_edit_or_send(callback.message, _settings_text(), reply_markup=_settings_kb())


@router.callback_query(F.data.startswith('admin_cdn_group:'))
async def cdn_toggle_group(callback: CallbackQuery, state: FSMContext):
    from bot.services import cdn
    cdn.toggle_cdn_group(int(callback.data.split(':')[1]))
    await callback.answer()
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


# ------------------------------------------------ CDN у провайдера (Yandex Cloud) --

PROVIDER_FIELDS = {
    'key': ('yc_api_key', '🔑 Ключ API',
            'API-ключ сервисного аккаунта Yandex Cloud с ролью <code>monitoring.viewer</code> '
            '(только чтение статистики). Сообщение с ключом бот сразу удалит.'),
    'folder': ('yc_folder_id', '📁 ID каталога',
               'ID каталога Yandex Cloud, где работает CDN (в консоли виден в шапке под названием каталога).'),
    'price': ('yc_price_per_gb_cents', '💵 Цена за ГБ, ₽',
              'Сколько вы платите провайдеру за 1 ГБ исходящего трафика CDN (по тарифу Yandex Cloud). '
              'Нужна только для оценки расхода. Пример: <code>2.5</code>'),
}


def _mask(value: str) -> str:
    return f'{value[:4]}…{value[-3:]}' if len(value) > 10 else ('задан' if value else 'не задан')


def _provider_kb() -> InlineKeyboardMarkup:
    from bot.services import yc_cdn
    price = yc_cdn.get_price_per_gb_cents()
    rows = []
    if yc_cdn.is_configured():
        rows.append([InlineKeyboardButton(text='🔄 Обновить', callback_data='admin_cdn_provider_refresh')])
    rows.append([InlineKeyboardButton(text=f'🔑 Ключ API: {_mask(yc_cdn.get_api_key())}', callback_data='admin_cdn_pset:key')])
    rows.append([InlineKeyboardButton(text=f'📁 ID каталога: {yc_cdn.get_folder_id() or "не задан"}', callback_data='admin_cdn_pset:folder')])
    rows.append([InlineKeyboardButton(
        text=f'💵 Цена за ГБ: {str(price / 100).rstrip("0").rstrip(".").replace(".", ",") + " ₽" if price else "не задана"}',
        callback_data='admin_cdn_pset:price')])
    if yc_cdn.get_api_key():
        rows.append([InlineKeyboardButton(text='🗑 Удалить ключ API', callback_data='admin_cdn_pclear')])
    rows.append([InlineKeyboardButton(text='⬅️ К настройкам CDN', callback_data='admin_cdn_settings')])
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def _provider_text(force: bool = False) -> str:
    from bot.services import yc_cdn
    from database import db_cdn
    head = '📊 <b>CDN у провайдера</b>\n\n'
    if not yc_cdn.is_configured():
        return head + (
            'Здесь будет трафик и расход CDN по данным Yandex Cloud.\n\n'
            '<b>Как подключить</b>\n'
            '1. В консоли Yandex Cloud создайте сервисный аккаунт и дайте ему роль <code>monitoring.viewer</code>.\n'
            '2. Создайте для него API-ключ и вставьте ниже («Ключ API»).\n'
            '3. Укажите ID каталога и цену за ГБ.\n\n'
            'Ключ даёт только чтение статистики.'
        )
    summary = await yc_cdn.fetch_summary(force=force)
    return head + yc_cdn.format_summary(summary, db_cdn.list_packs())


@router.callback_query(F.data.in_({'admin_cdn_provider', 'admin_cdn_provider_refresh'}))
async def cdn_provider_screen(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    await callback.answer('⏳ Запрашиваю данные…')
    text = await _provider_text(force=callback.data.endswith('refresh'))
    await safe_edit_or_send(callback.message, text, reply_markup=_provider_kb())


@router.callback_query(F.data.startswith('admin_cdn_pset:'))
async def cdn_provider_edit(callback: CallbackQuery, state: FSMContext):
    field = callback.data.split(':')[1]
    if field not in PROVIDER_FIELDS:
        await callback.answer('Неизвестная настройка', show_alert=True)
        return
    _, title, help_text = PROVIDER_FIELDS[field]
    await state.set_state(AdminStates.cdn_provider_value)
    await state.update_data(cdn_pfield=field)
    await safe_edit_or_send(
        callback.message, f'<b>{title}</b>\n\n{help_text}\n\nОтправьте значение сообщением.',
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text='❌ Отмена', callback_data='admin_cdn_provider')]]),
    )
    await callback.answer()


@router.message(AdminStates.cdn_provider_value, F.text, ~F.text.startswith('/'))
async def cdn_provider_save(message: Message, state: FSMContext):
    from bot.services import yc_cdn
    data = await state.get_data()
    field = data.get('cdn_pfield')
    if field not in PROVIDER_FIELDS:
        await state.clear()
        return
    raw = ''.join((message.text or '').split())
    setting = PROVIDER_FIELDS[field][0]
    if field == 'key':
        try:  # ключ нельзя оставлять в переписке
            await message.delete()
        except Exception:  # noqa: BLE001
            pass
    if field == 'price':
        try:
            value = str(int(round(float(raw.replace(',', '.')) * 100)))
            if int(value) < 0:
                raise ValueError
        except ValueError:
            await message.answer('❌ Введите цену числом, например 2.5')
            return
    else:
        value = raw
        if len(value) < 8:
            await message.answer('❌ Значение слишком короткое, проверьте и отправьте ещё раз')
            return
    yc_cdn.set_setting_value(setting, value)
    await state.clear()
    text = await _provider_text(force=True)
    await message.answer('✅ Сохранено.\n\n' + text, reply_markup=_provider_kb(), parse_mode='HTML')


@router.callback_query(F.data == 'admin_cdn_pclear')
async def cdn_provider_clear(callback: CallbackQuery, state: FSMContext):
    from bot.services import yc_cdn
    yc_cdn.set_setting_value('yc_api_key', '')
    await callback.answer('Ключ удалён')
    await safe_edit_or_send(callback.message, await _provider_text(), reply_markup=_provider_kb())

