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
