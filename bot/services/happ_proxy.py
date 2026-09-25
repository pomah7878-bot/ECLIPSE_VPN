"""
Клиент API happ-proxy.com (провайдерский функционал Happ):
  - лимитированные ссылки (add-install/update-install/list-install)
  - привязка домена подписки (add-domain/delete-domain/list-domain)
  - просмотр/удаление HWID по лимитированной ссылке (list-hwid/delete-hwid)
  - массовые push-уведомления (/remote/notification) — требует тариф Enterprise
  - remote-команды (/remote/command) — требует тариф Pro/Enterprise

Официальная документация: happ.su/main/dev-docs (раздел API), домен API —
https://happ-proxy.com. Все вызовы — GET (кроме push/remote — POST), ответ
всегда JSON с полями rc/msg (rc=1 успех, rc=2 "уже существует" — тоже
считаем успехом там, где это применимо, rc=0 ошибка, текст в msg).

provider_code/auth_key — ОТДЕЛЬНАЯ пара учётных данных от Provider ID
(который просто передаётся в заголовке подписки клиентам). Берутся из
личного кабинета happ-proxy.com, раздел API.
"""
import hashlib
import logging
from typing import Optional, Dict, Any, List
from urllib.parse import urlparse

import aiohttp

logger = logging.getLogger(__name__)

API_BASE = "https://api.happ-proxy.com"
_TIMEOUT = aiohttp.ClientTimeout(total=15)


class HappProxyError(Exception):
    """Ошибка ответа happ-proxy.com API (rc=0), сообщение — из поля msg."""

    def __init__(self, msg: str, rc: int = 0):
        self.msg = msg
        self.rc = rc
        super().__init__(msg)


def domain_hash(domain_or_url: str) -> str:
    """SHA-256(домен) в нижнем регистре hex, как того требует API.
    Принимает и просто домен ('sub.example.com'), и полный URL
    ('https://sub.example.com/happ-sub/xyz') — во втором случае домен
    извлекается через urlparse."""
    value = domain_or_url.strip()
    if "://" in value:
        value = urlparse(value).netloc
    value = value.split(":")[0].strip().lower().rstrip("/")
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def is_configured() -> bool:
    """Настроены ли provider_code/auth_key — без них ни один вызов API
    смысла не имеет (сервер ответит Auth error)."""
    from database.requests import get_happ_proxy_provider_code, get_happ_proxy_auth_key
    return bool(get_happ_proxy_provider_code() and get_happ_proxy_auth_key())


def _credentials() -> Dict[str, str]:
    from database.requests import get_happ_proxy_provider_code, get_happ_proxy_auth_key
    provider_code = get_happ_proxy_provider_code()
    auth_key = get_happ_proxy_auth_key()
    if not provider_code or not auth_key:
        raise HappProxyError("provider_code/auth_key не настроены в админке (Happ → happ-proxy.com API)")
    return {"provider_code": provider_code, "auth_key": auth_key}


async def _get(path: str, params: Dict[str, Any]) -> Dict[str, Any]:
    url = f"{API_BASE}{path}"
    clean_params = {k: v for k, v in params.items() if v is not None and v != ""}
    try:
        async with aiohttp.ClientSession(timeout=_TIMEOUT) as session:
            async with session.get(url, params=clean_params) as resp:
                data = await resp.json(content_type=None)
    except Exception as e:
        logger.warning(f"happ-proxy.com GET {path} не удался: {e}")
        raise HappProxyError(f"Сетевая ошибка запроса к happ-proxy.com: {e}")
    return data


async def _post(path: str, params: Dict[str, Any], json_body: Dict[str, Any]) -> Dict[str, Any]:
    url = f"{API_BASE}{path}"
    clean_params = {k: v for k, v in params.items() if v is not None and v != ""}
    try:
        async with aiohttp.ClientSession(timeout=_TIMEOUT) as session:
            async with session.post(url, params=clean_params, json=json_body) as resp:
                data = await resp.json(content_type=None)
    except Exception as e:
        logger.warning(f"happ-proxy.com POST {path} не удался: {e}")
        raise HappProxyError(f"Сетевая ошибка запроса к happ-proxy.com: {e}")
    return data


# ============================================================
# Домены
# ============================================================

