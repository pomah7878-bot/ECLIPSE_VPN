"""Отчёт бота партнёра о том, какие платные функции у него РЕАЛЬНО работают.

Партнёрский бот после каждой проверки лицензии (раз в 6 часов) присылает на сервер
лицензий список действующих функций. Сервер показывает его в карточке лицензии рядом
с тем, что заложено в ключе, — видно, что работает, что отключено и что ждёт синхронизации.
"""
import logging
import re
from typing import Any, Dict, Iterable, List, Optional, Set, Tuple

logger = logging.getLogger(__name__)

_RELEASE_RE = re.compile(r"^[0-9A-Za-z._-]{1,20}$")
_BOT_NAME_RE = re.compile(r"^[A-Za-z0-9_]{5,32}$")
# проверка лицензии идёт раз в 6 часов: сутки тишины — уже не сбой связи
LIVE_HOURS = 24


# ---------------------------- сторона партнёрского бота ----------------------------

def build_report() -> Dict[str, str]:
    """Что действует у этого бота прямо сейчас (с учётом срока и льготного периода)."""
    from bot.services.license import get_enabled_features, features_to_str
    from database.requests import get_setting

    try:
        from bot.version import BOT_RELEASE
    except Exception:
        BOT_RELEASE = ""
    return {
        "features": features_to_str(get_enabled_features()),
        "checked_at": get_setting("license_checked_at", "") or "",
        "release": str(BOT_RELEASE or ""),
    }


async def build_install_info() -> Dict[str, Any]:
    """Чей это бот и живой ли он: имя, владелец, число клиентов и действующих ключей."""
    from database.connection import get_db

    info: Dict[str, Any] = {}
    try:
        from bot.utils.runtime_state import get_bot_instance
        bot = get_bot_instance()
        if bot:
            me = await bot.get_me()
            info.update(bot_username=me.username or "", bot_name=me.full_name or "")
    except Exception as e:
        logger.debug("Имя бота для отчёта не получено: %s", e)
    try:
        from config import ADMIN_IDS
        with get_db() as conn:
            info["clients"] = conn.execute("SELECT COUNT(*) FROM users WHERE telegram_id > 0").fetchone()[0]
            info["active_keys"] = conn.execute(
                "SELECT COUNT(*) FROM vpn_keys WHERE expires_at > datetime('now')"
            ).fetchone()[0]
            if ADMIN_IDS:
                info["owner_id"] = int(ADMIN_IDS[0])
                row = conn.execute(
                    "SELECT username, first_name FROM users WHERE telegram_id = ?", (int(ADMIN_IDS[0]),)
                ).fetchone()
                if row:
                    info["owner_name"] = row["username"] or row["first_name"] or ""
    except Exception as e:
        logger.debug("Сведения об установке для отчёта не собраны: %s", e)
    return info


async def send_report(license_key: str) -> bool:
    """Отправляет отчёт на сервер лицензий. Любые сбои глотаем — отчёт не критичен."""
    import aiohttp
    from bot.services.license import get_instance_id, get_license_server_url

    try:
        payload = dict(build_report(), license_key=license_key, instance_id=get_instance_id())
        payload.update(await build_install_info())
        async with aiohttp.ClientSession() as session:
            async with session.post(
                get_license_server_url() + "/api/license/report",
                json=payload,
                timeout=aiohttp.ClientTimeout(total=15),
            ) as resp:
                return resp.status == 200
    except Exception as e:
        logger.debug("Отчёт о функциях лицензии не отправлен: %s", e)
        return False


# ------------------------------- сторона сервера лицензий -------------------------------

def _count(value) -> Optional[int]:
    try:
        n = int(value)
    except (TypeError, ValueError):
        return None
    return n if n >= 0 else None


def clean_install_info(data: dict) -> Dict[str, Any]:
    """Данные приходят от чужого сервера: оставляем только то, что похоже на настоящее."""
    username = str(data.get("bot_username") or "").lstrip("@")
    return {
        "bot_username": username if _BOT_NAME_RE.match(username) else None,
        "bot_name": str(data.get("bot_name") or "")[:64] or None,
        "owner_id": _count(data.get("owner_id")),
        "owner_name": str(data.get("owner_name") or "")[:64] or None,
        "clients": _count(data.get("clients")),
        "active_keys": _count(data.get("active_keys")),
    }


def record_report(license_key: str, instance_id: str, features: str, checked_at: str = "", release: str = "",
                  info: Optional[dict] = None) -> bool:
    """Сохраняет отчёт установки. Только для уже известных серверу установок."""
    from bot.services.license import features_from_str, features_to_str
    from database.connection import get_db

    key = (license_key or "").strip().upper()
    clean = features_to_str(features_from_str(features))
    release = release if _RELEASE_RE.match(release or "") else ""
    checked_at = (checked_at or "")[:40]
    with get_db() as conn:
        cur = conn.execute(
            "UPDATE license_activations SET reported_features = ?, reported_at = CURRENT_TIMESTAMP, "
            "reported_checked_at = ?, reported_release = ? "
            "WHERE license_key = ? AND instance_id = ? AND instance_id NOT LIKE 'legacy-%'",
            (clean, checked_at, release, key, instance_id),
        )
        known = cur.rowcount > 0
        # старые версии бота этих полей не шлют: прошлые сведения не затираем
        fresh = {k: v for k, v in clean_install_info(info or {}).items() if v is not None}
        if known and fresh:
            conn.execute(
                f"UPDATE license_activations SET {', '.join(f'{k} = ?' for k in fresh)} "
                "WHERE license_key = ? AND instance_id = ?",
                (*fresh.values(), key, instance_id),
            )
        conn.commit()
        return known


