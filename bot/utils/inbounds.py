"""
General rules for working with inbound panels.
"""
import json
import time
from typing import Any, Dict, FrozenSet, List, Optional, Tuple


IGNORED_INBOUND_PREFIX = "--!"
CDN_INBOUND_MARKER = "[CDN]"
_CDN_IDS_TTL_SECONDS = 15.0
_cdn_ids_cache: Tuple[float, FrozenSet[int]] = (0.0, frozenset())
MTPROTO_PROTOCOL = "mtproto"
MASQUE_PROTOCOL = "masque"
_MASQUE_TTL_SECONDS = 10.0
_masque_cache: Tuple[float, bool] = (0.0, False)


def inbound_protocol(inbound: Dict[str, Any]) -> str:
    """Return a normalized panel protocol name."""
    if not isinstance(inbound, dict):
        return ""
    return str(inbound.get("protocol") or "").strip().lower()


def is_mtproto_inbound(inbound: Dict[str, Any]) -> bool:
    """Whether the inbound is an MTProto proxy rather than a regular VPN key."""
    return inbound_protocol(inbound) == MTPROTO_PROTOCOL


def is_masque_inbound(inbound: Dict[str, Any]) -> bool:
    """Whether the inbound is MASQUE (CONNECT-IP). Клиенты в нём входят по email и паролю."""
    return inbound_protocol(inbound) == MASQUE_PROTOCOL


def masque_enabled() -> bool:
    """Включена ли поддержка MASQUE (настройка masque_enabled=1). По умолчанию выключена:
    пока приложения клиентов его не поддерживают, бот такие inbound не трогает."""
    global _masque_cache
    now = time.monotonic()
    cached_at, cached = _masque_cache
    if now - cached_at < _MASQUE_TTL_SECONDS:
        return cached
    value = False
    try:
        from database.db_settings import get_setting
        value = str(get_setting("masque_enabled", "0") or "0").strip() in ("1", "true", "True")
    except Exception:
        value = False
    _masque_cache = (now, value)
    return value


def reset_masque_cache() -> None:
    global _masque_cache
    _masque_cache = (0.0, False)


