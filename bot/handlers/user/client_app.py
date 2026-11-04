"""Раздел бота про своё приложение. Есть только там, где оно настроено (own_app_url)."""
from typing import Optional

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

router = Router()


def app_site_url() -> Optional[str]:
    """Адрес сайта, если приложение настроено; иначе None — раздела для этого бота нет."""
    from database.requests import get_effective_own_app_url, get_effective_webapp_url

    site = get_effective_webapp_url()
    if not get_effective_own_app_url() or not site:
        return None
    return site.rstrip("/")


def app_screen(site: str, code: str) -> tuple[str, InlineKeyboardMarkup]:
    text = (
        "📱 <b>Приложение ECLIPSE</b>\n\n"
        "Подписка, срок и баланс те же, что в боте: покупать ничего не нужно.\n\n"
        "<b>Как перейти</b>\n"
        "1. Скачайте приложение кнопкой ниже\n"
        "2. Нажмите «Войти в приложение»\n"
        "3. Нажмите большую кнопку, и VPN подключён\n\n"
        "Если кнопка входа не сработала, в приложении выберите «Код из Telegram-бота» и введите:\n"
        f"<code>{code}</code>\n\n"
        "⏳ Код одноразовый и действует 10 минут. Бот продолжит работать как раньше."
    )
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📥 Скачать приложение", url=f"{site}/app")],
        # код после #, чтобы он не попадал в журналы сервера
        [InlineKeyboardButton(text="🔓 Войти в приложение", url=f"{site}/app/login#{code}")],
        [InlineKeyboardButton(text="🈴 На главную", callback_data="start")],
    ])
    return text, keyboard


async def _show(message: Message, telegram_id: int) -> None:
    from database.requests import create_site_login_code

    site = app_site_url()
    if not site:
        await message.answer("Приложение пока недоступно.")
        return
    text, keyboard = app_screen(site, create_site_login_code(telegram_id, ttl_minutes=10))
    await message.answer(text, parse_mode="HTML", reply_markup=keyboard)


@router.message(Command("app"))
async def cmd_app(message: Message):
    await _show(message, message.from_user.id)


@router.callback_query(F.data == "client_app")
async def open_app(callback: CallbackQuery):
    await callback.answer()
    await _show(callback.message, callback.from_user.id)
