"""Единый реестр выдачи пробного периода и журнал попыток повторной выдачи.

Зачем отдельная таблица, а не флаг users.used_trial:
- у человека может быть несколько личностей (Telegram, сайт-аккаунт,
  подтверждённый номер) — пробник должен блокироваться по любой из них;
- у пробника есть область действия: на аккаунт (scope='account') или на
  конкретную группу тарифов (scope='group:<id>'), и они не должны
  смешиваться при смене режима админом;
- нужен атомарный захват: проверка «ещё не брал» и запись факта выдачи
  должны быть одной транзакцией, иначе двойное нажатие даёт два ключа.

trial_abuse_events — попытки получить пробник повторно (для анализа).
trial_blocks — запрет пробника конкретной личности (ручной или авто).
"""
import sqlite3
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from .connection import get_db

Identity = Tuple[str, str]

__all__ = [
    'find_trial_claim',
    'find_any_trial_claim',
    'add_trial_claim',
    'ensure_trial_claims',
    'release_trial_claims',
    'recent_trial_claims',
    'add_trial_abuse_event',
    'count_recent_trial_abuse',
    'recent_trial_abuse',
    'get_trial_abuse_event',
    'find_trial_block',
    'add_trial_block',
    'remove_trial_block',
    'list_trial_blocks',
    'trial_alert_recently_sent',
    'mark_trial_alert_sent',
    'trial_guard_stats',
]


def _dedupe(identities: Iterable[Identity]) -> List[Identity]:
    """Убирает пустые и повторяющиеся личности, приводит значения к строке."""
    seen = set()
    result: List[Identity] = []
    for item in identities or ():
        try:
            kind, value = item
        except (TypeError, ValueError):
            continue
        kind = str(kind or '').strip()
        value = str(value or '').strip()
        if not kind or not value:
            continue
        pair = (kind, value)
        if pair in seen:
            continue
        seen.add(pair)
        result.append(pair)
    return result


def find_trial_claim(identities: Iterable[Identity], scope: str) -> Optional[Dict[str, Any]]:
    """Первая уже выданная заявка по любой из личностей в указанной области."""
    pairs = _dedupe(identities)
    if not pairs:
        return None
    with get_db() as conn:
        for kind, value in pairs:
            row = conn.execute(
                "SELECT * FROM trial_claims "
                "WHERE identity_kind = ? AND identity_value = ? AND scope = ?",
                (kind, value, scope),
            ).fetchone()
            if row:
                return dict(row)
    return None


def find_any_trial_claim(identities: Iterable[Identity]) -> Optional[Dict[str, Any]]:
    """Любая заявка по этим личностям, в любой области (account или group:*)."""
    pairs = _dedupe(identities)
    if not pairs:
        return None
    with get_db() as conn:
        for kind, value in pairs:
            row = conn.execute(
                "SELECT * FROM trial_claims "
                "WHERE identity_kind = ? AND identity_value = ? ORDER BY id LIMIT 1",
                (kind, value),
            ).fetchone()
            if row:
                return dict(row)
    return None


def ensure_trial_claims(
    identities: Iterable[Identity],
    scope: str = 'account',
    channel: str = 'link',
) -> int:
    """Добавляет отметки, не падая на уже существующих (перенос между каналами)."""
    pairs = _dedupe(identities)
    added = 0
    with get_db() as conn:
        for kind, value in pairs:
            cur = conn.execute(
                "INSERT OR IGNORE INTO trial_claims "
                "(identity_kind, identity_value, scope, channel) VALUES (?, ?, ?, ?)",
                (kind, value, scope, channel),
            )
            added += int(cur.rowcount or 0)
    return added


