"""v1.218: хранение прокруток рулетки."""
import logging
from datetime import datetime, timedelta
from typing import Any, Optional

from database.connection import get_db

logger = logging.getLogger(__name__)

_COUNTED = ("pending", "granting", "done")


def reserve_roulette_spin(user_id: int, telegram_id: int, period_days: int) -> dict[str, Any]:
    """Резервирует попытку. Если период ещё не прошёл — возвращает {'wait_seconds': N}.
    Иначе создаёт запись pending и возвращает {'spin_id': id}."""
    period_days = max(1, int(period_days))
    with get_db() as conn:
        row = conn.execute(
            "SELECT MAX(created_at) AS last FROM roulette_spins "
            "WHERE user_id = ? AND status IN ('pending','granting','done') "
            "AND created_at > datetime('now', ?)",
            (user_id, f"-{period_days} days"),
        ).fetchone()
        last = row["last"] if row else None
        if last:
            last_dt = datetime.strptime(str(last)[:19], "%Y-%m-%d %H:%M:%S")
            wait = (last_dt + timedelta(days=period_days) - datetime.utcnow()).total_seconds()
            return {"wait_seconds": max(60, int(wait))}
        cur = conn.execute(
            "INSERT INTO roulette_spins (user_id, telegram_id, status) VALUES (?, ?, 'pending')",
            (user_id, telegram_id),
        )
        return {"spin_id": int(cur.lastrowid)}


def get_roulette_wait_seconds(user_id: int, period_days: int) -> int:
    """0 — можно крутить сейчас, иначе секунд до следующей попытки (ничего не пишет)."""
    period_days = max(1, int(period_days))
    with get_db() as conn:
        row = conn.execute(
            "SELECT MAX(created_at) AS last FROM roulette_spins "
            "WHERE user_id = ? AND status IN ('pending','granting','done') "
            "AND created_at > datetime('now', ?)",
            (user_id, f"-{period_days} days"),
        ).fetchone()
    last = row["last"] if row else None
    if not last:
        return 0
    last_dt = datetime.strptime(str(last)[:19], "%Y-%m-%d %H:%M:%S")
    wait = (last_dt + timedelta(days=period_days) - datetime.utcnow()).total_seconds()
    return max(0, int(wait))


def claim_roulette_spin(spin_id: int, dice_value: int) -> bool:
    """Атомарно переводит pending -> granting (приз выдаётся ровно один раз)."""
    with get_db() as conn:
        cur = conn.execute(
            "UPDATE roulette_spins SET status = 'granting', dice_value = ? "
            "WHERE id = ? AND status = 'pending'",
            (int(dice_value), int(spin_id)),
        )
        return cur.rowcount > 0


def finish_roulette_spin(spin_id: int, status: str, tier: str, prize_type: str, prize_amount: int) -> None:
    with get_db() as conn:
        conn.execute(
            "UPDATE roulette_spins SET status = ?, tier = ?, prize_type = ?, prize_amount = ? WHERE id = ?",
            (status, tier, prize_type, int(prize_amount), int(spin_id)),
        )


def fail_roulette_spin(spin_id: int) -> None:
    with get_db() as conn:
        conn.execute("UPDATE roulette_spins SET status = 'failed' WHERE id = ? AND status = 'pending'", (int(spin_id),))


def get_roulette_stats() -> dict[str, Any]:
    with get_db() as conn:
        by_status = {r["status"]: r["c"] for r in conn.execute(
            "SELECT status, COUNT(*) AS c FROM roulette_spins GROUP BY status")}
        by_tier = {r["tier"]: r["c"] for r in conn.execute(
            "SELECT tier, COUNT(*) AS c FROM roulette_spins WHERE status = 'done' AND tier IS NOT NULL GROUP BY tier")}
        days = conn.execute(
            "SELECT COALESCE(SUM(prize_amount),0) AS s FROM roulette_spins WHERE status='done' AND prize_type='days'"
        ).fetchone()["s"]
        rub = conn.execute(
            "SELECT COALESCE(SUM(prize_amount),0) AS s FROM roulette_spins WHERE status='done' AND prize_type='rub'"
        ).fetchone()["s"]
        users = conn.execute("SELECT COUNT(DISTINCT user_id) AS c FROM roulette_spins").fetchone()["c"]
        week = conn.execute(
            "SELECT COUNT(*) AS c FROM roulette_spins WHERE created_at > datetime('now','-7 days')"
        ).fetchone()["c"]
    return {"by_status": by_status, "by_tier": by_tier, "days": int(days), "rub": int(rub),
            "users": int(users), "week": int(week)}
