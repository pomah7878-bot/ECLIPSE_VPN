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

TERMS_VERSION = "2"
ACCEPT_AFTER_DAYS = 3
MAX_DESC = 1200
MAX_ITEMS = 40
OTHER_CAT = "Другое"
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
    "declined": "🚫 Отказано в выполнении",
}
FINAL_STATUSES = ("accepted", "canceled", "closed", "declined")

DEFAULT_PRICE_LIST = """# Формат: «## Категория», затем строки «Название | цена в рублях | пояснение для заказчика»
## Консультации
Быстрый вопрос | 1500 | Ответ или совет по настройке, до 30 минут переписки
Консультация голосом | 3000 | Разбор задачи голосом или с показом экрана, до 1 часа
Проверка настроек | 3500 | Просмотр настроек бота и сервера, письменные замечания и рекомендации без внесения изменений
## Диагностика
Диагностика проблемы | 3000 | Поиск причины неполадки и точная цена решения. Оплачивается отдельно, при заказе работ может быть засчитана в цену
Аудит безопасности сервера | 7500 | Проверка доступов, обновлений, открытых портов, резервных копий и журналов, отчёт со списком исправлений
## Настройка бота
Установка бота с GitHub | 6000 | Установка и первичная настройка бота на ваш сервер
Подключение оплаты | 4500 | Подключение и проверка одного способа оплаты (ЮKassa, СБП, криптовалюта и др.)
Домен и сертификат | 4500 | Привязка домена, выпуск и автоматическое продление сертификата
Оформление и тексты | 4500 | Название, оформление, приветствие, кнопки и тексты под ваш проект
Обновление и восстановление | 4500 | Обновление до актуальной версии, откат неудачного обновления, восстановление из резервной копии
## Сервер и VPN
Настройка VPN-сервера | 9000 | Установка 3x-ui и протоколов, подключение сервера к боту
Настройка CDN и белых списков | 9000 | Подключение CDN-режима и настройка работы при ограничениях мобильных сетей
Добавление сервера в бот | 4500 | Подключение дополнительного сервера и проверка выдачи ключей
Перенос на другой сервер | 9000 | Перенос бота, базы и настроек на новый сервер с проверкой работы
Резервное копирование | 4500 | Автоматические копии базы и настроек с проверкой восстановления
## Доработки
Небольшая правка | 4500 | Изменение текста, кнопки или логики в одном месте, до 1,5 часа работы
Новая функция (этап) | 15000 | Один этап доработки: описание, реализация, проверка. Следующие этапы оформляются отдельными заказами
Интеграция с сервисом | 18000 | Подключение одного стороннего сервиса к боту через его API
Сложная доработка (этап) | 30000 | Крупный этап: новый раздел бота или изменение схемы работы. Состав и срок указываются в предложении
## Неполадки
Исправление неполадки | 6000 | Бот или сервер не работает: поиск причины и исправление или откат. Без новых функций
Срочное исправление | 9000 | То же с началом работы в течение 2 часов, включая ночь и выходные
## Поддержка
Поддержка на месяц | 7000 | Мониторинг, обновления и до 3 часов работы в течение месяца
Расширенная поддержка на месяц | 15000 | Всё из обычной поддержки, до 8 часов работы и приоритетный ответ
Обучение админа | 4500 | Как пользоваться ботом и панелью, до 1,5 часов, с записью инструкции
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
    cols = {r[1] for r in conn.execute("PRAGMA table_info(help_orders)").fetchall()}
    for name, ddl in (("list_price_rub", "REAL"), ("discount_text", "TEXT")):   # добавлены в 1.281
        if name not in cols:
            conn.execute(f"ALTER TABLE help_orders ADD COLUMN {name} {ddl}")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_help_orders_thread ON help_orders(thread_id, id)")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS help_order_payments (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            order_id INTEGER NOT NULL,
            payment_id TEXT NOT NULL,
            amount_rub REAL NOT NULL,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_help_order_payments_order ON help_order_payments(order_id)")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS help_orders_local (
            order_no INTEGER PRIMARY KEY,
            status TEXT NOT NULL
        )
    """)


