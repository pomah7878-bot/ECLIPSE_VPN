"""v1.334: быстрые фильтры aiogram.

aiogram (3.30+) выполняет каждый синхронный фильтр через asyncio.to_thread — в отдельном
потоке. У бота больше 600 обработчиков, и на одно нажатие проверяется ~280 фильтров вида
F.data.startswith(...): это ~280 переходов в поток и обратно на каждый апдейт. Магические
фильтры F — чистые проверки без ввода-вывода, поэтому выполняем их сразу, без потока.
Остальные фильтры и обработчики работают как раньше.
"""
import logging

logger = logging.getLogger(__name__)
_INSTALLED = False


def install_fast_magic_filters() -> bool:
    global _INSTALLED
    if _INSTALLED:
        return True
    try:
        from aiogram.dispatcher.event.handler import FilterObject
    except Exception as e:  # noqa: BLE001
        logger.warning("Быстрые фильтры не включены: %s", e)
        return False
    original = FilterObject.call

    async def call(self, *args, **kwargs):
        if getattr(self, "magic", None) is not None and not getattr(self, "awaitable", True):
            try:
                prepared = self._prepare_kwargs(kwargs)
            except Exception:  # noqa: BLE001
                return await original(self, *args, **kwargs)
            return self.callback(*args, **prepared)
        return await original(self, *args, **kwargs)

    FilterObject.call = call
    _INSTALLED = True
    return True
