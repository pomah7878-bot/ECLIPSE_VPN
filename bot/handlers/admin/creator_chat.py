"""
Связь с создателем (v1.280) — экраны и команды. Логика и хранилище: bot/services/creator_chat.py.

  • Установка (партнёр / «с GitHub»): кнопка «🆘 Помощь для админа» (показывается, когда её включил создатель) или /creator.
  • Главная инсталляция (создатель): ответы на сообщения, блокировка, список чатов /creator_chats.
"""
import logging

from aiogram import Router, F
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from bot.states.admin_states import AdminStates
from bot.utils.admin import is_admin
from bot.utils.text import safe_edit_or_send

logger = logging.getLogger(__name__)
router = Router()


def _esc(value) -> str:
    from bot.utils.text import escape_html
    return escape_html(str(value or ""))


# ------------------------------------------------------------------ установка
def _chat_text() -> str:
    from bot.services import creator_chat as cc
    lines = [
        "🆘 <b>Помощь для админа</b>\n",
        "Опишите задачу или проблему. Создатель ответит здесь, в боте, и назовёт сроки.\n",
        "💰 <b>Помощь платная</b>: стоимость зависит от сложности задачи. "
        "Создатель пришлёт предложение с ценой и описанием. Работа начинается после вашего согласия "
        "с условиями и оплаты.\n",
        "<i>Вместе с сообщением уходят: ваш Telegram (ID и @username), username этого бота, "
        "версия и тип лицензии. Токены, ключи и данные ваших клиентов не передаются.</i>",
    ]
    history = cc.recent_local(5)
    if history:
        lines.append("\n<b>Последние сообщения</b>")
        for m in history:
            who = "Вы" if m["direction"] == "me" else "Создатель"
            text = m["text"] if len(m["text"]) <= 300 else m["text"][:297] + "…"
            lines.append(f"• <b>{who}:</b> {_esc(text)}")
    return "\n".join(lines)


def _chat_kb() -> InlineKeyboardMarkup:
    from bot.keyboards.admin_misc import back_button, home_button
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✍️ Написать создателю", callback_data="creator_chat_write")],
        [InlineKeyboardButton(text="🔄 Проверить ответы", callback_data="creator_chat_check")],
        [InlineKeyboardButton(text="📋 Мои заказы", callback_data="hlp_list")],
        [back_button('admin_panel'), home_button()],
    ])


@router.callback_query(F.data == "creator_chat")
async def creator_chat_screen(callback: CallbackQuery, state: FSMContext):
    if not is_admin(callback.from_user.id):
        await callback.answer("⛔ Доступ запрещён", show_alert=True)
        return
    from bot.services.license import is_license_server
    if is_license_server():
        await callback.answer("Это главная инсталляция: сообщения приходят вам сюда.", show_alert=True)
        return
    from bot.services import creator_chat as cc
    if not await cc.refresh_status():
        await callback.answer("Помощь для админа сейчас недоступна.", show_alert=True)
        return
    await state.clear()
    await safe_edit_or_send(callback.message, _chat_text(), reply_markup=_chat_kb())
    await callback.answer()


@router.message(Command("creator"))
async def creator_chat_command(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        return
    from bot.services.license import is_license_server
    if is_license_server():
        from bot.keyboards.admin_misc import home_button
        await message.answer("Это главная инсталляция: сообщения приходят вам сюда. Список чатов: /creator_chats",
                             reply_markup=InlineKeyboardMarkup(inline_keyboard=[[home_button()]]))
        return
    from bot.services import creator_chat as cc
    if not await cc.refresh_status():
        from bot.keyboards.admin_misc import home_button
        await message.answer("Помощь для админа сейчас недоступна.",
                             reply_markup=InlineKeyboardMarkup(inline_keyboard=[[home_button()]]))
        return
    await state.clear()
    await message.answer(_chat_text(), parse_mode="HTML", reply_markup=_chat_kb())


@router.callback_query(F.data == "creator_chat_write")
async def creator_chat_write_start(callback: CallbackQuery, state: FSMContext):
    if not is_admin(callback.from_user.id):
        await callback.answer("⛔ Доступ запрещён", show_alert=True)
        return
    from bot.keyboards.admin_misc import back_button, home_button
    await state.set_state(AdminStates.creator_chat_message)
    await safe_edit_or_send(
        callback.message,
        "✍️ Напишите сообщение создателю одним сообщением (до 1500 символов).",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[back_button('creator_chat'), home_button()]]),
    )
    await callback.answer()


