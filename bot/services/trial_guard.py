"""Единая защита пробного периода от повторной выдачи.

Одна точка правды для всех каналов (бот, сайт, приложение):
- собирает личности человека: Telegram, сайт-аккаунт, подтверждённый номер;
- атомарно фиксирует выдачу (trial_claims) до создания ключа;
- проверяет ручные и автоматические запреты (trial_blocks);
- пишет отказы в журнал (trial_abuse_events) и уведомляет админов;
- по настройке может банить повторников (по умолчанию выключено) и
  требовать подтверждённый номер (строгий режим, по умолчанию выключен).

Важно: старые флаги users.used_trial и user_group_trials продолжают
обновляться для совместимости и статистики, но решение о выдаче
принимается по trial_claims.
"""
import logging
from typing import Any, Dict, Iterable, List, Optional, Tuple

from database.db_trial_guard import (
    add_trial_abuse_event,
    add_trial_block,
    add_trial_claim,
    count_recent_trial_abuse,
    ensure_trial_claims,
    find_any_trial_claim,
    find_trial_block,
    find_trial_claim,
    get_trial_abuse_event,
    list_trial_blocks,
    mark_trial_alert_sent,
    recent_trial_abuse,
    recent_trial_claims,
    release_trial_claims,
    remove_trial_block,
    trial_alert_recently_sent,
    trial_guard_stats,
)

logger = logging.getLogger(__name__)

Identity = Tuple[str, str]

__all__ = [
    'REASON_MESSAGES',
    'bot_identities',
    'site_identities',
    'status_bot',
    'status_site',
    'claim_bot',
    'claim_site',
    'release_claim',
    'bind_device_to_trial',
    'record_refusal',
    'send_abuse_alert',
    'transfer_site_trial',
    'transfer_claimed_purchase',
    'identity_label',
    'labels_for',
    'stats',
    'strict_identity_enabled',
    'auto_ban_threshold',
    'alert_threshold',
    'recent_events',
    'blocked_list',
    'block_identity',
    'unblock',
    'find_block',
    'event_by_id',
    'claims_recent',
]

REASON_MESSAGES = {
    'banned': '⛔ Доступ заблокирован',
    'blocked': '🚫 Пробный период недоступен для этого аккаунта',
    'already_used': 'ℹ️ Вы уже использовали пробный период',
    'new_clients_only': 'ℹ️ Пробный период доступен только новым клиентам',
    'strict_identity_required': '📱 Для пробного периода подтвердите номер телефона: «Мои ключи» → «Код для входа на сайт»',
}

REASON_LABELS = {
    'banned': 'аккаунт забанен',
    'blocked': 'запрет пробника',
    'already_used': 'пробник уже использован',
    'new_clients_only': 'не новый клиент',
    'strict_identity_required': 'нет подтверждённого номера',
}

CHANNEL_LABELS = {
    'bot': 'бот',
    'site': 'сайт',
    'app': 'приложение',
    'link': 'привязка',
    'legacy': 'старый учёт',
}


def _dedupe(identities: Iterable[Identity]) -> List[Identity]:
    seen = set()
    result: List[Identity] = []
    for item in identities or ():
        try:
            kind, value = item
        except (TypeError, ValueError):
            continue
        kind, value = str(kind or ''), str(value or '')
        if not kind or not value or (kind, value) in seen:
            continue
        seen.add((kind, value))
        result.append((kind, value))
    return result


def _phone_identity(raw: Any) -> Optional[Identity]:
    from bot.services.trial_phone_registry import normalize_phone
    phone = normalize_phone(str(raw or ''))
    return ('phone', phone) if phone else None


def bot_identities(telegram_id: int) -> List[Identity]:
    """Все известные личности Telegram-пользователя."""
    try:
        tid = int(telegram_id)
    except (TypeError, ValueError):
        return []
    identities: List[Identity] = [('telegram', str(tid))]
    try:
        from database.db_accounts import get_site_account_by_telegram_id
        account = get_site_account_by_telegram_id(tid)
        if account:
            identities.extend(site_identities(account))
    except Exception as e:  # noqa: BLE001
        logger.debug('trial_guard: не удалось получить сайт-аккаунт tg %s: %s', tid, e)
    try:
        from bot.services.trial_phone_registry import get_phone_for_telegram_id
        phone = _phone_identity(get_phone_for_telegram_id(tid))
        if phone:
            identities.append(phone)
    except Exception as e:  # noqa: BLE001
        logger.debug('trial_guard: не удалось получить телефон tg %s: %s', tid, e)
    return _dedupe(identities)


