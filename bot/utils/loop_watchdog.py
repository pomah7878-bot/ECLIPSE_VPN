"""Сторож цикла событий (v1.214).

Если обработчик синхронно блокирует цикл (долгий запрос к БД, блокирующий вызов сети
и т.п.), бот перестаёт отвечать всем сразу. Отдельный поток следит за «пульсом» цикла и,
когда он пропадает дольше STALL_SEC секунд, пишет в журнал, ГДЕ именно застрял код.
Только наблюдает: на работу бота не влияет."""
import asyncio
import logging
import sys
import threading
import time
import traceback

logger = logging.getLogger(__name__)

STALL_SEC = 5.0
_started = False


def start_loop_watchdog(stall_sec: float = STALL_SEC) -> None:
    """Запускать из корутины, работающей в основном цикле событий."""
    global _started
    if _started:
        return
    _started = True
    loop_thread_id = threading.get_ident()
    state = {"beat": time.monotonic(), "reported": False}

    async def _heartbeat():
        while True:
            now = time.monotonic()
            gap = now - state["beat"]
            if state["reported"]:
                logger.warning("Цикл событий снова отвечает, простой составил ~%.0f с", gap)
                state["reported"] = False
            state["beat"] = now
            await asyncio.sleep(1)

    def _watch():
        while True:
            time.sleep(1)
            try:
                lag = time.monotonic() - state["beat"]
                if lag > stall_sec and not state["reported"]:
                    state["reported"] = True
                    frame = sys._current_frames().get(loop_thread_id)
                    stack = "".join(traceback.format_stack(frame, limit=25)) if frame else "(стек недоступен)"
                    logger.warning(
                        "Цикл событий не отвечает уже %.1f с — бот не обрабатывает сообщения. "
                        "Код, на котором он застрял:\n%s", lag, stack,
                    )
            except Exception as e:  # сторож не должен падать сам
                logger.debug("loop_watchdog: %s", e)

    asyncio.get_running_loop().create_task(_heartbeat())
    threading.Thread(target=_watch, name="loop-watchdog", daemon=True).start()
