"""
Подписанные веб-токены для доступа к WebApp API вне Telegram.
Формат: <payload_b64url>.<signature_b64url>
payload: {"uid": <telegram_id>, "exp": <unix_ts>}
Подпись: HMAC-SHA256(BOT_TOKEN, payload_b64url)
"""
import base64, hashlib, hmac, json, logging, time
from typing import Optional

logger = logging.getLogger(__name__)

def _b64url_encode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")

def _b64url_decode(data: str) -> bytes:
    return base64.urlsafe_b64decode(data + "=" * (-len(data) % 4))

def _sign(payload_b64: str, bot_token: str) -> str:
    return _b64url_encode(
        hmac.new(bot_token.encode(), payload_b64.encode("ascii"), hashlib.sha256).digest()
    )

def make_token(telegram_id: int, bot_token: str, ttl_seconds: int = 3600) -> str:
    """Создаёт подписанный токен. ttl_seconds=3600 (1 час) по умолчанию."""
    exp = int(time.time()) + ttl_seconds
    payload_b64 = _b64url_encode(
        json.dumps({"uid": int(telegram_id), "exp": exp}, separators=(",", ":")).encode()
    )
    return f"{payload_b64}.{_sign(payload_b64, bot_token)}"

def verify_token(token: str, bot_token: str) -> Optional[int]:
    """Проверяет токен. Возвращает telegram_id или None."""
    if not token or "." not in token:
        return None
    try:
        payload_b64, signature = token.rsplit(".", 1)
        if not hmac.compare_digest(signature, _sign(payload_b64, bot_token)):
            logger.warning("webtoken: signature mismatch")
            return None
        payload = json.loads(_b64url_decode(payload_b64).decode())
        if int(payload.get("exp", 0)) < int(time.time()):
            logger.info("webtoken: token expired")
            return None
        uid = payload.get("uid")
        return int(uid) if uid is not None else None
    except Exception as e:
        logger.error(f"webtoken: verify error: {e}")
        return None
