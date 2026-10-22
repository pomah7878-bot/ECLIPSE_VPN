"""Экран «⋯ Ещё» вида «Приложение»: всё, что не нужно на главной, по четырём разделам.

Главная остаётся короткой (Подключиться / Мои ключи / Тарифы / Ещё), а здесь — разделы
не больше шести кнопок по две в ряд: 💰 Баланс и бонусы, 🛟 Помощь, 🔑 Мой доступ, ℹ️ О сервисе.
Кнопки показываются по тем же правилам, что и на классической главной.
"""
import logging

from aiogram import F, Router
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup

from bot.utils.text import safe_edit_or_send

logger = logging.getLogger(__name__)
router = Router()

SECTIONS = {
    'money': '💰 Баланс и бонусы',
    'help': '🛟 Помощь',
    'access': '🔑 Мой доступ',
    'about': 'ℹ️ О сервисе',
}


def _enabled(user_id: int) -> dict:
    """Какие необязательные функции сейчас доступны этому пользователю."""
    from database.requests import is_referral_enabled, is_start_balance_button_enabled, is_ai_support_enabled
    from bot.services.license import is_feature_available
    flags = {
        'referral': bool(is_referral_enabled()),
        'balance': bool(is_start_balance_button_enabled()),
        'ai': bool(is_feature_available('ai_assistant') and is_ai_support_enabled()),
    }
    try:
        from bot.handlers.user.roulette import can_spin
        flags['roulette'] = bool(can_spin(user_id))
    except Exception:  # noqa: BLE001
        flags['roulette'] = False
    try:
        from bot.handlers.user.bonus_tasks import is_visible
        flags['bonus'] = bool(is_visible(user_id))
    except Exception:  # noqa: BLE001
        flags['bonus'] = False
    return flags


def _links() -> list:
    """Новости и сайт — только если настроены (у каждой установки свои)."""
    from database.requests import get_marketing_channel_id, get_effective_webapp_url
    buttons = []
    channel = (get_marketing_channel_id() or '').lstrip('@')
    if channel:
        buttons.append(InlineKeyboardButton(text='📰 Новости', url=f'https://t.me/{channel}'))
    site = (get_effective_webapp_url() or '').rstrip('/')
    if site:
        buttons.append(InlineKeyboardButton(text='🌐 Сайт', url=f'{site}/shop'))
    return buttons


def _cb(text: str, data: str) -> InlineKeyboardButton:
    return InlineKeyboardButton(text=text, callback_data=data)


def _rows(buttons: list) -> list:
    return [buttons[i:i + 2] for i in range(0, len(buttons), 2)]


def _nav(back: str) -> list:
    return [_cb('⬅️ Назад', back), _cb('🈴 На главную', 'start')]


def build_section(section: str, user_id: int) -> tuple:
    """(текст, клавиатура) раздела."""
    flags = _enabled(user_id)
    if section == 'money':
        buttons = []
        if flags['balance']:
            buttons.append(_cb('💳 Пополнить', 'balance_topup_menu'))
        if flags['referral']:
            buttons.append(_cb('🤝 Пригласить друзей', 'referral_system'))
        if flags['bonus']:
            buttons.append(_cb('🎁 Бонусные дни', 'bonus_tasks'))
        if flags['roulette']:
            buttons.append(_cb('🎰 Рулетка', 'roulette_spin'))
    elif section == 'help':
        buttons = [_cb('📖 Инструкция', 'help')]
        if flags['ai']:
            buttons.append(_cb('🤖 AI-помощник', 'ai_support_open'))
        buttons.append(_cb('💬 Поддержка', 'support_start'))
        buttons.append(_cb('📡 Статус серверов', 'server_status'))
    elif section == 'access':
        buttons = [_cb('🔑 Мои ключи', 'my_keys'), _cb('💳 Тарифы', 'buy_key'), _cb('🆔 Мой ID', 'show_id')]
    else:
        section = 'about'
        buttons = _links()
    rows = _rows(buttons) + [_nav('app_more')]
    text = f"<b>{SECTIONS[section]}</b>"
    if not buttons:
        text += "\n\nПока здесь пусто."
    return text, InlineKeyboardMarkup(inline_keyboard=rows)


def build_menu(user_id: int) -> tuple:
    """(текст, клавиатура) экрана «Ещё»: четыре раздела 2×2, ниже — «На главную»."""
    sections = list(SECTIONS)
    if not _links():
        sections.remove('about')
    buttons = [_cb(SECTIONS[s], f'app_more:{s}') for s in sections]
    rows = _rows(buttons) + [[_cb('🈴 На главную', 'start')]]
    return '<b>⋯ Ещё</b>\n\nВыберите раздел:', InlineKeyboardMarkup(inline_keyboard=rows)


@router.callback_query(F.data == 'app_more')
async def app_more_menu(callback: CallbackQuery):
    text, kb = build_menu(callback.from_user.id)
    await safe_edit_or_send(callback.message, text, reply_markup=kb)
    await callback.answer()


@router.callback_query(F.data.startswith('app_more:'))
async def app_more_section(callback: CallbackQuery):
    section = callback.data.split(':', 1)[1]
    text, kb = build_section(section, callback.from_user.id)
    await safe_edit_or_send(callback.message, text, reply_markup=kb)
    await callback.answer()
