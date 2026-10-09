"""
General rules for working with inbound panels.
"""
import time
from typing import Any, Dict, FrozenSet, List, Tuple


IGNORED_INBOUND_PREFIX = "--!"
CDN_INBOUND_MARKER = "[CDN]"
_CDN_IDS_TTL_SECONDS = 15.0
_cdn_ids_cache: Tuple[float, FrozenSet[int]] = (0.0, frozenset())
MTPROTO_PROTOCOL = "mtproto"


def inbound_protocol(inbound: Dict[str, Any]) -> str:
    """Return a normalized panel protocol name."""
    if not isinstance(inbound, dict):
        return ""
    return str(inbound.get("protocol") or "").strip().lower()


def is_mtproto_inbound(inbound: Dict[str, Any]) -> bool:
    """Whether the inbound is an MTProto proxy rather than a regular VPN key."""
    return inbound_protocol(inbound) == MTPROTO_PROTOCOL


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
    """True, если инбаунд — платный CDN: ID из настройки или метка [CDN] в начале remark."""
    if not isinstance(inbound, dict):
        return False
    remark = str(inbound.get("remark") or "").lstrip()
    if remark.upper().startswith(CDN_INBOUND_MARKER):
        return True
    try:
        return int(inbound.get("id")) in get_cdn_inbound_ids()
    except (TypeError, ValueError):
        return False


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
