"""Универсальный разбор текста поста: понимает любой формат вставки.

Поддерживается:
  • форматирование Telegram (жирный, курсив, цитаты, спойлеры, ссылки, эмодзи…);
  • HTML-теги, набранные или вставленные текстом (<b>, <i>, <blockquote>, <a href>…),
    в том числе когда весь текст вставлен моноширинным блоком;
  • Markdown (**жирный**, *курсив*, ~~зачёркнутый~~, `код`, ```блок```, [текст](ссылка),
    > цитата, # заголовок, - список, ||спойлер||);
  • «чужой» HTML (<br>, <p>, <h1>, <ul><li>…) — переводится в понятный Telegram вид.
Результат всегда корректный HTML для Telegram: теги сбалансированы, лишнее убрано.
"""
import html
import re

from aiogram.utils.text_decorations import add_surrogates, html_decoration, remove_surrogates

_AUTO_ENTITY_TYPES = frozenset({"url", "mention", "hashtag", "cashtag", "bot_command", "email", "phone_number"})
_KEEP_TAGS = {
    "b", "strong", "i", "em", "u", "ins", "s", "strike", "del",
    "a", "code", "pre", "tg-spoiler", "tg-emoji", "blockquote",
}
_TG_TAG_RE = re.compile(
    r"</?(?:b|strong|i|em|u|ins|s|strike|del|a|code|pre|tg-spoiler|tg-emoji|blockquote)(?:\s[^<>]*)?>",
    re.IGNORECASE,
)
_HTML_NAMES = (
    "b|strong|i|em|u|ins|s|strike|del|a|code|pre|tg-spoiler|tg-emoji|blockquote|"
    "br|hr|h[1-6]|li|ul|ol|p|div|span|font|center|table|tr|td|th"
)
_ANY_TAG_RE = re.compile(rf"</?(?:{_HTML_NAMES})(?:\s[^<>]*)?/?>", re.IGNORECASE)
_MD_MARK_RE = re.compile(r"(\*\*[^*\n]+\*\*|__[^_\n]+__|\]\(https?://|^#{1,6}\s|^>\s|```)", re.M)


def _drop_code_wrappers(text: str, entities: list) -> list:
    """Моноширинный блок, внутри которого лежат теги или Markdown, — это не код, а
    вставленный «исходник» поста: снимаем с него code/pre, чтобы разметка сработала."""
    if not entities:
        return []
    surr = add_surrogates(text)
    total = len(surr) // 2
    kept = []
    for e in entities:
        if getattr(e, "type", None) in ("code", "pre"):
            seg = remove_surrogates(surr[e.offset * 2:(e.offset + e.length) * 2])
            covers = e.length >= 0.9 * total
            if _TG_TAG_RE.search(seg) or (covers and _MD_MARK_RE.search(seg)):
                continue
        kept.append(e)
    return kept


def _esc(s: str) -> str:
    return html.escape(s, quote=False)


def markdown_to_html(text: str) -> str:
    """Markdown → HTML Telegram. Входной текст — обычный (не экранированный)."""
    stash: list = []

    def keep(fragment: str) -> str:
        stash.append(fragment)
        return f"\x00{len(stash) - 1}\x00"

    text = re.sub(
        r"```[A-Za-z0-9_+#.-]*[ \t]*\n?(.*?)```",
        lambda m: keep("<pre>" + _esc(m.group(1).strip("\n")) + "</pre>"),
        text, flags=re.S,
    )
    text = re.sub(r"`([^`\n]+)`", lambda m: keep("<code>" + _esc(m.group(1)) + "</code>"), text)
    text = _esc(text)

    text = re.sub(r"\[([^\]\n]+)\]\((https?://[^\s)]+)\)", r'<a href="\2">\1</a>', text)
    text = re.sub(r"^[ \t]*(?:-{3,}|\*{3,}|_{3,})[ \t]*$", "──────────", text, flags=re.M)
    text = re.sub(r"^#{1,6}[ \t]+(.+?)[ \t]*#*[ \t]*$", r"<b>\1</b>", text, flags=re.M)
    text = re.sub(r"^([ \t]*)[-*+][ \t]+", r"\1• ", text, flags=re.M)
    text = re.sub(r"\*\*\*(.+?)\*\*\*", r"<b><i>\1</i></b>", text, flags=re.S)
    text = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", text, flags=re.S)
    text = re.sub(r"(?<![\w])__(?=\S)(.+?)(?<=\S)__(?![\w])", r"<b>\1</b>", text, flags=re.S)
    text = re.sub(r"(?<![\w*])\*(?=[^\s*])(.+?)(?<=[^\s*])\*(?![\w*])", r"<i>\1</i>", text)
    text = re.sub(r"(?<![\w])_(?=[^\s_])(.+?)(?<=[^\s_])_(?![\w])", r"<i>\1</i>", text)
    text = re.sub(r"~~(.+?)~~", r"<s>\1</s>", text, flags=re.S)
    text = re.sub(r"\|\|(.+?)\|\|", r"<tg-spoiler>\1</tg-spoiler>", text, flags=re.S)

    # цитаты: подряд идущие строки «> …»
    out, quote = [], []
    for line in text.split("\n"):
        m = re.match(r"^&gt;[ \t]?(.*)$", line)
        if m:
            quote.append(m.group(1))
            continue
        if quote:
            out.append("<blockquote>" + "\n".join(quote) + "</blockquote>")
            quote = []
        out.append(line)
    if quote:
        out.append("<blockquote>" + "\n".join(quote) + "</blockquote>")
    text = "\n".join(out)

    return re.sub(r"\x00(\d+)\x00", lambda m: stash[int(m.group(1))], text)


