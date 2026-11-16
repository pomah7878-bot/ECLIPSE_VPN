"""Наказания за злоупотребление лимитом IP.

Эпизод нарушения фиксируется фоновой проверкой (device_limit_notify) и
проходит три стадии:
  1. предупреждение (уведомление уже отправляется);
  2. временная блокировка пользователя на N часов;
  3. постоянный бан при повторных эпизодах.

Страйки считаются не чаще одного раза в 12 часов, старые сбрасываются
через 30 дней без нарушений. Временная блокировка снимается автоматически;
ручной бан админа помечается permanent и автоматикой не снимается.
"""
import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from database.db_ip_abuse import (
    clear_ip_abuse_block,
    get_ip_abuse,
    ip_abuse_stats,
    list_ip_abusers,
    log_ip_abuse_event,
    mark_ip_abuse_blocked,
    mark_ip_abuse_permanent,
    recent_ip_abuse_events,
    record_ip_abuse_episode,
    release_ip_abuse_candidates,
    reset_ip_abuse,
)

logger = logging.getLogger(__name__)

EPISODE_COOLDOWN_SEC = 12 * 3600
TEMP_BLOCK_FROM_STRIKE = 2
SERIOUS_OVER_LIMIT = 2  # насколько IP должно быть больше лимита, чтобы считать злоупотреблением


def is_enabled() -> bool:
    from database.db_settings import is_ip_limit_enforce_enabled
    return is_ip_limit_enforce_enabled()


def _temp_hours() -> int:
    from database.db_settings import get_ip_limit_temp_hours
    return get_ip_limit_temp_hours()


def _permanent_strikes() -> int:
    from database.db_settings import get_ip_limit_permanent_strikes
    return get_ip_limit_permanent_strikes()


def _reset_days() -> int:
    from database.db_settings import get_ip_limit_strike_reset_days
    return get_ip_limit_strike_reset_days()


def is_serious_overflow(used: int, limit: int) -> bool:
    """Мелкое превышение (1 телефон переключил Wi-Fi/LTE) не наказываем."""
    try:
        used, limit = int(used), int(limit)
    except (TypeError, ValueError):
        return False
    if limit <= 0:
        return False
    return used >= limit + SERIOUS_OVER_LIMIT or (limit > 1 and used >= limit * 2)


def _user(user_id: int) -> Optional[Dict[str, Any]]:
    from database.db_users import get_user_by_id
    return get_user_by_id(int(user_id))


def _sync_keys(telegram_id: Optional[int]) -> None:
    if not telegram_id:
        return
    try:
        import asyncio
        from bot.services.key_lifecycle import sync_user_keys_panel_access
        asyncio.create_task(sync_user_keys_panel_access(int(telegram_id)))
    except Exception as e:  # noqa: BLE001
        logger.warning('ip_abuse: не удалось синхронизировать ключи tg %s: %s', telegram_id, e)


def _until_text(hours: int) -> str:
    return (datetime.now(timezone.utc) + timedelta(hours=int(hours))).strftime('%d.%m.%Y %H:%M UTC')


async def _notify_user(telegram_id: Optional[int], text: str) -> None:
    if not telegram_id or int(telegram_id) <= 0:
        return
    try:
        from bot.utils.runtime_state import get_bot_instance
        from bot.services.user_notify import notify_user
        bot = get_bot_instance()
        if bot:
            await notify_user(bot, int(telegram_id), text)
    except Exception as e:  # noqa: BLE001
        logger.debug('ip_abuse: уведомление пользователю не отправлено: %s', e)


async def temp_block(user_id: int, hours: Optional[int] = None, by: str = 'auto', reason: str = '') -> Dict[str, Any]:
    """Временная блокировка пользователя (все его ключи отключаются)."""
    user = _user(user_id)
    if not user:
        return {'ok': False, 'message': 'Пользователь не найден'}
    hours = int(hours or _temp_hours())
    until = mark_ip_abuse_blocked(int(user_id), hours)
    from database.db_users import set_user_banned
    set_user_banned(int(user['telegram_id']), True)
    _sync_keys(user.get('telegram_id'))
    await _notify_user(
        user.get('telegram_id'),
        f"⛔ <b>Доступ приостановлен на {hours} ч</b>\n\n"
        f"Причина: превышение лимита устройств (IP).\n"
        f"{reason}\n\n"
        f"Доступ вернётся автоматически: {_until_text(hours)}.",
    )
    log_ip_abuse_event(int(user_id), None, 0, 0, 'temp_block', f"by={by}; hours={hours}; until={until}")
    logger.warning('ip_abuse: временная блокировка user=%s tg=%s до %s (%s)', user_id, user.get('telegram_id'), until, by)
    return {'ok': True, 'until': until, 'hours': hours}


