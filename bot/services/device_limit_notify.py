"""Уведомления о лимите устройств (v1.208, v1.209).

Режим HWID (v1.208). Когда панель 3x-ui в режиме HWID-лимита отвечает 404/403 на
запрос подписки, это может значить две разные вещи:

  1. клиент на панели есть, но уже подключено максимальное число устройств;
  2. клиента на панели нет (рассинхронизация).

Модуль различает эти случаи запросом клиента на панели.

Режим IP (v1.209). Панель при превышении лимита IP подписку не блокирует, поэтому
отказа в ответе нет. Фоновая проверка раз в 10 минут читает у панели список IP
клиентов и сравнивает число недавних уникальных адресов с лимитом тарифа
(max_ips). Уведомление отправляется, только если превышение держится две проверки
подряд — иначе переключения Wi-Fi/LTE на одном телефоне давали бы ложные срабатывания.

Во всех режимах:
  - пользователь получает сообщение не чаще раза в 6 часов на ключ (кнопка ведёт на
    экран устройств ключа);
  - админ получает сообщение не чаще раза в сутки на ключ и не более 10 в час.

Всё выполняется в фоне и никогда не ломает выдачу подписки: любая ошибка здесь только
логируется.
"""
import asyncio
import html
import logging
import time
from typing import Any, Dict, Optional, Tuple

logger = logging.getLogger(__name__)

SETTING_USER = "device_limit_notify_user"
SETTING_ADMIN = "device_limit_notify_admin"

USER_COOLDOWN_SEC = 6 * 3600
ADMIN_COOLDOWN_SEC = 24 * 3600
ADMIN_HOURLY_CAP = 10

IP_CHECK_INTERVAL_SEC = 600
IP_WINDOW_SEC = 900
IP_STREAK_NEEDED = 2

_last_user_notice: Dict[int, float] = {}
_last_admin_notice: Dict[int, float] = {}
_admin_sent_times: list = []
_pending_tasks: set = set()
_ip_streak: Dict[int, int] = {}


def _esc(value: Any) -> str:
    return html.escape(str(value), quote=False)


def _enabled(name: str) -> bool:
    from database.requests import get_setting
    return (get_setting(name, "1") or "1") != "0"


def _limit_type() -> str:
    try:
        from database.requests import get_device_limit_type
        return get_device_limit_type()
    except Exception:
        return ""


# ---------------------------------------------------------------- режим HWID

def schedule(key: Dict[str, Any], upstream_status: int) -> None:
    """Запускает проверку в фоне. Безопасно вызывать из обработчика подписки."""
    try:
        if upstream_status not in (403, 404):
            return
        if key.get("panel_removed_at") or not key.get("id"):
            return
        if _limit_type() != "hwid":
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


def _key_name(key: Dict[str, Any]) -> str:
    return key.get("custom_name") or key.get("panel_email") or f"#{key.get('id')}"


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
        logger.info(
            f"device_limit_notify: ключ {key_id} ({_key_name(key)}): панель отказала в подписке, "
            f"причина: {'лимит устройств' if reason == 'limit' else 'клиент не найден на панели' if reason == 'missing' else 'не определена'}"
        )
        from bot.utils.runtime_state import get_bot_instance
        bot = get_bot_instance()
        if not bot:
            return
        await _notify(bot, key, reason)
    except Exception as e:
        logger.warning(f"device_limit_notify: ошибка обработки: {e}")


# ------------------------------------------------------------------ рассылка

def _user_text(key: Dict[str, Any], counts: Optional[Tuple[int, int]]) -> str:
    name = _esc(_key_name(key))
    if counts:
        used, limit = counts
        head = (
            f"С ключом «{name}» одновременно выходят в сеть больше устройств (IP-адресов), "
            f"чем разрешено: {used} из {limit}.\n\n"
            "Учтите: один телефон может занимать несколько адресов при переключении "
            "между Wi-Fi и мобильной сетью."
        )
    else:
        head = (
            f"К ключу «{name}» уже подключено максимальное число устройств, поэтому новое "
            "устройство не смогло получить подписку."
        )
    return (
        "📱 <b>Достигнут лимит устройств</b>\n\n"
        f"{head}\n\n"
        "Что можно сделать:\n"
        "• открыть список устройств (кнопка ниже) и убрать ненужное;\n"
        "• использовать ключ только на нужных устройствах;\n"
        "• при необходимости оформить ключ с большим числом устройств."
    )


def _user_markup(key_id: int):
    from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="📱 Устройства ключа", callback_data=f"key_devices:{key_id}")
    ]])


