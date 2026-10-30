"""Цвета кнопок «нового вида меню»: правила по callback_data и подписи (v1.467).

Ставит style только кнопкам, у которых он не задан (цвета из редактора страниц не трогаются):
  success (зелёная) — оплата, пополнение, продление, подтверждение, включённый переключатель;
  primary (синяя)   — подключение, «Мои ключи», тарифы и покупка;
  danger (красная)  — удаление, сброс, остановка.
«Назад», «На главную» и остальное остаются обычными.
"""
import re
from typing import Optional

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

_DANGER_CB = re.compile(r"(^|[_:])(delete|remove|revoke)([_:]|$)")
_DANGER_TX = re.compile(r"^\W*(удалить|удаление|сбросить|остановить|заблокировать|отозвать)", re.I)
_SUCCESS_CB = re.compile(r"(^|[_:])(pay|topup|renew|confirm|activate|apply)([_:]|$)|pay_with|balance_topup|trial_activate|key_renew")
_SUCCESS_TX = re.compile(r"^\W*(оплатить|оплата|пополнить|продлить|подтвердить|активировать|купить|применить)", re.I)
_PRIMARY_CB = re.compile(r"^(my_keys|buy_key|connect|cabinet)(:|$)")
_PRIMARY_TX = re.compile(r"^\W*(подключиться|мои ключи|тарифы)", re.I)
_NAV_TX = re.compile(r"^\W*(назад|на главную|отмена|закрыть)", re.I)


def auto_style(button: InlineKeyboardButton) -> Optional[str]:
    if getattr(button, "style", None):
        return None
    text = button.text or ""
    cb = button.callback_data or ""
    if _NAV_TX.match(text) or cb in ("start", "back"):
        return None
    if text.startswith("🟢") or text.startswith("✅ Включ"):
        return "success"
    if _DANGER_TX.match(text) or _DANGER_CB.search(cb):
        return "danger"
    if _SUCCESS_TX.match(text) or _SUCCESS_CB.search(cb):
        return "success"
    if _PRIMARY_TX.match(text) or _PRIMARY_CB.match(cb):
        return "primary"
    return None


def colorize(markup: InlineKeyboardMarkup) -> InlineKeyboardMarkup:
    """Новая клавиатура с цветами; если менять нечего — тот же объект."""
    changed, rows = False, []
    for row in markup.inline_keyboard:
        new_row = []
        for b in row:
            style = auto_style(b)
            if style:
                b, changed = b.model_copy(update={"style": style}), True
            new_row.append(b)
        rows.append(new_row)
    return InlineKeyboardMarkup(inline_keyboard=rows) if changed else markup
