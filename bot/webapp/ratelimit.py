"""Простой лимитер запросов в памяти процесса (бот работает одним процессом).

Ключ — любая строка вида "<правило>:<ip или телефон>". Записи старше окна не считаются,
пустые ключи удаляются, а при разрастании таблицы чистятся и давно не используемые.
"""
import time
from typing import Dict, List

_BUCKETS: Dict[str, List[float]] = {}
_MAX_KEYS = 5000
_LONGEST_WINDOW = 86400  # самое длинное окно среди правил (сутки); старше этого запись не нужна ни одному


def _prune(key: str, window: float) -> List[float]:
    now = time.time()
    items = [t for t in _BUCKETS.get(key, []) if now - t < window]
    if items:
        _BUCKETS[key] = items
    else:
        _BUCKETS.pop(key, None)
    if len(_BUCKETS) > _MAX_KEYS:
        for k in [k for k, v in _BUCKETS.items() if not v or now - v[-1] > _LONGEST_WINDOW]:
            _BUCKETS.pop(k, None)
    return items


def rl_allowed(key: str, limit: int, window: float) -> bool:
    return len(_prune(key, window)) < limit


def rl_record(key: str) -> None:
    _BUCKETS.setdefault(key, []).append(time.time())
