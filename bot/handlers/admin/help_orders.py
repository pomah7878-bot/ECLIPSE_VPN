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


def _nav(back_cb: str, *extra_rows):
    """Клавиатура навигации: свои ряды + «Назад» и «На главную» (как в остальных разделах бота)."""
    from bot.keyboards.admin_misc import back_button, home_button
    rows = [list(r) for r in extra_rows]
    rows.append([back_button(back_cb), home_button()])
    return _kb(rows)


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
    await safe_edit_or_send(callback.message, "📋 <b>Мои заказы помощи</b>", reply_markup=_nav("creator_chat", *rows))
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
    await safe_edit_or_send(callback.message, text, reply_markup=_nav("hlp_list", *rows))
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
    ver = str(data.get("terms_version") or ho.TERMS_VERSION)
    await safe_edit_or_send(callback.message, text, reply_markup=_nav(
        f"hlp_open:{no}", [_btn("✅ Согласен с условиями, оплатить", f"hlp_pay:{no}:{ver}")]))
    await callback.answer()


@router.callback_query(F.data.startswith("hlp_pay:"))
async def hlp_pay(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        await callback.answer("⛔ Доступ запрещён", show_alert=True)
        return
    parts = callback.data.split(":")
    no = int(parts[1])
    if len(parts) < 3:
        # старая кнопка без версии условий: сначала покажем условия
        await callback.answer("Сначала прочитайте условия.", show_alert=True)
        return
    ver = parts[2]
    await callback.answer("Создаю оплату…")
    state, data = await ho.call("accept", no, ver)
    if state == "unreachable":
        await callback.message.answer(_UNREACHABLE, reply_markup=_nav(f"hlp_open:{no}"))
        return
    if state != "ok":
        await callback.message.answer(f"❌ {data.get('message') or 'Не удалось создать оплату.'}",
                                      reply_markup=_nav(f"hlp_open:{no}"))
        return
    if data.get("status") == "paid":
        await callback.message.answer("✅ Оплата уже получена. Создатель скоро начнёт.", reply_markup=_nav("hlp_list"))
        return
    try:
        photo = BufferedInputFile(base64.b64decode(data["qr_png"]), filename=f"help{no}.png")
    except Exception:
        await callback.message.answer("❌ Не удалось показать QR. Нажмите «Оплатить» ещё раз.",
                                      reply_markup=_nav(f"hlp_open:{no}", [_btn("💳 Оплатить", f"hlp_terms:{no}")]))
        return
    await callback.message.answer_photo(
        photo=photo,
        caption=(f"💳 <b>Оплата заказа #{no}</b>\n\nСумма: <b>{ho.money(data['amount'])}</b>\n\n"
                 "Отсканируйте QR в приложении вашего банка (СБП), затем нажмите «Я оплатил». "
                 "Работа начнётся после подтверждения оплаты."),
        parse_mode="HTML",
        reply_markup=_nav(f"hlp_open:{no}", [_btn("✅ Я оплатил", f"hlp_check:{no}")]),
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
        await callback.message.answer(f"✅ Заказ #{no} оплачен. Создатель скоро начнёт, обновления придут сюда.",
                                      reply_markup=_nav("hlp_list"))
        await callback.answer()
    elif data.get("payment") == "late":
        await callback.answer("Заказ уже закрыт создателем. Если вы оплатили, деньги вернут.", show_alert=True)
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
    await callback.message.answer(f"🏁 Заказ #{no} принят. Спасибо!", reply_markup=_nav("hlp_list"))
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
    await callback.message.answer(f"✖️ Заказ #{no} отменён.", reply_markup=_nav("hlp_list"))
    await callback.answer()


# ===================================================================== СОЗДАТЕЛЬ
def _creator_order_text(o) -> str:
    from bot.services import creator_chat as cc
    t = cc.get_thread(o["thread_id"]) or {}
    lines = [f"💼 <b>Заказ #{o['id']}</b> · установка #{o['thread_id']}",
             f"<i>{ho._esc(cc.thread_title(t)) if t else ''}</i>",
             f"{ho._esc(o['title'])}",
             ho.price_line(o["amount_rub"], o.get("list_price_rub"), o.get("discount_text") or ""),
             f"Статус: {ho.STATUS_TITLES.get(o['status'], o['status'])}"]
    if o.get("description"):
        lines.append(f"\n{ho._esc(o['description'])}")
    if o.get("refund_rub"):
        lines.append(f"\nВозврат: {ho.money(o['refund_rub'])}. Сделайте его вручную в кабинете ЮKassa.")
    if o.get("discount_text") and o["status"] in ("paid", "in_progress", "delivered", "closed", "accepted"):
        lines.append(f"Скидка: {ho._esc(o['discount_text'])}")
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
    if st in ("offered", "awaiting_payment", "declined", "canceled"):
        rows.append([_btn("🔄 Проверить оплату", f"ho_chk:{no}")])
    if st in ("offered", "awaiting_payment"):
        rows.append([_btn("✖️ Отозвать предложение", f"ho_withdraw:{no}")])
    if st in ("offered", "awaiting_payment", "paid", "in_progress", "delivered"):
        rows.append([_btn("🎁 Скидка", f"ho_disc:{no}"), _btn("🚫 Отказать", f"ho_dec:{no}")])
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
    ], )
    kb = _nav("admin_panel", *kb.inline_keyboard)
    return text, kb


