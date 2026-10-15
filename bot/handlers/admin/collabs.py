"""Админ-раздел «🤝 Совместные акции»: создание в два шага, включение одной кнопкой,
аналитика, участники партнёра и выдача им бесплатных ключей."""
import logging

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder

from bot.keyboards.admin_misc import back_button, home_button
from bot.services import collabs as collab_service
from bot.states.admin_states import AdminStates
from bot.utils.admin import is_admin
from bot.utils.text import escape_html, get_message_text_for_storage, safe_edit_or_send
from database import db_collabs

logger = logging.getLogger(__name__)
router = Router()
from bot.utils.admin import is_admin as _is_admin_guard
router.callback_query.filter(lambda c: _is_admin_guard(c.from_user.id))
router.message.filter(lambda m: m.from_user is not None and _is_admin_guard(m.from_user.id))

# поле → (подпись, подсказка, тип, мин, макс)
EDIT_FIELDS = {
    "title": ("✏️ Название партнёра", "Введите название партнёра (оно показывается клиентам).", "text", None, None),
    "description": ("📝 Описание для клиентов", "Введите короткий текст для клиентов или <code>0</code>, чтобы убрать.", "text", None, None),
    "discount_percent": ("📊 Скидка", "Введите размер скидки в процентах (0–100).", "int", 0, 100),
    "min_months": ("📆 Тарифы от (мес.)", "С какого срока тарифа действует скидка, в месяцах. <code>0</code> — на любые тарифы.", "int", 0, 60),
    "promo_days": ("⏳ Срок акции (дней)", "Сколько дней действует промокод с сегодняшнего дня. <code>0</code> — без срока.", "int", 0, 3650),
    "activation_limit": ("🔢 Лимит активаций", "Сколько раз можно применить промокод. <code>0</code> — без лимита.", "int", 0, 1000000),
    "member_days": ("🔑 Срок ключа участника (дн.)", "На сколько дней выдавать бесплатный ключ участникам партнёра.", "int", 1, 3650),
    "member_devices": ("📱 Устройств на ключ", "Сколько устройств одновременно у ключа участника.", "int", 1, 50),
}


def _nav(back_cb: str):
    return back_button(back_cb), home_button()


async def _licensed(callback: CallbackQuery) -> bool:
    from bot.services.license import is_feature_available
    if not is_feature_available("promo_coupons"):
        await callback.answer("Эта функция доступна на вашем тарифе", show_alert=True)
        return False
    return True


async def _delete_input(message: Message) -> None:
    try:
        await message.delete()
    except Exception:
        pass


async def _bot_username(target) -> str | None:
    try:
        return (await target.bot.get_me()).username
    except Exception:
        return None


# ------------------------------------------------------------------ list --

def _list_kb(campaigns: list) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.row(InlineKeyboardButton(text="➕ Создать акцию", callback_data="admin_collab_new"))
    for c in campaigns[:20]:
        mark = "🟢" if c.get("is_active") else "⚪"
        b.row(InlineKeyboardButton(text=f"{mark} {c['title']}", callback_data=f"admin_collab_view:{c['id']}"))
    b.row(*_nav("admin_marketing"))
    return b.as_markup()


@router.callback_query(F.data == "admin_collabs")
async def admin_collabs(callback: CallbackQuery, state: FSMContext):
    if not await _licensed(callback):
        return
    await state.set_state(AdminStates.admin_menu)
    campaigns = db_collabs.list_campaigns()
    text = (
        "🤝 <b>Совместные акции</b>\n\n"
        "Акция с партнёром — это:\n"
        "• промокод и промо-ссылка со скидкой для аудитории партнёра;\n"
        "• бесплатные ключи для участников партнёра;\n"
        "• аналитика: переходы, оплаты, выручка.\n\n"
        "Клиенты видят, что это совместная акция."
    )
    if not campaigns:
        text += "\n\nАкций пока нет. Нажмите «➕ Создать акцию»: нужно только название партнёра."
    await safe_edit_or_send(callback.message, text, reply_markup=_list_kb(campaigns))
    await callback.answer()


# ------------------------------------------------------------------ card --

