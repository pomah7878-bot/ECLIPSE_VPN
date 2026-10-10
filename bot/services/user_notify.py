"""Безопасная отправка сообщения клиенту: всегда с кнопкой «На главную», сбой не ломает основной процесс."""
from __future__ import annotations

import logging
from typing import Any, Optional

logger = logging.getLogger(__name__)


async def notify_user(bot, telegram_id: Any, text: str, markup: Optional[Any] = None) -> bool:
    if not bot or not telegram_id:
        return False
    try:
        from bot.utils.notify_kb import with_home
        await bot.send_message(int(telegram_id), text, parse_mode="HTML", reply_markup=with_home(markup))
        return True
    except Exception as e:  # noqa: BLE001
        try:
            from bot.utils.delivery import is_bot_blocked_error
            from database.requests import mark_user_bot_blocked
            if is_bot_blocked_error(e):
                mark_user_bot_blocked(int(telegram_id))
        except Exception:  # noqa: BLE001
            pass
        logger.debug("notify_user: %s не доставлено: %s", telegram_id, e)
        return False
