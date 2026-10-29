"""Купон для вернувшихся: клиентам, у которых подписка закончилась неделю назад и они не вернулись,
раз в сутки отправляется личный купон со скидкой (скидка и срок — как в «Авто выдаче при покупке»).

Выключено по умолчанию (настройка lapsed_coupon_enabled). Один купон на одну «потерю» клиента:
повторов нет, пока клиент не купит и снова не уйдёт. За один запуск не больше MAX_PER_RUN сообщений.
"""
import asyncio
import logging

from aiogram import Bot
from aiogram.utils.keyboard import InlineKeyboardBuilder
from aiogram.types import InlineKeyboardButton

logger = logging.getLogger(__name__)

KEY_ENABLED = "lapsed_coupon_enabled"
AFTER_DAYS = 7      # через сколько дней после окончания подписки писать
WINDOW_DAYS = 60    # не писать тем, кто ушёл давно
MAX_PER_RUN = 50


def is_enabled() -> bool:
    from database.requests import get_setting
    return get_setting(KEY_ENABLED, "0") == "1"


def set_enabled(value: bool) -> None:
    from database.requests import set_setting
    set_setting(KEY_ENABLED, "1" if value else "0")


def find_candidates(limit: int = MAX_PER_RUN) -> list:
    """Клиенты, чей последний ключ закончился AFTER_DAYS..WINDOW_DAYS дней назад и кому ещё не писали."""
    from database.connection import get_db
    with get_db() as conn:
        rows = conn.execute(
            """
            SELECT u.id AS user_id, u.telegram_id, MAX(k.expires_at) AS last_exp
            FROM users u JOIN vpn_keys k ON k.user_id = u.id
            WHERE COALESCE(u.is_banned, 0) = 0 AND COALESCE(u.is_bot_blocked, 0) = 0
            GROUP BY u.id
            HAVING last_exp <= datetime('now', ?) AND last_exp > datetime('now', ?)
            ORDER BY last_exp DESC
            """,
            (f"-{AFTER_DAYS} days", f"-{WINDOW_DAYS} days"),
        ).fetchall()
        out = []
        for r in rows:
            source = f"lapsed:{r['user_id']}:{str(r['last_exp'])[:10]}"
            if conn.execute("SELECT 1 FROM promo_codes WHERE source = ? LIMIT 1", (source,)).fetchone():
                continue
            out.append({"user_id": r["user_id"], "telegram_id": r["telegram_id"], "source": source})
            if len(out) >= limit:
                break
    return out


def issue_coupon(user_id: int, source: str):
    from database.db_promotions import (
        create_coupon_batch, get_coupon_auto_discount_percent, get_coupon_auto_lifetime_days,
    )
    coupons = create_coupon_batch(
        discount_percent=get_coupon_auto_discount_percent(),
        lifetime_days=get_coupon_auto_lifetime_days(),
        count=1,
        source=source,
        issued_to_user_id=user_id,
    )
    return coupons[0] if coupons else None


async def process_lapsed_coupons(bot: Bot) -> int:
    """Ежедневный шаг. Возвращает число отправленных купонов; ошибки не пробрасывает."""
    try:
        if not is_enabled():
            return 0
        from bot.services.promotions import format_auto_coupon_text
        from bot.utils.delivery import is_bot_blocked_error
        from database.requests import mark_user_bot_blocked
        sent = 0
        for c in await asyncio.to_thread(find_candidates):
            coupon = await asyncio.to_thread(issue_coupon, c["user_id"], c["source"])
            if not coupon:
                continue
            kb = InlineKeyboardBuilder()
            kb.row(InlineKeyboardButton(text="🔑 Мои ключи", callback_data="my_keys"))
            kb.row(InlineKeyboardButton(text="🈴 На главную", callback_data="start"))
            text = ("👋 <b>Мы скучаем</b>\n\nВаша подписка закончилась. Возвращайтесь: "
                    "дарим скидку на продление." + format_auto_coupon_text(coupon))
            try:
                await bot.send_message(c["telegram_id"], text, reply_markup=kb.as_markup(), parse_mode="HTML")
                sent += 1
            except Exception as e:  # noqa: BLE001
                if is_bot_blocked_error(e):
                    mark_user_bot_blocked(c["telegram_id"])
                else:
                    logger.warning("Купон для вернувшихся не отправлен %s: %s", c["telegram_id"], e)
            await asyncio.sleep(0.3)
        if sent:
            logger.info("🎫 Отправлено купонов вернувшимся: %s", sent)
        return sent
    except Exception as e:  # noqa: BLE001
        logger.error("process_lapsed_coupons: %s", e)
        return 0
