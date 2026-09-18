"""
Система лицензирования для whitelabel-партнёров ECLIPSE VPN.

Каждая инсталляция бота (кроме ГЛАВНОЙ, у Романа) может иметь LICENSE_KEY
в secrets.env — уникальный ключ, выданный при продаже whitelabel-лицензии.
Бот периодически проверяет его статус на ГЛАВНОМ сервере (том, что настроен
как LICENSE_SERVER_URL) и кэширует результат локально в settings.

Проверка НАМЕРЕННО не блокирует само обновление кода через git (это легко
обходится вручную и не стоит усилий на защиту) — вместо этого доступ к
платным функциям проверяется В РЕАЛЬНОМ ВРЕМЕНИ при каждом обращении к ним,
на основе последнего известного статуса лицензии. Даже если партнёр обновит
код без активной лицензии, платные функции просто не активируются.

Если LICENSE_KEY не задан вообще — считается, что это ГЛАВНАЯ инсталляция
(у самого Романа), либо старая инсталляция без лицензирования — full доступ
без проверки (обратная совместимость, никого не отключаем задним числом).
"""
import logging
import os
from datetime import datetime, timedelta
from typing import Optional

logger = logging.getLogger(__name__)

FULL_TIER_FEATURES = {
    "ai_assistant",
    "zvonok_verification",
    "site_webapp",
    "broadcast_marketing",
}

_GRACE_PERIOD_HOURS = 72


def get_license_key() -> Optional[str]:
    return os.environ.get("LICENSE_KEY", "").strip() or None


def get_license_server_url() -> str:
    return os.environ.get("LICENSE_SERVER_URL", "https://eclipse.unlimited.bot.nu").rstrip("/")


def get_license_bot_username() -> str:
    """Username ГЛАВНОГО бота (Романа) — там живут тарифы на лицензии и
    команда /buy_license. Используется для диплинков "Купить/продлить"
    в панели партнёра, чтобы отправить его оформлять покупку именно
    туда, а не в его собственный бот (там нет тарифов на лицензии)."""
    return os.environ.get("LICENSE_BOT_USERNAME", "eclipse_unlimited_bot").lstrip("@")


async def refresh_license_status() -> None:
    license_key = get_license_key()
    if not license_key:
        return

    from database.requests import set_setting
    import aiohttp

    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(
                get_license_server_url() + "/api/license/check",
                json={"license_key": license_key},
                timeout=aiohttp.ClientTimeout(total=15),
            ) as resp:
                data = await resp.json()
    except Exception as e:
        logger.warning(
            f"Лицензия: не удалось связаться с сервером ({e}) — "
            f"используем последний известный статус (грейс-период {_GRACE_PERIOD_HOURS}ч)"
        )
        return

    if not data.get("valid"):
        set_setting("license_tier", "basic")
        set_setting("license_checked_at", datetime.utcnow().isoformat())
        logger.warning(f"Лицензия недействительна или истекла: {data.get('message', '')}")
        return

    tier = data.get("tier") if data.get("tier") in ("basic", "full") else "basic"
    set_setting("license_tier", tier)
    set_setting("license_checked_at", datetime.utcnow().isoformat())
    set_setting("license_expires_at", data.get("expires_at") or "")
    set_setting("license_partner_name", data.get("partner_name") or "")
    logger.info(f"Лицензия обновлена: тариф={tier}")


def get_license_tier() -> str:
    if not get_license_key():
        return "full"

    from database.requests import get_setting

    tier = get_setting("license_tier", "basic")
    checked_at_str = get_setting("license_checked_at", "")
    if not checked_at_str:
        return "basic"

    try:
        checked_at = datetime.fromisoformat(checked_at_str)
    except ValueError:
        return "basic"

    if datetime.utcnow() - checked_at > timedelta(hours=_GRACE_PERIOD_HOURS):
        return "basic"

    return tier if tier in ("basic", "full") else "basic"


def is_full_tier() -> bool:
    return get_license_tier() == "full"


def is_feature_available(feature: str) -> bool:
    if feature not in FULL_TIER_FEATURES:
        return True
    return is_full_tier()


FEATURE_UPGRADE_MESSAGE = (
    "🔒 <b>Эта функция доступна в полном тарифе</b>\n\n"
    "Чтобы разблокировать, обратитесь к поставщику лицензии для перехода на полный тариф."
)