@router.callback_query(F.data == "ho_home")
async def ho_home(callback: CallbackQuery, state: FSMContext):
    if not _creator_only(callback.from_user.id):
        await callback.answer("⛔ Доступ запрещён", show_alert=True)
        return
    await state.clear()
    text, kb = home_view()
    await safe_edit_or_send(callback.message, text, reply_markup=kb)
    await callback.answer()


@router.callback_query(F.data == "hp_terms")
async def hp_terms(callback: CallbackQuery):
    if not _creator_only(callback.from_user.id):
        await callback.answer("⛔ Доступ запрещён", show_alert=True)
        return
    await callback.message.answer(ho.terms_text(), parse_mode="HTML", reply_markup=_nav("ho_home"))
    await callback.answer()


@router.callback_query(F.data == "ho_list")
@router.message(Command("help_orders"))
async def ho_list(event, state: FSMContext):
    await state.clear()
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
    kb = _nav("ho_home", *rows)
    if isinstance(event, CallbackQuery):
        await safe_edit_or_send(event.message, text, reply_markup=kb)
        await event.answer()
    else:
        await event.answer(text, parse_mode="HTML", reply_markup=kb)


@router.callback_query(F.data.startswith("ho_open:"))
async def ho_open(callback: CallbackQuery, state: FSMContext):
    if not _creator_only(callback.from_user.id):
        await callback.answer("⛔ Доступ запрещён", show_alert=True)
        return
    await state.clear()
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
        "Сам возврат делается вручную в кабинете ЮKassa.", parse_mode="HTML", reply_markup=_nav(f"ho_open:{no}"))
    await callback.answer()


@router.message(AdminStates.help_order_close)
async def ho_close_entered(message: Message, state: FSMContext):
    if not _creator_only(message.from_user.id):
        return
    parts = [p.strip() for p in (message.text or "").split("|", 1)]
    digits = "".join(ch for ch in parts[0] if ch.isdigit() or ch == ".")
    data = await state.get_data()
    no = int(data.get("ho_no") or 0)
    if not digits:
        await message.answer("❌ Формат: сумма возврата | причина (сумма 0, если возврата нет).",
                             reply_markup=_nav(f"ho_open:{no}"))
        return
    await state.clear()
    if not ho.close_order(no, float(digits), parts[1] if len(parts) > 1 else ""):
        await message.answer("❌ Заказ не найден или его уже нельзя закрыть.", reply_markup=_nav("ho_list"))
        return
    o = ho.get_order(no)
    await message.answer(_creator_order_text(o), parse_mode="HTML", reply_markup=_creator_order_kb(o))


# ---- проверка оплаты вручную и отказ в выполнении
@router.callback_query(F.data.startswith("ho_chk:"))
async def ho_check_payment(callback: CallbackQuery):
    if not _creator_only(callback.from_user.id):
        await callback.answer("⛔ Доступ запрещён", show_alert=True)
        return
    no = int(callback.data.split(":", 1)[1])
    o = ho.get_order(no)
    if not o:
        await callback.answer("Заказ не найден", show_alert=True)
        return
    res = await ho._check_payment(o)
    texts = {"paid": "✅ Оплата поступила, статус обновлён.", "pending": "Оплаты пока нет.",
             "canceled": "Платёж отменён, оплаты нет.", "error": "Не удалось проверить оплату, попробуйте позже.",
             "late": "⚠️ Оплата пришла по закрытому заказу. Верните её в ЮKassa."}
    fresh = ho.get_order(no)
    try:
        await callback.message.edit_text(_creator_order_text(fresh), parse_mode="HTML", reply_markup=_creator_order_kb(fresh))
    except Exception:
        pass
    await callback.answer(texts.get(res, res), show_alert=True)


@router.callback_query(F.data.startswith("ho_dec:"))
async def ho_decline_start(callback: CallbackQuery, state: FSMContext):
    if not _creator_only(callback.from_user.id):
        await callback.answer("⛔ Доступ запрещён", show_alert=True)
        return
    no = int(callback.data.split(":", 1)[1])
    o = ho.get_order(no)
    if not o or o["status"] in ho.FINAL_STATUSES:
        await callback.answer("Этот заказ уже завершён.", show_alert=True)
        return
    await state.set_state(AdminStates.help_decline_reason)
    await state.update_data(ho_no=no)
    paid = o["status"] in ("paid", "in_progress", "delivered")
    await safe_edit_or_send(
        callback.message,
        f"🚫 <b>Отказ в выполнении заказа #{no}</b>\n\nНапишите причину одним сообщением. Её увидит заказчик.\n"
        + ("Дальше вы выберете сумму возврата." if paid else "Оплаты не было, возврат не нужен."),
        reply_markup=_nav(f"ho_open:{no}"))
    await callback.answer()


async def _finish_decline(target, state: FSMContext, refund):
    data = await state.get_data()
    no = int(data.get("ho_no") or 0)
    ok, msg = ho.decline_order(no, data.get("ho_reason") or "", refund)
    await state.clear()
    if not ok:
        await target.answer(f"❌ {msg}", reply_markup=_nav(f"ho_open:{no}"))
        return
    await _show_order_after(target, ho.get_order(no), "✅ " + msg + " Заказчик получит уведомление.")


