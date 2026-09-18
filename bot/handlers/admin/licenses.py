"""Визуальный раздел управления whitelabel-лицензиями партнёров — кнопка
«🔑 Лицензии партнёров» в главном меню админки (видна только на
инсталляции, которая сама выступает лицензионным сервером)."""
import logging

from aiogram import Router, F
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

from bot.states.admin_states import AdminStates
from bot.utils.admin import is_admin
from bot.utils.text import safe_edit_or_send
from bot.keyboards.admin_licenses import (
    licenses_menu_kb, license_detail_kb, license_deactivate_confirm_kb,
    license_create_tier_kb, license_create_duration_kb, license_tariffs_menu_kb,
    license_cancel_kb, my_license_kb,
)

logger = logging.getLogger(__name__)
router = Router()


def _format_license_summary(licenses: list) -> str:
    total = len(licenses)
    active_full = sum(1 for l in licenses if l["is_active"] and l["tier"] == "full")
    active_basic = sum(1 for l in licenses if l["is_active"] and l["tier"] == "basic")
    inactive = sum(1 for l in licenses if not l["is_active"])
    return (
        f"🔑 <b>Лицензии партнёров</b>\n\n"
        f"Всего: {total} · 💎 Полных: {active_full} · 🔹 Базовых: {active_basic} · 🚫 Неактивных: {inactive}\n\n"
        f"Выберите партнёра, чтобы посмотреть детали:"
    )


@router.callback_query(F.data == "admin_licenses")
async def show_licenses_menu(callback: CallbackQuery, state: FSMContext):
    if not is_admin(callback.from_user.id):
        await callback.answer("⛔ Доступ запрещён", show_alert=True)
        return

    from database.db_licenses import list_partner_licenses
    licenses = list_partner_licenses()

    if not licenses:
        text = "🔑 <b>Лицензии партнёров</b>\n\nПока никому не выдано ни одной лицензии."
    else:
        text = _format_license_summary(licenses)

    await state.set_state(AdminStates.admin_menu)
    await safe_edit_or_send(callback.message, text, reply_markup=licenses_menu_kb(licenses))
    await callback.answer()


@router.callback_query(F.data.startswith("license_view:"))
async def show_license_detail(callback: CallbackQuery, state: FSMContext):
    if not is_admin(callback.from_user.id):
        await callback.answer("⛔ Доступ запрещён", show_alert=True)
        return

    license_key = callback.data.split(":", 1)[1]
    from database.db_licenses import get_partner_license
    lic = get_partner_license(license_key)
    if not lic:
        await callback.answer("❌ Лицензия не найдена (возможно, удалена).", show_alert=True)
        return

    status_text = "✅ Активна" if lic["is_active"] else "🚫 Деактивирована"
    tier_text = "💎 Полный" if lic["tier"] == "full" else "🔹 Базовый"
    expires_text = lic["expires_at"] or "бессрочно"

    text = (
        f"🔑 <b>{lic['partner_name']}</b>\n\n"
        f"Статус: {status_text}\n"
        f"Тариф: {tier_text}\n"
        f"Действует до: {expires_text}\n"
        f"Ключ: <code>{lic['license_key']}</code>\n"
        f"Создана: {lic['created_at']}"
    )
    if lic.get("notes"):
        text += f"\n\nЗаметка: {lic['notes']}"

    await safe_edit_or_send(
        callback.message, text,
        reply_markup=license_detail_kb(lic["license_key"], bool(lic["is_active"]), lic["tier"]),
    )
    await callback.answer()


