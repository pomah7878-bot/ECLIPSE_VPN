"""
Locks to synchronize transactions with user balances.

Used to prevent race conditions during balance operations.
"""
import asyncio
from collections import defaultdict


user_locks: dict[int, asyncio.Lock] = defaultdict(asyncio.Lock)


# --- защита от двойной покупки (v1.328) -----------------------------------------------
# Каждое нажатие «Оплатить» создаёт новый заказ и списывает баланс отдельно, поэтому
# двойной тап (или повтор из-за задержки Telegram) при достаточном балансе давал два
# оплаченных ключа. Пока покупка выполняется — повторные нажатия отбрасываются, а сразу
# после успешной покупки такой же заказ в течение нескольких секунд не принимается.
import time as _time

PURCHASE_COOLDOWN_SEC = 30
_purchase_inflight: set = set()
_purchase_recent: dict = {}


def try_begin_purchase(key) -> bool:
    """True — можно оформлять; False — такая же покупка уже идёт или только что прошла."""
    now = _time.monotonic()
    for k in [k for k, ts in _purchase_recent.items() if now - ts >= PURCHASE_COOLDOWN_SEC]:
        _purchase_recent.pop(k, None)
    if key in _purchase_inflight or key in _purchase_recent:
        return False
    _purchase_inflight.add(key)
    return True


def end_purchase(key, done: bool) -> None:
    """done=True — покупка оформлена: запоминаем её на время паузы; False — можно пробовать снова."""
    _purchase_inflight.discard(key)
    if done:
        _purchase_recent[key] = _time.monotonic()