# ------------------------------------------------------------ прайс и условия
def parse_price_list(text: str) -> List[Dict[str, Any]]:
    """Разбирает «## Категория» и строки «Название | цена | пояснение». Строки с # и пустые пропускаются."""
    items: List[Dict[str, Any]] = []
    cat = ""
    for raw in (text or "").splitlines():
        line = raw.strip()
        if line.startswith("##"):
            cat = line.lstrip("#").strip()[:40]
            continue
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
                      "note": parts[2][:200] if len(parts) > 2 else "", "cat": cat})
    return items[:MAX_ITEMS]




def _load_items() -> List[Dict[str, Any]]:
    import json
    from database.requests import get_setting
    raw = get_setting(_PRICES_KEY, "") or ""
    if raw.strip().startswith("["):
        try:
            data = json.loads(raw)
            items = [{"title": str(i.get("title") or "")[:MAX_TITLE], "price": int(i.get("price")),
                      "note": str(i.get("note") or "")[:200], "cat": str(i.get("cat") or "")[:40]}
                     for i in data if isinstance(i, dict)]
            return [i for i in items if i["title"] and MIN_PRICE <= i["price"] <= MAX_PRICE][:MAX_ITEMS]
        except (ValueError, TypeError):
            pass
    return parse_price_list(DEFAULT_PRICE_LIST)


def get_price_list() -> List[Dict[str, Any]]:
    return _load_items()


def _save_items(items: List[Dict[str, Any]]) -> None:
    import json
    from database.requests import set_setting
    set_setting(_PRICES_KEY, json.dumps(items[:MAX_ITEMS], ensure_ascii=False))


def parse_price(raw: Any) -> Optional[int]:
    """Цена из текста вида «4500», «4 500 ₽», «4500,00». None, если не подходит."""
    text = str(raw or "").strip().replace(",", ".")
    if "." in text:
        text = text.split(".", 1)[0]
    digits = "".join(ch for ch in text if ch.isdigit())
    if not digits:
        return None
    price = int(digits)
    return price if MIN_PRICE <= price <= MAX_PRICE else None


def categories(items: Optional[List[Dict[str, Any]]] = None) -> List[str]:
    """Категории в порядке появления в прайсе (позиции без категории идут в «Другое»)."""
    out: List[str] = []
    for it in (items if items is not None else _load_items()):
        c = it.get("cat") or OTHER_CAT
        if c not in out:
            out.append(c)
    return out


def add_price_item(title: str, price: int, note: str = "", cat: str = "") -> bool:
    items = _load_items()
    title = (title or "").strip()[:MAX_TITLE]
    if not title or len(items) >= MAX_ITEMS or not (MIN_PRICE <= int(price) <= MAX_PRICE):
        return False
    items.append({"title": title, "price": int(price), "note": (note or "").strip()[:200],
                  "cat": (cat or "").strip()[:40]})
    _save_items(items)
    return True


def update_price_item(idx: int, **fields: Any) -> bool:
    items = _load_items()
    if not (0 <= idx < len(items)):
        return False
    if "title" in fields:
        title = str(fields["title"] or "").strip()[:MAX_TITLE]
        if not title:
            return False
        items[idx]["title"] = title
    if "price" in fields:
        price = parse_price(fields["price"])
        if price is None:
            return False
        items[idx]["price"] = price
    if "note" in fields:
        items[idx]["note"] = str(fields["note"] or "").strip()[:200]
    if "cat" in fields:
        items[idx]["cat"] = str(fields["cat"] or "").strip()[:40]
    _save_items(items)
    return True


def delete_price_item(idx: int) -> bool:
    items = _load_items()
    if not (0 <= idx < len(items)):
        return False
    del items[idx]
    _save_items(items)
    return True


def move_price_item_up(idx: int) -> bool:
    items = _load_items()
    if not (1 <= idx < len(items)):
        return False
    items[idx - 1], items[idx] = items[idx], items[idx - 1]
    _save_items(items)
    return True


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
        "7. <b>Отказ исполнителя.</b> Мы вправе отказаться от выполнения заказа, указав причину. "
        "Тогда возвращаем деньги за невыполненную часть.\n"
        "8. <b>Доступ.</b> Только временный. Пароли и ключи мы не сохраняем; после работ смените их.\n"
        "9. <b>Результат.</b> Мы отвечаем за выполнение описанных действий, а не за внешние факторы: "
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


