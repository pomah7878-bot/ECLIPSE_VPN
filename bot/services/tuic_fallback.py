"""Страховка для подписки: ссылки TUIC из Clash-подписки панели.

Зачем. Некоторые версии панели 3x-ui (замечено на 3.11.0) в обычной подписке
(список ссылок в base64) вместо TUIC-ссылок отдают пустые строки, хотя в
Clash-подписке и в карточке клиента они есть. Из-за этого TUIC пропадал у
клиентов в приложении и на экране «Подключения».

Что делаем. Если в обычной подписке нет ни одной ссылки tuic://, берём
Clash-подписку той же панели, достаём из неё записи type: tuic и собираем из
них ссылки сами. Если панель отдаёт tuic:// нормально, мы ничего не добавляем
(нет ни лишнего запроса, ни дублей). Любая ошибка здесь тихо игнорируется:
подписка остаётся такой, какой её отдала панель.
"""
from __future__ import annotations

import logging
import time
import urllib.parse
from typing import Any, Dict, List, Optional, Tuple

import aiohttp

logger = logging.getLogger(__name__)

_CACHE_TTL_OK = 300      # секунд: удачный результат (в т.ч. «TUIC у клиента нет»)
_CACHE_TTL_FAIL = 60     # секунд: не дёргаем панель повторно после сбоя
_CACHE_MAX = 2000
_cache: Dict[str, Tuple[float, List[str]]] = {}


def has_tuic(links: List[str]) -> bool:
    return any(link.lower().startswith("tuic://") for link in links)


def tuic_links_from_clash(text: str) -> List[str]:
    """Собирает tuic://-ссылки из Clash/Mihomo YAML (раздел proxies)."""
    try:
        import yaml
        data = yaml.safe_load(text)
    except Exception:  # нет PyYAML или это не YAML
        return []
    proxies = data.get("proxies") if isinstance(data, dict) else None
    if not isinstance(proxies, list):
        return []

    links: List[str] = []
    for proxy in proxies:
        if not isinstance(proxy, dict) or str(proxy.get("type", "")).lower() != "tuic":
            continue
        uuid, password = proxy.get("uuid"), proxy.get("password")
        server, port = proxy.get("server"), proxy.get("port")
        if not (uuid and password and server and isinstance(port, int) and not isinstance(port, bool)):
            continue
        alpn = proxy.get("alpn")
        if isinstance(alpn, list):
            alpn = ",".join(str(a) for a in alpn)
        query = {
            "congestion_control": proxy.get("congestion-controller") or "bbr",
            "udp_relay_mode": proxy.get("udp-relay-mode") or "native",
            "alpn": alpn or "h3",
            "sni": proxy.get("sni") or server,  # у части записей sni нет: сертификат выдан на сам домен
            "allow_insecure": "1" if proxy.get("skip-cert-verify") else "0",
        }
        name = urllib.parse.quote(str(proxy.get("name") or server), safe="")
        links.append(
            f"tuic://{urllib.parse.quote(str(uuid), safe='')}:{urllib.parse.quote(str(password), safe='')}"
            f"@{server}:{port}?{urllib.parse.urlencode(query)}#{name}"
        )
    return links


async def _clash_url(client, sub_id: str) -> Optional[str]:
    settings = await client.get_panel_settings()
    if not settings or not settings.get("subClashEnable"):
        return None
    prefix = (settings.get("subClashURI") or "").strip()
    if prefix:
        return prefix.rstrip("/") + "/" + sub_id
    # адрес не задан целиком: собираем из частей так же, как для обычной подписки
    domain = (settings.get("subDomain") or "").strip() or urllib.parse.urlparse(client.base_url).hostname
    path = (settings.get("subClashPath") or "").strip().strip("/")
    if not domain or not path:
        return None
    tls = bool((settings.get("subCertFile") or "").strip() and (settings.get("subKeyFile") or "").strip())
    try:
        port = int(settings.get("subPort") or 0)
    except (TypeError, ValueError):
        port = 0
    port_part = f":{port}" if port and port != (443 if tls else 80) else ""
    return f"{'https' if tls else 'http'}://{domain}{port_part}/{path}/{sub_id}"


async def fetch_tuic_links(client, sub_id: str, headers: Optional[Dict[str, str]] = None) -> List[str]:
    """tuic://-ссылки клиента из Clash-подписки панели (с коротким кэшем)."""
    cache_key = f"{getattr(client, 'server_id', '')}:{sub_id}"
    cached = _cache.get(cache_key)
    if cached and cached[0] > time.monotonic():
        return list(cached[1])

    links: List[str] = []
    ttl = _CACHE_TTL_OK
    try:
        url = await _clash_url(client, sub_id)
        if url:
            connector = aiohttp.TCPConnector(ssl=False)  # как и остальные запросы бота к панели
            async with aiohttp.ClientSession(connector=connector, timeout=aiohttp.ClientTimeout(total=8)) as session:
                async with session.get(url, headers=headers or {}) as resp:
                    if resp.status == 200:
                        links = tuic_links_from_clash(await resp.text())
                    else:
                        ttl = _CACHE_TTL_FAIL
    except Exception as e:  # noqa: BLE001
        logger.debug("tuic_fallback: Clash-подписка недоступна (%s): %s", sub_id[:8], e)
        ttl = _CACHE_TTL_FAIL

    if len(_cache) >= _CACHE_MAX:
        _cache.pop(next(iter(_cache)))
    _cache[cache_key] = (time.monotonic() + ttl, list(links))
    return links


async def add_missing_tuic_text(raw: str, client, sub_id: str, headers: Optional[Dict[str, str]] = None) -> str:
    """Для списка ссылок (экран «Подключения»): добавляет TUIC, если панель их не отдала."""
    lines = [line.strip() for line in (raw or "").splitlines() if line.strip()]
    if not lines or has_tuic(lines):
        return raw
    extra = await fetch_tuic_links(client, sub_id, headers)
    return "\n".join(lines + [x for x in extra if x not in lines]) if extra else raw


async def merge_tuic_into_subscription(body: bytes, key: Dict[str, Any], forward_headers: Dict[str, str]) -> bytes:
    """Для /happ-sub: добавляет TUIC в тело подписки в том же формате (base64 или текст)."""
    from bot.services.cdn import _split_links, merge_subscription_bodies

    links, _ = _split_links(body)
    if not links or has_tuic(links):
        return body
    if not key or not key.get("sub_id") or not key.get("server_id"):
        return body
    from bot.services.vpn_api import get_client
    client = await get_client(int(key["server_id"]))
    extra = await fetch_tuic_links(client, key["sub_id"], forward_headers)
    if not extra:
        return body
    return merge_subscription_bodies(body, "\n".join(extra).encode("utf-8"))
