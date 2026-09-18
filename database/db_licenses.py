"""Управление лицензиями whitelabel-партнёров — запускается на ГЛАВНОМ
сервере, который выступает лицензионным для остальных инсталляций бота."""
import secrets
import string
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Set

from database.connection import get_db


def generate_license_key() -> str:
    alphabet = string.ascii_uppercase + string.digits
    groups = ["".join(secrets.choice(alphabet) for _ in range(4)) for _ in range(3)]
    return "ECLW-" + "-".join(groups)


def create_partner_license(partner_name: str, features, duration_days: Optional[int] = None, notes: str = "") -> str:
    """features — набор (set/list) ключей функций из bot.services.license.GATED_FEATURES,
    например {'ai_assistant', 'site_webapp'}. Пустой набор — тариф без
    платных функций вообще."""
    from bot.services.license import features_to_str

    features_str = features_to_str(features) if not isinstance(features, str) else features
    all_keys = {"ai_assistant", "zvonok_verification", "site_webapp", "broadcast_marketing"}
    enabled = set(features_str.split(",")) if features_str else set()
    tier = "full" if enabled == all_keys else ("basic" if not enabled else "custom")

    license_key = generate_license_key()
    expires_at = None
    if duration_days:
        expires_at = (datetime.utcnow() + timedelta(days=duration_days)).strftime("%Y-%m-%d %H:%M:%S")

    with get_db() as conn:
        conn.execute(
            "INSERT INTO partner_licenses (license_key, partner_name, tier, features, expires_at, notes) VALUES (?, ?, ?, ?, ?, ?)",
            (license_key, partner_name, tier, features_str, expires_at, notes),
        )
        conn.commit()
    return license_key


def get_partner_license(license_key: str) -> Optional[Dict[str, Any]]:
    with get_db() as conn:
        row = conn.execute(
            "SELECT * FROM partner_licenses WHERE license_key = ?", (license_key.strip().upper(),)
        ).fetchone()
        return dict(row) if row else None


def list_partner_licenses() -> List[Dict[str, Any]]:
    with get_db() as conn:
        rows = conn.execute("SELECT * FROM partner_licenses ORDER BY created_at DESC").fetchall()
        return [dict(r) for r in rows]


def set_partner_license_features(license_key: str, features) -> bool:
    """Устанавливает ПОЛНЫЙ набор функций для лицензии (перезаписывает,
    не добавляет). features — set/list ключей или готовая CSV-строка."""
    from bot.services.license import features_to_str

    features_str = features_to_str(features) if not isinstance(features, str) else features
    all_keys = {"ai_assistant", "zvonok_verification", "site_webapp", "broadcast_marketing"}
    enabled = set(features_str.split(",")) if features_str else set()
    tier = "full" if enabled == all_keys else ("basic" if not enabled else "custom")

    with get_db() as conn:
        cursor = conn.execute(
            "UPDATE partner_licenses SET tier = ?, features = ?, updated_at = CURRENT_TIMESTAMP WHERE license_key = ?",
            (tier, features_str, license_key.strip().upper()),
        )
        conn.commit()
        return cursor.rowcount > 0


def extend_partner_license(license_key: str, extra_days: int) -> bool:
    license_row = get_partner_license(license_key)
    if not license_row:
        return False

    current_expires = license_row.get("expires_at")
    base = datetime.utcnow()
    if current_expires:
        try:
            parsed = datetime.strptime(current_expires, "%Y-%m-%d %H:%M:%S")
            if parsed > base:
                base = parsed
        except ValueError:
            pass

    new_expires = (base + timedelta(days=extra_days)).strftime("%Y-%m-%d %H:%M:%S")
    with get_db() as conn:
        cursor = conn.execute(
            "UPDATE partner_licenses SET expires_at = ?, is_active = 1, updated_at = CURRENT_TIMESTAMP WHERE license_key = ?",
            (new_expires, license_key.strip().upper()),
        )
        conn.commit()
        return cursor.rowcount > 0


