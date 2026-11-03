"""Проверка сертификата панели 3x-ui (v1.450, по умолчанию с v1.518).

Панель с сертификатом от настоящего центра (домен + Let's Encrypt) проверяется
обычным образом — продление сертификата ничего не ломает. Панели по IP с
самоподписанным сертификатом обычная проверка не пройдут, поэтому их отпечаток
запоминается при первом подключении и дальше принимается только он.

Выключается настройкой panel_tls_pin=0 (тогда ssl=False, как до v1.450).
Если сертификат самоподписанной панели сменили законно, сбросьте запомненный отпечаток:
  DELETE FROM settings WHERE key = 'panel_tls_pin_<id сервера>';
"""
import asyncio
import hashlib
import logging
import socket
import ssl
from typing import Optional, Union

import aiohttp

logger = logging.getLogger(__name__)

_CA = "ca"  # вместо отпечатка: панель прошла обычную проверку, дальше проверяем только так


def _probe_sync(host: str, port: int) -> str:
    """'ca', если сертификат проходит обычную проверку, иначе sha256-отпечаток сертификата."""
    try:
        with socket.create_connection((host, port), timeout=8) as sock:
            with ssl.create_default_context().wrap_socket(sock, server_hostname=host):
                return _CA
    except ssl.SSLCertVerificationError:
        pass
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    with socket.create_connection((host, port), timeout=8) as sock:
        with ctx.wrap_socket(sock, server_hostname=host) as tls:
            der = tls.getpeercert(binary_form=True)
    return hashlib.sha256(der).hexdigest()


async def panel_ssl(server_id: Optional[int], host: str, port: int, protocol: str = "https") -> Union[bool, aiohttp.Fingerprint]:
    """Значение для параметра ssl= в aiohttp: True (обычная проверка), Fingerprint или False (выключено)."""
    from database.db_settings import get_setting, set_setting
    if protocol != "https" or server_id is None or (get_setting("panel_tls_pin", "1") or "1") != "1":
        return False
    key = f"panel_tls_pin_{server_id}"
    pin = (get_setting(key, "") or "").strip().lower()
    if not pin:
        try:
            pin = await asyncio.get_running_loop().run_in_executor(None, _probe_sync, host, int(port))
        except Exception as e:  # noqa: BLE001
            # не отправляем логин панели без проверки: строгий режим, при самоподписанном — ошибка до следующей попытки
            logger.warning("panel_tls: не удалось получить сертификат сервера %s: %s", server_id, e)
            return True
        set_setting(key, pin)
        logger.info("panel_tls: сервер %s — %s", server_id,
                    "сертификат центра, обычная проверка" if pin == _CA else "запомнен отпечаток сертификата")
    return True if pin == _CA else aiohttp.Fingerprint(bytes.fromhex(pin))
