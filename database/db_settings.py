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
    'get_trial_mode',
    'set_trial_mode',
    'get_marketing_channel_id',
    'set_marketing_channel_id',
    'get_happ_provider_id',
    'set_happ_provider_id',
    'get_happ_proxy_provider_code',
    'set_happ_proxy_provider_code',
    'get_happ_proxy_auth_key',
    'set_happ_proxy_auth_key',
    'get_happ_install_limit',
    'set_happ_install_limit',
    'get_happ_proxy_registered_domain_hash',
    'set_happ_proxy_registered_domain_hash',
    'HAPP_ROUTING_MODES',
    'get_happ_routing_mode',
    'get_happ_routing_variant',
    'set_happ_routing_variant',
    'get_routing_user_choice_enabled',
    'set_routing_user_choice_enabled',
    'get_user_routing_variant',
    'set_user_routing_variant',
    'resolve_routing_variant',
    'routing_min_ts',
    'HAPP_ROUTING_VARIANTS',
    'set_happ_routing_mode',
    'get_happ_routing_profile_json',
    'set_happ_routing_profile_json',
    'get_menu_min_width',
    'set_menu_min_width',
    'get_happ_routing_profile_name',
    'set_happ_routing_profile_name',
    'get_default_happ_routing_profile_json',
    'get_effective_happ_routing_profile_json',
    'prepare_routing_profile',
    'request_routing_publish',
    'is_client_toggle_enabled',
    'set_client_toggle_enabled',
    'CLIENT_APPS',
    'CLIENT_TOGGLE_LABELS',
    'HAPP_TOGGLE_LABELS',
    'INCY_TOGGLE_LABELS',
    'CLIENT_TOGGLE_DEFAULT_ON',
    'INCY_AUTOROUTING_URL',
    'CLIENT_TOGGLE_LABELS_BY_APP',
    'get_incy_update_interval_hours',
    'set_incy_update_interval_hours',
    'get_zvonok_public_key',
    'set_zvonok_public_key',
    'get_zvonok_campaign_id',
    'set_zvonok_campaign_id',
    'get_zvonok_pincode_campaign_id',
    'set_zvonok_pincode_campaign_id',
    'get_zvonok_flashcall_real_campaign_id',
    'set_zvonok_flashcall_real_campaign_id',
    'get_zvonok_voice_code_campaign_id',
    'set_zvonok_voice_code_campaign_id',
    'get_zvonok_press_digit_campaign_id',
    'set_zvonok_press_digit_campaign_id',
    'ZVONOK_METHODS',
    'ZVONOK_CAMPAIGN_TYPE_SLUGS',
    'get_zvonok_verification_method',
    'get_zvonok_proxy_url',
    'set_zvonok_proxy_url',
    'set_zvonok_verification_method',
    'SITE_AUTH_METHODS',
    'is_site_auth_method_enabled',
    'set_site_auth_method_enabled',
    'CABINET_IMPORT_APPS',
    'is_cabinet_import_app_enabled',
    'set_cabinet_import_app_enabled',
    'get_effective_brand_name',
    'set_brand_name',
    'get_effective_turnstile_site_key',
    'set_turnstile_site_key',
    'get_effective_own_app_name',
    'set_own_app_name',
    'get_effective_own_app_url',
    'set_own_app_url',
    'is_start_import_buttons_enabled',
    'set_start_import_buttons_enabled',
    'is_start_balance_button_enabled',
    'is_balance_payment_enabled',
    'set_balance_payment_enabled',
    'set_start_balance_button_enabled',
    'is_welcome_page_enabled',
    'set_welcome_page_enabled',
    'WELCOME_TEMPLATES',
    'get_welcome_template_id',
    'set_welcome_template_id',
    'CABINET_THEMES',
    'get_cabinet_theme_id',
    'DEVICE_LIMIT_TYPES',
    'get_device_limit_type',
    'set_device_limit_type',
    'set_cabinet_theme_id',
    'SHOP_THEMES',
    'get_shop_theme_id',
    'set_shop_theme_id',
    'delete_setting',
    'get_site_session_secret',
    'get_zvonok_postback_token',
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
    'get_webapp_url_backup',
    'set_webapp_url_backup',
    'swap_webapp_url_with_backup',
    'is_ai_support_enabled',
    'set_ai_support_enabled',
    'get_cloudflare_api_token',
    'set_cloudflare_api_token',
    'get_effective_groq_api_key',
    'set_groq_api_key',
    'get_effective_tavily_api_key',
    'set_tavily_api_key',
    'get_effective_gemini_api_key',
    'set_gemini_api_key',
    'get_effective_oauth_credentials',
    'set_oauth_credentials',
    'is_post_footer_enabled',
    'set_post_footer_enabled',
    'is_channel_gate_enabled',
    'set_channel_gate_enabled',
    'get_gate_channel_id',
    'set_gate_channel_id',
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

# v1.334: кэш настроек. Раньше каждое чтение настройки открывало отдельное соединение с базой
# (плюс 7 служебных PRAGMA): на главном меню это было 56 соединений из 64. Теперь обычные
# настройки читаются одним запросом и живут в памяти 2 секунды (другие процессы — например,
# AI-сервис — видят изменения не позже чем через 2 с; в этом процессе запись видна сразу).
# Персональные ключи с «:» (sensitive_msgs:<id>, main_menu_msg:<id> …) их много и они часто
# меняются — читаются напрямую, как раньше.
import threading as _threading
import time as _time

_SETTINGS_TTL = 2.0
_SETTINGS_LOCK = _threading.Lock()
_SETTINGS_SNAPSHOT: Dict[str, str] = {}
_SETTINGS_LOADED_AT = 0.0


def _is_personal_key(key: str) -> bool:
    return ":" in key


def invalidate_settings_cache() -> None:
    """Сбросить кэш настроек: следующее чтение заново загрузит их из базы."""
    global _SETTINGS_LOADED_AT
    _SETTINGS_LOADED_AT = 0.0


def _settings_snapshot() -> Dict[str, str]:
    global _SETTINGS_SNAPSHOT, _SETTINGS_LOADED_AT
    if _time.monotonic() - _SETTINGS_LOADED_AT < _SETTINGS_TTL:
        return _SETTINGS_SNAPSHOT
    with _SETTINGS_LOCK:
        if _time.monotonic() - _SETTINGS_LOADED_AT < _SETTINGS_TTL:
            return _SETTINGS_SNAPSHOT
        with get_db() as conn:
            rows = conn.execute("SELECT key, value FROM settings WHERE instr(key, ':') = 0").fetchall()
        _SETTINGS_SNAPSHOT = {r[0]: r[1] for r in rows}
        _SETTINGS_LOADED_AT = _time.monotonic()
        return _SETTINGS_SNAPSHOT


def get_setting(key: str, default: Optional[str] = None) -> Optional[str]:
    """
    Gets the setting value.
    
    Args:
        key: Setting key
        default: Default value
        
    Returns:
        Setting value or default
    """
    if not _is_personal_key(key):
        try:
            value = _settings_snapshot().get(key)
        except sqlite3.Error:
            value = None
            invalidate_settings_cache()
        else:
            return value if value is not None else default
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
    if not _is_personal_key(key) and _SETTINGS_LOADED_AT:
        _SETTINGS_SNAPSHOT[key] = value


