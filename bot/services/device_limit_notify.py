"""v1.208: уведомления о лимите устройств.

Когда панель 3x-ui в режиме HWID-лимита отвечает 404/403 на запрос подписки,
это может значить две разные вещи:

  1. клиент на панели есть, но уже подключено максимальное число устройств;
  2. клиента на панели нет (рассинхронизация).

Модуль различает эти случаи (запросом клиента на панели) и:
  - в случае 1 один раз в несколько часов пишет владельцу ключа;
  - сообщает администратору (не чаще раза в сутки на ключ, с общим лимитом
    сообщений в час), чтобы чат не засорялся.

Всё выполняется в фоне и никогда не ломает выдачу подписки: любая ошибка
здесь только логируется.
"""
import asyncio
import logging
import time
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

SETTING_USER = "device_limit_notify_user"
SETTING_ADMIN = "device_limit_notify_admin"

USER_COOLDOWN_SEC = 6 * 3600
ADMIN_COOLDOWN_SEC = 24 * 3600
ADMIN_HOURLY_CAP = 10

_last_user_notice: Dict[int, float] = {}
_last_admin_notice: Dict[int, float] = {}
_admin_sent_times: list = []
_pending_tasks: set = set()


def _enabled(name: str) -> bool:
    from database.requests import get_setting
    return (get_setting(name, "1") or "1") != "0"


def _hwid_mode() -> bool:
    try:
        from database.requests import get_device_limit_type
        return get_device_limit_type() == "hwid"
    except Exception:
        return False


def schedule(key: Dict[str, Any], upstream_status: int) -> None:
    """Запускает проверку в фоне. Безопасно вызывать из обработчика подписки."""
    try:
        if upstream_status not in (403, 404):
            return
        if key.get("panel_removed_at") or not key.get("id"):
            return
        if not _hwid_mode():
            return
        if not (_enabled(SETTING_USER) or _enabled(SETTING_ADMIN)):
            return
        key_id = int(key["id"])
        now = time.time()
        # дешёвая отсечка до сетевых запросов к панели
        user_ready = now - _last_user_notice.get(key_id, 0) >= USER_COOLDOWN_SEC
        admin_ready = now - _last_admin_notice.get(key_id, 0) >= ADMIN_COOLDOWN_SEC
        if not (user_ready or admin_ready):
            return
        task = asyncio.get_running_loop().create_task(_process(dict(key)))
        _pending_tasks.add(task)
        task.add_done_callback(_pending_tasks.discard)
    except Exception as e:
        logger.debug(f"device_limit_notify.schedule: {e}")


async def _client_exists_on_panel(key: Dict[str, Any]) -> Optional[bool]:
    """True/False — клиент найден/не найден; None — проверить не удалось."""
    email = key.get("panel_email")
    server_id = key.get("server_id")
    if not email or not server_id:
        return None
    try:
        from bot.services.vpn_api import get_client
        client = await get_client(int(server_id))
        try:
            stats = await client.get_client_stats(email, resolve_inbound=False)
        except TypeError:
            stats = await client.get_client_stats(email)
        return bool(stats)
    except Exception as e:
        logger.debug(f"device_limit_notify: не удалось проверить клиента {email}: {e}")
        return None


async def _process(key: Dict[str, Any]) -> None:
    try:
        key_id = int(key["id"])
        exists = await _client_exists_on_panel(key)
        if exists is None:
            reason = "unknown"
        elif exists:
            reason = "limit"
        else:
            reason = "missing"
        name = key.get("custom_name") or key.get("panel_email") or f"#{key_id}"
        logger.info(
            f"device_limit_notify: ключ {key_id} ({name}): панель отказала в подписке, "
            f"причина: {'лимит устройств' if reason == 'limit' else 'клиент не найден на панели' if reason == 'missing' else 'не определена'}"
        )

        from bot.utils.runtime_state import get_bot_instance
        bot = get_bot_instance()
        if not bot:
            return
        now = time.time()

        if (
            reason == "limit"
            and _enabled(SETTING_USER)
            and now - _last_user_notice.get(key_id, 0) >= USER_COOLDOWN_SEC
            and key.get("telegram_id")
            and not key.get("is_banned")
        ):
            _last_user_notice[key_id] = now
            text = (
                "📱 <b>Достигнут лимит устройств</b>\n\n"
                f"К ключу «{_esc(name)}» уже подключено максимальное число устройств, "
                "поэтому новое устройство не смогло получить подписку.\n\n"
                "Что можно сделать:\n"
                "• использовать ключ на уже подключённых устройствах;\n"
                "• если подключали устройство давно и оно больше не нужно — напишите в поддержку, "
                "мы освободим место;\n"
                "• при необходимости оформите ключ с большим числом устройств."
            )
            try:
                await bot.send_message(int(key["telegram_id"]), text, parse_mode="HTML")
            except Exception as e:
                logger.debug(f"device_limit_notify: пользователю не отправлено: {e}")

        if (
            reason in ("limit", "missing")
            and _enabled(SETTING_ADMIN)
            and now - _last_admin_notice.get(key_id, 0) >= ADMIN_COOLDOWN_SEC
        ):
            _admin_sent_times[:] = [t for t in _admin_sent_times if now - t < 3600]
            if len(_admin_sent_times) >= ADMIN_HOURLY_CAP:
                return
            _last_admin_notice[key_id] = now
            _admin_sent_times.append(now)
            who = key.get("username")
            owner = f"@{_esc(who)}" if who else f"tg {key.get('telegram_id') or '—'}"
            if reason == "limit":
                head = "📱 <b>Лимит устройств</b>"
                body = "Панель отказала в подписке: на ключе исчерпан лимит устройств."
            else:
                head = "⚠️ <b>Ключ не найден на панели</b>"
                body = "Панель не нашла клиента этого ключа. Похоже на рассинхронизацию."
            text = (
                f"{head}\n\n"
                f"Ключ: #{key_id} ({_esc(name)})\n"
                f"Владелец: {owner}\n"
                f"{body}\n\n"
                "Отключить такие уведомления: /devlimit"
            )
            from config import ADMIN_IDS
            for admin_id in ADMIN_IDS:
                try:
                    await bot.send_message(admin_id, text, parse_mode="HTML")
                except Exception:
                    pass
    except Exception as e:
        logger.warning(f"device_limit_notify: ошибка обработки: {e}")


def _esc(value: Any) -> str:
    import html
    return html.escape(str(value), quote=False)
