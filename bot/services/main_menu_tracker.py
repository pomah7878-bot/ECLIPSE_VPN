"""Одно главное меню в чате: запоминаем последнее и убираем его при новом /start.

Раньше каждый /start присылал новое меню, и старые копились в чате. Теперь при /start
предыдущее меню клиента и сама команда удаляются, а новое появляется внизу чата.
"""
import asyncio
import logging
from contextvars import ContextVar
from typing import Dict, Iterable, List, Optional

logger = logging.getLogger(__name__)

_SETTING_PREFIX = "main_menu_msg:"
_cache: Dict[int, int] = {}

# Служебные сообщения «хвоста» (страницы оплаты, статусы, вопросы и т.п.), которые бот
# прислал клиенту как НОВЫЕ сообщения. При возврате на главную они убираются.
_trail: Dict[int, List[int]] = {}
_MAX_TRAIL = 40

# Сообщения с личным ключом (файл .conf, QR AmneziaWG/WireGuard). Удаляются при любом
# возврате на главную, даже если клиент ушёл командой /start, не нажимая кнопки под QR.
# Хранятся и в БД — переживают перезапуск бота.
_SENS_PREFIX = "sensitive_msgs:"
_MAX_SENSITIVE = 20

# Пользователь, чьё обновление обрабатывается сейчас (чтобы не трогать уведомления
# другим людям и сообщения из фоновых задач)
current_user: ContextVar[Optional[int]] = ContextVar("current_user", default=None)


def track_flow_message(chat_id: int, message_id: int) -> None:
    lst = _trail.setdefault(int(chat_id), [])
    if message_id not in lst:
        lst.append(int(message_id))
        del lst[:-_MAX_TRAIL]


def _load_sensitive(chat_id: int) -> List[int]:
    try:
        from database.db_settings import get_setting
        raw = get_setting(f"{_SENS_PREFIX}{int(chat_id)}") or ""
        return [int(x) for x in str(raw).split(",") if x.strip().isdigit()]
    except Exception:
        return []


def _save_sensitive(chat_id: int, ids: List[int]) -> None:
    try:
        from database.db_settings import set_setting
        set_setting(f"{_SENS_PREFIX}{int(chat_id)}", ",".join(str(i) for i in ids[-_MAX_SENSITIVE:]))
    except Exception as e:
        logger.debug("Не удалось сохранить сообщения с ключом %s: %s", chat_id, e)


def track_sensitive(chat_id: int, *messages) -> None:
    """Запоминает сообщения с личным ключом, чтобы убрать их при возврате на главную."""
    ids = _load_sensitive(chat_id)
    for m in messages:
        mid = getattr(m, "message_id", m)
        if mid and int(mid) not in ids:
            ids.append(int(mid))
    _save_sensitive(chat_id, ids)


def menu_id(user_id: int) -> Optional[int]:
    return _previous_menu_id(user_id)


def is_known_message(user_id: int, message_id: int) -> bool:
    """Сообщение — наше главное меню или страница из текущего «хвоста»."""
    return message_id == _previous_menu_id(user_id) or message_id in _trail.get(int(user_id), [])


async def _delete_many(bot, chat_id: int, ids: Iterable[int]) -> None:
    async def _one(i):
        try:
            await bot.delete_message(chat_id=chat_id, message_id=i)
        except Exception as e:
            logger.debug("Сообщение %s не удалено: %s", i, e)
    ids = list(ids)
    if ids:
        await asyncio.gather(*[_one(i) for i in ids])


async def purge_flow(bot, chat_id: int, keep: Iterable[int] = ()) -> None:
    """Удаляет накопившиеся служебные сообщения клиента, кроме перечисленных в keep."""
    keep = {int(k) for k in keep if k}
    ids = _trail.get(int(chat_id), [])
    _trail[int(chat_id)] = [i for i in ids if i in keep]
    sensitive = _load_sensitive(chat_id)
    if sensitive:
        _save_sensitive(chat_id, [i for i in sensitive if i in keep])
    todo = [i for i in ids if i not in keep] + [i for i in sensitive if i not in keep and i not in ids]
    await _delete_many(bot, chat_id, todo)


async def delete_old_menu(bot, chat_id: int, old_id: Optional[int], keep_id: Optional[int]) -> None:
    """Удаляет прежнее главное меню, если оно не совпадает с текущим."""
    if old_id and old_id != keep_id:
        await _delete_many(bot, chat_id, [old_id])


def remember_main_menu(user_id: int, message) -> None:
    """Запоминает сообщение с главным меню (id хранится и в БД — переживает перезапуск)."""
    try:
        message_id = int(getattr(message, "message_id", 0) or 0)
        if not user_id or not message_id:
            return
        # меню — уже не «хвост»
        if message_id in _trail.get(int(user_id), []):
            _trail[int(user_id)].remove(message_id)
        if _cache.get(int(user_id)) == message_id:
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
