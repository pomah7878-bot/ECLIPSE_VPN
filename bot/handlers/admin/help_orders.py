"""
Заказы «Помощи для админа» (v1.280) — экраны. Логика: bot/services/help_orders.py.

  • Установка (админ бота): «📋 Мои заказы», условия, оплата по СБП, приёмка работы.
  • Главная (создатель): предложения, оплаченные заказы, сдача работы, закрытие, прайс.
"""
import base64
import logging

from aiogram import Router, F
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.types import (BufferedInputFile, CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message)

from bot.services import help_orders as ho
from bot.states.admin_states import AdminStates
from bot.utils.admin import is_admin
from bot.utils.text import safe_edit_or_send

logger = logging.getLogger(__name__)
router = Router()

_UNREACHABLE = "⚠️ Сервер создателя сейчас недоступен. Попробуйте позже."


def _kb(rows):
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _btn(text, data):
    return InlineKeyboardButton(text=text, callback_data=data)


def _creator_only(user_id: int) -> bool:
    from bot.services.license import is_license_server
    return is_admin(user_id) and is_license_server()


# ===================================================================== УСТАНОВКА
async def _order_state(callback: CallbackQuery, action: str, no: int, terms_version: str = ""):
    state, data = await ho.call(action, no, terms_version)
    if state == "unreachable":
        await callback.answer(_UNREACHABLE, show_alert=True)
        return None
    if state != "ok":
        await callback.answer((data.get("message") or "Не удалось выполнить действие.")[:190], show_alert=True)
        return None
    return data


