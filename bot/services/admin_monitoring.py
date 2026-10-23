"""
Collection and formatting of monitoring for the admin panel.

Administrator handlers do not go directly to the database and panels: they receive a ready-made snapshot
and send it to the HTML renderers of this module.
"""
from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime
from typing import Any, Dict, List, Optional

from database.requests import (
    get_all_servers,
    get_daily_payments_stats,
    get_expiring_keys,
    get_keys_stats,
    get_new_users_count_today,
    get_payments_stats,
    get_users_stats,
)
from bot.services.vpn_api import format_traffic, get_client_from_server_data
from bot.utils.datetime_format import get_display_tzinfo
from bot.utils.text import escape_html

logger = logging.getLogger(__name__)

LOAD_WARNING_THRESHOLD = 90.0
NODE_DISPLAY_LIMIT = 8
MAX_SUMMARY_PROBLEMS = 3


def _safe_int(value: Any, default: int = 0) -> int:
    try:
        if value is None or value == "":
            return default
        return int(float(value))
    except (TypeError, ValueError):
        return default


def _safe_float(value: Any, default: Optional[float] = None) -> Optional[float]:
    try:
        if value is None or value == "":
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def _format_percent(value: Any) -> str:
    number = _safe_float(value)
    if number is None:
        return "—"
    if abs(number - round(number)) < 0.05:
        return f"{int(round(number))}%"
    return f"{number:.1f}%"


def _format_payments_sum(payments: Dict[str, Any]) -> str:
    parts: List[str] = []
    cents = _safe_int(payments.get("paid_cents"))
    rub = _safe_float(payments.get("paid_rub"), 0) or 0
    stars = _safe_int(payments.get("paid_stars"))

    if cents > 0:
        parts.append(f"${cents / 100:g}".replace(".", ","))
    if rub > 0:
        parts.append(f"{rub:g}".replace(".", ",") + " ₽")
    if stars > 0:
        parts.append(f"⭐{stars}")

    return " + ".join(parts) if parts else "0"


def _format_payment_parts(cents: Any = 0, rub: Any = 0, stars: Any = 0) -> str:
    """То же форматирование, что и _format_payments_sum, но принимает
    явные значения — нужно для раздельного вывода бот/сайт, где у сайта
    есть только rub (нет cents/stars)."""
    parts: List[str] = []
    cents_v = _safe_int(cents)
    rub_v = _safe_float(rub, 0) or 0
    stars_v = _safe_int(stars)

    if cents_v > 0:
        parts.append(f"${cents_v / 100:g}".replace(".", ","))
    if rub_v > 0:
        parts.append(f"{rub_v:g}".replace(".", ",") + " ₽")
    if stars_v > 0:
        parts.append(f"⭐{stars_v}")

    return " + ".join(parts) if parts else "0"


def _format_rate_pair(up: Any, down: Any) -> str:
    up_value = _safe_int(up)
    down_value = _safe_int(down)
    if up_value <= 0 and down_value <= 0:
        return ""
    return f"↑ {format_traffic(up_value)}/s ↓ {format_traffic(down_value)}/s"


def _is_xray_problem(node: Dict[str, Any]) -> bool:
    state = str(node.get("xrayState") or "").strip().lower()
    return state in {"error", "stop", "stopped"}


def _node_enabled(node: Dict[str, Any]) -> bool:
    return bool(node.get("enable", True))


def _normalize_node(raw: Dict[str, Any]) -> Dict[str, Any]:
    name = (
        raw.get("name")
        or raw.get("remark")
        or raw.get("address")
        or raw.get("guid")
        or f"node-{raw.get('id') or '?'}"
    )
    status = str(raw.get("status") or "unknown").strip().lower() or "unknown"
    cpu = _safe_float(raw.get("cpuPct"), 0) or 0
    mem = _safe_float(raw.get("memPct"), 0) or 0
    enabled = _node_enabled(raw)
    xray_problem = _is_xray_problem(raw)

    if not enabled:
        severity = 0
        problem = False
    elif status != "online":
        severity = 100
        problem = True
    elif xray_problem:
        severity = 90
        problem = True
    elif cpu >= LOAD_WARNING_THRESHOLD or mem >= LOAD_WARNING_THRESHOLD:
        severity = 50
        problem = True
    else:
        severity = 0
        problem = False

    node = dict(raw)
    node.update(
        {
            "name": str(name),
            "status": status,
            "enable": enabled,
            "cpuPct": cpu,
            "memPct": mem,
            "onlineCount": _safe_int(raw.get("onlineCount")),
            "clientCount": _safe_int(raw.get("clientCount")),
            "activeCount": _safe_int(raw.get("activeCount")),
            "disabledCount": _safe_int(raw.get("disabledCount")),
            "depletedCount": _safe_int(raw.get("depletedCount")),
            "netUp": _safe_int(raw.get("netUp")),
            "netDown": _safe_int(raw.get("netDown")),
            "latencyMs": _safe_int(raw.get("latencyMs")),
            "lastHeartbeat": _safe_int(raw.get("lastHeartbeat")),
            "problem": problem,
            "severity": severity,
            "xray_problem": xray_problem,
        }
    )
    return node


