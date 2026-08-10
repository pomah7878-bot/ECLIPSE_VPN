"""
Публикация постов в маркетинговый Telegram-канал — с указанием даты и
времени и превью перед подтверждением. Сами публикации выполняет
встроенный планировщик (bot/services/scheduler.py -> run_channel_posts_scheduler),
эти обработчики только создают записи в очереди.
"""
import logging
from datetime import datetime, timedelta

from aiogram import Router, F
from aiogram.types import CallbackQuery, Message
from aiogram.fsm.context import FSMContext

from bot.utils.admin import is_admin
from bot.utils.text import safe_edit_or_send, escape_html, get_message_text_for_storage
from bot.states.admin_states import AdminStates
from bot.keyboards.admin_channel_posts import (
    channel_posts_menu_kb,
    channel_post_cancel_kb,
    channel_post_preview_kb,
    channel_posts_queue_kb,
)

logger = logging.getLogger(__name__)
router = Router()

MSK_OFFSET_HOURS = 3

# Автоматически добавляется в конец КАЖДОГО поста, созданного через это меню
# — чтобы не приходилось вручную набирать ссылки в каждом посте.
POST_FOOTER = (
    "\n\n━━━━━━━━━━━━━━\n"
    "🤖 Подключиться в боте: <a href=\"https://t.me/vless_keysvpn_bot\">@vless_keysvpn_bot</a>\n"
    "🛒 Купить на сайте: <a href=\"https://eclipse.unlimited.bot.nu/shop\">eclipse.unlimited.bot.nu</a>"
)


@router.callback_query(F.data == 'admin_channel_posts')
async def show_channel_posts_menu(callback: CallbackQuery, state: FSMContext):
    """Главное меню раздела публикации постов в канал."""
    if not is_admin(callback.from_user.id):
        await callback.answer('⛔ Доступ запрещён', show_alert=True)
        return
    await state.clear()
    from database.requests import get_marketing_channel_id
    channel_id = get_marketing_channel_id()
    channel_text = channel_id if channel_id else '⚠️ не настроен'
    await safe_edit_or_send(
        callback.message,
        '📰 <b>Публикация в канал</b>\n\n'
        f'Текущий канал: {channel_text}\n\n'
        'Создать новый пост с датой и временем публикации, или посмотреть очередь уже запланированных.',
        reply_markup=channel_posts_menu_kb(),
    )
    await callback.answer()


@router.callback_query(F.data == 'admin_channel_post_new')
async def start_channel_post_new(callback: CallbackQuery, state: FSMContext):
    """Начало создания нового поста — запрос текста."""
    if not is_admin(callback.from_user.id):
        await callback.answer('⛔ Доступ запрещён', show_alert=True)
        return
    from database.requests import get_marketing_channel_id
    if not get_marketing_channel_id():
        await callback.answer(
            '⚠️ Сначала настройте канал (кнопка «⚙️ Настроить канал» в меню)',
            show_alert=True,
        )
        return
    await state.set_state(AdminStates.channel_post_text)
    await safe_edit_or_send(
        callback.message,
        '✏️ <b>Текст поста</b>\n\n'
        'Отправьте текст поста (поддерживается HTML-разметка: <b>жирный</b>, <i>курсив</i> и т.д.).',
        reply_markup=channel_post_cancel_kb(),
    )
    await callback.answer()


@router.message(AdminStates.channel_post_text, F.text, ~F.text.startswith('/'))
async def process_channel_post_text(message: Message, state: FSMContext):
    """Сохраняет текст поста, запрашивает дату публикации."""
    if not is_admin(message.from_user.id):
        return
    text = get_message_text_for_storage(message, 'html')
    text_with_footer = text + POST_FOOTER
    await state.update_data(post_text=text_with_footer)
    await state.set_state(AdminStates.channel_post_date)
    await safe_edit_or_send(
        message,
        '📅 <b>Дата публикации</b>\n\nВведите дату в формате ДД.ММ.ГГГГ (например, 15.08.2026).',
        reply_markup=channel_post_cancel_kb(),
        force_new=True,
    )


@router.message(AdminStates.channel_post_date, F.text, ~F.text.startswith('/'))
async def process_channel_post_date(message: Message, state: FSMContext):
    """Парсит дату публикации, запрашивает время."""
    if not is_admin(message.from_user.id):
        return
    text = get_message_text_for_storage(message, 'plain').strip()
    try:
        parsed_date = datetime.strptime(text, '%d.%m.%Y').date()
    except ValueError:
        await safe_edit_or_send(
            message,
            '❌ Неверный формат. Введите дату как ДД.ММ.ГГГГ, например 15.08.2026',
            reply_markup=channel_post_cancel_kb(),
            force_new=True,
        )
        return
    await state.update_data(post_date=parsed_date.isoformat())
    await state.set_state(AdminStates.channel_post_time)
    await safe_edit_or_send(
        message,
        '🕐 <b>Время публикации (по Москве)</b>\n\nВведите время в формате ЧЧ:ММ (например, 14:00).',
        reply_markup=channel_post_cancel_kb(),
        force_new=True,
    )


