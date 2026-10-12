"""
Платные заказы «Помощи для админа» (v1.280).

Схема (подробности для людей — в условиях TERMS_TEXT ниже):
  • Создатель в чате с установкой выставляет ПРЕДЛОЖЕНИЕ: услуга из прайса или своя цена,
    описание, фиксированная сумма.
  • Админ установки видит предложение в своём боте, читает условия, нажимает «Согласен»
    и платит по СБП (ЮKassa главной инсталляции, QR). Работа начинается ТОЛЬКО после оплаты.
  • Каждый этап оплачивается целиком вперёд: «остатка» нет, в долг создатель не работает.
  • Статусы: offered → awaiting_payment → paid → in_progress → delivered → accepted
    (+ canceled, closed — закрыт с возвратом или без).
  • Через 3 дня после «работа сдана» без ответа заказ считается принятым.
  • Возврат денег создатель делает вручную в кабинете ЮKassa; бот только фиксирует сумму и причину.

Деньги, статусы и условия живут на ГЛАВНОЙ инсталляции. Установка общается с ней по
POST /api/creator/order (авторизация: instance_id + токен чата), ничего не хранит о деньгах,
кроме локальных отметок «о каком статусе уже уведомили».
"""
import base64
import logging
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

TERMS_VERSION = "1"
ACCEPT_AFTER_DAYS = 3
MAX_DESC = 1200
MAX_TITLE = 80
MIN_PRICE = 100
MAX_PRICE = 500_000

_PRICES_KEY = "help_price_list"
_EXECUTOR_KEY = "help_executor_info"

STATUS_TITLES = {
    "offered": "📋 Предложение ждёт вашего решения",
    "awaiting_payment": "💳 Ждёт оплаты",
    "paid": "✅ Оплачено, скоро начнём",
    "in_progress": "🛠 В работе",
    "delivered": "📦 Работа сдана, проверьте результат",
    "accepted": "🏁 Принято, заказ завершён",
    "canceled": "✖️ Отменён",
    "closed": "🔒 Закрыт",
}
FINAL_STATUSES = ("accepted", "canceled", "closed")

DEFAULT_PRICE_LIST = """# Название | цена в рублях | пояснение (необязательно)
Вопрос в чате (ответ или совет до 30 минут) | 1500
Диагностика: разобраться и назвать цену решения | 3000 | не возвращается, засчитывается в заказ по желанию создателя
Типовая настройка (домен, сертификат, оплата, включить функцию, обновление) | 4500
Исправление проблемы (бот или сервер не работает, ошибка, откат) | 6000
Настройка VPN-сервера (3x-ui, протоколы, CDN, подключение к боту) | 9000
Миграция на другой сервер | 9000
Новая функция или доработка бота (первый этап) | 15000
Абонентская поддержка на месяц (мониторинг, обновления, до 3 часов) | 7000
"""

DEFAULT_EXECUTOR = "Исполнитель: владелец сервиса ECLIPSE Unlimited (реквизиты и чек — в сообщении об оплате)."


def _conn():
    from database.connection import get_db
    return get_db()


def _esc(value: Any) -> str:
    from bot.utils.text import escape_html
    return escape_html(str(value or ""))


def money(value: Any) -> str:
    try:
        return f"{float(value):,.0f}".replace(",", " ") + " ₽"
    except (TypeError, ValueError):
        return "—"


# --------------------------------------------------------------------- таблицы
def ensure_tables(conn) -> None:
    conn.execute("""
        CREATE TABLE IF NOT EXISTS help_orders (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            thread_id INTEGER NOT NULL,
            title TEXT NOT NULL,
            description TEXT NOT NULL DEFAULT '',
            amount_rub REAL NOT NULL,
            status TEXT NOT NULL DEFAULT 'offered',
            terms_version TEXT,
            accepted_terms_at TEXT,
            yk_payment_id TEXT,
            yk_qr_url TEXT,
            paid_at TEXT,
            started_at TEXT,
            delivered_at TEXT,
            closed_at TEXT,
            refund_rub REAL NOT NULL DEFAULT 0,
            close_reason TEXT,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_help_orders_thread ON help_orders(thread_id, id)")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS help_orders_local (
            order_no INTEGER PRIMARY KEY,
            status TEXT NOT NULL
        )
    """)


