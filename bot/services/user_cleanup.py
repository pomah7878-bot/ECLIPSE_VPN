"""
Полное удаление клиента из бота.

Админ удаляет человека «везде»: ключи снимаются с панелей (что получилось —
в отчёте), затем из локальной базы пропадают сам пользователь, его ключи,
платежи, баланс, реферальные начисления, обращения в поддержку, аккаунт
сайта, подтверждённые номера и пробные выдачи.

Лицензии и партнёрские записи не трогаем: это отдельный контур (деньги
реселлеров), который не относится к клиентским данным.
"""
import logging
from typing import Any, Dict, List, Optional

from database.connection import get_db

logger = logging.getLogger(__name__)


def _server_data(key: Dict[str, Any]) -> dict:
    """Данные сервера для подключения к панели по строке ключа."""
    return {
        'id': key.get('server_id'),
        'name': key.get('server_name'),
        'host': key.get('host'),
        'port': key.get('port'),
        'web_base_path': key.get('web_base_path') or '',
        'login': key.get('login'),
        'password': key.get('password'),
        'protocol': key.get('protocol') or 'https',
        'inbound_group': key.get('inbound_group'),
    }


async def _remove_key_from_panel(key: Dict[str, Any]) -> bool:
    """Снимает клиента с панели: подписку — по panel_email, обычный ключ — по inbound+uuid."""
    if not key.get('host') or key.get('panel_removed_at'):
        return True  # сервера уже нет или ключ снят раньше — локальную запись всё равно удалим

    from bot.services.vpn_api import get_client_from_server_data

    try:
        client = get_client_from_server_data(_server_data(key))
        if key.get('sub_id') and key.get('panel_email'):
            deleted = await client.delete_clients_by_email_on_server(key['panel_email'])
            return deleted > 0
        if key.get('panel_inbound_id') and key.get('client_uuid'):
            await client.delete_client(key['panel_inbound_id'], key['client_uuid'])
            return True
        return False
    except Exception as e:  # noqa: BLE001 — панель может быть недоступна, это попадёт в отчёт
        logger.warning(f"Клиент ключа #{key.get('id')} не удалён с панели: {e}")
        return False


def _in(values: List[int]) -> str:
    return ",".join("?" * len(values))


def _run(conn, sql: str, params: List[Any]) -> None:
    """Выполняет удаление, пропуская отсутствующие в этой версии схемы таблицы."""
    try:
        conn.execute(sql, params)
    except Exception as e:  # noqa: BLE001 — старая схема может не иметь таблицы
        if "no such table" in str(e):
            logger.debug(f"Пропуск удаления (нет таблицы): {e}")
            return
        raise


