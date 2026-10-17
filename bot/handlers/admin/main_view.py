"""
Админка → Настройки бота → «🏠 Вид главной» (v1.347).

Выбор вида главной страницы для клиентов:
- «Классический» — одна главная для всех, как было раньше;
- «Приложение» — разная главная для новичка, клиента и истёкшей подписки.
Текст и кнопки каждого вида сохраняются, переключаться можно в любой момент.
"""
import logging

from aiogram import F, Router
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup

from bot.keyboards.admin_misc import back_button, home_button, state_pair_buttons
from bot.utils.admin import is_admin
from bot.utils.text import safe_edit_or_send

logger = logging.getLogger(__name__)
router = Router()

VIEW_LABELS = {'classic': '🏛 Классический', 'app': '📱 Приложение'}


def view_label() -> str:
    try:
        from bot.services.main_screen import current_view
        return VIEW_LABELS.get(current_view(), '🏛 Классический')
    except Exception:  # noqa: BLE001
        return '🏛 Классический'


def _text(view: str) -> str:
    return (
        "🏠 <b>Вид главной страницы</b>\n\n"
        f"Сейчас: <b>{VIEW_LABELS.get(view, view)}</b>\n\n"
        "<b>🏛 Классический</b>\n"
        "• одна главная для всех — ваш текст и тарифы\n"
        "• кнопки — как в редакторе страниц\n\n"
        "<b>📱 Приложение</b>\n"
        "• новичку — короткое предложение и «🎁 Попробовать бесплатно»\n"
        "• клиенту — карточка подписки и «⚡ Подключиться»\n"
        "• если подписка кончилась — «🔄 Продлить»\n\n"
        "<i>Текст и кнопки каждого вида сохраняются: переключайтесь в любой момент, "
        "ничего не потеряется. Картинка (баннер) общая для обоих видов.</i>"
    )


def _kb(view: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        list(state_pair_buttons(
            view == 'classic',
            'Классический', 'admin_main_view_set:classic',
            'Приложение', 'admin_main_view_set:app',
            right_active_emoji='🟢',
        )),
        [back_button('admin_bot_settings'), home_button()],
    ])


async def _show(callback: CallbackQuery) -> None:
    from bot.services.main_screen import current_view
    view = current_view()
    await safe_edit_or_send(callback.message, _text(view), reply_markup=_kb(view))


@router.callback_query(F.data == 'admin_main_view')
async def cb_main_view(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        await callback.answer('⛔ Доступ запрещён', show_alert=True)
        return
    await _show(callback)
    await callback.answer()


@router.callback_query(F.data.startswith('admin_main_view_set:'))
async def cb_main_view_set(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        await callback.answer('⛔ Доступ запрещён', show_alert=True)
        return
    target = callback.data.split(':', 1)[1]
    if target not in VIEW_LABELS:
        await callback.answer()
        return
    from bot.services.main_screen import switch_view
    try:
        changed = switch_view(target)
    except Exception as e:  # noqa: BLE001
        logger.error('Не удалось переключить вид главной: %s', e)
        await callback.answer('❌ Не удалось переключить вид', show_alert=True)
        return
    await _show(callback)
    await callback.answer(
        f"✅ Включён вид «{VIEW_LABELS[target].split(' ', 1)[1]}». Нажмите /start, чтобы посмотреть."
        if changed else 'Этот вид уже включён'
    )