def get_site_session_secret() -> str:
    """Отдельный секрет для подписи сессионных cookie личного кабинета на
    сайте (site_session). Раньше для этого ошибочно переиспользовался
    SUPPORT_API_TOKEN — секрет совсем другого, отдельного AI-support
    сервиса; утечка того токена позволила бы подделать сессию любого
    пользователя сайта. Теперь — свой случайный секрет per-инсталляция,
    генерируется один раз при первом обращении и хранится в БД, никакой
    ручной настройки не требуется."""
    value = get_setting('site_session_secret')
    if not value:
        value = secrets.token_hex(32)
        set_setting('site_session_secret', value)
    return value


def get_zvonok_postback_token() -> str:
    """Секретный токен для постбека (вебхука) от zvonok.com. Постбек —
    это ПУБЛИЧНЫЙ GET-эндпоинт (/api/public/zvonok/postback), а call_id
    при этом отдаётся САМОМУ БРАУЗЕРУ в ответе на запрос верификации —
    то есть без этого токена любой пользователь мог сам дёрнуть постбек
    с result=ok и подтвердить свой номер телефона, ни разу не позвонив
    (полный обход анти-фрод защиты). Генерируется один раз при первом
    обращении и хранится в БД — админу нужно один раз скопировать его в
    ссылки постбека в личном кабинете zvonok.com (см. Интеграции →
    Zvonok → постбек), дальше ничего делать не нужно."""
    value = get_setting('zvonok_postback_token')
    if not value:
        value = secrets.token_hex(16)
        set_setting('zvonok_postback_token', value)
    return value

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
        removed = cursor.rowcount > 0
    if not _is_personal_key(key):
        _SETTINGS_SNAPSHOT.pop(key, None)
    return removed


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


def is_post_footer_enabled() -> bool:
    """Добавлять ли автоматический подвал (ссылки на бота и магазин) к постам
    маркетингового канала. По умолчанию включено — сохраняет прежнее
    поведение (подвал добавлялся всегда, без возможности отключить)."""
    return get_setting('post_footer_enabled', '1') == '1'


def set_post_footer_enabled(enabled: bool) -> None:
    """Включает/выключает автоматический подвал для постов канала."""
    set_setting('post_footer_enabled', '1' if enabled else '0')


def is_start_import_buttons_enabled() -> bool:
    """Показывать ли на главной странице кнопки быстрого импорта подписки
    в приложение ("Импорт HAPP"/"Импорт INCY"). По умолчанию включено —
    у новых пользователей бота кнопки видны сразу, без действий админа."""
    return get_setting('start_import_buttons_enabled', '1') == '1'


def set_start_import_buttons_enabled(enabled: bool) -> None:
    """Включает/выключает кнопки быстрого импорта на главной странице."""
    set_setting('start_import_buttons_enabled', '1' if enabled else '0')


def is_balance_payment_enabled() -> bool:
    """Оплата тарифов с личного баланса. Независимый переключатель (Оплаты).
    Пока админ его не трогал — включено: на баланс попадают возвраты, призы и ручные
    пополнения, и потратить их клиент должен везде одинаково (бот, сайт, WebApp)."""
    return get_setting('balance_payment_enabled', '') != '0'


def set_balance_payment_enabled(enabled: bool) -> None:
    set_setting('balance_payment_enabled', '1' if enabled else '0')


def is_start_balance_button_enabled() -> bool:
    """Показывать ли на главной странице кнопку "💰 Пополнить баланс". По
    умолчанию включено — у новых пользователей бота кнопка видна сразу."""
    return get_setting('start_balance_button_enabled', '1') == '1'


def set_start_balance_button_enabled(enabled: bool) -> None:
    """Включает/выключает кнопку пополнения баланса на главной странице."""
    set_setting('start_balance_button_enabled', '1' if enabled else '0')


def is_welcome_page_enabled() -> bool:
    """Показывать ли публичную страницу-витрину /welcome для новых
    (ещё не подключившихся) посетителей сайта — описание сервиса и
    актуальные тарифы, без входа в бота. По умолчанию ВЫКЛЮЧЕНО — это
    новая опциональная фича, страница отдаёт 404, пока админ явно не
    включит её (после того как проверит контент/домен)."""
    return get_setting('welcome_page_enabled', '0') == '1'


def set_welcome_page_enabled(enabled: bool) -> None:
    """Включает/выключает публичную страницу-витрину /welcome."""
    set_setting('welcome_page_enabled', '1' if enabled else '0')


WELCOME_TEMPLATES = {
    'minimal':  {'file': 'welcome_minimal.html',  'label': '🌙 Минимальный', 'description': 'Тёмный, лаконичный дизайн — акцент на бренде и тарифах'},
    'bold':     {'file': 'welcome_bold.html',     'label': '🔥 Яркий продающий', 'description': 'Крупный CTA, список преимуществ, продающая подача'},
    'detailed': {'file': 'welcome_detailed.html', 'label': '🛠 Подробный технический', 'description': 'Спецификации протокола, FAQ — для технической аудитории'},
}


def get_welcome_template_id() -> str:
    """id выбранного шаблона страницы /welcome. По умолчанию 'minimal'."""
    value = get_setting('welcome_template_id', 'minimal')
    return value if value in WELCOME_TEMPLATES else 'minimal'


def set_welcome_template_id(template_id: str) -> None:
    """Задаёт выбранный шаблон страницы /welcome."""
    if template_id not in WELCOME_TEMPLATES:
        raise ValueError(f"Неизвестный шаблон витрины: {template_id}")
    set_setting('welcome_template_id', template_id)


CABINET_THEMES = {
    'classic': {'label': '🌙 Классический', 'description': 'Тёплое золото, текущий вид — ничего менять не нужно'},
    'sharp':   {'label': '⚡ Neo-Brutalism', 'description': 'Жёсткие контрастные рамки, офсетные тени, кислотно-салатовый акцент — дерзкий топовый тренд 2026'},
    'vivid':   {'label': '🔮 Liquid Glass', 'description': 'Матовое стекло, блюр, полупрозрачные панели на цветном фоне — стиль последних iOS/macOS'},
    'soft':    {'label': '🧈 Neumorphism', 'description': 'Мягкий тактильный UI — карточки будто вдавлены или выпуклы, парные тени вместо рамок'},
    'mono':    {'label': '⬜ Минимализм', 'description': 'Ультра-чистый редакторский стиль — крупная типографика, почти без цвета и теней'},
}


def get_cabinet_theme_id() -> str:
    """id выбранной темы личного кабинета (index.html). По умолчанию 'classic'
    — визуально ничего не меняется, пока админ явно не выберет другую."""
    value = get_setting('cabinet_theme_id', 'classic')
    return value if value in CABINET_THEMES else 'classic'


def set_cabinet_theme_id(theme_id: str) -> None:
    """Задаёт тему личного кабинета."""
    if theme_id not in CABINET_THEMES:
        raise ValueError(f"Неизвестная тема кабинета: {theme_id}")
    set_setting('cabinet_theme_id', theme_id)


