"""Расширенная диагностика «🧩 Диагностика»: состояние всей системы одним экраном.

Каждая проверка только читает данные, ничего не меняет, ограничена по времени
и не роняет экран при ошибке: сбой одной проверки показывается её строкой.
Строка — (значок, название, подробности): ✅ норма, ⚠️ стоит посмотреть, 🔴 проблема, ⚪ нет данных.
"""
import asyncio
import logging
import os
import re
import shutil
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, List, Optional, Tuple

logger = logging.getLogger(__name__)

Check = Tuple[str, str, str]

BOT_DIR = Path(__file__).resolve().parents[2]
BACKUP_DIR = Path("/root/backups/db")
PANEL_TIMEOUT = 10.0
BACKUP_MAX_AGE_HOURS = 26
PING_MAX_AGE_MINUTES = 40


def _short_error(e: BaseException) -> str:
    """Короткое описание ошибки без адресов серверов."""
    if isinstance(e, asyncio.TimeoutError):
        return "не ответила вовремя"
    name = type(e).__name__
    text = str(e).split("\n")[0]
    for marker in ("http://", "https://"):
        if marker in text:
            text = ""
    return f"{name}: {text[:60]}" if text else name


def _parse_utc(value: Any) -> Optional[datetime]:
    if not value:
        return None
    try:
        ts = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return ts if ts.tzinfo else ts.replace(tzinfo=timezone.utc)


