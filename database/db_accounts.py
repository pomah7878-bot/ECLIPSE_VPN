"""
Личный кабинет на публичном сайте — учётные записи через OAuth
(Google/Яндекс/VK), не связанные с Telegram напрямую. См. также
bot/services/oauth.py (сам OAuth-обмен) и bot/services/anonymous_purchase.py
(покупки/ключи).
"""
from typing import Optional, Dict, Any
import sqlite3
from .connection import get_db

__all__ = [
    'get_or_create_site_account',
    'get_site_account_by_id',
    'get_site_account_by_telegram_id',
    'link_oauth_to_site_account',
    'attach_oauth_to_existing_account',
    'create_site_login_code',
    'consume_site_login_code',
    'link_purchase_to_account',
    'get_latest_purchase_for_account',
]


def get_or_create_site_account(
    provider: str,
    provider_user_id: str,
    email: Optional[str] = None,
    display_name: Optional[str] = None,
) -> Dict[str, Any]:
    with get_db() as conn:
        row = conn.execute(
            "SELECT * FROM site_accounts WHERE provider = ? AND provider_user_id = ?",
            (provider, provider_user_id),
        ).fetchone()
        if row:
            return dict(row)
        cursor = conn.execute(
            "INSERT INTO site_accounts (provider, provider_user_id, email, display_name) VALUES (?, ?, ?, ?)",
            (provider, provider_user_id, email, display_name),
        )
        account_id = cursor.lastrowid
        row = conn.execute("SELECT * FROM site_accounts WHERE id = ?", (account_id,)).fetchone()
        return dict(row)


def get_site_account_by_id(account_id: int) -> Optional[Dict[str, Any]]:
    with get_db() as conn:
        row = conn.execute("SELECT * FROM site_accounts WHERE id = ?", (account_id,)).fetchone()
        return dict(row) if row else None


def get_site_account_by_telegram_id(telegram_id: int) -> Optional[Dict[str, Any]]:
    with get_db() as conn:
        row = conn.execute("SELECT * FROM site_accounts WHERE telegram_id = ?", (telegram_id,)).fetchone()
        return dict(row) if row else None


def _get_or_create_telegram_site_account(telegram_id: int) -> Dict[str, Any]:
    """Аккаунт-«мост» для существующего клиента бота — использует
    telegram_id как псевдо-provider_user_id (provider='telegram'),
    сохраняя ту же уникальность, что и для OAuth-провайдеров."""
    existing = get_site_account_by_telegram_id(telegram_id)
    if existing:
        return existing
    with get_db() as conn:
        cursor = conn.execute(
            "INSERT INTO site_accounts (provider, provider_user_id, telegram_id) VALUES ('telegram', ?, ?)",
            (str(telegram_id), telegram_id),
        )
        account_id = cursor.lastrowid
        row = conn.execute("SELECT * FROM site_accounts WHERE id = ?", (account_id,)).fetchone()
        return dict(row)


def link_oauth_to_site_account(account_id: int, telegram_id: int) -> bool:
    """Привязывает telegram_id к уже существующему (например, OAuth) аккаунту —
    используется, когда клиент подтверждает через бота, что этот аккаунт сайта его.

    Если этот telegram_id уже занят ДРУГИМ аккаунтом (обычно — «мостовым»
    telegram-аккаунтом, созданным при входе по коду из бота ДО того, как
    человек привязал OAuth) — сливает их: переносит покупки на текущий
    аккаунт и удаляет дубликат, вместо того чтобы молча провалиться из-за
    конфликта уникальности telegram_id."""
    with get_db() as conn:
        duplicate = conn.execute(
            "SELECT id FROM site_accounts WHERE telegram_id = ? AND id != ?",
            (telegram_id, account_id),
        ).fetchone()
        if duplicate:
            duplicate_id = duplicate["id"]
            conn.execute(
                "UPDATE anonymous_purchases SET site_account_id = ? WHERE site_account_id = ?",
                (account_id, duplicate_id),
            )
            conn.execute("DELETE FROM site_accounts WHERE id = ?", (duplicate_id,))

        cursor = conn.execute(
            "UPDATE site_accounts SET telegram_id = ? WHERE id = ? AND telegram_id IS NULL",
            (telegram_id, account_id),
        )
        return cursor.rowcount > 0


def attach_oauth_to_existing_account(
    account_id: int,
    provider: str,
    provider_user_id: str,
    email: Optional[str] = None,
    display_name: Optional[str] = None,
) -> bool:
    """Прикрепляет OAuth-идентичность к уже существующему (обычно
    telegram-мостовому) аккаунту — превращает разовый вход по коду из
    бота в аккаунт с быстрым OAuth-входом на будущее. Отклоняет, если
    этот provider+provider_user_id уже занят ДРУГИМ аккаунтом."""
    with get_db() as conn:
        existing = conn.execute(
            "SELECT id FROM site_accounts WHERE provider = ? AND provider_user_id = ?",
            (provider, provider_user_id),
        ).fetchone()
        if existing and existing["id"] != account_id:
            return False
        cursor = conn.execute(
            """UPDATE site_accounts
               SET provider = ?, provider_user_id = ?,
                   email = COALESCE(?, email), display_name = COALESCE(?, display_name)
               WHERE id = ?""",
            (provider, provider_user_id, email, display_name, account_id),
        )
        return cursor.rowcount > 0