@router.message(AdminStates.channel_post_time, F.text, ~F.text.startswith('/'))
async def process_channel_post_time(message: Message, state: FSMContext):
    """Парсит время публикации, собирает итоговую дату/время и показывает превью."""
    if not is_admin(message.from_user.id):
        return
    text = get_message_text_for_storage(message, 'plain').strip()
    try:
        parsed_time = datetime.strptime(text, '%H:%M').time()
    except ValueError:
        await safe_edit_or_send(
            message,
            '❌ Неверный формат. Введите время как ЧЧ:ММ, например 14:00',
            reply_markup=channel_post_cancel_kb(),
            force_new=True,
        )
        return

    data = await state.get_data()
    post_date = datetime.fromisoformat(data['post_date']).date()
    msk_dt = datetime.combine(post_date, parsed_time)
    utc_dt = msk_dt - timedelta(hours=MSK_OFFSET_HOURS)
    scheduled_at = utc_dt.strftime('%Y-%m-%d %H:%M:%S')
    msk_display = msk_dt.strftime('%d.%m.%Y %H:%M')

    await state.update_data(scheduled_at=scheduled_at, msk_display=msk_display)
    await state.set_state(AdminStates.channel_post_preview)

    from database.requests import get_marketing_channel_id
    channel_id = get_marketing_channel_id() or '—'
    post_text = data['post_text']
    preview_msg = (
        f"👀 <b>Превью поста</b>\n\n"
        f"📅 Публикация: {msk_display} МСК\n"
        f"📢 Канал: {channel_id}\n\n"
        f"—————————\n\n"
        f"{post_text}"
    )
    await safe_edit_or_send(message, preview_msg, reply_markup=channel_post_preview_kb(), force_new=True)


@router.callback_query(AdminStates.channel_post_preview, F.data == 'admin_channel_post_confirm')
async def confirm_channel_post(callback: CallbackQuery, state: FSMContext):
    """Подтверждает и сохраняет пост в очередь публикации."""
    if not is_admin(callback.from_user.id):
        await callback.answer('⛔ Доступ запрещён', show_alert=True)
        return
    data = await state.get_data()
    from database.requests import create_scheduled_post, get_marketing_channel_id
    channel_id = get_marketing_channel_id()
    if not channel_id:
        await callback.answer('⚠️ Канал не настроен, публикация отменена', show_alert=True)
        await state.clear()
        return
    post_id = create_scheduled_post(channel_id, data['post_text'], data['scheduled_at'])
    await state.clear()
    logger.info(f"Админ {callback.from_user.id} запланировал пост #{post_id} на {data['msk_display']} МСК")
    await safe_edit_or_send(
        callback.message,
        f"✅ Пост #{post_id} запланирован на {data['msk_display']} МСК.",
        reply_markup=channel_posts_menu_kb(),
    )
    await callback.answer()


@router.callback_query(F.data == 'admin_channel_posts_queue')
async def show_channel_posts_queue(callback: CallbackQuery, state: FSMContext):
    """Показывает список последних запланированных/опубликованных постов."""
    if not is_admin(callback.from_user.id):
        await callback.answer('⛔ Доступ запрещён', show_alert=True)
        return
    from database.requests import get_all_scheduled_posts
    posts = get_all_scheduled_posts(limit=15)
    if not posts:
        text = '📋 <b>Очередь публикаций</b>\n\nПока нет запланированных постов.'
    else:
        lines = ['📋 <b>Очередь публикаций</b>\n']
        status_emoji = {'pending': '🕐', 'sent': '✅', 'failed': '❌'}
        for p in posts:
            emoji = status_emoji.get(p['status'], '❓')
            msk_time = (datetime.fromisoformat(p['scheduled_at']) + timedelta(hours=MSK_OFFSET_HOURS)).strftime('%d.%m %H:%M')
            preview = escape_html(p['content_preview'] or '')
            lines.append(f"{emoji} <code>{msk_time}</code> — {preview}...")
        text = '\n'.join(lines)
    await safe_edit_or_send(callback.message, text, reply_markup=channel_posts_queue_kb())
    await callback.answer()
