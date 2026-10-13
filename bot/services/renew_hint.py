"""
Подсветка «Продлить» для клиентов с заканчивающейся или истёкшей подпиской.

Клиенты в такой момент нажимали «Купить ключ» вместо «Продлить» и
путались. Здесь собраны ключи, которые пора продлить, и зелёные кнопки
продления для главного экрана и экрана «Купить ключ».
"""
import logging
from datetime import datetime, timezone
from typing import Any, Dict, List

logger = logging.getLogger(__name__)

RENEW_WARN_DAYS = 3
MAX_RENEW_BUTTONS = 3


def days_left(expires_at: Any):
    """Целых дней до конца срока (отрицательное/0 — уже истёк) или None."""
    if not expires_at:
        return None
    try:
        exp = datetime.fromisoformat(str(expires_at).replace('Z', '+00:00'))
    except (ValueError, TypeError):
        return None
    if exp.tzinfo is None:
        exp = exp.replace(tzinfo=timezone.utc)
    sec = (exp - datetime.now(timezone.utc)).total_seconds()
    if sec <= 0:
        return -1
    return int(sec // 86400)


def needs_renew(expires_at: Any, warn_days: int = RENEW_WARN_DAYS) -> bool:
    d = days_left(expires_at)
    return d is not None and (d < 0 or d <= warn_days)


def get_renew_keys(telegram_id: int) -> List[Dict[str, Any]]:
    """Ключи пользователя, которые истекли или скоро истекут (ближайшие первыми)."""
    try:
        from database.db_keys import get_user_keys_for_display
        keys = get_user_keys_for_display(telegram_id) or []
    except Exception as e:
        logger.debug("renew_hint: ключи не получены: %s", e)
        return []
    found = [k for k in keys if needs_renew(k.get('expires_at'))]
    found.sort(key=lambda k: str(k.get('expires_at') or ''))
    return found


def _label(key: Dict[str, Any]) -> str:
    d = days_left(key.get('expires_at'))
    name = str(key.get('display_name') or f"ключ #{key.get('id')}")
    if len(name) > 18:
        name = name[:17] + '…'
    if d is not None and d < 0:
        tail = 'истёк'
    elif d == 0:
        tail = 'сегодня последний день'
    else:
        tail = f'осталось {d} дн.'
    return f"🔄 ПРОДЛИТЬ · {name} ({tail})"


def build_renew_rows(telegram_id: int):
    """Строки зелёных кнопок «Продлить» (по одной на ключ). Пусто — если не нужно."""
    from aiogram.types import InlineKeyboardButton
    keys = get_renew_keys(telegram_id)
    rows = []
    for k in keys[:MAX_RENEW_BUTTONS]:
        rows.append([InlineKeyboardButton(
            text=_label(k), callback_data=f"key_renew:{int(k['id'])}", style='success')])
    if len(keys) > MAX_RENEW_BUTTONS:
        rows.append([InlineKeyboardButton(
            text=f"🔑 Все ключи ({len(keys)})", callback_data="my_keys")])
    return rows