async def add_domain(domain_or_url: str, domain_name: Optional[str] = None) -> Dict[str, Any]:
    """Регистрирует домен подписки в happ-proxy.com. rc=1 (создан) и rc=2
    ("уже существует") оба считаем успехом — домен так или иначе привязан."""
    creds = _credentials()
    data = await _get(
        "/api/add-domain",
        {**creds, "domain_hash": domain_hash(domain_or_url), "domain_name": domain_name},
    )
    if data.get("rc") in (1, 2):
        return data
    raise HappProxyError(data.get("msg", "Unknown error"), data.get("rc", 0))


async def ensure_domain_registered(webapp_url: str) -> bool:
    """Регистрирует домен сайта в happ-proxy.com, только если он ещё не
    был зарегистрирован (или сменился с прошлого раза) — чтобы не дёргать
    API на каждый импорт. Возвращает True, если домен зарегистрирован
    (сейчас или ранее), False — если регистрация не удалась."""
    from database.requests import (
        get_happ_proxy_registered_domain_hash,
        set_happ_proxy_registered_domain_hash,
    )
    current_hash = domain_hash(webapp_url)
    if get_happ_proxy_registered_domain_hash() == current_hash:
        return True
    try:
        await add_domain(webapp_url)
    except HappProxyError as e:
        logger.warning(f"happ-proxy.com: не удалось зарегистрировать домен: {e.msg}")
        return False
    set_happ_proxy_registered_domain_hash(current_hash)
    return True


async def delete_domain(domain_id: int) -> None:
    creds = _credentials()
    data = await _get("/api/delete-domain", {**creds, "id": domain_id})
    if data.get("rc") != 1:
        raise HappProxyError(data.get("msg", "Unknown error"), data.get("rc", 0))


async def list_domains() -> List[Dict[str, Any]]:
    creds = _credentials()
    data = await _get("/api/list-domain", creds)
    if data.get("rc") != 1:
        raise HappProxyError(data.get("msg", "Unknown error"), data.get("rc", 0))
    return data.get("data", [])


# ============================================================
# Лимитированные ссылки (install)
# ============================================================

async def add_install(install_limit: int, note: Optional[str] = None) -> Dict[str, Any]:
    """Создаёт новую лимитированную ссылку. Возвращает {"install_code":
    ..., "id": ...}. install_limit: 1..100."""
    creds = _credentials()
    data = await _get(
        "/api/add-install",
        {**creds, "install_limit": max(1, min(100, int(install_limit))), "note": note},
    )
    if data.get("rc") == 1:
        return {"install_code": data["install_code"], "id": data.get("id")}
    raise HappProxyError(data.get("msg", "Unknown error"), data.get("rc", 0))


async def update_install(
    install_id: int,
    install_limit: Optional[int] = None,
    note: Optional[str] = None,
    status: Optional[int] = None,
) -> Dict[str, Any]:
    """status: 5 — Disabled, 10 — Active."""
    creds = _credentials()
    data = await _get(
        "/api/update-install",
        {**creds, "id": install_id, "install_limit": install_limit, "note": note, "status": status},
    )
    if data.get("rc") == 1:
        return data
    raise HappProxyError(data.get("msg", "Unknown error"), data.get("rc", 0))


async def list_installs(install_id: Optional[int] = None) -> List[Dict[str, Any]]:
    creds = _credentials()
    data = await _get("/api/list-install", {**creds, "id": install_id})
    if data.get("rc") != 1:
        raise HappProxyError(data.get("msg", "Unknown error"), data.get("rc", 0))
    return data.get("data", [])


async def get_or_create_install_code_for_sub(sub_id: str) -> Optional[str]:
    """Возвращает install_code для данного sub_id ключа — из кэша (таблица
    happ_install_links), либо создаёт новую лимитированную ссылку через
    API (install_limit берётся из настройки happ_install_limit) и кэширует
    результат. Возвращает None, если API не настроено или запрос не
    удался — вызывающий код в этом случае просто не добавляет InstallID
    к ссылке (поведение как раньше, без лимита установок)."""
    if not is_configured():
        return None

    from database.requests import (
        get_happ_install_link,
        save_happ_install_link,
        get_happ_install_limit,
    )

    cached = get_happ_install_link(sub_id)
    if cached:
        return cached["install_code"]

    limit = get_happ_install_limit()
    try:
        result = await add_install(limit, note=f"eclipse:{sub_id[:40]}")
    except HappProxyError as e:
        logger.warning(f"happ-proxy.com: не удалось создать install-ссылку для sub_id={sub_id[:8]}...: {e.msg}")
        return None

    save_happ_install_link(sub_id, result["install_code"], result.get("id"), limit)
    return result["install_code"]