@router.message(AdminStates.help_decline_reason)
async def ho_decline_reason(message: Message, state: FSMContext):
    if not _creator_only(message.from_user.id):
        return
    data = await state.get_data()
    no = int(data.get("ho_no") or 0)
    reason = (message.text or "").strip()
    if len(reason) < 3 or reason.startswith("/"):
        await message.answer("❌ Напишите причину текстом (хотя бы несколько слов).", reply_markup=_nav(f"ho_open:{no}"))
        return
    o = ho.get_order(no)
    if not o or o["status"] in ho.FINAL_STATUSES:
        await state.clear()
        await message.answer("Этот заказ уже завершён.", reply_markup=_nav("ho_list"))
        return
    await state.update_data(ho_reason=reason[:300])
    if o["status"] in ("paid", "in_progress", "delivered"):
        await message.answer(
            f"💸 Заказ #{no} оплачен ({ho.money(o['amount_rub'])}). Сколько вернуть заказчику?\n"
            "Если работа уже частично сделана, укажите меньшую сумму.",
            reply_markup=_nav(f"ho_open:{no}", [_btn(f"💸 Вернуть всё ({ho.money(o['amount_rub'])})", f"ho_decr:{no}:full")],
                              [_btn("✏️ Другая сумма", f"ho_decr:{no}:custom")]))
        return
    await _finish_decline(message, state, None)


@router.callback_query(F.data.startswith("ho_decr:"))
async def ho_decline_refund(callback: CallbackQuery, state: FSMContext):
    if not _creator_only(callback.from_user.id):
        await callback.answer("⛔ Доступ запрещён", show_alert=True)
        return
    _, no, mode = callback.data.split(":")
    data = await state.get_data()
    if await state.get_state() != AdminStates.help_decline_reason or int(data.get("ho_no") or 0) != int(no):
        await callback.answer("Отказ уже оформлен или отменён.", show_alert=True)
        return
    if mode == "full":
        await callback.answer()
        await _finish_decline(callback.message, state, None)
        return
    await state.set_state(AdminStates.help_decline_refund)
    await safe_edit_or_send(callback.message,
                            f"✏️ Напишите сумму возврата в рублях, только число (от 0 до {ho.money(ho.get_order(int(no))['amount_rub'])}).",
                            reply_markup=_nav(f"ho_open:{no}"))
    await callback.answer()


@router.message(AdminStates.help_decline_refund)
async def ho_decline_refund_entered(message: Message, state: FSMContext):
    if not _creator_only(message.from_user.id):
        return
    data = await state.get_data()
    no = int(data.get("ho_no") or 0)
    text = (message.text or "").strip().replace(",", ".").replace(" ", "")
    try:
        value = float(text)
        if value < 0:
            raise ValueError
    except ValueError:
        await message.answer("❌ Напишите число, например 3000 (или 0, если возврата нет).", reply_markup=_nav(f"ho_open:{no}"))
        return
    await _finish_decline(message, state, value)


# ---- скидка на заказ (в любой момент)
def _discount_view(o):
    unpaid = o["status"] in ("offered", "awaiting_payment")
    no = o["id"]
    if unpaid:
        base = float(o.get("list_price_rub") or o["amount_rub"])
        text = (f"🎁 <b>Скидка на заказ #{no}</b>\n\n{ho.price_line(o['amount_rub'], o.get('list_price_rub'), o.get('discount_text') or '')}\n\n"
                f"Заказ ещё не оплачен, поэтому скидка меняет цену. Проценты и рубли считаются от первоначальной цены "
                f"({ho.money(base)}), новая скидка заменяет прежнюю. Если заказчик уже открыл оплату, ему покажут новый QR.")
    else:
        text = (f"🎁 <b>Скидка на заказ #{no}</b>\n\nЗаказ уже оплачен ({ho.money(o['amount_rub'])}), поэтому скидка "
                "оформляется как частичный возврат. Бот зафиксирует сумму и уведомит заказчика, сам возврат вы делаете "
                "вручную в кабинете ЮKassa.")
    rows = [[_btn(f"−{p}%", f"ho_dp:{no}:{p}") for p in (5, 10, 15, 20)],
            [_btn("✏️ Своя скидка", f"ho_dc:{no}")]]
    if unpaid and o.get("discount_text"):
        rows.append([_btn("↩️ Убрать скидку", f"ho_dr:{no}")])
    return text, _nav(f"ho_open:{no}", *rows)


async def _show_order_after(target, o, prefix: str):
    await target.answer(f"{prefix}\n\n{_creator_order_text(o)}", parse_mode="HTML", reply_markup=_creator_order_kb(o))


@router.callback_query(F.data.startswith("ho_disc:"))
async def ho_disc(callback: CallbackQuery, state: FSMContext):
    if not _creator_only(callback.from_user.id):
        await callback.answer("⛔ Доступ запрещён", show_alert=True)
        return
    await state.clear()
    o = ho.get_order(int(callback.data.split(":", 1)[1]))
    if not o or o["status"] not in ("offered", "awaiting_payment", "paid", "in_progress", "delivered"):
        await callback.answer("К этому заказу скидку уже нельзя применить.", show_alert=True)
        return
    text, kb = _discount_view(o)
    await safe_edit_or_send(callback.message, text, reply_markup=kb)
    await callback.answer()


@router.callback_query(F.data.startswith("ho_dp:"))
async def ho_discount_percent(callback: CallbackQuery):
    if not _creator_only(callback.from_user.id):
        await callback.answer("⛔ Доступ запрещён", show_alert=True)
        return
    _, no, pct = callback.data.split(":")
    ok, msg = ho.apply_discount(int(no), "pct", pct)
    if not ok:
        await callback.answer(msg[:190], show_alert=True)
        return
    await callback.answer()
    await _show_order_after(callback.message, ho.get_order(int(no)), "✅ " + msg)


