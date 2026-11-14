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
    if _DANGER_TX.match(text) or _DANGER_ANY.search(text) or _DANGER_CB.search(cb):
        return "danger"
    if text.startswith("🟢") or re.search(r"(вкл|✅|🟢)\s*$|:\s*(вкл|да)\b", text, re.I) or text.startswith("✅"):
        return "success"
    if _SUCCESS_TX.match(text) or _SUCCESS_CB.search(cb):
        return "success"
    return "primary"


def _pair_bonus_with_app(markup: InlineKeyboardMarkup) -> InlineKeyboardMarkup:
    """«Бонусные дни» и «Приложение» — в одной строке, в каком бы слое они ни появились.

    Кнопки могут прийти из страницы, append_buttons, расширения или prepend:
    поэтому склеиваем уже готовую клавиатуру по подписям.
    """
    rows = [list(r) for r in markup.inline_keyboard]
    bonus = next((b for row in rows for b in row if 'бонус' in (b.text or '').lower()), None)
    app = next((b for row in rows for b in row
                if b is not bonus and 'прилож' in (b.text or '').lower()), None)
    if bonus is None or app is None:
        return markup
    if any(bonus in row and app in row for row in rows):
        return markup

    changed = False
    cleaned = []
    for row in rows:
        if bonus in row:
            row = [b for b in row if b is not bonus]
            changed = True
            if not row:
                continue
        cleaned.append(row)
    app_row = next((i for i, row in enumerate(cleaned) if app in row), None)
    if app_row is None:
        return markup
    if len(cleaned[app_row]) != 1:
        return markup
    cleaned[app_row].append(bonus)
    return InlineKeyboardMarkup(inline_keyboard=cleaned) if changed else markup


def colorize(markup: InlineKeyboardMarkup) -> InlineKeyboardMarkup:
    """Новая клавиатура с единым фирменным стилем ECLIPSE: подпись, иконка и цвет."""
    from bot.services import brand_buttons, button_icons
    icons = button_icons.icon_map() if button_icons.is_enabled() else {}
    changed, rows = False, []
    for row in markup.inline_keyboard:
        new_row = []
        for b in row:
            button = b
            text = brand_buttons.normalize_text(b.text or "", b.callback_data)
            if text != (b.text or ""):
                button = b.model_copy(update={"text": text})
                changed = True
            if icons:
                with_icon = button_icons.style_button(button, icons)
                if with_icon is not button:
                    button = with_icon
                    changed = True
            style = auto_style(button)
            if style:
                button = button.model_copy(update={"style": style})
                changed = True
            new_row.append(button)
        rows.append(new_row)
    result = InlineKeyboardMarkup(inline_keyboard=rows) if changed else markup
    return _pair_bonus_with_app(result)