@router.callback_query(F.data == "hlp_list")
async def hlp_list(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        await callback.answer("⛔ Доступ запрещён", show_alert=True)
        return
    data = await _order_state(callback, "list", 0)
    if data is None:
        return
    orders = data.get("orders") or []
    if not orders:
        await callback.answer("Заказов пока нет. Опишите задачу в чате: создатель пришлёт предложение с ценой.",
                              show_alert=True)
        return
    rows = [[_btn(f"#{o['no']} {o['title']} · {ho.money(o['amount'])}"[:60], f"hlp_open:{o['no']}")] for o in orders[:10]]
    rows.append([_btn("⬅️ Назад", "creator_chat")])
    await safe_edit_or_send(callback.message, "📋 <b>Мои заказы помощи</b>", reply_markup=_kb(rows))
    await callback.answer()


@router.callback_query(F.data.startswith("hlp_open:"))
async def hlp_open(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        await callback.answer("⛔ Доступ запрещён", show_alert=True)
        return
    no = int(callback.data.split(":", 1)[1])
    data = await _order_state(callback, "get", no)
    if data is None:
        return
    text, buttons = ho.client_card(data["order"])
    rows = [[_btn(t, c) for t, c in buttons]] if buttons else []
    rows.append([_btn("⬅️ К заказам", "hlp_list")])
    await safe_edit_or_send(callback.message, text, reply_markup=_kb(rows))
    await callback.answer()


@router.callback_query(F.data.startswith("hlp_terms:"))
async def hlp_terms(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        await callback.answer("⛔ Доступ запрещён", show_alert=True)
        return
    no = int(callback.data.split(":", 1)[1])
    data = await _order_state(callback, "get", no)
    if data is None:
        return
    if not data.get("terms"):
        await callback.answer("Этот заказ уже нельзя оплатить.", show_alert=True)
        return
    o = data["order"]
    text = (f"{data['terms']}\n\n💼 <b>Заказ #{no}</b>: {ho._esc(o['title'])}\n"
            f"К оплате: <b>{ho.money(o['amount'])}</b> (СБП)")
    await safe_edit_or_send(callback.message, text, reply_markup=_kb([
        [_btn("✅ Согласен с условиями, оплатить", f"hlp_pay:{no}")],
        [_btn("⬅️ Назад", f"hlp_open:{no}")],
    ]))
    await callback.answer()


@router.callback_query(F.data.startswith("hlp_pay:"))
async def hlp_pay(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        await callback.answer("⛔ Доступ запрещён", show_alert=True)
        return
    no = int(callback.data.split(":", 1)[1])
    await callback.answer("Создаю оплату…")
    state, data = await ho.call("accept", no, ho.TERMS_VERSION)
    if state == "unreachable":
        await callback.message.answer(_UNREACHABLE)
        return
    if state != "ok":
        await callback.message.answer(f"❌ {data.get('message') or 'Не удалось создать оплату.'}")
        return
    if data.get("status") == "paid":
        await callback.message.answer("✅ Оплата уже получена. Создатель скоро начнёт.")
        return
    try:
        photo = BufferedInputFile(base64.b64decode(data["qr_png"]), filename=f"help{no}.png")
    except Exception:
        await callback.message.answer("❌ Не удалось показать QR. Нажмите «Оплатить» ещё раз.")
        return
    await callback.message.answer_photo(
        photo=photo,
        caption=(f"💳 <b>Оплата заказа #{no}</b>\n\nСумма: <b>{ho.money(data['amount'])}</b>\n\n"
                 "Отсканируйте QR в приложении вашего банка (СБП), затем нажмите «Я оплатил». "
                 "Работа начнётся после подтверждения оплаты."),
        parse_mode="HTML",
        reply_markup=_kb([[_btn("✅ Я оплатил", f"hlp_check:{no}")]]),
    )


@router.callback_query(F.data.startswith("hlp_check:"))
async def hlp_check(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        await callback.answer("⛔ Доступ запрещён", show_alert=True)
        return
    no = int(callback.data.split(":", 1)[1])
    data = await _order_state(callback, "check", no)
    if data is None:
        return
    if data.get("payment") == "paid" or data.get("status") in ("paid", "in_progress", "delivered", "accepted"):
        await callback.message.answer(f"✅ Заказ #{no} оплачен. Создатель скоро начнёт, обновления придут сюда.")
        await callback.answer()
    elif data.get("payment") == "canceled":
        await callback.answer("Платёж отменён. Откройте заказ и оплатите заново.", show_alert=True)
    elif data.get("payment") == "error":
        await callback.answer("Не удалось проверить оплату. Попробуйте через минуту.", show_alert=True)
    else:
        await callback.answer("Оплата ещё не поступила. Если вы уже оплатили, подождите минуту и нажмите ещё раз.",
                              show_alert=True)


@router.callback_query(F.data.startswith("hlp_confirm:"))
async def hlp_confirm(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        await callback.answer("⛔ Доступ запрещён", show_alert=True)
        return
    no = int(callback.data.split(":", 1)[1])
    data = await _order_state(callback, "confirm", no)
    if data is None:
        return
    await callback.message.answer(f"🏁 Заказ #{no} принят. Спасибо!")
    await callback.answer()


@router.callback_query(F.data.startswith("hlp_cancel:"))
async def hlp_cancel(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        await callback.answer("⛔ Доступ запрещён", show_alert=True)
        return
    no = int(callback.data.split(":", 1)[1])
    data = await _order_state(callback, "cancel", no)
    if data is None:
        return
    await callback.message.answer(f"✖️ Заказ #{no} отменён.")
    await callback.answer()


# ===================================================================== СОЗДАТЕЛЬ
def _creator_order_text(o) -> str:
    from bot.services import creator_chat as cc
    t = cc.get_thread(o["thread_id"]) or {}
    lines = [f"💼 <b>Заказ #{o['id']}</b> · установка #{o['thread_id']}",
             f"<i>{ho._esc(cc.thread_title(t)) if t else ''}</i>",
             f"{ho._esc(o['title'])}", f"Сумма: <b>{ho.money(o['amount_rub'])}</b>",
             f"Статус: {ho.STATUS_TITLES.get(o['status'], o['status'])}"]
    if o.get("description"):
        lines.append(f"\n{ho._esc(o['description'])}")
    if o.get("refund_rub"):
        lines.append(f"\nВозврат: {ho.money(o['refund_rub'])}. Сделайте его вручную в кабинете ЮKassa.")
    if o.get("close_reason"):
        lines.append(f"Причина: {ho._esc(o['close_reason'])}")
    return "\n".join(lines)


def _creator_order_kb(o) -> InlineKeyboardMarkup:
    no, st = o["id"], o["status"]
    rows = []
    if st == "paid":
        rows.append([_btn("▶️ Взял в работу", f"ho_start:{no}")])
    if st in ("paid", "in_progress"):
        rows.append([_btn("📦 Сдать работу", f"ho_deliver:{no}")])
    if st in ("offered", "awaiting_payment"):
        rows.append([_btn("✖️ Отозвать предложение", f"ho_withdraw:{no}")])
    if st in ("paid", "in_progress", "delivered"):
        rows.append([_btn("🔒 Закрыть (возврат или нет)", f"ho_close:{no}")])
    rows.append([_btn("💼 Все заказы", "ho_list"), _btn("🈴 На главную", "start")])
    return _kb(rows)


def home_view():
    """Текст и клавиатура раздела «Помощь для админа» на главной инсталляции."""
    from bot.services import creator_chat as cc
    on = cc.server_enabled()
    text = ("💼 <b>Помощь для админа</b>\n\n"
            f"Кнопка у установок: <b>{'включена' if on else 'выключена'}</b>\n"
            "Предложения выставляются из чата с установкой (/creator_chats).")
    kb = _kb([
        [_btn(f"🆘 Кнопка у всех: {'вкл' if on else 'выкл'}", "creator_help_toggle")],
        [_btn("📋 Заказы", "ho_list"), _btn("🏷 Прайс", "hp_screen")],
        [_btn("🧾 Сведения об исполнителе", "hp_executor")],
        [_btn("📜 Условия для заказчиков", "hp_terms")],
        [_btn("⬅️ Назад", "admin_panel")],
    ])
    return text, kb


@router.callback_query(F.data == "ho_home")
async def ho_home(callback: CallbackQuery):
    if not _creator_only(callback.from_user.id):
        await callback.answer("⛔ Доступ запрещён", show_alert=True)
        return
    text, kb = home_view()
    await safe_edit_or_send(callback.message, text, reply_markup=kb)
    await callback.answer()


@router.callback_query(F.data == "hp_terms")
async def hp_terms(callback: CallbackQuery):
    if not _creator_only(callback.from_user.id):
        await callback.answer("⛔ Доступ запрещён", show_alert=True)
        return
    await callback.message.answer(ho.terms_text(), parse_mode="HTML")
    await callback.answer()


@router.callback_query(F.data == "ho_list")
@router.message(Command("help_orders"))
async def ho_list(event):
    uid = event.from_user.id
    if not _creator_only(uid):
        if isinstance(event, CallbackQuery):
            await event.answer("⛔ Доступ запрещён", show_alert=True)
        return
    orders = ho.list_orders(12)
    if not orders:
        text, rows = "Заказов пока нет.", []
    else:
        text = "💼 <b>Заказы помощи</b>"
        rows = [[_btn(f"#{o['id']} {ho.STATUS_TITLES.get(o['status'], o['status'])[:2]} {o['title']} · "
                      f"{ho.money(o['amount_rub'])}"[:60], f"ho_open:{o['id']}")] for o in orders]
    rows.append([_btn("⬅️ Назад", "ho_home")])
    if isinstance(event, CallbackQuery):
        await safe_edit_or_send(event.message, text, reply_markup=_kb(rows))
        await event.answer()
    else:
        await event.answer(text, parse_mode="HTML", reply_markup=_kb(rows))


@router.callback_query(F.data.startswith("ho_open:"))
async def ho_open(callback: CallbackQuery):
    if not _creator_only(callback.from_user.id):
        await callback.answer("⛔ Доступ запрещён", show_alert=True)
        return
    o = ho.get_order(int(callback.data.split(":", 1)[1]))
    if not o:
        await callback.answer("Заказ не найден", show_alert=True)
        return
    await callback.message.answer(_creator_order_text(o), parse_mode="HTML", reply_markup=_creator_order_kb(o))
    await callback.answer()


async def _status_action(callback: CallbackQuery, func, done_text: str):
    if not _creator_only(callback.from_user.id):
        await callback.answer("⛔ Доступ запрещён", show_alert=True)
        return
    no = int(callback.data.split(":", 1)[1])
    if not func(no):
        await callback.answer("Сейчас это действие недоступно для заказа.", show_alert=True)
        return
    o = ho.get_order(no)
    try:
        await callback.message.edit_text(_creator_order_text(o), parse_mode="HTML", reply_markup=_creator_order_kb(o))
    except Exception:
        pass
    await callback.answer(done_text, show_alert=True)


@router.callback_query(F.data.startswith("ho_start:"))
async def ho_start(callback: CallbackQuery):
    await _status_action(callback, ho.mark_started, "Заказчик увидит, что работа началась.")


@router.callback_query(F.data.startswith("ho_deliver:"))
async def ho_deliver(callback: CallbackQuery):
    await _status_action(callback, ho.mark_delivered,
                         f"Заказчик уведомлён. Без ответа через {ho.ACCEPT_AFTER_DAYS} дня заказ примется сам.")


@router.callback_query(F.data.startswith("ho_withdraw:"))
async def ho_withdraw(callback: CallbackQuery):
    await _status_action(callback, ho.cancel_unpaid, "Предложение отозвано.")


@router.callback_query(F.data.startswith("ho_close:"))
async def ho_close_start(callback: CallbackQuery, state: FSMContext):
    if not _creator_only(callback.from_user.id):
        await callback.answer("⛔ Доступ запрещён", show_alert=True)
        return
    no = int(callback.data.split(":", 1)[1])
    await state.set_state(AdminStates.help_order_close)
    await state.update_data(ho_no=no)
    await callback.message.answer(
        f"🔒 Закрытие заказа #{no}. Напишите: <b>сумма возврата | причина</b>\n"
        "Например: <code>0 | сделано полностью по этапу</code> или <code>3000 | нет доступа, возврат за невыполненное</code>\n"
        "Сам возврат делается вручную в кабинете ЮKassa.", parse_mode="HTML")
    await callback.answer()


@router.message(AdminStates.help_order_close)
async def ho_close_entered(message: Message, state: FSMContext):
    if not _creator_only(message.from_user.id):
        return
    parts = [p.strip() for p in (message.text or "").split("|", 1)]
    digits = "".join(ch for ch in parts[0] if ch.isdigit() or ch == ".")
    if not digits:
        await message.answer("❌ Формат: сумма возврата | причина (сумма 0, если возврата нет).")
        return
    data = await state.get_data()
    no = int(data.get("ho_no") or 0)
    await state.clear()
    if not ho.close_order(no, float(digits), parts[1] if len(parts) > 1 else ""):
        await message.answer("❌ Заказ не найден или его уже нельзя закрыть.")
        return
    o = ho.get_order(no)
    await message.answer(_creator_order_text(o), parse_mode="HTML", reply_markup=_creator_order_kb(o))


# ---- предложение
@router.callback_query(F.data.startswith("ho_new:"))
async def ho_new(callback: CallbackQuery, state: FSMContext):
    if not _creator_only(callback.from_user.id):
        await callback.answer("⛔ Доступ запрещён", show_alert=True)
        return
    thread_id = int(callback.data.split(":", 1)[1])
    await state.clear()
    items = ho.get_price_list()
    rows = [[_btn(f"{it['title']} · {ho.money(it['price'])}"[:60], f"ho_pick:{thread_id}:{i}")]
            for i, it in enumerate(items)]
    rows.append([_btn("✏️ Своя услуга и цена", f"ho_custom:{thread_id}")])
    await callback.message.answer(f"💼 <b>Предложение для установки #{thread_id}</b>\nВыберите услугу:",
                                  parse_mode="HTML", reply_markup=_kb(rows))
    await callback.answer()


async def _ask_description(target, state: FSMContext, thread_id: int, title: str, price: float):
    await state.set_state(AdminStates.help_offer_desc)
    await state.update_data(ho_thread=thread_id, ho_title=title, ho_price=price)
    await target.answer(
        f"📋 {ho._esc(title)} · <b>{ho.money(price)}</b>\n\n"
        "Опишите одним сообщением, что входит в работу и что не входит (до "
        f"{ho.MAX_DESC} символов). «-» чтобы пропустить.", parse_mode="HTML")


@router.callback_query(F.data.startswith("ho_pick:"))
async def ho_pick(callback: CallbackQuery, state: FSMContext):
    if not _creator_only(callback.from_user.id):
        await callback.answer("⛔ Доступ запрещён", show_alert=True)
        return
    _, thread_id, idx = callback.data.split(":")
    items = ho.get_price_list()
    try:
        it = items[int(idx)]
    except (IndexError, ValueError):
        await callback.answer("Позиция прайса изменилась. Откройте предложение заново.", show_alert=True)
        return
    await _ask_description(callback.message, state, int(thread_id), it["title"], it["price"])
    await callback.answer()


@router.callback_query(F.data.startswith("ho_custom:"))
async def ho_custom(callback: CallbackQuery, state: FSMContext):
    if not _creator_only(callback.from_user.id):
        await callback.answer("⛔ Доступ запрещён", show_alert=True)
        return
    await state.set_state(AdminStates.help_offer_custom)
    await state.update_data(ho_thread=int(callback.data.split(":", 1)[1]))
    await callback.message.answer(f"✏️ Напишите: <b>название | цена</b>\nНапример: <code>Перенос бота на новый сервер | 8000</code>\n"
                                  f"Цена от {ho.MIN_PRICE} до {ho.MAX_PRICE} ₽.", parse_mode="HTML")
    await callback.answer()


@router.message(AdminStates.help_offer_custom)
async def ho_custom_entered(message: Message, state: FSMContext):
    if not _creator_only(message.from_user.id):
        return
    parts = [p.strip() for p in (message.text or "").split("|")]
    digits = "".join(ch for ch in (parts[1] if len(parts) > 1 else "") if ch.isdigit())
    if not parts[0] or not digits or not (ho.MIN_PRICE <= int(digits) <= ho.MAX_PRICE):
        await message.answer(f"❌ Формат: название | цена (от {ho.MIN_PRICE} до {ho.MAX_PRICE} ₽).")
        return
    data = await state.get_data()
    await _ask_description(message, state, int(data.get("ho_thread") or 0), parts[0][:ho.MAX_TITLE], int(digits))


@router.message(AdminStates.help_offer_desc)
async def ho_desc_entered(message: Message, state: FSMContext):
    if not _creator_only(message.from_user.id):
        return
    text = (message.text or "").strip()
    if not text or text.startswith("/"):
        await message.answer("❌ Отправьте текст описания или «-».")
        return
    data = await state.get_data()
    await state.clear()
    desc = "" if text == "-" else text
    no = ho.create_order(int(data.get("ho_thread") or 0), data.get("ho_title") or "", desc, data.get("ho_price") or 0)
    if not no:
        await message.answer("❌ Не удалось создать предложение: проверьте цену и что чат установки существует.")
        return
    o = ho.get_order(no)
    await message.answer("✅ Предложение создано. Установка получит его в течение нескольких минут.\n\n"
                         + _creator_order_text(o), parse_mode="HTML", reply_markup=_creator_order_kb(o))


# ---- прайс и сведения об исполнителе
@router.callback_query(F.data == "hp_screen")
@router.message(Command("help_prices"))
async def hp_screen(event):
    if not _creator_only(event.from_user.id):
        if isinstance(event, CallbackQuery):
            await event.answer("⛔ Доступ запрещён", show_alert=True)
        return
    items = ho.get_price_list()
    lines = ["🏷 <b>Прайс помощи</b>\n"] + [f"• {ho._esc(i['title'])} — <b>{ho.money(i['price'])}</b>" for i in items]
    lines.append("\nНа экране предложения установка не видит весь прайс, только выбранную вами услугу.")
    kb = _kb([[_btn("✏️ Заменить прайс", "hp_edit")], [_btn("↩️ Вернуть стандартный", "hp_reset")],
              [_btn("⬅️ Назад", "ho_home")]])
    if isinstance(event, CallbackQuery):
        await safe_edit_or_send(event.message, "\n".join(lines), reply_markup=kb)
        await event.answer()
    else:
        await event.answer("\n".join(lines), parse_mode="HTML", reply_markup=kb)


@router.callback_query(F.data == "hp_edit")
async def hp_edit(callback: CallbackQuery, state: FSMContext):
    if not _creator_only(callback.from_user.id):
        await callback.answer("⛔ Доступ запрещён", show_alert=True)
        return
    await state.set_state(AdminStates.help_prices_edit)
    await callback.message.answer(
        "✏️ Пришлите новый прайс одним сообщением: по строке на услугу, формат "
        "<code>Название | цена | пояснение</code> (пояснение необязательно). До 12 позиций. "
        "Текущий можно скопировать ниже и поправить.", parse_mode="HTML")
    await callback.message.answer(ho.get_price_text())
    await callback.answer()


@router.message(AdminStates.help_prices_edit)
async def hp_edit_entered(message: Message, state: FSMContext):
    if not _creator_only(message.from_user.id):
        return
    count = ho.set_price_text(message.text or "")
    await state.clear()
    await message.answer(f"✅ Прайс сохранён, позиций: {count}." if count
                         else "❌ Не нашёл ни одной строки вида «Название | цена». Прайс не изменён.")


@router.callback_query(F.data == "hp_reset")
async def hp_reset(callback: CallbackQuery):
    if not _creator_only(callback.from_user.id):
        await callback.answer("⛔ Доступ запрещён", show_alert=True)
        return
    ho.reset_price_list()
    await callback.answer("Вернул стандартный прайс.", show_alert=True)


@router.callback_query(F.data == "hp_executor")
async def hp_executor(callback: CallbackQuery, state: FSMContext):
    if not _creator_only(callback.from_user.id):
        await callback.answer("⛔ Доступ запрещён", show_alert=True)
        return
    await state.set_state(AdminStates.help_executor_edit)
    await callback.message.answer(
        "🧾 Сейчас в условиях:\n\n" + ho._esc(ho.get_executor_info()) +
        "\n\nПришлите новую строку (кто исполнитель, например ФИО или ИП/самозанятый, ИНН). Это увидит заказчик перед оплатой.",
        parse_mode="HTML")
    await callback.answer()


@router.message(AdminStates.help_executor_edit)
async def hp_executor_entered(message: Message, state: FSMContext):
    if not _creator_only(message.from_user.id):
        return
    text = (message.text or "").strip()
    if not text or text.startswith("/"):
        await message.answer("❌ Отправьте текст.")
        return
    ho.set_executor_info(text)
    await state.clear()
    await message.answer("✅ Сохранено.")