@router.callback_query(F.data.startswith("ho_dr:"))
async def ho_discount_reset(callback: CallbackQuery):
    if not _creator_only(callback.from_user.id):
        await callback.answer("⛔ Доступ запрещён", show_alert=True)
        return
    no = int(callback.data.split(":", 1)[1])
    ok, msg = ho.apply_discount(no, "reset")
    if not ok:
        await callback.answer(msg[:190], show_alert=True)
        return
    await callback.answer()
    await _show_order_after(callback.message, ho.get_order(no), "✅ " + msg)


@router.callback_query(F.data.startswith("ho_dc:"))
async def ho_discount_custom(callback: CallbackQuery, state: FSMContext):
    if not _creator_only(callback.from_user.id):
        await callback.answer("⛔ Доступ запрещён", show_alert=True)
        return
    no = int(callback.data.split(":", 1)[1])
    await state.set_state(AdminStates.help_discount)
    await state.update_data(ho_no=no)
    await safe_edit_or_send(
        callback.message,
        f"✏️ <b>Своя скидка на заказ #{no}</b>\n\nНапишите процент со знаком %, например <code>12%</code>, "
        "или сумму скидки в рублях, например <code>500</code>.", reply_markup=_nav(f"ho_disc:{no}"))
    await callback.answer()


@router.message(AdminStates.help_discount)
async def ho_discount_entered(message: Message, state: FSMContext):
    if not _creator_only(message.from_user.id):
        return
    data = await state.get_data()
    no = int(data.get("ho_no") or 0)
    text = (message.text or "").strip()
    if text.endswith("%"):
        mode, value = "pct", text[:-1]
    else:
        mode, value = "rub", text
    if not text or text.startswith("/") or not any(ch.isdigit() for ch in text):
        await message.answer("❌ Напишите число: процент со знаком % (12%) или рубли (500).",
                             reply_markup=_nav(f"ho_disc:{no}"))
        return
    ok, msg = ho.apply_discount(no, mode, value)
    if not ok:
        await message.answer(f"❌ {msg}", reply_markup=_nav(f"ho_disc:{no}"))
        return
    await state.clear()
    await _show_order_after(message, ho.get_order(no), "✅ " + msg)


# ---- предложение
def _offer_items_view(thread_id: int, ci: int = -1):
    """Экран выбора услуги: при нескольких категориях сначала категория, потом услуга."""
    items = ho.get_price_list()
    cats = ho.categories(items)
    custom = [_btn("✏️ Своя услуга и цена", f"ho_custom:{thread_id}")]
    if len(cats) > 1 and ci < 0:
        rows = [[_btn(f"📂 {c} ({sum(1 for it in items if _cat_of(it) == c)})"[:60], f"ho_cat:{thread_id}:{k}")]
                for k, c in enumerate(cats)]
        return (f"💼 <b>Предложение для установки #{thread_id}</b>\nВыберите категорию:",
                _nav(f"cr_open:{thread_id}", *rows, custom))
    if 0 <= ci < len(cats):
        pool = [(gi, it) for gi, it in enumerate(items) if _cat_of(it) == cats[ci]]
        head = f"📂 <b>{ho._esc(cats[ci])}</b>\nВыберите услугу:"
        back = f"ho_new:{thread_id}"
    else:
        pool = list(enumerate(items))
        head = f"💼 <b>Предложение для установки #{thread_id}</b>\nВыберите услугу:"
        back = f"cr_open:{thread_id}"
    rows = [[_btn(f"{it['title']} · {ho.money(it['price'])}"[:60], f"ho_pick:{thread_id}:{gi}")] for gi, it in pool]
    return head, _nav(back, *rows, custom)


@router.callback_query(F.data.startswith("ho_new:"))
async def ho_new(callback: CallbackQuery, state: FSMContext):
    if not _creator_only(callback.from_user.id):
        await callback.answer("⛔ Доступ запрещён", show_alert=True)
        return
    thread_id = int(callback.data.split(":", 1)[1])
    await state.clear()
    text, kb = _offer_items_view(thread_id)
    await callback.message.answer(text, parse_mode="HTML", reply_markup=kb)
    await callback.answer()


@router.callback_query(F.data.startswith("ho_cat:"))
async def ho_offer_category(callback: CallbackQuery, state: FSMContext):
    if not _creator_only(callback.from_user.id):
        await callback.answer("⛔ Доступ запрещён", show_alert=True)
        return
    _, thread_id, ci = callback.data.split(":")
    await state.clear()
    text, kb = _offer_items_view(int(thread_id), int(ci))
    await safe_edit_or_send(callback.message, text, reply_markup=kb)
    await callback.answer()


async def _ask_description(target, state: FSMContext, thread_id: int, title: str, price: float, note: str = ""):
    await state.set_state(AdminStates.help_offer_desc)
    await state.update_data(ho_thread=thread_id, ho_title=title, ho_price=price, ho_note=note)
    rows = []
    text = (f"📋 {ho._esc(title)} · <b>{ho.money(price)}</b>\n\n"
            "Опишите одним сообщением, что входит в работу и что не входит "
            f"(до {ho.MAX_DESC} символов). Заказчик увидит это в предложении.")
    if note:
        text += f"\n\nИз прайса:\n<i>{ho._esc(note)}</i>"
        rows.append([_btn("📝 Взять пояснение из прайса", f"ho_usenote:{thread_id}")])
    rows.append([_btn("⏭ Без описания", f"ho_nodesc:{thread_id}")])
    await target.answer(text, parse_mode="HTML", reply_markup=_nav(f"ho_new:{thread_id}", *rows))


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
    await _ask_description(callback.message, state, int(thread_id), it["title"], it["price"], it.get("note") or "")
    await callback.answer()