SHOP_THEMES = {
    'classic':  {'label': '🥇 Classic Gold', 'description': 'Тёплое золото на графите — текущий вид, ничего менять не нужно'},
    'platinum': {'label': '🖤 Platinum Noir', 'description': 'Жёсткая геометрия: острые углы, офсетные тени, тарифы в 2 колонки на широких экранах — строгий структурный премиум'},
    'rose':     {'label': '🌸 Rose Quartz', 'description': 'Матовое стекло, мягкое свечение, более просторная колонка — бутиковый премиум'},
}


def get_shop_theme_id() -> str:
    """id выбранной темы страницы /shop. По умолчанию 'classic' — визуально
    ничего не меняется, пока админ явно не выберет другую."""
    value = get_setting('shop_theme_id', 'classic')
    return value if value in SHOP_THEMES else 'classic'


def set_shop_theme_id(theme_id: str) -> None:
    """Задаёт тему страницы /shop."""
    if theme_id not in SHOP_THEMES:
        raise ValueError(f"Неизвестная тема магазина: {theme_id}")
    set_setting('shop_theme_id', theme_id)


def is_channel_gate_enabled() -> bool:
    """Требовать ли от пользователя подписку на канал перед использованием
    бота. По умолчанию выключено — не влияет на существующие деплои."""
    return get_setting('channel_gate_enabled', '0') == '1'


def set_channel_gate_enabled(enabled: bool) -> None:
    """Включает/выключает требование обязательной подписки на канал."""
    set_setting('channel_gate_enabled', '1' if enabled else '0')


def get_gate_channel_id() -> Optional[str]:
    """ID/username канала для обязательной подписки (отдельно от
    маркетингового канала — может быть другим чатом)."""
    return get_setting('gate_channel_id')


def set_gate_channel_id(channel_id: str) -> None:
    """Сохраняет канал для обязательной подписки."""
    set_setting('gate_channel_id', channel_id)

def get_trial_tariff_id() -> Optional[int]:
    """
    Returns the tariff ID for a trial subscription.
    
    Returns:
        Rate ID or None if no rate is specified
    """
    val = get_setting('trial_tariff_id', '')
    return int(val) if val and val.isdigit() else None

def get_trial_mode() -> str:
    """
    Режим пробного периода:
    - 'account' (по умолчанию) — один пробник на весь аккаунт, как раньше.
    - 'per_group' — пользователь может взять по одному пробнику в КАЖДОЙ
      группе тарифов, у которой настроен свой пробный тариф.
    """
    value = get_setting('trial_mode', 'account')
    return value if value in ('account', 'per_group') else 'account'


def set_trial_mode(mode: str) -> None:
    """Устанавливает режим пробного периода ('account' или 'per_group')."""
    if mode not in ('account', 'per_group'):
        raise ValueError(f"Недопустимый режим пробника: {mode}")
    set_setting('trial_mode', mode)


def get_marketing_channel_id() -> Optional[str]:
    """
    Username маркетингового Telegram-канала для раздела «📰 Группа» в
    админке (например, '@my_channel_news'). Пусто по умолчанию — каждый
    владелец бота должен явно настроить СВОЙ канал, а не публиковать
    случайно в чужой (при установке из шаблона репозитория)."""
    value = get_setting('marketing_channel_id', '')
    return value if value else None


def set_marketing_channel_id(channel_id: str) -> None:
    """Задаёт username маркетингового канала (с символом @ или без — как ввёл админ)."""
    set_setting('marketing_channel_id', channel_id.strip())


def get_happ_provider_id() -> Optional[str]:
    """Provider ID из личного кабинета happ-proxy.com — открывает доступ к
    'Advanced parameters' у Happ (в т.ч. sub-info/sub-expire, которые мы
    отправляем в /happ-sub). Без него эти параметры официально не
    гарантированы к работе (см. доки happ.su/main/dev-docs/provider-id).
    Пусто по умолчанию — каждый владелец бота регистрирует СВОЙ Provider ID,
    а не использует чужой (иначе статистика/привязка HWID утечёт в чужой
    аккаунт happ-proxy.com)."""
    value = get_setting('happ_provider_id', '')
    return value if value else None


def set_happ_provider_id(provider_id: str) -> None:
    """Задаёт Happ Provider ID."""
    set_setting('happ_provider_id', provider_id.strip())


def get_happ_proxy_provider_code() -> Optional[str]:
    """provider_code для API happ-proxy.com (8 символов [A-Za-z0-9]{8}).

    На практике это ТО ЖЕ САМОЕ значение, что и Happ Provider ID (личный
    кабинет happ-proxy.com показывает его один раз, в шапке — "ID
    провайдера" / "Provider ID" — и использует и как значение заголовка
    подписки, и как provider_code для API). Раньше это было заведено как
    две независимые настройки, что заставляло вводить один и тот же код
    дважды — теперь, если happ_proxy_provider_code не задан отдельно,
    используется уже сохранённый Happ Provider ID. Отдельное значение
    можно всё же задать явно (например, если когда-нибудь понадобится
    другой provider_code для API, отличный от того, что в подписке)."""
    value = get_setting('happ_proxy_provider_code', '')
    if value:
        return value
    return get_happ_provider_id()


def set_happ_proxy_provider_code(provider_code: str) -> None:
    set_setting('happ_proxy_provider_code', provider_code.strip())


def get_happ_proxy_auth_key() -> Optional[str]:
    """auth_key для API happ-proxy.com (32 символа [-_A-Za-z0-9]{32})."""
    value = get_setting('happ_proxy_auth_key', '')
    return value if value else None


def set_happ_proxy_auth_key(auth_key: str) -> None:
    set_setting('happ_proxy_auth_key', auth_key.strip())


def get_happ_install_limit() -> int:
    """Лимит устройств по умолчанию для новых лимитированных ссылок
    (install_limit, 1..100 по докам happ-proxy.com API). 1 по умолчанию —
    самый строгий вариант (ссылку/подписку можно поставить ровно на одно
    устройство), соответствует тому, как продаются наши ключи (один
    ключ = один клиент)."""
    value = get_setting('happ_install_limit', '1')
    try:
        limit = int(value)
    except (TypeError, ValueError):
        return 1
    return limit if 1 <= limit <= 100 else 1


def set_happ_install_limit(limit: int) -> None:
    set_setting('happ_install_limit', str(max(1, min(100, int(limit)))))


def get_happ_proxy_registered_domain_hash() -> Optional[str]:
    """SHA-256 домена, который мы последним успешно зарегистрировали в
    happ-proxy.com через add-domain. Используется, чтобы не дёргать API
    повторно на каждый импорт, если домен сайта не менялся."""
    value = get_setting('happ_proxy_registered_domain_hash', '')
    return value if value else None


def set_happ_proxy_registered_domain_hash(domain_hash: str) -> None:
    set_setting('happ_proxy_registered_domain_hash', domain_hash)


HAPP_ROUTING_MODES = ('disabled', 'add', 'onadd', 'off')