def site_identities(account: Dict[str, Any], extra_identities: Optional[Iterable[Identity]] = None) -> List[Identity]:
    """Все известные личности сайт-аккаунта (+ устройство из запроса, если есть)."""
    if not account or not account.get('id'):
        return _dedupe(extra_identities or ())
    identities: List[Identity] = [('site_account', str(account['id']))]
    if account.get('telegram_id'):
        try:
            identities.append(('telegram', str(int(account['telegram_id']))))
        except (TypeError, ValueError):
            pass
    try:
        from database.db_accounts import site_account_phone
        phone = _phone_identity(site_account_phone(account))
        if phone:
            identities.append(phone)
    except Exception as e:  # noqa: BLE001
        logger.debug('trial_guard: не удалось получить телефон сайта %s: %s', account.get('id'), e)
    try:
        from bot.services.trial_phone_registry import get_phone_for_site_account_id
        phone = _phone_identity(get_phone_for_site_account_id(account['id']))
        if phone:
            identities.append(phone)
    except Exception as e:  # noqa: BLE001
        logger.debug('trial_guard: не удалось получить телефон сайта (реестр) %s: %s', account.get('id'), e)
    return _dedupe(identities + list(extra_identities or ()))


def _has_phone(identities: Iterable[Identity]) -> bool:
    return any(kind == 'phone' for kind, _ in identities or ())


def _deny(reason: str, identities: Iterable[Identity]) -> Dict[str, Any]:
    return {
        'allowed': False,
        'reason': reason,
        'message': REASON_MESSAGES.get(reason, 'Пробный период недоступен'),
        'identities': list(identities),
    }


def _allow(identities: Iterable[Identity]) -> Dict[str, Any]:
    return {'allowed': True, 'reason': '', 'message': '', 'identities': list(identities)}


def _is_new_bot_client(telegram_id: int) -> bool:
    from database.db_trial_groups import user_has_any_keys_ever
    from database.db_users import get_user_internal_id
    internal_id = get_user_internal_id(telegram_id)
    if not internal_id:
        return True
    return not user_has_any_keys_ever(internal_id)


def strict_identity_enabled() -> bool:
    from database.db_settings import is_trial_strict_identity
    return is_trial_strict_identity()


def auto_ban_threshold() -> int:
    from database.db_settings import get_trial_auto_ban_threshold
    return get_trial_auto_ban_threshold()


def alert_threshold() -> int:
    from database.db_settings import get_trial_abuse_alert_threshold
    return get_trial_abuse_alert_threshold()


def status_bot(telegram_id: int, scope: str = 'account') -> Dict[str, Any]:
    """Можно ли выдать пробник этому Telegram-пользователю в указанной области.

    Любая ошибка БД трактуется как отказ (fail closed): лучше не выдать
    пробник, чем выдать второй из-за сбоя проверки.
    """
    try:
        return _status_bot_impl(telegram_id, scope)
    except Exception as e:  # noqa: BLE001
        logger.exception('trial_guard: ошибка проверки пробника tg %s: %s', telegram_id, e)
        return _deny('blocked', [])


def _status_bot_impl(telegram_id: int, scope: str = 'account') -> Dict[str, Any]:
    identities = bot_identities(telegram_id)
    if not identities:
        return _deny('blocked', identities)

    from database.db_users import is_user_banned
    try:
        if is_user_banned(int(telegram_id)):
            return _deny('banned', identities)
    except Exception as e:  # noqa: BLE001
        logger.warning('trial_guard: не удалось проверить бан tg %s: %s', telegram_id, e)

    if find_trial_block(identities):
        return _deny('blocked', identities)

    if find_trial_claim(identities, scope):
        return _deny('already_used', identities)

    if scope == 'account':
        # Смена режима админом не должна открывать второй пробник:
        # пробник, взятый в группе, тоже считается использованным.
        if find_any_trial_claim(identities):
            return _deny('already_used', identities)
        if not _is_new_bot_client(int(telegram_id)):
            return _deny('new_clients_only', identities)
    else:
        if find_trial_claim(identities, 'account'):
            return _deny('already_used', identities)
        if not find_any_trial_claim(identities) and not _is_new_bot_client(int(telegram_id)):
            return _deny('new_clients_only', identities)

    if strict_identity_enabled() and not _has_phone(identities):
        return _deny('strict_identity_required', identities)
    return _allow(identities)


