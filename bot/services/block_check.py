"""Проверка IP серверов и нод на блокировку ТСПУ через публичный сервис cheburcheck.ru.

Сервис отдаёт результат двумя запросами: /api/v1/check выдаёт токен проверки, а
/api/v1/probe/<токен> потоком (SSE) присылает вердикт каждого сканера из российских сетей:
tspu_block, uncertain или ok. Бот считает долю сканеров с блокировкой и сообщает админу.

Нагрузку на чужой бесплатный сервис держим малой: результат кэшируется на 10 минут,
плановая проверка идёт раз в 2 часа, цели проверяются по очереди с паузой.
"""
from __future__ import annotations

import asyncio
import json
import logging
import socket
import time
from typing import Any, Dict, List, Optional, Tuple

import aiohttp

logger = logging.getLogger(__name__)

BASE = "https://cheburcheck.ru/api/v1"
ALERT_RATIO = 0.3          # доля сканеров с блокировкой, с которой подаём тревогу
CACHE_TTL = 600
SCHEDULE_EVERY = 7200
STREAM_TIMEOUT = 40
PAUSE = 1.5
HEADERS = {"User-Agent": "EclipseBot-block-check/1.0"}

_cache: Dict[str, Tuple[float, Dict[str, Any]]] = {}
_sched: Dict[str, Any] = {"at": 0.0, "ts": 0.0, "checks": []}


def parse_sse(text: str) -> List[Dict[str, Any]]:
    """Все события result из текста SSE-потока."""
    out: List[Dict[str, Any]] = []
    event = None
    for line in text.splitlines():
        if line.startswith("event:"):
            event = line[6:].strip()
        elif line.startswith("data:") and event == "result":
            try:
                out.append(json.loads(line[5:].strip()))
            except ValueError:
                pass
    return out