def deactivate_partner_license(license_key: str) -> bool:
    with get_db() as conn:
        cursor = conn.execute(
            "UPDATE partner_licenses SET is_active = 0, updated_at = CURRENT_TIMESTAMP WHERE license_key = ?",
            (license_key.strip().upper(),),
        )
        conn.commit()
        return cursor.rowcount > 0


def check_license_validity(license_key: str) -> Dict[str, Any]:
    license_row = get_partner_license(license_key)
    if not license_row:
        return {"valid": False, "message": "Лицензия не найдена."}

    if not license_row.get("is_active"):
        return {"valid": False, "message": "Лицензия деактивирована."}

    expires_at = license_row.get("expires_at")
    if expires_at:
        try:
            parsed = datetime.strptime(expires_at, "%Y-%m-%d %H:%M:%S")
            if parsed < datetime.utcnow():
                return {"valid": False, "message": "Срок действия лицензии истёк."}
        except ValueError:
            pass

    return {
        "valid": True,
        "tier": license_row["tier"],
        "features": license_row.get("features") or "",
        "expires_at": expires_at,
        "partner_name": license_row["partner_name"],
    }


# ============================================================================
# ТАРИФЫ НА САМИ ЛИЦЕНЗИИ (продажа whitelabel-доступа через бота)
# ============================================================================

def create_license_tariff(name: str, features, price_rub: float, duration_days: Optional[int] = None) -> int:
    from bot.services.license import features_to_str

    features_str = features_to_str(features) if not isinstance(features, str) else features
    all_keys = {"ai_assistant", "zvonok_verification", "site_webapp", "broadcast_marketing"}
    enabled = set(features_str.split(",")) if features_str else set()
    tier = "full" if enabled == all_keys else ("basic" if not enabled else "custom")

    with get_db() as conn:
        cursor = conn.execute(
            "INSERT INTO license_tariffs (name, tier, features, duration_days, price_rub) VALUES (?, ?, ?, ?, ?)",
            (name, tier, features_str, duration_days, price_rub),
        )
        conn.commit()
        return cursor.lastrowid


def get_active_license_tariffs() -> List[Dict[str, Any]]:
    with get_db() as conn:
        rows = conn.execute(
            "SELECT * FROM license_tariffs WHERE is_active = 1 ORDER BY display_order, price_rub"
        ).fetchall()
        return [dict(r) for r in rows]


def get_license_tariff_by_id(tariff_id: int) -> Optional[Dict[str, Any]]:
    with get_db() as conn:
        row = conn.execute("SELECT * FROM license_tariffs WHERE id = ?", (tariff_id,)).fetchone()
        return dict(row) if row else None


def deactivate_license_tariff(tariff_id: int) -> bool:
    with get_db() as conn:
        cursor = conn.execute("UPDATE license_tariffs SET is_active = 0 WHERE id = ?", (tariff_id,))
        conn.commit()
        return cursor.rowcount > 0


# ============================================================================
# ЗАКАЗЫ НА ПОКУПКУ ЛИЦЕНЗИИ (для автовыдачи ключа после оплаты)
# ============================================================================

def create_license_purchase(order_id: str, telegram_id: int, license_tariff_id: int) -> None:
    with get_db() as conn:
        conn.execute(
            "INSERT INTO license_purchases (order_id, telegram_id, license_tariff_id, status) VALUES (?, ?, ?, 'pending')",
            (order_id, telegram_id, license_tariff_id),
        )
        conn.commit()


def save_license_purchase_payment_id(order_id: str, yookassa_payment_id: str) -> None:
    with get_db() as conn:
        conn.execute(
            "UPDATE license_purchases SET yookassa_payment_id = ? WHERE order_id = ?",
            (yookassa_payment_id, order_id),
        )
        conn.commit()


def get_license_purchase_by_order_id(order_id: str) -> Optional[Dict[str, Any]]:
    with get_db() as conn:
        row = conn.execute("SELECT * FROM license_purchases WHERE order_id = ?", (order_id,)).fetchone()
        return dict(row) if row else None


