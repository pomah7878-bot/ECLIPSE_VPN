"""Отложенное удаление клиентов с панели (v1.293).

Если при замене ключа старый сервер был недоступен, старый клиент остаётся
на панели и после возврата сервера снова работает — у клиента два доступа.
Такие клиенты записываются сюда и удаляются фоново, когда сервер ответит.
"""
import logging
import time

from database.connection import get_db

logger = logging.getLogger(__name__)

RETRY_INTERVAL_SECONDS = 600
MAX_AGE_DAYS = 30
BATCH_LIMIT = 20
_last_run = 0.0


def _ensure_table(conn) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS deferred_panel_deletes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            server_id INTEGER NOT NULL,
            panel_email TEXT,
            by_email INTEGER NOT NULL DEFAULT 1,
            inbound_id INTEGER,
            client_uuid TEXT,
            key_id INTEGER,
            attempts INTEGER NOT NULL DEFAULT 0,
            last_error TEXT,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP
        )
        """
    )


def schedule_deferred_delete(*, server_id, panel_email, by_email, inbound_id=None,
                             client_uuid=None, key_id=None) -> bool:
    if not server_id or not (panel_email if by_email else (inbound_id and client_uuid)):
        return False
    with get_db() as conn:
        _ensure_table(conn)
        conn.execute(
            "INSERT INTO deferred_panel_deletes (server_id, panel_email, by_email, inbound_id, client_uuid, key_id) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (int(server_id), panel_email, 1 if by_email else 0, inbound_id, client_uuid, key_id),
        )
    logger.warning(f"Старый клиент ключа {key_id} (сервер {server_id}) будет удалён позже")
    return True


def _pending_rows(limit: int) -> list:
    with get_db() as conn:
        _ensure_table(conn)
        conn.execute(
            "DELETE FROM deferred_panel_deletes WHERE created_at < datetime('now', ?)",
            (f"-{MAX_AGE_DAYS} days",),
        )
        rows = conn.execute(
            "SELECT * FROM deferred_panel_deletes ORDER BY id LIMIT ?", (int(limit),)
        ).fetchall()
        return [dict(r) for r in rows]


def _finish(row_id: int, error: str | None) -> None:
    with get_db() as conn:
        if error is None:
            conn.execute("DELETE FROM deferred_panel_deletes WHERE id = ?", (row_id,))
        else:
            conn.execute(
                "UPDATE deferred_panel_deletes SET attempts = attempts + 1, last_error = ? WHERE id = ?",
                (error[:300], row_id),
            )


def _is_not_found(message: str) -> bool:
    text = (message or '').lower()
    return 'not found' in text or 'не найден' in text or 'no client remained' in text


async def run_deferred_panel_deletes(force: bool = False) -> int:
    """Пробует удалить отложенных клиентов (не чаще раза в 10 минут). Возвращает число удалённых."""
    global _last_run
    now = time.monotonic()
    if not force and _last_run and now - _last_run < RETRY_INTERVAL_SECONDS:
        return 0
    _last_run = now
    rows = _pending_rows(BATCH_LIMIT)
    if not rows:
        return 0
    from bot.services.vpn_api import get_client

    done = 0
    for row in rows:
        try:
            client = await get_client(int(row['server_id']))
            if row['by_email']:
                await client.delete_clients_by_email_on_server(row['panel_email'])
            else:
                await client.delete_client(int(row['inbound_id']), row['client_uuid'])
            _finish(row['id'], None)
            done += 1
        except Exception as e:  # noqa: BLE001
            if _is_not_found(str(e)):
                _finish(row['id'], None)
                done += 1
            else:
                _finish(row['id'], str(e))
    if done:
        logger.info(f"Отложенно удалено старых клиентов с панели: {done}")
    return done


__all__ = ['schedule_deferred_delete', 'run_deferred_panel_deletes']
