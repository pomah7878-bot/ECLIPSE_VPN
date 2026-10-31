"""Rich-сообщения для «нового вида меню»: таблицы и отправка с безопасным запасным вариантом.

Если Telegram не принял rich-сообщение (старый клиент, ошибка), экран показывается обычным
текстом — так же, как до включения нового вида. Весь остальной текст бота переводится в новый вид
функцией to_rich (через bot/middlewares/rich_view.py).
"""
import contextlib
import contextvars
import html
import logging
import re
from typing import Iterable, Optional, Sequence

from aiogram.types import InlineKeyboardMarkup, Message

logger = logging.getLogger(__name__)

# Внутри plain() сообщения уходят как есть: рассылки, запасной обычный текст.
_PLAIN = contextvars.ContextVar("rich_view_plain", default=False)


@contextlib.contextmanager
def plain():
    token = _PLAIN.set(True)
    try:
        yield
    finally:
        _PLAIN.reset(token)


def is_plain() -> bool:
    return _PLAIN.get()


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


def heading(text: str, level: int = 4) -> str:
    return f"<h{level}>{html.escape(text)}</h{level}>" if text else ""


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
    with plain():
        return await safe_edit_or_send(msg, fallback_text, reply_markup=reply_markup, force_new=force_new)


# ------------------------------------------------------------------ текст бота -> новый вид

_TAG = re.compile(r"<(/?)([a-zA-Z][\w-]*)[^>]*?(/?)>")
_VOID = {"br"}
_KV = re.compile(r"^[^\w<]{0,4}\s*(?:<b>)?([^:<>\n]{2,30}?)(?::</b>|</b>:|:)\s*(.+)$")
_SECTION = re.compile(r"^\s*(?:<b>)?\s*[━─—=]{2,}\s*(.+?)\s*[━─—=]{2,}\s*(?:</b>)?\s*$")
_WHOLE_BOLD = re.compile(r"^[^\w<]{0,6}\s*<b>[^<]*</b>\s*:?$")
_BLOCK = re.compile(r"<(blockquote|pre)\b[^>]*>.*?</\1>", re.S)
_BULLETS = "◆◇•▪▫●○-–*✓✔➤►▶"


def _plain(s: str) -> str:
    return html.unescape(re.sub(r"<[^>]+>", "", s)).strip()


def _balanced(s: str) -> bool:
    stack = []
    for closing, name, selfclose in _TAG.findall(s):
        name = name.lower()
        if selfclose or name in _VOID:
            continue
        if not closing:
            stack.append(name)
        elif not stack or stack.pop() != name:
            return False
    return not stack


def _is_sep(line: str) -> bool:
    return bool(line) and not re.search(r"\w", _plain(line))


def _pair(line: str):
    """«Название: значение» -> (название, значение HTML) или None."""
    m = _KV.match(line)
    if not m:
        return None
    label = _plain(line[: m.start(2)]).rstrip(":").strip().lstrip("•-– ").strip()
    value = m.group(2).strip()
    if (not re.search(r"[^\W\d_]", label) or value.startswith("//") or len(_plain(value)) > 200
            or not _plain(value)):
        return None
    if not _balanced(value):
        value = html.escape(_plain(value))
    return html.escape(label), value


def to_rich(text: str) -> Optional[str]:
    """Обычный экран бота (HTML Telegram) -> новый вид: заголовки, компактные таблицы, списки, абзацы.

    - первая короткая строка — заголовок экрана; «━━ Раздел ━━», строка между «━━━» или
      короткая целиком жирная строка — заголовок раздела;
    - две и больше строк подряд «Название: значение» — компактная таблица;
    - строки с маркером (◆ • - ✓) — список; цитаты и блоки кода переносятся как есть;
    - остальное — абзацы; оформление внутри строк (жирный, ссылки, <code>) сохраняется.
    None — если текст так не перевести (незакрытые теги): тогда он уходит как есть.
    """
    if not text or not text.strip():
        return None
    blocks: dict = {}

    def keep(m):
        key = f"\x00{len(blocks)}\x00"
        body = m.group(0)
        if m.group(1) == "blockquote":
            body = body.strip().replace("\n", "<br>")
        blocks[key] = body
        return f"\n{key}\n"

    src = _BLOCK.sub(keep, text)
    lines = [ln.strip() for ln in src.split("\n")]
    if not all(_balanced(ln) for ln in lines if ln and ln not in blocks):
        return None

    out, para, rows, items = [], [], [], []

    def flush_para():
        if para:
            out.append("<p>" + "<br>".join(para) + "</p>")
            para.clear()

    def flush_rows():
        if len(rows) >= 2:
            flush_para()
            out.append("<table bordered striped compact>"
                       + "".join(f"<tr><td>{a}</td><td>{b}</td></tr>" for a, b, _ in rows) + "</table>")
        elif rows:
            para.append(rows[0][2])  # одна пара — обычной строкой того же абзаца
        rows.clear()

    def flush_items():
        if items:
            out.append("<ul>" + "".join(f"<li>{x}</li>" for x in items) + "</ul>")
            items.clear()

    def flush():
        flush_rows()
        flush_items()
        flush_para()

    first = next((i for i, ln in enumerate(lines) if ln and not _is_sep(ln)), None)
    for i, ln in enumerate(lines):
        if not ln or _is_sep(ln):
            flush()
            continue
        if ln in blocks:
            flush()
            out.append(blocks[ln])
            continue
        pl = _plain(ln)
        nxt = lines[i + 1] if i + 1 < len(lines) else ""
        prev = lines[i - 1] if i > 0 else ""
        section = _SECTION.match(ln)
        short = len(pl) <= 60 and not pl.endswith((".", "!", "?", "…", ","))
        letters = re.sub(r"[^\w]|\d|_", "", pl)
        caps = len(letters) >= 4 and letters.isupper() and len(pl) <= 60  # «🚀 БЫСТРОЕ ПОДКЛЮЧЕНИЕ»
        if section:
            flush()
            out.append(heading(_plain(section.group(1))))
            continue
        if i == first and len(pl) <= 60 and not pl.endswith((".", ",", "…")) and (
                not nxt or _is_sep(nxt) or _WHOLE_BOLD.match(ln)):
            flush()
            out.append(heading(pl.rstrip(":").strip(), 3))
            continue
        if (short and (_WHOLE_BOLD.match(ln) or (_is_sep(prev) and _is_sep(nxt)))) or caps:
            flush()
            out.append(heading(pl.rstrip(":").strip()))
            continue
        pair = _pair(ln)
        if pair:
            flush_items()
            rows.append((pair[0], pair[1], ln))
            continue
        flush_rows()
        if ln[0] in _BULLETS and len(ln) > 1 and ln[1] in "  ":
            flush_para()
            items.append(ln[1:].strip())
            continue
        flush_items()
        para.append(ln)
    flush()
    return "".join(out) or None
