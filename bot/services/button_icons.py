"""Иконки на кнопках (стиль «контурные иконки»).

Telegram умеет показывать перед текстом кнопки иконку из набора пользовательских эмодзи
(поле icon_custom_emoji_id). Здесь админ выбирает набор, а бот сам подменяет обычный эмодзи
в начале подписи кнопки на иконку из этого набора с тем же значением.

Настройки (settings):
  button_icons_enabled — "1" включено, по умолчанию выключено;
  button_icon_pack     — короткое имя набора (t.me/addemoji/<имя>);
  button_icon_map      — JSON {эмодзи: id иконки}, собирается при выборе набора.

Подмена делается в одном месте — bot/middlewares/button_icons.py, на любых кнопках бота.
Эмодзи-статусы (🟢 🔴 ⚪ ✅ ❌ и т.п.) не трогаются: по ним админ видит, включено ли что-то.
"""
import json
import logging
import re
from typing import Dict, Optional, Tuple

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

logger = logging.getLogger(__name__)

KEY_ENABLED = "button_icons_enabled"
KEY_PACK = "button_icon_pack"
KEY_MAP = "button_icon_map"

_VS16 = "️"
_ZWJ = "‍"
_KEYCAP = "⃣"
# Эмодзи-статусы: остаются обычными (после удаления U+FE0F)
STATUS_EMOJI = frozenset("🟢🔴⚪🟡🔵🟠🟣⚫✅❌⛔⚠🚧🔘🔕🔔")


def _is_emoji_start(ch: str) -> bool:
    cp = ord(ch)
    return (
        0x1F000 <= cp <= 0x1FAFF or 0x2190 <= cp <= 0x21FF or 0x2300 <= cp <= 0x23FF
        or 0x2460 <= cp <= 0x27BF or 0x2900 <= cp <= 0x297F or 0x2B00 <= cp <= 0x2BFF
        or cp in (0xA9, 0xAE, 0x203C, 0x2049, 0x2122, 0x2139, 0x3030, 0x303D, 0x3297, 0x3299)
    )


def split_leading_emoji(text: str) -> Tuple[str, str]:
    """('эмодзи', 'остаток подписи без пробела') либо ('', text), если текст не начинается с эмодзи.
    Ключ эмодзи без U+FE0F, чтобы «⬅️» и «⬅» совпадали."""
    if not text or not _is_emoji_start(text[0]):
        return "", text
    i = 1
    while i < len(text):
        ch = text[i]
        if ch in (_VS16, _KEYCAP) or 0x1F3FB <= ord(ch) <= 0x1F3FF or 0xE0020 <= ord(ch) <= 0xE007F:
            i += 1
        elif ch == _ZWJ and i + 1 < len(text) and _is_emoji_start(text[i + 1]):
            i += 2
        elif 0x1F1E6 <= ord(text[i - 1]) <= 0x1F1E6 + 25 and 0x1F1E6 <= ord(ch) <= 0x1F1E6 + 25 and i == 1:
            i += 1  # флаг: две региональные буквы
        else:
            break
    key = text[:i].replace(_VS16, "")
    return key, text[i:].lstrip()


# ── настройки ────────────────────────────────────────────────────────────────

def _get(key: str, default: str = "") -> str:
    from database.requests import get_setting
    value = get_setting(key, default)
    return default if value is None else str(value)


def is_enabled() -> bool:
    return _get(KEY_ENABLED, "0") == "1"


def set_enabled(value: bool) -> None:
    from database.requests import set_setting
    set_setting(KEY_ENABLED, "1" if value else "0")


def pack_name() -> str:
    return _get(KEY_PACK, "").strip()


_map_cache: Dict[str, object] = {"raw": None, "map": {}}


def icon_map() -> Dict[str, str]:
    raw = _get(KEY_MAP, "")
    if raw != _map_cache["raw"]:
        try:
            data = json.loads(raw) if raw else {}
            parsed = {str(k): str(v) for k, v in data.items()} if isinstance(data, dict) else {}
        except (ValueError, TypeError):
            parsed = {}
        _map_cache["raw"], _map_cache["map"] = raw, parsed
    return _map_cache["map"]  # type: ignore[return-value]


def active() -> bool:
    return is_enabled() and bool(icon_map())


# ── набор иконок ─────────────────────────────────────────────────────────────

_NAME_RE = re.compile(r"^[A-Za-z0-9_]{1,64}$")


def parse_pack_name(text: str) -> Optional[str]:
    """Имя набора из ссылки (t.me/addemoji/имя, tg://addemoji?set=имя) или просто имени."""
    value = (text or "").strip()
    m = re.search(r"(?:addemoji/|[?&]set=)([A-Za-z0-9_]+)", value)
    if m:
        value = m.group(1)
    return value if _NAME_RE.match(value) else None


def build_map(stickers) -> Dict[str, str]:
    """{эмодзи: custom_emoji_id} по стикерам набора; при повторе берётся первая иконка."""
    out: Dict[str, str] = {}
    for st in stickers:
        cid = getattr(st, "custom_emoji_id", None)
        key, _ = split_leading_emoji(getattr(st, "emoji", "") or "")
        if cid and key and key not in STATUS_EMOJI and key not in out:
            out[key] = str(cid)
    return out


async def load_pack(bot, name: str) -> Dict[str, str]:
    """Загружает набор у Telegram и сохраняет его в настройки. Бросает ValueError, если это не
    набор пользовательских эмодзи или в нём нет подходящих иконок."""
    from database.requests import set_setting
    sticker_set = await bot.get_sticker_set(name)
    if getattr(sticker_set, "sticker_type", None) != "custom_emoji":
        raise ValueError("это набор обычных стикеров, нужен набор пользовательских эмодзи (t.me/addemoji/…)")
    mapping = build_map(sticker_set.stickers)
    if not mapping:
        raise ValueError("в наборе нет иконок с понятным эмодзи")
    set_setting(KEY_PACK, name)
    set_setting(KEY_MAP, json.dumps(mapping, ensure_ascii=False))
    return mapping


# ── подмена на кнопках ───────────────────────────────────────────────────────

def style_button(btn: InlineKeyboardButton, mapping: Dict[str, str]) -> InlineKeyboardButton:
    if btn.web_app is not None or getattr(btn, "icon_custom_emoji_id", None):
        return btn
    key, rest = split_leading_emoji(btn.text or "")
    if not key or not rest or key in STATUS_EMOJI:
        return btn
    icon = mapping.get(key)
    if not icon:
        return btn
    return btn.model_copy(update={"text": rest, "icon_custom_emoji_id": icon})


def style_markup(markup: InlineKeyboardMarkup, mapping: Dict[str, str]) -> InlineKeyboardMarkup:
    """Новая клавиатура с иконками; если менять нечего — возвращает тот же объект."""
    changed = False
    rows = []
    for row in markup.inline_keyboard:
        new_row = []
        for b in row:
            nb = style_button(b, mapping)
            changed = changed or nb is not b
            new_row.append(nb)
        rows.append(new_row)
    return InlineKeyboardMarkup(inline_keyboard=rows) if changed else markup