# ============================================================
# HWID по лимитированной ссылке
# ============================================================

async def list_hwid(install_code: Optional[str] = None, install_id: Optional[int] = None, hwid: Optional[str] = None) -> List[Dict[str, Any]]:
    if not install_code and not install_id:
        raise HappProxyError("Нужен install_code или install_id")
    creds = _credentials()
    data = await _get(
        "/api/list-hwid",
        {**creds, "install_code": install_code, "install_id": install_id, "hwid": hwid},
    )
    if data.get("rc") != 1:
        raise HappProxyError(data.get("msg", "Unknown error"), data.get("rc", 0))
    return data.get("data", [])


async def delete_hwid(hwid: str, install_code: Optional[str] = None, install_id: Optional[int] = None) -> int:
    """Возвращает install_count после удаления."""
    if not install_code and not install_id:
        raise HappProxyError("Нужен install_code или install_id")
    creds = _credentials()
    data = await _get(
        "/api/delete-hwid",
        {**creds, "install_code": install_code, "install_id": install_id, "hwid": hwid},
    )
    if data.get("rc") != 1:
        raise HappProxyError(data.get("msg", "Unknown error"), data.get("rc", 0))
    return data.get("install_count", 0)


# ============================================================
# Push-уведомления (требуется тариф Enterprise happ-proxy.com)
# ============================================================

async def send_push_notification(
    body: str,
    title: Optional[str] = None,
    type_push: str = "optional",
    expire_days: int = 7,
    os_list: Optional[List[str]] = None,
    hwid_list: Optional[List[str]] = None,
) -> Dict[str, Any]:
    """Массовая push-рассылка. Либо os_list (массив 'android'/'ios'), либо
    hwid_list (до 5 HWID через запятую) — одно из двух обязательно."""
    if not os_list and not hwid_list:
        raise HappProxyError("Нужно указать os или hwid")
    if hwid_list and len(hwid_list) > 5:
        raise HappProxyError("Не более 5 HWID за раз")

    creds = _credentials()
    form: Dict[str, Any] = {"body": body, "type_push": type_push, "expire_days": expire_days}
    if title:
        form["title"] = title
    body_payload: Dict[str, Any] = {"PushNotificationForm": form}
    if hwid_list:
        body_payload["hwid"] = ",".join(hwid_list)
    else:
        body_payload["os"] = os_list

    data = await _post("/remote/notification", creds, body_payload)
    if data.get("rc") == 1:
        return data
    raise HappProxyError(data.get("msg", "Unknown error"), data.get("rc", 0))


# ============================================================
# Remote-команды (требуется тариф Pro/Enterprise happ-proxy.com)
# ============================================================

async def send_remote_command(
    action_type: str,
    hwid_list: Optional[List[str]] = None,
    os_list: Optional[List[str]] = None,
    **action_kwargs: Any,
) -> Dict[str, Any]:
    """action_type: 'import-data' | 'update-subscription' | 'set-settings' |
    'sub-change'. Ровно один из hwid_list/os_list обязателен (specific_device
    выбирается автоматически: hwid_list → true, os_list → false).
    action_kwargs — доп. поля для конкретного action_type (import_data=...,
    settings={...}, change_type=...&change_value=... для sub-change)."""
    if not hwid_list and not os_list:
        raise HappProxyError("Нужно указать hwid или os")
    if hwid_list and len(hwid_list) > 5:
        raise HappProxyError("Не более 5 HWID за раз")

    creds = _credentials()
    payload: Dict[str, Any] = {"action_type": action_type, **action_kwargs}
    if hwid_list:
        payload["specific_device_toggle"] = True
        payload["hwid"] = ",".join(hwid_list)
    else:
        payload["specific_device_toggle"] = False
        payload["os"] = os_list

    data = await _post("/remote/command", creds, payload)
    if data.get("rc") == 1:
        return data
    raise HappProxyError(data.get("msg", "Unknown error"), data.get("rc", 0))


async def update_subscription_now(hwid_list: Optional[List[str]] = None, os_list: Optional[List[str]] = None) -> Dict[str, Any]:
    """Удобная обёртка над самой востребованной remote-командой —
    принудительное обновление подписки у клиента(ов)."""
    return await send_remote_command("update-subscription", hwid_list=hwid_list, os_list=os_list)
