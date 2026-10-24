"""Экран клиента «Маршрутизация»: выбор варианта профиля кнопками (Умный / БС / как у всех)."""
from aiogram import F, Router
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup

from bot.utils.text import safe_edit_or_send

router = Router()

VARIANT_TITLES = {'smart': '🧠 Умный', 'whitelist': '🏦 БС'}


def _screen(key_id: int, telegram_id: int):
    from database.requests import get_user_routing_variant, get_happ_routing_variant
    own = get_user_routing_variant(telegram_id)
    default = get_happ_routing_variant()
    current = own or default
    text = (
        "🗺 <b>Маршрутизация</b>\n\n"
        "Выберите, какие сайты и приложения идут напрямую, а какие через VPN.\n\n"
        "<b>Варианты</b>\n"
        "• 🧠 <b>Умный</b> — российские сайты напрямую, зарубежные и AI через VPN. "
        "Подходит большинству.\n"
        "• 🏦 <b>БС</b> — напрямую только банки, Госуслуги, операторы и маркетплейсы, "
        "остальное через VPN. Попробуйте, если банковские приложения или Госуслуги не открываются.\n"
        "• 🔄 <b>Как у всех</b> — вариант, который выбрал администратор "
        f"(сейчас: {VARIANT_TITLES.get(default, default)}).\n\n"
        f"Сейчас у вас: <b>{VARIANT_TITLES.get(current, current)}</b>"
        f"{'' if own else ' (как у всех)'}\n\n"
        "<b>Как применить</b>\n"
        "1. Выберите вариант ниже.\n"
        "2. В приложении (Happ или INCY) обновите подписку.\n"
        "3. Если не помогло, перезапустите приложение.\n\n"
        "Работает в Happ и INCY."
    )

    def mark(active):
        return '✅ ' if active else ''

    rows = [
        [InlineKeyboardButton(text=mark(own == 'smart') + VARIANT_TITLES['smart'],
                              callback_data=f'key_routing_set:{key_id}:smart'),
         InlineKeyboardButton(text=mark(own == 'whitelist') + VARIANT_TITLES['whitelist'],
                              callback_data=f'key_routing_set:{key_id}:whitelist')],
        [InlineKeyboardButton(text=mark(own is None) + '🔄 Как у всех',
                              callback_data=f'key_routing_set:{key_id}:default')],
        [InlineKeyboardButton(text='⬅️ Назад', callback_data=f'key:{key_id}'),
         InlineKeyboardButton(text='🈴 На главную', callback_data='start')],
    ]
    return text, InlineKeyboardMarkup(inline_keyboard=rows)


async def _show(callback: CallbackQuery, key_id: int):
    from database.requests import (
        get_key_details_for_user, get_routing_user_choice_enabled, get_happ_routing_profile_json,
    )
    key = get_key_details_for_user(key_id, callback.from_user.id)
    if not key or not key.get('sub_id'):
        await callback.answer('Ключ не найден или у него нет подписки', show_alert=True)
        return False
    if not get_routing_user_choice_enabled() or get_happ_routing_profile_json():
        await callback.answer('Выбор маршрутизации сейчас недоступен', show_alert=True)
        return False
    text, kb = _screen(key_id, callback.from_user.id)
    await safe_edit_or_send(callback.message, text, reply_markup=kb)
    return True


@router.callback_query(F.data.startswith('key_routing:'))
async def key_routing_open(callback: CallbackQuery):
    if await _show(callback, int(callback.data.split(':')[1])):
        await callback.answer()


@router.callback_query(F.data.startswith('key_routing_set:'))
async def key_routing_set(callback: CallbackQuery):
    _, key_id, variant = callback.data.split(':')
    from database.requests import set_user_routing_variant
    try:
        set_user_routing_variant(callback.from_user.id, None if variant == 'default' else variant)
    except ValueError:
        await callback.answer('Неизвестный вариант', show_alert=True)
        return
    if await _show(callback, int(key_id)):
        await callback.answer('✅ Сохранено. Обновите подписку в приложении', show_alert=False)
