"""Совместные акции (коллаборации): промокод + бесплатные ключи участникам + аналитика.

Акция — это обычный промокод из db_promotions, к которому привязаны:
  • название партнёра (показывается клиентам: «Совместная акция <бренд> × <партнёр>»);
  • минимальный срок тарифа (скидка действует только на длинные тарифы);
  • настройки бесплатных ключей участникам партнёра (срок, число устройств).

Сам код не содержит данных конкретных партнёров: акции создаются админом в боте.
Работает под лицензионной функцией promo_coupons (как и обычные промокоды).
"""
import datetime
import logging
import re
from typing import Any, Dict, List, Optional, Tuple

from .connection import get_db

logger = logging.getLogger(__name__)

MEMBER_TARIFF_PREFIX = "Совместная акция: "
DEFAULT_DISCOUNT = 20
DEFAULT_MIN_MONTHS = 6
DEFAULT_PROMO_DAYS = 60
DEFAULT_ACTIVATION_LIMIT = 100
DEFAULT_MEMBER_DAYS = 90
DEFAULT_MEMBER_DEVICES = 5
# Месяц в днях для сравнения с тарифами (запас, чтобы «6 мес.» = 180/183 дн. проходили)
_DAYS_IN_MONTH = 30
_DAYS_TOLERANCE = 5


def create_tables(conn) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS collab_campaigns (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            title TEXT NOT NULL,
            description TEXT,
            promo_code_id INTEGER,
            min_months INTEGER NOT NULL DEFAULT 0,
            is_active INTEGER NOT NULL DEFAULT 0,
            member_days INTEGER NOT NULL DEFAULT 90,
            member_devices INTEGER NOT NULL DEFAULT 5,
            member_tariff_id INTEGER,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS collab_members (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            campaign_id INTEGER NOT NULL,
            user_id INTEGER,
            telegram_id INTEGER NOT NULL,
            key_id INTEGER,
            issued_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (campaign_id) REFERENCES collab_campaigns(id) ON DELETE CASCADE
        )
        """
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_collab_campaigns_promo ON collab_campaigns(promo_code_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_collab_members_campaign ON collab_members(campaign_id)")


def _row(row) -> Optional[Dict[str, Any]]:
    return dict(row) if row else None


# ---------------------------------------------------------------- campaigns --

def suggest_code(title: str, discount_percent: int) -> str:
    """Предлагает свободный промокод (base62) из названия партнёра и размера скидки."""
    from .db_promotions import generate_unique_promo_code, get_promo_code_by_code

    base = re.sub(r"[^0-9A-Za-z]", "", title or "").upper()[:12]
    if len(base) < 3:
        return generate_unique_promo_code(8)
    candidate = f"{base}{int(discount_percent)}"
    if not get_promo_code_by_code(candidate):
        return candidate
    for i in range(2, 100):
        alt = f"{candidate}{i}"
        if not get_promo_code_by_code(alt):
            return alt
    return generate_unique_promo_code(8)


def create_campaign(
    *,
    title: str,
    code: Optional[str] = None,
    discount_percent: int = DEFAULT_DISCOUNT,
    min_months: int = DEFAULT_MIN_MONTHS,
    promo_days: Optional[int] = DEFAULT_PROMO_DAYS,
    activation_limit: Optional[int] = DEFAULT_ACTIVATION_LIMIT,
    member_days: int = DEFAULT_MEMBER_DAYS,
    member_devices: int = DEFAULT_MEMBER_DEVICES,
    description: Optional[str] = None,
    is_active: bool = False,
    admin_id: Optional[int] = None,
) -> int:
    """Создаёт акцию и её промокод. По умолчанию акция выключена — включается кнопкой."""
    from .db_promotions import create_promo_code

    title = (title or "").strip()
    if not title:
        raise ValueError("Укажите название партнёра")
    code = (code or "").strip() or suggest_code(title, discount_percent)

    expires_at = None
    if promo_days and int(promo_days) > 0:
        expires_at = datetime.datetime.utcnow().replace(microsecond=0) + datetime.timedelta(days=int(promo_days))

    promo_id = create_promo_code(
        code=code,
        discount_percent=int(discount_percent),
        expires_at=expires_at,
        activation_limit=activation_limit,
        is_active=bool(is_active),
        created_by_admin_id=admin_id,
        source="collab",
        code_type="promo",
    )
    with get_db() as conn:
        cursor = conn.execute(
            """
            INSERT INTO collab_campaigns
                (title, description, promo_code_id, min_months, is_active, member_days, member_devices)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (title, description, promo_id, max(0, int(min_months)), 1 if is_active else 0,
             max(1, int(member_days)), max(1, int(member_devices))),
        )
        return int(cursor.lastrowid)