def complete_license_purchase(order_id: str, license_key: str) -> None:
    """Помечает заказ оплаченным и сохраняет выданный ключ (идемпотентно —
    повторный вызов для уже оплаченного заказа ничего не ломает)."""
    with get_db() as conn:
        conn.execute(
            "UPDATE license_purchases SET status = 'paid', license_key = ? WHERE order_id = ?",
            (license_key, order_id),
        )
        conn.commit()


def get_abandoned_license_purchases(older_than_minutes: int = 5) -> List[Dict[str, Any]]:
    """Заказы на лицензию, оставшиеся в pending — для фоновой автопроверки
    оплаты (клиент мог закрыть чат до подтверждения)."""
    with get_db() as conn:
        rows = conn.execute(
            """SELECT * FROM license_purchases
               WHERE status = 'pending' AND yookassa_payment_id IS NOT NULL
               AND created_at <= datetime('now', '-' || ? || ' minutes')""",
            (older_than_minutes,),
        ).fetchall()
        return [dict(r) for r in rows]


# ============================================================================
# РЕДАКТИРОВАНИЕ ТАРИФОВ НА ЛИЦЕНЗИИ (все поля + активация/деактивация)
# ============================================================================

def update_license_tariff_field(tariff_id: int, field: str, value) -> bool:
    """Обновляет ОДНО поле тарифа. field должно быть из белого списка —
    защита от SQL-инъекции через имя столбца."""
    allowed_fields = {"name", "tier", "features", "duration_days", "price_rub", "is_active", "display_order"}
    if field not in allowed_fields:
        raise ValueError(f"Недопустимое поле для обновления: {field}")
    with get_db() as conn:
        cursor = conn.execute(
            f"UPDATE license_tariffs SET {field} = ? WHERE id = ?",
            (value, tariff_id),
        )
        conn.commit()
        return cursor.rowcount > 0


def get_all_license_tariffs() -> List[Dict[str, Any]]:
    """В отличие от get_active_license_tariffs — возвращает ВСЕ тарифы,
    включая деактивированные (для экрана управления)."""
    with get_db() as conn:
        rows = conn.execute(
            "SELECT * FROM license_tariffs ORDER BY display_order, price_rub"
        ).fetchall()
        return [dict(r) for r in rows]


def activate_license_tariff(tariff_id: int) -> bool:
    with get_db() as conn:
        cursor = conn.execute("UPDATE license_tariffs SET is_active = 1 WHERE id = ?", (tariff_id,))
        conn.commit()
        return cursor.rowcount > 0


def delete_license_tariff(tariff_id: int) -> bool:
    with get_db() as conn:
        cursor = conn.execute("DELETE FROM license_tariffs WHERE id = ?", (tariff_id,))
        conn.commit()
        return cursor.rowcount > 0


def toggle_license_feature(license_key: str, feature: str) -> Set[str]:
    """Переключает ОДНУ функцию (вкл/выкл) для лицензии, остальные не
    трогает. Возвращает итоговый набор включённых функций."""
    from bot.services.license import features_from_str

    lic = get_partner_license(license_key)
    current = features_from_str(lic.get("features") if lic else "")
    if feature in current:
        current.discard(feature)
    else:
        current.add(feature)
    set_partner_license_features(license_key, current)
    return current


def toggle_tariff_feature(tariff_id: int, feature: str) -> Set[str]:
    """То же самое, но для тарифа на продажу (а не уже выданной лицензии)."""
    from bot.services.license import features_from_str, features_to_str

    tariff = get_license_tariff_by_id(tariff_id)
    current = features_from_str(tariff.get("features") if tariff else "")
    if feature in current:
        current.discard(feature)
    else:
        current.add(feature)
    features_str = features_to_str(current)
    all_keys = {"ai_assistant", "zvonok_verification", "site_webapp", "broadcast_marketing"}
    tier = "full" if current == all_keys else ("basic" if not current else "custom")
    with get_db() as conn:
        conn.execute(
            "UPDATE license_tariffs SET tier = ?, features = ? WHERE id = ?",
            (tier, features_str, tariff_id),
        )
        conn.commit()
    return current