def status_site(
    account: Dict[str, Any],
    *,
    used_trial: bool = False,
    is_new_client: bool = True,
    extra_identities: Optional[Iterable[Identity]] = None,
) -> Dict[str, Any]:
    """Можно ли выдать пробник этому сайт-аккаунту (fail closed при ошибке)."""
    try:
        return _status_site_impl(
            account,
            used_trial=used_trial,
            is_new_client=is_new_client,
            extra_identities=extra_identities,
        )
    except Exception as e:  # noqa: BLE001
        logger.exception('trial_guard: ошибка проверки пробника сайта %s: %s', (account or {}).get('id'), e)
        return _deny('blocked', [])


def _status_site_impl(
    account: Dict[str, Any],
    *,
    used_trial: bool = False,
    is_new_client: bool = True,
    extra_identities: Optional[Iterable[Identity]] = None,
) -> Dict[str, Any]:
    identities = site_identities(account, extra_identities)
    if not identities:
        return _deny('blocked', identities)

    if account.get('telegram_id'):
        try:
            from database.db_users import is_user_banned
            if is_user_banned(int(account['telegram_id'])):
                return _deny('banned', identities)
        except Exception as e:  # noqa: BLE001
            logger.warning('trial_guard: не удалось проверить бан сайта %s: %s', account.get('id'), e)
    if account.get('placeholder_user_id'):
        try:
            from database.db_users import is_user_banned_by_id
            if is_user_banned_by_id(int(account['placeholder_user_id'])):
                return _deny('banned', identities)
        except Exception as e:  # noqa: BLE001
            logger.warning('trial_guard: не удалось проверить бан служебной личности сайта %s: %s', account.get('id'), e)

    if find_trial_block(identities):
        return _deny('blocked', identities)
    if used_trial or find_trial_claim(identities, 'account'):
        return _deny('already_used', identities)
    if not is_new_client:
        return _deny('new_clients_only', identities)
    if strict_identity_enabled() and not _has_phone(identities):
        return _deny('strict_identity_required', identities)
    return _allow(identities)


def claim_bot(telegram_id: int, scope: str = 'account', channel: str = 'bot') -> Dict[str, Any]:
    return add_trial_claim(bot_identities(telegram_id), scope, channel)


def claim_site(
    account: Dict[str, Any],
    channel: str = 'site',
    scope: str = 'account',
    extra_identities: Optional[Iterable[Identity]] = None,
) -> Dict[str, Any]:
    return add_trial_claim(site_identities(account, extra_identities), scope, channel)


def bind_device_to_trial(key: Dict[str, Any], device_hash: str, client: str = '') -> None:
    """Помечает устройство, с которого реально использовали пробный ключ.

    Вызывается при загрузке подписки пробного ключа: устройство получает
    отметку в trial_claims, поэтому второй пробник с этого же устройства
    (из приложения, где есть HWID) уже не выдаётся даже на новый аккаунт.
    """
    device_hash = str(device_hash or '').strip()
    if not device_hash:
        return
    identities = [('device', device_hash)]
    # Обычно отметка уже есть — не открываем лишнюю запись на каждую загрузку подписки.
    if find_trial_claim(identities, 'account'):
        return
    ensure_trial_claims(
        identities,
        'account',
        f"device:{client or 'unknown'}",
    )


def release_claim(result: Dict[str, Any]) -> None:
    """Снимает заявку, если после неё не удалось создать ключ."""
    if result and result.get('ok'):
        release_trial_claims(result.get('claim_ids') or [])


