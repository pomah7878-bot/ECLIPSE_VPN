"""
Просмотр и точечное отключение подключённых устройств ключа.

Дополняет уже существующий "массовый сброс" лимита устройств
(bot/services/... вызывается из ai_support_main.py: clear_device_ips) —
там сбрасывается СРАЗУ ВЕСЬ список (все IP или все HWID разом). Этот
модуль даёт то же самое, что видит панель 3x-ui, но по каждому
устройству отдельно: список с типом/моделью (в режиме HWID) или списком
IP (в режиме IP, без типа устройства — панель их не хранит), плюс
отключение ОДНОГО конкретного устройства (доступно только в режиме HWID
— в режиме IP панель не поддерживает удаление одного IP, только сброс
всех разом).

Используется из трёх мест одинаково: бот (карточка ключа), веб (личный
кабинет на сайте) и AI-помощник (ai_support_main.py) — единая точка
правды, чтобы поведение не разъезжалось.
"""
import logging
import time
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

# Устройство считается "подключено сейчас", если панель видела его (IP)
# за последние 10 минут — используется только для отображения статуса в
# режиме IP (в режиме HWID last_seen панели надёжнее и показывается как есть).
_RECENT_WINDOW_SECONDS = 600


class DeviceManagementError(Exception):
    """Ошибка при обращении к панели за списком/отключением устройств."""


async def _get_client_for_key(key: Dict[str, Any]):
    from database.requests import get_server_by_id
    from bot.services.vpn_api import get_client_from_server_data

    server_id = key.get("server_id")
    if not server_id:
        raise DeviceManagementError("У ключа не настроен сервер.")
    server_data = get_server_by_id(server_id)
    if not server_data:
        raise DeviceManagementError("Сервер ключа не найден.")
    return get_client_from_server_data(server_data)


async def get_devices_for_key(key: Dict[str, Any]) -> Dict[str, Any]:
    """Возвращает текущий список подключённых устройств ключа и лимит.

    Returns:
        {
            "limit_type": "hwid" | "ip",
            "max_devices": int | None,
            "current_count": int,
            "devices": [
                {
                    "device_id": int | None,   # только в режиме hwid — нужен для отключения
                    "ip": str | None,          # только в режиме ip
                    "os": str | None,
                    "os_version": str | None,
                    "model": str | None,
                    "user_agent": str | None,
                    "first_seen": int | None,  # unix-секунды
                    "last_seen": int | None,   # unix-секунды
                },
                ...
            ],
        }

    Бросает DeviceManagementError с человекочитаемым текстом при сбое.
    """
    from database.requests import get_device_limit_type
    import asyncio

    panel_email = key.get("panel_email")
    if not panel_email:
        raise DeviceManagementError("У ключа ещё не настроен клиент на панели.")

    client = await _get_client_for_key(key)
    limit_type = get_device_limit_type()
    max_devices = key.get("max_ips")

    if limit_type == "hwid":
        try:
            result = await asyncio.wait_for(
                client._request("POST", f"/panel/api/clients/hwids/{panel_email}"),
                timeout=8.0,
            )
        except asyncio.TimeoutError:
            raise DeviceManagementError("Сервер не ответил вовремя, попробуйте ещё раз.")
        except Exception as e:
            logger.warning(f"Ошибка получения списка HWID для {panel_email}: {e}")
            raise DeviceManagementError("Не удалось получить список устройств с панели.")

        if not isinstance(result, dict) or not result.get("success"):
            raise DeviceManagementError(
                "Панель не поддерживает список привязанных устройств (нужна более новая версия 3x-ui)."
            )

        raw_devices = result.get("obj") or []
        devices = []
        for d in raw_devices:
            if not isinstance(d, dict):
                continue
            devices.append({
                "device_id": d.get("id"),
                "ip": None,
                "os": d.get("deviceOs"),
                "os_version": d.get("osVersion"),
                "model": d.get("deviceModel"),
                "user_agent": d.get("userAgent"),
                "first_seen": _ms_to_s(d.get("firstSeen")),
                "last_seen": _ms_to_s(d.get("lastSeen")),
            })
        # Стабильный порядок (по id) — нужен, чтобы "устройство №N" из
        # текстового списка не "плавало" между повторными запросами.
        devices.sort(key=lambda x: (x["device_id"] is None, x["device_id"]))
        return {
            "limit_type": "hwid",
            "max_devices": max_devices,
            "current_count": len(devices),
            "devices": devices,
        }

    # Режим 'ip' — панель хранит только адреса с таймстампом, без типа
    # устройства. Собираем со всех нод мастер-панели (см. check_active_devices
    # в ai_support_main.py — тот же принцип).
    try:
        result = await asyncio.wait_for(
            client._request("POST", "/panel/api/clients/clientIpsByGuid"),
            timeout=8.0,
        )
    except asyncio.TimeoutError:
        raise DeviceManagementError("Сервер не ответил вовремя, попробуйте ещё раз.")
    except Exception as e:
        logger.warning(f"Ошибка получения списка IP для {panel_email}: {e}")
        raise DeviceManagementError("Не удалось получить список устройств с панели.")

    by_guid = (result.get("obj") or {}) if isinstance(result, dict) else {}
    now = time.time()
    seen: Dict[str, int] = {}
    for node_entries in by_guid.values():
        for ip_entry in node_entries.get(panel_email, []):
            ip = ip_entry.get("ip")
            ts = int(ip_entry.get("timestamp") or 0)
            if not ip:
                continue
            if ip not in seen or ts > seen[ip]:
                seen[ip] = ts

    devices = [
        {
            "device_id": None,
            "ip": ip,
            "os": None,
            "os_version": None,
            "model": None,
            "user_agent": None,
            "first_seen": None,
            "last_seen": ts,
        }
        for ip, ts in sorted(seen.items(), key=lambda kv: -kv[1])
        if now - ts <= _RECENT_WINDOW_SECONDS
    ]
    return {
        "limit_type": "ip",
        "max_devices": max_devices,
        "current_count": len(devices),
        "devices": devices,
    }