async def permanent_ban(user_id: int, by: str = 'auto', reason: str = '') -> Dict[str, Any]:
    user = _user(user_id)
    if not user:
        return {'ok': False, 'message': 'Пользователь не найден'}
    mark_ip_abuse_permanent(int(user_id))
    from database.db_users import set_user_banned
    set_user_banned(int(user['telegram_id']), True)
    _sync_keys(user.get('telegram_id'))
    await _notify_user(
        user.get('telegram_id'),
        "⛔ <b>Доступ заблокирован</b>\n\n"
        "Причина: повторное превышение лимита устройств (IP).\n"
        "Для разбора обратитесь в поддержку.",
    )
    log_ip_abuse_event(int(user_id), None, 0, 0, 'permanent_ban', f"by={by}; {reason}")
    logger.warning('ip_abuse: постоянный бан user=%s tg=%s (%s)', user_id, user.get('telegram_id'), by)
    return {'ok': True}


async def unblock(user_id: int, by: str = 'admin', reset_strikes: bool = False, sync_keys: bool = True) -> Dict[str, Any]:
    user = _user(user_id)
    if not user:
        return {'ok': False, 'message': 'Пользователь не найден'}
    reset_ip_abuse(int(user_id), reset_strikes=reset_strikes)
    from database.db_users import set_user_banned
    set_user_banned(int(user['telegram_id']), False)
    if sync_keys:
        _sync_keys(user.get('telegram_id'))
    log_ip_abuse_event(int(user_id), None, 0, 0, 'unblock', f"by={by}; reset_strikes={reset_strikes}")
    return {'ok': True, 'telegram_id': user.get('telegram_id')}


async def record_episode(key: Dict[str, Any], used: int, limit: int) -> Dict[str, Any]:
    """Вызывается после подтверждённого превышения лимита IP (2 проверки подряд)."""
    if not is_enabled():
        return {'action': 'disabled'}
    user_id = key.get('user_id')
    if not user_id:
        return {'action': 'none'}
    if not is_serious_overflow(used, limit):
        return {'action': 'none', 'used': used, 'limit': limit}

    result = record_ip_abuse_episode(
        int(user_id), key.get('id'), int(used), int(limit),
        cooldown_sec=EPISODE_COOLDOWN_SEC, reset_days=_reset_days(),
    )
    if result.get('permanent'):
        return {'action': 'permanent', 'strikes': result.get('strikes', 0), 'counted': False}
    if not result.get('counted'):
        active = bool(result.get('blocked_until'))
        return {
            'action': 'blocked' if active else 'cooldown',
            'strikes': result.get('strikes', 0),
            'blocked_until': result.get('blocked_until'),
            'counted': False,
        }

    strikes = int(result.get('strikes') or 0)
    blocked_until = None
    if strikes >= _permanent_strikes():
        await permanent_ban(int(user_id), by='auto', reason=f'strikes={strikes}')
        action = 'permanent'
    elif strikes >= TEMP_BLOCK_FROM_STRIKE:
        blocked = await temp_block(
            int(user_id), _temp_hours(), by='auto',
            reason=f"С ключа одновременно выходило {used} IP при лимите {limit}.",
        )
        blocked_until = blocked.get('until')
        action = 'temp_block'
    else:
        action = 'warning'
    log_ip_abuse_event(int(user_id), key.get('id'), int(used), int(limit), action, f"strikes={strikes}")
    return {
        'action': action,
        'strikes': strikes,
        'counted': True,
        'blocked_until': blocked_until,
    }


async def release_expired(bot=None) -> int:
    """Снимает истёкшие временные блокировки. Возвращает число разблокированных."""
    released = 0
    for row in release_ip_abuse_candidates():
        user_id = int(row['user_id'])
        tg = row.get('telegram_id')
        try:
            from database.db_users import set_user_banned
            if tg is not None:
                set_user_banned(int(tg), False)
            clear_ip_abuse_block(user_id)
            _sync_keys(tg)
            await _notify_user(
                tg,
                "✅ <b>Доступ восстановлен</b>\n\n"
                "Временная блокировка за превышение лимита устройств снята. "
                "Пожалуйста, используйте ключ на разрешённом числе устройств.",
            )
            log_ip_abuse_event(user_id, None, 0, 0, 'auto_unblock', 'block expired')
            released += 1
        except Exception as e:  # noqa: BLE001
            logger.warning('ip_abuse: не удалось снять блокировку user=%s: %s', user_id, e)
    if released:
        logger.info('ip_abuse: снято временных блокировок: %s', released)
    return released


def list_offenders(limit: int = 20) -> List[Dict[str, Any]]:
    return list_ip_abusers(limit)


def stats() -> Dict[str, int]:
    return ip_abuse_stats()


def get_record(user_id: int) -> Optional[Dict[str, Any]]:
    return get_ip_abuse(int(user_id))


def recent_events(limit: int = 20) -> List[Dict[str, Any]]:
    return recent_ip_abuse_events(limit)
