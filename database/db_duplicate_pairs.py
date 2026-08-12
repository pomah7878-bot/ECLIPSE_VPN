"""
Хранение найденных потенциальных дубликатов клиентов (ботовый + вручную
созданный клиент с пересекающимися IP-адресами подключения).
"""
import logging
from typing import Any, Dict, List, Optional
from .connection import get_db

logger = logging.getLogger(__name__)

__all__ = [
    'save_duplicate_pair',
    'get_pending_duplicate_pairs',
    'get_duplicate_pair_by_id',
    'mark_duplicate_pair_resolved',
    'mark_duplicate_pair_ignored',
]


def save_duplicate_pair(server_id: int, bot_email: str, manual_email: str, shared_ips: str) -> bool:
    """
    Сохраняет найденную пару, если такая ещё не зафиксирована.

    Returns:
        True, если это НОВАЯ пара (реально вставлена) — используется,
        чтобы уведомлять админа только про новые находки, а не повторно
        про уже известные (и, возможно, уже проигнорированные) пары.
    """
    with get_db() as conn:
        cursor = conn.execute(
            """INSERT OR IGNORE INTO detected_duplicate_pairs
               (server_id, bot_email, manual_email, shared_ips)
               VALUES (?, ?, ?, ?)""",
            (server_id, bot_email, manual_email, shared_ips),
        )
        return cursor.rowcount > 0


def get_pending_duplicate_pairs(limit: int = 20) -> List[Dict[str, Any]]:
    """Возвращает ещё не рассмотренные пары."""
    with get_db() as conn:
        rows = conn.execute(
            """SELECT id, server_id, bot_email, manual_email, shared_ips, detected_at
               FROM detected_duplicate_pairs
               WHERE status = 'pending'
               ORDER BY detected_at DESC
               LIMIT ?""",
            (limit,),
        ).fetchall()
        return [dict(row) for row in rows]


def get_duplicate_pair_by_id(pair_id: int) -> Optional[Dict[str, Any]]:
    """Возвращает одну пару по ID."""
    with get_db() as conn:
        row = conn.execute(
            "SELECT * FROM detected_duplicate_pairs WHERE id = ?",
            (pair_id,),
        ).fetchone()
        return dict(row) if row else None


def mark_duplicate_pair_resolved(pair_id: int) -> None:
    """Отмечает пару как решённую (объединена или дубликат удалён)."""
    with get_db() as conn:
        conn.execute(
            "UPDATE detected_duplicate_pairs SET status = 'resolved', resolved_at = datetime('now') WHERE id = ?",
            (pair_id,),
        )


def mark_duplicate_pair_ignored(pair_id: int) -> None:
    """Отмечает пару как проигнорированную (админ решил, что это не дубликат)."""
    with get_db() as conn:
        conn.execute(
            "UPDATE detected_duplicate_pairs SET status = 'ignored', resolved_at = datetime('now') WHERE id = ?",
            (pair_id,),
        )
