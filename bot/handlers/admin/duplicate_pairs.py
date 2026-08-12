"""
Обработчики решений по найденным потенциальным дубликатам клиентов.
Ботовый клиент (тот, что в БД) НИКОГДА не удаляется — единственное
разрешённое изменение к нему — продление срока действия при объединении.
Удаляется только вручную созданный клиент-дубликат.
"""
import logging
from datetime import datetime, timezone

from aiogram import Router, F
from aiogram.types import CallbackQuery

from bot.utils.admin import is_admin
from bot.utils.text import safe_edit_or_send

logger = logging.getLogger(__name__)
router = Router()


async def _get_manual_client_expiry(server_id: int, manual_email: str):
    """Получает expiry_time (epoch ms) вручную созданного клиента с панели."""
    from bot.services.panel_sync import collect_server_snapshots
    from database.requests import get_all_active_keys_with_server, get_all_servers

    all_keys = get_all_active_keys_with_server()
    all_servers = [s for s in get_all_servers() if int(s['id']) == server_id]
    snapshots = await collect_server_snapshots(all_keys, all_servers)
    snap = snapshots.snapshots.get(server_id)
    if not snap:
        return None
    state = snap.clients.get(manual_email)
    return state.expiry_time if state else None


async def _delete_manual_duplicate(server_id: int, manual_email: str) -> bool:
    """Удаляет вручную созданного клиента-дубликата с панели."""
    from database.requests import get_active_servers
    from bot.services.vpn_api import get_client_from_server_data

    servers = [s for s in get_active_servers() if int(s['id']) == server_id]
    if not servers:
        return False
    client = get_client_from_server_data(servers[0])
    deleted_count = await client.delete_clients_by_email_on_server(manual_email)
    return deleted_count > 0


@router.callback_query(F.data.startswith('admin_duppair_ignore:'))
async def ignore_duplicate_pair(callback: CallbackQuery):
    """Помечает пару как проигнорированную — не дубликат."""
    if not is_admin(callback.from_user.id):
        await callback.answer('⛔ Доступ запрещён', show_alert=True)
        return
    pair_id = int(callback.data.split(':')[1])
    from database.requests import mark_duplicate_pair_ignored
    mark_duplicate_pair_ignored(pair_id)
    await safe_edit_or_send(callback.message, '➡️ Пара помечена как «не дубликат», больше не будет напоминать.')
    await callback.answer()


@router.callback_query(F.data.startswith('admin_duppair_delete:'))
async def delete_duplicate_pair(callback: CallbackQuery):
    """Просто удаляет вручную созданного клиента-дубликата, ботовый не трогает."""
    if not is_admin(callback.from_user.id):
        await callback.answer('⛔ Доступ запрещён', show_alert=True)
        return
    pair_id = int(callback.data.split(':')[1])
    from database.requests import get_duplicate_pair_by_id, mark_duplicate_pair_resolved

    pair = get_duplicate_pair_by_id(pair_id)
    if not pair:
        await callback.answer('⚠️ Запись не найдена', show_alert=True)
        return

    deleted = await _delete_manual_duplicate(pair['server_id'], pair['manual_email'])
    mark_duplicate_pair_resolved(pair_id)

    if deleted:
        logger.info(f"Админ {callback.from_user.id} удалил дубликат-клиента '{pair['manual_email']}' (пара #{pair_id})")
        await safe_edit_or_send(callback.message, f"🗑️ Дубликат <code>{pair['manual_email']}</code> удалён с панели. Ботовый ключ не тронут.")
    else:
        await safe_edit_or_send(callback.message, f"⚠️ Не удалось удалить <code>{pair['manual_email']}</code> с панели (возможно, уже удалён).")
    await callback.answer()


@router.callback_query(F.data.startswith('admin_duppair_merge:'))
async def merge_duplicate_pair(callback: CallbackQuery):
    """Продлевает ботовый ключ на разницу в сроке действия и удаляет дубликата."""
    if not is_admin(callback.from_user.id):
        await callback.answer('⛔ Доступ запрещён', show_alert=True)
        return
    pair_id = int(callback.data.split(':')[1])
    from database.requests import (
        get_duplicate_pair_by_id, mark_duplicate_pair_resolved,
        get_vpn_key_by_server_and_email, extend_vpn_key,
    )

    pair = get_duplicate_pair_by_id(pair_id)
    if not pair:
        await callback.answer('⚠️ Запись не найдена', show_alert=True)
        return

    bot_key = get_vpn_key_by_server_and_email(pair['server_id'], pair['bot_email'])
    if not bot_key:
        await callback.answer('⚠️ Ботовый ключ не найден в базе', show_alert=True)
        return

    manual_expiry_ms = await _get_manual_client_expiry(pair['server_id'], pair['manual_email'])
    days_to_add = 0
    if manual_expiry_ms:
        manual_expiry_dt = datetime.fromtimestamp(manual_expiry_ms / 1000, tz=timezone.utc)
        bot_expiry_dt = datetime.fromisoformat(bot_key['expires_at']).replace(tzinfo=timezone.utc)
        diff_days = (manual_expiry_dt - bot_expiry_dt).days
        if diff_days > 0:
            days_to_add = diff_days

    if days_to_add > 0:
        extend_vpn_key(bot_key['id'], days_to_add)
        try:
            from database.requests import get_active_servers
            from bot.services.vpn_api import get_client_from_server_data
            servers = [s for s in get_active_servers() if int(s['id']) == pair['server_id']]
            if servers:
                panel_client = get_client_from_server_data(servers[0])
                await panel_client.extend_client_expiry(
                    inbound_id=bot_key['panel_inbound_id'],
                    client_uuid=bot_key['client_uuid'],
                    email=bot_key['panel_email'],
                    days=days_to_add,
                )
        except Exception as e:
            logger.warning(f"Не удалось продлить срок ботового клиента на самой панели: {e}")
        extend_note = f"Ботовый ключ продлён на {days_to_add} дн. (у дубликата было больше времени)."
    else:
        extend_note = "У ботового ключа и так больше или равный срок — продление не потребовалось."

    deleted = await _delete_manual_duplicate(pair['server_id'], pair['manual_email'])
    mark_duplicate_pair_resolved(pair_id)

    logger.info(f"Админ {callback.from_user.id} объединил дубликат '{pair['manual_email']}' с ботовым ключом #{bot_key['id']} (+{days_to_add} дн.)")

    status_note = "Дубликат удалён с панели." if deleted else "⚠️ Не удалось удалить дубликат с панели (возможно, уже удалён)."
    await safe_edit_or_send(
        callback.message,
        f"🔗 <b>Объединено</b>\n\n{extend_note}\n{status_note}",
    )
    await callback.answer()
