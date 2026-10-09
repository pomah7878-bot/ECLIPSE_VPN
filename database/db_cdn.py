"""CDN-пакеты: платный/выданный админом доступ к CDN-инбаунду (обход белых списков).

Один пакет на ключ. Сам доступ — отдельный клиент панели ``cdn_<email>`` с тем же
subId, поэтому он попадает в подписку ключа; лимит ГБ и срок панель считает сама.
"""
from __future__ import annotations

import logging
import sqlite3
from datetime import datetime
from typing import Any, Dict, Iterable, List, Optional

from database.connection import get_db

logger = logging.getLogger(__name__)

STATUS_ACTIVE = "active"
STATUS_EXHAUSTED = "exhausted"
STATUS_EXPIRED = "expired"
STATUS_SUSPENDED = "suspended"
STATUS_REVOKED = "revoked"

_UPDATABLE = {
    "user_id", "server_id", "panel_email", "limit_bytes", "used_bytes", "expires_at",
    "status", "is_free", "notified_pct",
}

TIME_FORMAT = "%Y-%m-%d %H:%M:%S"


def create_table(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS cdn_packs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            vpn_key_id INTEGER NOT NULL UNIQUE,
            user_id INTEGER,
            server_id INTEGER,
            panel_email TEXT,
            limit_bytes INTEGER NOT NULL DEFAULT 0,
            used_bytes INTEGER NOT NULL DEFAULT 0,
            expires_at TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'active',
            is_free INTEGER NOT NULL DEFAULT 0,
            notified_pct INTEGER NOT NULL DEFAULT 0,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            updated_at TEXT DEFAULT CURRENT_TIMESTAMP
        )
        """
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_cdn_packs_status ON cdn_packs(status)")


def parse_time(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    text = str(value).strip().replace("T", " ")[:19]
    try:
        return datetime.strptime(text, TIME_FORMAT)
    except ValueError:
        return None


def format_time(value: datetime) -> str:
    return value.strftime(TIME_FORMAT)


def get_pack(key_id: int) -> Optional[Dict[str, Any]]:
    with get_db() as conn:
        row = conn.execute("SELECT * FROM cdn_packs WHERE vpn_key_id = ?", (key_id,)).fetchone()
        return dict(row) if row else None


def list_packs(statuses: Optional[Iterable[str]] = None) -> List[Dict[str, Any]]:
    with get_db() as conn:
        if statuses:
            marks = ",".join("?" for _ in statuses)
            rows = conn.execute(
                f"SELECT * FROM cdn_packs WHERE status IN ({marks}) ORDER BY id", tuple(statuses)
            ).fetchall()
        else:
            rows = conn.execute("SELECT * FROM cdn_packs ORDER BY id").fetchall()
        return [dict(r) for r in rows]


def save_pack(
    key_id: int,
    *,
    user_id: Optional[int],
    server_id: Optional[int],
    panel_email: str,
    limit_bytes: int,
    expires_at: datetime,
    is_free: bool,
    status: str = STATUS_ACTIVE,
) -> None:
    """Создаёт пакет или полностью заменяет (покупка/выдача нового пакета: счётчик с нуля)."""
    with get_db() as conn:
        conn.execute(
            """
            INSERT INTO cdn_packs (vpn_key_id, user_id, server_id, panel_email, limit_bytes,
                                   used_bytes, expires_at, status, is_free, notified_pct)
            VALUES (?, ?, ?, ?, ?, 0, ?, ?, ?, 0)
            ON CONFLICT(vpn_key_id) DO UPDATE SET
                user_id = excluded.user_id,
                server_id = excluded.server_id,
                panel_email = excluded.panel_email,
                limit_bytes = excluded.limit_bytes,
                used_bytes = 0,
                expires_at = excluded.expires_at,
                status = excluded.status,
                is_free = excluded.is_free,
                notified_pct = 0,
                updated_at = CURRENT_TIMESTAMP
            """,
            (key_id, user_id, server_id, panel_email, int(limit_bytes),
             format_time(expires_at), status, 1 if is_free else 0),
        )


def update_pack(key_id: int, **fields: Any) -> bool:
    data = {k: v for k, v in fields.items() if k in _UPDATABLE}
    if not data:
        return False
    if isinstance(data.get("expires_at"), datetime):
        data["expires_at"] = format_time(data["expires_at"])
    sets = ", ".join(f"{k} = ?" for k in data)
    with get_db() as conn:
        cur = conn.execute(
            f"UPDATE cdn_packs SET {sets}, updated_at = CURRENT_TIMESTAMP WHERE vpn_key_id = ?",
            (*data.values(), key_id),
        )
        return cur.rowcount > 0


def delete_pack(key_id: int) -> None:
    with get_db() as conn:
        conn.execute("DELETE FROM cdn_packs WHERE vpn_key_id = ?", (key_id,))


def get_cdn_emails() -> set:
    """Все email CDN-клиентов (в нижнем регистре) — чтобы не считать их «сиротами» панели."""
    with get_db() as conn:
        rows = conn.execute("SELECT panel_email FROM cdn_packs WHERE panel_email IS NOT NULL").fetchall()
    return {("cdn_" + str(r["panel_email"])).lower() for r in rows}
