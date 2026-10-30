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

_DANGER_CB = re.compile(r"(^|[_:])(delete|remove|revoke|deactivate|block|ban|reset|stop)([_:]|$)")
_DANGER_TX = re.compile(r"^\W*(удалить|удаление|сбросить|остановить|заблокировать|отозвать|деактивировать|отключить|очистить|забанить|отменить заказ)", re.I)
_SUCCESS_CB = re.compile(r"(^|[_:])(pay|topup|renew|confirm|activate|apply|add|create|save|enable)([_:]|$)|pay_with|balance_topup|trial_activate|key_renew")
_SUCCESS_TX = re.compile(r"^\W*(оплатить|оплата|пополнить|продлить|подтвердить|активировать|купить|применить|добавить|создать|сохранить|включить|выдать|запустить|отправить|начислить)", re.I)
_NAV_TX = re.compile(r"^\W*(назад|на главную|отмена|отменить|закрыть|нет\b|⬅)", re.I)
_DANGER_ANY = re.compile(r"удал|сброс|деактив|отключ|выключить|заблок|останов|отозв|очист", re.I)


def auto_style(button: InlineKeyboardButton) -> Optional[str]:
    """Цвет кнопки: красная — опасное, зелёная — добавить/оплатить/включить, синяя — всё остальное с действием.
    «Назад», «На главную», кнопки-ссылки и кнопки с уже заданным цветом не трогаются."""
    if getattr(button, "style", None):
        return None
    text = button.text or ""
    cb = button.callback_data or ""
    if not cb or _NAV_TX.match(text) or cb in ("start", "back", "noop"):
        return None
    if re.search(r"(выкл|❌|🔴)\s*$|:\s*(выкл|нет)", text, re.I):
        return "danger"
    if text.startswith("🟢") or re.search(r"(вкл|✅|🟢)\s*$|:\s*(вкл|да)\b", text, re.I) or text.startswith("✅"):
        return "success"
    if _DANGER_TX.match(text) or _DANGER_ANY.search(text) or _DANGER_CB.search(cb):
        return "danger"
    if _SUCCESS_TX.match(text) or _SUCCESS_CB.search(cb):
        return "success"
    return "primary"


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
