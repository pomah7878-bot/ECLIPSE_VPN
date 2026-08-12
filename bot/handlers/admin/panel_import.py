"""
Импорт вручную созданных клиентов панели (например, добавленных напрямую
через 3x-ui, минуя бота) в базу бота — привязка к конкретному
Telegram-пользователю без создания нового клиента на панели.
"""
import logging
from datetime import datetime, timezone

from aiogram import Router, F
from aiogram.types import CallbackQuery, Message
from aiogram.fsm.context import FSMContext

from bot.utils.admin import is_admin
from bot.utils.text import safe_edit_or_send, get_message_text_for_storage
from bot.states.admin_states import AdminStates
from bot.keyboards.admin_panel_import import (
    orphan_list_kb,
    orphan_import_cancel_kb,
    orphan_import_confirm_kb,
)

logger = logging.getLogger(__name__)
router = Router()

ADMIN_TARIFF_ID = 1


async def _get_orphan_emails(server_id: int) -> tuple[list, dict]:
    """Возвращает (список email без записи в БД, словарь email -> PanelClientState)."""
    from bot.services.panel_sync import collect_server_snapshots
    from database.requests import get_all_active_keys_with_server, get_all_servers

    all_keys = get_all_active_keys_with_server()
    all_servers = [s for s in get_all_servers() if int(s['id']) == server_id]
    snapshots = await collect_server_snapshots(all_keys, all_servers)

    snap = snapshots.snapshots.get(server_id)
    if not snap:
        return [], {}

    db_emails = {
        (k.get('panel_email') or '').strip().lower()
        for k in all_keys
        if k.get('panel_email') and int(k.get('server_id') or 0) == server_id
    }
    orphan_emails = sorted(e for e in snap.clients.keys() if e not in db_emails)
    return orphan_emails, snap.clients


@router.callback_query(F.data.startswith('admin_import_orphans:'))
async def show_orphan_list(callback: CallbackQuery, state: FSMContext):
    """Показывает список клиентов панели без записи в БД."""
    if not is_admin(callback.from_user.id):
        await callback.answer('⛔ Доступ запрещён', show_alert=True)
        return
    server_id = int(callback.data.split(':')[1])
    await callback.answer('🔍 Проверяю панель...')

    orphan_emails, _ = await _get_orphan_emails(server_id)

    if not orphan_emails:
        await safe_edit_or_send(
            callback.message,
            '✅ Все клиенты этого сервера уже есть в базе бота — нечего импортировать.',
            reply_markup=orphan_list_kb(server_id, []),
        )
        return

    await safe_edit_or_send(
        callback.message,
        f'🔗 <b>Импорт ручных клиентов</b>\n\n'
        f'Найдено {len(orphan_emails)} клиентов на панели без записи в боте. '
        f'Выберите, кого привязать к Telegram-пользователю:',
        reply_markup=orphan_list_kb(server_id, orphan_emails),
    )


@router.callback_query(F.data.startswith('admin_import_orphan_pick:'))
async def pick_orphan_client(callback: CallbackQuery, state: FSMContext):
    """Сохраняет выбранного клиента, запрашивает Telegram ID для привязки."""
    if not is_admin(callback.from_user.id):
        await callback.answer('⛔ Доступ запрещён', show_alert=True)
        return
    _, server_id_str, email = callback.data.split(':', 2)
    server_id = int(server_id_str)

    orphan_emails, clients_map = await _get_orphan_emails(server_id)
    client_state = clients_map.get(email)
    if not client_state:
        await callback.answer('⚠️ Клиент больше не найден на панели', show_alert=True)
        return

    client_raw = client_state.client or {}
    client_uuid = str(client_raw.get('id') or client_raw.get('uuid') or '')
    inbound_id = next(iter(client_state.inbound_ids), None)
    expiry_ms = client_state.expiry_time or 0

    # ВАЖНО: ключи в snapshot.clients нормализованы в нижний регистр, а
    # панель хранит email в оригинальном виде (например, 'Tatik2'). API
    # панели ищет клиента по точному email, поэтому в БД нужно сохранять
    # именно оригинальное написание, иначе последующие обновления
    # клиента будут падать с "record not found".
    real_email = str(client_raw.get('email') or '').strip() or email

    await state.update_data(
        server_id=server_id,
        email=real_email,
        client_uuid=client_uuid,
        inbound_id=inbound_id,
        expiry_ms=expiry_ms,
        traffic_limit=client_state.total_gb * 1024 * 1024 * 1024 if client_state.total_gb else 0,
        sub_id=client_state.sub_id or '',
    )
    await state.set_state(AdminStates.import_orphan_telegram_id)

    await safe_edit_or_send(
        callback.message,
        f'👤 Клиент: <code>{email}</code>\n\n'
        f'Отправьте Telegram ID пользователя, к которому привязать этого клиента.',
        reply_markup=orphan_import_cancel_kb(server_id),
    )
    await callback.answer()


