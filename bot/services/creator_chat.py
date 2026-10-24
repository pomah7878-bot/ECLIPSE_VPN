"""
Связь с создателем (v1.280): чат «установка бота ↔ владелец сервера лицензий».

Любой, кто установил бота (с GitHub, по лицензии или по пробному периоду), может
написать создателю из админ-меню: кнопка «🆘 Помощь для админа» или команда /creator.
Кнопку включает и выключает ТОЛЬКО создатель (на главной инсталляции): флаг
хранится у него, установки узнают о нём через POST /api/creator/status. Пока флаг
выключен (или статус ещё не получен), кнопка у всех скрыта. Помощь платная,
стоимость зависит от сложности задачи.

Схема:
  • Установка (клиент) шлёт сообщение на сервер лицензий: POST /api/creator/send.
    Ключ лицензии НЕ нужен: установка опознаётся по instance_id. При первом
    сообщении сервер выдаёт секретный токен чата — по нему установка потом
    забирает ответы (чужой токен угадать нельзя, значит, чужие ответы не прочитать).
  • Создателю (ADMIN_IDS главной инсталляции) приходит сообщение в Telegram с кнопками
    «Ответить» и «Заблокировать».
  • Ответ хранится на сервере; бот установки раз в ~5 минут спрашивает
    POST /api/creator/poll и пересылает ответ своим админам.

Защита от спама: лимит сообщений на установку в сутки, лимит запросов по IP,
длина текста, блокировка установки создателем. Токен хранится на сервере только
в виде хеша. Токены, ключи и данные клиентов в сообщение не попадают.
"""
import hashlib
import hmac
import logging
import re
import secrets
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

MAX_TEXT = 1500
MAX_PER_DAY = 20
ACTIVE_DAYS = 14
POLL_ACTIVE_SECONDS = 300
POLL_IDLE_SECONDS = 6 * 3600
_INSTANCE_RE = re.compile(r"^[A-Za-z0-9_-]{8,64}$")
_TOKEN_KEY = "creator_chat_token"
_ENABLED_KEY = "creator_help_enabled"          # главная: включено ли (создатель)
_CACHED_KEY = "creator_help_cached"            # установка: последнее известное состояние
_STATUS_TS_KEY = "creator_help_checked_at"     # установка: когда спрашивали в последний раз
STATUS_REFRESH_SECONDS = 30 * 60
_AFTER_KEY = "creator_chat_after_id"
_ACTIVITY_KEY = "creator_chat_activity"


def _conn():
    from database.connection import get_db
    return get_db()


def ensure_tables(conn) -> None:
    conn.execute("""
        CREATE TABLE IF NOT EXISTS creator_threads (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            instance_id TEXT NOT NULL UNIQUE,
            token_hash TEXT NOT NULL,
            admin_tg_id INTEGER,
            admin_username TEXT,
            bot_username TEXT,
            version TEXT,
            license_tier TEXT,
            partner_name TEXT,
            ip TEXT,
            blocked INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            last_in_at TEXT
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS creator_messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            thread_id INTEGER NOT NULL,
            direction TEXT NOT NULL,
            text TEXT NOT NULL,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_creator_messages_thread ON creator_messages(thread_id, id)")
    from bot.services.help_orders import ensure_tables as _ensure_help
    _ensure_help(conn)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS creator_chat_local (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            server_msg_id INTEGER UNIQUE,
            direction TEXT NOT NULL,
            text TEXT NOT NULL,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        )
    """)


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def clean_text(raw: Any) -> str:
    return str(raw or "").replace("\x00", "").strip()[:MAX_TEXT]


# ====================================================================== СЕРВЕР
def server_enabled() -> bool:
    """Включена ли «Помощь для админа» (решает только создатель). По умолчанию выключена."""
    from database.requests import get_setting
    return get_setting(_ENABLED_KEY, "0") == "1"


def set_server_enabled(on: bool) -> None:
    from database.requests import set_setting
    set_setting(_ENABLED_KEY, "1" if on else "0")


def server_status() -> Dict[str, Any]:
    return {"ok": True, "enabled": server_enabled()}


