"""Одно главное меню в чате: запоминаем последнее и убираем его при новом /start.

Раньше каждый /start присылал новое меню, и старые копились в чате. Теперь при /start
предыдущее меню клиента и сама команда удаляются, а новое появляется внизу чата.
"""
import logging
from typing import Dict, Optional

logger = logging.getLogger(__name__)

_SETTING_PREFIX = "main_menu_msg:"
_cache: Dict[int, int] = {}


def remember_main_menu(user_id: int, message) -> None:
    """Запоминает сообщение с главным меню (id хранится и в БД — переживает перезапуск)."""
    try:
        message_id = int(getattr(message, "message_id", 0) or 0)
        if not user_id or not message_id or _cache.get(int(user_id)) == message_id:
            return
        _cache[int(user_id)] = message_id
        from database.db_settings import set_setting
        set_setting(f"{_SETTING_PREFIX}{int(user_id)}", str(message_id))
    except Exception as e:
        logger.debug("Не удалось запомнить главное меню %s: %s", user_id, e)


def _previous_menu_id(user_id: int) -> Optional[int]:
    if int(user_id) in _cache:
        return _cache[int(user_id)]
    try:
        from database.db_settings import get_setting
        value = get_setting(f"{_SETTING_PREFIX}{int(user_id)}")
        return int(value) if value else None
    except Exception:
        return None


async def cleanup_before_start(bot, chat_id: int, user_id: int, command_message=None) -> None:
    """Удаляет предыдущее главное меню клиента и сообщение с командой /start."""
    prev = _previous_menu_id(user_id)
    current_cmd = getattr(command_message, "message_id", None)
    if prev and prev != current_cmd:
        try:
            await bot.delete_message(chat_id=chat_id, message_id=prev)
        except Exception as e:
            logger.debug("Старое главное меню %s не удалено: %s", prev, e)
        _cache.pop(int(user_id), None)
    if command_message is not None:
        try:
            await command_message.delete()
        except Exception as e:
            logger.debug("Сообщение /start не удалено: %s", e)