def decline_order(order_no: int, reason: str, refund_rub: Optional[float] = None) -> Tuple[bool, str]:
    """Отказ исполнителя. До оплаты: просто отказ с причиной. После оплаты: возврат (по умолчанию полный;
    если работа уже частично сделана, можно указать меньшую сумму). Сам возврат делается вручную в ЮKassa."""
    order = get_order(order_no)
    if not order:
        return False, "Заказ не найден."
    if order["status"] in FINAL_STATUSES:
        return False, "Этот заказ уже завершён."
    reason = (reason or "").strip()[:300]
    if len(reason) < 3:
        return False, "Укажите причину отказа: её увидит заказчик."
    paid = order["status"] in ("paid", "in_progress", "delivered")
    refund = 0.0
    if paid:
        amount = float(order["amount_rub"])
        refund = amount if refund_rub is None else max(0.0, min(float(refund_rub), amount))
    with _conn() as conn:
        cur = conn.execute(
            "UPDATE help_orders SET status = 'declined', closed_at = CURRENT_TIMESTAMP, refund_rub = ?, close_reason = ? "
            "WHERE id = ? AND status NOT IN ('accepted','canceled','closed','declined')",
            (refund, reason, order_no),
        )
        if cur.rowcount != 1:
            return False, "Этот заказ уже завершён."
    if paid and refund > 0:
        return True, f"Отказ оформлен. Верните {money(refund)} в кабинете ЮKassa вручную."
    if paid:
        return True, "Отказ оформлен без возврата."
    return True, "Отказ оформлен. Оплаты не было."


def order_public(o: Dict[str, Any]) -> Dict[str, Any]:
    return {"no": o["id"], "title": o["title"], "description": o["description"],
            "amount": o["amount_rub"], "status": o["status"], "refund": o.get("refund_rub") or 0,
            "reason": o.get("close_reason") or "", "list_price": o.get("list_price_rub") or o["amount_rub"],
            "discount": o.get("discount_text") or ""}


def price_line(amount: Any, list_price: Any = None, discount: str = "") -> str:
    """«Сумма: 5 400 ₽» или со скидкой: «Сумма: <s>6 000 ₽</s> 5 400 ₽ (−10%)»."""
    try:
        has = bool(list_price) and float(list_price) > float(amount)
    except (TypeError, ValueError):
        has = False
    if has:
        return f"Сумма: <s>{money(list_price)}</s> <b>{money(amount)}</b>" + (f" ({_esc(discount)})" if discount else "")
    return f"Сумма: <b>{money(amount)}</b>"