# ------------------------------------------------------------ прайс и условия
def parse_price_list(text: str) -> List[Dict[str, Any]]:
    """Разбирает список «Название | цена | пояснение». Строки с # и пустые пропускаются."""
    items: List[Dict[str, Any]] = []
    for raw in (text or "").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        parts = [p.strip() for p in line.split("|")]
        if len(parts) < 2 or not parts[0]:
            continue
        digits = "".join(ch for ch in parts[1] if ch.isdigit())
        if not digits:
            continue
        price = int(digits)
        if not (MIN_PRICE <= price <= MAX_PRICE):
            continue
        items.append({"title": parts[0][:MAX_TITLE], "price": price,
                      "note": parts[2][:200] if len(parts) > 2 else ""})
    return items[:12]


def get_price_text() -> str:
    from database.requests import get_setting
    return get_setting(_PRICES_KEY, "") or DEFAULT_PRICE_LIST


def get_price_list() -> List[Dict[str, Any]]:
    return parse_price_list(get_price_text())


def set_price_text(text: str) -> int:
    """Сохраняет прайс; возвращает число распознанных позиций (0 — не сохраняет)."""
    from database.requests import set_setting
    items = parse_price_list(text)
    if items:
        set_setting(_PRICES_KEY, text.strip())
    return len(items)


def reset_price_list() -> None:
    from database.requests import set_setting
    set_setting(_PRICES_KEY, "")


def get_executor_info() -> str:
    from database.requests import get_setting
    return (get_setting(_EXECUTOR_KEY, "") or DEFAULT_EXECUTOR)[:400]


def set_executor_info(text: str) -> None:
    from database.requests import set_setting
    set_setting(_EXECUTOR_KEY, (text or "").strip()[:400])


def terms_text() -> str:
    """Условия (версия TERMS_VERSION). При изменении смысла поднимайте версию."""
    return (
        "<b>Условия помощи для админа</b> (версия " + TERMS_VERSION + ")\n\n"
        "1. <b>Что делаем.</b> Только то, что описано в предложении. Цена в нём фиксирована и после "
        "оплаты не меняется. Расширение объёма это новый заказ.\n"
        "2. <b>Оплата.</b> Каждый этап оплачивается полностью до начала. Без оплаты работа не ведётся.\n"
        "3. <b>Сдача.</b> После сообщения «работа выполнена» у вас 3 дня, чтобы принять или написать "
        "замечания. Без ответа работа считается принятой. 14 дней после приёмки исправляем недочёты "
        "по этому заказу бесплатно.\n"
        "4. <b>Если закончить не удалось не по нашей вине</b> (нет доступа, вы не отвечаете 7 дней, "
        "сбой хостинга, Telegram или сторонних сервисов, блокировки, форс-мажор): выполненная часть "
        "остаётся оплаченной и не возвращается, за невыполненную часть возвращаем деньги или оставляем "
        "кредит на 90 дней.\n"
        "5. <b>Если не вышло по нашей вине:</b> возвращаем деньги за невыполненное.\n"
        "6. <b>Если вы отказались сами:</b> оплачивается выполненная часть и понесённые расходы, "
        "остаток возвращается.\n"
        "7. <b>Доступ.</b> Только временный. Пароли и ключи мы не сохраняем; после работ смените их.\n"
        "8. <b>Результат.</b> Мы отвечаем за выполнение описанных действий, а не за внешние факторы: "
        "блокировки операторами, поведение сторонних сервисов, доход вашего проекта.\n\n"
        "<i>" + _esc(get_executor_info()) + "</i>"
    )


