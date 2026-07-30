import sqlite3
import logging
import json
import secrets
import string
import datetime
import re
from typing import Optional, List, Dict, Any, Tuple
from .connection import get_db

logger = logging.getLogger(__name__)

__all__ = [
    'get_setting',
    'set_setting',
    'delete_setting',
    'is_update_notifications_enabled',
    'get_display_timezone',
    'set_display_timezone',
    'normalize_display_timezone',
    'is_crypto_enabled',
    'is_stars_enabled',
    'is_crypto_configured',
    'is_cards_enabled',
    'is_cards_configured',
    'is_yookassa_qr_enabled',
    'is_yookassa_qr_configured',
    'get_yookassa_credentials',
    'is_wata_enabled',
    'is_wata_configured',
    'get_wata_token',
    'is_platega_enabled',
    'is_platega_configured',
    'get_platega_credentials',
    'is_cardlink_enabled',
    'is_cardlink_configured',
    'get_cardlink_credentials',
    'is_trial_enabled',
    'get_trial_tariff_id',
    'is_demo_payment_enabled',
    'get_effective_webapp_url',
    'set_webapp_url',
    'get_effective_groq_api_key',
    'set_groq_api_key',
    'get_effective_tavily_api_key',
    'set_tavily_api_key',
    'get_effective_oauth_credentials',
    'set_oauth_credentials',
]

DEFAULT_DISPLAY_TIMEZONE = 'Europe/Moscow'
DISPLAY_TIMEZONE_SETTING = 'display_timezone'
UPDATE_NOTIFICATIONS_ENABLED_SETTING = 'update_notifications_enabled'

_TIMEZONE_ALIASES = {
    'москва': DEFAULT_DISPLAY_TIMEZONE,
    'мск': DEFAULT_DISPLAY_TIMEZONE,
    'moscow': DEFAULT_DISPLAY_TIMEZONE,
    'msk': DEFAULT_DISPLAY_TIMEZONE,
    'utc': 'UTC',
    'gmt': 'UTC',
}
_UTC_OFFSET_RE = re.compile(r'^(?:utc|gmt)?\s*([+-])\s*(\d{1,2})(?::?(\d{2}))?$')

def get_setting(key: str, default: Optional[str] = None) -> Optional[str]:
    """
    Gets the setting value.
    
    Args:
        key: Setting key
        default: Default value
        
    Returns:
        Setting value or default
    """
    with get_db() as conn:
        cursor = conn.execute(
            "SELECT value FROM settings WHERE key = ?",
            (key,)
        )
        row = cursor.fetchone()
        return row['value'] if row else default

def set_setting(key: str, value: str) -> None:
    """
    Sets the setting value.
    
    Args:
        key: Setting key
        value: Setting value
    """
    with get_db() as conn:
        conn.execute("""
            INSERT INTO settings (key, value)
            VALUES (?, ?)
            ON CONFLICT(key) DO UPDATE SET value = excluded.value
        """, (key, value))
        logger.info(f"Настройка обновлена: {key}")

def delete_setting(key: str) -> bool:
    """
    Removes a setting.
    
    Args:
        key: Setting key
        
    Returns:
        True if the setting was removed
    """
    with get_db() as conn:
        cursor = conn.execute("DELETE FROM settings WHERE key = ?", (key,))
        return cursor.rowcount > 0


def is_update_notifications_enabled() -> bool:
    """Returns the state of hidden new version notifications."""
    return get_setting(UPDATE_NOTIFICATIONS_ENABLED_SETTING, '1') == '1'


def normalize_display_timezone(value: Optional[str]) -> str:
    """Normalizes the hidden time zone setting for displaying dates."""
    raw = (value or '').strip()
    if not raw:
        return DEFAULT_DISPLAY_TIMEZONE

    key = raw.lower().replace('ё', 'е')
    compact_key = key.replace(' ', '')
    if key in _TIMEZONE_ALIASES:
        return _TIMEZONE_ALIASES[key]
    if compact_key in _TIMEZONE_ALIASES:
        return _TIMEZONE_ALIASES[compact_key]

    match = _UTC_OFFSET_RE.match(compact_key)
    if match:
        sign, hours_raw, minutes_raw = match.groups()
        hours = int(hours_raw)
        minutes = int(minutes_raw or '0')
        if hours <= 23 and minutes <= 59:
            return f'UTC{sign}{hours:02d}:{minutes:02d}'

    if '/' in raw and all(part for part in raw.split('/')):
        return raw

    return DEFAULT_DISPLAY_TIMEZONE


