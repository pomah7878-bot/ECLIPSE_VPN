"""Учёт устройств для пробных ключей (v1.190): один пробник на устройство.

Приложения (Happ, INCY и др.) при загрузке подписки присылают идентификатор устройства X-HWID.
Если тот же идентификатор уже встречался с пробным ключом ДРУГОГО пользователя, второй пробный
ключ получает «заблокированный» ответ подписки (с кнопкой покупки). Храним только sha256 от
идентификатора, не сам идентификатор. Любая ошибка здесь не должна ломать выдачу подписки.
"""
import hashlib
import logging
from typing import Any, Dict, List, Optional

from database.connection import get_db

logger = logging.getLogger(__name__)

_SETTING_ENABLED = "trial_device_guard"
_BAD_VALUES = {"", "unknown", "null", "none", "undefined", "0", "00000000-0000-0000-0000-000000000000"}


def guard_setting_on() -> bool:
    """Настройка администратора: включено по умолчанию."""
    try:
        from database.requests import get_setting
        return (get_setting(_SETTING_ENABLED, "1") or "1") != "0"
    except Exception:
        return True


def guard_enabled() -> bool:
    """Проверка действует, если она включена И есть лицензия на пробный период."""
    try:
        from bot.services.license import is_feature_available
        if not is_feature_available("trial_period"):
            return False
    except Exception:
        pass
    return guard_setting_on()


def set_guard_enabled(on: bool) -> None:
    from database.requests import set_setting
    set_setting(_SETTING_ENABLED, "1" if on else "0")


def normalize_hwid(raw: Optional[str]) -> Optional[str]:
    """sha256 от идентификатора устройства; None, если значение непригодно (пустое, служебное, слишком короткое)."""
    value = (raw or "").strip()
    if not (8 <= len(value) <= 200):
        return None
    if value.lower() in _BAD_VALUES or len(set(value)) <= 2:
        return None
    return hashlib.sha256(value.lower().encode("utf-8")).hexdigest()


def check_and_register(hwid_hash: str, key_id: int, user_id: int) -> Dict[str, Any]:
    """Возвращает {'status': 'new'|'same'|'allowed'|'duplicate', 'owner_user_id': int|None}."""
    with get_db() as conn:
        row = conn.execute(
            "SELECT user_id, allowed FROM trial_devices WHERE hwid_hash = ?", (hwid_hash,)
        ).fetchone()
        if row is None:
            cur = conn.execute(
                "INSERT OR IGNORE INTO trial_devices (hwid_hash, user_id, key_id) VALUES (?, ?, ?)",
                (hwid_hash, user_id, key_id),
            )
            if cur.rowcount:
                return {"status": "new", "owner_user_id": user_id}
            row = conn.execute(
                "SELECT user_id, allowed FROM trial_devices WHERE hwid_hash = ?", (hwid_hash,)
            ).fetchone()
            if row is None:
                return {"status": "new", "owner_user_id": user_id}
        if row["allowed"]:
            return {"status": "allowed", "owner_user_id": row["user_id"]}
        if row["user_id"] == user_id:
            return {"status": "same", "owner_user_id": user_id}
        return {"status": "duplicate", "owner_user_id": row["user_id"]}


def is_key_blocked(key_id: int) -> bool:
    with get_db() as conn:
        return conn.execute(
            "SELECT 1 FROM trial_device_blocks WHERE key_id = ?", (key_id,)
        ).fetchone() is not None


def block_key(key_id: int, hwid_hash: str, user_id: int, owner_user_id: Optional[int]) -> bool:
    """True, если блокировка создана сейчас (False — уже была)."""
    with get_db() as conn:
        cur = conn.execute(
            "INSERT OR IGNORE INTO trial_device_blocks (key_id, hwid_hash, user_id, owner_user_id) VALUES (?, ?, ?, ?)",
            (key_id, hwid_hash, user_id, owner_user_id),
        )
        return bool(cur.rowcount)


def touch_block(key_id: int) -> None:
    with get_db() as conn:
        conn.execute("UPDATE trial_device_blocks SET hits = hits + 1 WHERE key_id = ?", (key_id,))


def unblock_key(key_id: int) -> bool:
    """Снимает блокировку и помечает устройство разрешённым (исключение для этого устройства)."""
    with get_db() as conn:
        row = conn.execute(
            "SELECT hwid_hash FROM trial_device_blocks WHERE key_id = ?", (key_id,)
        ).fetchone()
        if row is None:
            return False
        conn.execute("UPDATE trial_devices SET allowed = 1 WHERE hwid_hash = ?", (row["hwid_hash"],))
        conn.execute("DELETE FROM trial_device_blocks WHERE key_id = ?", (key_id,))
        return True


def stats() -> Dict[str, int]:
    with get_db() as conn:
        devices = conn.execute("SELECT COUNT(*) AS c FROM trial_devices").fetchone()["c"]
        blocks = conn.execute("SELECT COUNT(*) AS c FROM trial_device_blocks").fetchone()["c"]
    return {"devices": devices, "blocks": blocks}


def recent_blocks(limit: int = 10) -> List[Dict[str, Any]]:
    with get_db() as conn:
        rows = conn.execute(
            """SELECT b.key_id, b.created_at, b.hits, b.user_id, b.owner_user_id,
                      u.telegram_id AS tg, o.telegram_id AS owner_tg
               FROM trial_device_blocks b
               LEFT JOIN users u ON u.id = b.user_id
               LEFT JOIN users o ON o.id = b.owner_user_id
               ORDER BY b.created_at DESC LIMIT ?""",
            (limit,),
        ).fetchall()
    return [dict(r) for r in rows]