def server_receive(instance_id: str, token: str, text: str, meta: Dict[str, Any], ip: str) -> Dict[str, Any]:
    """Принимает сообщение установки. Возвращает JSON-ответ для клиента."""
    if not server_enabled():
        return {"ok": False, "reason": "disabled", "message": "Помощь для админа сейчас недоступна."}
    instance_id = (instance_id or "").strip()
    text = clean_text(text)
    if not _INSTANCE_RE.fullmatch(instance_id) or not text:
        return {"ok": False, "reason": "bad", "message": "Некорректный запрос."}
    meta = meta if isinstance(meta, dict) else {}

    def _s(key: str, limit: int = 64) -> str:
        return str(meta.get(key) or "").strip()[:limit]

    try:
        admin_tg = int(meta.get("admin_tg_id") or 0) or None
    except (TypeError, ValueError):
        admin_tg = None

    with _conn() as conn:
        ensure_tables(conn)
        row = conn.execute("SELECT * FROM creator_threads WHERE instance_id = ?", (instance_id,)).fetchone()
        new_token = None
        if row is None:
            new_token = secrets.token_urlsafe(24)
            conn.execute(
                "INSERT INTO creator_threads (instance_id, token_hash, admin_tg_id, admin_username, bot_username, "
                "version, license_tier, partner_name, ip, last_in_at) VALUES (?,?,?,?,?,?,?,?,?,CURRENT_TIMESTAMP)",
                (instance_id, _hash(new_token), admin_tg, _s("admin_username"), _s("bot_username"),
                 _s("version", 24), _s("license_tier", 24), _s("partner_name", 80), ip),
            )
            row = conn.execute("SELECT * FROM creator_threads WHERE instance_id = ?", (instance_id,)).fetchone()
        else:
            if not token or not hmac.compare_digest(_hash(str(token)), row["token_hash"]):
                return {"ok": False, "reason": "auth",
                        "message": "Не удалось подтвердить эту установку бота."}
            conn.execute(
                "UPDATE creator_threads SET admin_tg_id = COALESCE(?, admin_tg_id), "
                "admin_username = COALESCE(NULLIF(?, ''), admin_username), "
                "bot_username = COALESCE(NULLIF(?, ''), bot_username), version = COALESCE(NULLIF(?, ''), version), "
                "license_tier = COALESCE(NULLIF(?, ''), license_tier), partner_name = COALESCE(NULLIF(?, ''), partner_name), "
                "ip = ?, last_in_at = CURRENT_TIMESTAMP WHERE id = ?",
                (admin_tg, _s("admin_username"), _s("bot_username"), _s("version", 24),
                 _s("license_tier", 24), _s("partner_name", 80), ip, row["id"]),
            )
        thread_id = row["id"]
        if row["blocked"]:
            # заблокированному не сообщаем об этом: ответ выглядит как успех, но создателю ничего не приходит
            return {"ok": True, "token": new_token, "delivered": False}
        today = conn.execute(
            "SELECT COUNT(*) FROM creator_messages WHERE thread_id = ? AND direction = 'in' "
            "AND created_at >= datetime('now','-1 day')", (thread_id,)
        ).fetchone()[0]
        if today >= MAX_PER_DAY:
            return {"ok": False, "reason": "limit", "message": "Слишком много сообщений за сутки. Попробуйте завтра."}
        cur = conn.execute(
            "INSERT INTO creator_messages (thread_id, direction, text) VALUES (?, 'in', ?)", (thread_id, text)
        )
        return {"ok": True, "token": new_token, "delivered": True, "thread_id": thread_id, "msg_id": cur.lastrowid}


