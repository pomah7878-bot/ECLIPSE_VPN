"""Rich-сообщения для «нового вида меню»: таблицы и отправка с безопасным запасным вариантом.

Если Telegram не принял rich-сообщение (старый клиент, ошибка), экран показывается обычным
текстом — так же, как до включения нового вида.
"""
import html
import logging
from typing import Iterable, Optional, Sequence, Tuple

from aiogram.types import InlineKeyboardMarkup, InputRichMessage, Message

logger = logging.getLogger(__name__)


def table(caption: str, rows: Iterable[Sequence[str]], header: Optional[Sequence[str]] = ("Параметр", "Значение")) -> str:
    """HTML таблицы rich-сообщения; значения экранируются."""
    esc = html.escape
    head = "".join(f"<th>{esc(str(c))}</th>" for c in header) if header else ""
    body = "".join("<tr>" + "".join(f"<td>{esc(str(c))}</td>" for c in r) + "</tr>" for r in rows)
    cap = f"<caption>{esc(caption)}</caption>" if caption else ""
    return f"<table bordered striped>{cap}" + (f"<tr>{head}</tr>" if head else "") + f"{body}</table>"


async def send(msg: Message, rich_html: str, fallback_text: str, reply_markup: Optional[InlineKeyboardMarkup],
               force_new: bool = False) -> Optional[Message]:
    """Показывает экран rich-сообщением. Редактирует текущее сообщение; если нельзя — шлёт новое и
    удаляет старое; если Telegram отказал совсем — обычный текст."""
    rich = InputRichMessage(html=rich_html)
    if not force_new:
        try:
            result = await msg.bot.edit_message_text(
                chat_id=msg.chat.id, message_id=msg.message_id, rich_message=rich, reply_markup=reply_markup)
            if isinstance(result, Message):
                return result
            return msg
        except Exception as e:  # noqa: BLE001
            if "not modified" in str(e).lower():
                return msg
            logger.debug("rich_view: правка не удалась (%s), отправляю новое", e)
    try:
        sent = await msg.answer_rich(rich, reply_markup=reply_markup)
        if not force_new:
            try:
                await msg.delete()
            except Exception:  # noqa: BLE001
                pass
        return sent
    except Exception as e:  # noqa: BLE001
        logger.warning("rich_view: rich-сообщение не отправлено, обычный текст: %s", e)
    from bot.utils.text import safe_edit_or_send
    return await safe_edit_or_send(msg, fallback_text, reply_markup=reply_markup, force_new=force_new)
