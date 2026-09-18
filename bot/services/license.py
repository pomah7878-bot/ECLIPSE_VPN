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
(у самого Романа), либо старая инсталляция без лицензирования — полный
доступ без проверки (обратная совместимость, никого не отключаем задним
числом).

С версии, добавившей произвольный набор функций (features): вместо
жёсткого выбора между двумя готовыми наборами (basic/full) администратор
вручную выбирает ЛЮБОЙ набор функций для каждой конкретной лицензии —
например, можно выдать AI-помощника и сайт, но без рассылок. tier
('basic'/'full') сохраняется только для отображения и обратной
совместимости со старыми лицензиями, реальная проверка идёт по features.
"""
import logging
import os
from datetime import datetime, timedelta
from typing import Optional, Set

logger = logging.getLogger(__name__)

# Все функции, которые можно ВКЛЮЧАТЬ/ВЫКЛЮЧАТЬ по отдельности для каждой
# лицензии/тарифа. Ключ → человекочитаемое название (для UI выбора).
GATED_FEATURES = {
    "ai_assistant": "🤖 AI-помощник",
    "zvonok_verification": "📞 Верификация по звонку (Zvonok)",
    "site_webapp": "🌐 Сайт/личный кабинет",
    "broadcast_marketing": "📢 Рассылка",
    "promo_coupons": "🎟 Промокоды и купоны",
    "custom_app": "📱 Своё Android-приложение",
    "channel_posts": "📰 Публикация в канал/группу",
    "trial_period": "🎁 Пробный период (автовыдача)",
    "referral_system": "🔗 Реферальная система",
    "app_import": "📲 Импорт в Happ/INCY/Karing",
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


def features_to_str(features) -> str:
    """Сериализует набор функций в строку для хранения в БД."""
    return ",".join(sorted(f for f in features if f in GATED_FEATURES))


def features_from_str(features_str: Optional[str]) -> Set[str]:
    """Разбирает строку из БД обратно в набор функций."""
    if not features_str:
        return set()
    return {f.strip() for f in features_str.split(",") if f.strip() in GATED_FEATURES}


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
        set_setting("license_features", "")
        set_setting("license_tier", "basic")
        set_setting("license_checked_at", datetime.utcnow().isoformat())
        logger.warning(f"Лицензия недействительна или истекла: {data.get('message', '')}")
        return

    features_str = data.get("features") or ""
    set_setting("license_features", features_str)
    set_setting("license_tier", data.get("tier") or "basic")
    set_setting("license_checked_at", datetime.utcnow().isoformat())
    set_setting("license_expires_at", data.get("expires_at") or "")
    set_setting("license_partner_name", data.get("partner_name") or "")
    logger.info(f"Лицензия обновлена: функции={features_str or '(нет)'}")


def get_enabled_features() -> Set[str]:
    """Возвращает набор функций, доступных ЭТОЙ инсталляции прямо сейчас.

    Если LICENSE_KEY не задан вообще (главная инсталляция или старая без
    лицензирования) — доступны ВСЕ функции, проверка не требуется.

    Если задан, но ещё ни разу не проверялся, или последняя проверка
    старше грейс-периода — доступных функций НЕТ (безопасный дефолт)."""
    if not get_license_key():
        return set(GATED_FEATURES.keys())

    from database.requests import get_setting

    checked_at_str = get_setting("license_checked_at", "")
    if not checked_at_str:
        return set()

    try:
        checked_at = datetime.fromisoformat(checked_at_str)
    except ValueError:
        return set()

    if datetime.utcnow() - checked_at > timedelta(hours=_GRACE_PERIOD_HOURS):
        return set()

    return features_from_str(get_setting("license_features", ""))


def get_license_tier() -> str:
    """Сохранено для обратной совместимости и отображения — 'full', если
    включены ВСЕ функции, 'basic', если ни одной, иначе 'custom'."""
    enabled = get_enabled_features()
    if enabled == set(GATED_FEATURES.keys()):
        return "full"
    if not enabled:
        return "basic"
    return "custom"


def is_full_tier() -> bool:
    return get_license_tier() == "full"


def is_feature_available(feature: str) -> bool:
    if feature not in GATED_FEATURES:
        return True
    return feature in get_enabled_features()


FEATURE_UPGRADE_MESSAGE = (
    "🔒 <b>Эта функция недоступна на вашем тарифе</b>\n\n"
    "Чтобы разблокировать, обратитесь к поставщику лицензии."
)
