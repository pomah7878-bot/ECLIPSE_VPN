"""Личный кабинет клиента в виде rich-сообщения (лицензионная функция client_cabinet, включается админом отдельно).

Таблица «Параметр / Значение», полоса прогресса трафика и цветные кнопки. Если Telegram не принял
rich-сообщение, обработчик показывает тот же кабинет обычным текстом (см. bot/handlers/user/cabinet.py).
"""
import html
import logging
from datetime import datetime, timezone
from typing import Any, Dict, Optional

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

logger = logging.getLogger(__name__)

SETTING = "client_cabinet_enabled"
BAR_WIDTH = 10


def licensed() -> bool:
    try:
        from bot.services.license import is_feature_available
        return is_feature_available("client_cabinet")
    except Exception:  # noqa: BLE001
        return False


def enabled() -> bool:
    from database.db_settings import get_setting
    return licensed() and str(get_setting(SETTING, "0") or "0") == "1"


def set_enabled(on: bool) -> None:
    from database.db_settings import set_setting
    set_setting(SETTING, "1" if on else "0")


def bar(used: float, limit: float, width: int = BAR_WIDTH) -> str:
    """▓▓▓░░░░░░░ 30% (без лимита — пустая строка)."""
    if not limit or limit <= 0:
        return ""
    share = max(0.0, min(1.0, used / limit))
    full = round(share * width)
    return "▓" * full + "░" * (width - full) + f" {round(share * 100)}%"


def _days_left(expires_at: Any) -> Optional[int]:
    try:
        exp = datetime.strptime(str(expires_at)[:19], "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return None
    sec = (exp - datetime.now(timezone.utc)).total_seconds()
    return max(0, int(-(-sec // 86400)))


def collect(telegram_id: int) -> Dict[str, Any]:
    from database.connection import get_db
    from database.requests import get_user_balance, get_user_internal_id, get_user_keys_for_display
    from bot.services.vpn_api import format_traffic
    from bot.utils.ru_format import format_rub

    uid = get_user_internal_id(telegram_id)
    keys = get_user_keys_for_display(telegram_id) or []
    active = [k for k in keys if k.get("is_active")]
    nearest = min(active, key=lambda k: str(k.get("expires_at"))) if active else None

    traffic = traffic_bar = ""
    if nearest:
        used, limit = nearest.get("traffic_used") or 0, nearest.get("traffic_limit") or 0
        traffic = f"{format_traffic(used)} / {format_traffic(limit) if limit > 0 else '∞'}"
        traffic_bar = bar(used, limit)

    paid30 = refs = 0
    if uid:
        with get_db() as conn:
            row = conn.execute("""
                SELECT COALESCE(SUM(final_amount_cents), 0) AS c FROM payments
                WHERE user_id = ? AND status = 'paid' AND paid_at >= datetime('now', '-30 days')
                  AND COALESCE(payment_type, '') NOT IN ('trial', 'promo_free', 'demo', 'balance', 'crypto', 'stars')
            """, (uid,)).fetchone()
            paid30 = (row["c"] or 0) / 100
            refs = conn.execute("SELECT COUNT(*) AS n FROM users WHERE referred_by = ?", (uid,)).fetchone()["n"]

    return {
        "balance": format_rub(get_user_balance(uid) if uid else 0, cents=True),
        "keys_total": len(keys), "keys_active": len(active),
        "days_left": _days_left(nearest.get("expires_at")) if nearest else None,
        "traffic": traffic, "traffic_bar": traffic_bar,
        "paid30": f"{paid30:g}".replace(".", ",") + " ₽", "refs": int(refs or 0),
    }


def _rows(d: Dict[str, Any]) -> list:
    rows = [("💰 Баланс", d["balance"]),
            ("🔑 Ключи", f"{d['keys_active']} активных из {d['keys_total']}")]
    if d["days_left"] is not None:
        rows.append(("⏳ Ближайшее окончание", f"через {d['days_left']} дн"))
    if d["traffic"]:
        rows.append(("📶 Трафик", d["traffic"] + (f"  {d['traffic_bar']}" if d["traffic_bar"] else "")))
    rows += [("💳 Оплачено за 30 дней", d["paid30"]), ("🤝 Приглашено", str(d["refs"]))]
    return rows


def build_html(d: Dict[str, Any]) -> str:
    from bot.utils.rich_view import table
    return table("👤 Личный кабинет", _rows(d))


def build_fallback(d: Dict[str, Any]) -> str:
    lines = "\n".join(f"{html.escape(k)}: <b>{html.escape(v)}</b>" for k, v in _rows(d))
    return f"👤 <b>Личный кабинет</b>\n<blockquote>{lines}</blockquote>"


def keyboard() -> InlineKeyboardMarkup:
    from bot.keyboards.admin_misc import back_button, home_button
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔑 Мои ключи", callback_data="my_keys", style="primary"),
         InlineKeyboardButton(text="💳 Пополнить", callback_data="balance_topup_menu", style="success")],
        [InlineKeyboardButton(text="🛒 Тарифы", callback_data="buy_key"),
         InlineKeyboardButton(text="🤝 Рефералы", callback_data="referral_system")],
        [back_button("start"), home_button()],
    ])