def _card_kb(campaign: dict) -> InlineKeyboardMarkup:
    cid = campaign["id"]
    b = InlineKeyboardBuilder()
    toggle = "🟢 Включена — выключить" if campaign.get("is_active") else "⚪ Выключена — включить"
    b.row(InlineKeyboardButton(text=toggle, callback_data=f"admin_collab_toggle:{cid}"))
    b.row(InlineKeyboardButton(text="📊 Аналитика", callback_data=f"admin_collab_stats:{cid}"))
    b.row(InlineKeyboardButton(text="🔑 Выдать ключ участнику", callback_data=f"admin_collab_issue:{cid}"))
    b.row(
        InlineKeyboardButton(text="👥 Участники", callback_data=f"admin_collab_members:{cid}"),
        InlineKeyboardButton(text="⚙️ Настройки", callback_data=f"admin_collab_settings:{cid}"),
    )
    b.row(*_nav("admin_collabs"))
    return b.as_markup()


async def _show_card(target, campaign_id: int, state: FSMContext | None = None, notice: str = "") -> None:
    campaign = db_collabs.get_campaign_full(campaign_id)
    msg = target.message if isinstance(target, CallbackQuery) else target
    if not campaign:
        await safe_edit_or_send(msg, "❌ Акция не найдена", reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[list(_nav("admin_collabs"))]))
        return
    text = collab_service.campaign_card_text(campaign, await _bot_username(msg))
    if notice:
        text = notice + "\n\n" + text
    await safe_edit_or_send(msg, text, reply_markup=_card_kb(campaign))


@router.callback_query(F.data.startswith("admin_collab_view:"))
async def admin_collab_view(callback: CallbackQuery, state: FSMContext):
    if not await _licensed(callback):
        return
    await state.set_state(AdminStates.admin_menu)
    await _show_card(callback, int(callback.data.split(":")[1]))
    await callback.answer()


@router.callback_query(F.data.startswith("admin_collab_toggle:"))
async def admin_collab_toggle(callback: CallbackQuery, state: FSMContext):
    if not await _licensed(callback):
        return
    cid = int(callback.data.split(":")[1])
    campaign = db_collabs.get_campaign(cid)
    if not campaign:
        await callback.answer("Акция не найдена", show_alert=True)
        return
    new_state = not bool(campaign.get("is_active"))
    db_collabs.set_campaign_active(cid, new_state)
    notice = (
        "✅ <b>Акция включена.</b> Промокод и промо-ссылка работают, клиенты видят экран совместной акции."
        if new_state else "⏸ <b>Акция выключена.</b> Промокод больше не принимается."
    )
    await _show_card(callback, cid, notice=notice)
    await callback.answer()


# ---------------------------------------------------------------- create --

@router.callback_query(F.data == "admin_collab_new")
async def admin_collab_new(callback: CallbackQuery, state: FSMContext):
    if not await _licensed(callback):
        return
    await state.set_state(AdminStates.collab_new_title)
    text = (
        "➕ <b>Новая совместная акция</b>\n\n"
        "Введите название партнёра, например название коллектива или канала.\n\n"
        "Остальное бот заполнит сам, потом всё можно поменять в «⚙️ Настройки»:\n"
        f"• скидка {db_collabs.DEFAULT_DISCOUNT}% на тарифы от {db_collabs.DEFAULT_MIN_MONTHS} мес.;\n"
        f"• промокод на {db_collabs.DEFAULT_PROMO_DAYS} дней, до {db_collabs.DEFAULT_ACTIVATION_LIMIT} активаций;\n"
        f"• ключи участникам: безлимит, {db_collabs.DEFAULT_MEMBER_DEVICES} устройств, "
        f"{db_collabs.DEFAULT_MEMBER_DAYS} дней.\n\n"
        "Акция создаётся выключенной — включите её одной кнопкой."
    )
    await safe_edit_or_send(callback.message, text, reply_markup=InlineKeyboardMarkup(
        inline_keyboard=[list(_nav("admin_collabs"))]))
    await callback.answer()