@router.message(AdminStates.creator_chat_message)
async def creator_chat_message_entered(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        return
    text = (message.text or "").strip()
    if not text or text.startswith("/"):
        from bot.keyboards.admin_misc import back_button, home_button
        await message.answer("❌ Отправьте текстовое сообщение (без команды).",
                             reply_markup=InlineKeyboardMarkup(inline_keyboard=[[back_button('creator_chat'), home_button()]]))
        return
    from bot.services import creator_chat as cc
    wait = await message.answer("🔄 Отправляю…")
    state_name, data = await cc.send_to_creator(text, message.from_user)
    await state.clear()
    if state_name == "ok":
        note = "✅ Сообщение отправлено. Ответ придёт сюда, в бот."
        if data.get("delivered") is False:
            note = "✅ Сообщение отправлено."
    elif state_name == "unreachable":
        note = "⚠️ Сервер создателя сейчас недоступен. Сообщение не отправлено, попробуйте позже."
    else:
        note = f"❌ {data.get('message') or 'Сообщение не принято.'}"
    try:
        await wait.delete()
    except Exception:
        pass
    await message.answer(note, reply_markup=_chat_kb())


@router.callback_query(F.data == "creator_chat_check")
async def creator_chat_check(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        await callback.answer("⛔ Доступ запрещён", show_alert=True)
        return
    from bot.services import creator_chat as cc
    if not cc.get_token():
        await callback.answer("Вы ещё не писали создателю.", show_alert=True)
        return
    fresh = await cc.poll_once(callback.bot)
    await callback.answer("Новых ответов нет." if not fresh else f"Новых ответов: {fresh}", show_alert=not fresh)
    if fresh:
        await safe_edit_or_send(callback.message, _chat_text(), reply_markup=_chat_kb())


# ------------------------------------------------------------ главная (создатель)
def _creator_only(user_id: int) -> bool:
    from bot.services.license import is_license_server
    return is_admin(user_id) and is_license_server()


@router.callback_query(F.data == "creator_help_toggle")
async def creator_help_toggle(callback: CallbackQuery):
    """Единый выключатель кнопки «Помощь для админа» у всех установок (только главная)."""
    if not _creator_only(callback.from_user.id):
        await callback.answer("⛔ Доступ запрещён", show_alert=True)
        return
    from bot.services import creator_chat as cc
    on = not cc.server_enabled()
    cc.set_server_enabled(on)
    try:
        from bot.handlers.admin.help_orders import home_view
        text, kb = home_view()
        await safe_edit_or_send(callback.message, text, reply_markup=kb)
    except Exception:
        pass
    await callback.answer(
        "✅ Включено: кнопка появится у установок в течение ~30 минут." if on
        else "⏸ Выключено: кнопка исчезнет у установок в течение ~30 минут.",
        show_alert=True,
    )


def _thread_text(thread_id: int) -> str:
    from bot.services import creator_chat as cc
    t = cc.get_thread(thread_id)
    if not t:
        return "Чат не найден."
    lines = [f"💬 <b>Установка #{thread_id}</b>{' 🚫 заблокирована' if t['blocked'] else ''}",
             f"<i>{_esc(cc.thread_title(t))}</i>\n"]
    for m in cc.thread_history(thread_id, 6):
        who = "Он" if m["direction"] == "in" else "Вы"
        text = m["text"] if len(m["text"]) <= 400 else m["text"][:397] + "…"
        lines.append(f"• <b>{who}:</b> {_esc(text)}")
    return "\n".join(lines)


@router.message(Command("creator_chats"))
async def creator_chats_list(message: Message):
    if not _creator_only(message.from_user.id):
        return
    from bot.services import creator_chat as cc
    threads = cc.list_threads(10)
    from bot.keyboards.admin_misc import back_button, home_button
    nav_row = [back_button('ho_home'), home_button()]
    if not threads:
        await message.answer("Пока никто не писал.", reply_markup=InlineKeyboardMarkup(inline_keyboard=[nav_row]))
        return
    rows = [[InlineKeyboardButton(
        text=f"{'🚫 ' if t['blocked'] else ''}#{t['id']} {cc.thread_title(t)}"[:60],
        callback_data=f"cr_open:{t['id']}")] for t in threads]
    await message.answer("✉️ <b>Чаты с установками</b>", parse_mode="HTML",
                         reply_markup=InlineKeyboardMarkup(inline_keyboard=rows + [nav_row]))


@router.callback_query(F.data.startswith("cr_open:"))
async def creator_thread_open(callback: CallbackQuery, state: FSMContext):
    if not _creator_only(callback.from_user.id):
        await callback.answer("⛔ Доступ запрещён", show_alert=True)
        return
    await state.clear()
    from bot.services import creator_chat as cc
    thread_id = int(callback.data.split(":", 1)[1])
    t = cc.get_thread(thread_id)
    await callback.message.answer(_thread_text(thread_id), parse_mode="HTML",
                                  reply_markup=cc.creator_kb(thread_id, bool(t and t["blocked"])))
    await callback.answer()


@router.callback_query(F.data.startswith("cr_reply:"))
async def creator_reply_start(callback: CallbackQuery, state: FSMContext):
    if not _creator_only(callback.from_user.id):
        await callback.answer("⛔ Доступ запрещён", show_alert=True)
        return
    thread_id = int(callback.data.split(":", 1)[1])
    await state.set_state(AdminStates.creator_reply)
    await state.update_data(creator_thread_id=thread_id)
    from bot.keyboards.admin_misc import back_button, home_button
    await callback.message.answer(
        f"✍️ Напишите ответ для установки #{thread_id} (одним сообщением):",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[back_button(f"cr_open:{thread_id}"), home_button()]]))
    await callback.answer()