async def disconnect_device(key: Dict[str, Any], device_id: int) -> Dict[str, Any]:
    """Отключает ОДНО устройство по его device_id (из get_devices_for_key,
    только режим 'hwid' — режим 'ip' такого не поддерживает на уровне
    панели, там доступен только полный сброс списка).

    Returns: {"ok": bool, "message": str}
    """
    from database.requests import get_device_limit_type
    import asyncio

    panel_email = key.get("panel_email")
    if not panel_email:
        return {"ok": False, "message": "У ключа ещё не настроен клиент на панели."}

    limit_type = get_device_limit_type()
    if limit_type != "hwid":
        return {
            "ok": False,
            "message": (
                "В текущем режиме ограничения (по IP) панель не поддерживает отключение "
                "одного устройства — доступен только сброс всего списка сразу."
            ),
        }

    try:
        client = await _get_client_for_key(key)
        result = await asyncio.wait_for(
            client._request("DELETE", f"/panel/api/clients/hwids/{panel_email}/{device_id}"),
            timeout=8.0,
        )
    except DeviceManagementError as e:
        return {"ok": False, "message": str(e)}
    except asyncio.TimeoutError:
        return {"ok": False, "message": "Сервер не ответил вовремя, попробуйте ещё раз."}
    except Exception as e:
        logger.warning(f"Ошибка отключения устройства {device_id} для {panel_email}: {e}")
        return {"ok": False, "message": "Не удалось отключить устройство из-за технической ошибки."}

    if not isinstance(result, dict) or not result.get("success"):
        return {"ok": False, "message": "Панель не подтвердила отключение устройства."}

    logger.info(f"Отключено устройство {device_id} ключа с panel_email={panel_email}")
    return {"ok": True, "message": "Устройство отключено."}


def _ms_to_s(value: Optional[int]) -> Optional[int]:
    """3x-ui отдаёт firstSeen/lastSeen в миллисекундах — приводим к секундам,
    как везде в остальном коде (time.time())."""
    if not value:
        return None
    try:
        value = int(value)
    except (TypeError, ValueError):
        return None
    return value // 1000 if value > 10_000_000_000 else value


def format_device_line(device: Dict[str, Any], index: int) -> str:
    """Человекочитаемая строка одного устройства для бота/AI (без HTML-разметки,
    вызывающий код сам решает, экранировать ли и как оформлять)."""
    if device.get("model") or device.get("os"):
        os_part = device.get("os") or "—"
        if device.get("os_version"):
            os_part += f" {device['os_version']}"
        model_part = device.get("model") or "неизвестная модель"
        line = f"{index}. {model_part} ({os_part})"
    elif device.get("ip"):
        line = f"{index}. IP {device['ip']}"
    else:
        line = f"{index}. Устройство"

    last_seen = device.get("last_seen")
    if last_seen:
        try:
            from datetime import datetime, timezone
            from bot.utils.datetime_format import format_datetime_for_display
            dt = datetime.fromtimestamp(last_seen, tz=timezone.utc)
            line += f" — последняя активность {format_datetime_for_display(dt)}"
        except Exception:
            pass
    return line