def record_refusal(
    identities: Iterable[Identity],
    *,
    primary: Identity,
    scope: str,
    channel: str,
    reason: str,
    ip: str = '',
    user_agent: str = '',
    detail: str = '',
) -> Dict[str, Any]:
    """Журналирует отказ и, если настроено, уведомляет/банит."""
    identities = _dedupe(identities) or [primary]
    try:
        add_trial_abuse_event(
            primary[0], primary[1], scope, channel, reason, ip=ip, user_agent=user_agent, detail=detail,
        )
    except Exception as e:  # noqa: BLE001
        logger.warning('trial_guard: не записал отказ (%s): %s', reason, e)
        return {'count': 0, 'alert': False, 'auto_banned': False}

    try:
        count = count_recent_trial_abuse(primary[0], primary[1], 24)
    except Exception:  # noqa: BLE001
        count = 1

    alert = False
    try:
        threshold = alert_threshold()
        if count >= threshold and not trial_alert_recently_sent(primary[0], primary[1], 24):
            mark_trial_alert_sent(primary[0], primary[1])
            alert = True
    except Exception as e:  # noqa: BLE001
        logger.debug('trial_guard: не удалось отметить уведомление: %s', e)

    auto_banned = False
    try:
        ban_after = auto_ban_threshold()
        if ban_after > 0 and count >= ban_after:
            for kind, value in identities:
                add_trial_block(kind, value, reason=f'auto:{reason}', created_by='auto')
            try:
                from database.db_users import set_user_banned
                for kind, value in identities:
                    if kind == 'telegram' and str(value).isdigit():
                        set_user_banned(int(value), True)
            except Exception as e:  # noqa: BLE001
                logger.warning('trial_guard: не удалось забанить Telegram из отказа: %s', e)
            auto_banned = True
    except Exception as e:  # noqa: BLE001
        logger.warning('trial_guard: не удалось применить автобан: %s', e)

    return {'count': count, 'alert': alert, 'auto_banned': auto_banned}


async def send_abuse_alert(
    *,
    channel: str,
    identities: Iterable[Identity],
    primary: Identity,
    reason: str,
    count: int,
    auto_banned: bool = False,
) -> None:
    """Короткое уведомление админам о повторной попытке."""
    try:
        from bot.utils.runtime_state import get_bot_instance
        from config import ADMIN_IDS
        from bot.utils.text import escape_html

        bot_instance = get_bot_instance()
        if not bot_instance:
            return
        channel_label = CHANNEL_LABELS.get(channel, channel)
        reason_label = REASON_LABELS.get(reason, reason)
        who = identity_label(*primary)
        phone_hint = labels_for([i for i in identities if i[0] == 'phone'])
        lines = [
            '⚠️ <b>Повторная попытка получить пробник</b>',
            '',
            f'📡 Канал: {escape_html(channel_label)}',
            f'👤 Кто: {escape_html(who)}',
            f'📄 Причина: {escape_html(reason_label)}',
            f'🔁 Попыток за 24 ч: {int(count)}',
        ]
        if phone_hint:
            lines.append(f'📱 Номер: {escape_html(phone_hint)}')
        if auto_banned:
            lines.append('⛔ Сработал автобан: пробник запрещён, аккаунт забанен.')
        else:
            lines.append('Разбор попыток: Админ-панель → Пробная подписка → Защита от повторных.')
        text = '\n'.join(lines)
        for admin_id in list(ADMIN_IDS)[:5]:
            try:
                await bot_instance.send_message(admin_id, text, parse_mode='HTML')
            except Exception as send_err:  # noqa: BLE001
                logger.debug('trial_guard: уведомление админу %s не доставлено: %s', admin_id, send_err)
    except Exception as e:  # noqa: BLE001
        logger.warning('trial_guard: не удалось отправить уведомление админам: %s', e)


