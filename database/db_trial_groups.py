"""
Учёт использования пробного периода в режиме 'per_group' — каждый
пользователь может взять по одному пробнику в КАЖДОЙ группе тарифов, у
которой настроен свой пробный тариф. Отдельно от старого общего флага
users.used_trial, который продолжает работать в режиме 'account'.
"""
import logging
from typing import Optional
from .connection import get_db

logger = logging.getLogger(__name__)

__all__ = [
    'has_used_group_trial',
    'mark_group_trial_used',
    'get_claimed_trial_group_ids',
    'user_has_any_keys_ever',
    'get_eligible_trial_group_ids',
]


def has_used_group_trial(user_id: int, group_id: int) -> bool:
    """Проверяет, брал ли пользователь уже пробник именно в этой группе."""
    with get_db() as conn:
        row = conn.execute(
            "SELECT 1 FROM user_group_trials WHERE user_id = ? AND group_id = ?",
            (user_id, group_id),
        ).fetchone()
        return row is not None


def mark_group_trial_used(user_id: int, group_id: int) -> None:
    """Отмечает, что пользователь взял пробник в этой группе."""
    with get_db() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO user_group_trials (user_id, group_id) VALUES (?, ?)",
            (user_id, group_id),
        )
        logger.info(f"Пользователь ID {user_id} использовал пробник группы {group_id}")


def get_claimed_trial_group_ids(user_id: int) -> set:
    """Возвращает id групп, в которых пользователь уже взял пробник."""
    with get_db() as conn:
        rows = conn.execute(
            "SELECT group_id FROM user_group_trials WHERE user_id = ?",
            (user_id,),
        ).fetchall()
        return {row['group_id'] for row in rows}


def user_has_any_keys_ever(user_id: int) -> bool:
    """Проверяет, был ли у пользователя вообще хоть один VPN-ключ
    (пробный или платный) за всё время — не только сейчас активный."""
    with get_db() as conn:
        row = conn.execute(
            "SELECT 1 FROM vpn_keys WHERE user_id = ? LIMIT 1",
            (user_id,),
        ).fetchone()
        return row is not None


def get_eligible_trial_group_ids(user_id: int, group_ids: list) -> list:
    """
    Фильтрует список id групп, оставляя только те, где пользователь
    ДЕЙСТВИТЕЛЬНО может взять пробник прямо сейчас.

    Правило: САМЫЙ ПЕРВЫЙ пробник вообще доступен только тем, у кого
    никогда не было ни одного ключа (ни пробного, ни платного) — это
    отсекает существующих клиентов. Но как только пользователь взял хотя
    бы один пробник через эту систему, дальше он может брать по одному
    пробнику в КАЖДОЙ оставшейся группе как обычно — сам факт наличия
    ключа (уже от пробника) больше не блокирует.
    """
    claimed = get_claimed_trial_group_ids(user_id)
    already_in_system = bool(claimed)
    if not already_in_system and user_has_any_keys_ever(user_id):
        # Не в системе пробников по группам, и уже есть ключ откуда-то ещё
        # (платный тариф или старый общий пробник) — значит не "новый".
        return []
    return [gid for gid in group_ids if gid not in claimed]
