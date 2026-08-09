"""
Запланированные посты для маркетингового Telegram-канала.
Публикуются встроенным планировщиком бота (bot/services/scheduler.py) —
надёжнее, чем системный at/atd, который оказался нестабилен на сервере.
"""
import logging
from typing import Any, Dict, List, Optional
from .connection import get_db

logger = logging.getLogger(__name__)

__all__ = [
    'create_scheduled_post',
    'get_due_scheduled_posts',
    'mark_scheduled_post_sent',
    'mark_scheduled_post_failed',
    'get_all_scheduled_posts',
    'reschedule_post_to_next_day',
    'record_post_retry_error',
]


def create_scheduled_post(channel_id: str, content: str, scheduled_at: str) -> int:
    """
    Добавляет пост в очередь на публикацию.

    Args:
        channel_id: Username или ID канала (например, '@eclipse_unlimited_news')
        content: HTML-текст поста
        scheduled_at: Время публикации в формате 'YYYY-MM-DD HH:MM:SS' (UTC)

    Returns:
        ID созданной записи
    """
    with get_db() as conn:
        cursor = conn.execute(
            """INSERT INTO scheduled_channel_posts (channel_id, content, scheduled_at)
               VALUES (?, ?, ?)""",
            (channel_id, content, scheduled_at),
        )
        return cursor.lastrowid


def get_due_scheduled_posts(limit: int = 10) -> List[Dict[str, Any]]:
    """Возвращает посты, время публикации которых уже наступило."""
    with get_db() as conn:
        rows = conn.execute(
            """SELECT id, channel_id, content, scheduled_at
               FROM scheduled_channel_posts
               WHERE status = 'pending' AND scheduled_at <= datetime('now')
               ORDER BY scheduled_at ASC
               LIMIT ?""",
            (limit,),
        ).fetchall()
        return [dict(row) for row in rows]


def mark_scheduled_post_sent(post_id: int) -> None:
    """Отмечает пост как успешно опубликованный."""
    with get_db() as conn:
        conn.execute(
            "UPDATE scheduled_channel_posts SET status = 'sent', sent_at = datetime('now') WHERE id = ?",
            (post_id,),
        )


def mark_scheduled_post_failed(post_id: int, error_message: str) -> None:
    """Отмечает пост как неудавшийся, с текстом ошибки."""
    with get_db() as conn:
        conn.execute(
            "UPDATE scheduled_channel_posts SET status = 'failed', error_message = ? WHERE id = ?",
            (error_message[:1000], post_id),
        )


def reschedule_post_to_next_day(post_id: int) -> None:
    """
    Переносит пост на тот же час:минуту следующего дня — используется, когда
    все попытки публикации за текущий день исчерпаны (после 21:00 МСК).
    Статус остаётся 'pending', попытки продолжатся на следующий день.
    """
    with get_db() as conn:
        conn.execute(
            """UPDATE scheduled_channel_posts
               SET scheduled_at = datetime(scheduled_at, '+1 day')
               WHERE id = ?""",
            (post_id,),
        )


def record_post_retry_error(post_id: int, error_message: str) -> None:
    """
    Записывает ошибку неудачной попытки, НЕ меняя статус (пост остаётся
    'pending' и будет повторно опробован на следующем цикле планировщика,
    в пределах текущего дня до 21:00 МСК)."""
    with get_db() as conn:
        conn.execute(
            "UPDATE scheduled_channel_posts SET error_message = ? WHERE id = ?",
            (error_message[:1000], post_id),
        )


def get_all_scheduled_posts(limit: int = 20) -> List[Dict[str, Any]]:
    """Возвращает последние запланированные посты (для просмотра статуса)."""
    with get_db() as conn:
        rows = conn.execute(
            """SELECT id, channel_id, status, scheduled_at, sent_at, error_message,
                      substr(content, 1, 60) as content_preview
               FROM scheduled_channel_posts
               ORDER BY scheduled_at DESC
               LIMIT ?""",
            (limit,),
        ).fetchall()
        return [dict(row) for row in rows]
