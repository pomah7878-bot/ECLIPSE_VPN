"""Учёт устройств для пробных ключей (v1.190): один пробник на устройство.

Приложения (Happ, INCY, ECLIPSE TV, v2rayTun и др.) при загрузке подписки присылают
идентификатор устройства: чаще всего X-HWID, некоторые — X-Device-Id или похожий
заголовок. Если тот же идентификатор уже встречался с пробным ключом ДРУГОГО
пользователя, второй пробный ключ получает «заблокированный» ответ подписки
(с кнопкой покупки). Клиент определяется по User-Agent, чтобы админ видел,
из какого приложения пришло устройство.

Храним только sha256 от идентификатора, не сам идентификатор. Любая ошибка
здесь не должна ломать выдачу подписки.
"""
import hashlib
import logging
from typing import Any, Dict, List, Optional

from database.connection import get_db
from bot.services import client_apps

logger = logging.getLogger(__name__)

_SETTING_ENABLED = "trial_device_guard"
_BAD_VALUES = {"", "unknown", "null", "none", "undefined", "0", "00000000-0000-0000-0000-000000000000"}

# Идентификаторы устройства, которые присылают Android/iOS-клиенты.
# X-HWID — основной (Happ/INCY/v2rayTun), остальные — совместимость с
# другими сборками и новым клиентом ECLIPSE.
DEVICE_HEADERS = (
    "X-HWID",
    "X-Device-Id",
    "X-Device-ID",
    "X-DeviceId",
    "X-Android-Id",
    "X-Install-Id",
    "X-Instance-Id",
)


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


def _device_label(request: Any, client: Dict[str, Any]) -> str:
    """Короткая подпись устройства для админки: клиент · ОС · модель."""
    headers = getattr(request, "headers", {}) or {}

    def _get(name: str) -> str:
        try:
            return str(headers.get(name) or "").strip()
        except Exception:  # noqa: BLE001
            return ""

    os_name = _get("X-Device-OS")
    os_ver = _get("X-Ver-OS")
    model = _get("X-Device-Model")
    if os_name and os_ver:
        os_name = f"{os_name} {os_ver}"
    parts = [str(client.get("label") or "")]
    if os_name:
        parts.append(os_name)
    if model:
        parts.append(model)
    return " · ".join(part for part in parts if part)[:120]


def extract_device(request: Any) -> Optional[Dict[str, Any]]:
    """Устройство из заголовков запроса (Android/iOS-клиенты).

    Возвращает None, если настоящего идентификатора нет: по одному
    User-Agent/модели устройство не определяем, иначе одинаковые телефоны
    блокировали бы друг друга.
    """
    ua = ""
    try:
        ua = str(request.headers.get("User-Agent") or "")
    except Exception:  # noqa: BLE001
        pass
    client = client_apps.detect_client(ua)
    for header in DEVICE_HEADERS:
        try:
            raw = request.headers.get(header)
        except Exception:  # noqa: BLE001
            raw = None
        hwid_hash = normalize_hwid(raw)
        if hwid_hash:
            return {
                "hash": hwid_hash,
                "header": header,
                "strong": True,
                "client": client["key"],
                "client_label": client["label"],
                "platform": client["platform"],
                "android": client["android"],
                "label": _device_label(request, client),
                "ua": ua[:200],
            }
    return None


def check_and_register(
    hwid_hash: str,
    key_id: int,
    user_id: int,
    client: Optional[str] = None,
    device_label: Optional[str] = None,
) -> Dict[str, Any]:
    """Возвращает {'status': 'new'|'same'|'allowed'|'duplicate', 'owner_user_id': int|None}."""
    with get_db() as conn:
        row = conn.execute(
            "SELECT user_id, allowed FROM trial_devices WHERE hwid_hash = ?", (hwid_hash,)
        ).fetchone()
        if row is None:
            cur = conn.execute(
                "INSERT OR IGNORE INTO trial_devices "
                "(hwid_hash, user_id, key_id, client, device_label, last_seen) "
                "VALUES (?, ?, ?, ?, ?, CURRENT_TIMESTAMP)",
                (hwid_hash, user_id, key_id, client, device_label),
            )
            if cur.rowcount:
                return {"status": "new", "owner_user_id": user_id}
            row = conn.execute(
                "SELECT user_id, allowed FROM trial_devices WHERE hwid_hash = ?", (hwid_hash,)
            ).fetchone()
            if row is None:
                return {"status": "new", "owner_user_id": user_id}
        conn.execute(
            "UPDATE trial_devices SET last_seen = CURRENT_TIMESTAMP, "
            "client = COALESCE(?, client), device_label = COALESCE(?, device_label) "
            "WHERE hwid_hash = ?",
            (client, device_label, hwid_hash),
        )
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


def block_key(
    key_id: int,
    hwid_hash: str,
    user_id: int,
    owner_user_id: Optional[int],
    client: Optional[str] = None,
    device_label: Optional[str] = None,
) -> bool:
    """True, если блокировка создана сейчас (False — уже была)."""
    with get_db() as conn:
        cur = conn.execute(
            "INSERT OR IGNORE INTO trial_device_blocks "
            "(key_id, hwid_hash, user_id, owner_user_id, client, device_label) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (key_id, hwid_hash, user_id, owner_user_id, client, device_label),
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
                      b.client, b.device_label,
                      u.telegram_id AS tg, o.telegram_id AS owner_tg
               FROM trial_device_blocks b
               LEFT JOIN users u ON u.id = b.user_id
               LEFT JOIN users o ON o.id = b.owner_user_id
               ORDER BY b.created_at DESC LIMIT ?""",
            (limit,),
        ).fetchall()
    return [dict(r) for r in rows]
