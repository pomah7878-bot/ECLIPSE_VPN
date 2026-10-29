"""
Админка → Настройки бота → «🏠 Вид главной» (v1.347).

Выбор вида главной страницы для клиентов:
- «Классический» — одна главная для всех, как было раньше;
- «Приложение» — разная главная для новичка, клиента и истёкшей подписки.
Текст и кнопки каждого вида сохраняются, переключаться можно в любой момент.
"""
import logging

from aiogram import F, Router
from aiogram.filters import StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from bot.keyboards.admin_misc import back_button, home_button, state_pair_buttons
from bot.utils.admin import is_admin
from bot.utils.text import safe_edit_or_send

logger = logging.getLogger(__name__)
router = Router()

class IconPackState(StatesGroup):
    waiting_pack = State()


VIEW_LABELS = {'classic': '🏛 Классический', 'app': '📱 Приложение'}


def view_label() -> str:
    try:
        from bot.services.main_screen import current_view
        return VIEW_LABELS.get(current_view(), '🏛 Классический')
    except Exception:  # noqa: BLE001
        return '🏛 Классический'


def _webapp_on() -> bool:
    from bot.services.main_screen import webapp_app_view_enabled
    return webapp_app_view_enabled()


def _cosmic_on() -> bool:
    from bot.services.main_screen import webapp_cosmic_enabled
    return webapp_cosmic_enabled()


def _text(view: str) -> str:
    cs = '🟢 включена' if _cosmic_on() else '⚪ выключена'
    wa = '🟢 включён' if _webapp_on() else '⚪ выключен'
    from bot.services import button_icons as bi
    ic = '🟢 включены' if bi.is_enabled() else '⚪ выключены'
    pk = f"<code>{bi.pack_name()}</code> ({len(bi.icon_map())} иконок)" if bi.icon_map() else 'не выбран'
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
        f"<b>🌐 Вид «Приложение» в мини-приложении:</b> {wa}\n"
        "• переключается отдельно от бота\n"
        "• новичку — предложение и «🎁 Попробовать», клиенту — карточка подписки, "
        "при истёкшей — «🔄 Продлить»\n\n"
        f"<b>🌌 Космическая тема (мини-приложение и сайт):</b> {cs}\n"
        "• звёздное небо, туманности и свечение на всех экранах кабинета и на сайте\n"
        "• включается отдельно, функции кабинета не меняются\n\n"
        f"<b>🎨 Иконки на кнопках:</b> {ic}\n"
        f"• набор: {pk}\n"
        "• вместо обычного эмодзи в начале кнопки показывается иконка из набора (часто анимированная)\n"
        "• нужен набор пользовательских эмодзи (t.me/addemoji/…); у владельца бота должен быть Telegram Premium, "
        "иначе Telegram иконки не покажет, и бот сам вернёт обычные кнопки\n\n"
        "<i>Текст и кнопки каждого вида сохраняются: переключайтесь в любой момент, "
        "ничего не потеряется. Картинка (баннер) общая для обоих видов.</i>"
    )


def _kb(view: str) -> InlineKeyboardMarkup:
    from bot.services import button_icons as bi
    return InlineKeyboardMarkup(inline_keyboard=[
        list(state_pair_buttons(
            view == 'classic',
            'Классический', 'admin_main_view_set:classic',
            'Приложение', 'admin_main_view_set:app',
            right_active_emoji='🟢',
        )),
        [InlineKeyboardButton(
            text=('🟢 Webapp: «Приложение» — выключить' if _webapp_on() else '⚪ Webapp: «Приложение» — включить'),
            callback_data='admin_webapp_view_toggle',
        )],
        [InlineKeyboardButton(
            text=('🟢 Космическая тема — выключить' if _cosmic_on() else '⚪ Космическая тема — включить'),
            callback_data='admin_webapp_cosmic_toggle',
        )],
        [InlineKeyboardButton(
            text=('🟢 Иконки на кнопках — выключить' if bi.is_enabled() else '⚪ Иконки на кнопках — включить'),
            callback_data='admin_btn_icons_toggle',
        )],
        [InlineKeyboardButton(text='📦 Набор иконок', callback_data='admin_btn_icons_pack')],
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


@router.callback_query(F.data == 'admin_webapp_view_toggle')
async def cb_webapp_view_toggle(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        await callback.answer('⛔ Доступ запрещён', show_alert=True)
        return
    from bot.services.main_screen import set_webapp_app_view
    new = not _webapp_on()
    try:
        set_webapp_app_view(new)
    except Exception as e:  # noqa: BLE001
        logger.error('Не удалось переключить вид webapp: %s', e)
        await callback.answer('❌ Не удалось переключить', show_alert=True)
        return
    await _show(callback)
    await callback.answer('✅ Webapp: вид «Приложение» ' + ('включён' if new else 'выключен'))


@router.callback_query(F.data == 'admin_webapp_cosmic_toggle')
async def cb_webapp_cosmic_toggle(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        await callback.answer('⛔ Доступ запрещён', show_alert=True)
        return
    from bot.services.main_screen import set_webapp_cosmic
    new = not _cosmic_on()
    try:
        set_webapp_cosmic(new)
    except Exception as e:  # noqa: BLE001
        logger.error('Не удалось переключить космическую тему: %s', e)
        await callback.answer('❌ Не удалось переключить', show_alert=True)
        return
    await _show(callback)
    await callback.answer('✅ Космическая тема ' + ('включена' if new else 'выключена'))


@router.callback_query(F.data == 'admin_btn_icons_toggle')
async def cb_icons_toggle(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        await callback.answer('⛔ Доступ запрещён', show_alert=True)
        return
    from bot.services import button_icons as bi
    if not bi.is_enabled() and not bi.icon_map():
        await callback.answer('Сначала выберите набор иконок: кнопка «📦 Набор иконок»', show_alert=True)
        return
    new = not bi.is_enabled()
    bi.set_enabled(new)
    await _show(callback)
    await callback.answer('✅ Иконки ' + ('включены' if new else 'выключены'))


@router.callback_query(F.data == 'admin_btn_icons_pack')
async def cb_icons_pack(callback: CallbackQuery, state: FSMContext):
    if not is_admin(callback.from_user.id):
        await callback.answer('⛔ Доступ запрещён', show_alert=True)
        return
    await state.set_state(IconPackState.waiting_pack)
    await safe_edit_or_send(
        callback.message,
        "📦 <b>Набор иконок</b>\n\nПришлите ссылку на набор пользовательских эмодзи, например "
        "<code>t.me/addemoji/имя</code>, или просто его имя.\n"
        "Подойдёт любой набор, в том числе анимированный. Лучше выбрать набор с контурными иконками "
        "для меню: ключ, монета, звезда, люди, книга.",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[back_button('admin_main_view'), home_button()]]),
    )
    await callback.answer()


@router.message(StateFilter(IconPackState.waiting_pack))
async def msg_icons_pack(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        return
    from bot.services import button_icons as bi
    name = bi.parse_pack_name(message.text or '')
    if not name:
        await message.answer('Не похоже на набор. Пришлите ссылку вида t.me/addemoji/имя')
        return
    try:
        mapping = await bi.load_pack(message.bot, name)
    except Exception as e:  # noqa: BLE001
        await message.answer(f'❌ Не удалось взять набор: {e}')
        return
    await state.clear()
    await message.answer(
        f"✅ Набор <code>{name}</code> сохранён: {len(mapping)} иконок.\n"
        "Теперь включите «Иконки на кнопках» в «Вид главной страницы».",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[back_button('admin_main_view'), home_button()]]),
    )
