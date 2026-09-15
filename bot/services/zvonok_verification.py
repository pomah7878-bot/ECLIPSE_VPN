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


def _get_proxy():
    """Возвращает URL прокси для запросов к zvonok.com, если он настроен
    в админке (обход гео-редиректа zvonok.com -> callo.com для серверов
    вне России), иначе None — запросы уходят напрямую, как раньше."""
    try:
        from database.requests import get_zvonok_proxy_url
    except ImportError:
        return None
    return get_zvonok_proxy_url() or None


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
                proxy=_get_proxy(),
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


async def request_phone_confirmation_pincode(phone: str) -> Optional[Dict[str, Any]]:
    """Инициирует ЗВОНОК КЛИЕНТУ (не клиент звонит нам) с вводом кода —
    альтернативный способ верификации (кампания 'Ввод кода при звонке'
    на zvonok.com, отдельный campaign_id от 'Звонок на проверочный
    номер'). Код можно не указывать — Zvonok сгенерирует сам и вернёт
    его в ответе (data.pincode), мы должны ПОКАЗАТЬ этот код клиенту
    на сайте ДО или сразу после инициации звонка — клиент вводит его с
    клавиатуры телефона, приняв входящий вызов.

    Возвращает {call_id, pincode} либо None при ошибке."""
    from database.requests import get_zvonok_public_key, get_zvonok_pincode_campaign_id

    public_key = get_zvonok_public_key()
    campaign_id = get_zvonok_pincode_campaign_id()
    if not public_key or not campaign_id:
        logger.warning("Zvonok: не настроен public_key или campaign_id (пин-код) — верификация звонком с кодом недоступна")
        return None

    import aiohttp
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(
                BASE_URL + "phones/confirm/",
                data={"public_key": public_key, "campaign_id": campaign_id, "phone": phone},
                timeout=aiohttp.ClientTimeout(total=10),
                proxy=_get_proxy(),
            ) as resp:
                result = await resp.json()
    except Exception as e:
        logger.error(f"Zvonok: ошибка запроса phones/confirm/ (пин-код): {e}")
        return None

    if result.get("status") != "ok":
        logger.warning(f"Zvonok: phones/confirm/ (пин-код) вернул ошибку: {result}")
        return None

    data = result.get("data") or {}
    return {
        "call_id": data.get("call_id"),
        "pincode": data.get("pincode"),
    }


async def request_phone_confirmation_flashcall_real(phone: str) -> Optional[Dict[str, Any]]:
    """Инициирует НАСТОЯЩИЙ Flash Call (кампания типа 'Flash Call' на
    zvonok.com, отдельный campaign_id) — Zvonok сам звонит клиенту,
    код подтверждения — последние 4 цифры номера, с которого поступил
    звонок. Клиенту НЕ нужно отвечать на звонок — он просто читает
    цифры со своего экрана входящего вызова и вводит их у нас на
    сайте. Подтверждение проверяется универсально через
    check_phone_confirmation (Zvonok сам сверяет введённое с реальным
    звонком на своей стороне).

    Возвращает {call_id} либо None при ошибке."""
    from database.requests import get_zvonok_public_key, get_zvonok_flashcall_real_campaign_id

    public_key = get_zvonok_public_key()
    campaign_id = get_zvonok_flashcall_real_campaign_id()
    if not public_key or not campaign_id:
        logger.warning("Zvonok: не настроен public_key или campaign_id (Flash Call) — способ недоступен")
        return None

    import aiohttp
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(
                BASE_URL + "phones/confirm/",
                data={"public_key": public_key, "campaign_id": campaign_id, "phone": phone},
                timeout=aiohttp.ClientTimeout(total=10),
                proxy=_get_proxy(),
            ) as resp:
                result = await resp.json()
    except Exception as e:
        logger.error(f"Zvonok: ошибка запроса phones/confirm/ (Flash Call): {e}")
        return None

    if result.get("status") != "ok":
        logger.warning(f"Zvonok: phones/confirm/ (Flash Call) вернул ошибку: {result}")
        return None

    data = result.get("data") or {}
    return {"call_id": data.get("call_id")}