@router.message(AdminStates.import_orphan_telegram_id, F.text, ~F.text.startswith('/'))
async def process_telegram_id_input(message: Message, state: FSMContext):
    """Ищет пользователя по Telegram ID, показывает превью подтверждения."""
    if not is_admin(message.from_user.id):
        return
    text = get_message_text_for_storage(message, 'plain').strip()
    data = await state.get_data()
    server_id = data['server_id']

    if not text.isdigit():
        await safe_edit_or_send(
            message,
            '❌ Telegram ID должен быть числом. Попробуйте ещё раз.',
            reply_markup=orphan_import_cancel_kb(server_id),
            force_new=True,
        )
        return

    from database.requests import get_user_by_telegram_id
    target_user = get_user_by_telegram_id(int(text))
    if not target_user:
        await safe_edit_or_send(
            message,
            f'❌ Пользователь с Telegram ID {text} не найден в боте (должен хотя бы раз запустить бота).',
            reply_markup=orphan_import_cancel_kb(server_id),
            force_new=True,
        )
        return

    await state.update_data(target_user_id=target_user['id'], target_telegram_id=int(text))

    expiry_ms = data.get('expiry_ms') or 0
    if expiry_ms:
        expiry_str = datetime.fromtimestamp(expiry_ms / 1000, tz=timezone.utc).strftime('%d.%m.%Y')
    else:
        expiry_str = 'без ограничения'
    traffic_gb = (data.get('traffic_limit') or 0) / (1024**3)
    traffic_str = f'{traffic_gb:.0f} ГБ' if traffic_gb else 'безлимит'

    await safe_edit_or_send(
        message,
        f'👀 <b>Подтверждение импорта</b>\n\n'
        f'Клиент: <code>{data["email"]}</code>\n'
        f'Привязать к: Telegram ID {text} ({target_user.get("username") or "без username"})\n'
        f'Истекает: {expiry_str}\n'
        f'Лимит трафика: {traffic_str}\n\n'
        f'Тариф будет установлен как "Admin Tariff" (можно сменить позже через карточку ключа).',
        reply_markup=orphan_import_confirm_kb(server_id),
        force_new=True,
    )


@router.callback_query(F.data == 'admin_import_orphan_confirm')
async def confirm_orphan_import(callback: CallbackQuery, state: FSMContext):
    """Создаёт запись vpn_keys, привязывающую клиента к пользователю."""
    if not is_admin(callback.from_user.id):
        await callback.answer('⛔ Доступ запрещён', show_alert=True)
        return
    data = await state.get_data()
    from database.requests import create_vpn_key_from_panel_import, vpn_key_exists_for_panel_email

    # Защита от гонки состояний: клиент мог быть импортирован уже ПОСЛЕ
    # того, как этот процесс начался (двойное нажатие, второй админ и т.д.)
    if vpn_key_exists_for_panel_email(data['server_id'], data['email']):
        await state.clear()
        await callback.answer(
            f"⚠️ Клиент {data['email']} уже был импортирован ранее — пропускаю, чтобы не создавать дубликат.",
            show_alert=True,
        )
        await safe_edit_or_send(
            callback.message,
            f'⚠️ Клиент <code>{data["email"]}</code> уже есть в базе бота (импортирован ранее). '
            f'Новая запись не создана.',
            reply_markup=orphan_import_cancel_kb(data['server_id']),
        )
        return

    expiry_ms = data.get('expiry_ms') or 0
    if expiry_ms:
        expires_at = datetime.fromtimestamp(expiry_ms / 1000, tz=timezone.utc).strftime('%Y-%m-%d %H:%M:%S')
    else:
        expires_at = datetime(2099, 1, 1).strftime('%Y-%m-%d %H:%M:%S')

    key_id = create_vpn_key_from_panel_import(
        user_id=data['target_user_id'],
        server_id=data['server_id'],
        tariff_id=ADMIN_TARIFF_ID,
        panel_inbound_id=data.get('inbound_id'),
        client_uuid=data.get('client_uuid', ''),
        panel_email=data['email'],
        expires_at=expires_at,
        traffic_limit=data.get('traffic_limit') or 0,
        sub_id=data.get('sub_id', ''),
        custom_name=data['email'],
    )
    await state.clear()
    logger.info(f"Админ {callback.from_user.id} импортировал клиента '{data['email']}' как ключ #{key_id} для пользователя {data['target_telegram_id']}")

    await safe_edit_or_send(
        callback.message,
        f'✅ Готово! Клиент <code>{data["email"]}</code> привязан к Telegram ID {data["target_telegram_id"]} как ключ #{key_id}.',
        reply_markup=orphan_import_cancel_kb(data['server_id']),
    )
    await callback.answer()
