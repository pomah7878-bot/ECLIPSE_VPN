"""v1.202: короткоживущие одноразовые токены для страницы «Открыть в AmneziaVPN».

Ключ vpn:// содержит личный ключ клиента, поэтому в адрес страницы он не
попадает (иначе осядет в логах nginx) — в адресе только случайный токен.
Хранится в памяти процесса бота (сайт работает в том же процессе)."""
import secrets
import threading
import time

_TTL = 15 * 60
_MAX = 2000
_LOCK = threading.Lock()
_STORE: dict = {}


def _cleanup(now: float) -> None:
    for tok in [t for t, (_, exp) in _STORE.items() if exp <= now]:
        _STORE.pop(tok, None)
    if len(_STORE) > _MAX:
        for tok in sorted(_STORE, key=lambda t: _STORE[t][1])[: len(_STORE) - _MAX]:
            _STORE.pop(tok, None)


def create_token(link: str, ttl: int = _TTL) -> str:
    now = time.time()
    token = secrets.token_urlsafe(18)
    with _LOCK:
        _cleanup(now)
        _STORE[token] = (link, now + ttl)
    return token


def get_link(token: str):
    """Ссылка по токену или None (не найден / истёк). Токен живёт TTL, открыть можно повторно."""
    now = time.time()
    with _LOCK:
        item = _STORE.get(token or "")
        if not item or item[1] <= now:
            _STORE.pop(token or "", None)
            return None
        return item[0]
