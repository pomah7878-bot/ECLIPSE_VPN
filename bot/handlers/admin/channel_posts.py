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
    channel_post_detail_kb,
    channel_post_delete_confirm_kb,
    channel_post_edit_cancel_kb,
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
    from database.requests import is_post_footer_enabled
    footer_enabled = is_post_footer_enabled()
    await safe_edit_or_send(
        callback.message,
        '📰 <b>Публикация в канал</b>\n\n'
        f'Текущий канал: {channel_text}\n\n'
        'Создать новый пост с датой и временем публикации, или посмотреть очередь уже запланированных.',
        reply_markup=channel_posts_menu_kb(footer_enabled),
    )
    await callback.answer()

@router.callback_query(F.data == 'admin_toggle_post_footer')
async def toggle_post_footer(callback: CallbackQuery, state: FSMContext):
    """Включает/выключает автоматическую рекламу бота в подвале постов канала."""
    if not is_admin(callback.from_user.id):
        await callback.answer('⛔ Доступ запрещён', show_alert=True)
        return

    from database.requests import is_post_footer_enabled, set_post_footer_enabled
    current = is_post_footer_enabled()
    set_post_footer_enabled(not current)

    await show_channel_posts_menu(callback, state)


@router.callback_query(F.data == 'admin_channel_settings')
async def start_channel_settings(callback: CallbackQuery, state: FSMContext):
    """Запрашивает username маркетингового канала."""
    if not is_admin(callback.from_user.id):
        await callback.answer('⛔ Доступ запрещён', show_alert=True)
        return
    await state.set_state(AdminStates.channel_settings_input)
    await safe_edit_or_send(
        callback.message,
        '⚙️ <b>Настройка канала</b>\n\n'
        'Отправьте username канала, куда бот будет публиковать посты (например, <code>@my_channel</code>).\n\n'
        'Бот должен быть добавлен администратором этого канала с правом «Публикация сообщений».',
        reply_markup=channel_post_cancel_kb(),
    )
    await callback.answer()


@router.message(AdminStates.channel_settings_input, F.text, ~F.text.startswith('/'))
async def process_channel_settings_input(message: Message, state: FSMContext):
    """Сохраняет username канала."""
    if not is_admin(message.from_user.id):
        return
    from database.requests import set_marketing_channel_id
    text = get_message_text_for_storage(message, 'plain').strip()
    if not text.startswith('@'):
        text = f'@{text}'
    set_marketing_channel_id(text)
    await state.clear()
    logger.info(f"Админ {message.from_user.id} настроил канал публикаций: {text}")
    await safe_edit_or_send(
        message,
        f'✅ Канал сохранён: {text}',
        reply_markup=channel_posts_menu_kb(),
        force_new=True,
    )


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
    from database.requests import is_post_footer_enabled
    text_with_footer = text + POST_FOOTER if is_post_footer_enabled() else text
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
    """Показывает список ещё не опубликованных постов — по кнопке на каждый."""
    if not is_admin(callback.from_user.id):
        await callback.answer('⛔ Доступ запрещён', show_alert=True)
        return
    from database.requests import get_pending_scheduled_posts_list
    raw_posts = get_pending_scheduled_posts_list(limit=15)

    if not raw_posts:
        text = '📋 <b>Очередь публикаций</b>\n\nПока нет запланированных постов.'
        buttons_data = []
    else:
        text = f'📋 <b>Очередь публикаций</b>\n\nВсего в очереди: {len(raw_posts)}. Нажмите на пост, чтобы посмотреть, заменить или удалить.'
        buttons_data = []
        for p in raw_posts:
            msk_time = (datetime.fromisoformat(p['scheduled_at']) + timedelta(hours=MSK_OFFSET_HOURS)).strftime('%d.%m %H:%M')
            preview = escape_html(p['content_preview'] or '')
            buttons_data.append({'id': p['id'], 'msk_time': msk_time, 'preview': preview})

    await safe_edit_or_send(callback.message, text, reply_markup=channel_posts_queue_kb(buttons_data))
    await callback.answer()


