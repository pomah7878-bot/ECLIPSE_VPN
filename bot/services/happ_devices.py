"""
Учёт устройств Happ на стороне бота (v1.277).

Зачем: у Provider ID на happ-proxy.com есть лимит устройств по тарифу. При
превышении Happ отключает премиум-функции (скрытие настроек, автоподключение
и т.д.) на ВСЕХ устройствах. Кабинет Happ узнаёт об этом поздно, поэтому бот
сам считает уникальные устройства по X-HWID из запросов подписки и заранее
предупреждает админа.

Хранится только sha256 от HWID (сам идентификатор не сохраняется).
Счёт ведётся по окну 30 дней, как в кабинете Happ («Все устройства»).
Это оценка: точное число знает только Happ.
"""
import logging
import time
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

WINDOW_DAYS = 30
DEFAULT_LIMIT = 20
ALERT_LEVELS = (80, 100)
_LIMIT_KEY = "happ_device_limit"
_LEVEL_KEY = "happ_dev_alert_level"
_ENABLED_KEY = "happ_dev_alert_enabled"
_LAST_PRUNE = 0.0


def _conn():
    from database.connection import get_db
    return get_db()


def ensure_table(conn) -> None:
    conn.execute("""
        CREATE TABLE IF NOT EXISTS happ_devices (
            hwid_hash TEXT PRIMARY KEY,
            key_id INTEGER,
            first_seen TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            last_seen TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        )
    """)


def get_limit() -> int:
    from database.requests import get_setting
    try:
        value = int(get_setting(_LIMIT_KEY, str(DEFAULT_LIMIT)) or DEFAULT_LIMIT)
    except (TypeError, ValueError):
        value = DEFAULT_LIMIT
    return max(1, value)


def set_limit(value: int) -> None:
    from database.requests import set_setting
    set_setting(_LIMIT_KEY, str(max(1, int(value))))


def alerts_enabled() -> bool:
    from database.requests import get_setting
    return get_setting(_ENABLED_KEY, "1") != "0"


def set_alerts_enabled(on: bool) -> None:
    from database.requests import set_setting
    set_setting(_ENABLED_KEY, "1" if on else "0")


def record(hwid_raw: Optional[str], key_id: Optional[int]) -> bool:
    """Запоминает устройство. True, если оно новое. Ошибки не пробрасывает."""
    global _LAST_PRUNE
    try:
        from bot.services.trial_device import normalize_hwid
        hwid_hash = normalize_hwid(hwid_raw)
        if not hwid_hash:
            return False
        with _conn() as conn:
            ensure_table(conn)
            row = conn.execute(
                "SELECT 1 FROM happ_devices WHERE hwid_hash = ?", (hwid_hash,)
            ).fetchone()
            if row:
                conn.execute(
                    "UPDATE happ_devices SET last_seen = CURRENT_TIMESTAMP, key_id = COALESCE(?, key_id) "
                    "WHERE hwid_hash = ?",
                    (key_id, hwid_hash),
                )
                is_new = False
            else:
                conn.execute(
                    "INSERT INTO happ_devices (hwid_hash, key_id) VALUES (?, ?)", (hwid_hash, key_id)
                )
                is_new = True
            now = time.time()
            if now - _LAST_PRUNE > 3600:
                _LAST_PRUNE = now
                conn.execute(
                    f"DELETE FROM happ_devices WHERE last_seen < datetime('now', '-{WINDOW_DAYS + 5} days')"
                )
        return is_new
    except Exception as exc:
        logger.debug(f"happ_devices.record: {exc}")
        return False


def stats() -> Dict[str, Any]:
    """{'total': устройств за 30 дн., 'active7': за 7 дн., 'limit': N, 'percent': int}."""
    limit = get_limit()
    try:
        with _conn() as conn:
            ensure_table(conn)
            total = conn.execute(
                f"SELECT COUNT(*) FROM happ_devices WHERE last_seen >= datetime('now', '-{WINDOW_DAYS} days')"
            ).fetchone()[0]
            active7 = conn.execute(
                "SELECT COUNT(*) FROM happ_devices WHERE last_seen >= datetime('now', '-7 days')"
            ).fetchone()[0]
    except Exception as exc:
        logger.debug(f"happ_devices.stats: {exc}")
        total = active7 = 0
    return {"total": total, "active7": active7, "limit": limit, "percent": int(total * 100 / limit)}


def pending_alert_level(percent: int, stored: int) -> Optional[int]:
    """Какой порог нужно объявить сейчас (None — ничего). Чистая функция для тестов."""
    reached = [lvl for lvl in ALERT_LEVELS if percent >= lvl]
    top = max(reached) if reached else 0
    if top > stored:
        return top
    return None


async def check_and_alert() -> None:
    """Вызывать после появления нового устройства. Шлёт админам по разу на порог."""
    try:
        if not alerts_enabled():
            return
        from database.requests import get_setting, set_setting
        st = stats()
        try:
            stored = int(get_setting(_LEVEL_KEY, "0") or 0)
        except (TypeError, ValueError):
            stored = 0
        level = pending_alert_level(st["percent"], stored)
        if level is None:
            # ниже порогов — сбрасываем, чтобы предупредить снова при новом росте
            lower = max([lvl for lvl in ALERT_LEVELS if st["percent"] >= lvl] or [0])
            if lower < stored:
                set_setting(_LEVEL_KEY, str(lower))
            return
        set_setting(_LEVEL_KEY, str(level))
        if level >= 100:
            text = (
                f"🚨 <b>Лимит устройств Happ достигнут</b>\n\n"
                f"Устройств за 30 дней: <b>{st['total']}</b> из {st['limit']}.\n"
                f"Если в кабинете Happ счётчик выше лимита тарифа, Happ отключает премиум-функции "
                f"(скрытие настроек и др.) у всех клиентов.\n\n"
                f"Что сделать: повысить тариф в кабинете Happ или перевести клиентов на своё приложение."
            )
        else:
            text = (
                f"⚠️ <b>Устройства Happ: {st['percent']}% лимита</b>\n\n"
                f"За 30 дней: <b>{st['total']}</b> из {st['limit']}.\n"
                f"Скоро лимит тарифа будет исчерпан. Подумайте о повышении тарифа Happ."
            )
        from bot.utils.runtime_state import get_bot_instance
        from config import ADMIN_IDS
        bot_instance = get_bot_instance()
        if not bot_instance:
            return
        from bot.utils.notify_kb import with_home
        for admin_id in ADMIN_IDS:
            try:
                await bot_instance.send_message(admin_id, text, parse_mode="HTML", reply_markup=with_home())
            except Exception:
                pass
    except Exception as exc:
        logger.debug(f"happ_devices.check_and_alert: {exc}")
