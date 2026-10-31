"""Автоматическая настройка нового ключа после оплаты (без выбора сервера клиентом).

Клиент не должен выбирать сервер: система сама берёт наименее загруженный
сервер из доступных для тарифа. Сменить подписку клиент может позже сам.
"""
from bot.utils.inbounds import filter_inbounds_for_tariff_id
import asyncio
import logging
import uuid as _uuid
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

_CONFIG_LOCKS: Dict[int, asyncio.Lock] = {}


def pick_server(servers: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """Наименее загруженный сервер (по числу выданных ключей); при равенстве — первый."""
    if not servers:
        return None
    if len(servers) == 1:
        return servers[0]
    try:
        from database.connection import get_db
        with get_db() as conn:
            rows = conn.execute(
                "SELECT server_id, COUNT(*) FROM vpn_keys WHERE server_id IS NOT NULL GROUP BY server_id"
            ).fetchall()
        load = {int(r[0]): int(r[1]) for r in rows}
        return min(servers, key=lambda s: load.get(int(s['id']), 0))
    except Exception as e:
        logger.warning("pick_server: не удалось оценить нагрузку, берём первый сервер: %s", e)
        return servers[0]


async def configure_draft_key(key_id: int, order_id: str) -> bool:
    """Назначает сервер и создаёт клиентов в панели для черновика ключа.

    Возвращает True, если ключ настроен (или уже был настроен). False — не удалось,
    черновик остаётся, клиента можно довести вручную в боте.
    """
    from database.requests import (
        find_order_by_order_id, get_tariff_by_id, get_user_by_id,
        update_vpn_key_config, update_payment_key_id, get_active_servers,
    )
    from database.connection import get_db
    from bot.services.vpn_api import (
        get_client, get_client_subscription_inbounds, is_subscription_mode,
        sync_key_to_panel_state,
    )
    from bot.handlers.admin.users_keys import generate_unique_email
    from bot.utils.groups import get_servers_for_key

    if not is_subscription_mode():
        return False  # в режиме выбора протокола нужен диалог — оставляем как раньше

    lock = _CONFIG_LOCKS.setdefault(int(key_id), asyncio.Lock())
    try:
        async with lock:
            with get_db() as conn:
                row = conn.execute("SELECT server_id FROM vpn_keys WHERE id = ?", (key_id,)).fetchone()
            if row is None:
                return False
            if row[0]:
                return True  # уже настроен другим запросом

            order = find_order_by_order_id(order_id)
            if not order:
                return False
            tariff = get_tariff_by_id(order['tariff_id'])
            if not tariff:
                return False
            owner = get_user_by_id(order['user_id'])
            if not owner:
                return False

            servers = get_servers_for_key(order['tariff_id']) or get_active_servers()
            server = pick_server(servers)
            if not server:
                logger.error("auto_provision: нет активных серверов (ключ %s)", key_id)
                return False
            server_id = server['id']

            days = order.get('period_days') or order.get('duration_days') or tariff.get('duration_days') or 30
            limit_gb = tariff.get('traffic_limit_gb', 0) or 0
            max_ips = tariff.get('max_ips', 1) or 1
            telegram_id = owner.get('telegram_id')
            panel_email = generate_unique_email({'telegram_id': telegram_id, 'username': owner.get('username')})
            sub_id = _uuid.uuid4().hex

            client = await get_client(server_id)
            inbounds = await get_client_subscription_inbounds(client)
            inbounds = filter_inbounds_for_tariff_id(inbounds, order['tariff_id'])
            if not inbounds:
                raise RuntimeError('На сервере нет доступных inbound')

            sem = asyncio.Semaphore(6)

            async def _add(inb):
                async with sem:
                    try:
                        flow = await client.get_inbound_flow(inb['id'])
                        res = await client.add_client(
                            inbound_id=inb['id'], email=panel_email, total_gb=limit_gb,
                            expire_days=days, limit_ip=max_ips, enable=True,
                            tg_id=str(telegram_id), flow=flow, sub_id=sub_id,
                        )
                        return inb['id'], res['uuid'], None
                    except Exception as e:  # синхронизатор доберёт недостающее
                        return inb['id'], None, e

            results = await asyncio.gather(*[_add(i) for i in inbounds])
            first_uuid = first_inbound = None
            for inbound_id, uuid_value, err in results:
                if err is not None:
                    logger.warning("auto_provision: inbound %s (ключ %s): %s", inbound_id, key_id, err)
                    continue
                if first_uuid is None or inbound_id < first_inbound:
                    first_uuid, first_inbound = uuid_value, inbound_id
            if first_uuid is None:
                raise RuntimeError('Не удалось создать ни одного клиента на сервере')

            update_vpn_key_config(
                key_id=key_id, server_id=server_id, panel_inbound_id=first_inbound,
                panel_email=panel_email, client_uuid=first_uuid, sub_id=sub_id,
            )
            update_payment_key_id(order_id, key_id)
            try:
                stats = await sync_key_to_panel_state(key_id)
                if not stats.get('ok'):
                    logger.warning("auto_provision: ключ %s синхронизирован не полностью: %s", key_id, stats)
            except Exception as e:
                logger.warning("auto_provision: sync ключа %s: %s", key_id, e)
            try:
                from bot.services import cdn as _cdn
                await _cdn.grant_tariff_pack(key_id)
            except Exception as e:
                logger.warning("auto_provision: пакет CDN для ключа %s: %s", key_id, e)
            try:
                from bot.services.key_lifecycle import emit_key_lifecycle_event_safe
                await emit_key_lifecycle_event_safe('key_configured', {
                    'key_id': key_id, 'user_id': order['user_id'], 'tariff_id': order['tariff_id'],
                    'order_id': order_id, 'server_id': server_id, 'panel_inbound_id': first_inbound,
                    'panel_email': panel_email, 'sub_id': sub_id, 'subscription_mode': True,
                    'created_in_this_flow': False, 'source': 'auto_provision',
                })
            except Exception as e:
                logger.warning("auto_provision: lifecycle ключа %s: %s", key_id, e)
            return True
    except Exception as e:
        logger.error("auto_provision: ключ %s (заказ %s) не настроен: %s", key_id, order_id, e)
        return False
    finally:
        if not lock.locked():
            _CONFIG_LOCKS.pop(int(key_id), None)