async def _notify(bot, key: Dict[str, Any], reason: str, counts: Optional[Tuple[int, int]] = None) -> None:
    """reason: 'limit' | 'missing' | 'unknown'. counts: (занято, лимит) для режима IP."""
    key_id = int(key["id"])
    now = time.time()

    if (
        reason == "limit"
        and _enabled(SETTING_USER)
        and now - _last_user_notice.get(key_id, 0) >= USER_COOLDOWN_SEC
        and key.get("telegram_id")
        and not key.get("is_banned")
    ):
        _last_user_notice[key_id] = now
        try:
            await bot.send_message(
                int(key["telegram_id"]),
                _user_text(key, counts),
                parse_mode="HTML",
                reply_markup=_user_markup(key_id),
            )
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
        if reason == "limit" and counts:
            head = "📱 <b>Лимит устройств (IP)</b>"
            body = f"Одновременно активных IP-адресов: {counts[0]} при лимите {counts[1]}."
        elif reason == "limit":
            head = "📱 <b>Лимит устройств</b>"
            body = "Панель отказала в подписке: на ключе исчерпан лимит устройств."
        else:
            head = "⚠️ <b>Ключ не найден на панели</b>"
            body = "Панель не нашла клиента этого ключа. Похоже на рассинхронизацию."
        text = (
            f"{head}\n\n"
            f"Ключ: #{key_id} ({_esc(_key_name(key))})\n"
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


# ------------------------------------------------------------------ режим IP

def collect_ip_timestamps(by_guid: Any) -> Dict[str, Dict[str, int]]:
    """Ответ панели {guid: {email: [{"ip", "timestamp"}, ...]}} → {email: {ip: последний timestamp}}.
    Устойчив к неожиданным формам данных: всё лишнее пропускается."""
    out: Dict[str, Dict[str, int]] = {}
    if not isinstance(by_guid, dict):
        return out
    for node_entries in by_guid.values():
        if not isinstance(node_entries, dict):
            continue
        for email, entries in node_entries.items():
            if not isinstance(entries, list):
                continue
            for entry in entries:
                if not isinstance(entry, dict):
                    continue
                ip = entry.get("ip")
                try:
                    ts = int(entry.get("timestamp") or 0)
                except (TypeError, ValueError):
                    continue
                if not ip or ts <= 0:
                    continue
                cur = out.setdefault(email, {})
                if ts > cur.get(ip, 0):
                    cur[ip] = ts
    return out


def count_recent_ips(ip_ts: Dict[str, int], now: float, window: int = IP_WINDOW_SEC) -> int:
    return sum(1 for ts in ip_ts.values() if now - ts <= window)


def _load_ip_keys() -> list:
    from database.connection import get_db
    with get_db() as conn:
        rows = conn.execute("""
            SELECT vk.id, vk.panel_email, vk.custom_name, vk.server_id, vk.tariff_id,
                   t.max_ips, u.telegram_id, u.username, u.is_banned
            FROM vpn_keys vk
            JOIN servers s ON vk.server_id = s.id
            JOIN users u ON vk.user_id = u.id
            LEFT JOIN tariffs t ON vk.tariff_id = t.id
            WHERE (vk.expires_at > datetime('now') OR vk.expires_at IS NULL)
              AND vk.panel_email IS NOT NULL
              AND s.is_active = 1
        """).fetchall()
        return [dict(r) for r in rows]


async def _fetch_server_ips(server_id: int) -> Optional[Dict[str, Dict[str, int]]]:
    from database.requests import get_server_by_id
    from bot.services.vpn_api import get_client_from_server_data
    server = get_server_by_id(server_id)
    if not server:
        return None
    client = get_client_from_server_data(server)
    result = await asyncio.wait_for(
        client._request("POST", "/panel/api/clients/clientIpsByGuid"),
        timeout=15.0,
    )
    by_guid = (result.get("obj") or {}) if isinstance(result, dict) else {}
    return collect_ip_timestamps(by_guid)


async def check_ip_limits_once(bot) -> int:
    """Одна проверка всех ключей. Возвращает число ключей, по которым отправлены уведомления."""
    keys = await asyncio.to_thread(_load_ip_keys)
    by_server: Dict[int, list] = {}
    for key in keys:
        if key.get("server_id") and (key.get("max_ips") or 0) > 0:
            by_server.setdefault(int(key["server_id"]), []).append(key)

    notified = 0
    now = time.time()
    for server_id, server_keys in by_server.items():
        try:
            ips = await _fetch_server_ips(server_id)
        except Exception as e:
            logger.warning(f"device_limit_notify: не удалось получить IP клиентов сервера {server_id}: {e}")
            continue
        if ips is None:
            continue
        for key in server_keys:
            key_id = int(key["id"])
            used = count_recent_ips(ips.get(key["panel_email"], {}), now)
            limit = int(key["max_ips"])
            if used > limit:
                _ip_streak[key_id] = _ip_streak.get(key_id, 0) + 1
            else:
                _ip_streak.pop(key_id, None)
                continue
            if _ip_streak[key_id] >= IP_STREAK_NEEDED:
                before = (_last_user_notice.get(key_id), _last_admin_notice.get(key_id))
                await _notify(bot, key, "limit", counts=(used, limit))
                if before != (_last_user_notice.get(key_id), _last_admin_notice.get(key_id)):
                    notified += 1
                    logger.info(
                        f"device_limit_notify: ключ {key_id} ({_key_name(key)}): активных IP {used} при лимите {limit}"
                    )
    return notified


async def run_ip_limit_scheduler(bot) -> None:
    """Фоновый цикл. В режиме HWID или при выключенных уведомлениях только ждёт."""
    logger.info("📱 Проверка лимита IP запущена (каждые 10 минут, работает только в режиме «по IP»)")
    await asyncio.sleep(120)
    while True:
        try:
            if _limit_type() == "ip" and (_enabled(SETTING_USER) or _enabled(SETTING_ADMIN)):
                await check_ip_limits_once(bot)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.error(f"device_limit_notify: ошибка проверки лимита IP: {e}", exc_info=True)
        await asyncio.sleep(IP_CHECK_INTERVAL_SEC)
