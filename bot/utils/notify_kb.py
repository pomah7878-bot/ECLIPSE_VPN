"""Клавиатуры для уведомлений: кнопка «🈴 На главную» должна быть под каждым уведомлением,
чтобы из любого сообщения можно было вернуться на главный экран."""
from __future__ import annotations

from typing import Any, Optional

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

HOME_CALLBACK = "start"
HOME_TEXT = "🈴 На главную"


def home_button() -> InlineKeyboardButton:
    return InlineKeyboardButton(text=HOME_TEXT, callback_data=HOME_CALLBACK)


def has_home(markup: Any) -> bool:
    rows = getattr(markup, "inline_keyboard", None) or []
    return any(getattr(b, "callback_data", None) == HOME_CALLBACK for row in rows for b in row)


def with_home(markup: Optional[Any] = None) -> InlineKeyboardMarkup:
    """Возвращает клавиатуру с кнопкой «На главную» в конце (не дублирует, если уже есть).
    Не inline-клавиатуры (ReplyKeyboardRemove и т.п.) заменяются на одну кнопку «На главную»."""
    rows = []
    if isinstance(markup, InlineKeyboardMarkup):
        if has_home(markup):
            return markup
        rows = [list(r) for r in markup.inline_keyboard]
    rows.append([home_button()])
    return InlineKeyboardMarkup(inline_keyboard=rows)
