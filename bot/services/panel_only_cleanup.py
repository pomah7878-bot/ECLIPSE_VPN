"""
Удаление неактивных ключей именно с VPN-панели — БЕЗ удаления самого
ключа из бота.

Отличие от expired_key_autodelete.py: та фича удаляет ключ полностью
(из бота и с панели) через N дней (по умолчанию 30) и уведомляет
клиента. Эта фича — более ранняя, узкая: убирает клиента ТОЛЬКО с
панели (освобождает место в самой 3x-ui) через N дней после истечения
(по умолчанию 0 — сразу), но ключ в боте остаётся полностью рабочим —
клиент по-прежнему может продлить его в течение обычного срока
(expired_key_autodelete_days), и после продления восстановится та же
самая ссылка подписки, ничего менять клиенту не нужно.

Важно: обычная фоновая синхронизация (materialize_subscription_state)
и так уже НЕ трогает истёкшие ключи (get_all_active_keys_with_server
явно исключает expires_at в прошлом) — значит эта очистка не будет
конфликтовать с обычной синхронизацией и ключ не пересоздастся сам
собой на панели раньше времени.
"""
import logging
from typing import Any, Dict

logger = logging.getLogger(__name__)

SETTING_DAYS = 'panel_cleanup_delay_days'
DEFAULT_DAYS = 0


def get_panel_cleanup_delay_days() -> int:
    """Через сколько дней после истечения подписки клиент удаляется с
    VPN-панели (сам ключ в боте остаётся). По умолчанию 0 — сразу же
    после истечения."""
    from database.requests import get_setting
    value = get_setting(SETTING_DAYS)
    try:
        return max(0, int(value)) if value not in (None, '') else DEFAULT_DAYS
    except (TypeError, ValueError):
        return DEFAULT_DAYS


def set_panel_cleanup_delay_days(days: int) -> None:
    """Задаёт задержку (в днях) перед удалением клиента с панели."""
    from database.requests import set_setting
    set_setting(SETTING_DAYS, str(max(0, int(days))))


async def process_panel_only_cleanup() -> Dict[str, int]:
    """
    Находит ключи, истёкшие более N дней назад (N — настройка выше),
    у которых клиент ещё не был убран с панели, и удаляет их ТОЛЬКО с
    панели — сам ключ (и возможность его продлить) в боте не трогается.

    Returns:
        dict со статистикой: cleaned_count, errors_count
    """
    days = get_panel_cleanup_delay_days()

    from database.connection import get_db
    with get_db() as conn:
        rows = conn.execute(
            """
            SELECT vk.id
            FROM vpn_keys vk
            WHERE vk.expires_at IS NOT NULL
              AND datetime(vk.expires_at) < datetime('now', ? || ' days')
              AND vk.panel_removed_at IS NULL
            """,
            (f"-{days}",),
        ).fetchall()
    pending_ids = [int(r['id']) for r in rows]

    if not pending_ids:
        return {'cleaned_count': 0, 'errors_count': 0}

    from database.requests import get_vpn_key_by_id, mark_key_panel_removed
    from bot.handlers.admin.users_keys_deleted import _delete_key_from_panel

    cleaned_count = 0
    errors_count = 0

    for key_id in pending_ids:
        try:
            key = get_vpn_key_by_id(key_id)
            if not key:
                continue
            removed = await _delete_key_from_panel(key)
            # Помечаем как обработанный в любом случае (даже если клиента
            # на панели уже не было) — иначе задача будет бесконечно
            # пытаться удалить один и тот же ключ каждый цикл.
            mark_key_panel_removed(key_id)
            if removed:
                cleaned_count += 1
                logger.info(f"Панель-очистка: ключ #{key_id} убран с панели (после {days}д неактивности)")
        except Exception as e:
            errors_count += 1
            logger.error(f"Панель-очистка: ошибка удаления ключа {key_id} с панели: {e}")

    if cleaned_count or errors_count:
        logger.info(f"🧹 Панель-очистка неактивных ключей: {{'cleaned': {cleaned_count}, 'errors': {errors_count}}}")

    return {'cleaned_count': cleaned_count, 'errors_count': errors_count}
