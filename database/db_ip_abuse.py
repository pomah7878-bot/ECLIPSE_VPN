"""Учёт превышений лимита IP и временных/постоянных блокировок.

Один ряд на пользователя: сколько эпизодов нарушения, когда последний,
до какого времени действует временная блокировка, постоянный ли он.
Сами решения (предупредить/заблокировать/забанить) принимает
bot/services/ip_abuse.py; здесь только данные.
"""
import sqlite3
from typing import Any, Dict, List, Optional

from .connection import get_db

__all__ = [
    'get_ip_abuse',
    'record_ip_abuse_episode',
    'mark_ip_abuse_blocked',
    'mark_ip_abuse_permanent',
    'clear_ip_abuse_block',
    'reset_ip_abuse',
    'release_ip_abuse_candidates',
    'list_ip_abusers',
    'log_ip_abuse_event',
    'recent_ip_abuse_events',
    'ip_abuse_stats',
]


def get_ip_abuse(user_id: int) -> Optional[Dict[str, Any]]:
    with get_db() as conn:
        row = conn.execute(
            "SELECT * FROM ip_abuse WHERE user_id = ?", (int(user_id),)
        ).fetchone()
    return dict(row) if row else None


def record_ip_abuse_episode(
    user_id: int,
    key_id: Optional[int],
    used: int,
    limit_ips: int,
    *,
    cooldown_sec: int = 12 * 3600,
    reset_days: int = 30,
) -> Dict[str, Any]:
    """Фиксирует эпизод нарушения с защитой от повторного счёта каждые 10 минут.

    Возвращает {'counted': bool, 'strikes': int, 'previous': int,
    'blocked_until': str|None, 'permanent': bool}.
    """
    user_id = int(user_id)
    key_id = int(key_id) if key_id else None
    reset_sec = max(1, int(reset_days)) * 86400
    cooldown_sec = max(0, int(cooldown_sec))

    with get_db() as conn:
        row = conn.execute(
            "SELECT strikes, permanent, blocked_until, "
            "CAST(strftime('%s','now') AS INTEGER) - "
            "  CAST(strftime('%s', COALESCE(last_strike_at, '1970-01-01 00:00:00')) AS INTEGER) AS age_sec "
            "FROM ip_abuse WHERE user_id = ?",
            (user_id,),
        ).fetchone()
        if row is None:
            conn.execute(
                "INSERT INTO ip_abuse "
                "(user_id, key_id, strikes, first_strike_at, last_strike_at, last_seen_at, last_used, last_limit, updated_at) "
                "VALUES (?, ?, 1, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP, ?, ?, CURRENT_TIMESTAMP)",
                (user_id, key_id, int(used), int(limit_ips)),
            )
            return {'counted': True, 'strikes': 1, 'previous': 0, 'blocked_until': None, 'permanent': False}

        age = int(row['age_sec'] or 0)
        previous = int(row['strikes'] or 0)
        permanent = bool(row['permanent'])

        # Старые страйки сбрасываются, если человек давно не превышал лимит.
        if age > reset_sec:
            previous = 0
        # В пределах кулдауна не наращиваем страйки: одна и та же серия не должна
        # превращаться в блокировку каждые 10 минут.
        if permanent or (age < cooldown_sec and previous > 0):
            conn.execute(
                "UPDATE ip_abuse SET last_seen_at = CURRENT_TIMESTAMP, last_used = ?, last_limit = ?, "
                "key_id = COALESCE(?, key_id), updated_at = CURRENT_TIMESTAMP WHERE user_id = ?",
                (int(used), int(limit_ips), key_id, user_id),
            )
            return {
                'counted': False,
                'strikes': previous,
                'previous': previous,
                'blocked_until': row['blocked_until'],
                'permanent': permanent,
            }

        strikes = previous + 1
        conn.execute(
            "UPDATE ip_abuse SET strikes = ?, first_strike_at = COALESCE(first_strike_at, CURRENT_TIMESTAMP), "
            "last_strike_at = CURRENT_TIMESTAMP, last_seen_at = CURRENT_TIMESTAMP, last_used = ?, last_limit = ?, "
            "key_id = COALESCE(?, key_id), updated_at = CURRENT_TIMESTAMP WHERE user_id = ?",
            (strikes, int(used), int(limit_ips), key_id, user_id),
        )
        blocked_until = conn.execute(
            "SELECT blocked_until FROM ip_abuse WHERE user_id = ?", (user_id,)
        ).fetchone()
        return {
            'counted': True,
            'strikes': strikes,
            'previous': previous,
            'blocked_until': blocked_until['blocked_until'] if blocked_until else None,
            'permanent': False,
        }