def get_happ_routing_mode() -> str:
    """Режим заголовка 'routing' в подписке Happ (happ.su/main/dev-docs
    → «Геонастройки / Routing»):
      'disabled' — заголовок не отправляется вообще (по умолчанию)
      'add'      — happ://routing/add/{base64} — добавляет профиль, но не
                   активирует его автоматически (если он не единственный)
      'onadd'    — happ://routing/onadd/{base64} — добавляет и сразу
                   активирует профиль, даже если уже есть другой активный
      'off'      — happ://routing/off — отключает маршрутизацию у клиента
    'add'/'onadd' требуют заданного happ_routing_profile_json."""
    value = get_setting('happ_routing_mode', 'disabled')
    return value if value in HAPP_ROUTING_MODES else 'disabled'


def set_happ_routing_mode(mode: str) -> None:
    if mode not in HAPP_ROUTING_MODES:
        raise ValueError(f"Недопустимый режим маршрутизации Happ: {mode}")
    set_setting('happ_routing_mode', mode)


def get_happ_routing_profile_json() -> Optional[str]:
    """JSON-профиль маршрутизации Happ (см. структуру профиля в доках:
    поля Name/GlobalProxy/RemoteDNS.../DirectSites/ProxySites/BlockSites
    и т.д.) — хранится как есть, отправляется закодированным в base64 в
    заголовке 'routing' подписки, когда happ_routing_mode = add/onadd."""
    value = get_setting('happ_routing_profile_json', '')
    return value if value else None


def set_happ_routing_profile_json(profile_json: str) -> None:
    set_setting('happ_routing_profile_json', profile_json.strip())
    set_setting('routing_force_publish', _force_token())


def get_menu_min_width() -> int:
    """Минимальная ширина текста меню в символах (0 — выключено)."""
    try:
        raw = get_setting('menu_min_width', None)
        if raw in (None, ''):
            return 40
        return max(0, min(80, int(str(raw).strip())))
    except Exception:
        return 40


def set_menu_min_width(value: int) -> None:
    set_setting('menu_min_width', str(max(0, min(80, int(value)))))


def get_happ_routing_profile_name() -> str:
    """Переопределение поля 'Name' дефолтного профиля ECLIPSE (см.
    get_default_happ_routing_profile_json). Если не задано — используется
    get_effective_brand_name(), чтобы у каждого белолейбл-админа профиль
    назывался брендом его бота, а не жёстко 'ECLIPSE RU'."""
    return get_setting('happ_routing_profile_name', '')


def set_happ_routing_profile_name(name: str) -> None:
    set_setting('happ_routing_profile_name', name.strip())
    set_setting('routing_force_publish', _force_token())


_HAPP_ROUTING_DEFAULT_TEMPLATE = {
    "GlobalProxy": "true",
    "UseChunkFiles": "true",
    "RemoteDNSType": "DoH",
    "RemoteDNSDomain": "https://8.8.8.8/dns-query",
    "RemoteDNSIP": "8.8.8.8",
    "DomesticDNSType": "DoH",
    "DomesticDNSDomain": "https://77.88.8.8/dns-query",
    "DomesticDNSIP": "77.88.8.8",
    "Geoipurl": "https://raw.githubusercontent.com/pomah7878-bot/eclipse-routing/main/geo/geoip.dat",
    "Geositeurl": "https://raw.githubusercontent.com/pomah7878-bot/eclipse-routing/main/geo/geosite.dat",
    "RouteOrder": "block-proxy-direct",
    "DirectSites": ["geosite:private", "geosite:category-ru", "geosite:whitelist"],
    "DirectIp": ["geoip:private", "geoip:direct"],
    "ProxySites": [],
    "ProxyIp": [],
    "BlockSites": [],
    "BlockIp": [],
    "DomainStrategy": "IPIfNonMatch",
    "FakeDNS": "false",
}


# --- live geo profile v1.143 ---
_LIVE_PROFILE_URL = "https://raw.githubusercontent.com/pomah7878-bot/eclipse-routing/main/HAPP/DEFAULT.JSON"
INCY_AUTOROUTING_URL = _LIVE_PROFILE_URL
# Варианты дефолтного профиля: «Умный» (RU напрямую, остальное через прокси) и «БС»
# (напрямую только банки/госсервисы из whitelist, всё остальное через прокси)
_LIVE_PROFILE_URLS = {
    'smart': _LIVE_PROFILE_URL,
    'whitelist': _LIVE_PROFILE_URL.replace('DEFAULT.JSON', 'WHITELIST.JSON'),
}
HAPP_ROUTING_VARIANTS = ('smart', 'whitelist')
_LIVE_PROFILE_TTL = 3600
_live_profile_cache = {v: {"data": None, "ts": 0.0, "busy": False} for v in HAPP_ROUTING_VARIANTS}


def _force_token() -> str:
    import time as _time
    return str(_time.time_ns())


def get_routing_user_choice_enabled() -> bool:
    """Может ли клиент сам выбрать вариант маршрутизации (кнопки на карточке ключа)."""
    return get_setting('routing_user_choice', '1') != '0'


def set_routing_user_choice_enabled(value: bool) -> None:
    set_setting('routing_user_choice', '1' if value else '0')


def get_user_routing_variant(telegram_id) -> Optional[str]:
    """Личный выбор клиента или None («как у всех»)."""
    value = get_setting(f'routing_variant_user:{int(telegram_id)}', '')
    return value if value in HAPP_ROUTING_VARIANTS else None


def set_user_routing_variant(telegram_id, variant: Optional[str]) -> None:
    if variant is not None and variant not in HAPP_ROUTING_VARIANTS:
        raise ValueError(f"Недопустимый вариант маршрутизации: {variant}")
    set_setting(f'routing_variant_user:{int(telegram_id)}', variant or '')
    set_setting(f'routing_variant_user_ts:{int(telegram_id)}', str(int(__import__('time').time())))


def routing_min_ts(telegram_id=None) -> int:
    """Нижняя граница LastUpdated: время последней смены варианта (админом или самим клиентом).
    Happ/INCY принимают профиль с тем же Name только при БОЛЬШЕМ LastUpdated, а у варианта
    «БС» своя метка публикации — без этого при переключении клиент мог бы не применить новый."""
    def _i(key):
        try:
            return int(get_setting(key, '') or 0)
        except Exception:
            return 0
    own = _i(f'routing_variant_user_ts:{int(telegram_id)}') if telegram_id else 0
    return max(_i('routing_variant_epoch'), own)


def resolve_routing_variant(telegram_id=None) -> str:
    """Вариант профиля для клиента: личный выбор (если разрешён и нет своего JSON у админа),
    иначе общий вариант из админки."""
    if telegram_id and get_routing_user_choice_enabled() and not get_happ_routing_profile_json():
        try:
            own = get_user_routing_variant(telegram_id)
        except Exception:
            own = None
        if own:
            return own
    return get_happ_routing_variant()


def get_happ_routing_variant() -> str:
    value = get_setting('happ_routing_variant', 'smart')
    return value if value in HAPP_ROUTING_VARIANTS else 'smart'


def set_happ_routing_variant(variant: str) -> None:
    if variant not in HAPP_ROUTING_VARIANTS:
        raise ValueError(f"Недопустимый вариант маршрутизации: {variant}")
    set_setting('happ_routing_variant', variant)
    set_setting('routing_variant_epoch', str(int(__import__('time').time())))
    set_setting('routing_force_publish', _force_token())


