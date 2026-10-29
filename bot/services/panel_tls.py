"""Закрепление сертификата панели 3x-ui (v1.450).

Панели ходят по IP с самоподписанным сертификатом, поэтому обычная проверка
невозможна. Вместо отключения проверки целиком запоминаем отпечаток сертификата
при первом подключении и дальше принимаем только его.

Включается настройкой panel_tls_pin=1 (по умолчанию выключено — поведение прежнее).
Если сертификат панели сменили законно, сбросьте запомненный отпечаток:
  DELETE FROM settings WHERE key = 'panel_tls_pin_<id сервера>';
"""
import asyncio
import hashlib
import logging
import ssl
from typing import Optional, Union

import aiohttp

logger = logging.getLogger(__name__)


def _fetch_pin_sync(host: str, port: int) -> str:
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    import socket
    with socket.create_connection((host, port), timeout=8) as sock:
        with ctx.wrap_socket(sock, server_hostname=host) as tls:
            der = tls.getpeercert(binary_form=True)
    return hashlib.sha256(der).hexdigest()


async def panel_ssl(server_id: Optional[int], host: str, port: int, protocol: str = "https") -> Union[bool, aiohttp.Fingerprint]:
    """Значение для параметра ssl= в aiohttp: False (как раньше) или Fingerprint."""
    from database.db_settings import get_setting, set_setting
    if protocol != "https" or server_id is None or (get_setting("panel_tls_pin", "0") or "0") != "1":
        return False
    key = f"panel_tls_pin_{server_id}"
    pin = (get_setting(key, "") or "").strip().lower()
    if not pin:
        try:
            pin = await asyncio.get_running_loop().run_in_executor(None, _fetch_pin_sync, host, int(port))
        except Exception as e:  # noqa: BLE001
            logger.warning("panel_tls: не удалось получить сертификат сервера %s: %s", server_id, e)
            return False
        set_setting(key, pin)
        logger.info("panel_tls: запомнен отпечаток сертификата сервера %s", server_id)
    return aiohttp.Fingerprint(bytes.fromhex(pin))
