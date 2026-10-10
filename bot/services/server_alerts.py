"""Оповещение админов о недоступности серверов (панелей) и о восстановлении.

Данные берутся из обычного 5-минутного цикла синхронизации трафика: если панель сервера не
ответила несколько циклов подряд, админам уходит одно сообщение; когда сервер снова
отвечает — второе. Состояние хранится в памяти: после перезапуска бота счёт начинается заново.
"""
from __future__ import annotations

import logging
import time
from typing import Any, Dict, Iterable, Optional

logger = logging.getLogger(__name__)

FAILS_BEFORE_ALERT = 3   # ≈15 минут подряд при цикле в 5 минут
_state: Dict[int, Dict[str, Any]] = {}


def _server_name(servers: Iterable[Dict[str, Any]], server_id: int) -> str:
    for s in servers:
        if int(s.get("id", -1)) == int(server_id):
            return str(s.get("name") or f"#{server_id}")
    return f"#{server_id}"


async def _send(bot, text: str) -> None:
    from config import ADMIN_IDS
    from bot.utils.notify_kb import with_home
    for admin_id in list(ADMIN_IDS)[:5]:
        try:
            await bot.send_message(admin_id, text, parse_mode="HTML", reply_markup=with_home())
        except Exception as e:  # noqa: BLE001
            logger.debug("server_alerts: админу %s не доставлено: %s", admin_id, e)


def _esc(value: str) -> str:
    from bot.utils.text import escape_html
    return escape_html(value)


async def process_server_health(bot, servers: Iterable[Dict[str, Any]], errors: Dict[int, str],
                                checked_ids: Optional[Iterable[int]] = None, now: Optional[float] = None) -> None:
    """errors — {server_id: ошибка} из последнего цикла. checked_ids — серверы, которые проверялись
    (с ответом или ошибкой); непроверенные не считаются ни упавшими, ни восстановленными."""
    now = time.time() if now is None else now
    servers = list(servers)
    failed = {int(k) for k in (errors or {})}
    checked = {int(i) for i in (checked_ids or [])} | failed
    for sid in failed:
        st = _state.setdefault(sid, {"fails": 0, "alerted": False, "since": now})
        if st["fails"] == 0:
            st["since"] = now
        st["fails"] += 1
        if st["fails"] >= FAILS_BEFORE_ALERT and not st["alerted"]:
            st["alerted"] = True
            minutes = max(1, int((now - st["since"]) // 60))
            await _send(
                bot,
                f"🔴 <b>Сервер недоступен: {_esc(_server_name(servers, sid))}</b>\n\n"
                f"Панель не отвечает уже около {minutes} мин.\n"
                f"Ошибка: {_esc(str(errors.get(sid) or errors.get(str(sid)) or '—')[:200])}\n"
                "Клиенты этого сервера могут не подключаться.",
            )
    for sid in list(_state.keys()):
        if sid in failed or sid not in checked:
            continue
        st = _state.pop(sid)
        if st.get("alerted"):
            minutes = max(1, int((now - st["since"]) // 60))
            await _send(
                bot,
                f"🟢 <b>Сервер снова доступен: {_esc(_server_name(servers, sid))}</b>\n\n"
                f"Простой составил около {minutes} мин.",
            )


def reset_state() -> None:
    _state.clear()