def apply_discount(order_no: int, mode: str, value: Any = None) -> Tuple[bool, str]:
    """Скидка в любой момент. mode: 'pct' (процент), 'rub' (рублей), 'reset' (убрать, только до оплаты).
    До оплаты меняется цена заказа (процент и рубли считаются от первоначальной цены).
    После оплаты скидка это частичный возврат: сумма фиксируется, возврат создатель делает вручную."""
    order = get_order(order_no)
    if not order:
        return False, "Заказ не найден."
    st = order["status"]
    try:
        num = float(str(value).replace(",", ".").replace(" ", "")) if value is not None else 0.0
    except ValueError:
        return False, "Нужно число."
    if mode == "pct" and not (0 < num <= 90):
        return False, "Процент скидки от 1 до 90."
    if mode == "rub" and num <= 0:
        return False, "Сумма скидки должна быть больше нуля."
    if st in ("offered", "awaiting_payment"):
        base = float(order.get("list_price_rub") or order["amount_rub"])
        if mode == "reset":
            new, text, listp = base, None, None
        else:
            off = round(base * num / 100) if mode == "pct" else round(num)
            new = base - off
            text = f"−{num:g}%" if mode == "pct" else f"−{off:.0f} ₽"
            listp = base
            if new < MIN_PRICE:
                return False, f"После скидки цена не может быть меньше {MIN_PRICE} ₽."
        with _conn() as conn:
            conn.execute(
                "UPDATE help_orders SET amount_rub = ?, list_price_rub = ?, discount_text = ?, "
                "yk_payment_id = CASE WHEN status = 'awaiting_payment' THEN NULL ELSE yk_payment_id END, "
                "yk_qr_url = CASE WHEN status = 'awaiting_payment' THEN NULL ELSE yk_qr_url END "
                "WHERE id = ? AND status IN ('offered','awaiting_payment')",
                (new, listp, text, order_no),
            )
        return True, (f"Скидка {text}. Новая цена: {money(new)}." if text else f"Скидка убрана. Цена: {money(new)}.")
    if st in ("paid", "in_progress", "delivered"):
        if mode == "reset":
            return False, "Уже оформленную скидку после оплаты отменить нельзя."
        paid = float(order["amount_rub"])
        already = float(order.get("refund_rub") or 0)
        part = round(paid * num / 100) if mode == "pct" else round(num)
        if part <= 0 or already + part > paid - MIN_PRICE:
            return False, f"Скидка не должна оставлять в оплате меньше {MIN_PRICE} ₽."
        text = (order.get("discount_text") or "")
        text = (text + ", " if text else "") + f"возврат {part:.0f} ₽"
        with _conn() as conn:
            conn.execute("UPDATE help_orders SET refund_rub = ?, discount_text = ? WHERE id = ? "
                         "AND status IN ('paid','in_progress','delivered')", (already + part, text, order_no))
        return True, f"Скидка {money(part)} оформлена. Верните эту сумму в кабинете ЮKassa вручную."
    return False, "К этому заказу скидку уже нельзя применить."


def orders_for_thread(thread_id: int) -> List[Dict[str, Any]]:
    """Что отдаётся установке при опросе: свежие заказы и все незавершённые."""
    with _conn() as conn:
        ensure_tables(conn)
        _auto_accept(conn)
        rows = conn.execute(
            "SELECT * FROM help_orders WHERE thread_id = ? AND (status NOT IN ('accepted','canceled','closed','declined') "
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
                InlineKeyboardButton(text=f"💼 Заказ #{order_no}", callback_data=f"ho_open:{order_no}"),
                InlineKeyboardButton(text="🈴 На главную", callback_data="start")]])
        for admin_id in ADMIN_IDS:
            try:
                await bot.send_message(admin_id, text, parse_mode="HTML", reply_markup=kb)
            except Exception as exc:
                logger.debug(f"help_orders: не отправлено создателю {admin_id}: {exc}")
    except Exception as exc:
        logger.warning(f"help_orders.notify_creator_text: {exc}")


