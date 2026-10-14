"""Отчёт бота партнёра о том, какие платные функции у него РЕАЛЬНО работают.

Партнёрский бот после каждой проверки лицензии (раз в 6 часов) присылает на сервер
лицензий список действующих функций. Сервер показывает его в карточке лицензии рядом
с тем, что заложено в ключе, — видно, что работает, что отключено и что ждёт синхронизации.
"""
import logging
import re
from typing import Dict, Iterable, List, Optional, Set, Tuple

logger = logging.getLogger(__name__)

_RELEASE_RE = re.compile(r"^[0-9A-Za-z._-]{1,20}$")


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


async def send_report(license_key: str) -> bool:
    """Отправляет отчёт на сервер лицензий. Любые сбои глотаем — отчёт не критичен."""
    import aiohttp
    from bot.services.license import get_instance_id, get_license_server_url

    try:
        payload = dict(build_report(), license_key=license_key, instance_id=get_instance_id())
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

def record_report(license_key: str, instance_id: str, features: str, checked_at: str = "", release: str = "") -> bool:
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
        conn.commit()
        return cur.rowcount > 0


def latest_report(activations: Iterable[dict]) -> Optional[dict]:
    reported = [a for a in activations if a.get("reported_at")]
    return max(reported, key=lambda a: a["reported_at"]) if reported else None


# ---------------------------------- отображение ----------------------------------

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
