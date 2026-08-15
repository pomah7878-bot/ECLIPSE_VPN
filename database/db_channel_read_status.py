"""
Учёт "прочитанности" постов маркетингового канала на главном экране бота —
похоже на счётчик непрочитанных сообщений в чатах. Поскольку кнопка ведёт
по внешней ссылке на канал (Telegram не уведомляет бота о клике по
url-кнопке), пост считается "увиденным" в момент показа счётчика в
главном меню, а не в момент реального перехода в канал.
"""
import logging
from typing import Optional
from .connection import get_db

logger = logging.getLogger(__name__)

__all__ = [
    'get_last_seen_channel_post_id',
    'mark_channel_posts_seen',
    'count_unread_channel_posts',
    'get_max_sent_post_id',
]


def get_last_seen_channel_post_id(telegram_id: int) -> int:
    """Возвращает id последнего поста, который пользователь уже видел (0, если ни разу не заходил)."""
    with get_db() as conn:
        row = conn.execute(
            "SELECT last_seen_post_id FROM user_channel_read_status WHERE telegram_id = ?",
            (telegram_id,),
        ).fetchone()
        return row['last_seen_post_id'] if row else 0


def mark_channel_posts_seen(telegram_id: int, up_to_post_id: int) -> None:
    """Отмечает все посты вплоть до up_to_post_id как увиденные этим пользователем."""
    with get_db() as conn:
        conn.execute(
            """INSERT INTO user_channel_read_status (telegram_id, last_seen_post_id, updated_at)
               VALUES (?, ?, datetime('now'))
               ON CONFLICT(telegram_id) DO UPDATE SET
                   last_seen_post_id = MAX(last_seen_post_id, excluded.last_seen_post_id),
                   updated_at = datetime('now')""",
            (telegram_id, up_to_post_id),
        )


def count_unread_channel_posts(telegram_id: int) -> int:
    """Считает, сколько уже опубликованных постов пользователь ещё не видел."""
    last_seen = get_last_seen_channel_post_id(telegram_id)
    with get_db() as conn:
        row = conn.execute(
            """SELECT COUNT(*) as cnt, COALESCE(MAX(id), 0) as max_id
               FROM scheduled_channel_posts
               WHERE status = 'sent' AND id > ?""",
            (last_seen,),
        ).fetchone()
        return row['cnt'] if row else 0


def get_max_sent_post_id() -> int:
    """ID последнего реально ОТПРАВЛЕННОГО поста (status='sent'). Используется
    для пометки "увиденным" вместо get_all_scheduled_posts(limit=1), который
    сортирует по scheduled_at и может вернуть ещё не отправленный (pending)
    пост из будущего — это отметило бы прочитанным то, чего пользователь не
    видел, и одновременно "проглатывало" реально отправленные посты с id
    больше этого pending-поста, из-за чего счётчик никогда не обнулялся."""
    with get_db() as conn:
        row = conn.execute(
            "SELECT COALESCE(MAX(id), 0) as max_id FROM scheduled_channel_posts WHERE status = 'sent'"
        ).fetchone()
        return row['max_id'] if row else 0