async def _check_payment(order: Dict[str, Any]) -> str:
    """'paid' | 'pending' | 'canceled' | 'error'. Смотрит ВСЕ платежи заказа: после скидки старый QR
    мог остаться действующим, и если по нему заплатили (больше текущей цены), заказ всё равно оплачен,
    а разница фиксируется как переплата к возврату."""
    from bot.services.billing import check_yookassa_payment_status
    with _conn() as conn:
        ensure_tables(conn)
        rows = conn.execute("SELECT payment_id, amount_rub FROM help_order_payments WHERE order_id = ? ORDER BY id DESC",
                            (order["id"],)).fetchall()
    pays = [(r["payment_id"], float(r["amount_rub"])) for r in rows]
    if order.get("yk_payment_id") and order["yk_payment_id"] not in [p[0] for p in pays]:
        pays.insert(0, (order["yk_payment_id"], float(order["amount_rub"])))
    if not pays:
        return "pending"
    pending = errored = False
    for pid, amount in pays:
        try:
            status = await check_yookassa_payment_status(pid, order_id=f"help{order['id']}")
        except Exception as exc:
            logger.error(f"help_orders: ошибка проверки оплаты #{order['id']}: {exc}")
            errored = True
            continue
        if status == "succeeded":
            late = get_order(order["id"]) or order
            if late["status"] in ("declined", "canceled"):
                with _conn() as conn:
                    cur = conn.execute(
                        "UPDATE help_orders SET refund_rub = ? WHERE id = ? AND refund_rub = 0 "
                        "AND status IN ('declined','canceled')", (amount, order["id"]))
                if cur.rowcount == 1:
                    await notify_creator_text(
                        f"⚠️ <b>Заказ #{order['id']}</b>: после {'отказа' if late['status'] == 'declined' else 'отмены'} "
                        f"поступила оплата {money(amount)}. Верните её в кабинете ЮKassa.", order["id"])
                return "late"
            if _transition(order["id"], "paid", ("awaiting_payment", "offered"), "paid_at"):
                fresh = get_order(order["id"]) or order
                over = round(amount - float(fresh["amount_rub"]), 2)
                warn = ""
                if over > 0:
                    with _conn() as conn:
                        conn.execute("UPDATE help_orders SET refund_rub = ? WHERE id = ?", (over, order["id"]))
                    warn = f"\n⚠️ Оплачено {money(amount)} при цене {money(fresh['amount_rub'])}: верните {money(over)} в ЮKassa."
                await notify_creator_text(
                    f"💰 <b>Заказ #{order['id']} оплачен</b>\n{_esc(order['title'])}\n"
                    f"Сумма: {money(fresh['amount_rub'])}. Можно начинать.{warn}", order["id"])
            return "paid"
        if status != "canceled":
            pending = True
    if pending:
        return "pending"
    return "error" if errored else "canceled"


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
                    "message": "Условия обновились. Обновите бота (кнопка «🔄 Обновление») и откройте заказ заново."}
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
            conn.execute("INSERT INTO help_order_payments (order_id, payment_id, amount_rub) VALUES (?,?,?)",
                         (order["id"], result["yookassa_payment_id"], float(order["amount_rub"])))
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
    head = f"💼 <b>Заказ #{no}</b>: {_esc(o['title'])}\n{price_line(o['amount'], o.get('list_price'), o.get('discount'))}\n"
    st = o["status"]
    if st in ("offered", "awaiting_payment") and o.get("discount"):
        head = "🎁 <b>Для вас скидка!</b>\n" + head
    if st in ("paid", "in_progress", "delivered") and float(o.get("refund") or 0) > 0:
        head += f"🎁 Скидка оформлена: вам вернут {money(o['refund'])}. Деньги придут на ваш счёт в течение нескольких дней.\n"
    if st == "offered":
        body = head + (f"\n{_esc(o['description'])}\n" if o.get("description") else "") + \
            "\nСначала прочитайте условия, потом оплата. Работа начинается после оплаты."
        return body, [("📜 Условия и оплата", f"hlp_terms:{no}"), ("✖️ Отказаться", f"hlp_cancel:{no}")]
    if st == "awaiting_payment":
        return head + "\n💳 Ждём оплату.", [("💳 Оплатить", f"hlp_terms:{no}"), ("✅ Я оплатил", f"hlp_check:{no}")]
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
    if st == "declined":
        extra = f"\nПричина: {_esc(o['reason'])}" if o.get("reason") else ""
        if float(o.get("refund") or 0) > 0:
            extra += f"\n💸 Возврат: {money(o['refund'])}. Деньги придут на ваш счёт в течение нескольких дней."
        else:
            extra += "\nОплата не списывалась или возврата не требуется."
        return head + "\n🚫 Создатель не сможет выполнить этот заказ." + extra, None
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
            key = f"{status}|{o.get('amount')}|{o.get('refund') or 0}"     # смена статуса, цены или скидки
            row = conn.execute("SELECT status FROM help_orders_local WHERE order_no = ?", (no,)).fetchone()
            if row and row["status"] == key:
                continue
            conn.execute("INSERT OR REPLACE INTO help_orders_local (order_no, status) VALUES (?, ?)", (no, key))
        text, buttons = _client_notice(o)
        rows = [[InlineKeyboardButton(text=t, callback_data=c) for t, c in buttons]] if buttons else []
        rows.append([InlineKeyboardButton(text="📋 Мои заказы", callback_data="hlp_list"),
                     InlineKeyboardButton(text="🈴 На главную", callback_data="start")])
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
