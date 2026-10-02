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
    from bot.services.license import features_to_str, GATED_FEATURES

    features_str = features_to_str(features) if not isinstance(features, str) else features
    all_keys = set(GATED_FEATURES.keys())
    enabled = set(features_str.split(",")) if features_str else set()
    from bot.services.license import tier_of
    tier = tier_of(enabled)

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
    from bot.services.license import features_to_str, GATED_FEATURES

    features_str = features_to_str(features) if not isinstance(features, str) else features
    all_keys = set(GATED_FEATURES.keys())
    enabled = set(features_str.split(",")) if features_str else set()
    from bot.services.license import tier_of
    tier = tier_of(enabled)

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
    from bot.services.license import features_to_str, GATED_FEATURES

    features_str = features_to_str(features) if not isinstance(features, str) else features
    all_keys = set(GATED_FEATURES.keys())
    enabled = set(features_str.split(",")) if features_str else set()
    from bot.services.license import tier_of
    tier = tier_of(enabled)

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
    """Создаёт заказ. Условия тарифа (функции, срок, цена) фиксируются В ЗАКАЗЕ
    на момент создания: правка тарифа админом позже не меняет то, что получит
    покупатель за уже созданную оплату."""
    tariff = get_license_tariff_by_id(license_tariff_id) or {}
    with get_db() as conn:
        conn.execute(
            "INSERT INTO license_purchases (order_id, telegram_id, license_tariff_id, status, "
            "features, duration_days, price_rub) VALUES (?, ?, ?, 'pending', ?, ?, ?)",
            (order_id, telegram_id, license_tariff_id, tariff.get("features") or "",
             tariff.get("duration_days"), tariff.get("price_rub")),
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


def claim_license_purchase(order_id: str) -> bool:
    """Атомарно «захватывает» неоплаченный заказ для выдачи ключа. True получает
    ровно ОДИН вызов — параллельные нажатия и фоновая автопроверка не выдадут
    второй ключ за тот же платёж. Зависший захват (процесс упал посреди выдачи)
    освобождается через 2 минуты."""
    with get_db() as conn:
        cursor = conn.execute(
            "UPDATE license_purchases SET status = 'issuing', claimed_at = datetime('now') "
            "WHERE order_id = ? AND (status = 'pending' OR "
            "(status = 'issuing' AND claimed_at < datetime('now', '-2 minutes')))",
            (order_id,),
        )
        conn.commit()
        return cursor.rowcount == 1


def release_license_purchase_claim(order_id: str) -> None:
    """Возвращает захваченный заказ в 'pending' (выдача не удалась)."""
    with get_db() as conn:
        conn.execute(
            "UPDATE license_purchases SET status = 'pending', claimed_at = NULL "
            "WHERE order_id = ? AND status = 'issuing'",
            (order_id,),
        )
        conn.commit()


def mark_license_purchase_status(order_id: str, status: str) -> None:
    with get_db() as conn:
        conn.execute(
            "UPDATE license_purchases SET status = ? WHERE order_id = ? AND status != 'paid'",
            (status, order_id),
        )
        conn.commit()


def complete_license_purchase(order_id: str, license_key: str) -> None:
    """Помечает заказ оплаченным и сохраняет выданный ключ. Уже оплаченный
    заказ не перезаписывается."""
    with get_db() as conn:
        conn.execute(
            "UPDATE license_purchases SET status = 'paid', license_key = ? "
            "WHERE order_id = ? AND status != 'paid'",
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
               AND created_at <= datetime('now', '-' || ? || ' minutes')
               AND created_at >= datetime('now', '-1 day')""",
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
    from bot.services.license import GATED_FEATURES
    all_keys = set(GATED_FEATURES.keys())
    from bot.services.license import tier_of
    tier = tier_of(current)
    with get_db() as conn:
        conn.execute(
            "UPDATE license_tariffs SET tier = ?, features = ? WHERE id = ?",
            (tier, features_str, tariff_id),
        )
        conn.commit()
    return current


# ============================================================================
# v1.166: установки, журнал событий, напоминания
# ============================================================================

def register_activation(license_key: str, instance_id: str, ip: str = "", stale_days: int = 14):
    """Учитывает установку, использующую ключ. Возвращает (разрешено, занято мест).
    Установка, молчавшая дольше stale_days, место не занимает. Старые версии
    бота (instance_id начинается с 'legacy-') только записываются, не ограничиваются."""
    key = license_key.strip().upper()
    with get_db() as conn:
        lic = conn.execute("SELECT max_instances FROM partner_licenses WHERE license_key = ?", (key,)).fetchone()
        limit = 2
        if lic is not None and lic["max_instances"] is not None:
            limit = int(lic["max_instances"])
        known = conn.execute(
            "SELECT 1 FROM license_activations WHERE license_key = ? AND instance_id = ?", (key, instance_id)
        ).fetchone()
        busy = conn.execute(
            "SELECT COUNT(*) FROM license_activations WHERE license_key = ? AND instance_id NOT LIKE 'legacy-%' "
            "AND instance_id != ? AND last_seen >= datetime('now', ?)",
            (key, instance_id, "-%d days" % stale_days),
        ).fetchone()[0]
        if known:
            conn.execute(
                "UPDATE license_activations SET last_seen = CURRENT_TIMESTAMP, last_ip = ? "
                "WHERE license_key = ? AND instance_id = ?", (ip, key, instance_id),
            )
            conn.commit()
            return True, busy + 1
        if not instance_id.startswith("legacy-") and limit > 0 and busy >= limit:
            return False, busy
        conn.execute(
            "INSERT INTO license_activations (license_key, instance_id, last_ip) VALUES (?, ?, ?)",
            (key, instance_id, ip),
        )
        conn.commit()
        return True, busy + 1


def list_activations(license_key: str) -> List[Dict[str, Any]]:
    with get_db() as conn:
        rows = conn.execute(
            "SELECT * FROM license_activations WHERE license_key = ? ORDER BY last_seen DESC",
            (license_key.strip().upper(),),
        ).fetchall()
        return [dict(r) for r in rows]


def reset_activations(license_key: str) -> int:
    """Освобождает все места (партнёр переехал на другой сервер). Возвращает число записей."""
    with get_db() as conn:
        cur = conn.execute("DELETE FROM license_activations WHERE license_key = ?", (license_key.strip().upper(),))
        conn.commit()
        return cur.rowcount


def log_license_event(license_key: str, event: str, details: str = "") -> None:
    with get_db() as conn:
        conn.execute(
            "INSERT INTO license_events (license_key, event, details) VALUES (?, ?, ?)",
            (license_key.strip().upper(), event, details),
        )
        conn.commit()


def has_license_event(license_key: str, event: str) -> bool:
    with get_db() as conn:
        row = conn.execute(
            "SELECT 1 FROM license_events WHERE license_key = ? AND event = ? LIMIT 1",
            (license_key.strip().upper(), event),
        ).fetchone()
        return row is not None


def get_expiring_licenses_with_buyers(within_days: int = 7) -> List[Dict[str, Any]]:
    """Активные лицензии, купленные через бота, срок которых истекает в ближайшие within_days дней."""
    with get_db() as conn:
        rows = conn.execute(
            "SELECT l.license_key, l.expires_at, l.partner_name, p.telegram_id "
            "FROM partner_licenses l JOIN license_purchases p ON p.license_key = l.license_key "
            "WHERE l.is_active = 1 AND l.expires_at IS NOT NULL "
            "AND l.expires_at > datetime('now') AND l.expires_at <= datetime('now', ?)",
            ("+%d days" % within_days,),
        ).fetchall()
        return [dict(r) for r in rows]


# ============================================================================
# ПРОБНАЯ ЛИЦЕНЗИЯ (v1.179)
# ============================================================================

def issue_trial_license(telegram_id: int, partner_name: str, days: int, features) -> Dict[str, Any]:
    """Выдаёт пробную лицензию ровно один раз на аккаунт Telegram.
    Возвращает {"status": "issued"|"already", "license_key", "expires_at"}."""
    with get_db() as conn:
        cur = conn.execute("INSERT OR IGNORE INTO license_trials (telegram_id) VALUES (?)", (telegram_id,))
        inserted = cur.rowcount > 0
        row = conn.execute("SELECT license_key FROM license_trials WHERE telegram_id = ?", (telegram_id,)).fetchone()
    if not inserted:
        key = row["license_key"] if row else None
        lic = get_partner_license(key) if key else None
        return {"status": "already", "license_key": key, "expires_at": lic.get("expires_at") if lic else None}

    try:
        key = create_partner_license(
            f"{partner_name} (пробный)", features, days, notes=f"trial tg={telegram_id}",
        )
        with get_db() as conn:
            conn.execute("UPDATE license_trials SET license_key = ? WHERE telegram_id = ?", (key, telegram_id))
            conn.execute("UPDATE partner_licenses SET max_instances = 1 WHERE license_key = ?", (key,))
    except Exception:
        with get_db() as conn:
            conn.execute("DELETE FROM license_trials WHERE telegram_id = ?", (telegram_id,))
        raise
    lic = get_partner_license(key)
    return {"status": "issued", "license_key": key, "expires_at": lic.get("expires_at") if lic else None}


def has_used_trial(telegram_id: int) -> bool:
    with get_db() as conn:
        return conn.execute("SELECT 1 FROM license_trials WHERE telegram_id = ?", (telegram_id,)).fetchone() is not None


def count_trials() -> int:
    with get_db() as conn:
        return conn.execute("SELECT COUNT(*) FROM license_trials").fetchone()[0]
