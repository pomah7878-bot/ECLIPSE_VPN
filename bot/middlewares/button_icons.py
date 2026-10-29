"""Подменяет эмодзи в начале подписи кнопок на иконки выбранного набора (см. bot/services/button_icons.py).

Безопасность: если Telegram отклонил клавиатуру с иконками (например, у владельца бота нет Premium),
запрос тут же повторяется с обычными кнопками. После нескольких таких отказов подряд иконки на
15 минут отключаются, чтобы не делать лишние запросы."""
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


def _paused() -> bool:
    return time.monotonic() < _state["paused_until"]


def _note_failure() -> None:
    _state["fails"] += 1
    if _state["fails"] >= FAILS_TO_PAUSE:
        _state["paused_until"] = time.monotonic() + PAUSE_SECONDS
        _state["fails"] = 0
        logger.warning("Иконки на кнопках: Telegram отклоняет их %s раза подряд — отключены на %s мин. "
                       "Проверьте набор и Premium у владельца бота.", FAILS_TO_PAUSE, PAUSE_SECONDS // 60)


class ButtonIconsMiddleware(BaseRequestMiddleware):
    async def __call__(self, make_request, bot, method) -> Any:
        styled_method = None
        try:
            from bot.services import button_icons
            markup = getattr(method, "reply_markup", None)
            if isinstance(markup, InlineKeyboardMarkup) and not _paused() and button_icons.active():
                styled = button_icons.style_markup(markup, button_icons.icon_map())
                if styled is not markup:
                    styled_method = method.model_copy(update={"reply_markup": styled})
        except Exception as e:  # noqa: BLE001
            logger.debug("button icons middleware skipped: %s", e)
        if styled_method is None:
            return await make_request(bot, method)
        try:
            result = await make_request(bot, styled_method)
        except TelegramBadRequest as e:
            if "not modified" in str(e).lower():
                raise
            logger.warning("Кнопки с иконками отклонены Telegram (%s) — отправляю обычные", e)
            _note_failure()
            return await make_request(bot, method)
        _state["fails"] = 0
        return result