def filter_regular_inbounds(inbounds: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Return inbounds supported by the single-key flow (MTProto excluded)."""
    return [inbound for inbound in inbounds if not is_mtproto_inbound(inbound)]


def get_cdn_inbound_ids() -> FrozenSet[int]:
    """ID инбаундов, помеченных админом как платный CDN (настройка cdn_inbound_ids)."""
    global _cdn_ids_cache
    now = time.monotonic()
    cached_at, cached = _cdn_ids_cache
    if now - cached_at < _CDN_IDS_TTL_SECONDS:
        return cached
    ids: set = set()
    try:
        from database.db_settings import get_setting
        raw = get_setting("cdn_inbound_ids", "") or ""
        for part in str(raw).replace(";", ",").split(","):
            part = part.strip()
            if part.isdigit():
                ids.add(int(part))
    except Exception:
        ids = set()
    result = frozenset(ids)
    _cdn_ids_cache = (now, result)
    return result


def reset_cdn_inbound_ids_cache() -> None:
    """Сбрасывает кэш ID CDN-инбаундов (после изменения настройки)."""
    global _cdn_ids_cache
    _cdn_ids_cache = (0.0, frozenset())


def is_cdn_inbound(inbound: Dict[str, Any]) -> bool:
    """True, если инбаунд — платный CDN: ID из настройки или слово CDN в названии (remark)."""
    if not isinstance(inbound, dict):
        return False
    remark = str(inbound.get("remark") or "").lstrip()
    if "CDN" in remark.upper():
        return True
    try:
        return int(inbound.get("id")) in get_cdn_inbound_ids()
    except (TypeError, ValueError):
        return False


AWG_PROTOCOL = "amneziawg"
# Параметры расширенного AmneziaWG (2.0+), которых нет у классического «для роутеров».
_AWG_EXTENDED_FIELDS = (
    "s3", "s4", "i1", "i2", "i3", "i4", "i5", "headerProtectionKey",
    "contentPaddingAddition", "keepaliveTimeout", "maxHandshakeAttempts",
    "rejectAfterTime", "rekeyAfterTime", "rekeyTimeout", "randomTrailers", "disableCookies",
)


def is_awg_inbound(inbound: Dict[str, Any]) -> bool:
    return inbound_protocol(inbound) == AWG_PROTOCOL


def is_awg_router_inbound(inbound: Dict[str, Any]) -> bool:
    """AmneziaWG без расширенных параметров. Непарсящиеся settings — обычный AmneziaWG."""
    if not is_awg_inbound(inbound):
        return False
    settings = inbound.get("settings")
    if isinstance(settings, str):
        try:
            settings = json.loads(settings)
        except ValueError:
            return False
    if not isinstance(settings, dict) or not settings:
        return False
    server = settings.get("server")
    params = server if isinstance(server, dict) else settings
    return all(params.get(name) in (None, "", 0, "0", False) for name in _AWG_EXTENDED_FIELDS)


def inbound_allowed_for_tariff(inbound: Dict[str, Any], tariff: Optional[Dict[str, Any]]) -> bool:
    """AmneziaWG выдаётся только тарифам с галочкой; без тарифа и остальные протоколы — всем."""
    if tariff is None or not is_awg_inbound(inbound):
        return True
    column = "awg_router_enabled" if is_awg_router_inbound(inbound) else "awg_enabled"
    # Нет колонки в словаре тарифа (неполный SELECT) — не отнимаем подключение
    return bool(tariff.get(column, 1))


def split_inbounds_for_tariff(
    inbounds: List[Dict[str, Any]], tariff: Optional[Dict[str, Any]],
) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    """Делит инбаунды на разрешённые тарифу и запрещённые, порядок сохраняется."""
    allowed: List[Dict[str, Any]] = []
    denied: List[Dict[str, Any]] = []
    for inbound in inbounds:
        (allowed if inbound_allowed_for_tariff(inbound, tariff) else denied).append(inbound)
    return allowed, denied


def filter_inbounds_for_tariff_id(inbounds: List[Dict[str, Any]], tariff_id: Any) -> List[Dict[str, Any]]:
    """Оставляет инбаунды, разрешённые тарифу по ID. Тариф не найден или ошибка базы — без фильтра."""
    tariff = None
    if tariff_id:
        try:
            from database.db_tariffs import get_tariff_by_id
            tariff = get_tariff_by_id(int(tariff_id))
        except Exception:
            tariff = None
    allowed, _ = split_inbounds_for_tariff(inbounds, tariff)
    return allowed


def is_prefix_ignored_inbound(inbound: Dict[str, Any]) -> bool:
    """True только для инбаундов с префиксом --! (запрет на любое добавление клиентов).

    CDN-инбаунды сюда не входят: в них клиентов создаёт модуль CDN-пакетов."""
    if not isinstance(inbound, dict):
        return False
    remark = inbound.get("remark") or ""
    return str(remark).lstrip().startswith(IGNORED_INBOUND_PREFIX)


def is_ignored_inbound(inbound: Dict[str, Any]) -> bool:
    """True if inbound is hidden from the bot through the prefix at the beginning of remark.

    CDN-инбаунды тоже скрыты от обычной синхронизации: клиенты попадают в них
    только через купленный/выданный CDN-пакет (bot/services/cdn.py)."""
    if not isinstance(inbound, dict):
        return False
    remark = inbound.get("remark") or ""
    if str(remark).lstrip().startswith(IGNORED_INBOUND_PREFIX):
        return True
    return is_cdn_inbound(inbound)


def split_ignored_inbounds(
    inbounds: List[Dict[str, Any]],
) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    """Divides inbounds into those visible to the bot and hidden."""
    visible: List[Dict[str, Any]] = []
    ignored: List[Dict[str, Any]] = []
    for inbound in inbounds:
        if is_ignored_inbound(inbound):
            ignored.append(inbound)
        else:
            visible.append(inbound)
    return visible, ignored


def filter_visible_inbounds(inbounds: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Returns only inbounds without the hidden service prefix."""
    visible, _ = split_ignored_inbounds(inbounds)
    return visible
