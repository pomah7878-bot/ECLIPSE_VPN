"""
Общий реестр номеров телефонов, связанных с человеком: Telegram-аккаунт
(кнопка "Поделиться контактом", верифицируется самим Telegram) и обычный
сайт (подтверждение звонком/SMS, см. bot/services/phone_verification.py).

Реестр отвечает на вопрос «чей это номер», а не «брал ли он пробник»:
факт выдачи пробного периода хранится в trial_claims
(database/db_trial_guard.py) и проверяется по номеру как по одной из
личностей. Так привязка номера ради входа на сайт больше не расходует
пробник.
"""
import re
from typing import Optional


def normalize_phone(raw_phone: str) -> str:
    """Приводит номер к единому виду для сравнения: только цифры,
    ведущая '8' заменяется на '7' (российский стандарт), без '+'.
    '+7 (912) 345-67-89' и '89123456789' дают одинаковый результат.

    Номер, набранный без кода страны ('9123456789'), тоже считается
    российским. Иначе один человек попадал в две личности: в боте номер
    привязан к Telegram как '79123456789', а на сайте/в приложении без
    '+7' появлялся пустой аккаунт '9123456789' без подписки."""
    digits = re.sub(r"\D", "", raw_phone or "")
    if len(digits) == 11 and digits.startswith("8"):
        digits = "7" + digits[1:]
    elif len(digits) == 10 and digits.startswith("9"):
        digits = "7" + digits
    return digits


def has_phone_used_trial(raw_phone: str) -> bool:
    """Есть ли номер в реестре (уже привязан к какому-то аккаунту).
    Историческое имя; факт выдачи пробника проверяется в trial_claims."""
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
    """Историческая отметка номера. Для реальной выдачи пробника используйте
    trial_claims (database/db_trial_guard.py): эта таблица — справочник
    «номер → человек», а не факт использования пробника.
    Возвращает False, если номер уже был отмечен ДО этого вызова."""
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


def link_phone(raw_phone: str, telegram_id: int) -> bool:
    """Привязывает номер к Telegram-аккаунту (вход на сайт по номеру, бонус).

    Не означает «пробник использован» — факт выдачи живёт в trial_claims.
    Возвращает False, если номер уже принадлежит другому аккаунту.
    """
    phone = normalize_phone(raw_phone)
    if not phone:
        return False
    from database.connection import get_db
    with get_db() as conn:
        row = conn.execute(
            "SELECT telegram_id FROM trial_verified_phones WHERE phone_normalized = ?",
            (phone,),
        ).fetchone()
        if row is None:
            conn.execute(
                "INSERT INTO trial_verified_phones (phone_normalized, telegram_id) VALUES (?, ?)",
                (phone, telegram_id),
            )
            return True
        owner = row["telegram_id"]
        if owner is None:
            conn.execute(
                "UPDATE trial_verified_phones SET telegram_id = ? WHERE phone_normalized = ?",
                (telegram_id, phone),
            )
            return True
        return int(owner) == int(telegram_id)


def register_site_account_phone(
    raw_phone: str,
    site_account_id: int,
    telegram_id: Optional[int] = None,
) -> bool:
    """Записывает подтверждённый в личном кабинете номер в общий реестр
    «номер → человек».

    Раньше номер, добавленный на сайте, лежал только в
    site_accounts.phone, а бот читает ЭТУ таблицу — поэтому «Поделиться
    контактом» и сайт видели разные номера. Теперь оба канала пишут в
    один реестр. Возвращает False, если номер уже принадлежит ДРУГОМУ
    сайт-аккаунту или другому Telegram (чужой номер не перехватить).
    """
    phone = normalize_phone(raw_phone)
    if not phone or not site_account_id:
        return False
    from database.connection import get_db
    with get_db() as conn:
        row = conn.execute(
            "SELECT id, telegram_id, site_account_id FROM trial_verified_phones "
            "WHERE phone_normalized = ?",
            (phone,),
        ).fetchone()
        if row is None:
            conn.execute(
                "INSERT INTO trial_verified_phones (phone_normalized, telegram_id, site_account_id) "
                "VALUES (?, ?, ?)",
                (phone, telegram_id, site_account_id),
            )
            return True
        owner_account = row["site_account_id"]
        owner_telegram = row["telegram_id"]
        if owner_account not in (None, site_account_id):
            return False
        if telegram_id and owner_telegram not in (None, telegram_id):
            return False
        conn.execute(
            "UPDATE trial_verified_phones "
            "SET site_account_id = ?, telegram_id = COALESCE(telegram_id, ?) WHERE id = ?",
            (site_account_id, telegram_id, row["id"]),
        )
        return True


