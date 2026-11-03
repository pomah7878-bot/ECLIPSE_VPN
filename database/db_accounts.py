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
    'adopt_account_purchase_keys',
    'make_telegram_link_token',
    'site_account_phone',
    'find_site_account_by_phone',
    'make_phone_proof',
    'verify_phone_proof',
    'phone_merge_blocker',
    'merge_site_account_into',
    'verify_telegram_link_token',
    'get_site_referrer_code_for_order',
    'get_latest_purchase_for_account',
    'create_oauth_exchange_code',
    'consume_oauth_exchange_code',
    'create_email_login_code',
    'consume_email_login_code',
    'get_or_create_placeholder_user_for_site_account',
    'get_site_account_by_placeholder_user_id',
]


def get_or_create_site_account(
    provider: str,
    provider_user_id: str,
    email: Optional[str] = None,
    display_name: Optional[str] = None,
    referred_by_code: Optional[str] = None,
) -> Dict[str, Any]:
    with get_db() as conn:
        row = conn.execute(
            "SELECT * FROM site_accounts WHERE provider = ? AND provider_user_id = ?",
            (provider, provider_user_id),
        ).fetchone()
        if row:
            return dict(row)
        cursor = conn.execute(
            "INSERT INTO site_accounts (provider, provider_user_id, email, display_name, referred_by_code) VALUES (?, ?, ?, ?, ?)",
            (provider, provider_user_id, email, display_name, referred_by_code),
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


def _get_or_create_telegram_site_account(telegram_id: int, referred_by_code: Optional[str] = None) -> Dict[str, Any]:
    """Аккаунт-«мост» для существующего клиента бота — использует
    telegram_id как псевдо-provider_user_id (provider='telegram'),
    сохраняя ту же уникальность, что и для OAuth-провайдеров."""
    existing = get_site_account_by_telegram_id(telegram_id)
    if existing:
        return existing
    with get_db() as conn:
        cursor = conn.execute(
            "INSERT INTO site_accounts (provider, provider_user_id, telegram_id, referred_by_code) VALUES ('telegram', ?, ?, ?)",
            (str(telegram_id), telegram_id, referred_by_code),
        )
        account_id = cursor.lastrowid
        row = conn.execute("SELECT * FROM site_accounts WHERE id = ?", (account_id,)).fetchone()
        return dict(row)


def site_account_phone(account: Dict[str, Any]) -> Optional[str]:
    """Номер аккаунта: привязанный (phone) или тот, по которому он создан (provider='phone')."""
    if account.get("phone"):
        return account["phone"]
    return account.get("provider_user_id") if account.get("provider") == "phone" else None


def find_site_account_by_phone(phone_normalized: str, exclude_id: int = 0) -> Optional[Dict[str, Any]]:
    """Аккаунт, которому принадлежит номер: привязанный (phone) или созданный входом по нему."""
    with get_db() as conn:
        row = conn.execute(
            "SELECT * FROM site_accounts WHERE (phone = ? OR (provider = 'phone' AND provider_user_id = ?)) AND id != ? ORDER BY id LIMIT 1",
            (phone_normalized, phone_normalized, exclude_id),
        ).fetchone()
        return dict(row) if row else None


def make_phone_proof(account_id: int, other_id: int, phone: str, ttl_seconds: int = 600) -> str:
    """Подтверждение звонком, что номер чужого аккаунта принадлежит вошедшему: для «объединить / войти в тот»."""
    import time as _time
    expires = int(_time.time()) + ttl_seconds
    body = f"{account_id}.{other_id}.{phone}.{expires}"
    return f"{body}.{_sign_short('phoneproof:' + body)}"


def verify_phone_proof(proof: str, account_id: int):
    """(other_id, phone) из подтверждения или None (подделка, истёк, выдано другому аккаунту)."""
    import hmac as _hmac
    import time as _time
    try:
        acc, other, phone, expires, sig = (proof or "").split(".")
        if int(acc) != account_id or int(expires) < _time.time():
            return None
        if not _hmac.compare_digest(sig, _sign_short(f"phoneproof:{acc}.{other}.{phone}.{expires}")):
            return None
        return int(other), phone
    except ValueError:
        return None


def phone_merge_blocker(keep: Dict[str, Any], drop: Dict[str, Any]) -> Optional[str]:
    """Почему нельзя влить drop в keep без потерь (None — можно)."""
    if drop.get("provider") not in ("phone", "telegram"):
        return "Тот аккаунт создан входом через Google/Яндекс/VK — при объединении этот вход пропал бы."
    if keep.get("telegram_id") and drop.get("telegram_id") and keep["telegram_id"] != drop["telegram_id"]:
        return "К аккаунтам привязаны разные Telegram."
    if not drop.get("telegram_id") and drop.get("placeholder_user_id"):
        with get_db() as conn:
            row = conn.execute("SELECT personal_balance FROM users WHERE id = ?", (drop["placeholder_user_id"],)).fetchone()
        if row and (row["personal_balance"] or 0) > 0:
            return "На том аккаунте есть баланс — для переноса напишите в поддержку."
    return None


def merge_site_account_into(keep_id: int, drop_id: int, phone_normalized: str) -> Optional[str]:
    """Вливает аккаунт drop (вход по номеру / кодом из бота) в keep: покупки, ключи, Telegram, номер.
    Возвращает текст ошибки или None при успехе."""
    keep, drop = get_site_account_by_id(keep_id), get_site_account_by_id(drop_id)
    if not keep or not drop or keep_id == drop_id:
        return "Аккаунт не найден."
    blocker = phone_merge_blocker(keep, drop)
    if blocker:
        return blocker
    telegram_id = keep.get("telegram_id") or drop.get("telegram_id")
    keep_placeholder = keep.get("placeholder_user_id")
    if not telegram_id:
        keep_placeholder = get_or_create_placeholder_user_for_site_account(keep_id)
    with get_db() as conn:
        owner = (conn.execute("SELECT id FROM users WHERE telegram_id = ?", (telegram_id,)).fetchone() or {"id": None})["id"] \
            if telegram_id else keep_placeholder
        if not owner:
            return "Пользователь Telegram не найден."
        for placeholder in {drop.get("placeholder_user_id"), keep_placeholder if telegram_id else None} - {None, owner}:
            conn.execute("UPDATE vpn_keys SET user_id = ? WHERE user_id = ?", (owner, placeholder))
        conn.execute("UPDATE anonymous_purchases SET site_account_id = ? WHERE site_account_id = ?", (keep_id, drop_id))
        conn.execute("DELETE FROM site_accounts WHERE id = ?", (drop_id,))
        conn.execute("UPDATE site_accounts SET phone = ?, telegram_id = ? WHERE id = ?", (phone_normalized, telegram_id, keep_id))
    return None


def get_or_create_site_account_by_phone(phone_normalized: str, referred_by_code: Optional[str] = None) -> Dict[str, Any]:
    """Аккаунт по номеру телефона (provider='phone', provider_user_id=
    нормализованный номер) — подтверждается звонком через zvonok.com
    при каждом входе. Повторный вход тем же номером возвращает ТОТ ЖЕ
    аккаунт (со всей историей покупок), а не создаёт новый."""
    with get_db() as conn:
        # Сначала аккаунт, к которому этот номер привязан как второй способ входа (Google/Яндекс/VK + телефон),
        # иначе вход по добавленному номеру открывал бы новый пустой аккаунт
        row = conn.execute(
            "SELECT * FROM site_accounts WHERE phone = ? ORDER BY id LIMIT 1", (phone_normalized,),
        ).fetchone() or conn.execute(
            "SELECT * FROM site_accounts WHERE provider = 'phone' AND provider_user_id = ?",
            (phone_normalized,),
        ).fetchone()
        if row:
            return dict(row)
        cursor = conn.execute(
            "INSERT INTO site_accounts (provider, provider_user_id, referred_by_code) VALUES ('phone', ?, ?)",
            (phone_normalized, referred_by_code),
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
        if cursor.rowcount:
            # Ключи, купленные в кабинете до привязки, лежат на служебной личности аккаунта —
            # переносим их на Telegram-пользователя, иначе после привязки они пропадут из кабинета
            conn.execute(
                """UPDATE vpn_keys SET user_id = (SELECT id FROM users WHERE telegram_id = ?)
                   WHERE user_id = (SELECT placeholder_user_id FROM site_accounts WHERE id = ?)
                     AND EXISTS (SELECT 1 FROM users WHERE telegram_id = ?)""",
                (telegram_id, account_id, telegram_id),
            )
        return cursor.rowcount > 0


def _sign_short(message: str) -> str:
    import hashlib as _hashlib
    import hmac as _hmac
    from database.db_settings import get_site_session_secret
    return _hmac.new(get_site_session_secret().encode(), message.encode(), _hashlib.sha256).hexdigest()[:20]


def _telegram_link_sig(account_id: int, expires: int) -> str:
    return _sign_short(f"tglink:{account_id}:{expires}")


def make_telegram_link_token(account_id: int, ttl_seconds: int = 900) -> str:
    """Код для ссылки t.me/<бот>?start=sitelink_<код>: клиент сайта привязывает свой Telegram.
    Подписан секретом сессий сайта; повторно не сработает — привязка ставится, только пока telegram_id пуст."""
    import time as _time
    expires = int(_time.time()) + ttl_seconds
    return f"{account_id}_{expires}_{_telegram_link_sig(account_id, expires)}"


def verify_telegram_link_token(token: str) -> Optional[int]:
    """account_id из кода привязки Telegram или None (подделка, истёк срок)."""
    import hmac as _hmac
    import time as _time
    try:
        account_id_str, expires_str, sig = (token or "").split("_")
        account_id, expires = int(account_id_str), int(expires_str)
    except ValueError:
        return None
    if expires < _time.time() or not _hmac.compare_digest(sig, _telegram_link_sig(account_id, expires)):
        return None
    return account_id


def set_account_phone(account_id: int, phone_normalized: str) -> bool:
    """Привязывает подтверждённый номер телефона к аккаунту личного
    кабинета (site_accounts.phone) — НЕ трогает provider/email, в
    отличие от attach_oauth_to_existing_account. Позволяет одному
    аккаунту иметь и email (через OAuth/telegram), и телефон
    одновременно, для полной связки. Отклоняет, если номер уже
    привязан к ДРУГОМУ аккаунту."""
    with get_db() as conn:
        # занят и привязанный номер, и номер, по которому создан другой аккаунт (вход по телефону)
        existing = conn.execute(
            "SELECT id FROM site_accounts WHERE (phone = ? OR (provider = 'phone' AND provider_user_id = ?)) AND id != ?",
            (phone_normalized, phone_normalized, account_id),
        ).fetchone()
        if existing:
            return False
        cursor = conn.execute(
            "UPDATE site_accounts SET phone = ? WHERE id = ?",
            (phone_normalized, account_id),
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


def adopt_account_purchase_keys(site_account_id: int, owner_user_id: int) -> int:
    """Ключи покупок, привязанных к сайт-аккаунту, переводит на его владельца,
    чтобы единый кабинет находил их по владельцу. Только ключи, которые ещё лежат
    на служебной личности (telegram_id < 0): забранные в бот ключи не трогаем."""
    where = """WHERE id IN (SELECT vpn_key_id FROM anonymous_purchases
                            WHERE site_account_id = ? AND vpn_key_id IS NOT NULL)
                 AND user_id != ?
                 AND user_id IN (SELECT id FROM users WHERE telegram_id < 0)"""
    args = (site_account_id, owner_user_id)
    with get_db() as conn:
        # Вызывается на каждый запрос кабинета: пишем в базу, только если есть что переносить
        if not conn.execute(f"SELECT 1 FROM vpn_keys {where} LIMIT 1", args).fetchone():
            return 0
        return conn.execute(f"UPDATE vpn_keys SET user_id = ? {where}", (owner_user_id, *args)).rowcount


def get_site_referrer_code_for_order(order_id: str) -> Optional[str]:
    """Реферальный код, под которым был создан сайт-аккаунт, оплативший
    этот заказ (через anonymous_purchases.site_account_id) — используется
    для начисления реферального вознаграждения за оплаченные (не
    пробные) покупки с сайта, где у покупателя может вообще не быть
    Telegram."""
    with get_db() as conn:
        row = conn.execute(
            """
            SELECT sa.referred_by_code
            FROM anonymous_purchases ap
            JOIN site_accounts sa ON sa.id = ap.site_account_id
            WHERE ap.order_id = ?
            """,
            (order_id,),
        ).fetchone()
    return row["referred_by_code"] if row and row["referred_by_code"] else None


def get_site_account_telegram_id_for_order(order_id: str) -> Optional[int]:
    """v1.195: telegram_id сайт-аккаунта, оплатившего заказ (для защиты от самореферала)."""
    with get_db() as conn:
        row = conn.execute(
            """
            SELECT sa.telegram_id
            FROM anonymous_purchases ap
            JOIN site_accounts sa ON sa.id = ap.site_account_id
            WHERE ap.order_id = ?
            """,
            (order_id,),
        ).fetchone()
    return int(row["telegram_id"]) if row and row["telegram_id"] else None


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
def create_oauth_exchange_code(account_id: int, ttl_minutes: int = 10, code_challenge: Optional[str] = None) -> str:
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
                    "INSERT INTO oauth_exchange_codes (code, account_id, expires_at, code_challenge) VALUES (?, ?, ?, ?)",
                    (code, account_id, expires_at, code_challenge),
                )
                return code
            except sqlite3.IntegrityError:
                code = "".join(_secrets.choice(alphabet) for _ in range(4)) + "-" + "".join(_secrets.choice(alphabet) for _ in range(4))
        raise RuntimeError("Не удалось сгенерировать уникальный код обмена OAuth")


def consume_oauth_exchange_code(code: str, code_verifier: Optional[str] = None) -> Optional[int]:
    """Проверяет и «сжигает» (одноразово) код обмена OAuth. Возвращает
    account_id, если код валиден, не использован и не истёк — иначе None.
    v1.162: если при создании кода был задан code_challenge (PKCE), нужен
    code_verifier, у которого base64url(sha256) совпадает с ним; при неверном
    verifier код НЕ сжигается (перехватчик не может его угадать)."""
    import base64 as _b64
    import hashlib as _hashlib
    import hmac as _hmac

    with get_db() as conn:
        row = conn.execute(
            """SELECT account_id, code_challenge FROM oauth_exchange_codes
               WHERE code = ? AND used = 0 AND expires_at > datetime('now')""",
            (code.strip().upper(),),
        ).fetchone()
        if not row:
            return None
        challenge = row["code_challenge"]
        if challenge:
            if not code_verifier:
                return None
            digest = _hashlib.sha256(code_verifier.encode("utf-8")).digest()
            expected = _b64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")
            if not _hmac.compare_digest(expected.encode("ascii"), str(challenge).encode("ascii")):
                return None
        conn.execute("UPDATE oauth_exchange_codes SET used = 1 WHERE code = ?", (code.strip().upper(),))
        return row["account_id"]


def create_email_login_code(email: str, ttl_minutes: int = 10) -> str:
    """Новый 6-значный код входа по Email. Предыдущий код этого адреса перестаёт
    действовать, счётчик попыток начинается заново."""
    import secrets as _secrets
    import datetime as _dt

    code = f"{_secrets.randbelow(1_000_000):06d}"
    expires_at = (_dt.datetime.utcnow() + _dt.timedelta(minutes=ttl_minutes)).strftime("%Y-%m-%d %H:%M:%S")
    code_hash = _sign_short(f"emailcode:{email}:{code}")  # до транзакции: секрет при первом вызове пишется в базу
    with get_db() as conn:
        conn.execute("DELETE FROM email_login_codes WHERE expires_at <= datetime('now')")
        conn.execute(
            "INSERT OR REPLACE INTO email_login_codes (email, code_hash, attempts, expires_at) VALUES (?, ?, 0, ?)",
            (email, code_hash, expires_at),
        )
    return code


def consume_email_login_code(email: str, code: str, max_attempts: int = 5) -> bool:
    """Проверяет код и «сжигает» его при успехе. После max_attempts неверных
    вводов код удаляется — нужен новый."""
    import hmac as _hmac

    code_hash = _sign_short(f"emailcode:{email}:{code}")
    with get_db() as conn:
        row = conn.execute(
            "SELECT code_hash, attempts FROM email_login_codes WHERE email = ? AND expires_at > datetime('now')",
            (email,),
        ).fetchone()
        if not row:
            return False
        if _hmac.compare_digest(row["code_hash"], code_hash):
            conn.execute("DELETE FROM email_login_codes WHERE email = ?", (email,))
            return True
        if row["attempts"] + 1 >= max_attempts:
            conn.execute("DELETE FROM email_login_codes WHERE email = ?", (email,))
        else:
            conn.execute("UPDATE email_login_codes SET attempts = attempts + 1 WHERE email = ?", (email,))
        return False


def get_or_create_placeholder_user_for_site_account(account_id: int) -> int:
    """Возвращает СТАБИЛЬНЫЙ внутренний users.id для этого сайт-аккаунта
    (без Telegram) — создаётся один раз и переиспользуется для ВСЕХ
    его покупок и реферальных начислений, в отличие от старого
    поведения, где каждая покупка получала одноразовую служебную
    личность. Благодаря этому у чисто сайтового пользователя может
    быть СВОЙ реферальный код (через ensure_user_referral_code) и
    накопленный баланс/дни от рефералов, привязанные к его же ключам."""
    import time
    from database.requests import get_or_create_user

    with get_db() as conn:
        row = conn.execute("SELECT placeholder_user_id FROM site_accounts WHERE id = ?", (account_id,)).fetchone()
        if row and row["placeholder_user_id"]:
            return row["placeholder_user_id"]

    placeholder_tg_id = -int(time.time() * 1000) - account_id
    placeholder_username = f"site_{account_id}"
    owner, _ = get_or_create_user(placeholder_tg_id, username=placeholder_username)
    placeholder_user_id = owner["id"]

    with get_db() as conn:
        conn.execute(
            "UPDATE site_accounts SET placeholder_user_id = ? WHERE id = ?",
            (placeholder_user_id, account_id),
        )
        conn.commit()
    return placeholder_user_id


def get_site_account_by_placeholder_user_id(user_id: int) -> Optional[Dict[str, Any]]:
    """Обратный поиск: сайт-аккаунт по его служебной (placeholder) личности
    в users — нужен, например, чтобы подписать в уведомлении админу, чей
    именно сайтовый аккаунт продлил ключ (email/провайдер), когда под
    рукой есть только владелец ключа (users.id из vpn_keys), а не сам
    исходный site_account_id."""
    with get_db() as conn:
        row = conn.execute(
            "SELECT * FROM site_accounts WHERE placeholder_user_id = ?", (user_id,)
        ).fetchone()
        return dict(row) if row else None