async def delete_user_everywhere(telegram_id: int) -> Optional[Dict[str, Any]]:
    """Удаляет клиента и все его данные. Возвращает отчёт или None, если такого нет."""
    with get_db() as conn:
        user = conn.execute("SELECT * FROM users WHERE telegram_id = ?", (telegram_id,)).fetchone()
        if not user:
            return None
        user_id = int(user['id'])

        accounts = [dict(r) for r in conn.execute(
            "SELECT id, placeholder_user_id FROM site_accounts "
            "WHERE telegram_id = ? OR placeholder_user_id = ?",
            (telegram_id, user_id),
        ).fetchall()]
        keys = [dict(r) for r in conn.execute(
            """SELECT vk.*, s.name AS server_name, s.host, s.port, s.web_base_path,
                      s.login, s.password, s.protocol, s.inbound_group
               FROM vpn_keys vk
               LEFT JOIN servers s ON s.id = vk.server_id
               WHERE vk.user_id = ?""",
            (user_id,),
        ).fetchall()]

    failures: List[str] = []
    for key in keys:
        if not await _remove_key_from_panel(key):
            failures.append(key.get('server_name') or f"сервер #{key.get('server_id')}")

    key_ids = [int(k['id']) for k in keys]
    account_ids = [int(a['id']) for a in accounts]
    placeholder_ids = [int(a['placeholder_user_id']) for a in accounts if a['placeholder_user_id']]
    # клиент сайта без Telegram: его «личность» — служебный пользователь
    extra_user_ids = [uid for uid in placeholder_ids if uid != user_id]

    report: Dict[str, Any] = {
        'keys': len(key_ids),
        'keys_failed': failures,
        'accounts': len(account_ids),
    }

    with get_db() as conn:
        thread_ids = [int(r['id']) for r in conn.execute(
            "SELECT id FROM support_threads WHERE user_id = ? OR user_telegram_id = ?",
            (user_id, telegram_id),
        ).fetchall()]
        report['threads'] = len(thread_ids)

        if key_ids:
            placeholders = _in(key_ids)
            for table, column in (
                ("traffic_history", "vpn_key_id"),
                ("notification_log", "vpn_key_id"),
                ("key_lifecycle_event_log", "vpn_key_id"),
                ("deferred_panel_deletes", "key_id"),
                ("happ_devices", "key_id"),
            ):
                _run(conn, f"DELETE FROM {table} WHERE {column} IN ({placeholders})", key_ids)
            _run(conn, f"DELETE FROM key_operation_log WHERE vpn_key_id IN ({placeholders}) OR user_id = ?",
                 key_ids + [user_id])
        else:
            _run(conn, "DELETE FROM key_operation_log WHERE user_id = ?", [user_id])
        _run(conn, "DELETE FROM vpn_keys WHERE user_id = ?", [user_id])

        simple = (
            ("payments", "user_id", user_id),
            ("balance_operations", "user_id", user_id),
            ("cdn_packs", "user_id", user_id),
            ("cdn_promo_claims", "user_id", user_id),
            ("promo_redemptions", "user_id", user_id),
            ("ip_abuse", "user_id", user_id),
            ("ip_abuse_events", "user_id", user_id),
            ("trial_device_blocks", "user_id", user_id),
            ("trial_devices", "user_id", user_id),
            ("user_group_trials", "user_id", user_id),
            ("user_channel_read_status", "telegram_id", telegram_id),
            ("site_login_codes", "telegram_id", telegram_id),
            ("trial_verified_phones", "telegram_id", telegram_id),
            ("anonymous_purchases", "claimed_by_telegram_id", telegram_id),
        )
        for table, column, value in simple:
            _run(conn, f"DELETE FROM {table} WHERE {column} = ?", [value])

        for table in ("bonus_task_claims", "collab_members", "promo_link_visits", "roulette_spins"):
            _run(conn, f"DELETE FROM {table} WHERE user_id = ? OR telegram_id = ?", [user_id, telegram_id])
        _run(conn, "DELETE FROM referral_stats WHERE referrer_id = ? OR referral_id = ?", [user_id, user_id])
        for table in ("trial_claims", "trial_abuse_events", "trial_abuse_alerts"):
            _run(conn, f"DELETE FROM {table} WHERE identity_kind = 'telegram_id' AND identity_value = ?",
                 [str(telegram_id)])

        if thread_ids:
            placeholders = _in(thread_ids)
            for table in ("support_messages", "support_admin_notifications", "help_orders"):
                _run(conn, f"DELETE FROM {table} WHERE thread_id IN ({placeholders})", thread_ids)
            _run(conn, f"DELETE FROM support_threads WHERE id IN ({placeholders})", thread_ids)

        for account_id in account_ids:
            _run(conn, "DELETE FROM site_trial_phone_pending WHERE account_id = ?", [account_id])
            _run(conn, "DELETE FROM oauth_exchange_codes WHERE account_id = ?", [account_id])
            _run(conn, "DELETE FROM trial_verified_phones WHERE site_account_id = ?", [account_id])
            _run(conn, "DELETE FROM anonymous_purchases WHERE site_account_id = ?", [account_id])
            _run(conn, "DELETE FROM site_accounts WHERE id = ?", [account_id])

        # На этого пользователя могли ссылаться как на пригласившего
        _run(conn, "UPDATE users SET referred_by = NULL WHERE referred_by = ?", [user_id])
        _run(conn, "DELETE FROM users WHERE id = ?", [user_id])
        for extra_id in extra_user_ids:
            _run(conn, "DELETE FROM users WHERE id = ?", [extra_id])

    logger.info(
        f"Клиент {telegram_id} удалён: ключей {report['keys']}, "
        f"аккаунтов сайта {report['accounts']}, обращений {report['threads']}, "
        f"ошибок панелей {len(failures)}"
    )
    return report