@router.message(AdminStates.creator_reply)
async def creator_reply_entered(message: Message, state: FSMContext):
    if not _creator_only(message.from_user.id):
        return
    text = (message.text or "").strip()
    if not text or text.startswith("/"):
        from bot.keyboards.admin_misc import home_button
        await message.answer("❌ Отправьте текст ответа (без команды).",
                             reply_markup=InlineKeyboardMarkup(inline_keyboard=[[home_button()]]))
        return
    from bot.services import creator_chat as cc
    data = await state.get_data()
    thread_id = int(data.get("creator_thread_id") or 0)
    await state.clear()
    mid = cc.server_reply(thread_id, text)
    from bot.keyboards.admin_misc import back_button, home_button
    await message.answer(
        f"✅ Ответ сохранён, установка #{thread_id} получит его в течение нескольких минут." if mid
        else "❌ Не удалось сохранить ответ: чат не найден.",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[back_button(f"cr_open:{thread_id}"), home_button()]]),
    )


@router.callback_query(F.data.startswith("cr_block:"))
async def creator_thread_block(callback: CallbackQuery):
    if not _creator_only(callback.from_user.id):
        await callback.answer("⛔ Доступ запрещён", show_alert=True)
        return
    from bot.services import creator_chat as cc
    thread_id = int(callback.data.split(":", 1)[1])
    t = cc.get_thread(thread_id)
    if not t:
        await callback.answer("Чат не найден", show_alert=True)
        return
    now_blocked = not t["blocked"]
    cc.set_blocked(thread_id, now_blocked)
    try:
        await callback.message.edit_reply_markup(reply_markup=cc.creator_kb(thread_id, now_blocked))
    except Exception:
        pass
    await callback.answer("🚫 Установка заблокирована" if now_blocked else "✅ Разблокирована")