def server_poll(instance_id: str, token: str, after_id: int) -> Dict[str, Any]:
    instance_id = (instance_id or "").strip()
    if not _INSTANCE_RE.fullmatch(instance_id) or not token:
        return {"ok": False, "reason": "bad", "messages": []}
    try:
        after_id = max(0, int(after_id or 0))
    except (TypeError, ValueError):
        after_id = 0
    with _conn() as conn:
        ensure_tables(conn)
        row = conn.execute("SELECT id, token_hash FROM creator_threads WHERE instance_id = ?", (instance_id,)).fetchone()
        if row is None or not hmac.compare_digest(_hash(str(token)), row["token_hash"]):
            return {"ok": False, "reason": "auth", "messages": []}
        rows = conn.execute(
            "SELECT id, text, created_at FROM creator_messages WHERE thread_id = ? AND direction = 'out' "
            "AND id > ? ORDER BY id LIMIT 20", (row["id"], after_id)
        ).fetchall()
    from bot.services.help_orders import orders_for_thread
    return {"ok": True, "messages": [{"id": r["id"], "text": r["text"], "created_at": r["created_at"]} for r in rows],
            "orders": orders_for_thread(row["id"])}


def server_reply(thread_id: int, text: str) -> Optional[int]:
    text = clean_text(text)
    if not text:
        return None
    with _conn() as conn:
        ensure_tables(conn)
        if not conn.execute("SELECT 1 FROM creator_threads WHERE id = ?", (thread_id,)).fetchone():
            return None
        return conn.execute(
            "INSERT INTO creator_messages (thread_id, direction, text) VALUES (?, 'out', ?)", (thread_id, text)
        ).lastrowid


def get_thread(thread_id: int) -> Optional[Dict[str, Any]]:
    with _conn() as conn:
        ensure_tables(conn)
        row = conn.execute("SELECT * FROM creator_threads WHERE id = ?", (thread_id,)).fetchone()
        return dict(row) if row else None


def list_threads(limit: int = 10) -> List[Dict[str, Any]]:
    with _conn() as conn:
        ensure_tables(conn)
        rows = conn.execute(
            "SELECT t.*, (SELECT COUNT(*) FROM creator_messages m WHERE m.thread_id = t.id) AS total "
            "FROM creator_threads t ORDER BY COALESCE(t.last_in_at, t.created_at) DESC LIMIT ?", (limit,)
        ).fetchall()
        return [dict(r) for r in rows]


def thread_history(thread_id: int, limit: int = 6) -> List[Dict[str, Any]]:
    with _conn() as conn:
        ensure_tables(conn)
        rows = conn.execute(
            "SELECT direction, text, created_at FROM creator_messages WHERE thread_id = ? ORDER BY id DESC LIMIT ?",
            (thread_id, limit),
        ).fetchall()
        return [dict(r) for r in reversed(rows)]


def set_blocked(thread_id: int, blocked: bool) -> None:
    with _conn() as conn:
        ensure_tables(conn)
        conn.execute("UPDATE creator_threads SET blocked = ? WHERE id = ?", (1 if blocked else 0, thread_id))


def _esc(value: Any) -> str:
    from bot.utils.text import escape_html
    return escape_html(str(value or ""))


def thread_title(t: Dict[str, Any]) -> str:
    """Короткое описание установки для создателя (данные со слов бота, не проверены)."""
    who = f"@{t['admin_username']}" if t.get("admin_username") else (f"tg{t['admin_tg_id']}" if t.get("admin_tg_id") else "аноним")
    bot = f"@{t['bot_username']}" if t.get("bot_username") else "бот не указан"
    extra = ", ".join(x for x in (t.get("partner_name"), t.get("license_tier") and f"лицензия: {t['license_tier']}",
                                  t.get("version") and f"версия {t['version']}") if x)
    return f"{who} · {bot}" + (f" · {extra}" if extra else "")


def creator_kb(thread_id: int, blocked: bool = False):
    from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="💼 Выставить предложение", callback_data=f"ho_new:{thread_id}")],
        [InlineKeyboardButton(text="✍️ Ответить", callback_data=f"cr_reply:{thread_id}"),
         InlineKeyboardButton(text="✅ Разблокировать" if blocked else "🚫 Заблокировать",
                              callback_data=f"cr_block:{thread_id}")],
        [InlineKeyboardButton(text="💼 Заказы и прайс", callback_data="ho_home"),
         InlineKeyboardButton(text="📜 История", callback_data=f"cr_open:{thread_id}")],
        [InlineKeyboardButton(text="⬅️ Назад", callback_data="creator_chats_list"),
         InlineKeyboardButton(text="🈴 На главную", callback_data="start")],
    ])


