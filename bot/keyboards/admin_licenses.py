"""Клавиатуры для визуального раздела управления whitelabel-лицензиями
партнёров (в меню админки — кнопка «🔑 Лицензии партнёров»)."""
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from aiogram.utils.keyboard import InlineKeyboardBuilder

from bot.keyboards.admin_misc import back_button, home_button


def licenses_menu_kb(licenses: list) -> InlineKeyboardMarkup:
    """Главный экран раздела — сводка + список партнёров кнопками."""
    builder = InlineKeyboardBuilder()
    for lic in licenses[:20]:  # не более 20 в одном экране, чтобы не раздувать сообщение
        status_icon = "✅" if lic["is_active"] else "🚫"
        tier_icon = "💎" if lic["tier"] == "full" else "🔹"
        builder.row(InlineKeyboardButton(
            text=f"{status_icon} {tier_icon} {lic['partner_name']}",
            callback_data=f"license_view:{lic['license_key']}",
        ))
    builder.row(InlineKeyboardButton(text="➕ Выдать новую лицензию", callback_data="license_create_start"))
    builder.row(InlineKeyboardButton(text="🏷 Тарифы на продажу", callback_data="license_tariffs_menu"))
    builder.row(back_button('admin_panel'), home_button())
    return builder.as_markup()


def license_detail_kb(license_key: str, is_active: bool, tier: str) -> InlineKeyboardMarkup:
    """Действия для конкретной лицензии."""
    builder = InlineKeyboardBuilder()
    builder.row(InlineKeyboardButton(text="📅 Продлить на 30 дней", callback_data=f"license_extend:{license_key}:30"))
    builder.row(InlineKeyboardButton(text="📅 Продлить на 365 дней", callback_data=f"license_extend:{license_key}:365"))

    other_tier = "basic" if tier == "full" else "full"
    other_tier_label = "Базовый" if other_tier == "basic" else "Полный"
    builder.row(InlineKeyboardButton(text=f"🔄 Сменить на «{other_tier_label}»", callback_data=f"license_set_tier:{license_key}:{other_tier}"))

    if is_active:
        builder.row(InlineKeyboardButton(text="🚫 Деактивировать", callback_data=f"license_deactivate_confirm:{license_key}"))
    builder.row(back_button('admin_licenses'), home_button())
    return builder.as_markup()


def license_deactivate_confirm_kb(license_key: str) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.row(
        InlineKeyboardButton(text="✅ Да, деактивировать", callback_data=f"license_deactivate_do:{license_key}"),
        InlineKeyboardButton(text="❌ Отмена", callback_data=f"license_view:{license_key}"),
    )
    return builder.as_markup()


def license_create_tier_kb() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.row(
        InlineKeyboardButton(text="🔹 Базовый", callback_data="license_create_tier:basic"),
        InlineKeyboardButton(text="💎 Полный", callback_data="license_create_tier:full"),
    )
    builder.row(back_button('admin_licenses'))
    return builder.as_markup()


def license_create_duration_kb() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.row(
        InlineKeyboardButton(text="30 дней", callback_data="license_create_duration:30"),
        InlineKeyboardButton(text="90 дней", callback_data="license_create_duration:90"),
        InlineKeyboardButton(text="365 дней", callback_data="license_create_duration:365"),
    )
    builder.row(InlineKeyboardButton(text="♾ Бессрочно", callback_data="license_create_duration:0"))
    builder.row(back_button('admin_licenses'))
    return builder.as_markup()


def license_tariffs_menu_kb(tariffs: list) -> InlineKeyboardMarkup:
    """Список тарифов на продажу лицензий (для /buy_license)."""
    builder = InlineKeyboardBuilder()
    for t in tariffs:
        duration_text = f"{t['duration_days']}дн" if t["duration_days"] else "бессрочно"
        builder.row(InlineKeyboardButton(
            text=f"{t['name']} — {t['price_rub']:.0f}₽ ({duration_text})",
            callback_data=f"license_tariff_view:{t['id']}",
        ))
    builder.row(InlineKeyboardButton(text="➕ Новый тариф на продажу", callback_data="license_tariff_create_start"))
    builder.row(back_button('admin_licenses'), home_button())
    return builder.as_markup()


def license_cancel_kb() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.row(back_button('admin_licenses'))
    return builder.as_markup()


def my_license_kb(buy_deep_link: str) -> InlineKeyboardMarkup:
    """Клавиатура партнёрской (обратной) стороны — «Моя лицензия»."""
    builder = InlineKeyboardBuilder()
    builder.row(InlineKeyboardButton(text="💳 Купить / продлить", url=buy_deep_link))
    builder.row(InlineKeyboardButton(text="🔄 Обновить статус", callback_data="my_license_refresh"))
    builder.row(back_button('admin_panel'), home_button())
    return builder.as_markup()
