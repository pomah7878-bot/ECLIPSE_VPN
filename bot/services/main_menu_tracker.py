"""Одно главное меню в чате: запоминаем последнее и убираем его при новом /start.

Раньше каждый /start присылал новое меню, и старые копились в чате. Теперь при /start
предыдущее меню клиента и прежняя команда удаляются, а новое меню появляется внизу чата.
Самая последняя команда /start остаётся: без сообщений клиента Telegram показывает кнопку «Старт».
v1.505: /start чистит и всю остальную переписку клиента (clean_chat_history) — в чате остаются
только /start и главное меню. Чаты админов не чистятся.
"""
import asyncio
import logging
from contextvars import ContextVar
from typing import Dict, Iterable, List, Optional

logger = logging.getLogger(__name__)

_SETTING_PREFIX = "main_menu_msg:"
_CMD_PREFIX = "start_cmd_msg:"
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


_CLEAN_PREFIX = "chat_clean_from:"
_CLEAN_DEPTH = 300  # при первой очистке — столько последних сообщений назад
_CLEAN_BATCH = 100  # deleteMessages принимает до 100 id за раз


async def clean_chat_history(bot, chat_id: int, user_id: int, current_id: int) -> None:
    """/start: чистит переписку клиента — всё до текущей команды (она остаётся, см. ниже).
    Удаляет пачками по 100; несуществующие id Telegram пропускает, сообщения старше 48 часов
    боту удалить нельзя — они остаются. Чаты админов не трогаем: там уведомления об оплатах и сбоях."""
    try:
        from config import ADMIN_IDS
        if int(user_id) in {int(a) for a in ADMIN_IDS} or int(chat_id) != int(user_id) or not current_id:
            return
        from database.db_settings import get_setting, set_setting
        key = f"{_CLEAN_PREFIX}{int(user_id)}"
        start = max(1, int(get_setting(key) or 0), int(current_id) - _CLEAN_DEPTH)
        ids = list(range(start, int(current_id)))
        for i in range(0, len(ids), _CLEAN_BATCH):
            try:
                await bot.delete_messages(chat_id=chat_id, message_ids=ids[i:i + _CLEAN_BATCH])
            except Exception as e:  # старые (>48 ч) или уже удалённые — не страшно
                logger.debug("Очистка чата %s: пачка с %s не удалена: %s", chat_id, ids[i], e)
        # следующая очистка начнётся с этой команды /start (её уберём в следующий раз)
        set_setting(key, str(int(current_id)))
        _trail.pop(int(chat_id), None)
    except Exception as e:
        logger.debug("Очистка чата %s не выполнена: %s", chat_id, e)


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
        # v1.311: текущее /start остаётся в чате (в диалоге должно быть хотя бы одно сообщение
        # клиента, иначе Telegram на iPhone показывает синюю кнопку «Старт» вместо поля ввода).
        # Удаляется только предыдущее /start — больше одного в чате не копится.
        try:
            from database.db_settings import get_setting, set_setting
            key = f"{_CMD_PREFIX}{int(user_id)}"
            prev_cmd = int(get_setting(key) or 0)
            current = int(getattr(command_message, "message_id", 0) or 0)
            if prev_cmd and prev_cmd != current:
                try:
                    await bot.delete_message(chat_id=chat_id, message_id=prev_cmd)
                except Exception as e:
                    logger.debug("Прежнее /start %s не удалено: %s", prev_cmd, e)
            if current:
                set_setting(key, str(current))
        except Exception as e:
            logger.debug("Не удалось обработать сообщение /start: %s", e)
        # Вся остальная переписка клиента тоже убирается: в чате остаются только /start и главное меню
        await clean_chat_history(bot, chat_id, user_id, int(getattr(command_message, "message_id", 0) or 0))