def _convert_typed_tags(escaped: str) -> str:
    """В экранированном тексте возвращает набранные теги: допустимые — как есть,
    чужие (<br>, <p>, <h1>, <li>…) — в понятный Telegram вид."""
    s = escaped
    s = re.sub(r"&lt;br\s*/?&gt;", "\n", s, flags=re.I)
    s = re.sub(r"&lt;hr\s*/?&gt;", "\n──────────\n", s, flags=re.I)
    s = re.sub(r"&lt;h[1-6](?:\s[^\n]*?)?&gt;", "<b>", s, flags=re.I)
    s = re.sub(r"&lt;/h[1-6]&gt;", "</b>\n", s, flags=re.I)
    s = re.sub(r"&lt;li(?:\s[^\n]*?)?&gt;", "• ", s, flags=re.I)
    s = re.sub(r"&lt;/(?:li|p|div|ul|ol|tr)&gt;", "\n", s, flags=re.I)
    s = re.sub(r"&lt;(?:p|div|ul|ol|table|tr|td|th|span|font|center)(?:\s[^\n]*?)?&gt;", "", s, flags=re.I)
    s = re.sub(r"&lt;/(?:span|font|center|table|td|th)&gt;", "", s, flags=re.I)
    names = "|".join(sorted(_KEEP_TAGS, key=len, reverse=True))
    s = re.sub(rf"&lt;(/?)({names})((?:\s[^\n]*?)?)&gt;", lambda m: f"<{m.group(1)}{m.group(2)}{m.group(3)}>", s, flags=re.I)
    return s


def _convert_outside_code(escaped: str) -> str:
    """Теги внутри настоящих code/pre не трогаем — это код, а не разметка."""
    parts = re.split(r"(<pre>.*?</pre>|<code>.*?</code>)", escaped, flags=re.S)
    return "".join(p if p.startswith(("<pre>", "<code>")) else _convert_typed_tags(
        re.sub(r"&amp;(#?\w+;)", r"&\1", p)
    ) for p in parts)


def _repair(fragment: str) -> str:
    """Оставляет только допустимые теги, чинит вложенность, закрывает незакрытые."""
    out, stack, pos = [], [], 0
    for m in re.finditer(r"<(/?)([a-zA-Z][a-zA-Z0-9-]*)([^<>]*)>", fragment):
        out.append(fragment[pos:m.start()])
        pos = m.end()
        closing, name, rest = m.group(1) == "/", m.group(2).lower(), m.group(3)
        if name not in _KEEP_TAGS:
            continue
        if name == "strong":
            name = "b"
        elif name == "em":
            name = "i"
        elif name == "ins":
            name = "u"
        elif name in ("strike", "del"):
            name = "s"
        if closing:
            if name in stack:
                while stack:
                    top = stack.pop()
                    out.append(f"</{top}>")
                    if top == name:
                        break
            continue
        if name == "a":
            href = re.search(r"""href\s*=\s*(?:"([^"]*)"|'([^']*)'|([^\s>]+))""", rest)
            url = next((g for g in (href.groups() if href else ()) if g), "")
            if not url:
                continue
            out.append(f'<a href="{html.escape(html.unescape(url), quote=True)}">')
        elif name == "tg-emoji":
            eid = re.search(r"emoji-id\s*=\s*[\"']?(\d+)", rest)
            if not eid:
                continue
            out.append(f'<tg-emoji emoji-id="{eid.group(1)}">')
        elif name == "blockquote":
            out.append("<blockquote expandable>" if "expandable" in rest.lower() else "<blockquote>")
        else:
            out.append(f"<{name}>")
        stack.append(name)
    out.append(fragment[pos:])
    while stack:
        out.append(f"</{stack.pop()}>")
    result = "".join(out)
    # &nbsp; и прочие именованные сущности Telegram не понимает
    result = re.sub(r"&(?!(?:amp|lt|gt|quot);)([a-zA-Z]+|#\d+|#x[0-9a-fA-F]+);",
                    lambda m: _esc(html.unescape(m.group(0))), result)
    return result


def convert_post_text(message) -> str:
    """Главная функция: сообщение админа → корректный HTML для публикации."""
    text = message.text or message.caption or ""
    entities = list(message.entities or message.caption_entities or [])
    if not text.strip():
        return ""

    entities = _drop_code_wrappers(text, entities)
    has_formatting = any(getattr(e, "type", None) not in _AUTO_ENTITY_TYPES for e in entities)
    probe = re.sub(r"```.*?```|`[^`\n]*`", "", text, flags=re.S)  # теги внутри кода не в счёт
    has_typed_tags = bool(_ANY_TAG_RE.search(probe))

    if not has_formatting and not has_typed_tags:
        result = markdown_to_html(text)
    else:
        escaped = html_decoration.unparse(text, entities)
        result = _convert_outside_code(escaped)
    result = _repair(result)
    result = re.sub(r"\n{3,}", "\n\n", result)
    return result.strip()
