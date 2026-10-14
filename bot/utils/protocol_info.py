"""Справочник протоколов: что это такое и когда подходит — для показа клиенту.

Тексты короткие, без жаргона. status: "ready" — работает у нас; "soon" — готовимся,
пока не выдаётся клиентам.
"""
from typing import Any, Dict, Optional

PROTOCOLS: Dict[str, Dict[str, str]] = {
    "vless": {
        "title": "VLESS",
        "summary": "Современный быстрый протокол. Обычно работает вместе с REALITY или TLS и выглядит как посещение обычного сайта.",
        "good_for": "Основной выбор для большинства: высокая скорость и стабильность.",
        "note": "Поддерживается почти всеми современными приложениями.",
        "status": "ready",
    },
    "vmess": {
        "title": "VMess",
        "summary": "Старый протокол семейства V2Ray. Работает во всех приложениях, но менее современный, чем VLESS.",
        "good_for": "Старые приложения и устройства, где нет VLESS.",
        "note": "Если есть выбор, берите VLESS.",
        "status": "ready",
    },
    "trojan": {
        "title": "Trojan",
        "summary": "Маскируется под обычный защищённый веб-трафик (TLS). Прост и надёжен.",
        "good_for": "Сети, где обычный HTTPS пропускают без проблем.",
        "note": "Поддерживается большинством приложений.",
        "status": "ready",
    },
    "ss": {
        "title": "Shadowsocks",
        "summary": "Лёгкий проверенный протокол с минимальной нагрузкой на устройство.",
        "good_for": "Слабые телефоны и роутеры, мобильный интернет.",
        "note": "Его проще распознать, чем VLESS с REALITY.",
        "status": "ready",
    },
    "hysteria2": {
        "title": "Hysteria2",
        "summary": "Быстрый протокол на базе QUIC (UDP). Хорошо держит скорость на нестабильных и далёких каналах.",
        "good_for": "Видео, загрузки, плохая связь.",
        "note": "Нужен UDP: в некоторых сетях его режут — тогда переключитесь на VLESS.",
        "status": "ready",
    },
    "tuic": {
        "title": "TUIC",
        "summary": "Протокол на базе QUIC (UDP) с малой задержкой. Похож на Hysteria2.",
        "good_for": "Игры и звонки, где важен отклик.",
        "note": "Нужен UDP и приложение с поддержкой TUIC.",
        "status": "ready",
    },
    "amneziawg": {
        "title": "AmneziaWG",
        "summary": "Улучшенный WireGuard с маскировкой трафика: обходит блокировки, которые распознают обычный WireGuard.",
        "good_for": "Высокая скорость на смартфоне и компьютере.",
        "note": "Работает через приложение AmneziaVPN или AmneziaWG. Один файл конфигурации — одно устройство.",
        "status": "ready",
    },
    "wireguard": {
        "title": "WireGuard",
        "summary": "Быстрый современный VPN на уровне системы.",
        "good_for": "Скорость и экономия заряда там, где WireGuard не блокируют.",
        "note": "Его легко распознают и блокируют, один файл конфигурации — одно устройство.",
        "status": "ready",
    },
    "masque": {
        "title": "MASQUE",
        "summary": "Новый стандарт интернет-протоколов (IETF): VPN-туннель внутри HTTP/3, снаружи похожий на обычный сайт.",
        "good_for": "Сети, где обычный веб-трафик разрешён, а привычные VPN-протоколы режут.",
        "note": "Пока готовимся: появится у нас, как только его поддержат популярные приложения. Нужен UDP.",
        "status": "soon",
    },
}

_ALIASES = {"shadowsocks": "ss", "hy2": "hysteria2", "awg": "amneziawg"}


def get_protocol_info(protocol: Optional[str]) -> Optional[Dict[str, Any]]:
    """Описание протокола по ключу или названию (регистр не важен)."""
    key = str(protocol or "").strip().lower()
    key = _ALIASES.get(key, key)
    info = PROTOCOLS.get(key)
    return dict(info, key=key) if info else None


def protocol_summary(protocol: Optional[str]) -> str:
    info = get_protocol_info(protocol)
    return info["summary"] if info else ""
