"""
Общий реестр номеров телефонов, использованных для получения пробного
периода — единая проверка и с сайта, и из бота, чтобы один и тот же
человек не мог получить пробник дважды под разными аккаунтами
(Telegram-аккаунт + отдельный, никак не связанный с ним OAuth-аккаунт
на сайте).

Номер телефона — единственный практически общий идентификатор между
Telegram (передаётся через кнопку "Поделиться контактом", уже
верифицирован самим Telegram) и обычным сайтом (верифицируется через
звонок/SMS с кодом, см. bot/services/phone_verification.py).
"""
import re
from typing import Optional


def normalize_phone(raw_phone: str) -> str:
    """Приводит номер к единому виду для сравнения: только цифры,
    ведущая '8' заменяется на '7' (российский стандарт), без '+'.
    '+7 (912) 345-67-89' и '89123456789' дают одинаковый результат."""
    digits = re.sub(r"\D", "", raw_phone or "")
    if len(digits) == 11 and digits.startswith("8"):
        digits = "7" + digits[1:]
    return digits


def has_phone_used_trial(raw_phone: str) -> bool:
    """Проверяет, использовался ли уже этот номер для пробного периода
    (с любой стороны — сайт или бот)."""
    phone = normalize_phone(raw_phone)
    if not phone:
        return False
    from database.connection import get_db
    with get_db() as conn:
        row = conn.execute(
            "SELECT id FROM trial_verified_phones WHERE phone_normalized = ?",
            (phone,),
        ).fetchone()
    return row is not None


def mark_phone_trial_used(
    raw_phone: str,
    telegram_id: Optional[int] = None,
    site_account_id: Optional[int] = None,
) -> bool:
    """Отмечает номер как использованный для пробного периода.
    Возвращает False, если номер уже был использован ДО этого вызова
    (защита от гонки — двух почти одновременных попыток одним и тем же
    номером с разных сторон)."""
    phone = normalize_phone(raw_phone)
    if not phone:
        return False
    from database.connection import get_db
    with get_db() as conn:
        try:
            conn.execute(
                "INSERT INTO trial_verified_phones (phone_normalized, telegram_id, site_account_id) "
                "VALUES (?, ?, ?)",
                (phone, telegram_id, site_account_id),
            )
            conn.commit()
            return True
        except Exception:
            # UNIQUE constraint — номер уже был отмечен кем-то другим
            return False


def unmark_phone_trial_used(raw_phone: str) -> None:
    """Откатывает пометку номера как использованного — вызывается, если
    после успешной верификации телефона провижининг ключа всё же не
    удался (например, сервер недоступен), чтобы клиент не потерял
    возможность попробовать ещё раз тем же номером."""
    phone = normalize_phone(raw_phone)
    if not phone:
        return
    from database.connection import get_db
    with get_db() as conn:
        conn.execute("DELETE FROM trial_verified_phones WHERE phone_normalized = ?", (phone,))
        conn.commit()


def get_telegram_id_for_verified_phone(raw_phone: str) -> Optional[int]:
    """Если этот номер телефона уже был подтверждён РАНЕЕ через бота
    (клиент делился контактом при получении пробного периода) — вход по
    телефону на сайте должен узнать в нём того же человека и войти в
    его существующий Telegram-аккаунт (со всей историей покупок), а не
    создавать отдельный, пустой аккаунт с provider='phone'."""
    phone = normalize_phone(raw_phone)
    if not phone:
        return None
    from database.connection import get_db
    with get_db() as conn:
        row = conn.execute(
            "SELECT telegram_id FROM trial_verified_phones WHERE phone_normalized = ? AND telegram_id IS NOT NULL",
            (phone,),
        ).fetchone()
    return int(row["telegram_id"]) if row else None


def has_telegram_id_linked_phone(telegram_id: int) -> bool:
    """Проверяет, привязан ли уже к этому Telegram-аккаунту какой-либо
    номер телефона (через пробник в боте или отдельную привязку).
    Используется, чтобы не предлагать привязку повторно тем, у кого
    она уже есть."""
    from database.connection import get_db
    with get_db() as conn:
        row = conn.execute(
            "SELECT id FROM trial_verified_phones WHERE telegram_id = ?",
            (telegram_id,),
        ).fetchone()
    return row is not None