def mark_ip_abuse_blocked(user_id: int, hours: int) -> Optional[str]:
    """Ставит временную блокировку. Возвращает строку «до какого времени»."""
    with get_db() as conn:
        conn.execute(
            "UPDATE ip_abuse SET blocked_until = datetime('now', ?), permanent = 0, "
            "updated_at = CURRENT_TIMESTAMP WHERE user_id = ?",
            (f"+{max(1, int(hours))} hours", int(user_id)),
        )
        row = conn.execute(
            "SELECT blocked_until FROM ip_abuse WHERE user_id = ?", (int(user_id),)
        ).fetchone()
    return row['blocked_until'] if row else None


def mark_ip_abuse_permanent(user_id: int) -> None:
    with get_db() as conn:
        conn.execute(
            "UPDATE ip_abuse SET permanent = 1, blocked_until = NULL, "
            "updated_at = CURRENT_TIMESTAMP WHERE user_id = ?",
            (int(user_id),),
        )


def clear_ip_abuse_block(user_id: int, keep_strikes: bool = True) -> None:
    with get_db() as conn:
        if keep_strikes:
            conn.execute(
                "UPDATE ip_abuse SET blocked_until = NULL, updated_at = CURRENT_TIMESTAMP WHERE user_id = ?",
                (int(user_id),),
            )
        else:
            conn.execute("DELETE FROM ip_abuse WHERE user_id = ?", (int(user_id),))


def reset_ip_abuse(user_id: int, reset_strikes: bool = False) -> None:
    """Снимает временную и постоянную блокировку (ручное решение админа)."""
    with get_db() as conn:
        if reset_strikes:
            conn.execute("DELETE FROM ip_abuse WHERE user_id = ?", (int(user_id),))
        else:
            conn.execute(
                "UPDATE ip_abuse SET blocked_until = NULL, permanent = 0, "
                "updated_at = CURRENT_TIMESTAMP WHERE user_id = ?",
                (int(user_id),),
            )


def release_ip_abuse_candidates() -> List[Dict[str, Any]]:
    """Пользователи, у которых истекла временная блокировка."""
    with get_db() as conn:
        rows = conn.execute(
            "SELECT ia.user_id, u.telegram_id, u.username "
            "FROM ip_abuse ia JOIN users u ON u.id = ia.user_id "
            "WHERE ia.permanent = 0 AND ia.blocked_until IS NOT NULL "
            "AND ia.blocked_until <= CURRENT_TIMESTAMP"
        ).fetchall()
    return [dict(row) for row in rows]


def list_ip_abusers(limit: int = 20) -> List[Dict[str, Any]]:
    with get_db() as conn:
        rows = conn.execute(
            "SELECT ia.*, u.telegram_id, u.username, u.is_banned, "
            "CASE WHEN ia.blocked_until > CURRENT_TIMESTAMP THEN 1 ELSE 0 END AS block_active "
            "FROM ip_abuse ia JOIN users u ON u.id = ia.user_id "
            "ORDER BY ia.permanent DESC, ia.blocked_until IS NOT NULL DESC, ia.strikes DESC, ia.last_strike_at DESC "
            "LIMIT ?",
            (max(1, int(limit)),),
        ).fetchall()
    return [dict(row) for row in rows]


def log_ip_abuse_event(
    user_id: int,
    key_id: Optional[int],
    used: int,
    limit_ips: int,
    action: str,
    detail: str = '',
) -> None:
    with get_db() as conn:
        conn.execute(
            "INSERT INTO ip_abuse_events (user_id, key_id, used, limit_ips, action, detail) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (int(user_id), int(key_id) if key_id else None, int(used), int(limit_ips),
             str(action), (detail or '')[:500]),
        )


def recent_ip_abuse_events(limit: int = 20) -> List[Dict[str, Any]]:
    with get_db() as conn:
        rows = conn.execute(
            "SELECT * FROM ip_abuse_events ORDER BY id DESC LIMIT ?",
            (max(1, int(limit)),),
        ).fetchall()
    return [dict(row) for row in rows]


def ip_abuse_stats() -> Dict[str, int]:
    with get_db() as conn:
        total = conn.execute("SELECT COUNT(*) AS c FROM ip_abuse").fetchone()['c']
        blocked = conn.execute(
            "SELECT COUNT(*) AS c FROM ip_abuse WHERE permanent = 0 AND blocked_until > CURRENT_TIMESTAMP"
        ).fetchone()['c']
        permanent = conn.execute("SELECT COUNT(*) AS c FROM ip_abuse WHERE permanent = 1").fetchone()['c']
    return {'total': int(total or 0), 'blocked': int(blocked or 0), 'permanent': int(permanent or 0)}