async def notify_creator(thread_id: int, text: str) -> None:
    """Пересылает новое сообщение создателю (ADMIN_IDS главной инсталляции)."""
    try:
        from bot.utils.runtime_state import get_bot_instance
        from config import ADMIN_IDS
        bot = get_bot_instance()
        t = get_thread(thread_id)
        if not bot or not t:
            return
        body = (f"✉️ <b>Сообщение от установки #{thread_id}</b>\n"
                f"<i>{_esc(thread_title(t))}</i>\n\n{_esc(text)}")
        for admin_id in ADMIN_IDS:
            try:
                await bot.send_message(admin_id, body, parse_mode="HTML", reply_markup=creator_kb(thread_id, bool(t["blocked"])))
            except Exception as exc:
                logger.debug(f"creator_chat: не отправлено админу {admin_id}: {exc}")
    except Exception as exc:
        logger.warning(f"creator_chat.notify_creator: {exc}")


# ===================================================================== КЛИЕНТ
def get_token() -> str:
    from database.requests import get_setting
    return get_setting(_TOKEN_KEY, "") or ""


def help_enabled() -> bool:
    """Показывать ли кнопку на этой установке (по последнему ответу сервера создателя)."""
    from database.requests import get_setting
    return get_setting(_CACHED_KEY, "0") == "1"


async def refresh_status(force: bool = False) -> bool:
    """Спрашивает у сервера создателя, включена ли помощь. При недоступности сервера
    остаётся прежнее значение. Возвращает актуальное состояние."""
    import time
    from database.requests import get_setting, set_setting
    try:
        last = float(get_setting(_STATUS_TS_KEY, "0") or 0)
    except ValueError:
        last = 0.0
    if not force and time.time() - last < STATUS_REFRESH_SECONDS:
        return help_enabled()
    from bot.services.license import get_instance_id
    state, data = await _post("/api/creator/status", {"instance_id": get_instance_id()})
    if state == "ok":
        set_setting(_CACHED_KEY, "1" if data.get("enabled") else "0")
        set_setting(_STATUS_TS_KEY, str(time.time()))
    return help_enabled()


def _touch_activity() -> None:
    from database.requests import set_setting
    from datetime import datetime
    set_setting(_ACTIVITY_KEY, datetime.utcnow().isoformat())


def poll_interval() -> int:
    """Как часто спрашивать ответы: 5 минут после недавнего общения, иначе раз в 6 часов."""
    from database.requests import get_setting
    from datetime import datetime, timedelta
    if not get_token():
        return POLL_IDLE_SECONDS
    try:
        last = datetime.fromisoformat(get_setting(_ACTIVITY_KEY, "") or "")
    except ValueError:
        return POLL_IDLE_SECONDS
    return POLL_ACTIVE_SECONDS if datetime.utcnow() - last < timedelta(days=ACTIVE_DAYS) else POLL_IDLE_SECONDS


def save_local(direction: str, text: str, server_msg_id: Optional[int] = None) -> bool:
    with _conn() as conn:
        ensure_tables(conn)
        cur = conn.execute(
            "INSERT OR IGNORE INTO creator_chat_local (server_msg_id, direction, text) VALUES (?,?,?)",
            (server_msg_id, direction, text),
        )
        return cur.rowcount > 0