@router.callback_query(F.data.startswith("ho_custom:"))
async def ho_custom(callback: CallbackQuery, state: FSMContext):
    if not _creator_only(callback.from_user.id):
        await callback.answer("⛔ Доступ запрещён", show_alert=True)
        return
    await state.set_state(AdminStates.help_offer_custom)
    await state.update_data(ho_thread=int(callback.data.split(":", 1)[1]))
    thread_id = int(callback.data.split(":", 1)[1])
    await callback.message.answer(
        "✏️ <b>Своя услуга</b>\n\nШаг 1 из 2. Напишите название услуги, например: "
        "<code>Перенос бота на новый сервер</code>", parse_mode="HTML", reply_markup=_nav(f"ho_new:{thread_id}"))
    await callback.answer()


@router.message(AdminStates.help_offer_custom)
async def ho_custom_entered(message: Message, state: FSMContext):
    if not _creator_only(message.from_user.id):
        return
    title = (message.text or "").strip()
    if not title or title.startswith("/"):
        data = await state.get_data()
        await message.answer("❌ Напишите название услуги текстом.",
                             reply_markup=_nav(f"ho_new:{int(data.get('ho_thread') or 0)}"))
        return
    data = await state.get_data()
    thread_id = int(data.get("ho_thread") or 0)
    await state.update_data(ho_title=title[:ho.MAX_TITLE])
    await state.set_state(AdminStates.help_offer_price)
    await message.answer(
        f"✏️ <b>{ho._esc(title[:ho.MAX_TITLE])}</b>\n\nШаг 2 из 2. Напишите цену в рублях, только число, "
        f"например: <code>8000</code> (от {ho.MIN_PRICE} до {ho.MAX_PRICE}).",
        parse_mode="HTML", reply_markup=_nav(f"ho_new:{thread_id}"))


@router.message(AdminStates.help_offer_price)
async def ho_price_entered(message: Message, state: FSMContext):
    if not _creator_only(message.from_user.id):
        return
    price = ho.parse_price(message.text)
    data = await state.get_data()
    if price is None:
        await message.answer(f"❌ Нужно число от {ho.MIN_PRICE} до {ho.MAX_PRICE}, например: 8000",
                             reply_markup=_nav(f"ho_new:{int(data.get('ho_thread') or 0)}"))
        return
    await _ask_description(message, state, int(data.get("ho_thread") or 0), data.get("ho_title") or "", price)


async def _finish_offer(target, state: FSMContext, desc: str):
    data = await state.get_data()
    await state.clear()
    no = ho.create_order(int(data.get("ho_thread") or 0), data.get("ho_title") or "", desc, data.get("ho_price") or 0)
    if not no:
        await target.answer("❌ Не удалось создать предложение: проверьте цену и что чат установки существует.",
                            reply_markup=_nav("ho_home"))
        return
    o = ho.get_order(no)
    await target.answer("✅ Предложение создано. Установка получит его в течение нескольких минут.\n\n"
                        + _creator_order_text(o), parse_mode="HTML", reply_markup=_creator_order_kb(o))


@router.message(AdminStates.help_offer_desc)
async def ho_desc_entered(message: Message, state: FSMContext):
    if not _creator_only(message.from_user.id):
        return
    text = (message.text or "").strip()
    if not text or text.startswith("/"):
        data = await state.get_data()
        await message.answer("❌ Отправьте текст описания или нажмите «Без описания».",
                             reply_markup=_nav(f"ho_new:{int(data.get('ho_thread') or 0)}",
                                               [_btn("⏭ Без описания", f"ho_nodesc:{int(data.get('ho_thread') or 0)}")]))
        return
    await _finish_offer(message, state, text)


@router.callback_query(F.data.startswith("ho_usenote:"))
async def ho_usenote(callback: CallbackQuery, state: FSMContext):
    if not _creator_only(callback.from_user.id):
        await callback.answer("⛔ Доступ запрещён", show_alert=True)
        return
    if await state.get_state() != AdminStates.help_offer_desc:
        await callback.answer("Предложение уже создано или отменено.", show_alert=True)
        return
    data = await state.get_data()
    await callback.answer()
    await _finish_offer(callback.message, state, data.get("ho_note") or "")


@router.callback_query(F.data.startswith("ho_nodesc:"))
async def ho_nodesc(callback: CallbackQuery, state: FSMContext):
    if not _creator_only(callback.from_user.id):
        await callback.answer("⛔ Доступ запрещён", show_alert=True)
        return
    if await state.get_state() != AdminStates.help_offer_desc:
        await callback.answer("Предложение уже создано или отменено.", show_alert=True)
        return
    await callback.answer()
    await _finish_offer(callback.message, state, "")


# ---- прайс (по образцу тарифов на лицензии: список, карточка, пошаговое добавление)
_FIELD_TITLES = {"t": "название", "p": "цену", "n": "пояснение", "c": "категорию"}


def _cat_of(it) -> str:
    return it.get("cat") or ho.OTHER_CAT