def _live_setting_key(variant: str) -> str:
    return 'live_routing_profile' if variant == 'smart' else f'live_routing_profile_{variant}'


def _live_profile_refresh(variant="smart"):
    import json as _json, time as _time, urllib.request as _rq
    try:
        with _rq.urlopen(_LIVE_PROFILE_URLS[variant], timeout=8) as r:
            data = _json.loads(r.read().decode("utf-8"))
        ok = (isinstance(data, dict) and data.get("Geoipurl") and data.get("Geositeurl")
              and isinstance(data.get("DirectSites"), list))
        if ok:
            _live_profile_cache[variant]["data"] = data
            try:
                # переживает перезапуск бота: иначе первые запросы после старта
                # получали бы урезанный встроенный шаблон, а клиенты — смену профиля туда-обратно
                set_setting(_live_setting_key(variant), _json.dumps(data, ensure_ascii=False))
            except Exception:
                pass
            try:
                # версия geo-баз (ETag файлов в GitHub): когда базы обновились, профиль
                # нужно «опубликовать» заново, чтобы клиенты перекачали их (см. prepare_routing_profile)
                vers = []
                for _u in (data.get("Geoipurl"), data.get("Geositeurl")):
                    _rq_head = _rq.Request(_u, method="HEAD")
                    with _rq.urlopen(_rq_head, timeout=8) as _hr:
                        vers.append(_hr.headers.get("ETag") or _hr.headers.get("Last-Modified") or "")
                if all(vers):
                    set_setting('live_geo_version', "|".join(vers))
            except Exception:
                pass
    except Exception:
        pass
    finally:
        _live_profile_cache[variant]["ts"] = _time.time()
        _live_profile_cache[variant]["busy"] = False


def _get_live_profile(variant=None):
    """Свежий профиль из GitHub (кэш, фоновое обновление). None — пока нет данных."""
    import time as _time, threading as _th
    variant = variant or get_happ_routing_variant()
    c = _live_profile_cache[variant]
    if c["data"] is None and c["ts"] == 0.0:
        try:
            import json as _json
            saved = get_setting(_live_setting_key(variant), '')
            if saved:
                c["data"] = _json.loads(saved)
        except Exception:
            pass
    if (_time.time() - c["ts"] > _LIVE_PROFILE_TTL) and not c["busy"]:
        c["busy"] = True
        _th.Thread(target=_live_profile_refresh, args=(variant,), daemon=True).start()
    return c["data"]



def get_default_happ_routing_profile_json(variant=None) -> str:
    """Профиль маршрутизации 'из коробки' — компактные geo-базы ECLIPSE
    (форк mvrvntn/routing, проверено что не превышает лимит памяти туннеля
    на слабых устройствах, в отличие от полного Loyalsoldier v2ray-rules-dat).
    Используется автоматически, пока админ не задал свой JSON-профиль вручную
    (см. set_happ_routing_profile_json). Название профиля (поле 'Name') —
    happ_routing_profile_name, если задано, иначе бренд бота, чтобы у каждого
    белолейбл-админа было своё название без ручной правки JSON."""
    import json as _json
    from database.db_settings import get_effective_brand_name
    name = get_happ_routing_profile_name() or get_effective_brand_name()
    profile = {'Name': name}
    live = _get_live_profile(variant)
    profile.update(_HAPP_ROUTING_DEFAULT_TEMPLATE)
    if live:
        profile.update({k: v for k, v in live.items() if k not in ('Name', 'LastUpdated')})
    return _json.dumps(profile, ensure_ascii=False)


def get_effective_happ_routing_profile_json(variant=None) -> str:
    """JSON, который реально уходит клиенту в заголовке 'routing': свой,
    если админ задал его целиком вручную, иначе — дефолтный ECLIPSE-профиль
    (см. get_default_happ_routing_profile_json)."""
    custom = get_happ_routing_profile_json()
    return custom if custom else get_default_happ_routing_profile_json(variant)


def _in_publish_window() -> bool:
    """Ночное окно публикации изменений маршрутизации (по умолчанию 02:00–06:00
    по Москве). Настройки: routing_publish_window = 'ч1-ч2' (или 'always' — без
    ограничения), routing_publish_utc_offset — смещение часового пояса от UTC."""
    import datetime as _dt
    window = (get_setting('routing_publish_window', '2-6') or '2-6').strip().lower()
    if window in ('always', 'any', '*', ''):
        return True
    try:
        h1, h2 = (int(x) for x in window.split('-', 1))
        off = int(get_setting('routing_publish_utc_offset', '3') or 3)
    except Exception:
        return True
    hour = (_dt.datetime.now(_dt.timezone.utc) + _dt.timedelta(hours=off)).hour
    return (h1 <= hour < h2) if h1 < h2 else (hour >= h1 or hour < h2)


def request_routing_publish() -> None:
    """Опубликовать изменения профиля сразу (при ручной правке админом)."""
    set_setting('routing_force_publish', _force_token())


def prepare_routing_profile(profile_json: str, app: str, variant=None, min_ts: int = 0) -> str:
    """Готовит JSON профиля маршрутизации к отправке клиенту.

    LastUpdated (Unix-время, строкой): Happ и INCY обновляют уже добавленный
    профиль с тем же Name только если пришедшее LastUpdated БОЛЬШЕ сохранённого.
    Без него изменения профиля (новые правила, geo-ссылки) у клиентов не
    применяются. Время меняется ТОЛЬКО когда изменилось содержимое профиля
    или версия geo-баз, а не на каждый запрос.

    Ночная публикация: клиенты перекачивают профиль и geo-базы, когда видят
    новое LastUpdated. Чтобы это происходило не днём, новая версия
    «публикуется» только в ночном окне (см. _in_publish_window), весь остальной
    день клиентам отдаётся последняя опубликованная. Исключения: первая
    публикация, ручная правка профиля админом (request_routing_publish),
    окно 'always'. Если админ сам указал LastUpdated — не трогаем.
    Для INCY дополнительно useChunkFiles (булево, имя из его документации)."""
    import json as _json, hashlib as _hl, time as _time
    try:
        profile = _json.loads(profile_json)
    except Exception:
        return profile_json
    if not isinstance(profile, dict):
        return profile_json
    if not profile.get('LastUpdated'):
        body = {k: v for k, v in profile.items() if k != 'LastUpdated'}
        fingerprint = _json.dumps(body, sort_keys=True, ensure_ascii=False) + "|" + get_setting('live_geo_version', '')
        digest = _hl.sha1(fingerprint.encode('utf-8')).hexdigest()
        variant = variant or get_happ_routing_variant()
        pub_key = 'routing_published' if variant == 'smart' else f'routing_published_{variant}'
        try:
            pub = _json.loads(get_setting(pub_key, '') or '{}')
        except Exception:
            pub = {}
        # токен срочной публикации: у каждого варианта профиля своя «отметка», что он его уже учёл
        token = get_setting('routing_force_publish', '')
        force = bool(token) and pub.get('ftok') != token
        if not pub.get('profile') or (pub.get('digest') != digest and (force or _in_publish_window())):
            pub = {'digest': digest, 'ts': str(int(_time.time())), 'profile': body, 'ftok': token}
            set_setting(pub_key, _json.dumps(pub, ensure_ascii=False))
        elif force:
            pub['ftok'] = token
            set_setting(pub_key, _json.dumps(pub, ensure_ascii=False))
        profile = dict(pub['profile'])
        profile['LastUpdated'] = str(max(int(pub['ts']), int(min_ts or 0)))
    if app == 'incy' and 'useChunkFiles' not in profile:
        chunk = profile.get('UseChunkFiles')
        if chunk is not None:
            profile['useChunkFiles'] = str(chunk).lower() == 'true'
    return _json.dumps(profile, ensure_ascii=False)


