"""Платформа клиента: откуда пришёл пользователь или заказ.

Единая таксономия для статистики и будущих функций:
telegram — Telegram-бот, web — сайт/браузер, android — Android-приложение,
android_tv — Android TV, windows — Windows-клиент, ios — iOS, app — нативное
приложение без уточнения платформы, unknown — не определили.

Приоритет: явный заголовок X-Eclipse-Platform → User-Agent → X-Eclipse-App.
"""
from typing import Any, Dict

__all__ = [
    'PLATFORM_LABELS',
    'PLATFORM_ORDER',
    'normalize_platform',
    'detect_request_platform',
    'platform_label',
    'is_native_platform',
]

PLATFORM_ORDER = ('telegram', 'web', 'android', 'android_tv', 'windows', 'ios', 'app', 'unknown')

PLATFORM_LABELS = {
    'telegram': '🈴 Telegram-бот',
    'web': '🌐 Сайт',
    'android': '📱 Android',
    'android_tv': '📺 Android TV',
    'windows': '🖥 Windows',
    'ios': '🍎 iOS',
    'app': '📲 Приложение',
    'unknown': '❔ Неизвестно',
}

_ALIASES = {
    'telegram': 'telegram', 'tg': 'telegram', 'bot': 'telegram',
    'web': 'web', 'site': 'web', 'browser': 'web',
    'android': 'android', 'android_app': 'android', 'phone': 'android',
    'android_tv': 'android_tv', 'android-tv': 'android_tv', 'tv': 'android_tv',
    'windows': 'windows', 'win': 'windows', 'desktop': 'windows',
    'ios': 'ios', 'iphone': 'ios', 'ipad': 'ios',
    'app': 'app', 'native': 'app',
    'unknown': 'unknown', '': 'unknown',
}


def normalize_platform(value: Any) -> str:
    return _ALIASES.get(str(value or '').strip().lower(), 'unknown')


def platform_label(platform: Any) -> str:
    return PLATFORM_LABELS.get(normalize_platform(platform), PLATFORM_LABELS['unknown'])


def is_native_platform(platform: Any) -> bool:
    return normalize_platform(platform) in ('android', 'android_tv', 'windows', 'ios', 'app')


def detect_request_platform(request: Any) -> str:
    """Платформа HTTP-запроса (сайт/приложение). По умолчанию web."""
    headers = getattr(request, 'headers', {}) or {}

    def _get(name: str) -> str:
        try:
            return str(headers.get(name) or '')
        except Exception:  # noqa: BLE001
            return ''

    explicit = normalize_platform(_get('X-Eclipse-Platform'))
    if explicit != 'unknown':
        return explicit

    ua = _get('User-Agent')
    low = ua.lower()
    if 'eclipse-windows' in low:
        return 'windows'
    if 'eclipse-android' in low:
        import re
        return 'android_tv' if re.search(r'-tv(?:[\s/]|$)', low) or 'eclipse tv' in low else 'android'
    if 'eclipse tv' in low:
        return 'android_tv'
    if any(marker in low for marker in ('iphone', 'ipad', 'ios ', 'ios/', 'darwin')):
        return 'ios'
    if _get('X-Eclipse-App') == '1':
        return 'app'
    return 'web'
