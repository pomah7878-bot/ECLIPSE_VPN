"""Подпись ответов лицензионного сервера (Ed25519) и её проверка на стороне бота.

Сервер лицензий при первом запуске создаёт пару ключей; закрытый ключ хранится
в настройке license_signing_private и никуда не передаётся. Бот партнёра при
первой удачной проверке запоминает открытый ключ сервера (или берёт его из
LICENSE_SERVER_PUBKEY в secrets.env) и дальше принимает только ответы с
верной подписью, привязанной к его ключу лицензии, его установке и его
одноразовому коду запроса (nonce). Подделанный или записанный заранее ответ
не пройдёт.
"""
import base64
import hashlib
import json
import logging
import os
import time
from typing import Any, Dict, Optional, Tuple

logger = logging.getLogger(__name__)


def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def key_fingerprint(license_key: str) -> str:
    return hashlib.sha256(license_key.strip().upper().encode()).hexdigest()[:32]


def _canonical(payload: Dict[str, Any]) -> bytes:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()


def _private_key():
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    from cryptography.hazmat.primitives import serialization
    from database.requests import get_setting, set_setting

    stored = get_setting("license_signing_private", "")
    if stored:
        return Ed25519PrivateKey.from_private_bytes(_unb64(stored))
    key = Ed25519PrivateKey.generate()
    raw = key.private_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PrivateFormat.Raw,
        encryption_algorithm=serialization.NoEncryption(),
    )
    set_setting("license_signing_private", _b64(raw))
    logger.info("Лицензии: создан ключ подписи ответов")
    return key


def public_key_b64() -> str:
    from cryptography.hazmat.primitives import serialization

    pub = _private_key().public_key().public_bytes(
        encoding=serialization.Encoding.Raw, format=serialization.PublicFormat.Raw
    )
    return _b64(pub)


def sign_license_response(result: Dict[str, Any], license_key: str, instance_id: str, nonce: str) -> Dict[str, Any]:
    """Сервер: подписанные данные лицензии для конкретной установки и запроса."""
    payload = {
        "v": 1,
        "kh": key_fingerprint(license_key),
        "inst": instance_id,
        "nonce": nonce,
        "tier": result.get("tier"),
        "features": result.get("features") or "",
        "expires_at": result.get("expires_at"),
        "partner_name": result.get("partner_name"),
        "iat": int(time.time()),
    }
    raw = _canonical(payload)
    return {"payload": _b64(raw), "sig": _b64(_private_key().sign(raw)), "pub": public_key_b64()}


def _pinned_pubkey() -> str:
    env = (os.getenv("LICENSE_SERVER_PUBKEY") or "").strip()
    if env:
        return env
    from database.requests import get_setting
    return get_setting("license_server_pubkey", "") or ""


def verify_license_response(
    signed: Optional[Dict[str, Any]], license_key: str, instance_id: str, nonce: str
) -> Tuple[str, Optional[Dict[str, Any]]]:
    """Бот: ("ok", данные) | ("unsigned", None) — сервер старой версии и ключ
    сервера ещё не запомнен | ("bad", None) — подпись неверна или ответ чужой."""
    try:
        from cryptography.exceptions import InvalidSignature
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
    except ImportError:
        logger.warning("Лицензии: не установлена библиотека cryptography — подпись не проверяется (pip install cryptography)")
        return "unsigned", None
    from database.requests import set_setting

    pinned = _pinned_pubkey()
    if not isinstance(signed, dict) or not signed.get("payload") or not signed.get("sig"):
        return ("bad", None) if pinned else ("unsigned", None)
    try:
        pub = str(signed.get("pub") or "")
        if pinned and pub != pinned:
            return "bad", None
        raw = _unb64(signed["payload"])
        Ed25519PublicKey.from_public_bytes(_unb64(pub)).verify(_unb64(signed["sig"]), raw)
        payload = json.loads(raw.decode())
    except (InvalidSignature, ValueError, KeyError, TypeError):
        return "bad", None
    if (
        payload.get("kh") != key_fingerprint(license_key)
        or payload.get("inst") != instance_id
        or payload.get("nonce") != nonce
    ):
        return "bad", None
    if not pinned:
        set_setting("license_server_pubkey", pub)
        logger.info("Лицензии: запомнен открытый ключ лицензионного сервера")
    return "ok", payload