CLIENT_APPS = {'happ': 'Happ', 'incy': 'INCY'}

# У Happ и INCY общий движок, но РАЗНЫЕ реальные "Advanced parameter"
# заголовки (проверено по официальной документации обоих: happ.su/main/
# dev-docs/app-management и docs.incy.cc/app-management) — некоторые
# функции есть у одного и нет у другого, а некоторые называются по-разному
# и работают не идентично (например, hide-settings у Happ блокирует
# просмотр/редактирование конфигов серверов, а hide-url у INCY —
# только копирование самой ссылки подписки, конфиги внутри остаются
# видимыми). Поэтому у каждого приложения — СВОЙ список переключателей,
# а не общий на оба.
HAPP_TOGGLE_LABELS = {
    'autoconnect': 'Автовыбор быстрого сервера',
    'sort_ping': 'Автосортировка по пингу',
    'notify_expire': 'Родные уведомления об истечении',
    'auto_update': 'Глобальное автообновление подписок',
    'hide_settings': 'Скрыть настройки серверов',
}

INCY_TOGGLE_LABELS = {
    'sort_ping': 'Автосортировка по пингу',
    'hide_url': 'Скрыть ссылку подписки',
    'announce': 'Баннер о сроке и пробнике',
    'hide_check': 'Скрыть кнопку «Проверка»',
    'fragmentation': 'TCP-фрагментация (против DPI)',
    'autorouting': 'Автообновление маршрутов',
}

# Переключатели, включённые по умолчанию (остальные — выключены).
CLIENT_TOGGLE_DEFAULT_ON = {('incy', 'announce')}

CLIENT_TOGGLE_LABELS_BY_APP = {'happ': HAPP_TOGGLE_LABELS, 'incy': INCY_TOGGLE_LABELS}

# Оставлено для обратной совместимости кода, который ещё не различает
# приложения (используется только для валидации ключа toggle в целом).
CLIENT_TOGGLE_LABELS = {**HAPP_TOGGLE_LABELS, **INCY_TOGGLE_LABELS}


def is_client_toggle_enabled(app: str, toggle: str) -> bool:
    """Раздельные настройки для Happ и INCY — несмотря на общий движок,
    эти два приложения по-разному трактуют одни и те же 'Advanced
    parameter' заголовки (обнаружено на практике: включённая настройка,
    нужная для нормальной работы Happ, ломала импорт в INCY, и
    наоборот) — поэтому каждый переключатель настраивается отдельно
    для каждого приложения, а не один общий на оба сразу.

    app: 'happ' или 'incy'. toggle: 'autoconnect' | 'sort_ping' |
    'notify_expire' | 'auto_update' | 'hide_settings'. Все выключены
    по умолчанию."""
    default = '1' if (app, toggle) in CLIENT_TOGGLE_DEFAULT_ON else '0'
    return get_setting(f'{app}_{toggle}_enabled', default) == '1'


def set_client_toggle_enabled(app: str, toggle: str, enabled: bool) -> None:
    set_setting(f'{app}_{toggle}_enabled', '1' if enabled else '0')


def get_incy_update_interval_hours() -> Optional[int]:
    """Интервал автообновления подписки в INCY, в часах
    (profile-update-interval) — в отличие от Happ, у INCY это ЧИСЛО, а
    не простой переключатель вкл/выкл. None — заголовок не отправляется
    вообще (приложение использует своё собственное поведение по
    умолчанию)."""
    value = get_setting('incy_update_interval_hours', '')
    try:
        return int(value) if value else None
    except ValueError:
        return None


def set_incy_update_interval_hours(hours: Optional[int]) -> None:
    """None или 0 — не отправлять заголовок (сброс к поведению по умолчанию)."""
    set_setting('incy_update_interval_hours', str(hours) if hours else '')


def get_zvonok_public_key() -> Optional[str]:
    """API Public Key из личного кабинета zvonok.com — нужен для
    верификации номера телефона на сайте перед выдачей пробного
    периода (защита от повторного получения через сайт после бота).
    Каждая white-label инсталляция регистрирует свой собственный
    аккаунт zvonok.com — общий ключ использовать нельзя."""
    value = get_setting('zvonok_public_key', '')
    return value if value else None


def set_zvonok_public_key(public_key: str) -> None:
    set_setting('zvonok_public_key', public_key.strip())


def get_zvonok_campaign_id() -> Optional[str]:
    """ID кампании типа 'Звонок на проверочный номер' в zvonok.com."""
    value = get_setting('zvonok_campaign_id', '')
    return value if value else None


def set_zvonok_campaign_id(campaign_id: str) -> None:
    set_setting('zvonok_campaign_id', campaign_id.strip())


def get_zvonok_pincode_campaign_id() -> Optional[str]:
    """ID ОТДЕЛЬНОЙ кампании типа 'Ввод кода при звонке' (пин-код) в
    zvonok.com — НЕ тот же campaign_id, что для 'Звонок на проверочный
    номер' (у каждого типа кампании свой собственный ID, оба
    создаются отдельно в личном кабинете zvonok.com)."""
    value = get_setting('zvonok_pincode_campaign_id', '')
    return value if value else None


def set_zvonok_pincode_campaign_id(campaign_id: str) -> None:
    set_setting('zvonok_pincode_campaign_id', campaign_id.strip())


def get_zvonok_flashcall_real_campaign_id() -> Optional[str]:
    """ID ОТДЕЛЬНОЙ кампании типа 'Flash Call' в zvonok.com — Zvonok
    сам звонит клиенту, код подтверждения — последние 4 цифры номера,
    с которого поступил звонок (клиенту даже не нужно отвечать)."""
    value = get_setting('zvonok_flashcall_real_campaign_id', '')
    return value if value else None


def set_zvonok_flashcall_real_campaign_id(campaign_id: str) -> None:
    set_setting('zvonok_flashcall_real_campaign_id', campaign_id.strip())


def get_zvonok_voice_code_campaign_id() -> Optional[str]:
    """ID ОТДЕЛЬНОЙ кампании типа 'Диктовка кода роботом' в zvonok.com
    — робот сам звонит клиенту и произносит код, клиент вводит
    услышанное НА НАШЕМ САЙТЕ (а не на клавиатуре телефона, в отличие
    от способа 'pincode')."""
    value = get_setting('zvonok_voice_code_campaign_id', '')
    return value if value else None


def set_zvonok_voice_code_campaign_id(campaign_id: str) -> None:
    set_setting('zvonok_voice_code_campaign_id', campaign_id.strip())