def get_campaign(campaign_id: int) -> Optional[Dict[str, Any]]:
    with get_db() as conn:
        return _row(conn.execute("SELECT * FROM collab_campaigns WHERE id = ?", (campaign_id,)).fetchone())


def get_campaign_by_promo_id(promo_id: int) -> Optional[Dict[str, Any]]:
    with get_db() as conn:
        return _row(conn.execute(
            "SELECT * FROM collab_campaigns WHERE promo_code_id = ? ORDER BY id LIMIT 1", (promo_id,)
        ).fetchone())


def list_campaigns() -> List[Dict[str, Any]]:
    with get_db() as conn:
        rows = conn.execute(
            """
            SELECT c.*, p.code AS promo_code, p.discount_percent AS discount_percent,
                   p.expires_at AS promo_expires_at, p.activation_limit AS activation_limit,
                   p.usage_count AS usage_count
            FROM collab_campaigns c
            LEFT JOIN promo_codes p ON p.id = c.promo_code_id
            ORDER BY c.is_active DESC, c.id DESC
            """
        ).fetchall()
        return [dict(r) for r in rows]


def get_campaign_full(campaign_id: int) -> Optional[Dict[str, Any]]:
    for item in list_campaigns():
        if int(item["id"]) == int(campaign_id):
            return item
    return None


def set_campaign_active(campaign_id: int, is_active: bool) -> bool:
    from .db_promotions import set_promo_code_active

    campaign = get_campaign(campaign_id)
    if not campaign:
        return False
    with get_db() as conn:
        conn.execute(
            "UPDATE collab_campaigns SET is_active = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
            (1 if is_active else 0, campaign_id),
        )
    if campaign.get("promo_code_id"):
        set_promo_code_active(int(campaign["promo_code_id"]), bool(is_active))
    return True


_CAMPAIGN_FIELDS = {"title", "description", "min_months", "member_days", "member_devices"}


def update_campaign(campaign_id: int, **fields) -> bool:
    """Обновляет поля акции. discount_percent / promo_days / activation_limit уходят в промокод."""
    from .db_promotions import update_promo_code

    campaign = get_campaign(campaign_id)
    if not campaign:
        return False

    own = {k: v for k, v in fields.items() if k in _CAMPAIGN_FIELDS}
    if own:
        if "min_months" in own:
            own["min_months"] = max(0, int(own["min_months"]))
        if "member_days" in own:
            own["member_days"] = max(1, int(own["member_days"]))
        if "member_devices" in own:
            own["member_devices"] = max(1, int(own["member_devices"]))
        sets = ", ".join(f"{k} = ?" for k in own)
        with get_db() as conn:
            conn.execute(
                f"UPDATE collab_campaigns SET {sets}, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                (*own.values(), campaign_id),
            )
        if campaign.get("member_tariff_id") and ({"member_days", "member_devices", "title"} & set(own)):
            ensure_member_tariff(campaign_id)

    promo_kwargs: Dict[str, Any] = {}
    if "discount_percent" in fields:
        promo_kwargs["discount_percent"] = int(fields["discount_percent"])
    if "promo_days" in fields:
        days = fields["promo_days"]
        if days and int(days) > 0:
            promo_kwargs["expires_at"] = (
                datetime.datetime.utcnow().replace(microsecond=0) + datetime.timedelta(days=int(days))
            )
        else:
            promo_kwargs["expires_at"] = None
    if "activation_limit" in fields:
        promo_kwargs["activation_limit"] = fields["activation_limit"]
    if promo_kwargs and campaign.get("promo_code_id"):
        update_promo_code(int(campaign["promo_code_id"]), **promo_kwargs)
    return True


