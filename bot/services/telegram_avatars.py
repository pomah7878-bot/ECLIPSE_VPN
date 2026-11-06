"""Telegram profile photos for the signed-in site's own account.

The bot token never leaves the server. Photos are cached in memory so opening
the Android profile does not call Telegram again on every account refresh.
"""
import logging
import time
from typing import Dict, Optional, Tuple

logger = logging.getLogger(__name__)

_MAX_PHOTO_BYTES = 5 * 1024 * 1024
_PHOTO_TTL_SECONDS = 6 * 60 * 60
_EMPTY_TTL_SECONDS = 10 * 60
_CACHE: Dict[int, Tuple[float, Optional[bytes]]] = {}


def _cached(telegram_id: int) -> Tuple[bool, Optional[bytes]]:
    item = _CACHE.get(telegram_id)
    if not item or item[0] <= time.monotonic():
        _CACHE.pop(telegram_id, None)
        return False, None
    return True, item[1]


def _store(telegram_id: int, photo: Optional[bytes]) -> None:
    ttl = _PHOTO_TTL_SECONDS if photo else _EMPTY_TTL_SECONDS
    _CACHE[telegram_id] = (time.monotonic() + ttl, photo)


async def get_profile_photo(bot, telegram_id: int) -> Optional[bytes]:
    """Returns the user's current Telegram profile photo, or None.

    Missing photos and temporary Telegram errors are deliberately non-fatal:
    account login and VPN loading must keep working without an avatar.
    """
    if bot is None:
        return None
    cached, photo = _cached(telegram_id)
    if cached:
        return photo
    try:
        photos = await bot.get_user_profile_photos(telegram_id, limit=1)
        if not photos.total_count or not photos.photos:
            _store(telegram_id, None)
            return None
        # Telegram returns several sizes; the last one is the largest.
        file_info = await bot.get_file(photos.photos[0][-1].file_id)
        if file_info.file_size and file_info.file_size > _MAX_PHOTO_BYTES:
            logger.warning("Telegram avatar is too large; using the fallback avatar")
            _store(telegram_id, None)
            return None
        downloaded = await bot.download_file(file_info.file_path)
        data = downloaded.read()
        if not data or len(data) > _MAX_PHOTO_BYTES:
            logger.warning("Telegram avatar is empty or too large; using the fallback avatar")
            _store(telegram_id, None)
            return None
        _store(telegram_id, data)
        return data
    except Exception as exc:  # Telegram/network failures must not break the session
        logger.warning("Could not load Telegram profile photo: %s", exc)
        return None