def recent_local(limit: int = 5) -> List[Dict[str, Any]]:
    with _conn() as conn:
        ensure_tables(conn)
        rows = conn.execute(
            "SELECT direction, text, created_at FROM creator_chat_local ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()
        return [dict(r) for r in reversed(rows)]


async def _post(path: str, payload: Dict[str, Any]) -> Tuple[str, Dict[str, Any]]:
    import aiohttp
    from bot.services.license import get_license_server_url
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(get_license_server_url() + path, json=payload,
                                    timeout=aiohttp.ClientTimeout(total=15)) as resp:
                status = resp.status
                data = await resp.json(content_type=None)
    except Exception as exc:
        logger.warning(f"creator_chat: сервер недоступен ({exc})")
        return "unreachable", {}
    if status == 429:
        return "limit", {"message": "Слишком часто. Попробуйте через несколько минут."}
    if status != 200 or not isinstance(data, dict):
        return "unreachable", {}
    return ("ok" if data.get("ok") else "denied"), data


async def _collect_meta(tg_user) -> Dict[str, Any]:
    meta: Dict[str, Any] = {"admin_tg_id": getattr(tg_user, "id", None),
                            "admin_username": getattr(tg_user, "username", "") or ""}
    try:
        from bot.utils.runtime_state import get_bot_instance
        bot = get_bot_instance()
        if bot:
            meta["bot_username"] = (await bot.get_me()).username or ""
    except Exception:
        pass
    try:
        from bot.utils.git_utils import run_git_command
        ok, out = run_git_command(["rev-parse", "--short", "HEAD"])
        if ok:
            meta["version"] = out.strip()[:24]
    except Exception:
        pass
    try:
        from bot.services.license import get_license_tier
        from database.requests import get_setting
        meta["license_tier"] = get_license_tier()
        meta["partner_name"] = get_setting("license_partner_name", "") or ""
    except Exception:
        pass
    return meta


async def send_to_creator(text: str, tg_user) -> Tuple[str, Dict[str, Any]]:
    """Отправляет сообщение создателю. ("ok" | "denied" | "limit" | "unreachable", данные)."""
    from bot.services.license import get_instance_id
    from database.requests import set_setting
    text = clean_text(text)
    if not text:
        return "denied", {"message": "Пустое сообщение."}
    state, data = await _post("/api/creator/send", {
        "instance_id": get_instance_id(), "token": get_token(), "text": text,
        "meta": await _collect_meta(tg_user),
    })
    if state == "ok":
        if data.get("token"):
            set_setting(_TOKEN_KEY, data["token"])
        save_local("me", text)
        _touch_activity()
    return state, data


async def poll_once(bot) -> int:
    """Забирает ответы создателя и пересылает их админам этого бота. Возвращает число новых."""
    from bot.services.license import get_instance_id
    from database.requests import get_setting, set_setting
    token = get_token()
    if not token:
        return 0
    after = int(get_setting(_AFTER_KEY, "0") or 0)
    state, data = await _post("/api/creator/poll", {"instance_id": get_instance_id(), "token": token, "after_id": after})
    if state != "ok":
        return 0
    fresh = 0
    from config import ADMIN_IDS
    from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
    for msg in data.get("messages") or []:
        mid = int(msg.get("id") or 0)
        text = clean_text(msg.get("text"))
        if not mid or not text:
            continue
        set_setting(_AFTER_KEY, str(max(mid, int(get_setting(_AFTER_KEY, "0") or 0))))
        if not save_local("creator", text, mid):
            continue
        fresh += 1
        _touch_activity()
        kb = InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="✍️ Ответить", callback_data="creator_chat_write"),
            InlineKeyboardButton(text="🈴 На главную", callback_data="start"),
        ]])
        for admin_id in ADMIN_IDS:
            try:
                await bot.send_message(admin_id, f"💬 <b>Ответ создателя</b>\n\n{_esc(text)}", parse_mode="HTML", reply_markup=kb)
            except Exception as exc:
                logger.debug(f"creator_chat: ответ не доставлен админу {admin_id}: {exc}")
    try:
        from bot.services.help_orders import client_sync
        fresh += await client_sync(bot, data.get("orders") or [])
    except Exception as exc:
        logger.warning(f"creator_chat: синхронизация заказов: {exc}")
    return fresh


async def run_creator_chat_poller(bot) -> None:
    """Фоновая задача партнёрской установки: раз в ~5 минут (после общения) забирает ответы."""
    import asyncio
    from bot.services.license import is_license_server
    if is_license_server():
        return
    logger.info("🆘 Помощь для админа: фоновая задача запущена")
    while True:
        try:
            await refresh_status()
            if get_token():
                await poll_once(bot)
        except asyncio.CancelledError:
            break
        except Exception as exc:
            logger.warning(f"creator_chat: ошибка опроса: {exc}")
        await asyncio.sleep(poll_interval() if get_token() else 1800)
