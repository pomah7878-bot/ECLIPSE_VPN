"""
Выравнивание ширины «пузыря» сообщений с inline-кнопками.

Telegram делает пузырь ровно по самой длинной строке текста, поэтому меню с
короткими текстами получаются узкими, а с длинными — широкими, и при переходе
по кнопкам ширина «прыгает». Здесь к последней строке текста добавляется
невидимое заполнение (U+2800, «braille blank»), чтобы пузырь был не уже
заданной ширины. Ширина (в символах) хранится в настройке `menu_min_width`
(0 — выключено), меняется в Админка → Настройки бота → «Ширина меню».
"""
import html
import logging
import re
import time
from typing import Any, Optional

from aiogram.client.session.middlewares.base import BaseRequestMiddleware
from aiogram.methods import EditMessageText, SendMessage
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

logger = logging.getLogger(__name__)

PAD_CHAR = "⠀"
DEFAULT_MIN_WIDTH = 40
MAX_MIN_WIDTH = 80
TEXT_LIMIT = 4096

_TAG_RE = re.compile(r"<[^>]+>")
_cache: dict = {"value": None, "ts": 0.0}
_TTL = 10.0


def get_min_width() -> int:
    """Ширина из настройки с кэшем на 10 секунд (чтобы не дёргать БД на каждый запрос)."""
    now = time.monotonic()
    if _cache["value"] is not None and now - _cache["ts"] < _TTL:
        return _cache["value"]
    value = DEFAULT_MIN_WIDTH
    try:
        from database.requests import get_setting
        raw = get_setting("menu_min_width", None)
        if raw not in (None, ""):
            value = max(0, min(MAX_MIN_WIDTH, int(str(raw).strip())))
    except Exception as e:
        logger.debug("menu_min_width read failed: %s", e)
    _cache["value"], _cache["ts"] = value, now
    return value


def reset_cache() -> None:
    _cache["value"] = None


def _visible(line: str) -> str:
    return html.unescape(_TAG_RE.sub("", line)).replace(PAD_CHAR, "")


def pad_text(text: str, width: int) -> str:
    """Возвращает text с заполнением на последней строке, если самая длинная
    видимая строка короче width. Иначе — text без изменений."""
    if not text or width <= 0:
        return text
    lines = text.split("\n")
    longest = max(len(_visible(l)) for l in lines)
    if longest >= width:
        return text
    last = len(_visible(lines[-1]))
    pad = width - last
    if pad <= 0 or len(text) + pad > TEXT_LIMIT:
        return text
    return text + PAD_CHAR * pad


_NAV_STRIP = re.compile(r"[^\w]+", re.UNICODE)


def _label(btn) -> str:
    """Текст кнопки без эмодзи/стрелок/пробелов, в нижнем регистре."""
    return _NAV_STRIP.sub("", btn.text or "").lower()


def _is_home(btn) -> bool:
    return (
        btn.callback_data == "start" and _label(btn) in ("наглавную", "")
    ) or _label(btn) == "наглавную"


def _is_back(btn) -> bool:
    return _label(btn) == "назад" and bool(btn.callback_data)


def normalize_nav(markup: InlineKeyboardMarkup) -> InlineKeyboardMarkup:
    """Правило по умолчанию: «Назад» и «На главную» всегда в ОДНОЙ строке
    в самом низу клавиатуры (Назад слева, На главную справа)."""
    back = home = None
    rows = []
    for row in markup.inline_keyboard:
        kept = []
        for b in row:
            if back is None and _is_back(b):
                back = b
            elif home is None and _is_home(b):
                home = b
            elif _is_back(b) or _is_home(b):
                continue  # дубликат навигации
            else:
                kept.append(b)
        if kept:
            rows.append(kept)
    nav = [b for b in (back, home) if b is not None]
    if not nav:
        return markup
    rows.append(nav)
    if rows == [list(r) for r in markup.inline_keyboard]:
        return markup
    return InlineKeyboardMarkup(inline_keyboard=rows)


class MenuWidthMiddleware(BaseRequestMiddleware):
    async def __call__(self, make_request, bot, method) -> Any:
        try:
            markup = getattr(method, "reply_markup", None)
            if isinstance(markup, InlineKeyboardMarkup):
                new_markup = normalize_nav(markup)
                if new_markup is not markup:
                    method = method.model_copy(update={"reply_markup": new_markup})
            if (
                isinstance(method, (SendMessage, EditMessageText))
                and isinstance(method.reply_markup, InlineKeyboardMarkup)
                and isinstance(method.text, str)
            ):
                chat_id = getattr(method, "chat_id", None)
                # только личные чаты (chat_id > 0); группы/каналы не трогаем
                if chat_id is None or (isinstance(chat_id, int) and chat_id > 0):
                    new_text = pad_text(method.text, get_min_width())
                    if new_text != method.text:
                        method = method.model_copy(update={"text": new_text})
        except Exception as e:
            logger.debug("menu middleware skipped: %s", e)
        return await make_request(bot, method)
