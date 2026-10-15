"""Оповещения администратору о сбоях: узел, панель, диск, бэкапы, планировщик.

Раз в 5 минут бот проверяет систему теми же проверками, что и экран «🩺 Диагностика»,
и пишет админам в Telegram, когда что-то упало или снова заработало.

Правила, чтобы не спамить:
  • сообщение приходит, только если проблема 🔴 держится 2 проверки подряд (~10 минут);
  • по одной и той же проблеме — одно сообщение, повтор не чаще раза в 6 часов;
  • когда проблема исчезла — одно сообщение «✅ восстановлено»;
  • предупреждения ⚠️ (например, высокая нагрузка) не оповещают, их видно в диагностике;
  • проверки без данных ⚪ состояние не меняют.

Включается и выключается кнопкой на экране диагностики (настройка health_alerts_enabled).
"""
import asyncio
import logging
import time
from typing import Any, Dict, List, Tuple

logger = logging.getLogger(__name__)

CHECK_INTERVAL = 300          # секунд между проверками
FAIL_THRESHOLD = 2            # сколько проверок подряд 🔴 до первого сообщения
REPEAT_AFTER = 6 * 3600       # повторить напоминание, если проблема не исчезла
START_DELAY = 90              # дать боту и панелям подняться после запуска
SETTING_KEY = "health_alerts_enabled"

Check = Tuple[str, str, str]
Event = Tuple[str, str, str]  # (вид: down | still_down | recovered, название, подробности)


def is_enabled() -> bool:
    from database.db_settings import get_setting
    return str(get_setting(SETTING_KEY, "1") or "1") == "1"


def set_enabled(enabled: bool) -> None:
    from database.db_settings import set_setting
    set_setting(SETTING_KEY, "1" if enabled else "0")


def evaluate(checks: List[Check], state: Dict[str, Dict[str, Any]], now: float) -> List[Event]:
    """Обновляет state по результатам проверок и возвращает события для сообщений.

    state[название] = {"fails": подряд 🔴, "alerted": было ли сообщение, "last": время последнего сообщения}.
    Чистая функция без обращений к сети и БД — удобно проверять тестами.
    """
    events: List[Event] = []
    for icon, title, detail in checks:
        if icon == "⚪":
            continue
        item = state.setdefault(title, {"fails": 0, "alerted": False, "last": 0.0})
        if icon == "🔴":
            item["fails"] += 1
            if item["fails"] < FAIL_THRESHOLD:
                continue
            if not item["alerted"]:
                item["alerted"], item["last"] = True, now
                events.append(("down", title, detail))
            elif now - item["last"] >= REPEAT_AFTER:
                item["last"] = now
                events.append(("still_down", title, detail))
        else:
            if item["alerted"]:
                events.append(("recovered", title, detail))
            item["fails"], item["alerted"], item["last"] = 0, False, 0.0
    return events


def format_message(events: List[Event]) -> str:
    from bot.utils.text import escape_html
    down = [e for e in events if e[0] in ("down", "still_down")]
    back = [e for e in events if e[0] == "recovered"]
    blocks: List[str] = []
    if down:
        lines = ["🔴 <b>Сбой в системе</b>"]
        for kind, title, detail in down:
            tail = " (всё ещё не работает)" if kind == "still_down" else ""
            lines.append(f"\n<b>{escape_html(title)}</b>{tail}\n{escape_html(detail)}")
        lines.append("\nПроверка шла несколько раз подряд, это не разовая ошибка. Подробности — в «🩺 Диагностика».")
        blocks.append("\n".join(lines))
    if back:
        lines = ["✅ <b>Восстановлено</b>"]
        for _, title, detail in back:
            lines.append(f"\n<b>{escape_html(title)}</b>\n{escape_html(detail)}")
        blocks.append("\n".join(lines))
    return "\n\n".join(blocks)


def _markup():
    from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
    from bot.keyboards.admin_misc import back_button, home_button
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🩺 Диагностика", callback_data="admin_extensions_diagnostics")],
        [back_button("admin_panel"), home_button()],
    ])


async def collect_alert_checks() -> List[Check]:
    """Проверки, о которых стоит сообщать сразу: панели и узлы, диск, бэкапы, планировщик."""
    from bot.services import system_health as sh
    loop = asyncio.get_running_loop()
    sync = await loop.run_in_executor(None, lambda: [sh.check_disk(), sh.check_backups(), sh.check_scheduler()])
    try:
        panels = await sh.check_panels()
    except Exception as e:
        panels = [("🔴", "Панели", sh._short_error(e))]
    return list(sync) + list(panels)


async def run_check_once(bot, state: Dict[str, Dict[str, Any]]) -> int:
    """Одна проверка и рассылка. Возвращает число событий."""
    if not is_enabled():
        return 0
    events = evaluate(await collect_alert_checks(), state, time.time())
    if not events:
        return 0
    text = format_message(events)
    from config import ADMIN_IDS
    for admin_id in ADMIN_IDS:
        try:
            await bot.send_message(admin_id, text, parse_mode="HTML", reply_markup=_markup())
        except Exception as e:
            logger.debug("Оповещение админу %s не доставлено: %s", admin_id, e)
    return len(events)


async def run_health_alert_scheduler(bot) -> None:
    logger.info("🔔 Оповещения о сбоях запущены (каждые %d с)", CHECK_INTERVAL)
    state: Dict[str, Dict[str, Any]] = {}
    await asyncio.sleep(START_DELAY)
    while True:
        try:
            await run_check_once(bot, state)
        except asyncio.CancelledError:
            logger.info("Оповещения о сбоях остановлены")
            break
        except Exception as e:
            logger.error("Ошибка в оповещениях о сбоях: %s", e)
        await asyncio.sleep(CHECK_INTERVAL)
