"""Метка «Powered by ECLIPSE».

По умолчанию показывается в боте (нижняя кнопка главного экрана) и на сайте-витрине.
Администратор инсталляции, которому поставщик выдал право «отключение метки»,
может отключить её командой /powered_by off. Без лицензии метку отключить нельзя.
"""
import html
import logging

logger = logging.getLogger(__name__)

POWERED_BY_TEXT = "Powered by ECLIPSE"
POWERED_BY_URL = "https://github.com/pomah7878-bot/ECLIPSE_VPN"
_SETTING_KEY = "powered_by_hidden"


def can_hide_powered_by() -> bool:
    """Отключить метку можно, если в лицензии включено право hide_powered_by
    (его выдаёт поставщик кнопкой в карточке лицензии; на главном сервере оно есть всегда)."""
    try:
        from bot.services.license import get_enabled_features, HIDE_POWERED_BY_FEATURE
        return HIDE_POWERED_BY_FEATURE in get_enabled_features()
    except Exception as e:
        logger.warning(f"Powered by: не удалось проверить лицензию ({e}) — метка остаётся")
        return False


def is_powered_by_visible() -> bool:
    """Метка видна, если её не отключили ИЛИ лицензии нет (без лицензии отключение не действует)."""
    try:
        from database.requests import get_setting
        hidden = (get_setting(_SETTING_KEY, "0") or "0") == "1"
    except Exception:
        return True
    return (not hidden) or (not can_hide_powered_by())


def set_powered_by_hidden(hidden: bool) -> bool:
    """Сохраняет выбор. Скрыть можно только с лицензией; показать — всегда. Возвращает успех."""
    from database.requests import set_setting
    if hidden and not can_hide_powered_by():
        return False
    set_setting(_SETTING_KEY, "1" if hidden else "0")
    return True


def powered_by_html() -> str:
    """Подвал для сайта-витрины (пустая строка, если метка отключена)."""
    if not is_powered_by_visible():
        return ""
    return (
        '<div style="text-align:center;padding:18px 12px 28px;font-size:12px;opacity:.6">'
        f'<a href="{html.escape(POWERED_BY_URL)}" target="_blank" rel="noopener" '
        'style="color:inherit;text-decoration:none">⚡ ' + html.escape(POWERED_BY_TEXT) + '</a></div>'
    )