@router.callback_query(F.data.startswith("license_extend:"))
async def extend_license_action(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        await callback.answer("⛔ Доступ запрещён", show_alert=True)
        return

    _, license_key, days_str = callback.data.split(":")
    from database.db_licenses import extend_partner_license
    extend_partner_license(license_key, int(days_str))
    await callback.answer(f"✅ Продлено на {days_str} дней")
    # Обновляем экран деталей, чтобы сразу видеть новую дату
    callback.data = f"license_view:{license_key}"
    await show_license_detail(callback, None)


@router.callback_query(F.data.startswith("license_set_tier:"))
async def set_license_tier_action(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        await callback.answer("⛔ Доступ запрещён", show_alert=True)
        return

    _, license_key, new_tier = callback.data.split(":")
    from database.db_licenses import set_partner_license_tier
    set_partner_license_tier(license_key, new_tier)
    await callback.answer(f"✅ Тариф изменён на {new_tier}")
    callback.data = f"license_view:{license_key}"
    await show_license_detail(callback, None)


@router.callback_query(F.data.startswith("license_deactivate_confirm:"))
async def confirm_deactivate_license(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        await callback.answer("⛔ Доступ запрещён", show_alert=True)
        return

    license_key = callback.data.split(":", 1)[1]
    await safe_edit_or_send(
        callback.message,
        f"⚠️ Деактивировать лицензию <code>{license_key}</code>?\n\nПартнёр потеряет доступ к платным функциям.",
        reply_markup=license_deactivate_confirm_kb(license_key),
    )
    await callback.answer()


@router.callback_query(F.data.startswith("license_deactivate_do:"))
async def do_deactivate_license(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        await callback.answer("⛔ Доступ запрещён", show_alert=True)
        return

    license_key = callback.data.split(":", 1)[1]
    from database.db_licenses import deactivate_partner_license
    deactivate_partner_license(license_key)
    await callback.answer("🚫 Лицензия деактивирована")
    callback.data = f"license_view:{license_key}"
    await show_license_detail(callback, None)


# ============================================================================
# Создание новой лицензии (мастер из 3 шагов: имя → тариф → срок)
# ============================================================================

@router.callback_query(F.data == "license_create_start")
async def license_create_start(callback: CallbackQuery, state: FSMContext):
    if not is_admin(callback.from_user.id):
        await callback.answer("⛔ Доступ запрещён", show_alert=True)
        return

    await state.set_state(AdminStates.license_create_name)
    await state.update_data(license_flow=None)
    await safe_edit_or_send(
        callback.message,
        "➕ <b>Новая лицензия</b>\n\nВведите имя партнёра (для внутренней пометки):",
        reply_markup=license_cancel_kb(),
    )
    await callback.answer()


@router.message(AdminStates.license_create_name)
async def license_create_name_entered(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        return

    partner_name = (message.text or "").strip()
    if not partner_name or partner_name.startswith("/"):
        await message.answer("❌ Введите корректное имя партнёра.")
        return

    try:
        await message.delete()
    except Exception:
        pass

    await state.update_data(license_partner_name=partner_name)
    await message.answer(
        f"Партнёр: <b>{partner_name}</b>\n\nВыберите тариф:",
        parse_mode="HTML",
        reply_markup=license_create_tier_kb(),
    )


@router.callback_query(F.data.startswith("license_create_tier:"))
async def license_create_tier_selected(callback: CallbackQuery, state: FSMContext):
    """Общий обработчик выбора тарифа — используется и в потоке выдачи
    лицензии напрямую, и в потоке создания тарифа на продажу. Различает
    их по флагу license_flow в данных состояния (устанавливается тем
    потоком, который открыл этот экран)."""
    if not is_admin(callback.from_user.id):
        await callback.answer("⛔ Доступ запрещён", show_alert=True)
        return

    data = await state.get_data()
    tier = callback.data.split(":", 1)[1]

    if data.get("license_flow") == "tariff_create":
        await state.update_data(license_tariff_tier=tier)
        await state.set_state(AdminStates.license_tariff_create_price)
        await safe_edit_or_send(
            callback.message,
            "Введите цену в рублях (например: 5000):",
            reply_markup=license_cancel_kb(),
        )
        await callback.answer()
        return

    await state.update_data(license_tier=tier)
    tier_label = "Полный" if tier == "full" else "Базовый"
    await safe_edit_or_send(
        callback.message,
        f"Тариф: <b>{tier_label}</b>\n\nНа какой срок?",
        reply_markup=license_create_duration_kb(),
    )
    await callback.answer()


@router.callback_query(F.data.startswith("license_create_duration:"))
async def license_create_duration_selected(callback: CallbackQuery, state: FSMContext):
    if not is_admin(callback.from_user.id):
        await callback.answer("⛔ Доступ запрещён", show_alert=True)
        return

    duration_days = int(callback.data.split(":", 1)[1]) or None  # 0 → бессрочно (None)
    data = await state.get_data()
    partner_name = data.get("license_partner_name")
    tier = data.get("license_tier")

    if not partner_name or not tier:
        await callback.answer("❌ Данные потеряны, начните заново.", show_alert=True)
        return

    from database.db_licenses import create_partner_license
    license_key = create_partner_license(partner_name, tier, duration_days)

    await state.set_state(AdminStates.admin_menu)
    duration_text = f"{duration_days} дней" if duration_days else "бессрочно"
    tier_label = "Полный" if tier == "full" else "Базовый"
    await safe_edit_or_send(
        callback.message,
        f"✅ <b>Лицензия создана</b>\n\n"
        f"Партнёр: {partner_name}\n"
        f"Тариф: {tier_label}\n"
        f"Срок: {duration_text}\n\n"
        f"Ключ (отправьте партнёру для вставки в его secrets.env как LICENSE_KEY):\n"
        f"<code>{license_key}</code>",
        reply_markup=license_cancel_kb(),
    )
    await callback.answer()


# ============================================================================
# Тарифы на продажу лицензий (для команды /buy_license)
# ============================================================================

@router.callback_query(F.data == "license_tariffs_menu")
async def show_license_tariffs_menu(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        await callback.answer("⛔ Доступ запрещён", show_alert=True)
        return

    from database.db_licenses import get_active_license_tariffs
    tariffs = get_active_license_tariffs()
    text = "🏷 <b>Тарифы на продажу лицензий</b>\n\n"
    text += "Эти тарифы видят пользователи в команде /buy_license." if tariffs else "Пока нет ни одного тарифа на продажу."

    await safe_edit_or_send(callback.message, text, reply_markup=license_tariffs_menu_kb(tariffs))
    await callback.answer()


@router.callback_query(F.data == "license_tariff_create_start")
async def license_tariff_create_start(callback: CallbackQuery, state: FSMContext):
    if not is_admin(callback.from_user.id):
        await callback.answer("⛔ Доступ запрещён", show_alert=True)
        return

    await state.set_state(AdminStates.license_tariff_create_name)
    await safe_edit_or_send(
        callback.message,
        "➕ <b>Новый тариф на продажу</b>\n\nВведите название тарифа (клиент увидит его в /buy_license):",
        reply_markup=license_cancel_kb(),
    )
    await callback.answer()


@router.message(AdminStates.license_tariff_create_name)
async def license_tariff_create_name_entered(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        return

    name = (message.text or "").strip()
    if not name or name.startswith("/"):
        await message.answer("❌ Введите корректное название.")
        return

    try:
        await message.delete()
    except Exception:
        pass

    await state.update_data(license_tariff_name=name)
    await state.set_state(AdminStates.admin_menu)
    await message.answer(
        f"Название: <b>{name}</b>\n\nВыберите тариф:",
        parse_mode="HTML",
        reply_markup=license_create_tier_kb(),
    )
    # Переиспользуем тот же выбор тарифа, но со своим следующим шагом —
    # помечаем в data, что это поток создания ТАРИФА НА ПРОДАЖУ, а не
    # выдачи лицензии напрямую.
    await state.update_data(license_flow="tariff_create")


@router.message(AdminStates.license_tariff_create_price)
async def license_tariff_create_price_entered(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        return

    text = (message.text or "").strip().replace(",", ".")
    try:
        price_rub = float(text)
    except ValueError:
        await message.answer("❌ Введите число, например: 5000")
        return

    try:
        await message.delete()
    except Exception:
        pass

    data = await state.get_data()
    name = data.get("license_tariff_name")
    tier = data.get("license_tariff_tier")

    from database.db_licenses import create_license_tariff
    create_license_tariff(name, tier, price_rub, duration_days=30)

    await state.set_state(AdminStates.admin_menu)
    await message.answer(
        f"✅ Тариф «{name}» создан ({price_rub:.0f} ₽, 30 дней). Теперь он виден в /buy_license.",
    )


# ============================================================================
# ОБРАТНАЯ СТОРОНА — «💳 Моя лицензия» (на партнёрской инсталляции, где
# задан LICENSE_KEY). Показывает СВОЙ статус и ведёт диплинком на
# покупку/продление в ГЛАВНЫЙ бот, где живут тарифы на лицензии.
# ============================================================================

def _build_my_license_text() -> str:
    from bot.services.license import get_license_key, get_license_tier
    from database.requests import get_setting

    license_key = get_license_key()
    tier = get_license_tier()
    tier_label = "💎 Полный" if tier == "full" else "🔹 Базовый"
    expires_at = get_setting("license_expires_at", "") or "бессрочно"
    partner_name = get_setting("license_partner_name", "") or "—"
    checked_at = get_setting("license_checked_at", "") or "ещё не проверялась"

    return (
        f"💳 <b>Моя лицензия</b>\n\n"
        f"Ключ: <code>{license_key}</code>\n"
        f"Партнёр: {partner_name}\n"
        f"Текущий тариф: {tier_label}\n"
        f"Действует до: {expires_at}\n"
        f"Последняя проверка: {checked_at}"
    )


@router.callback_query(F.data == "my_license")
async def show_my_license(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        await callback.answer("⛔ Доступ запрещён", show_alert=True)
        return

    from bot.services.license import get_license_bot_username

    deep_link = f"https://t.me/{get_license_bot_username()}?start=buy_license"
    await safe_edit_or_send(callback.message, _build_my_license_text(), reply_markup=my_license_kb(deep_link))
    await callback.answer()


@router.callback_query(F.data == "my_license_refresh")
async def refresh_my_license(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        await callback.answer("⛔ Доступ запрещён", show_alert=True)
        return

    from bot.services.license import refresh_license_status, get_license_bot_username

    await callback.answer("Проверяю статус лицензии...")
    await refresh_license_status()

    deep_link = f"https://t.me/{get_license_bot_username()}?start=buy_license"
    await safe_edit_or_send(callback.message, _build_my_license_text(), reply_markup=my_license_kb(deep_link))