def _price_list_view():
    items = ho.get_price_list()
    cats = ho.categories(items)
    lines = ["🏷 <b>Прайс помощи</b>", "",
             "Список услуг, из которого вы выбираете цену при выставлении предложения. "
             "Откройте категорию, чтобы изменить услугу.", ""]
    for c in cats:
        lines.append(f"📂 <b>{ho._esc(c)}</b>")
        lines += [f"• {ho._esc(it['title'])} — {ho.money(it['price'])}" for it in items if _cat_of(it) == c]
        lines.append("")
    if not items:
        lines.append("Список пуст. Добавьте первую услугу.")
    rows = [[_btn(f"📂 {c} ({sum(1 for it in items if _cat_of(it) == c)})"[:60], f"hp_cat:{ci}")] for ci, c in enumerate(cats)]
    if len(items) < ho.MAX_ITEMS:
        rows.append([_btn("➕ Добавить услугу", "hp_add")])
    rows.append([_btn("↩️ Вернуть стандартный прайс", "hp_reset_ask")])
    text = "\n".join(lines)
    return text[:3900], _nav("ho_home", *rows)


def _price_cat_view(ci: int):
    items = ho.get_price_list()
    cats = ho.categories(items)
    if not (0 <= ci < len(cats)):
        return None
    cat = cats[ci]
    rows = [[_btn(f"{it['title']} · {ho.money(it['price'])}"[:60], f"hp_item:{gi}")]
            for gi, it in enumerate(items) if _cat_of(it) == cat]
    if len(items) < ho.MAX_ITEMS:
        rows.append([_btn("➕ Добавить в эту категорию", f"hp_addc:{ci}")])
    return f"📂 <b>{ho._esc(cat)}</b>\n\nНажмите на услугу, чтобы изменить её или удалить.", _nav("hp_screen", *rows)


def _price_item_view(idx: int):
    items = ho.get_price_list()
    if not (0 <= idx < len(items)):
        return None
    it = items[idx]
    cats = ho.categories(items)
    text = (f"🏷 <b>{ho._esc(it['title'])}</b>\n\nКатегория: {ho._esc(_cat_of(it))}\nЦена: <b>{ho.money(it['price'])}</b>\n"
            f"Пояснение: {ho._esc(it['note']) if it['note'] else '<i>нет</i>'}")
    rows = [[_btn("✏️ Название", f"hp_f:{idx}:t"), _btn("💰 Цена", f"hp_f:{idx}:p")],
            [_btn("📝 Пояснение", f"hp_f:{idx}:n"), _btn("📂 Категория", f"hp_f:{idx}:c")]]
    if idx > 0:
        rows.append([_btn("⬆️ Поднять выше", f"hp_up:{idx}")])
    rows.append([_btn("🗑 Удалить", f"hp_del:{idx}")])
    return text, _nav(f"hp_cat:{cats.index(_cat_of(it))}", *rows)


@router.callback_query(F.data == "hp_screen")
@router.message(Command("help_prices"))
async def hp_screen(event, state: FSMContext):
    if not _creator_only(event.from_user.id):
        if isinstance(event, CallbackQuery):
            await event.answer("⛔ Доступ запрещён", show_alert=True)
        return
    await state.clear()
    text, kb = _price_list_view()
    if isinstance(event, CallbackQuery):
        await safe_edit_or_send(event.message, text, reply_markup=kb)
        await event.answer()
    else:
        await event.answer(text, parse_mode="HTML", reply_markup=kb)


@router.callback_query(F.data.startswith("hp_item:"))
async def hp_item(callback: CallbackQuery, state: FSMContext):
    if not _creator_only(callback.from_user.id):
        await callback.answer("⛔ Доступ запрещён", show_alert=True)
        return
    await state.clear()
    view = _price_item_view(int(callback.data.split(":", 1)[1]))
    if not view:
        await callback.answer("Услуга не найдена", show_alert=True)
        return
    await safe_edit_or_send(callback.message, view[0], reply_markup=view[1])
    await callback.answer()


@router.callback_query(F.data.startswith("hp_cat:"))
async def hp_cat(callback: CallbackQuery, state: FSMContext):
    if not _creator_only(callback.from_user.id):
        await callback.answer("⛔ Доступ запрещён", show_alert=True)
        return
    await state.clear()
    view = _price_cat_view(int(callback.data.split(":", 1)[1]))
    if not view:
        text, kb = _price_list_view()
        await safe_edit_or_send(callback.message, text, reply_markup=kb)
    else:
        await safe_edit_or_send(callback.message, view[0], reply_markup=view[1])
    await callback.answer()


@router.callback_query(F.data.startswith("hp_addc:"))
async def hp_add_to_category(callback: CallbackQuery, state: FSMContext):
    if not _creator_only(callback.from_user.id):
        await callback.answer("⛔ Доступ запрещён", show_alert=True)
        return
    cats = ho.categories()
    ci = int(callback.data.split(":", 1)[1])
    cat = cats[ci] if 0 <= ci < len(cats) else ""
    await _start_add(callback, state, "" if cat == ho.OTHER_CAT else cat)


@router.callback_query(F.data == "hp_add")
async def hp_add(callback: CallbackQuery, state: FSMContext):
    await _start_add(callback, state, "")