def _flatten_nodes(raw_nodes: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    result: List[Dict[str, Any]] = []

    def walk(items: List[Dict[str, Any]]) -> None:
        for item in items:
            if not isinstance(item, dict):
                continue
            children = item.get("children") or item.get("nodes") or []
            current = dict(item)
            current.pop("children", None)
            current.pop("nodes", None)
            result.append(_normalize_node(current))
            if isinstance(children, list):
                walk([child for child in children if isinstance(child, dict)])

    walk(raw_nodes)
    return result


def _panel_problem_line(entry: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    server = entry["server"]
    if not entry.get("is_active"):
        return None
    if entry.get("panel_online"):
        return None
    name = escape_html(str(server.get("name") or "Панель"))
    error = escape_html(str(entry.get("error") or "нет подключения"))
    return {"severity": 120, "text": f"🔴 {name} — панель недоступна: {error}"}


def _node_problem_line(server: Dict[str, Any], node: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    if not node.get("problem"):
        return None

    server_name = escape_html(str(server.get("name") or "Панель"))
    node_name = escape_html(str(node.get("name") or "Нода"))
    status = node.get("status")

    if status != "online":
        detail = escape_html(str(node.get("lastError") or "нет связи"))
        text = f"🔴 {server_name} / {node_name} — {detail}"
    elif node.get("xray_problem"):
        xray_state = escape_html(str(node.get("xrayState") or "Xray"))
        xray_error = escape_html(str(node.get("xrayError") or "").strip())
        suffix = f": {xray_error}" if xray_error else ""
        text = f"🟣 {server_name} / {node_name} — Xray {xray_state}{suffix}"
    else:
        text = (
            f"🟠 {server_name} / {node_name} — "
            f"CPU {_format_percent(node.get('cpuPct'))}, RAM {_format_percent(node.get('memPct'))}"
        )

    return {"severity": node.get("severity", 0), "text": text}


# Панель, которая отвечает дольше, не должна держать открытым весь экран админки
SERVER_ENTRY_TIMEOUT = 6.0
_snapshot_cache: Dict[str, Any] = {"at": 0.0, "data": None}


async def _collect_server_entry(server: Dict[str, Any]) -> Dict[str, Any]:
    started = time.monotonic()
    try:
        entry = await asyncio.wait_for(_collect_server_entry_inner(server), SERVER_ENTRY_TIMEOUT)
    except asyncio.TimeoutError:
        logger.warning(f"Мониторинг панели {server.get('name')}: нет ответа за {SERVER_ENTRY_TIMEOUT:.0f} с")
        return {"server": server, "is_active": bool(server.get("is_active")), "panel_online": False,
                "stats": {}, "nodes": [], "error": "нет ответа"}
    elapsed = time.monotonic() - started
    if elapsed > 2.0:
        logger.info(f"Мониторинг панели {server.get('name')}: ответ за {elapsed:.1f} с")
    return entry


async def _collect_server_entry_inner(server: Dict[str, Any]) -> Dict[str, Any]:
    entry: Dict[str, Any] = {
        "server": server,
        "is_active": bool(server.get("is_active")),
        "panel_online": False,
        "stats": {},
        "nodes": [],
        "error": None,
    }

    if not entry["is_active"]:
        return entry

    try:
        client = get_client_from_server_data(server)
        # Статистика и ноды — независимые запросы: идут одновременно, а не друг за другом
        stats, raw_nodes = await asyncio.gather(client.get_stats(), client.get_nodes(), return_exceptions=True)
        if isinstance(stats, BaseException):
            raise stats
        entry["stats"] = stats
        entry["panel_online"] = bool(stats.get("online"))
        if not entry["panel_online"]:
            entry["error"] = stats.get("error") or "нет подключения"
            return entry

        if isinstance(raw_nodes, BaseException):
            logger.debug(f"Не удалось получить ноды панели {server.get('name')}: {raw_nodes}")
            entry["nodes"] = []
        else:
            entry["nodes"] = _flatten_nodes(raw_nodes)
    except Exception as e:
        logger.warning(f"Ошибка получения мониторинга панели {server.get('name')}: {e}")
        entry["error"] = "ошибка подключения"

    return entry


async def collect_admin_monitoring_snapshot(max_age: float = 0.0) -> Dict[str, Any]:
    """Collects a common snapshot for the main admin panel and the servers section.

    max_age > 0 — можно вернуть снимок не старше max_age секунд (главная админки открывается сразу)."""
    if max_age > 0 and _snapshot_cache["data"] is not None and time.monotonic() - _snapshot_cache["at"] < max_age:
        return _snapshot_cache["data"]
    snapshot = await _collect_admin_monitoring_snapshot()
    _snapshot_cache.update(at=time.monotonic(), data=snapshot)
    return snapshot


async def _collect_admin_monitoring_snapshot() -> Dict[str, Any]:
    servers = get_all_servers()
    entries = await asyncio.gather(*[_collect_server_entry(server) for server in servers]) if servers else []

    users = get_users_stats()
    keys = get_keys_stats()
    payments = get_daily_payments_stats()
    payments_7d = get_payments_stats(7)
    expiring_24h = len(get_expiring_keys(1))
    expiring_3d = len(get_expiring_keys(3))
    new_users = get_new_users_count_today()

    problems: List[Dict[str, Any]] = []
    for entry in entries:
        panel_problem = _panel_problem_line(entry)
        if panel_problem:
            problems.append(panel_problem)
        for node in entry.get("nodes", []):
            node_problem = _node_problem_line(entry["server"], node)
            if node_problem:
                problems.append(node_problem)

    problems.sort(key=lambda item: item.get("severity", 0), reverse=True)

    return {
        "collected_at": datetime.now(get_display_tzinfo()),
        "servers": entries,
        "users": users,
        "keys": keys,
        "payments": payments,
        "payments_7d": payments_7d,
        "new_users": new_users,
        "expiring_24h": expiring_24h,
        "expiring_3d": expiring_3d,
        "problems": problems,
    }


def _snapshot_counts(snapshot: Dict[str, Any]) -> Dict[str, int]:
    entries = snapshot.get("servers", [])
    active_panels = [entry for entry in entries if entry.get("is_active")]
    online_panels = [entry for entry in active_panels if entry.get("panel_online")]
    inactive_panels = [entry for entry in entries if not entry.get("is_active")]

    nodes = [node for entry in entries for node in entry.get("nodes", [])]
    enabled_nodes = [node for node in nodes if node.get("enable", True)]
    online_nodes = [node for node in enabled_nodes if node.get("status") == "online"]
    disabled_nodes = [node for node in nodes if not node.get("enable", True)]

    return {
        "panels_total": len(entries),
        "active_panels": len(active_panels),
        "online_panels": len(online_panels),
        "inactive_panels": len(inactive_panels),
        "nodes_total": len(enabled_nodes),
        "online_nodes": len(online_nodes),
        "disabled_nodes": len(disabled_nodes),
        "panel_online_clients": sum(
            _safe_int((entry.get("stats") or {}).get("online_clients"))
            for entry in online_panels
        ),
    }


def _plural(n: int, one: str, few: str, many: str) -> str:
    n = abs(int(n))
    if n % 10 == 1 and n % 100 != 11:
        return one
    if 2 <= n % 10 <= 4 and not 12 <= n % 100 <= 14:
        return few
    return many


def _fmt_rub(value: Any) -> str:
    rub = int(round(_safe_float(value, 0) or 0))
    return f"{rub:,}".replace(",", "\u202f") + " ₽"


def _money_text(payments: Dict[str, Any]) -> str:
    """Сумма за период: рубли, затем доллары (крипта) и звёзды, если были."""
    parts = [_fmt_rub(payments.get("paid_rub"))]
    cents = _safe_int(payments.get("paid_cents"))
    stars = _safe_int(payments.get("paid_stars"))
    if cents > 0:
        parts.append(f"${cents / 100:g}".replace(".", ","))
    if stars > 0:
        parts.append(f"⭐{stars}")
    return " + ".join(parts)


def _payments_count_text(count: int) -> str:
    return f"{count} {_plural(count, 'оплата', 'оплаты', 'оплат')}"


def _pct(value: Any) -> str:
    number = _safe_float(value)
    return "—" if number is None else f"{int(round(number))}%"


_LINE_FIT = 37  # примерно столько знаков помещается в строку цитаты на телефоне


def _visual_len(text: str) -> int:
    """Грубая ширина строки: эмодзи шире букв, флаг — два символа-половинки."""
    import re as _re
    import unicodedata as _ud
    plain = _re.sub(r"<[^>]+>", "", text)
    width = 0
    for ch in plain:
        code = ord(ch)
        if code in (0xFE0F, 0x200D):
            continue
        if 0x1F1E6 <= code <= 0x1F1FF:
            width += 1
        elif code > 0xFFFF or _ud.category(ch) == "So":
            width += 2
        else:
            width += 1
    return width


def _server_lines(entries: List[Dict[str, Any]]) -> List[str]:
    """Короткие строки, чтобы помещались в ширину телефона без переносов."""
    lines: List[str] = []
    for entry in entries:
        server = entry["server"]
        name = escape_html(str(server.get("name") or "Сервер"))
        if len(entries) > 1 or entry.get("nodes"):
            lines.append(f"<b>{name}</b>")
        if not entry.get("is_active"):
            lines.append("⏸ Мониторинг выключен")
            continue
        if not entry.get("panel_online"):
            error = escape_html(str(entry.get("error") or "нет подключения"))
            lines.append(f"🔴 Панель — {error}")
            continue
        stats = entry.get("stats") or {}
        parts = ["🟢 Панель", f"🔑 {_safe_int(stats.get('online_clients'))}"]
        if stats.get("cpu_percent") is not None:
            parts.append(f"CPU {_pct(stats.get('cpu_percent'))}")
        lines.append(" · ".join(parts))

        nodes = entry.get("nodes", [])
        selected = _select_nodes_for_display(nodes)
        rows: List[Any] = []
        for node in selected:
            icon = _node_icon(node)
            node_name = escape_html(str(node.get("name") or "Нода"))
            if not node.get("enable", True):
                rows.append(f"{icon} {node_name} — выключена")
            elif node.get("status") != "online":
                detail = escape_html(str(node.get("lastError") or "нет связи"))
                rows.append(f"{icon} {node_name} — {detail}")
            else:
                node_parts = [
                    f"{icon} {node_name}",
                    f"🔑 {node.get('onlineCount', 0)}",
                    f"CPU {_pct(node.get('cpuPct'))}",
                    f"RAM {_pct(node.get('memPct'))}",
                ]
                if node.get("xray_problem"):
                    node_parts.append(f"Xray {escape_html(str(node.get('xrayState') or ''))}")
                rows.append(node_parts)
        # Если хоть одна нода не помещается в строку — нагрузку всех нод
        # выносим на вторую строку, чтобы список выглядел одинаково.
        split = any(
            isinstance(row, list) and _visual_len(" · ".join(row)) > _LINE_FIT
            for row in rows
        )
        for row in rows:
            if not isinstance(row, list):
                lines.append(row)
            elif split:
                lines.append(" · ".join(row[:2]))
                lines.append("      " + " · ".join(row[2:]))
            else:
                lines.append(" · ".join(row))
        omitted = len(nodes) - len(selected)
        if omitted > 0:
            lines.append(f"… ещё {omitted}")
    return lines


def build_admin_summary_text(snapshot: Dict[str, Any]) -> str:
    """Главный экран админ-панели: сводка сверху, подробности в раскрываемых блоках."""
    entries = snapshot.get("servers", [])
    if not entries:
        return (
            "⚙️ <b>Админ-панель</b>\n\n"
            "🖥️ Серверов пока нет.\n"
            "Добавьте первый сервер в разделе «Сервера»."
        )

    counts = _snapshot_counts(snapshot)
    problems = snapshot.get("problems", [])
    collected_at = snapshot.get("collected_at")
    updated = collected_at.strftime("%H:%M") if isinstance(collected_at, datetime) else "—"

    if counts["active_panels"] == 0:
        health = "⚠️ <b>Мониторинг выключен</b>"
    elif counts["online_panels"] == 0:
        health = "🔴 <b>Все панели недоступны</b>"
    elif problems:
        health = "⚠️ <b>Есть проблемы</b>"
    else:
        health = "🟢 <b>Всё работает</b>"

    payments = snapshot.get("payments", {}) or {}
    payments_7d = snapshot.get("payments_7d", {}) or {}
    users = snapshot.get("users", {}) or {}
    keys = snapshot.get("keys", {}) or {}
    new_users = _safe_int(snapshot.get("new_users"))

    servers_total = counts["active_panels"] + counts["nodes_total"]
    servers_online = counts["online_panels"] + counts["online_nodes"]
    total_users = _safe_int(users.get("total"))
    active_users = _safe_int(users.get("active"))

    users_line = (
        f"👥 {total_users} {_plural(total_users, 'клиент', 'клиента', 'клиентов')}"
        f" · {active_users} {_plural(active_users, 'активный', 'активных', 'активных')}"
    )
    if new_users:
        users_line += f" · +{new_users} за сутки"

    summary = [
        f"💰 {_money_text(payments)} сегодня · {_money_text(payments_7d)} за 7 дн.",
        users_line,
        f"📶 {counts['panel_online_clients']} онлайн · серверы {servers_online}/{servers_total}",
    ]

    lines = [
        "⚙️ <b>Админ-панель</b>",
        f"{health} · {updated}",
        "<blockquote>" + "\n".join(summary) + "</blockquote>",
    ]

    if problems:
        shown = [problem["text"] for problem in problems[:MAX_SUMMARY_PROBLEMS]]
        omitted = len(problems) - MAX_SUMMARY_PROBLEMS
        if omitted > 0:
            shown.append(f"… ещё {omitted}")
        lines.append("<b>Требует внимания</b>")
        lines.append("<blockquote>" + "\n".join(shown) + "</blockquote>")

    # Серверы
    lines.append(
        "<blockquote expandable>🖥 <b>Серверы</b>\n"
        + "\n".join(_server_lines(entries))
        + "</blockquote>"
    )

    # Оплаты
    day_count = _safe_int(payments.get("paid_count"))
    week_count = _safe_int(payments_7d.get("paid_count"))
    pay_lines = ["💰 <b>Оплаты</b>"]
    if week_count == 0:
        pay_lines.append("За 7 дней оплат не было")
    else:
        pay_lines.append(
            f"Сегодня: {_payments_count_text(day_count)} · {_money_text(payments)}"
            if day_count else "Сегодня оплат не было"
        )
        pay_lines.append(f"7 дней: {_payments_count_text(week_count)} · {_money_text(payments_7d)}")
        rub_count = _safe_int(payments_7d.get("rub_count"))
        rub_total = _safe_float(payments_7d.get("paid_rub"), 0) or 0
        if rub_count > 0 and rub_total > 0:
            pay_lines.append(f"Средний чек: {_fmt_rub(rub_total / rub_count)}")
        pay_lines.append(
            f"Бот: {_safe_int(payments_7d.get('bot_count'))}"
            f" · Сайт: {_safe_int(payments_7d.get('site_count'))}"
        )
    lines.append("<blockquote expandable>" + "\n".join(pay_lines) + "</blockquote>")

    # Ключи
    exp_1 = _safe_int(snapshot.get("expiring_24h"))
    exp_3 = _safe_int(snapshot.get("expiring_3d"))
    key_lines = [
        "🔑 <b>Ключи</b>",
        f"Активных: {_safe_int(keys.get('active'))}",
        f"Создано за сутки: {_safe_int(keys.get('created_today'))}",
        # самая длинная строка — после третьей: в свёрнутом виде стрелка не закрывает текст
        f"Истекут: за сутки {exp_1} · за 3 дня {exp_3}",
    ]
    lines.append("<blockquote expandable>" + "\n".join(key_lines) + "</blockquote>")

    return "\n".join(lines)


def _node_icon(node: Dict[str, Any]) -> str:
    if not node.get("enable", True):
        return "⚪"
    if node.get("status") != "online":
        return "🔴"
    if node.get("xray_problem"):
        return "🟣"
    if node.get("problem"):
        return "🟠"
    return "🟢"


def _format_node_line(node: Dict[str, Any], prefix: str) -> str:
    icon = _node_icon(node)
    name = escape_html(str(node.get("name") or "Нода"))

    if not node.get("enable", True):
        return f"   {prefix} {icon} {name} | выключена"

    if node.get("status") != "online":
        detail = escape_html(str(node.get("lastError") or "нет связи"))
        return f"   {prefix} {icon} {name} | {detail}"

    parts = [
        f"🔑 {node.get('onlineCount', 0)}",
        f"💻 {_format_percent(node.get('cpuPct'))}",
        f"🧠 {_format_percent(node.get('memPct'))}",
    ]
    rate = _format_rate_pair(node.get("netUp"), node.get("netDown"))
    if rate:
        parts.append(rate)

    if node.get("xray_problem"):
        state = escape_html(str(node.get("xrayState") or "Xray"))
        error = escape_html(str(node.get("xrayError") or "").strip())
        xray_text = f"Xray {state}"
        if error:
            xray_text += f": {error}"
        parts.append(xray_text)

    return f"   {prefix} {icon} {name} | " + " | ".join(parts)


def _select_nodes_for_display(nodes: List[Dict[str, Any]], limit: int = NODE_DISPLAY_LIMIT) -> List[Dict[str, Any]]:
    problems = [node for node in nodes if node.get("problem")]
    healthy = [node for node in nodes if not node.get("problem")]
    ordered = sorted(problems, key=lambda item: item.get("severity", 0), reverse=True) + healthy
    return ordered[:limit]


def build_servers_monitoring_text(snapshot: Dict[str, Any]) -> str:
    """Generates detailed monitoring for the “Servers” section."""
    entries = snapshot.get("servers", [])
    if not entries:
        return (
            "🖥️ <b>Сервера</b>\n\n"
            "Серверов пока нет.\n"
            "Нажмите «➕ Добавить сервер» чтобы добавить первый!"
        )

    collected_at = snapshot.get("collected_at")
    updated = collected_at.strftime("%H:%M") if isinstance(collected_at, datetime) else "—"
    lines = ["🖥️ <b>Сервера</b>", f"🕒 Обновлено: {updated}", ""]

    for entry in entries:
        server = entry["server"]
        server_name = escape_html(str(server.get("name") or "Сервер"))
        host = escape_html(f"{server.get('host')}:{server.get('port')}")

        if not entry.get("is_active"):
            lines.extend(
                [
                    f"🔴 <b>{server_name}</b> (<code>{host}</code>)",
                    "   ⏸️ Деактивирован",
                    "",
                ]
            )
            continue

        if not entry.get("panel_online"):
            error = escape_html(str(entry.get("error") or "нет подключения"))
            lines.extend(
                [
                    f"🔴 <b>{server_name}</b> (<code>{host}</code>)",
                    f"   ⚠️ Панель недоступна: {error}",
                    "",
                ]
            )
            continue

        stats = entry.get("stats") or {}
        traffic = format_traffic(_safe_int(stats.get("total_traffic_bytes")))
        cpu_text = ""
        if stats.get("cpu_percent") is not None:
            cpu_text = f" | 💻 {_format_percent(stats.get('cpu_percent'))}"

        lines.append(f"🟢 <b>{server_name}</b> (<code>{host}</code>)")
        lines.append(
            f"   Панель: онлайн | 🔑 {stats.get('online_clients', 0)} онлайн "
            f"| 📊 {traffic}{cpu_text}"
        )

        nodes = entry.get("nodes", [])
        if not nodes:
            lines.append("   🌐 Ноды: нет")
            lines.append("")
            continue

        enabled_nodes = [node for node in nodes if node.get("enable", True)]
        online_nodes = [node for node in enabled_nodes if node.get("status") == "online"]
        disabled_nodes = [node for node in nodes if not node.get("enable", True)]
        node_online_clients = sum(_safe_int(node.get("onlineCount")) for node in enabled_nodes)

        disabled_text = f" | ⏸️ {len(disabled_nodes)} выкл." if disabled_nodes else ""
        lines.append(
            f"   🌐 Ноды: {len(online_nodes)}/{len(enabled_nodes)} онлайн "
            f"| 🔑 {node_online_clients} онлайн{disabled_text}"
        )

        selected_nodes = _select_nodes_for_display(nodes)
        omitted = max(0, len(nodes) - len(selected_nodes))
        for index, node in enumerate(selected_nodes):
            is_last = index == len(selected_nodes) - 1 and omitted == 0
            prefix = "└" if is_last else "├"
            lines.append(_format_node_line(node, prefix))
        if omitted > 0:
            lines.append(f"   └ … ещё {omitted}")

        lines.append("")

    return "\n".join(lines).rstrip()
