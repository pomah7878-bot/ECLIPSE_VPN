"""Личный кабинет клиента (rich-сообщение с таблицей). Работает только когда функция включена админом."""
import logging

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.types import CallbackQuery, InputRichMessage, Message

from bot.services import client_cabinet as cab

logger = logging.getLogger(__name__)
router = Router()


async def _open(target: Message, telegram_id: int) -> None:
    from database.requests import is_user_banned
    if is_user_banned(telegram_id):
        return
    data = cab.collect(telegram_id)
    kb = cab.keyboard()
    try:
        await target.answer_rich(InputRichMessage(html=cab.build_html(data)), reply_markup=kb)
    except Exception as e:  # noqa: BLE001 — старый клиент/ошибка Telegram: показываем обычным текстом
        logger.warning("Кабинет: rich-сообщение не отправлено, обычный текст: %s", e)
        await target.answer(cab.build_fallback(data), parse_mode="HTML", reply_markup=kb)


@router.message(Command("cabinet"))
async def cabinet_command(message: Message):
    if cab.enabled():
        await _open(message, message.from_user.id)


@router.callback_query(F.data == "cabinet")
async def cabinet_callback(callback: CallbackQuery):
    if not cab.enabled():
        await callback.answer("Раздел недоступен", show_alert=True)
        return
    await _open(callback.message, callback.from_user.id)
    try:
        await callback.message.delete()
    except Exception:  # noqa: BLE001
        pass
    await callback.answer()
