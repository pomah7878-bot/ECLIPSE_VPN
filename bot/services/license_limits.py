"""Бесплатные лимиты установки без лицензии.

Без лицензии (нет ни одной платной функции): до 2 активных серверов и до 100 активных ключей.
Режимы (команда /license_limits):
  warn    — по умолчанию: ничего не блокируется, админам раз в сутки приходит предупреждение;
  enforce — нельзя добавить сервер сверх лимита (ключи клиентов НЕ блокируются никогда);
  off     — лимиты выключены.
Главный сервер и любая лицензия с платной функцией лимитов не имеют.
"""
import logging
from datetime import datetime, timedelta

logger = logging.getLogger(__name__)

FREE_MAX_SERVERS = 2
FREE_MAX_KEYS = 100


def is_licensed() -> bool:
    try:
        from bot.services.license import get_enabled_features
        return bool(get_enabled_features())
    except Exception:
        return True  # при сомнении — не ограничиваем


def limits_mode() -> str:
    from database.requests import get_setting
    mode = (get_setting("license_limits_mode", "warn") or "warn").strip().lower()
    return mode if mode in ("warn", "enforce", "off") else "warn"


def set_limits_mode(mode: str) -> None:
    from database.requests import set_setting
    set_setting("license_limits_mode", mode)


def count_servers() -> int:
    from database.connection import get_db
    with get_db() as conn:
        return conn.execute("SELECT COUNT(*) FROM servers WHERE is_active = 1").fetchone()[0]


def count_keys() -> int:
    from database.connection import get_db
    with get_db() as conn:
        return conn.execute("SELECT COUNT(*) FROM vpn_keys WHERE expires_at > datetime('now')").fetchone()[0]


def is_over_limits() -> bool:
    return count_servers() > FREE_MAX_SERVERS or count_keys() > FREE_MAX_KEYS


def limits_line() -> str:
    """Строка для экрана «Моя лицензия» (пусто, если лицензия есть или лимиты выключены)."""
    try:
        if is_licensed() or limits_mode() == "off":
            return ""
        s, k = count_servers(), count_keys()
        warn = " ⚠️ лимит превышен" if (s > FREE_MAX_SERVERS or k > FREE_MAX_KEYS) else ""
        return f"\n\n📊 Без лицензии: серверы {s} из {FREE_MAX_SERVERS}, активные ключи {k} из {FREE_MAX_KEYS}.{warn}"
    except Exception:
        return ""


def can_add_server():
    """(можно, сообщение). Блокирует только в режиме enforce и без лицензии."""
    try:
        if is_licensed() or limits_mode() != "enforce":
            return True, ""
        if count_servers() >= FREE_MAX_SERVERS:
            return False, (
                f"🔒 Без лицензии доступно до {FREE_MAX_SERVERS} серверов. "
                "Оформите лицензию: «💳 Моя лицензия»."
            )
    except Exception as e:
        logger.warning(f"Лимиты: не удалось проверить ({e}) — разрешаем")
    return True, ""


async def process_license_notices(bot) -> None:
    """Раз в проверку лицензии: предупреждение о лимитах (не чаще раза в сутки) и
    напоминание за сутки об окончании ПРОБНОЙ лицензии (платные покупатели получают
    напоминания с главного сервера)."""
    from config import ADMIN_IDS
    from database.requests import get_setting, set_setting

    async def _notify(text: str):
        for admin_id in ADMIN_IDS:
            try:
                await bot.send_message(admin_id, text, parse_mode="HTML")
            except Exception:
                pass

    try:
        if not is_licensed() and limits_mode() != "off" and is_over_limits():
            last = get_setting("license_limits_warned_at", "")
            due = True
            if last:
                try:
                    due = datetime.utcnow() - datetime.fromisoformat(last) > timedelta(hours=24)
                except ValueError:
                    due = True
            if due:
                set_setting("license_limits_warned_at", datetime.utcnow().isoformat())
                extra = (
                    "Сейчас всё работает (режим предупреждения)."
                    if limits_mode() == "warn"
                    else "Добавление новых серверов сверх лимита заблокировано."
                )
                await _notify(
                    "⚠️ <b>Превышен бесплатный лимит</b>\n\n"
                    f"Серверы: {count_servers()} из {FREE_MAX_SERVERS}, активные ключи: {count_keys()} из {FREE_MAX_KEYS}.\n"
                    f"{extra}\n\nЧтобы снять лимиты, оформите лицензию: «💳 Моя лицензия»."
                )
    except Exception as e:
        logger.warning(f"Уведомление о лимитах: {e}")

    try:
        from bot.services.license import get_license_key
        expires = get_setting("license_expires_at", "") or ""
        name = get_setting("license_partner_name", "") or ""
        if get_license_key() and expires and name.endswith("(пробный)"):
            left = datetime.strptime(expires, "%Y-%m-%d %H:%M:%S") - datetime.utcnow()
            if timedelta(0) < left <= timedelta(hours=30) and get_setting("license_expiry_notified", "") != expires:
                set_setting("license_expiry_notified", expires)
                await _notify(
                    f"⏰ <b>Пробная лицензия заканчивается {expires[:10]}</b>\n\n"
                    "Чтобы платные функции не отключились: «💳 Моя лицензия» → «💳 Купить / продлить»."
                )
    except Exception as e:
        logger.warning(f"Напоминание о пробной лицензии: {e}")