def transfer_site_trial(account_id: int, telegram_id: Optional[int] = None) -> None:
    """Помечает сайт-пробник на связанные личности (при привязке аккаунта)."""
    try:
        from database.db_accounts import get_site_account_by_id
        account = get_site_account_by_id(account_id)
    except Exception:  # noqa: BLE001
        account = None
    identities: List[Identity] = site_identities(account) if account else [('site_account', str(account_id))]
    if telegram_id:
        try:
            identities.append(('telegram', str(int(telegram_id))))
        except (TypeError, ValueError):
            pass
        try:
            from bot.services.trial_phone_registry import get_phone_for_telegram_id
            phone = _phone_identity(get_phone_for_telegram_id(int(telegram_id)))
            if phone:
                identities.append(phone)
        except Exception:  # noqa: BLE001
            pass
    ensure_trial_claims(identities, 'account', 'link')
    if telegram_id:
        try:
            from database.db_users import get_user_internal_id, mark_trial_used
            internal_id = get_user_internal_id(int(telegram_id))
            if internal_id:
                mark_trial_used(internal_id)
        except Exception as e:  # noqa: BLE001
            logger.warning('trial_guard: не удалось перенести отметку пробника на tg %s: %s', telegram_id, e)


def transfer_claimed_purchase(purchase: Dict[str, Any], telegram_id: int) -> None:
    """Пробник, купленный/полученный на сайте, привязан по коду в боте."""
    if not purchase or (purchase.get('yookassa_payment_id') or '') != 'trial':
        return
    account_id = purchase.get('site_account_id')
    if account_id:
        transfer_site_trial(int(account_id), telegram_id)
    else:
        try:
            from database.db_users import get_user_internal_id, mark_trial_used
            internal_id = get_user_internal_id(int(telegram_id))
            if internal_id:
                mark_trial_used(internal_id)
        except Exception:  # noqa: BLE001
            pass


def identity_label(kind: str, value: str) -> str:
    """Человекочитаемая подпись личности (без HTML)."""
    kind, value = str(kind), str(value)
    if kind == 'telegram':
        try:
            from database.db_users import get_user_by_telegram_id
            user = get_user_by_telegram_id(int(value))
            if user:
                username = f" (@{user['username']})" if user.get('username') else ''
                return f"tg {value}{username}"
        except Exception:  # noqa: BLE001
            pass
        return f"tg {value}"
    if kind == 'site_account':
        try:
            from database.db_accounts import get_site_account_by_id
            account = get_site_account_by_id(int(value))
            if account:
                if account.get('email'):
                    return f"сайт #{value} ({account['email']})"
                if account.get('provider'):
                    return f"сайт #{value} ({account['provider']})"
        except Exception:  # noqa: BLE001
            pass
        return f"сайт #{value}"
    if kind == 'phone':
        digits = ''.join(ch for ch in value if ch.isdigit())
        if len(digits) >= 10:
            return f"номер +{digits[0]} ••• •••-{digits[-4:]}"
        return 'номер'
    if kind == 'device':
        return f"устройство {value[:10]}"
    return f"{kind}:{value}"


def labels_for(identities: Iterable[Identity]) -> str:
    return ', '.join(identity_label(kind, value) for kind, value in _dedupe(identities))


def stats() -> Dict[str, int]:
    return trial_guard_stats()


def recent_events(limit: int = 20) -> List[Dict[str, Any]]:
    return recent_trial_abuse(limit)


def claims_recent(limit: int = 10) -> List[Dict[str, Any]]:
    return recent_trial_claims(limit)


def blocked_list(limit: int = 20) -> List[Dict[str, Any]]:
    return list_trial_blocks(limit)


def event_by_id(event_id: int) -> Optional[Dict[str, Any]]:
    return get_trial_abuse_event(event_id)


def find_block(identities: Iterable[Identity]) -> Optional[Dict[str, Any]]:
    return find_trial_block(identities)


def block_identity(kind: str, value: str, reason: str = '', created_by: str = 'admin') -> Optional[int]:
    return add_trial_block(kind, value, reason=reason, created_by=created_by)


def unblock(block_id: int) -> Optional[Dict[str, Any]]:
    """Снимает запрет. Авто-бан Telegram снимает только для авто-блокировки."""
    removed = remove_trial_block(block_id)
    if removed and removed.get('created_by') == 'auto' and removed.get('identity_kind') == 'telegram':
        try:
            from database.db_users import set_user_banned
            set_user_banned(int(removed['identity_value']), False)
        except Exception as e:  # noqa: BLE001
            logger.warning('trial_guard: не удалось снять бан tg %s: %s', removed.get('identity_value'), e)
    return removed
