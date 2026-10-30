"""Раскрашивает кнопки исходящих сообщений по правилам bot/services/menu_style.py, когда включён «новый вид меню».

Если Telegram отклонил клавиатуру с цветами — запрос тут же повторяется без них; после нескольких отказов подряд
цвета на 15 минут отключаются (как у иконок на кнопках)."""
import logging
import time
from typing import Any

from aiogram.client.session.middlewares.base import BaseRequestMiddleware
from aiogram.exceptions import TelegramBadRequest
from aiogram.types import InlineKeyboardMarkup

logger = logging.getLogger(__name__)

FAILS_TO_PAUSE = 3
PAUSE_SECONDS = 900
_state = {"fails": 0, "paused_until": 0.0}


class ButtonColorsMiddleware(BaseRequestMiddleware):
    async def __call__(self, make_request, bot, method) -> Any:
        colored = None
        try:
            markup = getattr(method, "reply_markup", None)
            if isinstance(markup, InlineKeyboardMarkup) and time.monotonic() >= _state["paused_until"]:
                from bot.services import client_cabinet, menu_style
                if client_cabinet.enabled():
                    new_markup = menu_style.colorize(markup)
                    if new_markup is not markup:
                        colored = method.model_copy(update={"reply_markup": new_markup})
        except Exception as e:  # noqa: BLE001
            logger.debug("button colors middleware skipped: %s", e)
        if colored is None:
            return await make_request(bot, method)
        try:
            result = await make_request(bot, colored)
        except TelegramBadRequest as e:
            if "not modified" in str(e).lower():
                raise
            logger.warning("Цветные кнопки отклонены Telegram (%s) — отправляю обычные", e)
            _state["fails"] += 1
            if _state["fails"] >= FAILS_TO_PAUSE:
                _state["paused_until"] = time.monotonic() + PAUSE_SECONDS
                _state["fails"] = 0
            return await make_request(bot, method)
        _state["fails"] = 0
        return result
