"""Строка-подсказка в списке серверов подписки.

Бесплатная замена баннеру: у Happ расширенные объявления (sub-info) требуют платного
Provider ID, у INCY баннер — только для Premium. Нерабочая запись с понятным названием
видна в любом приложении.
"""
import base64
import urllib.parse

# Адрес заведомо нерабочий: запись только показывает текст в списке серверов.
_PLACEHOLDER = "vless://00000000-0000-0000-0000-000000000000@127.0.0.1:1?type=tcp&security=none&encryption=none"


def add_info_line(body: bytes, name: str) -> bytes:
    """Ставит запись с названием name сразу ПОСЛЕ первого настоящего сервера: приложения
    по умолчанию выбирают первый сервер списка, и им должен оказаться рабочий.
    Идемпотентна; на пустое или неразборчивое тело возвращает его без изменений."""
    line = f"{_PLACEHOLDER}#{urllib.parse.quote(name, safe='')}"

    def _add(text: str):
        if line in text:
            return None
        rows = text.split("\n")
        at = next((i for i, r in enumerate(rows) if "://" in r), None)
        if at is None:
            return None
        rows.insert(at + 1, line)
        return "\n".join(rows)

    try:
        text = body.decode("utf-8")
    except Exception:
        return body
    if "://" in text:
        out = _add(text)
        return body if out is None else out.encode("utf-8")
    try:
        raw = text.strip().replace("-", "+").replace("_", "/")
        raw += "=" * (-len(raw) % 4)
        decoded = base64.b64decode(raw, validate=True).decode("utf-8")
    except Exception:
        return body
    out = _add(decoded)
    return body if out is None else base64.b64encode(out.encode("utf-8"))