def add_trial_claim(
    identities: Iterable[Identity],
    scope: str,
    channel: str = 'unknown',
) -> Dict[str, Any]:
    """Атомарно записывает выдачу по всем личностям.

    Возвращает {'ok': bool, 'conflict': (kind, value) | None, 'claim_ids': [...]}.
    Если хотя бы одна личность уже имеет заявку в этой области — не пишет
    ничего и возвращает конфликт. Транзакция BEGIN IMMEDIATE не даёт двум
    одновременным запросам пройти проверку до записи.
    """
    pairs = _dedupe(identities)
    if not pairs:
        return {'ok': False, 'conflict': None, 'claim_ids': []}

    with get_db() as conn:
        try:
            conn.execute("BEGIN IMMEDIATE")
        except sqlite3.OperationalError:
            # Транзакция уже начата вызывающим кодом — работаем в ней.
            pass
        for kind, value in pairs:
            row = conn.execute(
                "SELECT id FROM trial_claims "
                "WHERE identity_kind = ? AND identity_value = ? AND scope = ?",
                (kind, value, scope),
            ).fetchone()
            if row:
                return {'ok': False, 'conflict': (kind, value), 'claim_ids': []}

        claim_ids: List[int] = []
        for kind, value in pairs:
            cur = conn.execute(
                "INSERT OR IGNORE INTO trial_claims "
                "(identity_kind, identity_value, scope, channel) VALUES (?, ?, ?, ?)",
                (kind, value, scope, channel),
            )
            if cur.rowcount == 0:
                # Проиграли гонку между SELECT и INSERT — откатываем всё.
                conn.rollback()
                return {'ok': False, 'conflict': (kind, value), 'claim_ids': []}
            claim_ids.append(int(cur.lastrowid))
        return {'ok': True, 'conflict': None, 'claim_ids': claim_ids}


def release_trial_claims(claim_ids: Sequence[int]) -> None:
    """Откатывает заявку, если ключ так и не был создан (сбой провижининга)."""
    ids = [int(cid) for cid in (claim_ids or []) if str(cid).isdigit()]
    if not ids:
        return
    with get_db() as conn:
        for cid in ids:
            conn.execute("DELETE FROM trial_claims WHERE id = ?", (cid,))


def recent_trial_claims(limit: int = 10) -> List[Dict[str, Any]]:
    with get_db() as conn:
        rows = conn.execute(
            "SELECT * FROM trial_claims ORDER BY id DESC LIMIT ?",
            (max(1, int(limit)),),
        ).fetchall()
    return [dict(row) for row in rows]


def add_trial_abuse_event(
    identity_kind: str,
    identity_value: str,
    scope: str,
    channel: str,
    reason: str,
    ip: str = '',
    user_agent: str = '',
    detail: str = '',
    retention_days: int = 90,
) -> int:
    """Пишет попытку повторной выдачи и чистит старые записи."""
    with get_db() as conn:
        cur = conn.execute(
            "INSERT INTO trial_abuse_events "
            "(identity_kind, identity_value, scope, channel, reason, ip, user_agent, detail) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (str(identity_kind), str(identity_value), str(scope), str(channel),
             str(reason), (ip or '')[:64], (user_agent or '')[:300], (detail or '')[:500]),
        )
        try:
            conn.execute(
                "DELETE FROM trial_abuse_events WHERE created_at < datetime('now', ?)",
                (f"-{max(1, int(retention_days))} days",),
            )
        except Exception:
            pass
        return int(cur.lastrowid)


def count_recent_trial_abuse(identity_kind: str, identity_value: str, hours: int = 24) -> int:
    with get_db() as conn:
        row = conn.execute(
            "SELECT COUNT(*) AS cnt FROM trial_abuse_events "
            "WHERE identity_kind = ? AND identity_value = ? "
            "AND created_at >= datetime('now', ?)",
            (str(identity_kind), str(identity_value), f"-{max(1, int(hours))} hours"),
        ).fetchone()
    return int(row['cnt'] or 0) if row else 0


def recent_trial_abuse(limit: int = 20) -> List[Dict[str, Any]]:
    with get_db() as conn:
        rows = conn.execute(
            "SELECT * FROM trial_abuse_events ORDER BY id DESC LIMIT ?",
            (max(1, int(limit)),),
        ).fetchall()
    return [dict(row) for row in rows]


