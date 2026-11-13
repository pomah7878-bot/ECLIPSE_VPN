"""ECLIPSE brand buttons for the Telegram bot.

Telegram does not allow custom button colours or fonts: an inline button only supports the
default, primary (blue), success (green) and danger (red) styles, and a button label is plain
text. The bot therefore uses one icon vocabulary across every menu, plus the same colour rules
as the app: primary for navigation/actions, success for payments and enabling, danger for
destructive actions.

The vocabulary replaces the random emoji that different keyboards used (📋, 🛒, 🤖, ❓, …) with
one set: 🔑 subscriptions, 🛒 tariff purchase, 💳 payments, ⚡ connection, 👤 profile, 🤝 referrals,
🎧 support, 🤖 AI, ⚙️ settings, 📱 devices, etc. Status emojis (✅/❌/⚠️) and country flags are
kept, pagination and separators are never touched.
"""
from __future__ import annotations

import re
from typing import Optional

from bot.services.button_icons import split_leading_emoji

# One vocabulary for all bot menus.
ICONS = {
    "back": "⬅️",
    "home": "🏠",
    "close": "✖️",
    "keys": "🔑",
    "buy": "🛒",
    "pay": "💳",
    "balance": "💰",
    "renew": "🔄",
    "connect": "⚡",
    "trial": "🎁",
    "profile": "👤",
    "referral": "🤝",
    "support": "🎧",
    "ai": "🤖",
    "settings": "⚙️",
    "devices": "📱",
    "promo": "🏷️",
    "servers": "🌍",
    "stats": "📊",
    "info": "ℹ️",
    "site": "🌐",
    "app": "📱",
    "channel": "📢",
    "danger": "🗑️",
    "confirm": "✅",
    "cancel": "❌",
    # Admin menus use the same vocabulary but keep their specific symbols.
    "users": "👥",
    "broadcast": "📣",
    "channel": "📢",
    "license": "🪪",
    "panel": "🖥️",
    "backup": "💾",
    "logs": "📜",
    "language": "🌐",
    "history": "🧾",
    "download": "📥",
    "clock": "⏱",
    "star": "⭐",
    "attach": "📎",
    "chat": "💬",
    "more": "❓",
    "eye": "👁️",
    "rocket": "🚀",
    "ticket": "🎫",
    "edit": "✏️",
    "plus": "➕",
    "minus": "➖",
    "search": "🔍",
    "folder": "📂",
    "tools": "🛠️",
    "logout": "🚪",
    "generic": "✨",
}

_STATUS_START = re.compile(r"^[✅❌⚠️🚫🔴🟢🟡⚪⚫✔✖]")
_ONLY_EMOJI_OR_SYMBOL = re.compile(r"^[\W_]+$", re.UNICODE)


def _clean(text: str) -> str:
    """Remove the keyboard-specific leading emoji and collapse whitespace."""
    _, rest = split_leading_emoji(text or "")
    rest = re.sub(r"\s+", " ", rest or "").strip()
    return rest


def _has_flag(text: str) -> bool:
    return bool(re.match(r"^[\U0001F1E6-\U0001F1FF]{2}", text or ""))


def _is_pagination(text: str, cb: str) -> bool:
    t = text.strip()
    if not t:
        return True
    if re.fullmatch(r"[\d\s/.,:•]+", t):
        return True
    if t in {"◀", "▶", "◀️", "▶️", "‹", "›", "<", ">", "«", "»"}:
        return True
    if "page" in (cb or "").lower() and len(t) <= 4:
        return True
    return False


