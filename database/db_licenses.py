"""Управление лицензиями whitelabel-партнёров — запускается на ГЛАВНОМ
сервере, который выступает лицензионным для остальных инсталляций бота."""
import secrets
import string
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

from database.connection import get_db


def generate_license_key() -> str:
    alphabet = string.ascii_uppercase + string.digits
    groups = ["".join(secrets.choice(alphabet) for _ in range(4)) for _ in range(3)]
    return "ECLW-" + "-".join(groups)


def create_partner_license(partner_name: str, tier: str, duration_days: Optional[int] = None, notes: str = "") -> str:
    if tier not in ("basic", "full"):
        raise ValueError(f"Неизвестный тариф: {tier}")

    license_key = generate_license_key()
    expires_at = None
    if duration_days:
        expires_at = (datetime.utcnow() + timedelta(days=duration_days)).strftime("%Y-%m-%d %H:%M:%S")

    with get_db() as conn:
        conn.execute(
            "INSERT INTO partner_licenses (license_key, partner_name, tier, expires_at, notes) VALUES (?, ?, ?, ?, ?)",
            (license_key, partner_name, tier, expires_at, notes),
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


def set_partner_license_tier(license_key: str, tier: str) -> bool:
    if tier not in ("basic", "full"):
        raise ValueError(f"Неизвестный тариф: {tier}")
    with get_db() as conn:
        cursor = conn.execute(
            "UPDATE partner_licenses SET tier = ?, updated_at = CURRENT_TIMESTAMP WHERE license_key = ?",
            (tier, license_key.strip().upper()),
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
        "expires_at": expires_at,
        "partner_name": license_row["partner_name"],
    }


# ============================================================================
# ТАРИФЫ НА САМИ ЛИЦЕНЗИИ (продажа whitelabel-доступа через бота)
# ============================================================================

def create_license_tariff(name: str, tier: str, price_rub: float, duration_days: Optional[int] = None) -> int:
    if tier not in ("basic", "full"):
        raise ValueError(f"Неизвестный тариф: {tier}")
    with get_db() as conn:
        cursor = conn.execute(
            "INSERT INTO license_tariffs (name, tier, duration_days, price_rub) VALUES (?, ?, ?, ?)",
            (name, tier, duration_days, price_rub),
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