def _age_text(delta: timedelta) -> str:
    minutes = int(delta.total_seconds() // 60)
    if minutes < 60:
        return f"{max(minutes, 0)} мин назад"
    hours = minutes // 60
    if hours < 48:
        return f"{hours} ч назад"
    return f"{hours // 24} дн назад"


# ── проверки ────────────────────────────────────────────────────────────────

def check_version() -> Check:
    try:
        from bot.version import BOT_RELEASE
        from database.migrations import LATEST_VERSION, get_current_version as get_version
        db_ver = get_version()
        if db_ver != LATEST_VERSION:
            return ("🔴", "Версия", f"бот {BOT_RELEASE}, база v{db_ver} из v{LATEST_VERSION} — перезапустите бота")
        return ("✅", "Версия", f"бот {BOT_RELEASE}, база v{db_ver}")
    except Exception as e:
        return ("⚪", "Версия", _short_error(e))


def check_updates() -> Check:
    """Сколько обновлений ждёт (по последнему fetch, без обращения к GitHub)."""
    try:
        from bot.utils.git_utils import run_git_command
        ok, out = run_git_command(["rev-list", "--count", "HEAD..origin/main"])
        if not ok:
            return ("⚪", "Обновления", "нет данных")
        n = int(out.strip() or 0)
        if n:
            return ("⚠️", "Обновления", f"доступно коммитов: {n} — «🔄 Обновить бота»")
        return ("✅", "Обновления", "установлена последняя версия")
    except Exception as e:
        return ("⚪", "Обновления", _short_error(e))


def check_database() -> Check:
    try:
        from database.connection import DB_PATH, get_db
        with get_db() as conn:
            result = conn.execute("PRAGMA quick_check").fetchone()[0]
        size_mb = os.path.getsize(DB_PATH) / 1024 / 1024
        if str(result).lower() != "ok":
            return ("🔴", "База данных", f"повреждение: {str(result)[:80]}")
        return ("✅", "База данных", f"целостность в порядке, {size_mb:.1f} МБ")
    except Exception as e:
        return ("🔴", "База данных", _short_error(e))


def check_disk() -> Check:
    try:
        usage = shutil.disk_usage(BOT_DIR)
        free_gb = usage.free / 1024 ** 3
        free_pct = usage.free / usage.total * 100
        text = f"свободно {free_gb:.1f} ГБ ({free_pct:.0f}%)"
        if free_gb < 1 or free_pct < 5:
            return ("🔴", "Диск", text + " — почти закончилось место")
        if free_gb < 3 or free_pct < 10:
            return ("⚠️", "Диск", text)
        return ("✅", "Диск", text)
    except Exception as e:
        return ("⚪", "Диск", _short_error(e))


def check_backups(now: Optional[datetime] = None) -> Check:
    try:
        if not BACKUP_DIR.is_dir():
            return ("⚪", "Бэкапы", "папка бэкапов не найдена на этом сервере")
        files = sorted(BACKUP_DIR.glob("vpn_bot_*.db*"), key=lambda p: p.stat().st_mtime)
        if not files:
            return ("🔴", "Бэкапы", "ни одного бэкапа базы")
        now = now or datetime.now(timezone.utc)
        newest = datetime.fromtimestamp(files[-1].stat().st_mtime, timezone.utc)
        age = now - newest
        text = f"последний {_age_text(age)}, всего {len(files)}"
        if age > timedelta(hours=BACKUP_MAX_AGE_HOURS):
            return ("🔴", "Бэкапы", text + " — ежедневный бэкап не выполнялся")
        return ("✅", "Бэкапы", text)
    except Exception as e:
        return ("⚪", "Бэкапы", _short_error(e))


def check_scheduler(now: Optional[datetime] = None) -> Check:
    """Планировщик жив, если пинг серверов обновлялся недавно (раз в ~15 минут)."""
    try:
        from database.requests import get_all_servers
        servers = [s for s in get_all_servers() if s.get("is_active")]
        if not servers:
            return ("⚪", "Планировщик", "нет активных серверов")
        stamps = [_parse_utc(s.get("latency_checked_at")) for s in servers]
        stamps = [s for s in stamps if s]
        if not stamps:
            return ("⚠️", "Планировщик", "пинг серверов ещё не измерялся")
        now = now or datetime.now(timezone.utc)
        age = now - max(stamps)
        if age > timedelta(minutes=PING_MAX_AGE_MINUTES):
            return ("🔴", "Планировщик", f"фоновые задачи стоят: последний пинг {_age_text(age)}")
        return ("✅", "Планировщик", f"фоновые задачи работают, пинг {_age_text(age)}")
    except Exception as e:
        return ("⚪", "Планировщик", _short_error(e))


_ADDR_RE = re.compile(r"(https?://\S+)|\b\d{1,3}(?:\.\d{1,3}){3}(?::\d+)?\b|\b[a-z0-9-]+(?:\.[a-z0-9-]+)+\.[a-z]{2,}(?::\d+)?\b", re.I)


def _hide_addresses(text: str) -> str:
    """Убирает из текста ошибки IP-адреса, домены и ссылки."""
    return _ADDR_RE.sub("…", str(text or "")).strip()[:60]


def node_checks(panel_name: str, raw_nodes: List[dict]) -> List[Check]:
    """Строка на каждую ноду панели: онлайн, нагрузка, состояние Xray."""
    from bot.services.admin_monitoring import LOAD_WARNING_THRESHOLD, _flatten_nodes
    out: List[Check] = []
    for node in _flatten_nodes(raw_nodes or []):
        name = str(node.get("name") or "нода")[:30]
        title = f"Нода {name}"
        if not node.get("enable", True):
            out.append(("⚪", title, "выключена в панели"))
        elif node.get("status") != "online":
            err = _hide_addresses(node.get("lastError") or "")
            out.append(("🔴", title, "нет связи" + (f": {err}" if err else "")))
        elif node.get("xray_problem"):
            out.append(("🔴", title, f"Xray остановлен ({node.get('xrayState') or 'ошибка'})"))
        else:
            cpu, mem = float(node.get("cpuPct") or 0), float(node.get("memPct") or 0)
            text = f"онлайн, клиентов {node.get('onlineCount', 0)}, CPU {cpu:.0f}%, RAM {mem:.0f}%"
            icon = "⚠️" if cpu >= LOAD_WARNING_THRESHOLD or mem >= LOAD_WARNING_THRESHOLD else "✅"
            out.append((icon, title, text + (" — высокая нагрузка" if icon == "⚠️" else "")))
    return out


async def check_panels() -> List[Check]:
    """Каждая активная панель 3x-ui: вход, список подключений и её ноды (параллельно, с таймаутом)."""
    from database.requests import get_all_servers
    from bot.services.vpn_api import get_client

    servers = [s for s in get_all_servers() if s.get("is_active")]
    if not servers:
        return [("⚪", "Панели", "нет активных серверов")]

    async def one(srv) -> List[Check]:
        name = str(srv.get("name") or f"#{srv.get('id')}")[:48]
        started = time.monotonic()
        try:
            client = await get_client(srv["id"])
            inbounds = await asyncio.wait_for(client.get_inbounds(), PANEL_TIMEOUT)
            ms = int((time.monotonic() - started) * 1000)
        except Exception as e:
            return [("🔴", f"Панель {name}", _short_error(e))]
        if not inbounds:
            result = [("⚠️", f"Панель {name}", f"вход есть, но подключений нет ({ms} мс)")]
        else:
            result = [("✅", f"Панель {name}", f"подключений: {len(inbounds)}, ответ {ms} мс")]
        get_nodes = getattr(client, "get_nodes", None)
        if get_nodes is not None:
            try:
                raw_nodes = await asyncio.wait_for(get_nodes(), PANEL_TIMEOUT)
                result += node_checks(name, raw_nodes)
            except Exception as e:
                logger.debug("Ноды панели %s не получены: %s", name, e)
        return result

    groups = await asyncio.gather(*(one(s) for s in servers))
    return [c for g in groups for c in g]


async def check_telegram(bot) -> Check:
    if bot is None:
        return ("⚪", "Telegram API", "нет данных")
    started = time.monotonic()
    try:
        await asyncio.wait_for(bot.get_me(), 10)
        ms = int((time.monotonic() - started) * 1000)
        icon = "⚠️" if ms > 3000 else "✅"
        return (icon, "Telegram API", f"ответ {ms} мс")
    except Exception as e:
        return ("🔴", "Telegram API", _short_error(e))


def check_license(now: Optional[datetime] = None) -> Check:
    try:
        from bot.services.license import (
            _GRACE_PERIOD_HOURS, get_enabled_features, get_license_key, is_license_server,
        )
        from database.requests import get_setting
        if is_license_server():
            return ("✅", "Лицензия", "это сервер лицензий — все функции включены")
        if not get_license_key():
            return ("⚪", "Лицензия", "не введена — работают только бесплатные функции")
        checked = _parse_utc(get_setting("license_checked_at", ""))
        features = get_enabled_features()
        if checked is None:
            return ("⚠️", "Лицензия", "ещё не проверялась")
        now = now or datetime.now(timezone.utc)
        age = now - checked
        text = f"проверена {_age_text(age)}, функций: {len(features)}"
        if age > timedelta(hours=_GRACE_PERIOD_HOURS):
            return ("🔴", "Лицензия", text + " — сервер лицензий недоступен, функции отключены")
        if age > timedelta(hours=12):
            return ("⚠️", "Лицензия", text + " — давно не проверялась")
        return ("✅", "Лицензия", text)
    except Exception as e:
        return ("⚪", "Лицензия", _short_error(e))


def check_business() -> List[Check]:
    """Ключи, оплаты и заявки — быстрые счётчики из базы."""
    out: List[Check] = []
    try:
        from database.connection import get_db
        with get_db() as conn:
            active = conn.execute(
                "SELECT COUNT(*) FROM vpn_keys WHERE expires_at > datetime('now')"
            ).fetchone()[0]
            soon = conn.execute(
                "SELECT COUNT(*) FROM vpn_keys WHERE expires_at > datetime('now') "
                "AND expires_at <= datetime('now', '+3 days')"
            ).fetchone()[0]
            paid = conn.execute(
                "SELECT COUNT(*) FROM payments WHERE status = 'paid' AND paid_at >= datetime('now', '-1 day')"
            ).fetchone()[0]
        out.append(("✅", "Ключи", f"активных {active}, истекают за 3 дня: {soon}"))
        out.append(("✅" if paid else "⚪", "Оплаты", f"за 24 часа: {paid}"))
    except Exception as e:
        out.append(("⚪", "Ключи и оплаты", _short_error(e)))
    try:
        from bot.handlers.user.bonus_tasks import is_licensed, pending_count
        if is_licensed():
            n = pending_count()
            out.append(("⚠️" if n else "✅", "Бонусные дни", f"заявок на проверке: {n}"))
    except Exception:
        pass
    return out


async def collect(bot=None) -> List[Check]:
    """Все проверки. Синхронные выполняются в отдельном потоке, чтобы не тормозить бота."""
    loop = asyncio.get_running_loop()

    def sync_checks() -> List[Check]:
        return [check_version(), check_updates(), check_database(), check_disk(),
                check_backups(), check_scheduler(), check_license()] + check_business()

    sync_task = loop.run_in_executor(None, sync_checks)
    panels, telegram = await asyncio.gather(check_panels(), check_telegram(bot), return_exceptions=True)
    results = list(await sync_task)
    results += panels if isinstance(panels, list) else [("🔴", "Панели", _short_error(panels))]
    results.append(telegram if isinstance(telegram, tuple) else ("🔴", "Telegram API", _short_error(telegram)))
    return results


def format_block(checks: List[Check]) -> List[str]:
    bad = sum(1 for c in checks if c[0] == "🔴")
    warn = sum(1 for c in checks if c[0] == "⚠️")
    if bad:
        head = f"🔴 Проблем: {bad}" + (f", предупреждений: {warn}" if warn else "")
    elif warn:
        head = f"⚠️ Предупреждений: {warn}"
    else:
        head = "✅ Всё в порядке"
    from bot.utils.text import escape_html
    lines = ["<b>━━ Система ━━</b>", head]
    order = {"🔴": 0, "⚠️": 1, "✅": 2, "⚪": 3}
    for icon, title, detail in sorted(checks, key=lambda c: order.get(c[0], 9)):
        lines.append(f"{icon} <b>{escape_html(title)}</b>: {escape_html(detail)}")
    return lines