def get_display_timezone() -> str:
    """Returns the time zone in which the bot displays dates to users and admins."""
    return normalize_display_timezone(
        get_setting(DISPLAY_TIMEZONE_SETTING, DEFAULT_DISPLAY_TIMEZONE)
    )


def set_display_timezone(value: str) -> str:
    """Preserves the display time zone and returns a normalized value."""
    timezone_value = normalize_display_timezone(value)
    set_setting(DISPLAY_TIMEZONE_SETTING, timezone_value)
    return timezone_value



def is_crypto_enabled() -> bool:
    """Checks if crypto payments are enabled."""
    return get_setting('crypto_enabled', '0') == '1'

def is_stars_enabled() -> bool:
    """Checks if Telegram Stars is enabled."""
    return get_setting('stars_enabled', '0') == '1'

def is_crypto_configured() -> bool:
    """
    Checks whether crypto payments are fully configured.
    
    Returns:
        True if crypto is included AND there is a link to the product (for standard mode) or just included
    """
    if not is_crypto_enabled():
        return False
    crypto_item_url = get_setting('crypto_item_url')
    return bool(crypto_item_url and crypto_item_url.strip())



def is_cards_enabled() -> bool:
    """Checks if TG payments are enabled."""
    return get_setting('cards_enabled', '0') == '1'

def is_cards_configured() -> bool:
    """
    Checks if TG payments are configured.
    
    Returns:
        True if TG payments are enabled AND there is a provider_token
    """
    if not is_cards_enabled():
        return False
    token = get_setting('cards_provider_token')
    return bool(token and token.strip())

def is_yookassa_qr_enabled() -> bool:
    """Checks whether direct payment through YuKassa is enabled."""
    return get_setting('yookassa_qr_enabled', '0') == '1'

def is_yookassa_qr_configured() -> bool:
    """
    Checks whether direct payment through YuKassa is fully configured.

    Returns:
        True if YuKassa is enabled AND there is shop_id and secret_key
    """
    if not is_yookassa_qr_enabled():
        return False
    shop_id = get_setting('yookassa_shop_id', '')
    secret_key = get_setting('yookassa_secret_key', '')
    return bool(shop_id and shop_id.strip() and secret_key and secret_key.strip())

def get_yookassa_credentials() -> tuple[str, str]:
    """
    Returns YuKass credentials for the direct API.

    Returns:
        Tuple (shop_id, secret_key)
    """
    shop_id = get_setting('yookassa_shop_id', '')
    secret_key = get_setting('yookassa_secret_key', '')
    return shop_id, secret_key

def is_wata_enabled() -> bool:
    """Checks whether payment via WATA is enabled."""
    return get_setting('wata_enabled', '0') == '1'

def is_wata_configured() -> bool:
    """
    Checks whether payment via WATA is fully configured.

    Returns:
        True if WATA is enabled AND a JWT token is specified
    """
    if not is_wata_enabled():
        return False
    token = get_setting('wata_jwt_token', '')
    return bool(token and token.strip())

def get_wata_token() -> str:
    """
    Returns the JWT token for the WATA API.

    Returns:
        JWT token string (or empty string)
    """
    return get_setting('wata_jwt_token', '') or ''

def is_platega_enabled() -> bool:
    """Checks if payment via Platega is enabled."""
    return get_setting('platega_enabled', '0') == '1'

def is_platega_configured() -> bool:
    """
    Checks whether payment via Platega is fully configured.

    Returns:
        True if Platega is enabled AND merchant_id and secret are specified
    """
    if not is_platega_enabled():
        return False
    merchant_id = get_setting('platega_merchant_id', '')
    secret = get_setting('platega_secret', '')
    return bool(merchant_id and merchant_id.strip() and secret and secret.strip())

def get_platega_credentials() -> tuple[str, str]:
    """
    Returns Platega credentials for the direct API.

    Returns:
        Tuple (merchant_id, secret)
    """
    merchant_id = get_setting('platega_merchant_id', '')
    secret = get_setting('platega_secret', '')
    return merchant_id, secret

def is_cardlink_enabled() -> bool:
    """Checks if payment via Cardlink is enabled."""
    return get_setting('cardlink_enabled', '0') == '1'

