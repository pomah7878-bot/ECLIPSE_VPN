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
    """HTML таблицы rich-сообщения; значения экранируются. Подпись — заголовком над таблицей,
    сама таблица компактная (меньше отступы), как в аккуратных ботах."""
    esc = html.escape
    rows = list(rows)
    head = "".join(f"<th>{esc(str(c))}</th>" for c in header) if header else ""
    body = "".join("<tr>" + "".join(f"<td>{esc(str(c))}</td>" for c in r) + "</tr>" for r in rows)
    cap = heading(caption)
    if not rows and not head:
        return cap
    return cap + "<table bordered striped compact>" + (f"<tr>{head}</tr>" if head else "") + f"{body}</table>"


def heading(text: str) -> str:
    return f"<h4>{html.escape(text)}</h4>" if text else ""


def paragraph(text: str, bold: bool = False, italic: bool = False) -> str:
    t = html.escape(text)
    if bold:
        t = f"<b>{t}</b>"
    if italic:
        t = f"<i>{t}</i>"
    return f"<p>{t}</p>"


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
    """Обычный HTML-текст экрана -> заголовок, компактная таблица и абзацы, если в нём есть хотя бы
    3 строки «Название: значение».

    Первая короткая строка — заголовок; подряд идущие пары — таблица; прочие строки — абзацы
    (короткая строка перед таблицей — жирный подзаголовок); строки-разделители отбрасываются.
    Экран остаётся как есть (None), если в нём ссылка, <pre>, длинный <code> (ключ, адрес — их
    копируют) или он слишком длинный.
    """
    import re
    if not text or len(text) > 3500 or re.search(r"<(a|pre|tg-spoiler)\b", text):
        return None
    if "├" in text or "└" in text or re.search(r"\|.*\|", text):
        return None  # деревья и строки «a | b | c» таблицей не получаются
    if any(len(c) > 40 or "://" in c for c in re.findall(r"<code>(.*?)</code>", text, re.S)):
        return None
    parsed = _parse_items(text, take_caption=True)
    if not parsed:
        return None
    caption, items = parsed
    pairs = sum(1 for it in items if it[0] == "row")
    if pairs < 3 or pairs < len(items) * 0.3 or len(items) > 30:
        return None
    return heading(caption) + _render(items, header=("Параметр", "Значение"))


def _plain(s: str) -> str:
    import re
    return html.unescape(re.sub(r"<[^>]+>", "", s)).strip()


def _parse_items(text: str, take_caption: bool) -> Optional[Tuple[str, list]]:
    """Строки экрана -> (заголовок, элементы): ("row", название, значение) или ("text", строка, курсив).
    None — слишком длинная строка."""
    import re
    global _KV
    if _KV is None:
        _KV = re.compile(r"^[^\w<]{0,4}\s*(?:<b>)?([^:<>\n]{2,30}?)(?::</b>|</b>:|:)\s*(.+)$")
    caption, items = "", []
    for ln in (l.strip() for l in text.split("\n") if l.strip()):
        pl = _plain(ln)
        if not re.search(r"\w", pl):  # ━━━ и прочие разделители
            continue
        m = _KV.match(ln)
        if m and len(_plain(m.group(2))) <= 160:
            label = _plain(ln[: m.start(2)]).rstrip(":").strip().lstrip("•-– ").strip()
            items.append(("row", label[:28], _plain(m.group(2))))
        elif take_caption and not items and not caption and len(pl) <= 60:
            caption = pl
        elif len(pl) <= 400:
            items.append(("text", pl, ln.startswith("<i>")))
        else:
            return None
    return caption, items


def _render(items: list, header: Optional[Sequence[str]] = None) -> str:
    """Подряд идущие пары — одна компактная таблица; строки текста — абзацы между таблицами."""
    out, rows = [], []
    for i, it in enumerate(items):
        if it[0] == "row":
            rows.append(it[1:])
            continue
        if rows:
            out.append(table("", rows, header=header))
            rows = []
        before_table = i + 1 < len(items) and items[i + 1][0] == "row"
        out.append(paragraph(it[1], bold=before_table and len(it[1]) <= 60, italic=it[2]))
    if rows:
        out.append(table("", rows, header=header))
    return "".join(out)


def sections_table(text: str) -> Optional[str]:
    """Длинный экран с разделами «━━ Название ━━» (например, «Диагностика») -> заголовок раздела и
    компактная таблица под ним. None — если экран так не показать (ссылки, <pre>, длинные строки, нет разделов)."""
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
        parsed = _parse_items("\n".join(buf), take_caption=(i == 0))
        if parsed is None:
            return None
        caption, items = parsed
        if items or caption:
            out.append(heading(title or caption) + _render(items))
    return "".join(out) or None