async def _start_add(callback: CallbackQuery, state: FSMContext, cat: str):
    if not _creator_only(callback.from_user.id):
        await callback.answer("⛔ Доступ запрещён", show_alert=True)
        return
    if len(ho.get_price_list()) >= ho.MAX_ITEMS:
        await callback.answer(f"В прайсе не больше {ho.MAX_ITEMS} услуг. Удалите ненужную.", show_alert=True)
        return
    await state.set_state(AdminStates.help_price_title)
    await state.update_data(hp_cat=cat)
    await safe_edit_or_send(
        callback.message,
        "➕ <b>Новая услуга</b>" + (f" в категории «{ho._esc(cat)}»" if cat else "") + "\n\nШаг 1 из 3. Напишите название услуги. Заказчик увидит его в предложении.\n"
        "Например: <code>Настройка сертификата</code>",
        reply_markup=_nav("hp_screen"))
    await callback.answer()


@router.message(AdminStates.help_price_title)
async def hp_title_entered(message: Message, state: FSMContext):
    if not _creator_only(message.from_user.id):
        return
    title = (message.text or "").strip()
    if not title or title.startswith("/"):
        await message.answer("❌ Напишите название текстом.", reply_markup=_nav("hp_screen"))
        return
    await state.update_data(hp_title=title[:ho.MAX_TITLE])
    await state.set_state(AdminStates.help_price_price)
    await message.answer(
        f"➕ <b>{ho._esc(title[:ho.MAX_TITLE])}</b>\n\nШаг 2 из 3. Напишите цену в рублях, только число.\n"
        f"Например: <code>4500</code> (от {ho.MIN_PRICE} до {ho.MAX_PRICE})",
        parse_mode="HTML", reply_markup=_nav("hp_screen"))


@router.message(AdminStates.help_price_price)
async def hp_price_entered(message: Message, state: FSMContext):
    if not _creator_only(message.from_user.id):
        return
    price = ho.parse_price(message.text)
    if price is None:
        await message.answer(f"❌ Нужно число от {ho.MIN_PRICE} до {ho.MAX_PRICE}, например: 4500",
                             reply_markup=_nav("hp_screen"))
        return
    await state.update_data(hp_price=price)
    await state.set_state(AdminStates.help_price_note)
    data = await state.get_data()
    await message.answer(
        f"➕ <b>{ho._esc(data.get('hp_title'))}</b> · {ho.money(price)}\n\n"
        "Шаг 3 из 3. Напишите короткое пояснение (что входит в услугу) или нажмите «Пропустить».",
        parse_mode="HTML", reply_markup=_nav("hp_screen", [_btn("⏭ Пропустить", "hp_skip_note")]))


async def _save_new_item(target, state: FSMContext, note: str):
    data = await state.get_data()
    await state.clear()
    if not ho.add_price_item(data.get("hp_title") or "", data.get("hp_price") or 0, note, data.get("hp_cat") or ""):
        await target.answer("❌ Не удалось добавить услугу (проверьте цену, в прайсе не больше "
                            f"{ho.MAX_ITEMS} услуг).", reply_markup=_nav("hp_screen"))
        return
    items_now = ho.get_price_list()
    idx = len(items_now) - 1
    cat_now = data.get("hp_cat") or ""
    if cat_now:   # новая услуга встаёт в конец своей категории
        last = max((i for i, it in enumerate(items_now[:-1]) if _cat_of(it) == cat_now), default=None)
        while last is not None and idx > last + 1:
            ho.move_price_item_up(idx)
            idx -= 1
    view = _price_item_view(idx)
    await target.answer("✅ Услуга добавлена.\n\n" + view[0], parse_mode="HTML", reply_markup=view[1])


@router.message(AdminStates.help_price_note)
async def hp_note_entered(message: Message, state: FSMContext):
    if not _creator_only(message.from_user.id):
        return
    note = (message.text or "").strip()
    if not note or note.startswith("/"):
        await message.answer("❌ Напишите пояснение текстом или нажмите «Пропустить».",
                             reply_markup=_nav("hp_screen", [_btn("⏭ Пропустить", "hp_skip_note")]))
        return
    await _save_new_item(message, state, note)


@router.callback_query(F.data == "hp_skip_note")
async def hp_skip_note(callback: CallbackQuery, state: FSMContext):
    if not _creator_only(callback.from_user.id):
        await callback.answer("⛔ Доступ запрещён", show_alert=True)
        return
    if await state.get_state() != AdminStates.help_price_note:
        await callback.answer("Это действие уже выполнено или отменено.", show_alert=True)
        return
    await callback.answer()
    await _save_new_item(callback.message, state, "")


@router.callback_query(F.data.startswith("hp_f:"))
async def hp_field_start(callback: CallbackQuery, state: FSMContext):
    if not _creator_only(callback.from_user.id):
        await callback.answer("⛔ Доступ запрещён", show_alert=True)
        return
    _, idx, field = callback.data.split(":")
    idx = int(idx)
    items = ho.get_price_list()
    if field not in _FIELD_TITLES or not (0 <= idx < len(items)):
        await callback.answer("Услуга не найдена", show_alert=True)
        return
    await state.set_state(AdminStates.help_price_edit)
    await state.update_data(hp_idx=idx, hp_field=field)
    hints = {"t": "Напишите новое название.", "p": f"Напишите новую цену в рублях, только число (от {ho.MIN_PRICE} до {ho.MAX_PRICE}).",
             "n": "Напишите новое пояснение. Чтобы убрать пояснение, отправьте «-».",
             "c": "Напишите название категории: существующей или новой. «-» убирает категорию (услуга попадёт в «Другое»)."}
    await safe_edit_or_send(callback.message, f"✏️ <b>{ho._esc(items[idx]['title'])}</b>\n\n{hints[field]}",
                            reply_markup=_nav(f"hp_item:{idx}"))
    await callback.answer()