@router.message(AdminStates.collab_new_title, F.text, ~F.text.startswith("/"))
async def collab_new_title(message: Message, state: FSMContext):
    await _delete_input(message)
    title = get_message_text_for_storage(message, "plain").strip()
    if not title or len(title) > 60:
        await safe_edit_or_send(message, "❌ Название — от 1 до 60 символов. Введите ещё раз.",
                                reply_markup=InlineKeyboardMarkup(inline_keyboard=[list(_nav("admin_collabs"))]),
                                force_new=True)
        return
    try:
        cid = db_collabs.create_campaign(title=title, admin_id=message.from_user.id)
    except Exception as e:
        logger.error("Не удалось создать совместную акцию: %s", e)
        await safe_edit_or_send(message, f"❌ Не удалось создать акцию: {escape_html(str(e))}",
                                reply_markup=InlineKeyboardMarkup(inline_keyboard=[list(_nav("admin_collabs"))]),
                                force_new=True)
        return
    await state.set_state(AdminStates.admin_menu)
    campaign = db_collabs.get_campaign_full(cid)
    text = "✅ <b>Акция создана</b> (пока выключена).\n\n" + collab_service.campaign_card_text(
        campaign, await _bot_username(message))
    await safe_edit_or_send(message, text, reply_markup=_card_kb(campaign), force_new=True)


# -------------------------------------------------------------- settings --

def _settings_kb(campaign_id: int) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    order = ["title", "description", "discount_percent", "min_months", "promo_days",
             "activation_limit", "member_days", "member_devices"]
    for field in order:
        b.row(InlineKeyboardButton(text=EDIT_FIELDS[field][0], callback_data=f"admin_collab_edit:{field}:{campaign_id}"))
    b.row(InlineKeyboardButton(text="🗑 Удалить акцию", callback_data=f"admin_collab_delete_ask:{campaign_id}"))
    b.row(*_nav(f"admin_collab_view:{campaign_id}"))
    return b.as_markup()


@router.callback_query(F.data.startswith("admin_collab_settings:"))
async def admin_collab_settings(callback: CallbackQuery, state: FSMContext):
    if not await _licensed(callback):
        return
    await state.set_state(AdminStates.admin_menu)
    cid = int(callback.data.split(":")[1])
    campaign = db_collabs.get_campaign_full(cid)
    if not campaign:
        await callback.answer("Акция не найдена", show_alert=True)
        return
    text = "⚙️ <b>Настройки акции</b>\n\nВыберите, что изменить:\n\n" + collab_service.campaign_card_text(campaign)
    await safe_edit_or_send(callback.message, text, reply_markup=_settings_kb(cid))
    await callback.answer()


@router.callback_query(F.data.startswith("admin_collab_edit:"))
async def admin_collab_edit(callback: CallbackQuery, state: FSMContext):
    if not await _licensed(callback):
        return
    _, field, cid = callback.data.split(":")
    if field not in EDIT_FIELDS:
        await callback.answer()
        return
    label, hint, *_ = EDIT_FIELDS[field]
    await state.set_state(AdminStates.collab_edit_value)
    await state.update_data(collab_edit_field=field, collab_edit_id=int(cid))
    await safe_edit_or_send(
        callback.message, f"{label}\n\n{hint}",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[list(_nav(f"admin_collab_settings:{cid}"))]),
    )
    await callback.answer()


@router.message(AdminStates.collab_edit_value, F.text, ~F.text.startswith("/"))
async def collab_edit_value(message: Message, state: FSMContext):
    await _delete_input(message)
    data = await state.get_data()
    field, cid = data.get("collab_edit_field"), data.get("collab_edit_id")
    if field not in EDIT_FIELDS or not cid:
        await state.set_state(AdminStates.admin_menu)
        return
    label, hint, kind, lo, hi = EDIT_FIELDS[field]
    raw = get_message_text_for_storage(message, "plain").strip()
    back_kb = InlineKeyboardMarkup(inline_keyboard=[list(_nav(f"admin_collab_settings:{cid}"))])
    if kind == "int":
        if not raw.isdigit() or not (lo <= int(raw) <= hi):
            await safe_edit_or_send(message, f"❌ Введите целое число от {lo} до {hi}.\n\n{hint}",
                                    reply_markup=back_kb, force_new=True)
            return
        value = int(raw)
    else:
        if field == "title" and (not raw or len(raw) > 60):
            await safe_edit_or_send(message, "❌ Название — от 1 до 60 символов.", reply_markup=back_kb, force_new=True)
            return
        if len(raw) > 500:
            await safe_edit_or_send(message, "❌ Слишком длинный текст (максимум 500 символов).",
                                    reply_markup=back_kb, force_new=True)
            return
        value = None if (field == "description" and raw == "0") else raw
    try:
        db_collabs.update_campaign(int(cid), **{field: value})
    except Exception as e:
        await safe_edit_or_send(message, f"❌ Не удалось сохранить: {escape_html(str(e))}",
                                reply_markup=back_kb, force_new=True)
        return
    await state.set_state(AdminStates.admin_menu)
    campaign = db_collabs.get_campaign_full(int(cid))
    text = "✅ <b>Сохранено</b>\n\n" + collab_service.campaign_card_text(campaign)
    await safe_edit_or_send(message, text, reply_markup=_settings_kb(int(cid)), force_new=True)