def delete_campaign(campaign_id: int) -> Tuple[bool, str]:
    """Удаляет акцию, только если по ней не было ни оплат, ни выданных ключей."""
    from .db_promotions import delete_promo_code

    campaign = get_campaign(campaign_id)
    if not campaign:
        return False, "Акция не найдена"
    with get_db() as conn:
        members = conn.execute(
            "SELECT COUNT(*) FROM collab_members WHERE campaign_id = ?", (campaign_id,)
        ).fetchone()[0]
    if members:
        return False, "По акции уже выданы ключи участникам — её можно только выключить"
    if campaign.get("promo_code_id"):
        ok, reason = delete_promo_code(int(campaign["promo_code_id"]))
        if not ok:
            return False, reason or "Промокод акции уже использовали — её можно только выключить"
    with get_db() as conn:
        conn.execute("DELETE FROM collab_campaigns WHERE id = ?", (campaign_id,))
    return True, ""


# ---------------------------------------------------------------- eligibility --

def min_days_for_months(months: int) -> int:
    return max(0, int(months) * _DAYS_IN_MONTH - _DAYS_TOLERANCE)


def tariff_blocked_reason(promo: Optional[Dict[str, Any]], tariff: Optional[Dict[str, Any]]) -> Optional[str]:
    """Текст причины, если промокод активной акции не действует на этот тариф; иначе None."""
    if not promo or not tariff or not promo.get("id"):
        return None
    campaign = get_campaign_by_promo_id(int(promo["id"]))
    if not campaign or not campaign.get("is_active"):
        return None
    months = int(campaign.get("min_months") or 0)
    if months <= 0:
        return None
    duration = int(tariff.get("duration_days") or 0)
    if duration <= 0:
        return None  # безлимитный/особый тариф — не ограничиваем
    if duration >= min_days_for_months(months):
        return None
    return f"Промокод акции «{campaign['title']}» действует на тарифы от {months} мес."


# ---------------------------------------------------------------- members --

def ensure_member_tariff(campaign_id: int) -> Optional[int]:
    """Скрытый тариф для бесплатных ключей участников: свой срок и лимит устройств.
    Нужен, чтобы лимит устройств ключа брался из него, а не из служебного админ-тарифа."""
    campaign = get_campaign(campaign_id)
    if not campaign:
        return None
    name = f"{MEMBER_TARIFF_PREFIX}{campaign['title']}"
    days = int(campaign.get("member_days") or DEFAULT_MEMBER_DAYS)
    devices = int(campaign.get("member_devices") or DEFAULT_MEMBER_DEVICES)
    tariff_id = campaign.get("member_tariff_id")
    with get_db() as conn:
        if tariff_id and conn.execute("SELECT 1 FROM tariffs WHERE id = ?", (tariff_id,)).fetchone():
            conn.execute(
                "UPDATE tariffs SET name = ?, duration_days = ?, max_ips = ?, is_active = 0 WHERE id = ?",
                (name, days, devices, tariff_id),
            )
        else:
            cursor = conn.execute(
                """
                INSERT INTO tariffs (name, duration_days, price_cents, price_stars, price_rub,
                                     display_order, is_active, traffic_limit_gb, max_ips)
                VALUES (?, ?, 0, 0, 0, 999, 0, 0, ?)
                """,
                (name, days, devices),
            )
            tariff_id = int(cursor.lastrowid)
            conn.execute(
                "UPDATE collab_campaigns SET member_tariff_id = ? WHERE id = ?", (tariff_id, campaign_id)
            )
    return int(tariff_id) if tariff_id else None