# ---------------------------------------------------------------- сервер: заказы
def _auto_accept(conn) -> None:
    conn.execute(
        "UPDATE help_orders SET status = 'accepted', closed_at = CURRENT_TIMESTAMP "
        "WHERE status = 'delivered' AND delivered_at <= datetime('now', ?)",
        (f"-{ACCEPT_AFTER_DAYS} days",),
    )


def _row(order) -> Dict[str, Any]:
    return dict(order) if order is not None else {}


def create_order(thread_id: int, title: str, description: str, amount_rub: float) -> Optional[int]:
    title = (title or "").strip()[:MAX_TITLE]
    description = (description or "").strip()[:MAX_DESC]
    try:
        amount = round(float(amount_rub), 2)
    except (TypeError, ValueError):
        return None
    if not title or not (MIN_PRICE <= amount <= MAX_PRICE):
        return None
    with _conn() as conn:
        ensure_tables(conn)
        if not conn.execute("SELECT 1 FROM creator_threads WHERE id = ?", (thread_id,)).fetchone():
            return None
        return conn.execute(
            "INSERT INTO help_orders (thread_id, title, description, amount_rub) VALUES (?,?,?,?)",
            (thread_id, title, description, amount),
        ).lastrowid


def get_order(order_no: int) -> Optional[Dict[str, Any]]:
    with _conn() as conn:
        ensure_tables(conn)
        _auto_accept(conn)
        row = conn.execute("SELECT * FROM help_orders WHERE id = ?", (order_no,)).fetchone()
        return _row(row) if row else None


