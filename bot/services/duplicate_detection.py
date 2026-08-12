"""
Обнаружение потенциальных дубликатов клиентов: один и тот же реальный
человек может иметь ДВА разных клиента на панели — один создан ботом
(при покупке), другой добавлен администратором вручную отдельно.

Технический признак связи — совпадение IP-адреса подключения. Это
ненадёжный сигнал (общий Wi-Fi/NAT может дать ложное совпадение), поэтому
результат используется только как ПОДСКАЗКА для ручной проверки
администратором, а не как автоматическое решение.
"""
import asyncio
import logging
import time as _time
from typing import Any, Dict, List

logger = logging.getLogger(__name__)

RECENT_WINDOW_SECONDS = 30 * 24 * 60 * 60  # учитываем IP за последние 30 дней


async def find_potential_duplicate_clients(server_id: int) -> List[Dict[str, Any]]:
    """
    Ищет пары (ботовый клиент, вручную созданный клиент) на одном сервере
    с пересекающимися IP-адресами подключения за последние 30 дней.

    Returns:
        Список словарей: {bot_email, manual_email, shared_ips: set}
    """
    from database.requests import get_active_servers
    from bot.services.vpn_api import get_client_from_server_data
    from bot.services.panel_sync import collect_server_snapshots
    from database.requests import get_all_active_keys_with_server, get_all_servers

    servers = [s for s in get_active_servers() if int(s['id']) == server_id]
    if not servers:
        return []
    server_data = servers[0]

    all_keys = get_all_active_keys_with_server()
    all_servers = [s for s in get_all_servers() if int(s['id']) == server_id]
    snapshots = await collect_server_snapshots(all_keys, all_servers)
    snap = snapshots.snapshots.get(server_id)
    if not snap:
        return []

    all_emails = list(snap.clients.keys())
    bot_emails = {e for e in all_emails if e.startswith('user_')}
    manual_emails = {e for e in all_emails if not e.startswith('user_')}
    if not bot_emails or not manual_emails:
        return []

    try:
        client = get_client_from_server_data(server_data)
        result = await asyncio.wait_for(
            client._request('POST', '/panel/api/clients/clientIpsByGuid'),
            timeout=8.0,
        )
    except Exception as e:
        logger.warning(f"Не удалось получить IP клиентов для сервера {server_id}: {e}")
        return []

    by_guid = (result.get('obj') or {}) if isinstance(result, dict) else {}
    now = _time.time()

    email_ips: Dict[str, set] = {}
    for node_entries in by_guid.values():
        for email, ip_entries in node_entries.items():
            ips = set()
            for ip_entry in ip_entries:
                ts = ip_entry.get('timestamp') or 0
                if now - ts <= RECENT_WINDOW_SECONDS:
                    ips.add(ip_entry.get('ip'))
            if ips:
                email_ips.setdefault(email, set()).update(ips)

    pairs = []
    for bot_email in bot_emails:
        bot_ips = email_ips.get(bot_email)
        if not bot_ips:
            continue
        for manual_email in manual_emails:
            manual_ips = email_ips.get(manual_email)
            if not manual_ips:
                continue
            shared = bot_ips & manual_ips
            if shared:
                pairs.append({
                    'bot_email': bot_email,
                    'manual_email': manual_email,
                    'shared_ips': shared,
                })
    return pairs
