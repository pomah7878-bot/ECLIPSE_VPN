"""Порядок в чате: запоминаем служебные сообщения клиента и убираем их при возврате на главную.

- UserContextMiddleware — отмечает, чьё обновление обрабатывается (только его чат и трогаем);
- TrackSentMessagesMiddleware — запоминает текстовые сообщения, которые бот отправил этому
  клиенту в ответ на его действие. Фото/QR с ключом не запоминаются и не удаляются.
"""
import logging
from typing import Any, Awaitable, Callable, Dict

from aiogram import BaseMiddleware
from aiogram.client.session.middlewares.base import BaseRequestMiddleware
from aiogram.methods import SendMessage, SendRichMessage
from aiogram.types import TelegramObject

from bot.services import main_menu_tracker as mt

logger = logging.getLogger(__name__)


class UserContextMiddleware(BaseMiddleware):
    async def __call__(
        self,
        handler: Callable[[TelegramObject, Dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: Dict[str, Any],
    ) -> Any:
        user = data.get("event_from_user")
        token = mt.current_user.set(int(user.id) if user else None)
        try:
            return await handler(event, data)
        finally:
            mt.current_user.reset(token)


class TrackSentMessagesMiddleware(BaseRequestMiddleware):
    async def __call__(self, make_request, bot, method):
        result = await make_request(bot, method)
        try:
            if isinstance(method, (SendMessage, SendRichMessage)):
                uid = mt.current_user.get()
                chat_id = method.chat_id
                if uid and isinstance(chat_id, int) and chat_id == uid:
                    message_id = getattr(result, "message_id", None)
                    if message_id:
                        mt.track_flow_message(chat_id, message_id)
        except Exception as e:
            logger.debug("Не удалось запомнить сообщение: %s", e)
        return result