def list_orders(limit: int = 15, thread_id: Optional[int] = None) -> List[Dict[str, Any]]:
    with _conn() as conn:
        ensure_tables(conn)
        _auto_accept(conn)
        if thread_id:
            rows = conn.execute("SELECT * FROM help_orders WHERE thread_id = ? ORDER BY id DESC LIMIT ?",
                                (thread_id, limit)).fetchall()
        else:
            rows = conn.execute("SELECT * FROM help_orders ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        return [dict(r) for r in rows]


def _transition(order_no: int, to_status: str, from_statuses: Tuple[str, ...], stamp: Optional[str] = None) -> bool:
    """Атомарный переход статуса. True, если именно этот вызов его выполнил."""
    marks = ",".join("?" for _ in from_statuses)
    extra = f", {stamp} = CURRENT_TIMESTAMP" if stamp else ""
    with _conn() as conn:
        ensure_tables(conn)
        cur = conn.execute(
            f"UPDATE help_orders SET status = ?{extra} WHERE id = ? AND status IN ({marks})",
            (to_status, order_no, *from_statuses),
        )
        return cur.rowcount == 1


def mark_started(order_no: int) -> bool:
    return _transition(order_no, "in_progress", ("paid",), "started_at")


def mark_delivered(order_no: int) -> bool:
    return _transition(order_no, "delivered", ("paid", "in_progress"), "delivered_at")


def cancel_unpaid(order_no: int) -> bool:
    return _transition(order_no, "canceled", ("offered", "awaiting_payment"), "closed_at")


def close_order(order_no: int, refund_rub: float, reason: str) -> bool:
    """Закрывает оплаченный заказ с фиксацией возврата (сам возврат делается вручную в ЮKassa)."""
    order = get_order(order_no)
    if not order or order["status"] not in ("paid", "in_progress", "delivered"):
        return False
    try:
        refund = max(0.0, min(float(refund_rub), float(order["amount_rub"])))
    except (TypeError, ValueError):
        return False
    with _conn() as conn:
        cur = conn.execute(
            "UPDATE help_orders SET status = 'closed', closed_at = CURRENT_TIMESTAMP, refund_rub = ?, "
            "close_reason = ? WHERE id = ? AND status IN ('paid','in_progress','delivered')",
            (refund, (reason or "").strip()[:300], order_no),
        )
        return cur.rowcount == 1


def order_public(o: Dict[str, Any]) -> Dict[str, Any]:
    return {"no": o["id"], "title": o["title"], "description": o["description"],
            "amount": o["amount_rub"], "status": o["status"], "refund": o.get("refund_rub") or 0,
            "reason": o.get("close_reason") or ""}


def orders_for_thread(thread_id: int) -> List[Dict[str, Any]]:
    """Что отдаётся установке при опросе: свежие заказы и все незавершённые."""
    with _conn() as conn:
        ensure_tables(conn)
        _auto_accept(conn)
        rows = conn.execute(
            "SELECT * FROM help_orders WHERE thread_id = ? AND (status NOT IN ('accepted','canceled','closed') "
            "OR COALESCE(closed_at, created_at) >= datetime('now','-30 days')) ORDER BY id DESC LIMIT 20",
            (thread_id,),
        ).fetchall()
    return [order_public(dict(r)) for r in rows]


# ------------------------------------------------------- сервер: API установки
def _auth_thread(instance_id: str, token: str) -> Optional[Dict[str, Any]]:
    import hmac
    from bot.services import creator_chat as cc
    instance_id = (instance_id or "").strip()
    if not cc._INSTANCE_RE.fullmatch(instance_id) or not token:
        return None
    with _conn() as conn:
        cc.ensure_tables(conn)
        row = conn.execute("SELECT * FROM creator_threads WHERE instance_id = ?", (instance_id,)).fetchone()
    if row is None or not hmac.compare_digest(cc._hash(str(token)), row["token_hash"]):
        return None
    return dict(row)


def _qr_png(data: str) -> bytes:
    import io
    import qrcode
    qr = qrcode.QRCode(version=1, error_correction=qrcode.constants.ERROR_CORRECT_M, box_size=10, border=4)
    qr.add_data(data)
    qr.make(fit=True)
    bio = io.BytesIO()
    qr.make_image(fill_color="black", back_color="white").save(bio, format="PNG")
    return bio.getvalue()


async def _bot_username() -> str:
    try:
        from bot.utils.runtime_state import get_bot_instance
        bot = get_bot_instance()
        return (await bot.get_me()).username or "bot"
    except Exception:
        return "bot"


async def notify_creator_text(text: str, order_no: Optional[int] = None) -> None:
    try:
        from bot.utils.runtime_state import get_bot_instance
        from config import ADMIN_IDS
        from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
        bot = get_bot_instance()
        if not bot:
            return
        kb = None
        if order_no:
            kb = InlineKeyboardMarkup(inline_keyboard=[[
                InlineKeyboardButton(text=f"💼 Заказ #{order_no}", callback_data=f"ho_open:{order_no}")]])
        for admin_id in ADMIN_IDS:
            try:
                await bot.send_message(admin_id, text, parse_mode="HTML", reply_markup=kb)
            except Exception as exc:
                logger.debug(f"help_orders: не отправлено создателю {admin_id}: {exc}")
    except Exception as exc:
        logger.warning(f"help_orders.notify_creator_text: {exc}")


async def _check_payment(order: Dict[str, Any]) -> str:
    """'paid' | 'pending' | 'canceled' | 'error'. Первый, кто увидел оплату, переводит статус."""
    from bot.services.billing import check_yookassa_payment_status
    if not order.get("yk_payment_id"):
        return "pending"
    try:
        status = await check_yookassa_payment_status(order["yk_payment_id"], order_id=f"help{order['id']}")
    except Exception as exc:
        logger.error(f"help_orders: ошибка проверки оплаты #{order['id']}: {exc}")
        return "error"
    if status == "succeeded":
        if _transition(order["id"], "paid", ("awaiting_payment", "offered"), "paid_at"):
            await notify_creator_text(
                f"💰 <b>Заказ #{order['id']} оплачен</b>\n{_esc(order['title'])}\n"
                f"Сумма: {money(order['amount_rub'])}. Можно начинать.", order["id"])
        return "paid"
    if status == "canceled":
        return "canceled"
    return "pending"


async def api_order(instance_id: str, token: str, action: str, order_no: Any = None,
                    terms_version: str = "") -> Dict[str, Any]:
    thread = _auth_thread(instance_id, token)
    if thread is None:
        return {"ok": False, "reason": "auth"}
    action = str(action or "")
    if action == "list":
        return {"ok": True, "orders": orders_for_thread(thread["id"])}
    try:
        no = int(order_no)
    except (TypeError, ValueError):
        return {"ok": False, "reason": "bad"}
    order = get_order(no)
    if not order or order["thread_id"] != thread["id"]:
        return {"ok": False, "reason": "not_found", "message": "Заказ не найден."}

    if action == "get":
        data = {"ok": True, "order": order_public(order)}
        if order["status"] in ("offered", "awaiting_payment"):
            data["terms"] = terms_text()
            data["terms_version"] = TERMS_VERSION
        return data

    if action == "accept":
        if str(terms_version) != TERMS_VERSION:
            return {"ok": False, "reason": "terms_changed",
                    "message": "Условия обновились. Откройте заказ заново и прочитайте их."}
        if order["status"] not in ("offered", "awaiting_payment"):
            return {"ok": False, "reason": "state", "message": "Этот заказ уже нельзя оплатить."}
        if order["status"] == "awaiting_payment":
            res = await _check_payment(order)
            if res == "paid":
                return {"ok": True, "status": "paid"}
            if res == "error":
                return {"ok": False, "reason": "payment",
                        "message": "Не удалось проверить прежний платёж. Попробуйте через минуту."}
            if res == "pending" and order.get("yk_qr_url"):
                # прежний платёж ещё действует: показываем тот же QR, второй не создаём
                return {"ok": True, "status": "awaiting_payment", "amount": order["amount_rub"],
                        "qr_png": base64.b64encode(_qr_png(order["yk_qr_url"])).decode(),
                        "qr_url": order["yk_qr_url"]}
        from bot.services.billing import create_yookassa_qr_payment
        try:
            result = await create_yookassa_qr_payment(
                amount_rub=float(order["amount_rub"]), order_id=f"help{order['id']}",
                description=f"Помощь для админа, заказ №{order['id']}", bot_name=await _bot_username(),
            )
        except Exception as exc:
            logger.error(f"help_orders: не создана оплата #{order['id']}: {exc}")
            return {"ok": False, "reason": "payment", "message": "Не удалось создать оплату. Попробуйте позже."}
        with _conn() as conn:
            conn.execute(
                "UPDATE help_orders SET status = 'awaiting_payment', yk_payment_id = ?, yk_qr_url = ?, "
                "terms_version = ?, accepted_terms_at = CURRENT_TIMESTAMP "
                "WHERE id = ? AND status IN ('offered','awaiting_payment')",
                (result["yookassa_payment_id"], result.get("qr_url", ""), TERMS_VERSION, order["id"]),
            )
        await notify_creator_text(
            f"📝 Заказ #{order['id']}: условия приняты, ждём оплату ({money(order['amount_rub'])}).", order["id"])
        return {"ok": True, "status": "awaiting_payment", "amount": order["amount_rub"],
                "qr_png": base64.b64encode(result["qr_image_data"]).decode(), "qr_url": result.get("qr_url", "")}

    if action == "check":
        res = await _check_payment(order)
        fresh = get_order(no) or order
        return {"ok": True, "payment": res, "status": fresh["status"]}

    if action == "confirm":
        if _transition(no, "accepted", ("delivered",), "closed_at"):
            await notify_creator_text(f"🏁 Заказ #{no} принят заказчиком.", no)
            return {"ok": True, "status": "accepted"}
        return {"ok": False, "reason": "state", "message": "Заказ сейчас нельзя принять."}

    if action == "cancel":
        if cancel_unpaid(no):
            await notify_creator_text(f"✖️ Заказ #{no}: заказчик отказался до оплаты.", no)
            return {"ok": True, "status": "canceled"}
        return {"ok": False, "reason": "state",
                "message": "Оплаченный заказ отменяется через чат с создателем."}
    return {"ok": False, "reason": "bad"}


# ------------------------------------------------------------------------ клиент
async def call(action: str, order_no: Optional[int] = None, terms_version: str = "") -> Tuple[str, Dict[str, Any]]:
    from bot.services import creator_chat as cc
    from bot.services.license import get_instance_id
    token = cc.get_token()
    if not token:
        return "denied", {"message": "Сначала напишите в чат помощи."}
    return await cc._post("/api/creator/order", {
        "instance_id": get_instance_id(), "token": token, "action": action,
        "order_no": order_no, "terms_version": terms_version,
    })


def _client_notice(o: Dict[str, Any]) -> Tuple[str, Optional[List[Tuple[str, str]]]]:
    """Текст и кнопки (текст, callback) уведомления установки о статусе заказа."""
    no = o["no"]
    head = f"💼 <b>Заказ #{no}</b>: {_esc(o['title'])}\nСумма: <b>{money(o['amount'])}</b>\n"
    st = o["status"]
    if st == "offered":
        body = head + (f"\n{_esc(o['description'])}\n" if o.get("description") else "") + \
            "\nСначала прочитайте условия, потом оплата. Работа начинается после оплаты."
        return body, [("📜 Условия и оплата", f"hlp_terms:{no}"), ("✖️ Отказаться", f"hlp_cancel:{no}")]
    if st == "awaiting_payment":
        return head + "\n💳 Ждём оплату.", [("💳 Оплатить", f"hlp_pay:{no}"), ("✅ Я оплатил", f"hlp_check:{no}")]
    if st == "paid":
        return head + "\n✅ Оплата получена. Создатель скоро начнёт.", None
    if st == "in_progress":
        return head + "\n🛠 Работа началась.", None
    if st == "delivered":
        return head + (f"\n📦 Работа выполнена. Проверьте результат. Если замечаний нет, подтвердите. "
                       f"Без ответа через {ACCEPT_AFTER_DAYS} дня заказ считается принятым. "
                       "Замечания напишите в чат помощи."), [("✅ Принять работу", f"hlp_confirm:{no}")]
    if st == "accepted":
        return head + "\n🏁 Заказ завершён. Спасибо!", None
    if st == "canceled":
        return head + "\n✖️ Заказ отменён.", None
    if st == "closed":
        extra = ""
        if float(o.get("refund") or 0) > 0:
            extra = f"\nВозврат: {money(o['refund'])}. Он придёт на ваш счёт в течение нескольких дней."
        if o.get("reason"):
            extra += f"\nПричина: {_esc(o['reason'])}"
        return head + "\n🔒 Заказ закрыт." + extra, None
    return head, None


async def client_sync(bot, orders: List[Dict[str, Any]]) -> int:
    """Уведомляет админов установки о новых и изменившихся заказах. Возвращает число уведомлений."""
    from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
    from config import ADMIN_IDS
    sent = 0
    for o in orders or []:
        try:
            no = int(o["no"])
            status = str(o["status"])
        except (KeyError, TypeError, ValueError):
            continue
        with _conn() as conn:
            ensure_tables(conn)
            row = conn.execute("SELECT status FROM help_orders_local WHERE order_no = ?", (no,)).fetchone()
            if row and row["status"] == status:
                continue
            conn.execute("INSERT OR REPLACE INTO help_orders_local (order_no, status) VALUES (?, ?)", (no, status))
        text, buttons = _client_notice(o)
        rows = [[InlineKeyboardButton(text=t, callback_data=c) for t, c in buttons]] if buttons else []
        rows.append([InlineKeyboardButton(text="📋 Мои заказы", callback_data="hlp_list")])
        for admin_id in ADMIN_IDS:
            try:
                await bot.send_message(admin_id, text, parse_mode="HTML",
                                       reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))
            except Exception as exc:
                logger.debug(f"help_orders: не доставлено админу {admin_id}: {exc}")
        sent += 1
    return sent


def client_card(o: Dict[str, Any]) -> Tuple[str, List[Tuple[str, str]]]:
    text, buttons = _client_notice(o)
    return text, buttons or []