def unregister_site_account_phone(
    raw_phone: str,
    site_account_id: int,
    telegram_id: Optional[int] = None,
) -> bool:
    """Убирает связку «номер → сайт-аккаунт» при смене или отвязке номера.

    telegram_id передаётся только при ПОЛНОЙ отвязке: тогда удаляется и
    связка с Telegram этого же человека, иначе после отвязки в кабинете
    номер продолжал бы показываться в боте. При смене номера историю не
    стираем — старая запись остаётся «номер подтверждён за человеком».
    Строку без владельцев удаляем, чтобы номер не остался в реестре
    навсегда чужим и не блокировал повторную привязку.
    """
    phone = normalize_phone(raw_phone)
    if not phone:
        return False
    from database.connection import get_db
    with get_db() as conn:
        cursor = conn.execute(
            "UPDATE trial_verified_phones SET site_account_id = NULL "
            "WHERE phone_normalized = ? AND site_account_id = ?",
            (phone, site_account_id),
        )
        if not cursor.rowcount:
            return False
        if telegram_id:
            conn.execute(
                "UPDATE trial_verified_phones SET telegram_id = NULL "
                "WHERE phone_normalized = ? AND telegram_id = ?",
                (phone, int(telegram_id)),
            )
        conn.execute(
            "DELETE FROM trial_verified_phones WHERE phone_normalized = ? AND telegram_id IS NULL",
            (phone,),
        )
        return True


def get_phone_for_telegram_id(telegram_id: int) -> Optional[str]:
    """Первый подтверждённый номер этого Telegram-аккаунта (для связки каналов)."""
    from database.connection import get_db
    with get_db() as conn:
        row = conn.execute(
            "SELECT phone_normalized FROM trial_verified_phones "
            "WHERE telegram_id = ? AND phone_normalized != '' ORDER BY id LIMIT 1",
            (telegram_id,),
        ).fetchone()
    return row["phone_normalized"] if row else None


def get_phone_for_site_account_id(site_account_id: int) -> Optional[str]:
    """Первый подтверждённый номер, привязанный к сайт-аккаунту."""
    from database.connection import get_db
    with get_db() as conn:
        row = conn.execute(
            "SELECT phone_normalized FROM trial_verified_phones "
            "WHERE site_account_id = ? AND phone_normalized != '' ORDER BY id LIMIT 1",
            (site_account_id,),
        ).fetchone()
    return row["phone_normalized"] if row else None


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


def unlink_phone_from_telegram(raw_phone: str, telegram_id: int) -> bool:
    """Отвязывает номер от Telegram-аккаунта (кнопка «Это не я» в
    уведомлении о входе чужим номером) — убирает только привязку к
    telegram_id, не трогая site_account_id, если он тоже есть. Проверяет
    telegram_id, чтобы отвязать мог только владелец аккаунта, в который
    вела привязка, а не кто угодно, кто знает номер."""
    phone = normalize_phone(raw_phone)
    if not phone:
        return False
    from database.connection import get_db
    with get_db() as conn:
        cursor = conn.execute(
            "UPDATE trial_verified_phones SET telegram_id = NULL "
            "WHERE phone_normalized = ? AND telegram_id = ?",
            (phone, telegram_id),
        )
        conn.commit()
        return cursor.rowcount > 0


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
