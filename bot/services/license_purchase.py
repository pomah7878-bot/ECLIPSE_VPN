"""Выдача whitelabel-лицензии после оплаты.

Общая логика для кнопки «Я оплатил» и фоновой автопроверки брошенных оплат.
Ключ выдаётся ровно один раз на заказ (атомарный захват), только владельцу
заказа и по условиям тарифа на момент создания оплаты.
"""
import logging
from typing import Any, Dict, Optional, Tuple

logger = logging.getLogger(__name__)


def mask_license_key(key: Optional[str]) -> str:
    """ECLW-ABCD-EFGH-IJKL -> ECLW-ABCD-****-**** (для логов)."""
    return (key[:9] + "-****-****") if key else ""


def build_issued_text(info: Dict[str, Any]) -> str:
    from bot.services.license import GATED_FEATURES, features_from_str

    enabled = features_from_str(info.get("features"))
    if enabled:
        features_text = ", ".join(GATED_FEATURES[k] for k in GATED_FEATURES if k in enabled)
    else:
        features_text = "нет платных функций"
    return (
        "🎉 <b>Оплата прошла успешно!</b>\n\n"
        f"Ваша лицензия включает: {features_text}\n\n"
        "Ключ лицензии — введите его в своём боте: «💳 Моя лицензия» → «🔑 Ввести код лицензии» "
        "(или вставьте в secrets.env как <code>LICENSE_KEY</code>):\n"
        f"<code>{info['license_key']}</code>\n\n"
        "Сохраните этот ключ — он понадобится при настройке и продлении."
    )


async def issue_license_for_order(
    order_id: str,
    expected_telegram_id: Optional[int] = None,
    partner_name: Optional[str] = None,
) -> Tuple[str, Optional[Dict[str, Any]]]:
    """Возвращает (статус, данные):
    not_found — заказа нет или он принадлежит другому пользователю;
    already   — ключ уже выдан (данные: заказ с license_key);
    unpaid    — оплата ещё не поступила;
    canceled  — платёж отменён;
    busy      — выдача идёт прямо сейчас в другом вызове;
    error     — не удалось проверить или выдать (можно повторить);
    issued    — ключ выдан именно этим вызовом (данные: license_key, features...).
    """
    from database.db_licenses import (
        get_license_purchase_by_order_id, get_license_tariff_by_id, claim_license_purchase,
        release_license_purchase_claim, complete_license_purchase, mark_license_purchase_status,
        create_partner_license,
    )
    from bot.services.billing import check_yookassa_payment_status

    purchase = get_license_purchase_by_order_id(order_id)
    if not purchase:
        return "not_found", None
    if expected_telegram_id is not None and purchase["telegram_id"] != expected_telegram_id:
        return "not_found", None
    if purchase["status"] == "paid":
        return "already", purchase
    if purchase["status"] in ("canceled", "expired"):
        return "canceled", purchase
    if not purchase.get("yookassa_payment_id"):
        return "unpaid", purchase

    try:
        status = await check_yookassa_payment_status(purchase["yookassa_payment_id"], order_id=order_id)
    except Exception as e:
        logger.error(f"Ошибка проверки оплаты лицензии {order_id}: {e}")
        return "error", purchase

    if status == "canceled":
        mark_license_purchase_status(order_id, "canceled")
        return "canceled", purchase
    if status != "succeeded":
        return "unpaid", purchase

    # Платёж подтверждён. Захватываем заказ атомарно: ключ получит только один вызов.
    if not claim_license_purchase(order_id):
        fresh = get_license_purchase_by_order_id(order_id)
        if fresh and fresh["status"] == "paid":
            return "already", fresh
        return "busy", fresh

    try:
        if purchase.get("price_rub") is not None:
            features = purchase.get("features") or ""
            duration_days = purchase.get("duration_days")
            tariff_name = ""
        else:
            # Заказ создан до версии 1.165 — снимка условий нет, берём текущий тариф
            tariff = get_license_tariff_by_id(purchase["license_tariff_id"])
            if not tariff:
                raise RuntimeError("тариф лицензии не найден")
            features = tariff.get("features") or ""
            duration_days = tariff["duration_days"]
            tariff_name = tariff.get("name") or ""
        name = partner_name or f"user_{purchase['telegram_id']}"
        license_key = create_partner_license(
            partner_name=name,
            features=features,
            duration_days=duration_days,
            notes=f"Куплено через бота, order_id={order_id}, telegram_id={purchase['telegram_id']}",
        )
        complete_license_purchase(order_id, license_key)
    except Exception as e:
        logger.error(f"Не удалось выдать лицензию по заказу {order_id}: {e}")
        release_license_purchase_claim(order_id)
        return "error", purchase

    logger.info(f"Лицензия выдана: order_id={order_id}, key={mask_license_key(license_key)}")
    return "issued", {
        "license_key": license_key, "features": features,
        "duration_days": duration_days, "tariff_name": tariff_name,
    }


async def process_abandoned_license_purchases(bot) -> None:
    """Автовыдача ключей тем, кто оплатил и не нажал «Я оплатил»."""
    from database.db_licenses import get_abandoned_license_purchases

    for purchase in get_abandoned_license_purchases(older_than_minutes=2):
        status, info = await issue_license_for_order(purchase["order_id"])
        if status == "issued":
            try:
                await bot.send_message(purchase["telegram_id"], build_issued_text(info), parse_mode="HTML")
            except Exception as e:
                logger.warning(
                    f"Ключ по заказу {purchase['order_id']} выдан, но сообщение покупателю не доставлено: {e}"
                )


async def process_license_expiry_reminders(bot) -> None:
    """Предупреждает покупателя лицензии за 7 и за 1 день до окончания."""
    from datetime import datetime
    from database.db_licenses import get_expiring_licenses_with_buyers, has_license_event, log_license_event

    for lic in get_expiring_licenses_with_buyers(7):
        try:
            expires = datetime.strptime(lic["expires_at"], "%Y-%m-%d %H:%M:%S")
        except (TypeError, ValueError):
            continue
        days_left = max(0, (expires - datetime.utcnow()).days)
        is_trial = bool(lic.get("is_trial"))
        if is_trial and days_left > 1:
            continue  # пробным — только последнее напоминание, за сутки
        threshold = 1 if days_left <= 1 else 7
        event = f"reminder_{threshold}d:{lic['expires_at']}"
        if has_license_event(lic["license_key"], event):
            continue
        log_license_event(lic["license_key"], event)
        if is_trial:
            text = (
                f"⏰ <b>Пробный период заканчивается {expires.strftime('%d.%m.%Y')}</b>\n\n"
                "Чтобы платные функции не отключились, выберите тариф командой /buy_license "
                "и введите новый код в своём боте: «💳 Моя лицензия» → «🔑 Ввести код лицензии»."
            )
        else:
            text = (
                f"⏰ <b>Лицензия заканчивается {expires.strftime('%d.%m.%Y')}</b>\n\n"
                "Чтобы платные функции не отключились, купите продление командой /buy_license "
                "и введите новый код в своём боте: «💳 Моя лицензия» → «🔑 Ввести код лицензии». "
                "Новый код заменит старый."
            )
        try:
            await bot.send_message(lic["telegram_id"], text, parse_mode="HTML")
        except Exception as e:
            logger.warning(f"Напоминание о лицензии {mask_license_key(lic['license_key'])} не доставлено: {e}")
