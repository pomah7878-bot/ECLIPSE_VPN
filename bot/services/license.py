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

Если LICENSE_KEY не задан вообще и это не ГЛАВНАЯ инсталляция — доступных
платных функций НЕТ (безопасный дефолт). Существующим партнёрам, у которых
не было лицензии до введения этой системы, доступ сохраняется не "по
умолчанию", а явно — им выдаётся ключ с нужным набором функций (в т.ч.
безлимитный "полный" ключ), который они вводят через кнопку
"🔑 Ввести код лицензии". Так остаётся аудируемая запись о том, кому и что
выдано, вместо неявного "нет ключа = всё бесплатно".

С версии, добавившей произвольный набор функций (features): вместо
жёсткого выбора между двумя готовыми наборами (basic/full) администратор
вручную выбирает ЛЮБОЙ набор функций для каждой конкретной лицензии —
например, можно выдать AI-помощника и сайт, но без рассылок. tier
('basic'/'full') сохраняется только для отображения и обратной
совместимости со старыми лицензиями, реальная проверка идёт по features.
"""
import logging
import os
import re
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
    "domain_autoprovision": "🌐 Автонастройка резервного домена (DNS+nginx+SSL)",
    "hide_powered_by": "🏷 Отключение метки Powered by ECLIPSE",
}

HIDE_POWERED_BY_FEATURE = "hide_powered_by"


def tier_of(enabled) -> str:
    """Тариф для отображения. Право отключения метки на тариф не влияет."""
    e = set(enabled) - {HIDE_POWERED_BY_FEATURE}
    core = set(GATED_FEATURES) - {HIDE_POWERED_BY_FEATURE}
    if e == core:
        return "full"
    return "basic" if not e else "custom"

_GRACE_PERIOD_HOURS = 72


def get_license_key() -> Optional[str]:
    """Проверяет сначала переменную окружения LICENSE_KEY (secrets.env —
    задаётся при установке через сервер), затем — ключ, введённый
    ПРЯМО В БОТЕ через кнопку "🔑 Ввести код лицензии" (для партнёров,
    которым Роман выдал ключ напрямую, без покупки, и которым не нужно
    лезть на сервер руками)."""
    # v1.165: ключ, введённый кнопкой в боте, важнее ключа из secrets.env —
    # иначе при продлении новый код молча игнорировался бы старым ключом.
    try:
        from database.requests import get_setting
        db_key = (get_setting("license_key_override", "") or "").strip()
        if db_key:
            return db_key
    except Exception:
        pass
    env_key = os.environ.get("LICENSE_KEY", "").strip()
    return env_key or None


def is_license_server() -> bool:
    """True — это ГЛАВНАЯ инсталляция, которая САМА выдаёт лицензии
    (у Романа). Определяется ЯВНЫМ признаком IS_LICENSE_SERVER=1 в
    secrets.env, а НЕ просто отсутствием LICENSE_KEY — иначе только что
    установленная, ещё НЕ настроенная партнёрская инсталляция (у
    которой ключа тоже пока нет) ошибочно показала бы панель ВЫДАЧИ
    лицензий вместо панели "купить/ввести код своей"."""
    return os.environ.get("IS_LICENSE_SERVER", "").strip() in ("1", "true", "True")


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


_KEY_FORMAT_RE = re.compile(r"^ECLW-[A-Z0-9]{4}-[A-Z0-9]{4}-[A-Z0-9]{4}$")


def is_valid_key_format(code: str) -> bool:
    return bool(_KEY_FORMAT_RE.match((code or "").strip().upper()))


def get_instance_id() -> str:
    """Постоянный случайный идентификатор ЭТОЙ установки бота. Сервер лицензий
    по нему считает, на скольких установках используется ключ."""
    import uuid
    from database.requests import get_setting, set_setting

    value = get_setting("license_instance_id", "")
    if not value:
        value = uuid.uuid4().hex
        set_setting("license_instance_id", value)
    return value


async def fetch_license(license_key: str):
    """Запрашивает статус лицензии на сервере лицензий.

    Возвращает ("valid" | "invalid" | "unreachable", данные).
    "invalid" — только если сервер ЯВНО ответил HTTP 200 с valid=false (ключ
    не найден, отозван, истёк). Любой сбой связи, не-JSON, ошибка прокси,
    лимит запросов — "unreachable": прежний статус лицензии сохраняется."""
    import aiohttp
    import secrets

    instance_id = get_instance_id()
    nonce = secrets.token_urlsafe(16)
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(
                get_license_server_url() + "/api/license/check",
                json={"license_key": license_key, "instance_id": instance_id, "nonce": nonce},
                timeout=aiohttp.ClientTimeout(total=15),
            ) as resp:
                status_code = resp.status
                data = await resp.json(content_type=None)
    except Exception as e:
        logger.warning(
            f"Лицензия: не удалось связаться с сервером ({e}) — "
            f"используем последний известный статус (грейс-период {_GRACE_PERIOD_HOURS}ч)"
        )
        return "unreachable", {}

    if status_code != 200 or not isinstance(data, dict):
        logger.warning(f"Лицензия: сервер ответил HTTP {status_code} — статус не меняем")
        return "unreachable", {}
    if data.get("valid") is True:
        # v1.166: ответ должен быть подписан сервером лицензий; данные берём
        # из подписанной части, а не из открытых полей ответа.
        from bot.services.license_signing import verify_license_response

        verdict, payload = verify_license_response(data.get("signed"), license_key, instance_id, nonce)
        if verdict == "bad":
            logger.warning("Лицензия: подпись ответа сервера неверна — ответ отклонён, статус не меняем")
            return "unreachable", {}
        if verdict == "ok":
            data = {
                "valid": True,
                "tier": payload.get("tier"),
                "features": payload.get("features") or "",
                "expires_at": payload.get("expires_at"),
                "partner_name": payload.get("partner_name"),
            }
        return "valid", data
    if data.get("valid") is False:
        return "invalid", data
    return "unreachable", {}


async def request_trial_license(telegram_id: int):
    """Запрашивает пробную лицензию у главного сервера.
    Возвращает ("ok" | "denied" | "unreachable", данные)."""
    import aiohttp

    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(
                get_license_server_url() + "/api/license/trial",
                json={"instance_id": get_instance_id(), "telegram_id": int(telegram_id)},
                timeout=aiohttp.ClientTimeout(total=15),
            ) as resp:
                status_code = resp.status
                data = await resp.json(content_type=None)
    except Exception as e:
        logger.warning(f"Пробная лицензия: не удалось связаться с сервером ({e})")
        return "unreachable", {}
    if status_code != 200 or not isinstance(data, dict):
        return "unreachable", {}
    if data.get("ok") is True and data.get("license_key"):
        return "ok", data
    return "denied", data


def _apply_valid_license(data: dict) -> None:
    from database.requests import set_setting

    features_str = data.get("features") or ""
    set_setting("license_features", features_str)
    set_setting("license_tier", data.get("tier") or "basic")
    set_setting("license_checked_at", datetime.utcnow().isoformat())
    set_setting("license_expires_at", data.get("expires_at") or "")
    set_setting("license_partner_name", data.get("partner_name") or "")
    logger.info(f"Лицензия обновлена: функции={features_str or '(нет)'}")


def _clear_license_status(message: str) -> None:
    from database.requests import set_setting

    set_setting("license_features", "")
    set_setting("license_tier", "basic")
    set_setting("license_checked_at", datetime.utcnow().isoformat())
    logger.warning(f"Лицензия недействительна или истекла: {message}")


async def refresh_license_status() -> None:
    license_key = get_license_key()
    if not license_key:
        return

    state, data = await fetch_license(license_key)
    if state == "valid":
        _apply_valid_license(data)
    elif state == "invalid":
        _clear_license_status(data.get("message", ""))
    # "unreachable": ничего не меняем, действует грейс-период


async def activate_license_code(code: str):
    """Проверяет код на сервере и сохраняет его ТОЛЬКО если он действителен —
    опечатка не перетирает рабочую лицензию. Возвращает (состояние, данные)."""
    from database.requests import set_setting

    state, data = await fetch_license(code)
    if state == "valid":
        set_setting("license_key_override", code)
        _apply_valid_license(data)
    return state, data


def get_enabled_features() -> Set[str]:
    """Возвращает набор функций, доступных ЭТОЙ инсталляции прямо сейчас.

    Если это ГЛАВНАЯ инсталляция (IS_LICENSE_SERVER=1 в secrets.env) —
    доступны ВСЕ функции, проверка не требуется.

    Иначе (партнёрская инсталляция) — если лицензия ещё не введена,
    ни разу не проверялась, или последняя проверка старше грейс-периода
    — доступных функций НЕТ (безопасный дефолт, а не "всё бесплатно")."""
    if is_license_server():
        return set(GATED_FEATURES.keys())
    if not get_license_key():
        return set()

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
    return tier_of(get_enabled_features())


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