async def request_phone_confirmation_voice_code(phone: str) -> Optional[Dict[str, Any]]:
    """Инициирует звонок способом 'Диктовка кода роботом' (кампания
    отдельного типа на zvonok.com) — робот сам звонит клиенту и
    ПРОИЗНОСИТ код вслух. В отличие от 'pincode' (клиент вводит с
    клавиатуры ВО ВРЕМЯ звонка), здесь клиент вводит услышанный код
    У НАС НА САЙТЕ уже ПОСЛЕ звонка — поэтому код нельзя показать
    заранее (клиент его ещё не знает), а нужно временно сохранить
    ожидаемое значение у себя (см. save_pending_voice_code) и сверить
    с тем, что клиент введёт (см. check_voice_code).

    Возвращает {call_id} либо None при ошибке (pincode НЕ возвращается
    вызывающей стороне — остаётся только в нашей БД для сверки)."""
    from database.requests import get_zvonok_public_key, get_zvonok_voice_code_campaign_id

    public_key = get_zvonok_public_key()
    campaign_id = get_zvonok_voice_code_campaign_id()
    if not public_key or not campaign_id:
        logger.warning("Zvonok: не настроен public_key или campaign_id (диктовка кода) — способ недоступен")
        return None

    import aiohttp
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(
                BASE_URL + "phones/confirm/",
                data={"public_key": public_key, "campaign_id": campaign_id, "phone": phone},
                timeout=aiohttp.ClientTimeout(total=10),
                proxy=_get_proxy(),
            ) as resp:
                result = await resp.json()
    except Exception as e:
        logger.error(f"Zvonok: ошибка запроса phones/confirm/ (диктовка кода): {e}")
        return None

    if result.get("status") != "ok":
        logger.warning(f"Zvonok: phones/confirm/ (диктовка кода) вернул ошибку: {result}")
        return None

    data = result.get("data") or {}
    call_id = data.get("call_id")
    pincode = data.get("pincode")
    if call_id and pincode:
        save_pending_voice_code(str(call_id), str(pincode))
    return {"call_id": call_id}


async def request_phone_confirmation_press_digit(phone: str) -> Optional[Dict[str, Any]]:
    """Инициирует звонок способом 'Подтверждение звонком' (кампания
    отдельного типа на zvonok.com) — робот звонит клиенту и просит
    нажать конкретную цифру для подтверждения (без диктовки кода).
    Проверяется универсально через check_phone_confirmation.

    Возвращает {call_id} либо None при ошибке."""
    from database.requests import get_zvonok_public_key, get_zvonok_press_digit_campaign_id

    public_key = get_zvonok_public_key()
    campaign_id = get_zvonok_press_digit_campaign_id()
    if not public_key or not campaign_id:
        logger.warning("Zvonok: не настроен public_key или campaign_id (подтверждение звонком) — способ недоступен")
        return None

    import aiohttp
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(
                BASE_URL + "phones/confirm/",
                data={"public_key": public_key, "campaign_id": campaign_id, "phone": phone},
                timeout=aiohttp.ClientTimeout(total=10),
                proxy=_get_proxy(),
            ) as resp:
                result = await resp.json()
    except Exception as e:
        logger.error(f"Zvonok: ошибка запроса phones/confirm/ (подтверждение звонком): {e}")
        return None

    if result.get("status") != "ok":
        logger.warning(f"Zvonok: phones/confirm/ (подтверждение звонком) вернул ошибку: {result}")
        return None

    data = result.get("data") or {}
    return {"call_id": data.get("call_id")}


def save_pending_voice_code(call_id: str, expected_pincode: str) -> None:
    """Сохраняет код, который робот продиктует клиенту — временно, до
    сверки с тем, что клиент введёт на сайте (способ 'voice_code')."""
    from database.connection import get_db
    with get_db() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO zvonok_pending_voice_codes (call_id, expected_pincode, created_at) "
            "VALUES (?, ?, CURRENT_TIMESTAMP)",
            (str(call_id), str(expected_pincode)),
        )
        conn.commit()


def check_voice_code(call_id: str, entered_code: str) -> bool:
    """Сверяет код, который клиент ввёл на сайте, с тем, что робот ему
    продиктовал (способ 'voice_code') — сравнение чисто локальное, не
    требует похода к API Zvonok, так как мы сами получили ожидаемое
    значение при инициации звонка."""
    from database.connection import get_db
    with get_db() as conn:
        row = conn.execute(
            "SELECT expected_pincode FROM zvonok_pending_voice_codes WHERE call_id = ?",
            (str(call_id),),
        ).fetchone()
    if not row:
        return False
    return str(row["expected_pincode"]).strip() == str(entered_code).strip()


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
                proxy=_get_proxy(),
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
