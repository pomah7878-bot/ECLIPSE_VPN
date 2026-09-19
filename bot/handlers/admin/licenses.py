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
    license_cancel_kb, my_license_kb, license_tariff_detail_kb,
    license_tariff_delete_confirm_kb, license_tariff_edit_features_kb,
    license_tariff_edit_duration_kb, license_tariff_edit_cancel_kb,
    license_features_edit_kb, feature_checkboxes_kb,
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
    from bot.services.license import features_from_str, GATED_FEATURES

    lic = get_partner_license(license_key)
    if not lic:
        await callback.answer("❌ Лицензия не найдена (возможно, удалена).", show_alert=True)
        return

    status_text = "✅ Активна" if lic["is_active"] else "🚫 Деактивирована"
    expires_text = lic["expires_at"] or "бессрочно"

    enabled = features_from_str(lic.get("features"))
    if enabled:
        features_list = "\n".join(f"  ✅ {GATED_FEATURES[k]}" for k in GATED_FEATURES if k in enabled)
    else:
        features_list = "  (нет платных функций)"

    text = (
        f"🔑 <b>{lic['partner_name']}</b>\n\n"
        f"Статус: {status_text}\n"
        f"Действует до: {expires_text}\n"
        f"Ключ: <code>{lic['license_key']}</code>\n"
        f"Создана: {lic['created_at']}\n\n"
        f"Функции:\n{features_list}"
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


@router.callback_query(F.data.startswith("license_features_edit:"))
async def license_features_edit_start(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        await callback.answer("⛔ Доступ запрещён", show_alert=True)
        return

    license_key = callback.data.split(":", 1)[1]
    from database.db_licenses import get_partner_license
    from bot.services.license import features_from_str
    from bot.keyboards.admin_licenses import license_features_edit_kb

    lic = get_partner_license(license_key)
    if not lic:
        await callback.answer("❌ Лицензия не найдена.", show_alert=True)
        return

    selected = features_from_str(lic.get("features"))
    await safe_edit_or_send(
        callback.message,
        f"🔧 <b>Функции лицензии — {lic['partner_name']}</b>\n\nНажимайте, чтобы включить/выключить:",
        reply_markup=license_features_edit_kb(license_key, selected),
    )
    await callback.answer()


@router.callback_query(F.data.startswith("license_toggle_feature:"))
async def license_toggle_feature_action(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        await callback.answer("⛔ Доступ запрещён", show_alert=True)
        return

    _, license_key, feature = callback.data.split(":")
    from database.db_licenses import toggle_license_feature
    from bot.keyboards.admin_licenses import license_features_edit_kb
    from database.db_licenses import get_partner_license

    new_selected = toggle_license_feature(license_key, feature)
    await callback.answer()

    lic = get_partner_license(license_key)
    await safe_edit_or_send(
        callback.message,
        f"🔧 <b>Функции лицензии — {lic['partner_name']}</b>\n\nНажимайте, чтобы включить/выключить:",
        reply_markup=license_features_edit_kb(license_key, new_selected),
    )


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

    await state.update_data(license_partner_name=partner_name, selected_features=[])
    await message.answer(
        f"Партнёр: <b>{partner_name}</b>\n\nВыберите функции, которые войдут в лицензию:",
        parse_mode="HTML",
        reply_markup=license_create_tier_kb(set()),
    )


@router.callback_query(F.data.startswith("license_create_toggle_feature:"))
async def license_create_toggle_feature(callback: CallbackQuery, state: FSMContext):
    if not is_admin(callback.from_user.id):
        await callback.answer("⛔ Доступ запрещён", show_alert=True)
        return

    feature = callback.data.split(":", 1)[1]
    data = await state.get_data()
    selected = set(data.get("selected_features", []))
    if feature in selected:
        selected.discard(feature)
    else:
        selected.add(feature)
    await state.update_data(selected_features=list(selected))

    await safe_edit_or_send(
        callback.message,
        "Выберите функции, которые войдут в лицензию:",
        reply_markup=license_create_tier_kb(selected),
    )
    await callback.answer()


@router.callback_query(F.data == "license_create_features_done")
async def license_create_features_done(callback: CallbackQuery, state: FSMContext):
    if not is_admin(callback.from_user.id):
        await callback.answer("⛔ Доступ запрещён", show_alert=True)
        return

    await safe_edit_or_send(
        callback.message,
        "На какой срок выдать лицензию?",
        reply_markup=license_create_duration_kb(),
    )
    await callback.answer()


@router.callback_query(F.data.startswith("license_tariff_create_toggle_feature:"))
async def license_tariff_create_toggle_feature(callback: CallbackQuery, state: FSMContext):
    if not is_admin(callback.from_user.id):
        await callback.answer("⛔ Доступ запрещён", show_alert=True)
        return

    feature = callback.data.split(":", 1)[1]
    data = await state.get_data()
    selected = set(data.get("selected_features", []))
    if feature in selected:
        selected.discard(feature)
    else:
        selected.add(feature)
    await state.update_data(selected_features=list(selected))

    from bot.keyboards.admin_licenses import feature_checkboxes_kb
    await safe_edit_or_send(
        callback.message,
        "Выберите функции, которые войдут в этот тариф на продажу:",
        reply_markup=feature_checkboxes_kb(
            selected,
            toggle_prefix="license_tariff_create_toggle_feature",
            done_callback="license_tariff_create_features_done",
            back_callback="admin_licenses",
        ),
    )
    await callback.answer()


@router.callback_query(F.data == "license_tariff_create_features_done")
async def license_tariff_create_features_done(callback: CallbackQuery, state: FSMContext):
    if not is_admin(callback.from_user.id):
        await callback.answer("⛔ Доступ запрещён", show_alert=True)
        return

    await state.set_state(AdminStates.license_tariff_create_price)
    await safe_edit_or_send(
        callback.message,
        "Введите цену в рублях (например: 5000):",
        reply_markup=license_cancel_kb(),
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
    selected_features = set(data.get("selected_features", []))

    if not partner_name:
        await callback.answer("❌ Данные потеряны, начните заново.", show_alert=True)
        return

    from database.db_licenses import create_partner_license
    from bot.services.license import GATED_FEATURES

    license_key = create_partner_license(partner_name, selected_features, duration_days)

    await state.set_state(AdminStates.admin_menu)
    duration_text = f"{duration_days} дней" if duration_days else "бессрочно"
    if selected_features:
        features_text = ", ".join(GATED_FEATURES[k] for k in GATED_FEATURES if k in selected_features)
    else:
        features_text = "нет платных функций"
    await safe_edit_or_send(
        callback.message,
        f"✅ <b>Лицензия создана</b>\n\n"
        f"Партнёр: {partner_name}\n"
        f"Функции: {features_text}\n"
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

    from database.db_licenses import get_all_license_tariffs
    tariffs = get_all_license_tariffs()
    text = "🏷 <b>Тарифы на продажу лицензий</b>\n\n"
    text += "Клиенты видят в /buy_license только включённые (без 🚫)." if tariffs else "Пока нет ни одного тарифа на продажу."

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

    from bot.keyboards.admin_licenses import feature_checkboxes_kb
    await state.update_data(license_tariff_name=name, selected_features=[])
    await state.set_state(AdminStates.admin_menu)
    await message.answer(
        f"Название: <b>{name}</b>\n\nВыберите функции, которые войдут в этот тариф на продажу:",
        parse_mode="HTML",
        reply_markup=feature_checkboxes_kb(
            set(),
            toggle_prefix="license_tariff_create_toggle_feature",
            done_callback="license_tariff_create_features_done",
            back_callback="admin_licenses",
        ),
    )


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
    selected_features = set(data.get("selected_features", []))

    from database.db_licenses import create_license_tariff
    create_license_tariff(name, selected_features, price_rub, duration_days=30)

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
    from bot.services.license import get_license_key, get_enabled_features, GATED_FEATURES
    from database.requests import get_setting

    license_key = get_license_key()
    if not license_key:
        return (
            f"💳 <b>Моя лицензия</b>\n\n"
            f"Лицензия ещё не активирована.\n\n"
            f"Если вы уже оплатили — введите код кнопкой ниже. Если ещё "
            f"нет — купите лицензию, чтобы разблокировать платные функции."
        )

    enabled = get_enabled_features()
    if enabled:
        features_list = "\n".join(f"  ✅ {GATED_FEATURES[k]}" for k in GATED_FEATURES if k in enabled)
    else:
        features_list = "  (нет активных платных функций)"

    expires_at = get_setting("license_expires_at", "") or "бессрочно"
    partner_name = get_setting("license_partner_name", "") or "—"
    checked_at = get_setting("license_checked_at", "") or "ещё не проверялась"

    return (
        f"💳 <b>Моя лицензия</b>\n\n"
        f"Ключ: <code>{license_key}</code>\n"
        f"Партнёр: {partner_name}\n"
        f"Действует до: {expires_at}\n"
        f"Последняя проверка: {checked_at}\n\n"
        f"Функции:\n{features_list}"
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


# ============================================================================
# ПРОСМОТР И РЕДАКТИРОВАНИЕ ОТДЕЛЬНОГО ТАРИФА НА ЛИЦЕНЗИЮ
# ============================================================================

def _format_tariff_detail_text(t: dict) -> str:
    from bot.services.license import features_from_str, GATED_FEATURES

    duration_text = f"{t['duration_days']} дней" if t["duration_days"] else "бессрочно"
    status_text = "✅ Включён (виден в /buy_license)" if t["is_active"] else "🚫 Отключён (скрыт из /buy_license)"

    enabled = features_from_str(t.get("features"))
    if enabled:
        features_list = "\n".join(f"  ✅ {GATED_FEATURES[k]}" for k in GATED_FEATURES if k in enabled)
    else:
        features_list = "  (нет платных функций)"

    return (
        f"🏷 <b>{t['name']}</b>\n\n"
        f"Срок действия лицензии: {duration_text}\n"
        f"Цена: {t['price_rub']:.0f} ₽\n"
        f"Статус: {status_text}\n\n"
        f"Функции:\n{features_list}"
    )


@router.callback_query(F.data.startswith("license_tariff_view:"))
async def show_license_tariff_detail(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        await callback.answer("⛔ Доступ запрещён", show_alert=True)
        return

    tariff_id = int(callback.data.split(":", 1)[1])
    from database.db_licenses import get_license_tariff_by_id
    t = get_license_tariff_by_id(tariff_id)
    if not t:
        await callback.answer("❌ Тариф не найден (возможно, удалён).", show_alert=True)
        return

    await safe_edit_or_send(
        callback.message, _format_tariff_detail_text(t),
        reply_markup=license_tariff_detail_kb(tariff_id, bool(t["is_active"])),
    )
    await callback.answer()


@router.callback_query(F.data.startswith("license_tariff_edit_name:"))
async def license_tariff_edit_name_start(callback: CallbackQuery, state: FSMContext):
    if not is_admin(callback.from_user.id):
        await callback.answer("⛔ Доступ запрещён", show_alert=True)
        return

    tariff_id = int(callback.data.split(":", 1)[1])
    await state.set_state(AdminStates.license_tariff_edit_name)
    await state.update_data(editing_tariff_id=tariff_id)
    await safe_edit_or_send(
        callback.message, "✏️ Введите новое название тарифа:",
        reply_markup=license_tariff_edit_cancel_kb(tariff_id),
    )
    await callback.answer()


@router.message(AdminStates.license_tariff_edit_name)
async def license_tariff_edit_name_entered(message: Message, state: FSMContext):
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

    data = await state.get_data()
    tariff_id = data["editing_tariff_id"]
    from database.db_licenses import update_license_tariff_field, get_license_tariff_by_id
    update_license_tariff_field(tariff_id, "name", name)
    await state.set_state(AdminStates.admin_menu)

    t = get_license_tariff_by_id(tariff_id)
    await message.answer(
        f"✅ Название обновлено.\n\n{_format_tariff_detail_text(t)}",
        parse_mode="HTML",
        reply_markup=license_tariff_detail_kb(tariff_id, bool(t["is_active"])),
    )


@router.callback_query(F.data.startswith("license_tariff_edit_price:"))
async def license_tariff_edit_price_start(callback: CallbackQuery, state: FSMContext):
    if not is_admin(callback.from_user.id):
        await callback.answer("⛔ Доступ запрещён", show_alert=True)
        return

    tariff_id = int(callback.data.split(":", 1)[1])
    await state.set_state(AdminStates.license_tariff_edit_price)
    await state.update_data(editing_tariff_id=tariff_id)
    await safe_edit_or_send(
        callback.message, "💰 Введите новую цену в рублях (например: 5000):",
        reply_markup=license_tariff_edit_cancel_kb(tariff_id),
    )
    await callback.answer()


@router.message(AdminStates.license_tariff_edit_price)
async def license_tariff_edit_price_entered(message: Message, state: FSMContext):
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
    tariff_id = data["editing_tariff_id"]
    from database.db_licenses import update_license_tariff_field, get_license_tariff_by_id
    update_license_tariff_field(tariff_id, "price_rub", price_rub)
    await state.set_state(AdminStates.admin_menu)

    t = get_license_tariff_by_id(tariff_id)
    await message.answer(
        f"✅ Цена обновлена.\n\n{_format_tariff_detail_text(t)}",
        parse_mode="HTML",
        reply_markup=license_tariff_detail_kb(tariff_id, bool(t["is_active"])),
    )


@router.callback_query(F.data.startswith("license_tariff_edit_duration:"))
async def license_tariff_edit_duration_start(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        await callback.answer("⛔ Доступ запрещён", show_alert=True)
        return

    tariff_id = int(callback.data.split(":", 1)[1])
    await safe_edit_or_send(
        callback.message, "📅 Выберите новый срок действия лицензии по этому тарифу:",
        reply_markup=license_tariff_edit_duration_kb(tariff_id),
    )
    await callback.answer()


@router.callback_query(F.data.startswith("license_tariff_set_duration:"))
async def license_tariff_set_duration(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        await callback.answer("⛔ Доступ запрещён", show_alert=True)
        return

    _, tariff_id_str, duration_str = callback.data.split(":")
    tariff_id = int(tariff_id_str)
    duration_days = int(duration_str) or None

    from database.db_licenses import update_license_tariff_field, get_license_tariff_by_id
    update_license_tariff_field(tariff_id, "duration_days", duration_days)
    await callback.answer("✅ Срок обновлён")

    t = get_license_tariff_by_id(tariff_id)
    await safe_edit_or_send(
        callback.message, _format_tariff_detail_text(t),
        reply_markup=license_tariff_detail_kb(tariff_id, bool(t["is_active"])),
    )


@router.callback_query(F.data.startswith("license_tariff_edit_tier:"))
async def license_tariff_edit_features_start(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        await callback.answer("⛔ Доступ запрещён", show_alert=True)
        return

    tariff_id = int(callback.data.split(":", 1)[1])
    from database.db_licenses import get_license_tariff_by_id
    from bot.services.license import features_from_str
    from bot.keyboards.admin_licenses import license_tariff_edit_features_kb

    t = get_license_tariff_by_id(tariff_id)
    if not t:
        await callback.answer("❌ Тариф не найден.", show_alert=True)
        return

    selected = features_from_str(t.get("features"))
    await safe_edit_or_send(
        callback.message, "🔧 Выберите функции, которые войдут в этот тариф:",
        reply_markup=license_tariff_edit_features_kb(tariff_id, selected),
    )
    await callback.answer()


@router.callback_query(F.data.startswith("license_tariff_toggle_feature:"))
async def license_tariff_toggle_feature_action(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        await callback.answer("⛔ Доступ запрещён", show_alert=True)
        return

    _, tariff_id_str, feature = callback.data.split(":")
    tariff_id = int(tariff_id_str)

    from database.db_licenses import toggle_tariff_feature
    from bot.keyboards.admin_licenses import license_tariff_edit_features_kb

    new_selected = toggle_tariff_feature(tariff_id, feature)
    await callback.answer()

    await safe_edit_or_send(
        callback.message, "🔧 Выберите функции, которые войдут в этот тариф:",
        reply_markup=license_tariff_edit_features_kb(tariff_id, new_selected),
    )


@router.callback_query(F.data.startswith("license_tariff_toggle:"))
async def license_tariff_toggle(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        await callback.answer("⛔ Доступ запрещён", show_alert=True)
        return

    _, tariff_id_str, new_state = callback.data.split(":")
    tariff_id = int(tariff_id_str)

    from database.db_licenses import update_license_tariff_field, get_license_tariff_by_id
    update_license_tariff_field(tariff_id, "is_active", int(new_state))
    await callback.answer("✅ Статус обновлён")

    t = get_license_tariff_by_id(tariff_id)
    await safe_edit_or_send(
        callback.message, _format_tariff_detail_text(t),
        reply_markup=license_tariff_detail_kb(tariff_id, bool(t["is_active"])),
    )


@router.callback_query(F.data.startswith("license_tariff_delete_confirm:"))
async def license_tariff_delete_confirm(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        await callback.answer("⛔ Доступ запрещён", show_alert=True)
        return

    tariff_id = int(callback.data.split(":", 1)[1])
    await safe_edit_or_send(
        callback.message,
        "⚠️ Удалить этот тариф навсегда? Уже выданные по нему лицензии не пострадают — удаляется только карточка тарифа.",
        reply_markup=license_tariff_delete_confirm_kb(tariff_id),
    )
    await callback.answer()


@router.callback_query(F.data.startswith("license_tariff_delete_do:"))
async def license_tariff_delete_do(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        await callback.answer("⛔ Доступ запрещён", show_alert=True)
        return

    tariff_id = int(callback.data.split(":", 1)[1])
    from database.db_licenses import delete_license_tariff, get_all_license_tariffs
    delete_license_tariff(tariff_id)
    await callback.answer("🗑 Тариф удалён")

    tariffs = get_all_license_tariffs()
    text = "🏷 <b>Тарифы на продажу лицензий</b>\n\n"
    text += "Клиенты видят в /buy_license только включённые (без 🚫)." if tariffs else "Пока нет ни одного тарифа на продажу."
    await safe_edit_or_send(callback.message, text, reply_markup=license_tariffs_menu_kb(tariffs))


@router.callback_query(F.data == "my_license_enter_code")
async def my_license_enter_code_start(callback: CallbackQuery, state: FSMContext):
    if not is_admin(callback.from_user.id):
        await callback.answer("⛔ Доступ запрещён", show_alert=True)
        return

    from bot.keyboards.admin_licenses import my_license_enter_code_cancel_kb

    await state.set_state(AdminStates.my_license_enter_code)
    await safe_edit_or_send(
        callback.message,
        "🔑 Введите код лицензии, который вам выдали (вида ECLW-XXXX-XXXX-XXXX):",
        reply_markup=my_license_enter_code_cancel_kb(),
    )
    await callback.answer()


@router.message(AdminStates.my_license_enter_code)
async def my_license_enter_code_entered(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        return

    code = (message.text or "").strip().upper()
    if not code or code.startswith("/"):
        await message.answer("❌ Введите корректный код лицензии.")
        return

    try:
        await message.delete()
    except Exception:
        pass

    from database.requests import set_setting
    from bot.services.license import refresh_license_status, get_license_bot_username

    # Сохраняем код ПРЯМО В БД (не требует правки secrets.env и
    # перезапуска бота вручную) — get_license_key() подхватит его
    # автоматически при следующей же проверке.
    set_setting("license_key_override", code)
    await state.set_state(AdminStates.admin_menu)

    await message.answer("🔄 Проверяю код...")
    await refresh_license_status()

    deep_link = f"https://t.me/{get_license_bot_username()}?start=buy_license"
    await message.answer(_build_my_license_text(), parse_mode="HTML", reply_markup=my_license_kb(deep_link))
