"""Rich-сообщения для «нового вида меню»: таблицы и отправка с безопасным запасным вариантом.

Если Telegram не принял rich-сообщение (старый клиент, ошибка), экран показывается обычным
текстом — так же, как до включения нового вида.
"""
import html
import logging
from typing import Iterable, Optional, Sequence, Tuple

from aiogram.types import InlineKeyboardMarkup, Message

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
    from aiogram.types import InputRichMessage  # есть не во всех версиях aiogram
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
    return await safe_edit_or_send(msg, fallback_text, reply_markup=reply_markup, force_new=force_new, _no_rich=True)


_KV = None


def auto_table(text: str) -> Optional[str]:
    """Обычный HTML-текст экрана -> таблица, если в нём есть хотя бы 3 строки «Название: значение».

    Первая короткая строка — подпись таблицы; прочие строки без пары — отдельные строки таблицы;
    строки-разделители отбрасываются. Экран остаётся как есть (None), если в нём ссылка, <pre>,
    длинный <code> (ключ, адрес — их копируют) или он слишком длинный.
    """
    import re
    if not text or len(text) > 3500 or re.search(r"<(a|pre|tg-spoiler)\b", text):
        return None
    if "├" in text or "└" in text or re.search(r"\|.*\|", text):
        return None  # деревья и строки «a | b | c» таблицей не получаются
    if any(len(c) > 40 or "://" in c for c in re.findall(r"<code>(.*?)</code>", text, re.S)):
        return None
    parsed = _parse_rows(text, take_caption=True)
    if not parsed:
        return None
    caption, rows, pairs = parsed
    if pairs < 3 or pairs < len(rows) * 0.3 or len(rows) > 30:
        return None
    return table(caption, rows)


def _plain(s: str) -> str:
    import re
    return html.unescape(re.sub(r"<[^>]+>", "", s)).strip()


def _parse_rows(text: str, take_caption: bool) -> Optional[Tuple[str, list, int]]:
    """Строки экрана -> (подпись, строки таблицы, число пар «название: значение»); None — слишком длинная строка."""
    import re
    global _KV
    if _KV is None:
        _KV = re.compile(r"^[^\w<]{0,4}\s*(?:<b>)?([^:<>\n]{2,30}?)(?::</b>|</b>:|:)\s*(.+)$")
    caption, rows, pairs = "", [], 0
    for ln in (l.strip() for l in text.split("\n") if l.strip()):
        pl = _plain(ln)
        if not re.search(r"\w", pl):  # ━━━ и прочие разделители
            continue
        m = _KV.match(ln)
        if m and len(_plain(m.group(2))) <= 160:
            label = _plain(ln[: m.start(2)]).rstrip(":").strip().lstrip("•-– ").strip()
            rows.append((label[:28], _plain(m.group(2))[:160]))
            pairs += 1
        elif take_caption and not rows and not caption and len(pl) <= 60:
            caption = pl
        elif len(pl) <= 300:
            rows.append((pl, ""))  # не режем: ячейка переносит текст, длина уже ≤ 300
        else:
            return None
    return caption, rows, pairs


def sections_table(text: str) -> Optional[str]:
    """Длинный экран с разделами «━━ Название ━━» (например, «Диагностика») -> по таблице на раздел.
    None — если экран таблицами не показать (ссылки, <pre>, слишком длинные строки, нет разделов)."""
    import re
    if not text or re.search(r"<(a|pre|tg-spoiler)\b", text):
        return None
    head = re.compile(r"^\s*(?:<b>)?\s*[━─—]{2,}\s*(.+?)\s*[━─—]{2,}\s*(?:</b>)?\s*$")
    chunks, title, buf = [], "", []
    for ln in text.split("\n"):
        m = head.match(ln)
        if m:
            chunks.append((title, buf))
            title, buf = _plain(m.group(1)), []
        else:
            buf.append(ln)
    chunks.append((title, buf))
    if len(chunks) < 2:
        return None
    out = []
    for i, (title, buf) in enumerate(chunks):
        parsed = _parse_rows("\n".join(buf), take_caption=(i == 0))
        if parsed is None:
            return None
        caption, rows, _ = parsed
        if rows or caption:
            out.append(table(title or caption, rows) if rows else table(caption, [], header=None))
    return "".join(out) or None