def get_trial_abuse_event(event_id: int) -> Optional[Dict[str, Any]]:
    with get_db() as conn:
        row = conn.execute(
            "SELECT * FROM trial_abuse_events WHERE id = ?", (int(event_id),)
        ).fetchone()
    return dict(row) if row else None


def find_trial_block(identities: Iterable[Identity]) -> Optional[Dict[str, Any]]:
    pairs = _dedupe(identities)
    if not pairs:
        return None
    with get_db() as conn:
        for kind, value in pairs:
            row = conn.execute(
                "SELECT * FROM trial_blocks WHERE identity_kind = ? AND identity_value = ?",
                (kind, value),
            ).fetchone()
            if row:
                return dict(row)
    return None


def add_trial_block(
    identity_kind: str,
    identity_value: str,
    reason: str = '',
    created_by: str = 'auto',
) -> Optional[int]:
    """Ставит запрет пробника. Возвращает id, если запись создана сейчас."""
    with get_db() as conn:
        cur = conn.execute(
            "INSERT OR IGNORE INTO trial_blocks "
            "(identity_kind, identity_value, reason, created_by) VALUES (?, ?, ?, ?)",
            (str(identity_kind), str(identity_value), (reason or '')[:300], str(created_by)[:64]),
        )
        if cur.rowcount == 0:
            return None
        return int(cur.lastrowid)


def remove_trial_block(block_id: int) -> Optional[Dict[str, Any]]:
    """Снимает запрет и возвращает удалённую запись (для разбана авто-блокировки)."""
    with get_db() as conn:
        row = conn.execute(
            "SELECT * FROM trial_blocks WHERE id = ?", (int(block_id),)
        ).fetchone()
        if not row:
            return None
        conn.execute("DELETE FROM trial_blocks WHERE id = ?", (int(block_id),))
        return dict(row)


def list_trial_blocks(limit: int = 20) -> List[Dict[str, Any]]:
    with get_db() as conn:
        rows = conn.execute(
            "SELECT * FROM trial_blocks ORDER BY id DESC LIMIT ?",
            (max(1, int(limit)),),
        ).fetchall()
    return [dict(row) for row in rows]


def trial_alert_recently_sent(identity_kind: str, identity_value: str, hours: int = 24) -> bool:
    with get_db() as conn:
        row = conn.execute(
            "SELECT 1 FROM trial_abuse_alerts "
            "WHERE identity_kind = ? AND identity_value = ? "
            "AND alerted_at >= datetime('now', ?)",
            (str(identity_kind), str(identity_value), f"-{max(1, int(hours))} hours"),
        ).fetchone()
    return row is not None


def mark_trial_alert_sent(identity_kind: str, identity_value: str) -> None:
    with get_db() as conn:
        conn.execute(
            "INSERT INTO trial_abuse_alerts (identity_kind, identity_value, alerted_at) "
            "VALUES (?, ?, CURRENT_TIMESTAMP) "
            "ON CONFLICT(identity_kind, identity_value) "
            "DO UPDATE SET alerted_at = CURRENT_TIMESTAMP",
            (str(identity_kind), str(identity_value)),
        )


def trial_guard_stats() -> Dict[str, int]:
    with get_db() as conn:
        claims = conn.execute("SELECT COUNT(*) AS c FROM trial_claims").fetchone()['c']
        events_24h = conn.execute(
            "SELECT COUNT(*) AS c FROM trial_abuse_events "
            "WHERE created_at >= datetime('now', '-1 day')"
        ).fetchone()['c']
        blocks = conn.execute("SELECT COUNT(*) AS c FROM trial_blocks").fetchone()['c']
    return {'claims': int(claims or 0), 'events_24h': int(events_24h or 0), 'blocks': int(blocks or 0)}
