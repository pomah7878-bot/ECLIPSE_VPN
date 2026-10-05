"""Автоматическая цена в Telegram Stars.

Цена в звёздах считается из рублёвой цены тарифа по курсу ЦБ так, чтобы
администратор получил не меньше, чем при оплате рублями:

    звёзды = ceil(цена_в_долларах * (1 + запас%) / выплата_за_1_звезду)

Настройки (database settings):
  stars_auto_price  — '1' авто (по умолчанию) / '0' ручные цены тарифов
  stars_net_usd     — сколько $ админ получает за 1 ⭐ (по умолчанию 0.013)
  stars_markup_pct  — запас в пользу админа, % (по умолчанию 5)
"""
import logging
import math
import time
from typing import Any, Dict, Optional

from database.requests import get_setting

logger = logging.getLogger(__name__)

DEFAULT_NET_USD = 0.013
DEFAULT_MARKUP_PCT = 5
_RATE_TTL = 900.0
_last_refresh = 0.0


def auto_enabled() -> bool:
    return (get_setting('stars_auto_price', '1') or '1') == '1'


def net_usd() -> float:
    try:
        v = float(get_setting('stars_net_usd', str(DEFAULT_NET_USD)))
    except (TypeError, ValueError):
        return DEFAULT_NET_USD
    return v if 0.005 <= v <= 0.05 else DEFAULT_NET_USD


def markup_pct() -> int:
    try:
        v = int(float(get_setting('stars_markup_pct', str(DEFAULT_MARKUP_PCT))))
    except (TypeError, ValueError):
        return DEFAULT_MARKUP_PCT
    return min(max(v, 0), 100)


def rate_rub() -> Optional[float]:
    """Курс ₽ за $1 из кеша настроек (None, пока курс ни разу не получен)."""
    raw = get_setting('usd_rub_rate', '') or ''
    try:
        v = int(raw) / 100.0
    except (TypeError, ValueError):
        return None
    return v if v > 1 else None


def calc_stars(tariff: Dict[str, Any]) -> Optional[int]:
    """Автоцена в звёздах или None, если посчитать нельзя."""
    usd = None
    try:
        rub = float(tariff.get('price_rub') or 0)
    except (TypeError, ValueError):
        rub = 0.0
    if rub > 1:
        rate = rate_rub()
        if rate:
            usd = rub / rate
    if usd is None:
        cents = int(tariff.get('price_cents') or 0)
        if cents > 0:
            usd = cents / 100.0
    if not usd or usd <= 0:
        return None
    stars = math.ceil(round(usd * (1 + markup_pct() / 100.0) / net_usd(), 6))
    return max(stars, 1)


def effective_price_stars(tariff: Dict[str, Any]) -> int:
    """Цена тарифа в звёздах, которую видит и платит клиент."""
    manual = int(tariff.get('price_stars') or 0)
    if not auto_enabled():
        return manual
    return calc_stars(tariff) or manual


async def refresh_stars_rate() -> None:
    """Обновляет курс ЦБ не чаще раза в 15 минут (если автоцена включена)."""
    global _last_refresh
    if not auto_enabled():
        return
    now = time.monotonic()
    if _last_refresh and now - _last_refresh < _RATE_TTL:
        return
    _last_refresh = now
    try:
        from bot.services.exchange_rate import get_usd_rub_rate
        await get_usd_rub_rate()
    except Exception as e:
        logger.warning(f"Не удалось обновить курс для цен в Stars: {e}")
