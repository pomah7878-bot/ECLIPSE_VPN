"""
Покупка whitelabel-лицензии прямо в боте — команда /buy_license.

Доступна ЛЮБОМУ пользователю (не только админам бота) — это отдельный
"продукт", который продаёт сам Роман через СВОЙ бот людям, желающим
запустить собственный whitelabel-бот. Полностью отдельно от системы
покупки VPN-ключей.

После успешной оплаты ключ выдаётся АВТОМАТИЧЕСКИ — без участия Романа.
"""
import logging

from aiogram import Router, F
from aiogram.filters import Command
from aiogram.types import CallbackQuery, InlineKeyboardButton, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder

from bot.utils.text import safe_edit_or_send

logger = logging.getLogger(__name__)
router = Router()


def _license_tariffs_kb(tariffs):
    from bot.services.license import features_from_str, GATED_FEATURES

    builder = InlineKeyboardBuilder()
    for t in tariffs:
        duration_text = f"{t['duration_days']} дн." if t["duration_days"] else "бессрочно"
        enabled = features_from_str(t.get("features"))
        if enabled == set(GATED_FEATURES.keys()):
            tier_label = "Полный"
        elif not enabled:
            tier_label = "Базовый"
        else:
            tier_label = f"{len(enabled)} функций"
        builder.row(InlineKeyboardButton(
            text=f"{t['name']} — {tier_label}, {duration_text} — {t['price_rub']:.0f} ₽",
            callback_data=f"buy_license_tariff:{t['id']}",
        ))
    return builder.as_markup()


@router.message(Command("buy_license"))
async def buy_license_cmd(message: Message):
    """Показывает доступные тарифы на whitelabel-лицензию."""
    from database.db_licenses import get_active_license_tariffs

    tariffs = get_active_license_tariffs()
    if not tariffs:
        await message.answer("😔 Пока нет доступных тарифов на лицензию. Обратитесь к администратору напрямую.")
        return

    await message.answer(
        "🏢 <b>Whitelabel-лицензия ECLIPSE</b>\n\n"
        "Запустите собственный VPN-бот под своим брендом на нашей платформе.\n\n"
        "Выберите тариф:",
        parse_mode="HTML",
        reply_markup=_license_tariffs_kb(tariffs),
    )


@router.callback_query(F.data.startswith("buy_license_tariff:"))
async def buy_license_tariff_selected(callback: CallbackQuery):
    """Создаёт оплату ЮKassa (QR/СБП) для выбранного тарифа лицензии."""
    from database.db_licenses import get_license_tariff_by_id, create_license_purchase, save_license_purchase_payment_id
    from bot.services.billing import create_yookassa_qr_payment

    tariff_id = int(callback.data.split(":")[1])
    tariff = get_license_tariff_by_id(tariff_id)
    if not tariff or not tariff["is_active"]:
        await callback.answer("❌ Тариф больше не доступен.", show_alert=True)
        return

    await callback.answer()

    import uuid
    order_id = f"lic{uuid.uuid4().hex[:12]}"
    create_license_purchase(order_id, callback.from_user.id, tariff_id)

    try:
        from aiogram import Bot
        bot_instance = callback.bot
        bot_info = await bot_instance.get_me()
        result = await create_yookassa_qr_payment(
            amount_rub=tariff["price_rub"],
            order_id=order_id,
            description=f"Whitelabel-лицензия «{tariff['name']}»",
            bot_name=bot_info.username,
        )
    except Exception as e:
        logger.error(f"Ошибка создания оплаты лицензии {order_id}: {e}")
        await safe_edit_or_send(callback.message, "❌ Не удалось создать оплату. Попробуйте позже.")
        return

    save_license_purchase_payment_id(order_id, result["yookassa_payment_id"])

    builder = InlineKeyboardBuilder()
    builder.row(InlineKeyboardButton(text="✅ Я оплатил", callback_data=f"check_license_pay:{order_id}"))

    await callback.message.answer_photo(
        photo=result["qr_image_url"],
        caption=(
            f"💳 <b>Оплата лицензии «{tariff['name']}»</b>\n\n"
            f"Сумма: {tariff['price_rub']:.0f} ₽\n\n"
            "Отсканируйте QR-код в приложении вашего банка (СБП), затем нажмите «Я оплатил»."
        ),
        parse_mode="HTML",
        reply_markup=builder.as_markup(),
    )


@router.callback_query(F.data.startswith("check_license_pay:"))
async def check_license_payment(callback: CallbackQuery):
    """Проверяет оплату и, если прошла, автоматически выдаёт ключ. Заказ можно
    проверить только с аккаунта, который его создал; ключ выдаётся один раз."""
    from bot.services.license_purchase import issue_license_for_order, build_issued_text

    order_id = callback.data.split(":", 1)[1]
    partner_name = callback.from_user.username or callback.from_user.full_name or f"user_{callback.from_user.id}"

    await callback.answer("Проверяю оплату...")
    status, info = await issue_license_for_order(
        order_id, expected_telegram_id=callback.from_user.id, partner_name=partner_name,
    )

    if status == "not_found":
        await callback.message.answer("❌ Заказ не найден.")
    elif status == "already":
        await callback.message.answer(
            f"✅ Лицензия уже выдана:\n<code>{info['license_key']}</code>", parse_mode="HTML",
        )
    elif status == "issued":
        await callback.message.answer(build_issued_text(info), parse_mode="HTML")
    elif status == "unpaid":
        await callback.message.answer(
            "⏳ Оплата ещё не поступила. Если вы уже оплатили — подождите немного и нажмите ещё раз."
        )
    elif status == "canceled":
        await callback.message.answer("❌ Платёж отменён. Создайте новый заказ командой /buy_license.")
    elif status == "busy":
        await callback.message.answer("⏳ Лицензия уже выдаётся, подождите несколько секунд и нажмите ещё раз.")
    else:
        await callback.message.answer("❌ Не удалось проверить оплату. Попробуйте ещё раз через минуту.")
