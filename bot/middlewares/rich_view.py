"""«Новый вид меню» для всего бота: каждое текстовое сообщение в личный чат уходит rich-сообщением.

Текст экрана переводится в заголовки, компактные таблицы, списки и абзацы (bot/utils/rich_view.to_rich).
Если Telegram не принял rich-сообщение, повторяем исходный запрос — экран показывается как раньше.
Не трогаем: группы и каналы, сообщения с готовой разметкой (entities, Markdown), запросы внутри
rich_view.plain() (рассылки, запасной обычный текст) и уже готовые rich-сообщения.
"""
import html
import logging
from typing import Any

from aiogram.client.session.middlewares.base import BaseRequestMiddleware
from aiogram.exceptions import TelegramBadRequest
from aiogram.methods import EditMessageText, SendMessage, SendRichMessage

logger = logging.getLogger(__name__)

# Ошибки, которые повтор обычным текстом не исправит
_KEEP_ERRORS = ("not modified", "message to edit not found", "chat not found", "bot was blocked")


def _enabled() -> bool:
    from bot.services.client_cabinet import enabled
    return enabled()


def _rich_html(method, bot) -> str | None:
    if getattr(method, "entities", None):
        return None
    from aiogram.client.default import Default
    parse_mode = getattr(method, "parse_mode", None)
    if isinstance(parse_mode, Default):
        parse_mode = bot.default[parse_mode.name] if bot is not None else None
    text = method.text or ""
    if parse_mode is None:
        text = html.escape(text)
    elif str(parse_mode).upper() != "HTML":
        return None
    from bot.utils.rich_view import to_rich
    return to_rich(text)


def convert(method, bot=None):
    """Запрос -> такой же запрос rich-сообщением; None — оставить как есть."""
    from bot.utils.rich_view import is_plain
    if is_plain() or not isinstance(method, (SendMessage, EditMessageText)):
        return None
    if isinstance(method, EditMessageText) and (method.rich_message is not None or method.inline_message_id):
        return None
    chat_id = method.chat_id
    if not isinstance(chat_id, int) or chat_id <= 0 or not method.text or not _enabled():
        return None
    rich_html = _rich_html(method, bot)
    if not rich_html:
        return None
    from aiogram.types import InputRichMessage
    rich = InputRichMessage(html=rich_html)
    if isinstance(method, EditMessageText):
        return EditMessageText(chat_id=chat_id, message_id=method.message_id, rich_message=rich,
                               reply_markup=method.reply_markup,
                               business_connection_id=method.business_connection_id)
    return SendRichMessage(
        chat_id=chat_id, rich_message=rich, reply_markup=method.reply_markup,
        message_thread_id=method.message_thread_id, disable_notification=method.disable_notification,
        protect_content=method.protect_content, reply_parameters=method.reply_parameters,
        business_connection_id=method.business_connection_id, message_effect_id=method.message_effect_id,
    )


class RichViewMiddleware(BaseRequestMiddleware):
    async def __call__(self, make_request, bot, method) -> Any:
        try:
            rich = convert(method, bot)
        except Exception as e:  # noqa: BLE001 — новый вид не должен ломать отправку
            logger.debug("Новый вид: не удалось перевести сообщение: %s", e)
            rich = None
        if rich is None:
            return await make_request(bot, method)
        try:
            return await make_request(bot, rich)
        except TelegramBadRequest as e:
            if any(s in str(e).lower() for s in _KEEP_ERRORS):
                raise
            logger.info("Новый вид: Telegram не принял rich-сообщение (%s), отправляю как раньше", e)
        return await make_request(bot, method)