def _kind(text: str, cb: str) -> Optional[str]:
    """Semantic kind for a button, or None when the label must stay untouched."""
    raw = (text or "").strip()
    t = _clean(raw).lower()
    c = (cb or "").lower()
    both = f"{t} {c}"

    if not raw or _has_flag(raw) or _is_pagination(raw, c):
        return None
    # Status toggles: keep their ✅/❌/⚠️ so the current state remains obvious.
    if _STATUS_START.match(raw):
        return None
    # A separator / noop row must stay as is.
    if not re.search(r"[A-Za-zА-Яа-я0-9]", t) or c in {"noop", "none", "ignore"}:
        return None

    # navigation first: it is the most common shared label
    if re.search(r"(^|[ _-])(back|back_to|prev|previous|назад)", c) or re.match(
        r"^(назад|back|◀|‹)", t
    ):
        return "back"
    if c in {"start", "main", "main_menu", "home", "cmd_start"} or re.match(
        r"^(на главную|главная|в главное меню|home)", t
    ):
        return "home"
    if re.search(r"(^|[ _-])(cancel|close|отмена|закрыть)", both) and not re.search(
        r"(удал|delete|remove|block|ban|revoke|disable)", both
    ):
        return "close"

    # destructive before generic words
    if re.search(
        r"(удал|удален|delete|remove|block|ban|revoke|deactivate|отключ|сброс|останов|stop|danger|dangerous)",
        both,
    ):
        return "danger"

    # AI before support: the AI entry point often contains the word „поддержка“ too.
    if re.search(r"(^|[_ ])(ai|ai_support|assistant|нейро)([_ ]|$)|ии-|ai-", both):
        return "ai"
    if re.search(r"(support|help|помощ|поддерж|faq|чат|chat)", both):
        return "support"
    if "trial" in c or re.search(r"(пробн|trial|тест-период)", t):
        return "trial"
    if re.search(r"(referral|refer|реферал|приглас)", both):
        return "referral"
    if re.search(r"(device|устройств)", both):
        return "devices"
    if re.search(r"(promo|промокод|promocode|промо)", both):
        return "promo"
    if re.search(r"(servers|server|сервер|локаци)", both):
        return "servers"
    if re.search(r"(site|website|сайт|лендинг|landing)", both):
        return "site"
    if re.search(r"(app|application|приложение|клиент)", both):
        return "app"
    if re.search(r"(channel|канал)", both):
        return "channel"
    if re.search(r"(stats|statistic|статистик)", both):
        return "stats"
    if re.search(r"(settings|setting|настройк|конфиг)", both):
        return "settings"
    if re.search(r"(profile|account|profile_id|show_id|my_id|профил|аккаунт|мой id)", both):
        return "profile"
    if re.search(r"(balance|баланс)", both):
        return "balance"
    if re.search(r"(renew|extend|продл|продлен)", both):
        return "renew"
    if re.search(r"(pay|payment|topup|top_up|prepayment|qr_payment|card|crypto|оплат|пополн|платеж|сбп)", both):
        return "pay"
    if re.search(r"(buy|purchase|tariff|тариф|купить|покупк)", both):
        return "buy"
    if re.search(r"(connect|connection|config|подключ|конфиг|ключ|key|subscription|подписк)", both):
        return "keys"
    if re.search(r"(broadcast|рассылк)", both):
        return "broadcast"
    if re.search(r"(channel|канал)", both):
        return "channel"
    if re.search(r"(users|пользовател)", both):
        return "users"
    if re.search(r"(license|лиценз)", both):
        return "license"
    if re.search(r"(panel|панел)", both):
        return "panel"
    if re.search(r"(backup|бэкап|резервн)", both):
        return "backup"
    if re.search(r"(logs|лог|журнал)", both):
        return "logs"
    if re.search(r"(language|language|язык)", both):
        return "language"
    if re.match(r"^(да|yes|подтверд|confirm|принять)", t):
        return "confirm"
    if re.match(r"^(нет|no)\b", t):
        return "cancel"

    # Common submenu actions that otherwise keep their old random emoji.
    if re.search(r"(history|истори|заказ|orders|receipt|чек|квитанц)", both):
        return "history"
    if re.search(r"(download|скач|выгруз|загруз|маршрут|файл)", both):
        return "download"
    if re.search(r"(period|срок|период|дней|день|время|таймер|clock)", both):
        return "clock"
    if re.search(r"(review|отзыв|рейтинг|star|звезд)", both):
        return "star"
    if re.search(r"(attach|скриншот|screenshot|прикреп|фото)", both):
        return "attach"
    if re.search(r"(support|help|помощ|поддерж|faq)", both):
        return "support"
    if re.search(r"(chat|чат|ответ|reply|написать)", both):
        return "chat"
    if re.search(r"(more|ещё|еще|далее|next)", both):
        return "more"
    if re.search(r"(preview|превью|посмотреть|eye)", both):
        return "eye"
    if re.search(r"(launch|запуск|к запуску|rocket)", both):
        return "rocket"
    if re.search(r"(ticket|купон|билет)", both):
        return "ticket"
    if re.search(r"(edit|измен|редакт|текст|назван)", both):
        return "edit"
    if re.search(r"(add|добав|созда|new)", both):
        return "plus"
    if re.search(r"(minus|списать|убав|вычесть)", both):
        return "minus"
    if re.search(r"(search|поиск|найти|find|провер)", both):
        return "search"
    if re.search(r"(folder|папк|группа|group)", both):
        return "folder"
    if re.search(r"(tools|инструмент|диагност|провер)", both):
        return "tools"
    if re.search(r"(выйти|выход|logout|отвязать)", both):
        return "logout"

    # Nothing matched: a neutral ECLIPSE spark keeps every actionable button in one style.
    return "generic"


def normalize_text(text: str, callback_data: Optional[str]) -> str:
    """One brand-prefixed label. Unknown/admin-specific labels are returned unchanged."""
    kind = _kind(text or "", callback_data or "")
    if kind is None:
        return text
    icon = ICONS.get(kind)
    if not icon:
        return text
    body = _clean(text or "")
    if not body:
        return text
    prefix = f"{icon} "
    # Already in the brand style: do not touch it.
    if (text or "").startswith(prefix):
        return text
    result = prefix + body
    # Telegram limits inline button labels to 64 characters.
    return result if len(result) <= 64 else text


def normalize_markup(markup):
    """Return the same markup with every eligible inline button label normalised."""
    try:
        from aiogram.types import InlineKeyboardMarkup
    except Exception:  # pragma: no cover - the bot always has aiogram
        return markup
    if not isinstance(markup, InlineKeyboardMarkup):
        return markup
    changed = False
    rows = []
    for row in markup.inline_keyboard:
        new_row = []
        for b in row:
            text = normalize_text(b.text or "", b.callback_data)
            if text != (b.text or ""):
                b = b.model_copy(update={"text": text})
                changed = True
            new_row.append(b)
        rows.append(new_row)
    return InlineKeyboardMarkup(inline_keyboard=rows) if changed else markup