@router.callback_query(F.data.startswith('admin_channel_post_view:'))
async def show_post_detail(callback: CallbackQuery, state: FSMContext):
    """Полный предпросмотр одного запланированного поста."""
    if not is_admin(callback.from_user.id):
        await callback.answer('⛔ Доступ запрещён', show_alert=True)
        return
    post_id = int(callback.data.split(':')[1])
    from database.requests import get_scheduled_post_full
    post = get_scheduled_post_full(post_id)
    if not post or post['status'] != 'pending':
        await callback.answer('⚠️ Пост не найден или уже опубликован', show_alert=True)
        return

    msk_time = (datetime.fromisoformat(post['scheduled_at']) + timedelta(hours=MSK_OFFSET_HOURS)).strftime('%d.%m.%Y %H:%M')
    text = (
        f"👀 <b>Пост в очереди</b>\n\n"
        f"📅 Публикация: {msk_time} МСК\n"
        f"📢 Канал: {post['channel_id']}\n\n"
        f"—————————\n\n"
        f"{post['content']}"
    )
    await safe_edit_or_send(callback.message, text, reply_markup=channel_post_detail_kb(post_id))
    await callback.answer()


@router.callback_query(F.data.startswith('admin_channel_post_delete_ask:'))
async def ask_delete_post(callback: CallbackQuery, state: FSMContext):
    """Запрашивает подтверждение удаления."""
    if not is_admin(callback.from_user.id):
        await callback.answer('⛔ Доступ запрещён', show_alert=True)
        return
    post_id = int(callback.data.split(':')[1])
    await safe_edit_or_send(
        callback.message,
        '⚠️ Удалить этот пост из очереди? Отменить это действие будет нельзя.',
        reply_markup=channel_post_delete_confirm_kb(post_id),
    )
    await callback.answer()


@router.callback_query(F.data.startswith('admin_channel_post_delete_confirm:'))
async def confirm_delete_post(callback: CallbackQuery, state: FSMContext):
    """Удаляет пост из очереди."""
    if not is_admin(callback.from_user.id):
        await callback.answer('⛔ Доступ запрещён', show_alert=True)
        return
    post_id = int(callback.data.split(':')[1])
    from database.requests import delete_scheduled_post
    deleted = delete_scheduled_post(post_id)
    logger.info(f"Админ {callback.from_user.id} удалил пост #{post_id} из очереди: {deleted}")
    if deleted:
        await safe_edit_or_send(callback.message, '🗑️ Пост удалён из очереди.', reply_markup=channel_posts_menu_kb())
    else:
        await safe_edit_or_send(callback.message, '⚠️ Не удалось удалить — возможно, пост уже опубликован.', reply_markup=channel_posts_menu_kb())
    await callback.answer()


@router.callback_query(F.data.startswith('admin_channel_post_edit:'))
async def start_edit_post(callback: CallbackQuery, state: FSMContext):
    """Запрашивает новый текст взамен существующего поста."""
    if not is_admin(callback.from_user.id):
        await callback.answer('⛔ Доступ запрещён', show_alert=True)
        return
    post_id = int(callback.data.split(':')[1])
    await state.update_data(editing_post_id=post_id)
    await state.set_state(AdminStates.channel_post_edit_text)
    await safe_edit_or_send(
        callback.message,
        '✏️ <b>Новый текст поста</b>\n\n'
        'Отправьте новый текст — он полностью заменит текущий (дата и время публикации не изменятся). '
        'Ссылки на бота и магазин будут добавлены автоматически.',
        reply_markup=channel_post_edit_cancel_kb(post_id),
    )
    await callback.answer()


@router.message(AdminStates.channel_post_edit_text, F.text, ~F.text.startswith('/'))
async def process_edit_post_text(message: Message, state: FSMContext):
    """Сохраняет новый текст, заменяя содержимое поста."""
    if not is_admin(message.from_user.id):
        return
    data = await state.get_data()
    post_id = data.get('editing_post_id')
    if not post_id:
        await state.clear()
        return

    text = get_message_text_for_storage(message, 'html')
    from database.requests import is_post_footer_enabled
    text_with_footer = text + POST_FOOTER if is_post_footer_enabled() else text

    from database.requests import update_scheduled_post_content
    updated = update_scheduled_post_content(post_id, text_with_footer)
    await state.clear()
    logger.info(f"Админ {message.from_user.id} заменил текст поста #{post_id}: {updated}")

    if updated:
        await safe_edit_or_send(
            message,
            f'✅ Текст поста #{post_id} заменён.',
            reply_markup=channel_posts_menu_kb(),
            force_new=True,
        )
    else:
        await safe_edit_or_send(
            message,
            '⚠️ Не удалось заменить — возможно, пост уже опубликован.',
            reply_markup=channel_posts_menu_kb(),
            force_new=True,
        )
