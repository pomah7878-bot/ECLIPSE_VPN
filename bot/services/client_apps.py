"""Распознавание клиентских приложений по User-Agent.

Нужно для пробного периода: понять, из какого клиента пришло устройство
(ECLIPSE TV, Happ, INCY, v2rayTun и т.д.), чтобы:
- показать админу источник повторного пробника;
- применять проверку устройства только к реальным клиентам, а не к
  браузерным предпросмотрам ссылок.

Модуль чистый: только разбор строки, без сети и БД.
"""
import re
from typing import Dict

__all__ = ['detect_client', 'is_android']

# Порядок важен: ECLIPSE TV проверяем раньше ECLIPSE VPN.
_PATTERNS = (
    ('eclipse_tv', re.compile(r'(ECLIPSE[\s_.-]*TV|ECLIPSE-Android/[^\s]*-tv(?:\s|$))', re.I), 'ECLIPSE TV', 'android'),
    ('eclipse_vpn', re.compile(r'(ECLIPSE[\s_.-]*(?:VPN|UNLIMITED)|ECLIPSE-Android/)', re.I), 'ECLIPSE VPN', 'android'),
    ('happ', re.compile(r'(?:^|[\s(])Happ(?:/|\s|$)', re.I), 'Happ', 'android/ios'),
    ('incy', re.compile(r'\bINCY\b', re.I), 'INCY', 'android/ios'),
    ('v2raytun', re.compile(r'v2rayTun', re.I), 'v2rayTun', 'android/ios'),
    ('v2rayng', re.compile(r'v2rayNG', re.I), 'v2rayNG', 'android'),
    ('v2box', re.compile(r'V2Box', re.I), 'V2Box', 'android/ios'),
    ('streisand', re.compile(r'Streisand', re.I), 'Streisand', 'ios'),
    ('shadowrocket', re.compile(r'Shadowrocket', re.I), 'Shadowrocket', 'ios'),
    ('singbox', re.compile(r'sing-box', re.I), 'sing-box', 'android/ios'),
    ('nekobox', re.compile(r'NekoBox', re.I), 'NekoBox', 'android'),
    ('hiddify', re.compile(r'Hiddify', re.I), 'Hiddify', 'android/ios'),
    ('clash', re.compile(r'Clash|Mihomo', re.I), 'Clash', 'android/ios'),
    ('browser', re.compile(r'Mozilla/|AppleWebKit/', re.I), 'Браузер', 'web'),
    ('telegram', re.compile(r'Telegram', re.I), 'Telegram', ''),
)

_UNKNOWN = {'key': 'unknown', 'label': 'Неизвестный клиент', 'platform': '', 'android': False}


def detect_client(user_agent: str) -> Dict[str, object]:
    """Ключ, подпись, платформа и признак Android для User-Agent."""
    ua = (user_agent or '').strip()
    low = ua.lower()
    android = 'android' in low
    ios = any(marker in low for marker in ('iphone', 'ipad', 'ios ', 'ios/', 'darwin'))
    if not ua:
        return dict(_UNKNOWN, android=android)
    for key, pattern, label, platform in _PATTERNS:
        if pattern.search(ua):
            if platform == 'android/ios':
                platform = 'ios' if ios else ('android' if android else 'android/ios')
            return {'key': key, 'label': label, 'platform': platform, 'android': android or platform == 'android'}
    return dict(_UNKNOWN, android=android)


def is_android(user_agent: str) -> bool:
    """Android-клиент по UA (в том числе незнакомый, но с Android в строке)."""
    return 'android' in (user_agent or '').lower()