@router.callback_query(F.data.startswith("admin_collab_delete_ask:"))
async def admin_collab_delete_ask(callback: CallbackQuery, state: FSMContext):
    if not await _licensed(callback):
        return
    cid = int(callback.data.split(":")[1])
    b = InlineKeyboardBuilder()
    b.row(InlineKeyboardButton(text="🗑 Да, удалить", callback_data=f"admin_collab_delete_yes:{cid}"))
    b.row(*_nav(f"admin_collab_settings:{cid}"))
    await safe_edit_or_send(
        callback.message,
        "🗑 <b>Удалить акцию?</b>\n\nУдалить можно только акцию без оплат и без выданных ключей. "
        "Иначе её можно только выключить.",
        reply_markup=b.as_markup(),
    )
    await callback.answer()


@router.callback_query(F.data.startswith("admin_collab_delete_yes:"))
async def admin_collab_delete_yes(callback: CallbackQuery, state: FSMContext):
    if not await _licensed(callback):
        return
    cid = int(callback.data.split(":")[1])
    ok, reason = db_collabs.delete_campaign(cid)
    if not ok:
        await callback.answer(reason or "Не удалось удалить", show_alert=True)
        await _show_card(callback, cid)
        return
    await callback.answer("Акция удалена")
    await admin_collabs(callback, state)


# ------------------------------------------------------------- analytics --

@router.callback_query(F.data.startswith("admin_collab_stats:"))
async def admin_collab_stats(callback: CallbackQuery, state: FSMContext):
    if not await _licensed(callback):
        return
    cid = int(callback.data.split(":")[1])
    campaign = db_collabs.get_campaign_full(cid)
    if not campaign:
        await callback.answer("Акция не найдена", show_alert=True)
        return
    b = InlineKeyboardBuilder()
    b.row(InlineKeyboardButton(text="🔄 Обновить", callback_data=f"admin_collab_stats:{cid}"))
    b.row(*_nav(f"admin_collab_view:{cid}"))
    await safe_edit_or_send(callback.message, collab_service.campaign_stats_text(campaign), reply_markup=b.as_markup())
    await callback.answer()


@router.callback_query(F.data.startswith("admin_collab_members:"))
async def admin_collab_members(callback: CallbackQuery, state: FSMContext):
    if not await _licensed(callback):
        return
    cid = int(callback.data.split(":")[1])
    campaign = db_collabs.get_campaign(cid)
    if not campaign:
        await callback.answer("Акция не найдена", show_alert=True)
        return
    members = db_collabs.list_members(cid)
    lines = [f"👥 <b>Участники: {escape_html(campaign['title'])}</b>", ""]
    if not members:
        lines.append("Пока никому не выдавали ключ. Нажмите «🔑 Выдать ключ участнику» в карточке акции.")
    for m in members[:40]:
        who = f"@{m['username']}" if m.get("username") else f"ID {m['telegram_id']}"
        lines.append(f"• {escape_html(who)} — ключ #{m.get('key_id') or '—'}, {str(m.get('issued_at') or '')[:10]}")
    b = InlineKeyboardBuilder()
    b.row(InlineKeyboardButton(text="🔑 Выдать ключ участнику", callback_data=f"admin_collab_issue:{cid}"))
    b.row(*_nav(f"admin_collab_view:{cid}"))
    await safe_edit_or_send(callback.message, "\n".join(lines), reply_markup=b.as_markup())
    await callback.answer()


# --------------------------------------------------------------- key issue --

