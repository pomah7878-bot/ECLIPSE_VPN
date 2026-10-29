"""Админка: свой баннер подписки (см. bot/services/custom_banner.py)."""
from aiogram import Router, F
from aiogram.types import CallbackQuery, Message, InlineKeyboardButton
from aiogram.utils.keyboard import InlineKeyboardBuilder
from aiogram.fsm.context import FSMContext

from bot.states.admin_states import AdminStates
from bot.utils.admin import is_admin
from bot.utils.text import get_message_text_for_storage, safe_edit_or_send
from bot.keyboards.admin import integrations_edit_cancel_kb
from bot.services import custom_banner as cb
from bot.keyboards.admin_misc import back_button, home_button
from html import escape

router = Router()

_FIELDS = {
    "text": ("Текст баннера", "Одна короткая строка (до 100 знаков; в INCY до 200). Например: «Скидка 30% до воскресенья»."),
    "url": ("Ссылка", "Адрес, который откроется по нажатию. Отправьте «-», чтобы убрать ссылку."),
    "button": ("Текст кнопки", "Подпись на кнопке в Happ (по умолчанию «Подробнее»)."),
    "days": ("Срок показа, дней", "Сколько дней показывать баннер. 0 — без срока."),
}


def _text() -> str:
    from datetime import datetime
    b = cb.active()
    until = int(cb._s("until") or 0)
    srok = datetime.fromtimestamp(until).strftime("%d.%m.%Y %H:%M") if until else "без срока"
    return (
        "📢 <b>Свой баннер</b>\n\n"
        f"Состояние: {'🟢 показывается' if b else '⚪ не показывается'}\n"
        f"• Текст: {escape(cb._s('text')) or '—'}\n"
        f"• Ссылка: {escape(cb._s('url')) or '—'}\n"
        f"• Кнопка: {escape(cb._s('button')) or 'Подробнее'}\n"
        f"• Показывать до: {srok}\n\n"
        "<b>Где виден</b>\n"
        "• Happ и INCY — баннером приложения\n"
        "• Остальные приложения — первой строкой списка серверов (кнопка ниже)\n"
        f"• Пока баннер действует, подписка обновляется каждые {cb.UPDATE_HOURS} ч.\n\n"
        "ℹ️ Автоматические баннеры (подписка истекла, пробный период, осталось ≤3 дней) важнее и "
        "показываются вместо этого. Клиент видит баннер после обновления подписки в приложении."
    )


def _kb():
    on = cb._s("enabled") == "1"
    b = InlineKeyboardBuilder()
    b.row(InlineKeyboardButton(text=f"{'🟢 Включён' if on else '⚪ Выключен'}", callback_data="admin_banner_toggle"))
    b.row(InlineKeyboardButton(text="✏️ Текст", callback_data="admin_banner_edit:text"),
          InlineKeyboardButton(text="🔗 Ссылка", callback_data="admin_banner_edit:url"))
    b.row(InlineKeyboardButton(text="🔘 Кнопка", callback_data="admin_banner_edit:button"),
          InlineKeyboardButton(text="⏳ Срок", callback_data="admin_banner_edit:days"))
    b.row(InlineKeyboardButton(text=f"📋 Строка в списке серверов: {'✅ вкл' if cb.line_enabled() else '⚪ выкл'}",
                               callback_data="admin_banner_line"))
    b.row(back_button("admin_integrations"), home_button())
    return b.as_markup()


async def _show(message):
    await safe_edit_or_send(message, _text(), reply_markup=_kb())


@router.callback_query(F.data == "admin_banner")
async def banner_menu(callback: CallbackQuery, state: FSMContext):
    if not is_admin(callback.from_user.id):
        await callback.answer("⛔ Доступ запрещён", show_alert=True)
        return
    await state.clear()
    await _show(callback.message)
    await callback.answer()


@router.callback_query(F.data.in_({"admin_banner_toggle", "admin_banner_line"}))
async def banner_toggle(callback: CallbackQuery, state: FSMContext):
    if not is_admin(callback.from_user.id):
        await callback.answer("⛔ Доступ запрещён", show_alert=True)
        return
    key = "enabled" if callback.data == "admin_banner_toggle" else "line"
    cb.save(key, "0" if cb._s(key) == "1" else "1")
    await _show(callback.message)
    await callback.answer("✅ Готово")


@router.callback_query(F.data.startswith("admin_banner_edit:"))
async def banner_edit_start(callback: CallbackQuery, state: FSMContext):
    if not is_admin(callback.from_user.id):
        await callback.answer("⛔ Доступ запрещён", show_alert=True)
        return
    field = callback.data.split(":", 1)[1]
    if field not in _FIELDS:
        await callback.answer()
        return
    await state.set_state(AdminStates.edit_banner_value)
    await state.update_data(banner_field=field)
    title, hint = _FIELDS[field]
    await safe_edit_or_send(callback.message, f"<b>{title}</b>\n\n{hint}\n\nОтправьте значение:",
                            reply_markup=integrations_edit_cancel_kb("admin_banner"))
    await callback.answer()


@router.message(AdminStates.edit_banner_value, F.text, ~F.text.startswith("/"))
async def banner_edit_save(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        return
    field = (await state.get_data()).get("banner_field")
    value = get_message_text_for_storage(message, "plain").strip()
    if field == "days":
        if not value.isdigit() or int(value) > 3650:
            await message.answer("❌ Нужно целое число дней (0 — без срока). Попробуйте ещё раз.")
            return
        cb.set_days(int(value))
    elif field == "url":
        value = "" if value == "-" else value
        if value and not value.startswith(("https://", "http://", "tg://")):
            await message.answer("❌ Ссылка должна начинаться с https://. Попробуйте ещё раз.")
            return
        cb.save("url", value)
    elif field in ("text", "button"):
        if not value or len(value) > (100 if field == "text" else 30):
            await message.answer("❌ Пусто или слишком длинно. Попробуйте ещё раз.")
            return
        cb.save(field, value)
    else:
        return
    await state.clear()
    await message.answer(_text(), parse_mode="HTML", reply_markup=_kb())