def latest_report(activations: Iterable[dict]) -> Optional[dict]:
    reported = [a for a in activations if a.get("reported_at")]
    return max(reported, key=lambda a: a["reported_at"]) if reported else None


# ---------------------------------- отображение ----------------------------------

def _install(activations: Iterable[dict]) -> Optional[dict]:
    """Самая свежая настоящая установка лицензии."""
    real = [a for a in activations if not str(a.get("instance_id")).startswith("legacy-")]
    return max(real, key=lambda a: a.get("last_seen") or "") if real else None


def _is_live(install: dict) -> bool:
    from datetime import datetime, timedelta
    try:
        seen = datetime.fromisoformat(str(install.get("last_seen")))
    except ValueError:
        return False
    return datetime.utcnow() - seen < timedelta(hours=LIVE_HOURS)


def install_mark(activations: Iterable[dict]) -> str:
    """🟢 работает, 🔴 молчит больше суток, ⚪ ключ никто не поставил."""
    install = _install(activations)
    if not install:
        return "⚪"
    return "🟢" if _is_live(install) else "🔴"


def license_list_title(lic: dict, activations: List[dict]) -> str:
    """Подпись в списке лицензий: бот партнёра понятнее, чем tg123456."""
    install = _install(activations) or {}
    name = f"@{install['bot_username']}" if install.get("bot_username") else str(lic.get("partner_name") or "")
    if str(lic.get("partner_name") or "").endswith("(пробный)") and install.get("bot_username"):
        name += " (пробный)"
    clients = f" · {install['clients']} кл." if install.get("clients") is not None else ""
    return f"{install_mark(activations)} {name}{clients}"


def format_install_block(lic: dict, activations: List[dict]) -> str:
    """Кто поставил лицензию и работает ли установка сейчас."""
    from html import escape

    install = _install(activations)
    if not install:
        return "⚪ <b>Не установлена:</b> ключ ещё не вводили ни в одном боте"
    state = "🟢 <b>Работает</b>" if _is_live(install) else "🔴 <b>Молчит</b>"
    lines = [f"{state}, последняя связь {escape(str(install.get('last_seen')))} UTC"]
    if install.get("bot_username"):
        title = f" ({escape(str(install['bot_name']))})" if install.get("bot_name") else ""
        lines.append(f"🤖 Бот: @{escape(str(install['bot_username']))}{title}")
    else:
        lines.append("🤖 Бот: нет данных, партнёру нужно обновить бота")
    match = re.match(r"tg(\d+)", str(lic.get("partner_name") or ""))
    owner_id = install.get("owner_id") or (int(match.group(1)) if match else None)
    if owner_id:
        name = str(install.get("owner_name") or owner_id)
        lines.append(f'👤 Владелец: <a href="tg://user?id={int(owner_id)}">{escape(name)}</a> · <code>{int(owner_id)}</code>')
    if install.get("clients") is not None:
        lines.append(f"👥 Клиентов: {install['clients']} · действующих ключей: {install.get('active_keys') or 0}")
    return "\n".join(lines)


def feature_status_lines(license_features: Set[str], license_active: bool,
                         report_features: Optional[Set[str]]) -> List[Tuple[str, str, str]]:
    """[(функция, значок, пояснение)] — что работает у партнёра на самом деле."""
    from bot.services.license import GATED_FEATURES

    rows: List[Tuple[str, str, str]] = []
    for f in GATED_FEATURES:
        in_license = f in license_features
        if report_features is None:
            if in_license:
                rows.append((f, "❔", "нет данных от бота партнёра"))
            continue
        working = f in report_features
        if in_license and license_active:
            rows.append((f, "✅", "работает") if working else (f, "⏳", "включится после синхронизации (до 6 ч)"))
        elif in_license and not license_active:
            rows.append((f, "🕒", "ещё работает, отключится после синхронизации") if working else (f, "⛔", "отключена"))
        elif working:
            rows.append((f, "🕒", "ещё работает, убрана из лицензии — отключится после синхронизации"))
    return rows


def format_feature_block(license_features: Set[str], license_active: bool, activations: List[dict]) -> str:
    from bot.services.license import GATED_FEATURES, features_from_str

    rep = latest_report(activations)
    report_set = features_from_str(rep.get("reported_features")) if rep else None
    rows = feature_status_lines(license_features, license_active, report_set)
    if not rows and report_set is None:
        return "Функции: (нет платных функций)"
    if rep:
        head = (f"Что реально работает у партнёра (отчёт бота {rep['reported_at']} UTC, "
                f"установка {str(rep['instance_id'])[:8]}…"
                + (f", версия {rep['reported_release']}" if rep.get("reported_release") else "") + "):")
    else:
        head = ("Функции по лицензии (отчёта от бота партнёра пока нет — придёт после обновления его бота, "
                "затем каждые 6 часов):")
    body = "\n".join(f"  {mark} {GATED_FEATURES[f]} — {note}" for f, mark, note in rows) or "  (платных функций нет)"
    return f"{head}\n{body}"