def get_zvonok_press_digit_campaign_id() -> Optional[str]:
    """ID ОТДЕЛЬНОЙ кампании типа 'Подтверждение звонком' в zvonok.com
    — робот звонит клиенту и просит нажать конкретную цифру для
    подтверждения (без диктовки кода)."""
    value = get_setting('zvonok_press_digit_campaign_id', '')
    return value if value else None


def set_zvonok_press_digit_campaign_id(campaign_id: str) -> None:
    set_setting('zvonok_press_digit_campaign_id', campaign_id.strip())


def get_zvonok_proxy_url():
    """HTTP(S)-прокси для запросов к zvonok.com — обходит гео-редирект
    на callo.com для серверов вне России. Пусто — прокси не используется."""
    value = get_setting('zvonok_proxy_url', '')
    return value or None


def set_zvonok_proxy_url(proxy_url: str) -> None:
    set_setting('zvonok_proxy_url', proxy_url.strip())


ZVONOK_METHODS = {
    'flash_call': '☎️ Звонок на проверочный номер',
    'flashcall_real': '⚡ Flash Call',
    'pincode': '📟 Ввод кода при звонке',
    'voice_code': '🗣 Диктовка кода роботом',
    'press_digit': '🔢 Подтверждение звонком',
}

# Тип кампании на самом zvonok.com для каждого способа — используется
# для прямой ссылки "Создать кампанию" в подсказках бота, чтобы админ
# не путал типы при создании (обнаружено на практике — Campaign ID
# для "Подтверждение звонком" оказался кампанией типа "Ввод кода").
ZVONOK_CAMPAIGN_TYPE_SLUGS = {
    'flash_call': 'pincode_incoming',
    'flashcall_real': 'flashcall',
    'pincode': 'pincode',
    'voice_code': 'tellcode',
    'press_digit': 'pincode_short',
}


def get_zvonok_verification_method() -> str:
    """Какой способ верификации звонком использовать при входе по
    телефону на сайте — все 5 официальных способов Zvonok:

      'flash_call'     — клиент сам звонит на один из наших служебных
                          номеров, узнаём его по номеру звонящего
      'flashcall_real' — настоящий Flash Call: Zvonok сам звонит
                          клиенту, код — последние 4 цифры номера,
                          с которого поступил звонок (отвечать не нужно)
      'pincode'        — мы звоним клиенту, показываем код на сайте
                          заранее, клиент вводит его с клавиатуры
                          телефона во время звонка
      'voice_code'      — робот звонит и ПРОИЗНОСИТ код клиенту, тот
                          вводит услышанное У НАС НА САЙТЕ
      'press_digit'    — робот звонит и просит нажать конкретную
                          цифру для подтверждения (без кода)

    Каждый способ требует СВОЙ, отдельно созданный Campaign ID в
    личном кабинете zvonok.com — у каждого типа кампании свой ID,
    их нельзя использовать вперемешку."""
    value = get_setting('zvonok_verification_method', 'flash_call')
    return value if value in ZVONOK_METHODS else 'flash_call'


def set_zvonok_verification_method(method: str) -> None:
    if method not in ZVONOK_METHODS:
        raise ValueError(f"Неизвестный способ верификации: {method}")
    set_setting('zvonok_verification_method', method)


SITE_AUTH_METHODS = {
    'google': '🔵 Google OAuth',
    'yandex': '🟡 Яндекс OAuth',
    'vk': '🔷 VK OAuth',
    'code': '🔑 Вход по коду из бота',
    'phone': '📱 Вход по номеру телефона',
}


def is_site_auth_method_enabled(method: str) -> bool:
    """Включён ли конкретный способ входа на сайте (независимо от того,
    настроены ли для него учётные данные — это ОТДЕЛЬНЫЙ переключатель).
    По умолчанию включены все — чтобы не менять поведение для тех, кто
    уже настроил OAuth/пользуется входом по коду."""
    if method not in SITE_AUTH_METHODS:
        return False
    value = get_setting(f'site_auth_{method}_enabled', '1')
    return value != '0'


def set_site_auth_method_enabled(method: str, enabled: bool) -> None:
    if method not in SITE_AUTH_METHODS:
        raise ValueError(f"Неизвестный способ входа: {method}")
    set_setting(f'site_auth_{method}_enabled', '1' if enabled else '0')


CABINET_IMPORT_APPS = {
    'happ': '🚀 Happ',
    'incy': '⚡️ INCY',
    'karing': '🎯 Karing',
}


def is_cabinet_import_app_enabled(app: str) -> bool:
    """Показывать ли в личном кабинете (веб-кабинет /, сайт /shop) кнопку
    импорта подписки в конкретное приложение (Happ/INCY/Karing). Это
    ОТДЕЛЬНЫЙ переключатель от лицензионной фичи app_import — сама функция
    импорта может быть куплена в лицензии, но админ конкретной установки
    вправе показывать не все три кнопки, а только часть из них. По
    умолчанию включены все три — не меняет поведение для тех, кто уже
    пользуется импортом."""
    if app not in CABINET_IMPORT_APPS:
        return False
    return get_setting(f'cabinet_import_{app}_enabled', '1') != '0'


def set_cabinet_import_app_enabled(app: str, enabled: bool) -> None:
    if app not in CABINET_IMPORT_APPS:
        raise ValueError(f"Неизвестное приложение импорта: {app}")
    set_setting(f'cabinet_import_{app}_enabled', '1' if enabled else '0')


def is_demo_payment_enabled() -> bool:
    """Is demo payment by RF card included?"""
    return get_setting('demo_payment_enabled', '0') == '1'


# ============================================================================
# ИНТЕГРАЦИИ, НАСТРАИВАЕМЫЕ ЧЕРЕЗ АДМИН-ПАНЕЛЬ
# (домен сайта, AI, OAuth) — приоритет над config.py/secrets.env, чтобы можно
# было донастроить бота после установки, если данные не были даны сразу.
# ============================================================================

DEVICE_LIMIT_TYPES = {
    'ip': {'label': '🌐 По IP-адресам', 'description': 'Классическое ограничение — считает уникальные IP-адреса подключений'},
    'hwid': {'label': '📱 По устройствам (HWID)', 'description': 'Панель запоминает устройство при запросе страницы подписки. Работает только со встроенной подпиской 3x-ui. ⚠️ Happ и INCY на одном телефоне считаются РАЗНЫМИ устройствами, поэтому лимит может закончиться быстрее, чем кажется'},
}


def get_device_limit_type() -> str:
    """Тип ограничения количества устройств на ключ — 'ip' (по умолчанию,
    классический способ) или 'hwid' (по фиксации устройства при запросе
    подписки — поддерживается панелью 3x-ui). Общая настройка для всех
    тарифов и ключей, не за каждый тариф отдельно."""
    value = get_setting('device_limit_type', 'ip')
    return value if value in DEVICE_LIMIT_TYPES else 'ip'


def set_device_limit_type(value: str) -> None:
    """Задаёт тип ограничения устройств — 'ip' или 'hwid'."""
    if value not in DEVICE_LIMIT_TYPES:
        raise ValueError(f"Неизвестный тип ограничения устройств: {value}")
    set_setting('device_limit_type', value)

