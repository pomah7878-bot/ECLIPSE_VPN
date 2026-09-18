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
    builder = InlineKeyboardBuilder()
    for t in tariffs:
        duration_text = f"{t['duration_days']} дн." if t["duration_days"] else "бессрочно"
        tier_label = "Полный" if t["tier"] == "full" else "Базовый"
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
    """Проверяет оплату и, если прошла — автоматически создаёт лицензию
    и выдаёт ключ клиенту без участия администратора."""
    from database.db_licenses import (
        get_license_purchase_by_order_id, get_license_tariff_by_id,
        complete_license_purchase, create_partner_license,
    )
    from bot.services.billing import check_yookassa_payment_status

    order_id = callback.data.split(":", 1)[1]
    purchase = get_license_purchase_by_order_id(order_id)
    if not purchase:
        await callback.answer("❌ Заказ не найден.", show_alert=True)
        return

    if purchase["status"] == "paid":
        await callback.answer()
        await callback.message.answer(
            f"✅ Лицензия уже выдана:\n<code>{purchase['license_key']}</code>",
            parse_mode="HTML",
        )
        return

    await callback.answer("Проверяю оплату...")

    try:
        status = await check_yookassa_payment_status(purchase["yookassa_payment_id"], order_id=order_id)
    except Exception as e:
        logger.error(f"Ошибка проверки оплаты лицензии {order_id}: {e}")
        await callback.message.answer("❌ Не удалось проверить оплату. Попробуйте ещё раз через минуту.")
        return

    if status != "succeeded":
        await callback.message.answer("⏳ Оплата ещё не поступила. Если вы уже оплатили — подождите немного и нажмите ещё раз.")
        return

    tariff = get_license_tariff_by_id(purchase["license_tariff_id"])
    partner_name = callback.from_user.username or callback.from_user.full_name or f"user_{callback.from_user.id}"
    license_key = create_partner_license(
        partner_name=partner_name,
        tier=tariff["tier"],
        duration_days=tariff["duration_days"],
        notes=f"Куплено через бота, order_id={order_id}",
    )
    complete_license_purchase(order_id, license_key)

    await callback.message.answer(
        f"🎉 <b>Оплата прошла успешно!</b>\n\n"
        f"Ваша лицензия ({'Полный' if tariff['tier'] == 'full' else 'Базовый'} тариф) активирована.\n\n"
        f"Ключ лицензии (вставьте в secrets.env вашего бота как <code>LICENSE_KEY</code>):\n"
        f"<code>{license_key}</code>\n\n"
        f"Сохраните этот ключ — он понадобится при настройке вашей инсталляции бота.",
        parse_mode="HTML",
    )
    logger.info(f"Лицензия автоматически выдана: order_id={order_id}, tier={tariff['tier']}, key={license_key}")