def summarize(results: List[Dict[str, Any]], check: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    blocked = [r for r in results if "tspu_block" in (r.get("verdicts") or [])]
    unsure = [r for r in results if "uncertain" in (r.get("verdicts") or [])]
    total = len(results)
    regions: List[str] = []
    for r in blocked:
        reg = str(r.get("region") or "").strip()
        if reg and reg not in regions:
            regions.append(reg)
    check = check or {}
    return {
        "total": total,
        "blocked": len(blocked),
        "uncertain": len(unsure),
        "ok": total - len(blocked) - len(unsure),
        "ratio": (len(blocked) / total) if total else 0.0,
        "regions": regions,
        "cdn": bool(check.get("cdn_providers")),
        "listed": bool(check.get("blocked_subnets")),
    }


async def check_ip(ip: str) -> Dict[str, Any]:
    """Проверяет один IPv4. При ошибке возвращает {"error": текст}."""
    cached = _cache.get(ip)
    if cached and cached[0] > time.monotonic():
        return cached[1]
    try:
        timeout = aiohttp.ClientTimeout(total=STREAM_TIMEOUT + 10)
        async with aiohttp.ClientSession(timeout=timeout, headers=HEADERS) as s:
            async with s.get(f"{BASE}/check", params={"target": ip}) as r:
                r.raise_for_status()
                check = await r.json()
            token = check.get("id")
            if not token:
                return {"error": "сервис не выдал токен проверки"}
            chunks: List[str] = []
            async with s.get(f"{BASE}/probe/{token}") as r:
                r.raise_for_status()
                started = time.monotonic()
                async for raw in r.content:
                    line = raw.decode("utf-8", "ignore")
                    chunks.append(line)
                    if line.startswith("event:") and line[6:].strip() == "done":
                        break
                    if time.monotonic() - started > STREAM_TIMEOUT:
                        break
        res = summarize(parse_sse("".join(chunks)), check)
        if not res["total"]:
            return {"error": "сканеры не ответили"}
    except Exception as e:  # noqa: BLE001
        logger.debug("block_check %s: %s", "ip", e)
        return {"error": "сервис проверки недоступен"}
    _cache[ip] = (time.monotonic() + CACHE_TTL, res)
    return res


async def resolve_ipv4(host: str) -> Optional[str]:
    host = (host or "").strip()
    if not host:
        return None
    try:
        infos = await asyncio.get_running_loop().getaddrinfo(host, None, family=socket.AF_INET)
        return infos[0][4][0]
    except Exception:  # noqa: BLE001
        return None


def verdict_line(res: Dict[str, Any]) -> Tuple[str, str]:
    """(значок, текст) без адресов."""
    if res.get("error"):
        return "⚪", res["error"]
    if res["cdn"]:
        return "⚪", "адрес за CDN, проверка по нему неточна"
    text = f"заблокирован в {res['blocked']} из {res['total']} регионов" if res["blocked"] else f"без блокировок ({res['total']} сканеров)"
    if res["uncertain"]:
        text += f", неясно: {res['uncertain']}"
    if res["ratio"] >= ALERT_RATIO:
        return "🔴", text
    return ("🟡" if res["blocked"] else "🟢"), text


async def targets_for_server(server: Dict[str, Any]) -> List[Tuple[str, str]]:
    """[(название, IPv4)] для панели сервера и её нод. Узлы «в ремонте» пропускаются."""
    from bot.services import failover, node_failover
    out: List[Tuple[str, str]] = []
    sid = server.get("id")
    name = str(server.get("name") or f"#{sid}")
    if not failover.is_down(sid):
        ip = await resolve_ipv4(str(server.get("host") or ""))
        if ip:
            out.append((name, ip))
    try:
        from bot.services.vpn_api import get_client
        nodes = await (await get_client(int(sid))).get_nodes()
    except Exception:  # noqa: BLE001
        nodes = []
    for n in nodes:
        if not isinstance(n, dict) or n.get("id") is None:
            continue
        if node_failover.excluded_ids(int(sid), int(n["id"])):
            continue
        ip = await resolve_ipv4(str(n.get("address") or ""))
        if ip:
            out.append((f"{name} / {n.get('name') or n.get('id')}", ip))
    seen, uniq = set(), []
    for title, ip in out:
        if ip not in seen:
            seen.add(ip)
            uniq.append((title, ip))
    return uniq


async def check_server(server: Dict[str, Any]) -> List[Tuple[str, str, str]]:
    """[(значок, название, текст)] по панели и нодам сервера."""
    rows = []
    for i, (title, ip) in enumerate(await targets_for_server(server)):
        if i:
            await asyncio.sleep(PAUSE)
        icon, text = verdict_line(await check_ip(ip))
        rows.append((icon, title, text))
    return rows


def licensed() -> bool:
    """Функция входит в лицензию (на главной установке доступна всегда)."""
    from bot.services.license import is_feature_available
    return is_feature_available("block_check")


def auto_enabled() -> bool:
    from database.requests import get_setting
    return licensed() and (get_setting("block_check_auto", "1") or "1") == "1"


async def scheduled_checks() -> List[Tuple[str, str, str]]:
    """Проверки для оповещений: не чаще раза в 2 часа, между запусками отдаёт прошлый результат."""
    if not auto_enabled():
        return []
    if time.monotonic() - _sched["at"] < SCHEDULE_EVERY and _sched["at"]:
        return list(_sched["checks"])
    from database.requests import get_all_servers
    checks: List[Tuple[str, str, str]] = []
    for srv in [s for s in get_all_servers() if s.get("is_active")]:
        for icon, title, text in await check_server(srv):
            if icon != "⚪":  # ошибки сервиса проверки тревогой не считаем
                checks.append((icon, f"Блокировка: {title}", text))
        await asyncio.sleep(PAUSE)
    _sched.update(at=time.monotonic(), ts=time.time(), checks=checks)
    return list(checks)


def diagnostics_lines() -> List[str]:
    """Блок для «Диагностики» из результата последней плановой проверки (без запросов к сервису)."""
    from bot.utils.text import escape_html
    if not licensed():
        return []
    lines = ["<b>━━ Блокировки IP ━━</b>"]
    if not auto_enabled():
        return lines + ["⏸ Авто-проверка выключена (кнопка на экране «Серверы»)"]
    if not _sched["ts"]:
        return lines + ["⏳ Ещё не выполнялась, первая проверка скоро"]
    for icon, title, text in _sched["checks"][:8]:
        lines.append(f"{icon} {escape_html(title.removeprefix('Блокировка: '))}: {escape_html(text)}")
    if not _sched["checks"]:
        lines.append("Нет адресов для проверки")
    mins = int((time.time() - _sched["ts"]) // 60)
    lines.append(f"Проверено {mins} мин назад")
    return lines
