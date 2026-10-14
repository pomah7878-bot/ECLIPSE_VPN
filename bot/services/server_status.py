"""Статус серверов для клиентов: публичная страница /status и экран в боте.

Показываем только название сервера и «работает / не отвечает».
Пинг клиентам не показываем: он измеряется от сервера бота до панели и
не отражает скорость у клиента, а большие цифры только пугают.
Адреса серверов (IP, домены) наружу не отдаются никогда: если админ назвал
сервер его адресом, вместо названия выводится «Сервер N».

Данные берутся из кэша пинга (servers.latency_ms), который планировщик
обновляет примерно раз в 15 минут, поэтому страница не нагружает панели.
По умолчанию выключено (настройка status_page_enabled).
"""
import re
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

STALE_MINUTES = 40  # пинг старше этого считаем «нет данных»

_IP_RE = re.compile(r"\b\d{1,3}(?:\.\d{1,3}){3}\b|\b[0-9a-f]{0,4}(?::[0-9a-f]{0,4}){2,7}\b", re.I)
_DOMAIN_RE = re.compile(r"\b[a-z0-9-]+(?:\.[a-z0-9-]+)*\.[a-z]{2,}\b", re.I)


def is_enabled() -> bool:
    try:
        from database.requests import get_setting
        return str(get_setting("status_page_enabled", "0") or "0") == "1"
    except Exception:
        return False


def set_enabled(enabled: bool) -> None:
    from database.requests import set_setting
    set_setting("status_page_enabled", "1" if enabled else "0")


def public_name(name: Optional[str], index: int) -> str:
    """Название для клиента: без IP-адресов и доменов."""
    text = str(name or "").strip()
    if not text or _IP_RE.search(text) or _DOMAIN_RE.search(text):
        return f"Сервер {index}"
    return text[:40]


def _parse_ts(value: Any) -> Optional[datetime]:
    if not value:
        return None
    try:
        ts = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)  # CURRENT_TIMESTAMP в SQLite — UTC
    return ts


def collect(now: Optional[datetime] = None) -> Dict[str, Any]:
    """Сводка для страницы и бота: state = up | down | unknown."""
    from database.requests import get_all_servers

    now = now or datetime.now(timezone.utc)
    items: List[Dict[str, Any]] = []
    newest: Optional[datetime] = None
    active = [s for s in get_all_servers() if s.get("is_active")]
    for i, srv in enumerate(active, start=1):
        checked = _parse_ts(srv.get("latency_checked_at"))
        latency = srv.get("latency_ms")
        if checked is None or (now - checked).total_seconds() > STALE_MINUTES * 60:
            state = "unknown"
        elif latency is None:
            state = "down"
        else:
            state = "up"
        if checked and (newest is None or checked > newest):
            newest = checked
        items.append({
            "name": public_name(srv.get("name"), i),
            "state": state,
        })
    up = sum(1 for x in items if x["state"] == "up")
    down = sum(1 for x in items if x["state"] == "down")
    if not items:
        overall = "unknown"
    elif down == 0 and up == len(items):
        overall = "ok"
    elif up == 0 and down:
        overall = "outage"
    else:
        overall = "partial"
    return {
        "overall": overall,
        "servers": items,
        "checked_at": newest.isoformat() if newest else None,
    }


STATE_ICON = {"up": "🟢", "down": "🔴", "unknown": "⚪"}
OVERALL_TEXT = {
    "ok": "✅ Все серверы работают",
    "partial": "⚠️ Часть серверов не отвечает",
    "outage": "⛔ Серверы не отвечают, уже разбираемся",
    "unknown": "⚪ Нет свежих данных",
}


def bot_text(data: Optional[Dict[str, Any]] = None) -> str:
    data = data or collect()
    lines = ["📡 <b>Статус серверов</b>", "", OVERALL_TEXT[data["overall"]], ""]
    from bot.utils.text import escape_html
    for s in data["servers"]:
        tail = {"up": " — работает", "down": " — не отвечает"}.get(s["state"], " — нет данных")
        lines.append(f"{STATE_ICON[s['state']]} {escape_html(s['name'])}{tail}")
    if not data["servers"]:
        lines.append("Серверы ещё не добавлены.")
    lines += ["", "<i>Проверка раз в 15 минут.</i>"]
    return "\n".join(lines)