def get_effective_webapp_url() -> str:
    """Домен сайта/WebApp. Значение из админки имеет приоритет над config.py.

    Игнорирует нетронутый плейсхолдер "https://ваш-домен.example.com" из
    config.py.example — иначе свежая установка, где админ ещё не настроил
    домен ни в БД, ни в config.py, ошибочно считала бы, что домен уже
    настроен, и пыталась бы использовать несуществующий адрес (например,
    в прокси-ссылках подписки для Happ)."""
    from_db = get_setting('webapp_url', '')
    if from_db:
        return from_db
    try:
        from config import WEBAPP_URL
        if WEBAPP_URL and WEBAPP_URL.strip() and WEBAPP_URL.strip() != 'https://ваш-домен.example.com':
            return WEBAPP_URL.strip()
    except ImportError:
        pass
    return ''

def get_effective_brand_name() -> str:
    """Название сервиса — используется в текстах AI-помощника (самоописание,
    какой сервис он представляет). Значение из админки имеет приоритет над
    config.py; если не задано нигде — 'ECLIPSE Unlimited' как исторический
    дефолт (это сохраняет поведение уже работающих установок без AI-brand
    настроек в config.py/БД без каких-либо действий с их стороны)."""
    from_db = get_setting('brand_name', '')
    if from_db:
        return from_db
    try:
        from config import BRAND_NAME
        if BRAND_NAME and BRAND_NAME.strip() and BRAND_NAME != 'ВАШ_БРЕНД':
            return BRAND_NAME.strip()
    except ImportError:
        pass
    return 'ECLIPSE Unlimited'

def set_brand_name(name: str) -> None:
    """Задаёт название сервиса для текстов AI-помощника."""
    set_setting('brand_name', name.strip())

def get_effective_turnstile_site_key() -> str:
    """Публичный sitekey Cloudflare Turnstile для виджета капчи на /shop.
    Значение из админки имеет приоритет над secrets.env; ключ ПРИВЯЗАН
    К ДОМЕНУ в кабинете Cloudflare — при смене домена сайта нужен новый
    ключ, иначе виджет капчи откажется грузиться (ошибка Cloudflare)."""
    from_db = get_setting('turnstile_site_key', '')
    if from_db:
        return from_db
    import os
    return os.environ.get('TURNSTILE_SITE_KEY', '').strip()

def set_turnstile_site_key(value: str) -> None:
    """Задаёт публичный sitekey Cloudflare Turnstile для витрины /shop."""
    set_setting('turnstile_site_key', value.strip())

def get_effective_own_app_name() -> str:
    """Название собственного Android-приложения (если есть) — AI-помощник
    рекомендует его в первую очередь при вопросах о подключении. См.
    get_effective_own_app_url() про три состояния (БД / нет в config.py /
    явно пусто в config.py) — та же логика."""
    from_db = get_setting('own_app_name', '')
    if from_db:
        return from_db
    try:
        from config import OWN_APP_NAME
    except ImportError:
        return 'ECLIPSE Unlimited' if get_effective_own_app_url() else ''
    return OWN_APP_NAME.strip() if OWN_APP_NAME else ''

def set_own_app_name(name: str) -> None:
    """Задаёт название собственного Android-приложения (пусто — не рекомендовать)."""
    set_setting('own_app_name', name.strip())

def get_effective_own_app_url() -> str:
    """Ссылка на собственное Android-приложение (если есть).

    Три состояния: 1) задано в БД через админку — используем его; 2) в
    config.py константы OWN_APP_URL нет вообще (старая установка, не
    знающая об этой настройке) — сохраняем исторический дефолт ECLIPSE
    Unlimited, чтобы поведение не изменилось без действий админа; 3) в
    config.py константа ЕСТЬ, но пустая — админ явно указал, что своего
    приложения нет, возвращаем пустую строку (а не проваливаемся к
    дефолту).
    """
    from_db = get_setting('own_app_url', '')
    if from_db:
        return from_db
    try:
        from config import OWN_APP_URL
    except ImportError:
        # Чужим установкам без константы своё приложение автора не рекомендуем.
        return ''
    return OWN_APP_URL.strip() if OWN_APP_URL else ''

def set_own_app_url(url: str) -> None:
    """Задаёт ссылку на собственное Android-приложение (пусто — не рекомендовать)."""
    set_setting('own_app_url', url.strip())

def set_webapp_url(url: str) -> None:
    """Сохраняет домен сайта/WebApp, заданный через админ-панель."""
    set_setting('webapp_url', url.strip().rstrip('/'))


def get_webapp_url_backup() -> str:
    """Резервный домен сайта — второй, заранее настроенный адрес (свои DNS
    A-запись + SSL-сертификат + тот же nginx/бот-бэкенд), указывающий на
    тот же сервер. Держится наготове на случай блокировки основного
    домена: одна кнопка "Сделать основным" — и все НОВЫЕ ссылки подписки
    сразу используют его, без правки кода и без перезапуска бота."""
    return get_setting('webapp_url_backup', '') or ''


def set_webapp_url_backup(url: str) -> None:
    """Сохраняет резервный домен сайта."""
    set_setting('webapp_url_backup', url.strip().rstrip('/'))


def swap_webapp_url_with_backup() -> None:
    """Меняет местами основной и резервный домен сайта. Один вызов —
    и резервный домен становится основным (используется для всех НОВЫХ
    ссылок подписки), а прежний основной становится резервным (остаётся
    под рукой, если понадобится откатиться назад)."""
    primary = get_setting('webapp_url', '') or ''
    backup = get_setting('webapp_url_backup', '') or ''
    set_setting('webapp_url', backup)
    set_setting('webapp_url_backup', primary)


def is_ai_support_enabled() -> bool:
    """Включён ли AI-суппорт админом. Отдельно от лицензии (is_feature_
    available('ai_assistant')) — партнёр с оплаченной функцией всё равно
    может временно выключить её сам (например, чтобы не тратить лимиты
    Groq/Gemini), не теряя саму лицензию. По умолчанию включён, если
    ключи вообще заданы."""
    return get_setting('ai_support_enabled', '1') == '1'


def set_ai_support_enabled(enabled: bool) -> None:
    set_setting('ai_support_enabled', '1' if enabled else '0')


def get_cloudflare_api_token() -> str:
    """API-токен Cloudflare для автосоздания DNS A-записи резервного
    домена. Не обязателен — без него автонастройка просто попросит
    добавить A-запись вручную (один раз) и дальше сделает nginx+SSL сама."""
    return get_setting('cloudflare_api_token', '') or ''


def set_cloudflare_api_token(token: str) -> None:
    set_setting('cloudflare_api_token', token.strip())

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


def get_effective_gemini_api_key() -> str:
    """Ключ Google Gemini для резервного лейна AI-консультанта. Значение из
    админки имеет приоритет над переменной окружения GEMINI_API_KEY."""
    from_db = get_setting('gemini_api_key', '')
    if from_db:
        return from_db
    import os
    return os.environ.get('GEMINI_API_KEY', '')


def set_gemini_api_key(api_key: str) -> None:
    """Сохраняет ключ Gemini, заданный через админ-панель."""
    set_setting('gemini_api_key', api_key.strip())

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

