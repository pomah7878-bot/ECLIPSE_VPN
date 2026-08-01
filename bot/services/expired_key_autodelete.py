"""
Автоматическое удаление истекших ключей из БД бота с уведомлением клиента.

Логика:
- Раз в сутки (в составе ежедневных задач планировщика) ищем ключи,
  срок действия которых истёк более N дней назад (по умолчанию 30).
- Для каждого такого ключа отправляем клиенту напоминание (текст и
  кнопка настраиваются через встроенный редактор сообщений в админке),
  затем удаляем ключ из БД и с VPN-панели.
- Включение/выключение и число дней настраиваются через settings.
"""
import logging
from typing import Any, Dict

logger = logging.getLogger(__name__)

SETTING_ENABLED = 'expired_key_autodelete_enabled'
SETTING_DAYS = 'expired_key_autodelete_days'
MESSAGE_KEY = 'expired_key_deletion_notice'

DEFAULT_DAYS = 30
DEFAULT_NOTICE_TEXT = (
    "\U0001F440 <b>Давно вас не было!</b>\n\n"
    "Ваш VPN-ключ истёк более {days} дней назад, и мы удалили его из системы, "
    "чтобы не занимать место.\n\n"
    "Соскучились по стабильному и быстрому VPN? Оформите новую подписку в любой момент — "
    "это займёт меньше минуты."
)


def is_autodelete_enabled() -> bool:
    from database.requests import get_setting
    value = get_setting(SETTING_ENABLED)
    return value != '0'  # по умолчанию включено


def get_autodelete_days() -> int:
    from database.requests import get_setting
    value = get_setting(SETTING_DAYS)
    try:
        return int(value) if value else DEFAULT_DAYS
    except (TypeError, ValueError):
        return DEFAULT_DAYS


async def process_expired_key_autodeletion(bot: Any) -> Dict[str, int]:
    """
    Находит ключи, истёкшие более N дней назад, уведомляет владельцев
    и удаляет ключи из БД и с панели.

    Returns:
        dict со статистикой: notified_count, deleted_count, errors_count
    """
    if not is_autodelete_enabled():
        return {'notified_count': 0, 'deleted_count': 0, 'errors_count': 0, 'skipped': True}

    days = get_autodelete_days()

    from database.connection import get_db
    with get_db() as conn:
        rows = conn.execute(
            """
            SELECT vk.id, vk.custom_name, u.telegram_id
            FROM vpn_keys vk
            JOIN users u ON u.id = vk.user_id
            WHERE vk.expires_at IS NOT NULL
              AND datetime(vk.expires_at) < datetime('now', ? || ' days')
            """,
            (f"-{days}",),
        ).fetchall()
    expired_keys = [dict(r) for r in rows]

    if not expired_keys:
        return {'notified_count': 0, 'deleted_count': 0, 'errors_count': 0, 'skipped': False}

    from bot.utils.message_editor import get_message_data
    from database.db_keys import delete_vpn_key, get_vpn_key_by_id
    from bot.utils.delivery import is_bot_blocked_error
    from database.requests import mark_user_bot_blocked
    from aiogram.utils.keyboard import InlineKeyboardBuilder
    from aiogram.types import InlineKeyboardButton

    message_data = get_message_data(MESSAGE_KEY, DEFAULT_NOTICE_TEXT)
    notice_text = message_data.get('text', DEFAULT_NOTICE_TEXT).replace('{days}', str(days))

    notified_count = 0
    deleted_count = 0
    errors_count = 0

    for key_row in expired_keys:
        key_id = key_row['id']
        telegram_id = key_row['telegram_id']

        try:
            builder = InlineKeyboardBuilder()
            builder.row(InlineKeyboardButton(text="\U0001F6D2 Оформить новую подписку", callback_data="buy"))
            await bot.send_message(
                telegram_id,
                notice_text,
                parse_mode='HTML',
                reply_markup=builder.as_markup(),
            )
            notified_count += 1
        except Exception as e:
            if is_bot_blocked_error(e):
                mark_user_bot_blocked(telegram_id)
            else:
                logger.warning(f"Не удалось уведомить пользователя {telegram_id} об удалении ключа {key_id}: {e}")

        try:
            key = get_vpn_key_by_id(key_id)
            if key:
                from bot.handlers.admin.users_keys_deleted import _delete_key_from_panel
                await _delete_key_from_panel(key)
            delete_vpn_key(key_id)
            deleted_count += 1
            logger.info(f"Автоудаление: ключ #{key_id} (истёк {days}+ дней назад) удалён, владелец уведомлён")
        except Exception as e:
            errors_count += 1
            logger.error(f"Автоудаление: ошибка удаления ключа {key_id}: {e}")

    return {
        'notified_count': notified_count,
        'deleted_count': deleted_count,
        'errors_count': errors_count,
        'skipped': False,
    }