def record_member(campaign_id: int, user_id: Optional[int], telegram_id: int, key_id: Optional[int]) -> int:
    with get_db() as conn:
        cursor = conn.execute(
            "INSERT INTO collab_members (campaign_id, user_id, telegram_id, key_id) VALUES (?, ?, ?, ?)",
            (campaign_id, user_id, int(telegram_id), key_id),
        )
        return int(cursor.lastrowid)


def list_members(campaign_id: int) -> List[Dict[str, Any]]:
    with get_db() as conn:
        rows = conn.execute(
            """
            SELECT m.*, u.username AS username
            FROM collab_members m
            LEFT JOIN users u ON u.telegram_id = m.telegram_id
            WHERE m.campaign_id = ?
            ORDER BY m.id DESC
            """,
            (campaign_id,),
        ).fetchall()
        return [dict(r) for r in rows]


# ---------------------------------------------------------------- analytics --

def get_stats(campaign_id: int) -> Dict[str, Any]:
    """Аналитика акции: переходы, оплаты, выручка, выданные скидки, ключи участникам."""
    campaign = get_campaign(campaign_id)
    result: Dict[str, Any] = {
        "visits": 0, "unique_visitors": 0, "holders": 0, "buyers": 0,
        "paid": 0, "reserved": 0, "money": {}, "members": 0,
    }
    if not campaign:
        return result
    promo_id = campaign.get("promo_code_id")
    with get_db() as conn:
        result["members"] = conn.execute(
            "SELECT COUNT(*) FROM collab_members WHERE campaign_id = ?", (campaign_id,)
        ).fetchone()[0]
        if not promo_id:
            return result
        row = conn.execute(
            "SELECT COUNT(*), COUNT(DISTINCT telegram_id) FROM promo_link_visits WHERE promo_code_id = ?",
            (promo_id,),
        ).fetchone()
        result["visits"], result["unique_visitors"] = int(row[0]), int(row[1])
        result["holders"] = conn.execute(
            "SELECT COUNT(*) FROM users WHERE active_promo_code_id = ?", (promo_id,)
        ).fetchone()[0]
        result["reserved"] = conn.execute(
            "SELECT COUNT(*) FROM promo_redemptions WHERE promo_code_id = ? AND status = 'reserved'",
            (promo_id,),
        ).fetchone()[0]
        rows = conn.execute(
            """
            SELECT amount_unit, COUNT(*) AS n, COUNT(DISTINCT user_id) AS buyers,
                   COALESCE(SUM(original_amount), 0) AS original,
                   COALESCE(SUM(discount_amount), 0) AS discount,
                   COALESCE(SUM(final_amount), 0) AS final
            FROM promo_redemptions
            WHERE promo_code_id = ? AND status = 'applied'
            GROUP BY amount_unit
            """,
            (promo_id,),
        ).fetchall()
        for r in rows:
            result["paid"] += int(r["n"])
            result["buyers"] += int(r["buyers"])
            result["money"][r["amount_unit"]] = {
                "original": int(r["original"]), "discount": int(r["discount"]), "final": int(r["final"]),
            }
    return result


def format_money(unit: str, amount: int) -> str:
    if unit == "stars":
        return f"{int(amount)} ⭐"
    amount = int(amount)
    if amount % 100 == 0:
        return f"{amount // 100} ₽"
    return f"{amount / 100:.2f} ₽".replace(".", ",")


def format_money_totals(money: Dict[str, Dict[str, int]], field: str) -> str:
    parts = [format_money(unit, vals[field]) for unit, vals in sorted(money.items()) if vals.get(field)]
    return " + ".join(parts) if parts else "0 ₽"