@router.callback_query(F.data.startswith("admin_collab_issue:"))
async def admin_collab_issue(callback: CallbackQuery, state: FSMContext):
    if not await _licensed(callback):
        return
    cid = int(callback.data.split(":")[1])
    campaign = db_collabs.get_campaign(cid)
    if not campaign:
        await callback.answer("Акция не найдена", show_alert=True)
        return
    await state.set_state(AdminStates.collab_issue_user)
    await state.update_data(collab_issue_id=cid)
    text = (
        "🔑 <b>Ключ участнику</b>\n\n"
        f"Партнёр: <b>{escape_html(campaign['title'])}</b>\n"
        f"Ключ: безлимит, {int(campaign['member_devices'])} устройств, {int(campaign['member_days'])} дн.\n\n"
        "Отправьте <b>Telegram ID</b> или <b>@username</b> участника.\n"
        "Участник должен хотя бы раз запустить бота."
    )
    await safe_edit_or_send(callback.message, text, reply_markup=InlineKeyboardMarkup(
        inline_keyboard=[list(_nav(f"admin_collab_view:{cid}"))]))
    await callback.answer()


@router.message(AdminStates.collab_issue_user, F.text, ~F.text.startswith("/"))
async def collab_issue_user(message: Message, state: FSMContext):
    from bot.handlers.admin.users_manage import format_user_display
    from database.requests import get_active_servers, get_user_by_telegram_id, get_user_by_username
    from bot.keyboards.admin_users import add_key_confirm_kb, add_key_server_kb
    from bot.services.vpn_api import is_subscription_mode

    await _delete_input(message)
    data = await state.get_data()
    cid = data.get("collab_issue_id")
    campaign = db_collabs.get_campaign(int(cid)) if cid else None
    back_kb = InlineKeyboardMarkup(inline_keyboard=[list(_nav(f"admin_collab_view:{cid}"))])
    if not campaign:
        await state.set_state(AdminStates.admin_menu)
        return

    raw = get_message_text_for_storage(message, "plain").strip()
    user = get_user_by_telegram_id(int(raw)) if raw.lstrip("-").isdigit() else get_user_by_username(raw.lstrip("@"))
    if not user:
        await safe_edit_or_send(
            message,
            "❌ Пользователь не найден. Он должен хотя бы раз запустить бота.\n\n"
            "Отправьте Telegram ID или @username ещё раз.",
            reply_markup=back_kb, force_new=True,
        )
        return

    tariff_id = db_collabs.ensure_member_tariff(int(cid))
    servers = get_active_servers()
    if not servers:
        await safe_edit_or_send(message, "❌ Нет активных серверов.", reply_markup=back_kb, force_new=True)
        return

    await state.update_data(
        add_key_user_id=user["id"], add_key_user_telegram_id=user["telegram_id"],
        add_key_traffic_gb=0, add_key_days=int(campaign["member_days"]),
        add_key_collab_id=int(cid), add_key_tariff_id=tariff_id,
        add_key_server_id=None, add_key_inbound_id=None,
    )
    who = escape_html(format_user_display(user))
    if is_subscription_mode() and len(servers) == 1:
        server = servers[0]
        await state.update_data(add_key_server_id=server["id"])
        await state.set_state(AdminStates.add_key_confirm)
        text = (
            "✅ <b>Подтверждение: ключ участнику акции</b>\n\n"
            f"🤝 Акция: {escape_html(campaign['title'])}\n"
            f"👤 Участник: {who}\n"
            f"🖥️ Сервер: {escape_html(str(server['name']))}\n"
            "📊 Трафик: без лимита\n"
            f"📅 Срок: {int(campaign['member_days'])} дней\n"
            f"📱 Устройств: {int(campaign['member_devices'])}\n"
        )
        await safe_edit_or_send(message, text, reply_markup=add_key_confirm_kb(), force_new=True)
        return

    # Несколько серверов или режим без подписки: дальше обычный мастер выдачи ключа
    await state.set_state(AdminStates.add_key_server)
    await safe_edit_or_send(
        message,
        f"➕ <b>Ключ участнику акции «{escape_html(campaign['title'])}»</b>\n"
        f"Участник: {who}\n\nВыберите сервер (трафик — 0 = без лимита, срок — {int(campaign['member_days'])} дн.):",
        reply_markup=add_key_server_kb(servers), force_new=True,
    )
