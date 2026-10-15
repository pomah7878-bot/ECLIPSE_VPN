"""Промокоды и совместные акции на сайте (/shop) и в WebApp.

Сайт не знает про пользователя бота, поэтому промокод хранится в cookie
браузера (ссылка /shop?promo=КОД или ввод кода), а цена считается на сервере:
клиент видит ровно ту сумму, которая уйдёт в платёж.

Скидка на сайте действует на покупку НОВОГО ключа (оплата картой/СБП или
балансом). Условия берутся из самого промокода: процент, срок действия, лимит
активаций, а для совместной акции — ещё и минимальный срок тарифа.
"""
import logging
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

SITE_PROMO_COOKIE = "site_promo_code"
SITE_PROMO_MAX_AGE = 60 * 60 * 24 * 30

_REASONS = {
    "not_found": "Промокод не найден.",
    "inactive": "Промокод выключен.",
    "expired": "Срок действия промокода истёк.",
    "exhausted": "Лимит применений промокода уже исчерпан.",
    "coupon": "Этот купон можно применить только в боте.",
}


def _clean(code: Optional[str]) -> str:
    return (code or "").strip()[:64]


def validate_code(code: Optional[str]) -> Dict[str, Any]:
    """Проверяет код без привязки к тарифу: {ok, promo, reason}."""
    code = _clean(code)
    if not code:
        return {"ok": False, "promo": None, "reason": "Введите промокод."}
    try:
        from database.db_promotions import get_promo_code_availability
        avail = get_promo_code_availability(code)
    except Exception as e:
        logger.warning("site promo availability failed: %s", e)
        return {"ok": False, "promo": None, "reason": "Промокод сейчас недоступен."}
    promo = avail.get("promo")
    if not avail.get("ok"):
        return {"ok": False, "promo": None, "reason": _REASONS.get(avail.get("reason"), "Промокод недоступен.")}
    if promo.get("type") == "coupon":
        return {"ok": False, "promo": None, "reason": _REASONS["coupon"]}
    if int(promo.get("discount_percent") or 0) <= 0:
        return {"ok": False, "promo": None, "reason": "Промокод недоступен."}
    # Покупки с сайта идут в общий лимит активаций
    limit = promo.get("activation_limit")
    if limit:
        try:
            from database.db_collabs import site_purchase_count
            from database.db_promotions import _usage_count_for_limit
            from database.connection import get_db
            with get_db() as conn:
                used = _usage_count_for_limit(conn, int(promo["id"])) + site_purchase_count(int(promo["id"]))
            if used >= int(limit):
                return {"ok": False, "promo": None, "reason": _REASONS["exhausted"]}
        except Exception as e:
            logger.warning("site promo limit check failed: %s", e)
    return {"ok": True, "promo": promo, "reason": None}


def quote(code: Optional[str], tariff: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """Цена тарифа с промокодом (в копейках).

    Всегда возвращает original_cents/final_cents; applied=True, если скидка
    действует на этот тариф. reason объясняет, почему не действует.
    """
    original = int(round(float((tariff or {}).get("price_rub") or 0) * 100))
    result = {
        "applied": False, "promo": None, "reason": None,
        "original_cents": original, "discount_cents": 0, "final_cents": original, "percent": 0,
    }
    if not tariff or original <= 0 or not _clean(code):
        return result
    checked = validate_code(code)
    if not checked["ok"]:
        result["reason"] = checked["reason"]
        return result
    promo = checked["promo"]
    try:
        from database.db_collabs import tariff_blocked_reason
        blocked = tariff_blocked_reason(promo, tariff)
    except Exception:
        blocked = None
    if blocked:
        result["reason"] = blocked
        result["promo"] = promo
        return result
    from bot.services.promotions import _discount_amount
    percent = int(promo.get("discount_percent") or 0)
    discount = _discount_amount(original, percent)
    final = original - discount
    if discount <= 0 or final <= 0:
        return result
    result.update(
        applied=True, promo=promo, percent=percent,
        discount_cents=discount, final_cents=final,
    )
    return result


def price_for_site(code: Optional[str], tariff: Dict[str, Any]) -> Dict[str, Any]:
    """Как quote(), но любая ошибка = полная цена (оплата не должна ломаться из-за акции)."""
    try:
        return quote(code, tariff)
    except Exception as e:
        logger.warning("site promo quote failed: %s", e)
        original = int(round(float((tariff or {}).get("price_rub") or 0) * 100))
        return {"applied": False, "promo": None, "reason": None, "original_cents": original,
                "discount_cents": 0, "final_cents": original, "percent": 0}


def tag_purchase(order_id: str, q: Dict[str, Any]) -> None:
    """Запоминает применённую скидку в заказе (для аналитики акции и лимита)."""
    if not q.get("applied") or not q.get("promo"):
        return
    try:
        from database.db_collabs import tag_site_purchase
        tag_site_purchase(order_id, int(q["promo"]["id"]), q["original_cents"], q["discount_cents"], q["final_cents"])
    except Exception as e:
        logger.warning("site promo tag failed: %s", e)


def banner(code: Optional[str]) -> Optional[Dict[str, Any]]:
    """Данные для баннера на сайте: заголовок и список условий (обычный текст).

    Для совместной акции заголовок «Совместная акция <бренд> × <партнёр>».
    """
    checked = validate_code(code)
    if not checked["ok"]:
        return None
    promo = checked["promo"]
    percent = int(promo.get("discount_percent") or 0)
    lines: List[str] = []
    title = f"Промокод {promo['code']}"
    is_collab = False
    min_months = 0
    try:
        from bot.services.collabs import get_active_campaign_for_promo, _brand, _format_date
        campaign = get_active_campaign_for_promo(promo)
    except Exception:
        campaign = None
    if campaign:
        is_collab = True
        min_months = int(campaign.get("min_months") or 0)
        title = f"Совместная акция {_brand()} × {campaign['title']}"
        lines.append(f"Скидка {percent}%" + (f" на тарифы от {min_months} мес." if min_months else ""))
        if promo.get("expires_at"):
            lines.append(f"Акция до {_format_date(promo.get('expires_at'))}")
        description = (campaign.get("description") or "").strip()
        if description:
            lines.append(description)
    else:
        lines.append(f"Скидка {percent}%")
    lines.append("Скидка применится к цене автоматически.")
    return {
        "code": promo["code"], "percent": percent, "title": title, "lines": lines,
        "is_collab": is_collab, "min_months": min_months,
    }


def telegram_prices(user_id: int, tariffs: List[Dict[str, Any]]) -> Dict[int, Dict[str, Any]]:
    """Цены со скидкой для Telegram WebApp: считаются теми же правилами, что и оплата в боте.

    Возвращает {tariff_id: {"final_rub", "original_rub", "percent"}} только там, где скидка действует.
    """
    out: Dict[int, Dict[str, Any]] = {}
    try:
        from bot.services.promotions import build_quote
        for t in tariffs:
            q = build_quote(user_id=user_id, tariff=t, payment_type="yookassa_qr")
            if q.get("ok") and q.get("promo") and int(q.get("discount_amount") or 0) > 0:
                out[int(t["id"])] = {
                    "final_rub": int(q["final_amount"]) / 100,
                    "original_rub": int(q["original_amount"]) / 100,
                    "percent": int(q.get("discount_percent") or 0),
                }
    except Exception as e:
        logger.warning("telegram promo prices failed: %s", e)
    return out
