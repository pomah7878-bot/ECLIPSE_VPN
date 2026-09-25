"""
Кэш лимитированных ссылок Happ (happ-proxy.com API) — таблица
happ_install_links (миграция v129). Один install_code на sub_id ключа,
переиспользуется при повторных импортах той же подписки, чтобы не плодить
новые install-записи в личном кабинете happ-proxy.com на каждый клик.
"""
import logging
from typing import Optional, Dict, Any, List

from .connection import get_db

logger = logging.getLogger(__name__)

__all__ = [
    'get_happ_install_link',
    'save_happ_install_link',
    'get_happ_install_link_by_sub_id',
    'list_happ_install_links',
    'delete_happ_install_link',
]


def get_happ_install_link(sub_id: str) -> Optional[Dict[str, Any]]:
    """Возвращает {sub_id, install_code, install_id, install_limit,
    created_at} для данного sub_id, либо None, если ещё не создавалась."""
    with get_db() as conn:
        row = conn.execute(
            "SELECT sub_id, install_code, install_id, install_limit, created_at "
            "FROM happ_install_links WHERE sub_id = ?",
            (sub_id,),
        ).fetchone()
    return dict(row) if row else None


# Совместимость с именем, использованным при проектировании (алиас).
get_happ_install_link_by_sub_id = get_happ_install_link


def save_happ_install_link(sub_id: str, install_code: str, install_id: Optional[int], install_limit: int) -> None:
    """Сохраняет/обновляет запись кэша (INSERT OR REPLACE по sub_id)."""
    with get_db() as conn:
        conn.execute(
            """
            INSERT INTO happ_install_links (sub_id, install_code, install_id, install_limit)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(sub_id) DO UPDATE SET
                install_code = excluded.install_code,
                install_id = excluded.install_id,
                install_limit = excluded.install_limit
            """,
            (sub_id, install_code, install_id, install_limit),
        )


def list_happ_install_links() -> List[Dict[str, Any]]:
    with get_db() as conn:
        rows = conn.execute(
            "SELECT sub_id, install_code, install_id, install_limit, created_at FROM happ_install_links"
        ).fetchall()
    return [dict(r) for r in rows]


def delete_happ_install_link(sub_id: str) -> None:
    with get_db() as conn:
        conn.execute("DELETE FROM happ_install_links WHERE sub_id = ?", (sub_id,))