def is_cardlink_configured() -> bool:
    """
    Checks whether payment via Cardlink is fully configured.

    Returns:
        True if Cardlink is enabled AND shop_id and api_token are specified
    """
    if not is_cardlink_enabled():
        return False
    shop_id = get_setting('cardlink_shop_id', '')
    token = get_setting('cardlink_api_token', '')
    return bool(shop_id and shop_id.strip() and token and token.strip())

def get_cardlink_credentials() -> tuple[str, str]:
    """
    Returns Cardlink credentials for the direct API.

    Returns:
        Tuple (shop_id, api_token)
    """
    shop_id = get_setting('cardlink_shop_id', '')
    token = get_setting('cardlink_api_token', '')
    return shop_id, token

def is_trial_enabled() -> bool:
    """Is the trial subscription feature enabled?"""
    return get_setting('trial_enabled', '0') == '1'

def get_trial_tariff_id() -> Optional[int]:
    """
    Returns the tariff ID for a trial subscription.
    
    Returns:
        Rate ID or None if no rate is specified
    """
    val = get_setting('trial_tariff_id', '')
    return int(val) if val and val.isdigit() else None

def is_demo_payment_enabled() -> bool:
    """Is demo payment by RF card included?"""
    return get_setting('demo_payment_enabled', '0') == '1'


# ============================================================================
# ИНТЕГРАЦИИ, НАСТРАИВАЕМЫЕ ЧЕРЕЗ АДМИН-ПАНЕЛЬ
# (домен сайта, AI, OAuth) — приоритет над config.py/secrets.env, чтобы можно
# было донастроить бота после установки, если данные не были даны сразу.
# ============================================================================

def get_effective_webapp_url() -> str:
    """Домен сайта/WebApp. Значение из админки имеет приоритет над config.py."""
    from_db = get_setting('webapp_url', '')
    if from_db:
        return from_db
    try:
        from config import WEBAPP_URL
        return WEBAPP_URL
    except ImportError:
        return ''

def set_webapp_url(url: str) -> None:
    """Сохраняет домен сайта/WebApp, заданный через админ-панель."""
    set_setting('webapp_url', url.strip().rstrip('/'))

def get_effective_groq_api_key() -> str:
    """Ключ Groq для AI-консультанта. Значение из админки имеет приоритет
    над переменной окружения GROQ_API_KEY."""
    from_db = get_setting('groq_api_key', '')
    if from_db:
        return from_db
    import os
    return os.environ.get('GROQ_API_KEY', '')

def set_groq_api_key(api_key: str) -> None:
    """Сохраняет ключ Groq, заданный через админ-панель."""
    set_setting('groq_api_key', api_key.strip())

def get_effective_tavily_api_key() -> str:
    """Ключ Tavily для веб-поиска AI-консультанта. Значение из админки
    имеет приоритет над переменной окружения TAVILY_API_KEY."""
    from_db = get_setting('tavily_api_key', '')
    if from_db:
        return from_db
    import os
    return os.environ.get('TAVILY_API_KEY', '')

def set_tavily_api_key(api_key: str) -> None:
    """Сохраняет ключ Tavily, заданный через админ-панель."""
    set_setting('tavily_api_key', api_key.strip())

def get_effective_oauth_credentials(provider: str) -> tuple[str, str]:
    """
    Возвращает (client_id, client_secret) для OAuth-провайдера
    (google/yandex/vk). Значения из админки имеют приоритет над
    переменными окружения *_OAUTH_CLIENT_ID / *_OAUTH_CLIENT_SECRET.
    """
    client_id = get_setting(f'{provider}_oauth_client_id', '')
    client_secret = get_setting(f'{provider}_oauth_client_secret', '')
    if client_id and client_secret:
        return client_id, client_secret
    import os
    env_prefix = provider.upper()
    return (
        os.environ.get(f'{env_prefix}_OAUTH_CLIENT_ID', ''),
        os.environ.get(f'{env_prefix}_OAUTH_CLIENT_SECRET', ''),
    )

def set_oauth_credentials(provider: str, client_id: str, client_secret: str) -> None:
    """Сохраняет OAuth-реквизиты провайдера, заданные через админ-панель."""
    set_setting(f'{provider}_oauth_client_id', client_id.strip())
    set_setting(f'{provider}_oauth_client_secret', client_secret.strip())