def create_site_login_code(telegram_id: int, ttl_minutes: int = 10) -> str:
    """Создаёт одноразовый недолгоживущий код для входа в личный кабинет
    на сайте для УЖЕ существующего клиента бота (со всеми его реальными
    ключами) — отличается от claim_code для анонимных покупок."""
    import secrets as _secrets
    import datetime as _dt

    alphabet = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
    code = "".join(_secrets.choice(alphabet) for _ in range(4)) + "-" + "".join(_secrets.choice(alphabet) for _ in range(4))
    expires_at = (_dt.datetime.utcnow() + _dt.timedelta(minutes=ttl_minutes)).strftime("%Y-%m-%d %H:%M:%S")

    with get_db() as conn:
        for _ in range(5):
            try:
                conn.execute(
                    "INSERT INTO site_login_codes (code, telegram_id, expires_at) VALUES (?, ?, ?)",
                    (code, telegram_id, expires_at),
                )
                return code
            except sqlite3.IntegrityError:
                code = "".join(_secrets.choice(alphabet) for _ in range(4)) + "-" + "".join(_secrets.choice(alphabet) for _ in range(4))
        raise RuntimeError("Не удалось сгенерировать уникальный код входа")


def consume_site_login_code(code: str) -> Optional[int]:
    """Проверяет и «сжигает» (одноразово) код входа. Возвращает telegram_id,
    если код валиден, не использован и не истёк — иначе None."""
    with get_db() as conn:
        row = conn.execute(
            """SELECT telegram_id FROM site_login_codes
               WHERE code = ? AND used = 0 AND expires_at > datetime('now')""",
            (code.strip().upper(),),
        ).fetchone()
        if not row:
            return None
        conn.execute("UPDATE site_login_codes SET used = 1 WHERE code = ?", (code.strip().upper(),))
        return row["telegram_id"]


def link_purchase_to_account(order_id: str, site_account_id: int) -> bool:
    with get_db() as conn:
        cursor = conn.execute(
            "UPDATE anonymous_purchases SET site_account_id = ? WHERE order_id = ?",
            (site_account_id, order_id),
        )
        return cursor.rowcount > 0


def get_latest_purchase_for_account(site_account_id: int) -> Optional[Dict[str, Any]]:
    """Последняя покупка/продление с готовым ключом для аккаунта —
    используется, чтобы сразу показать личный кабинет после OAuth-входа,
    без ручного ввода кода."""
    with get_db() as conn:
        row = conn.execute(
            """SELECT * FROM anonymous_purchases
               WHERE site_account_id = ? AND vpn_key_id IS NOT NULL
               ORDER BY id DESC LIMIT 1""",
            (site_account_id,),
        ).fetchone()
        return dict(row) if row else None
def create_oauth_exchange_code(account_id: int, ttl_minutes: int = 10) -> str:
    """Создаёт одноразовый короткоживущий код обмена OAuth-сессии
    (полученной в системном браузере при входе через Google/Яндекс/VK) на
    cookie-сессию нативного Android-приложения. Не путать с
    site_login_code — тот для входа существующего клиента бота по коду
    ИЗ бота, этот — для передачи OAuth-сессии из браузера в приложение."""
    import secrets as _secrets
    import datetime as _dt

    alphabet = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
    code = "".join(_secrets.choice(alphabet) for _ in range(4)) + "-" + "".join(_secrets.choice(alphabet) for _ in range(4))
    expires_at = (_dt.datetime.utcnow() + _dt.timedelta(minutes=ttl_minutes)).strftime("%Y-%m-%d %H:%M:%S")

    with get_db() as conn:
        for _ in range(5):
            try:
                conn.execute(
                    "INSERT INTO oauth_exchange_codes (code, account_id, expires_at) VALUES (?, ?, ?)",
                    (code, account_id, expires_at),
                )
                return code
            except sqlite3.IntegrityError:
                code = "".join(_secrets.choice(alphabet) for _ in range(4)) + "-" + "".join(_secrets.choice(alphabet) for _ in range(4))
        raise RuntimeError("Не удалось сгенерировать уникальный код обмена OAuth")


def consume_oauth_exchange_code(code: str) -> Optional[int]:
    """Проверяет и «сжигает» (одноразово) код обмена OAuth. Возвращает
    account_id, если код валиден, не использован и не истёк — иначе None."""
    with get_db() as conn:
        row = conn.execute(
            """SELECT account_id FROM oauth_exchange_codes
               WHERE code = ? AND used = 0 AND expires_at > datetime('now')""",
            (code.strip().upper(),),
        ).fetchone()
        if not row:
            return None
        conn.execute("UPDATE oauth_exchange_codes SET used = 1 WHERE code = ?", (code.strip().upper(),))
        return row["account_id"]