@router.message(AdminStates.help_price_edit)
async def hp_field_entered(message: Message, state: FSMContext):
    if not _creator_only(message.from_user.id):
        return
    data = await state.get_data()
    idx, field = int(data.get("hp_idx", -1)), data.get("hp_field")
    text = (message.text or "").strip()
    if not text or text.startswith("/"):
        await message.answer("❌ Отправьте текст.", reply_markup=_nav(f"hp_item:{idx}"))
        return
    if field == "p" and ho.parse_price(text) is None:
        await message.answer(f"❌ Нужно число от {ho.MIN_PRICE} до {ho.MAX_PRICE}, например: 4500",
                             reply_markup=_nav(f"hp_item:{idx}"))
        return
    key = {"t": "title", "p": "price", "n": "note", "c": "cat"}.get(field)
    value = "" if (field in ("n", "c") and text == "-") else text
    ok = key and ho.update_price_item(idx, **{key: value})
    await state.clear()
    view = _price_item_view(idx)
    if not ok or not view:
        await message.answer("❌ Не удалось сохранить.", reply_markup=_nav("hp_screen"))
        return
    await message.answer("✅ Сохранено.\n\n" + view[0], parse_mode="HTML", reply_markup=view[1])


@router.callback_query(F.data.startswith("hp_up:"))
async def hp_up(callback: CallbackQuery):
    if not _creator_only(callback.from_user.id):
        await callback.answer("⛔ Доступ запрещён", show_alert=True)
        return
    idx = int(callback.data.split(":", 1)[1])
    if ho.move_price_item_up(idx):
        view = _price_item_view(idx - 1)
        await safe_edit_or_send(callback.message, view[0], reply_markup=view[1])
    await callback.answer("Поднял выше.")


@router.callback_query(F.data.startswith("hp_del:"))
async def hp_delete_ask(callback: CallbackQuery):
    if not _creator_only(callback.from_user.id):
        await callback.answer("⛔ Доступ запрещён", show_alert=True)
        return
    idx = int(callback.data.split(":", 1)[1])
    items = ho.get_price_list()
    if not (0 <= idx < len(items)):
        await callback.answer("Услуга не найдена", show_alert=True)
        return
    await safe_edit_or_send(
        callback.message, f"🗑 Удалить услугу «{ho._esc(items[idx]['title'])}» из прайса?\n"
        "Уже выставленных предложений это не затронет.",
        reply_markup=_nav(f"hp_item:{idx}", [_btn("🗑 Да, удалить", f"hp_delok:{idx}")]))
    await callback.answer()


@router.callback_query(F.data.startswith("hp_delok:"))
async def hp_delete_do(callback: CallbackQuery):
    if not _creator_only(callback.from_user.id):
        await callback.answer("⛔ Доступ запрещён", show_alert=True)
        return
    ok = ho.delete_price_item(int(callback.data.split(":", 1)[1]))
    text, kb = _price_list_view()
    await safe_edit_or_send(callback.message, text, reply_markup=kb)
    await callback.answer("Удалено." if ok else "Услуга не найдена.")


@router.callback_query(F.data == "hp_reset_ask")
async def hp_reset_ask(callback: CallbackQuery):
    if not _creator_only(callback.from_user.id):
        await callback.answer("⛔ Доступ запрещён", show_alert=True)
        return
    await safe_edit_or_send(
        callback.message, "↩️ Вернуть стандартный прайс? Ваши изменения в списке будут потеряны.",
        reply_markup=_nav("hp_screen", [_btn("↩️ Да, вернуть", "hp_reset_do")]))
    await callback.answer()


@router.callback_query(F.data == "hp_reset_do")
async def hp_reset_do(callback: CallbackQuery):
    if not _creator_only(callback.from_user.id):
        await callback.answer("⛔ Доступ запрещён", show_alert=True)
        return
    ho.reset_price_list()
    text, kb = _price_list_view()
    await safe_edit_or_send(callback.message, text, reply_markup=kb)
    await callback.answer("Стандартный прайс возвращён.")


# ---- сведения об исполнителе
@router.callback_query(F.data == "hp_executor")
async def hp_executor(callback: CallbackQuery, state: FSMContext):
    if not _creator_only(callback.from_user.id):
        await callback.answer("⛔ Доступ запрещён", show_alert=True)
        return
    await state.set_state(AdminStates.help_executor_edit)
    await safe_edit_or_send(
        callback.message,
        "🧾 <b>Сведения об исполнителе</b>\n\nСейчас в условиях:\n<i>" + ho._esc(ho.get_executor_info()) + "</i>\n\n"
        "Напишите новый текст: кто исполнитель (ФИО или ИП/самозанятый, ИНН). "
        "Заказчик увидит это перед оплатой.", reply_markup=_nav("ho_home"))
    await callback.answer()


@router.message(AdminStates.help_executor_edit)
async def hp_executor_entered(message: Message, state: FSMContext):
    if not _creator_only(message.from_user.id):
        return
    text = (message.text or "").strip()
    if not text or text.startswith("/"):
        await message.answer("❌ Отправьте текст.", reply_markup=_nav("ho_home"))
        return
    ho.set_executor_info(text)
    await state.clear()
    await message.answer("✅ Сохранено.", reply_markup=_nav("ho_home"))
