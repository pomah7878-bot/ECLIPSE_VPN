"""Заявки на бонусные дни за задания (отзыв, сторис)."""
from typing import Any, Dict, List, Optional

from .connection import get_db

_FIELDS = "id, user_id, telegram_id, task, status, file_id, days, created_at, decided_at, decided_by"


def _row(row) -> Optional[Dict[str, Any]]:
    if row is None:
        return None
    keys = [k.strip() for k in _FIELDS.split(",")]
    return dict(zip(keys, tuple(row)))


def get_active_claim(user_id: int, task: str) -> Optional[Dict[str, Any]]:
    """Заявка на проверке или уже одобренная (отклонённая не мешает прислать снова)."""
    with get_db() as conn:
        row = conn.execute(
            f"SELECT {_FIELDS} FROM bonus_task_claims WHERE user_id = ? AND task = ? "
            "AND status IN ('pending', 'approved') ORDER BY id DESC LIMIT 1",
            (user_id, task),
        ).fetchone()
    return _row(row)


def create_claim(user_id: int, telegram_id: int, task: str, file_id: str, days: int) -> Optional[int]:
    """Создаёт заявку. None — у клиента уже есть заявка на проверке или награда получена."""
    with get_db() as conn:
        exists = conn.execute(
            "SELECT 1 FROM bonus_task_claims WHERE user_id = ? AND task = ? AND status IN ('pending', 'approved')",
            (user_id, task),
        ).fetchone()
        if exists:
            return None
        cur = conn.execute(
            "INSERT INTO bonus_task_claims (user_id, telegram_id, task, status, file_id, days) "
            "VALUES (?, ?, ?, 'pending', ?, ?)",
            (user_id, telegram_id, task, file_id, int(days)),
        )
        return int(cur.lastrowid)


def get_claim(claim_id: int) -> Optional[Dict[str, Any]]:
    with get_db() as conn:
        row = conn.execute(f"SELECT {_FIELDS} FROM bonus_task_claims WHERE id = ?", (claim_id,)).fetchone()
    return _row(row)


def decide_claim(claim_id: int, status: str, admin_telegram_id: int) -> bool:
    """Переводит заявку из «на проверке» в approved/rejected. False — уже решена другим админом."""
    if status not in ("approved", "rejected"):
        raise ValueError(status)
    with get_db() as conn:
        cur = conn.execute(
            "UPDATE bonus_task_claims SET status = ?, decided_at = CURRENT_TIMESTAMP, decided_by = ? "
            "WHERE id = ? AND status = 'pending'",
            (status, admin_telegram_id, claim_id),
        )
        return cur.rowcount == 1


def reopen_claim(claim_id: int) -> None:
    """Возвращает заявку на проверку (если начислить дни не получилось)."""
    with get_db() as conn:
        conn.execute(
            "UPDATE bonus_task_claims SET status = 'pending', decided_at = NULL, decided_by = NULL WHERE id = ?",
            (claim_id,),
        )


def list_pending(limit: int = 10) -> List[Dict[str, Any]]:
    with get_db() as conn:
        rows = conn.execute(
            f"SELECT {_FIELDS} FROM bonus_task_claims WHERE status = 'pending' ORDER BY id LIMIT ?",
            (limit,),
        ).fetchall()
    return [_row(r) for r in rows]


def list_recent(limit: int = 15) -> List[Dict[str, Any]]:
    """v1.424: последние заявки любого статуса с именем клиента — для списка «кто делился» в админке."""
    with get_db() as conn:
        rows = conn.execute(
            "SELECT c.id, c.telegram_id, c.task, c.status, c.days, c.created_at, c.decided_at, "
            "u.username, u.first_name FROM bonus_task_claims c LEFT JOIN users u ON u.id = c.user_id "
            "ORDER BY c.id DESC LIMIT ?",
            (max(1, min(int(limit), 50)),),
        ).fetchall()
    return [dict(r) for r in rows]


def stats() -> Dict[str, int]:
    out = {"pending": 0, "approved": 0, "rejected": 0, "days": 0}
    with get_db() as conn:
        for status, cnt, days in conn.execute(
            "SELECT status, COUNT(*), COALESCE(SUM(days), 0) FROM bonus_task_claims GROUP BY status"
        ).fetchall():
            out[status] = int(cnt)
            if status == "approved":
                out["days"] = int(days)
    return out
