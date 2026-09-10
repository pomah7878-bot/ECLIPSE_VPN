"""
Верификация номера телефона через API zvonok.com — метод "Звонок на
проверочный номер" (pincode_incoming).

Механика (подтверждена реальным тестовым вызовом):
1. Мы вызываем /phones/confirm/ с номером клиента — получаем call_id и
   список allowed_phones_for_call (2-3 служебных номера).
2. Показываем клиенту эти номера — он должен позвонить с СВОЕГО номера
   на любой из них (звонок можно сразу сбросить после соединения).
3. Мы опрашиваем /phones/calls_by_phone/ — статус "pincode_ok" означает
   успешную верификацию (подтверждено реальным тестом 07.09.2026).

Стоимость на момент теста — около 0.29 ₽ за подтверждение.
"""
import logging
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

BASE_URL = "https://zvonok.com/manager/cabapi_external/api/v1/"
SUCCESS_STATUS = "pincode_ok"


async def request_phone_confirmation(phone: str) -> Optional[Dict[str, Any]]:
    """Инициирует проверку номера. Возвращает {call_id, allowed_phones_for_call}
    либо None при ошибке (нет ключа/кампании, сбой сети и т.п.)."""
    from database.requests import get_zvonok_public_key, get_zvonok_campaign_id

    public_key = get_zvonok_public_key()
    campaign_id = get_zvonok_campaign_id()
    if not public_key or not campaign_id:
        logger.warning("Zvonok: не настроен public_key или campaign_id — верификация телефона недоступна")
        return None

    import aiohttp
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(
                BASE_URL + "phones/confirm/",
                data={"public_key": public_key, "campaign_id": campaign_id, "phone": phone},
                timeout=aiohttp.ClientTimeout(total=10),
            ) as resp:
                result = await resp.json()
    except Exception as e:
        logger.error(f"Zvonok: ошибка запроса phones/confirm/: {e}")
        return None

    if result.get("status") != "ok":
        logger.warning(f"Zvonok: phones/confirm/ вернул ошибку: {result}")
        return None

    data = result.get("data") or {}
    return {
        "call_id": data.get("call_id"),
        "allowed_phones_for_call": data.get("allowed_phones_for_call") or [],
    }


async def check_phone_confirmation(call_id) -> bool:
    """Проверяет, подтверждена ли КОНКРЕТНАЯ попытка звонка (по call_id,
    полученному от request_phone_confirmation) — а не любая попытка за
    всю историю номера. Критично: если проверять просто по номеру
    телефона, старый успешный звонок из прошлого (например, тестовый)
    навсегда "разблокирует" этот номер для любых будущих попыток входа
    без реального нового звонка. True — подтверждено (status ==
    'pincode_ok').

    Сначала проверяет ЛОКАЛЬНЫЙ кэш — туда мгновенно попадает результат
    через постбек (вебхук) от zvonok.com, если он настроен (см.
    save_postback_status / handle_zvonok_postback), что даёт ответ без
    похода к их API вообще. Если в кэше пусто (постбек не настроен или
    ещё не дошёl) — опрашивает API как раньше, это резервный вариант."""
    cached = get_cached_postback_status(call_id)
    if cached is not None:
        return cached

    from database.requests import get_zvonok_public_key

    public_key = get_zvonok_public_key()
    if not public_key or not call_id:
        return False

    import aiohttp
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(
                BASE_URL + "phones/call_by_id/",
                params={"public_key": public_key, "call_id": str(call_id)},
                timeout=aiohttp.ClientTimeout(total=10),
            ) as resp:
                result = await resp.json()
    except Exception as e:
        logger.error(f"Zvonok: ошибка запроса phones/call_by_id/: {e}")
        return False

    if isinstance(result, list):
        return any(item.get("status") == SUCCESS_STATUS for item in result)
    if isinstance(result, dict):
        return result.get("status") == SUCCESS_STATUS
    return False


def save_pending_verification(account_id: int, phone: str, call_id) -> None:
    """Сохраняет начатую верификацию для сайт-аккаунта (одна активная
    попытка на аккаунт — перезаписывает предыдущую)."""
    from database.connection import get_db
    with get_db() as conn:
        conn.execute(
            "INSERT INTO site_trial_phone_pending (account_id, phone_raw, call_id, verified_at) "
            "VALUES (?, ?, ?, NULL) "
            "ON CONFLICT(account_id) DO UPDATE SET phone_raw = excluded.phone_raw, "
            "call_id = excluded.call_id, verified_at = NULL, created_at = CURRENT_TIMESTAMP",
            (account_id, phone, str(call_id) if call_id else None),
        )
        conn.commit()


def mark_pending_verified(account_id: int) -> None:
    """Отмечает, что номер для этого аккаунта успешно подтверждён
    (звонок дошёл, status == 'pincode_ok')."""
    from database.connection import get_db
    with get_db() as conn:
        conn.execute(
            "UPDATE site_trial_phone_pending SET verified_at = CURRENT_TIMESTAMP WHERE account_id = ?",
            (account_id,),
        )
        conn.commit()


def get_verified_phone_for_account(account_id: int, max_age_minutes: int = 30) -> Optional[str]:
    """Возвращает подтверждённый номер телефона для аккаунта, если
    верификация прошла успешно и не устарела (по умолчанию — не старше
    30 минут). Иначе None."""
    from database.connection import get_db
    with get_db() as conn:
        row = conn.execute(
            "SELECT phone_raw, verified_at FROM site_trial_phone_pending "
            "WHERE account_id = ? AND verified_at IS NOT NULL "
            "AND datetime(verified_at) > datetime('now', ? || ' minutes')",
            (account_id, f"-{max_age_minutes}"),
        ).fetchone()
    return row["phone_raw"] if row else None


def get_cached_postback_status(call_id) -> Optional[bool]:
    """Возвращает результат ИЗ ЛОКАЛЬНОГО КЭША (заполняется постбеком от
    zvonok.com, см. save_postback_status) для конкретного call_id.
    None — в кэше ничего нет (постбек не настроен или ещё не пришёл),
    тогда вызывающая сторона идёт опрашивать API как раньше."""
    if not call_id:
        return None
    from database.connection import get_db
    with get_db() as conn:
        row = conn.execute(
            "SELECT confirmed FROM zvonok_postback_status WHERE call_id = ?",
            (str(call_id),),
        ).fetchone()
    return bool(row["confirmed"]) if row else None


def save_postback_status(call_id: str, confirmed: bool) -> None:
    """Сохраняет результат, пришедший через постбек (вебхук) от
    zvonok.com — вызывается из handle_zvonok_postback в webapp/server.py.
    INSERT OR REPLACE — если по этому call_id уже что-то было (например,
    сначала пришёл постбек 'нет ответа', а клиент потом всё же дозвонился
    и пришёл повторный успешный постбек), последний пришедший побеждает."""
    from database.connection import get_db
    with get_db() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO zvonok_postback_status (call_id, confirmed, received_at) "
            "VALUES (?, ?, CURRENT_TIMESTAMP)",
            (str(call_id), 1 if confirmed else 0),
        )
        conn.commit()
