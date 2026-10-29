"""Свой баннер для подписки: один текст и ссылка для всех клиентов.

Доставка (всё бесплатно, без платных тарифов приложений):
- Happ: заголовки sub-info-text / sub-info-button-*;
- INCY: заголовки announce / announce-url;
- остальные приложения: служебная строка в начале списка серверов (по переключателю);
- пока баннер активен, клиентам отдаётся короткий profile-update-interval.
Автоматические баннеры (истекла, пробный период, ≤3 дней) важнее своего.
"""
import base64
import time
import urllib.parse
from typing import Optional

from database.requests import get_setting, set_setting

UPDATE_HOURS = 3
_DUMMY = "vless://00000000-0000-0000-0000-000000000000@127.0.0.1:1?encryption=none&security=none&type=tcp#"


def _s(key: str) -> str:
    return get_setting(f"banner_{key}", "") or ""


def save(key: str, value: str) -> None:
    set_setting(f"banner_{key}", value)


def active() -> Optional[dict]:
    """Действующий баннер или None (выключен, пустой текст или срок вышел)."""
    text = _s("text").strip()
    if _s("enabled") != "1" or not text:
        return None
    until = int(_s("until") or 0)
    if until and time.time() > until:
        return None
    return {"text": text, "url": _s("url").strip(), "button": _s("button").strip() or "Подробнее"}


def set_days(days: int) -> None:
    save("until", str(int(time.time()) + days * 86400) if days > 0 else "0")


def line_enabled() -> bool:
    return _s("line") == "1"


def apply_headers(headers, app: Optional[str], has_auto: bool) -> bool:
    """Дописывает заголовки баннера. Возвращает True, если баннер действует."""
    b = active()
    if not b:
        return False
    if "profile-update-interval" not in headers and app != "incy":
        headers["profile-update-interval"] = str(UPDATE_HOURS)
    if has_auto:
        return True
    if app == "happ":
        headers["sub-info-text"] = b["text"]
        headers["sub-info-button-text"] = b["button"]
        if b["url"]:
            headers["sub-info-button-link"] = b["url"]
    elif app == "incy":
        headers["announce"] = "base64:" + base64.b64encode(b["text"][:200].encode("utf-8")).decode("ascii")
        if b["url"]:
            headers["announce-url"] = b["url"]
    return True


def inject_line(body: bytes) -> bytes:
    """Добавляет баннер первой строкой списка серверов (для приложений без заголовков)."""
    b = active()
    if not b or not line_enabled():
        return body
    line = _DUMMY + urllib.parse.quote(b["text"][:100], safe="")
    try:
        text = body.decode("utf-8")
    except Exception:
        return body
    if "://" in text:
        return (line + "\n" + text).encode("utf-8")
    try:
        raw = text.strip().replace("-", "+").replace("_", "/")
        raw += "=" * (-len(raw) % 4)
        decoded = base64.b64decode(raw, validate=True).decode("utf-8")
    except Exception:
        return body
    return base64.b64encode((line + "\n" + decoded).encode("utf-8"))
