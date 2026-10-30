"""Аналитика для админа (лицензионная функция admin_insights, включается админом отдельно).

Один компактный экран: последние оплаты, брошенные корзины, пробники, истекающие ключи,
шлюзы, бэкап и инциденты. Данные берутся из уже существующих таблиц; журнал
инцидентов (admin_incidents) пополняется оповещениями о состоянии серверов.
"""
import logging
import os
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from bot.utils.text import escape_html

logger = logging.getLogger(__name__)

SETTING = "admin_insights_enabled"
_NON_REAL = "('trial', 'promo_free', 'demo', 'balance')"


def licensed() -> bool:
    try:
        from bot.services.license import is_feature_available
        return is_feature_available("admin_insights")
    except Exception:  # noqa: BLE001
        return False


def enabled() -> bool:
    from database.db_settings import get_setting
    return licensed() and str(get_setting(SETTING, "0") or "0") == "1"


def set_enabled(on: bool) -> None:
    from database.db_settings import set_setting
    set_setting(SETTING, "1" if on else "0")


# ------------------------------------------------------------------ вспомогательное

def _parse(ts: Any) -> Optional[datetime]:
    try:
        return datetime.strptime(str(ts)[:19], "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return None


def _fmt_ts(ts: Any) -> str:
    dt = _parse(ts)
    if not dt:
        return ""
    from bot.utils.datetime_format import get_display_tzinfo
    return dt.astimezone(get_display_tzinfo()).strftime("%d.%m %H:%M")


def _ago(ts: Any) -> str:
    dt = _parse(ts)
    if not dt:
        return "—"
    sec = max(0, int((datetime.now(timezone.utc) - dt).total_seconds()))
    if sec < 3600:
        return f"{max(1, sec // 60)} мин назад"
    if sec < 86400:
        return f"{sec // 3600} ч назад"
    return f"{sec // 86400} дн назад"


def _who(row: Dict[str, Any]) -> str:
    name = " ".join(x for x in (row.get("first_name"), row.get("last_name")) if x).strip()
    if name:
        return escape_html(name)
    if row.get("username"):
        return "@" + escape_html(row["username"])
    return f"ID {row.get('telegram_id') or row.get('user_id') or '?'}"


def _rub(cents: Any) -> str:
    v = (cents or 0) / 100
    return f"{v:g}".replace(".", ",") + " ₽"


# ------------------------------------------------------------------ данные

def recent_payments(limit: int = 5) -> List[Dict[str, Any]]:
    """Последние настоящие оплаты: из бота (с именем) и с сайта без Telegram."""
    from database.connection import get_db
    from bot.services.notifications import _format_payment_amount
    out: List[Dict[str, Any]] = []
    with get_db() as conn:
        rows = conn.execute(f"""
            SELECT p.paid_at, p.payment_type, p.final_amount_cents, p.final_amount_stars,
                   t.name AS tariff, u.first_name, u.last_name, u.username, u.telegram_id, p.user_id
            FROM payments p
            LEFT JOIN users u ON u.id = p.user_id
            LEFT JOIN tariffs t ON t.id = p.tariff_id
            WHERE p.status = 'paid' AND COALESCE(p.payment_type, '') NOT IN {_NON_REAL}
              AND (COALESCE(p.final_amount_cents, 0) > 0 OR COALESCE(p.final_amount_stars, 0) > 0)
            ORDER BY p.paid_at DESC, p.id DESC LIMIT ?""", (limit,)).fetchall()
        for r in rows:
            r = dict(r)
            out.append({"at": r["paid_at"], "who": _who(r), "amount": _format_payment_amount(r),
                        "tariff": r.get("tariff") or "—", "channel": "бот"})
        rows = conn.execute("""
            SELECT ap.created_at AS at, t.name AS tariff, t.price_rub AS price
            FROM anonymous_purchases ap LEFT JOIN tariffs t ON t.id = ap.tariff_id
            WHERE ap.status IN ('paid', 'claimed') AND COALESCE(ap.yookassa_payment_id, '') NOT IN ('', 'trial')
            ORDER BY ap.created_at DESC LIMIT ?""", (limit,)).fetchall()
        for r in rows:
            out.append({"at": r["at"], "who": "гость с сайта", "amount": f"{(r['price'] or 0):g} ₽".replace(".", ","),
                        "tariff": r["tariff"] or "—", "channel": "сайт"})
    out.sort(key=lambda x: str(x["at"]), reverse=True)
    return out[:limit]


def abandoned_carts(hours: int = 24) -> Dict[str, Any]:
    """Созданные, но не оплаченные заказы за сутки (без тех, кто потом всё же заплатил)."""
    from database.connection import get_db
    with get_db() as conn:
        row = conn.execute(f"""
            SELECT COUNT(DISTINCT p.user_id) AS people,
                   COALESCE(SUM(CASE WHEN p.payment_type NOT IN ('crypto', 'stars') THEN p.final_amount_cents ELSE 0 END), 0) AS cents
            FROM payments p
            WHERE p.status = 'pending' AND COALESCE(p.payment_type, '') NOT IN {_NON_REAL}
              AND COALESCE(p.final_amount_cents, 0) > 0
              AND p.paid_at >= datetime('now', ?)
              AND NOT EXISTS (SELECT 1 FROM payments q WHERE q.user_id = p.user_id AND q.status = 'paid'
                              AND q.paid_at >= p.paid_at AND COALESCE(q.payment_type, '') NOT IN {_NON_REAL})
        """, (f"-{int(hours)} hours",)).fetchone()
        site = conn.execute("""
            SELECT COUNT(*) AS n, COALESCE(SUM(t.price_rub), 0) AS rub
            FROM anonymous_purchases ap LEFT JOIN tariffs t ON t.id = ap.tariff_id
            WHERE ap.status = 'pending' AND ap.created_at >= datetime('now', ?)""", (f"-{int(hours)} hours",)).fetchone()
    return {"people": int(row["people"] or 0), "rub": (row["cents"] or 0) / 100,
            "site_n": int(site["n"] or 0), "site_rub": float(site["rub"] or 0)}


def trial_funnel(days: int = 7) -> Dict[str, int]:
    """Пробники за N дней: сколько взяли и сколько из них потом заплатили."""
    from database.connection import get_db
    with get_db() as conn:
        row = conn.execute(f"""
            SELECT COUNT(DISTINCT t.user_id) AS taken,
                   COUNT(DISTINCT CASE WHEN EXISTS (
                       SELECT 1 FROM payments q WHERE q.user_id = t.user_id AND q.status = 'paid'
                         AND q.paid_at >= t.paid_at AND COALESCE(q.payment_type, '') NOT IN {_NON_REAL}
                         AND (COALESCE(q.final_amount_cents, 0) > 0 OR COALESCE(q.final_amount_stars, 0) > 0)
                   ) THEN t.user_id END) AS bought
            FROM payments t
            WHERE t.payment_type = 'trial' AND t.status = 'paid' AND t.paid_at >= datetime('now', ?)
        """, (f"-{int(days)} days",)).fetchone()
        site = conn.execute("""
            SELECT COUNT(*) AS n FROM anonymous_purchases
            WHERE yookassa_payment_id = 'trial' AND created_at >= datetime('now', ?)""", (f"-{int(days)} days",)).fetchone()
    return {"taken": int(row["taken"] or 0), "bought": int(row["bought"] or 0), "site": int(site["n"] or 0)}


def expiring_keys(days: int = 3, limit: int = 100) -> List[Dict[str, Any]]:
    from database.connection import get_db
    with get_db() as conn:
        rows = conn.execute("""
            SELECT vk.expires_at, t.name AS tariff, u.first_name, u.last_name, u.username, u.telegram_id, vk.user_id,
                   CAST((julianday(vk.expires_at) - julianday('now')) * 24 AS INTEGER) AS hours_left
            FROM vpn_keys vk JOIN users u ON u.id = vk.user_id LEFT JOIN tariffs t ON t.id = vk.tariff_id
            WHERE vk.expires_at > datetime('now') AND vk.expires_at <= datetime('now', ?) AND u.is_banned = 0
            ORDER BY vk.expires_at LIMIT ?""", (f"+{int(days)} days", limit)).fetchall()
    return [dict(r) for r in rows]


def gateway_activity() -> List[Dict[str, Any]]:
    """По каждому способу оплаты — время последней настоящей оплаты."""
    from database.connection import get_db
    from bot.services.notifications import _payment_type_label
    with get_db() as conn:
        rows = conn.execute(f"""
            SELECT payment_type, MAX(paid_at) AS last FROM payments
            WHERE status = 'paid' AND COALESCE(payment_type, '') NOT IN {_NON_REAL}
              AND (COALESCE(final_amount_cents, 0) > 0 OR COALESCE(final_amount_stars, 0) > 0)
            GROUP BY payment_type ORDER BY last DESC""").fetchall()
    return [{"label": _payment_type_label(r["payment_type"]), "last": r["last"]} for r in rows]


def last_backup() -> Optional[Dict[str, Any]]:
    """Самая свежая копия базы бота (backup/ГГГГ-ММ-ДД/vpn_bot.db и ручные *vpn_bot.db) по всем подпапкам."""
    from database.db_backup import BACKUP_DIR
    best = None
    try:
        for root, _dirs, files in os.walk(BACKUP_DIR):
            for name in files:
                if name.endswith("vpn_bot.db"):
                    path = os.path.join(root, name)
                    mtime = os.path.getmtime(path)
                    if best is None or mtime > best[0]:
                        best = (mtime, os.path.getsize(path))
    except OSError:
        return None
    if best is None:
        return None
    return {"age_h": (time.time() - best[0]) / 3600, "bytes": best[1]}


def _size(n: int) -> str:
    return f"{n / 1048576:.1f} МБ" if n >= 1048576 else f"{max(1, n // 1024)} КБ"


def record_events(events: List[Any]) -> None:
    """Пишет события оповещений о состоянии (down/recovered) в журнал инцидентов."""
    try:
        from database.connection import get_db
        with get_db() as conn:
            for kind, title, detail in events:
                conn.execute("INSERT INTO admin_incidents (kind, title, detail) VALUES (?, ?, ?)",
                             (str(kind), str(title)[:120], str(detail)[:200]))
            conn.execute("DELETE FROM admin_incidents WHERE id NOT IN (SELECT id FROM admin_incidents ORDER BY id DESC LIMIT 200)")
    except Exception as e:  # noqa: BLE001
        logger.debug("admin_incidents: %s", e)


def recent_incidents(limit: int = 3) -> List[Dict[str, Any]]:
    from database.connection import get_db
    with get_db() as conn:
        return [dict(r) for r in conn.execute(
            "SELECT at, kind, title FROM admin_incidents ORDER BY id DESC LIMIT ?", (limit,)).fetchall()]


# ------------------------------------------------------------------ текст

def build_text() -> str:
    lines = ["📊 <b>Аналитика</b>"]

    pays = recent_payments(5)
    body = "\n".join(f"{_fmt_ts(p['at'])} · {p['who']} · {p['amount']}" for p in pays) or "оплат пока не было"
    lines.append(f"<blockquote>💳 <b>Последние оплаты</b>\n{body}</blockquote>")

    cart, tr = abandoned_carts(24), trial_funnel(7)
    people, n_site = cart["people"], cart["site_n"]
    cart_txt = "нет" if not (people or n_site) else (
        f"{people + n_site} · ≈{cart['rub'] + cart['site_rub']:g} ₽".replace(".", ",")
        + (f" (сайт: {n_site})" if n_site else ""))
    trial_txt = f"взяли {tr['taken']}" + (f" (+{tr['site']} с сайта)" if tr["site"] else "")
    if tr["taken"]:
        trial_txt += f" · купили {tr['bought']} ({round(100 * tr['bought'] / tr['taken'])}%)"
    lines.append(f"<blockquote>🛒 <b>Не оплатили за 24 ч:</b> {cart_txt}\n🎁 <b>Пробники за 7 дн:</b> {trial_txt}</blockquote>")

    exp = expiring_keys(3)
    if exp:
        shown = [f"{_who(e)} · {e['hours_left'] // 24} дн {e['hours_left'] % 24} ч" for e in exp[:4]]
        more = f"\n… ещё {len(exp) - 4}" if len(exp) > 4 else ""
        lines.append(f"<blockquote>⏳ <b>Истекают за 3 дня: {len(exp)}</b>\n" + "\n".join(shown) + more + "</blockquote>")
    else:
        lines.append("<blockquote>⏳ <b>Истекают за 3 дня:</b> никто</blockquote>")

    gates = gateway_activity()
    gate_txt = " · ".join(f"{g['label']} {_ago(g['last'])}" for g in gates[:4]) or "нет данных"
    bk = last_backup()
    if not bk:
        bk_txt = "⚠️ файлов нет"
    else:
        bk_txt = f"{'⚠️ ' if bk['age_h'] > 36 else ''}{int(bk['age_h'])} ч назад · {_size(bk['bytes'])}"
    inc = recent_incidents(3)
    inc_txt = "\n".join(
        f"{'🟢' if i['kind'] == 'recovered' else '🔴'} {_fmt_ts(i['at'])} {escape_html(i['title'])}" for i in inc) or "нет"
    lines.append(f"<blockquote expandable>🚦 <b>Шлюзы (последняя оплата):</b> {escape_html(gate_txt)}\n"
                 f"🗄 <b>Бэкап базы:</b> {bk_txt}\n🧯 <b>Инциденты:</b>\n{inc_txt}</blockquote>")
    return "\n".join(lines)


def payments_list_text(limit: int = 15) -> str:
    pays = recent_payments(limit)
    if not pays:
        return "💳 <b>Последние оплаты</b>\n\nОплат пока не было."
    rows = [f"{_fmt_ts(p['at'])} · {p['who']} · {p['amount']}\n    {escape_html(p['tariff'])} · {p['channel']}" for p in pays]
    return "💳 <b>Последние оплаты</b>\n<blockquote expandable>" + "\n".join(rows) + "</blockquote>"


def expiring_list_text(limit: int = 20) -> str:
    exp = expiring_keys(3, limit)
    if not exp:
        return "⏳ <b>Истекают за 3 дня</b>\n\nНикто."
    rows = [f"{_who(e)} · {e['hours_left'] // 24} дн {e['hours_left'] % 24} ч\n    {escape_html(e.get('tariff') or '—')}" for e in exp]
    return f"⏳ <b>Истекают за 3 дня: {len(exp)}</b>\n<blockquote expandable>" + "\n".join(rows) + "</blockquote>"
