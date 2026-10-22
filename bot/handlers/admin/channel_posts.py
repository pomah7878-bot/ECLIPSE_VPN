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
POST_TEXT_LIMIT = 4096
# Подпись к фото/видео/GIF в Telegram — 1024 символа; 24 оставлены под строку с датой в превью.
POST_CAPTION_LIMIT = 1000
MEDIA_ICONS = {'photo': '🖼', 'video': '🎬', 'animation': '🎞'}


def _extract_media(message: Message):
    """(тип, file_id) фото, видео или GIF из сообщения админа; иначе (None, None)."""
    if message.photo:
        return 'photo', message.photo[-1].file_id
    if message.video:
        return 'video', message.video.file_id
    if message.animation:
        return 'animation', message.animation.file_id
    return None, None


async def _compose_post(message: Message, has_media: bool):
    """Текст сообщения админа → HTML поста с подвалом. Возвращает (текст, ошибка)."""
    from bot.utils.post_format import convert_post_text
    from database.requests import is_post_footer_enabled
    text = convert_post_text(message)
    if is_post_footer_enabled():
        text = (text + (await _build_post_footer(message.bot))).strip()
    limit = POST_CAPTION_LIMIT if has_media else POST_TEXT_LIMIT
    n = _visible_len(text)
    if n > limit:
        what = 'Подпись к медиа' if has_media else 'Пост'
        return None, (
            f'❌ {what} слишком длинн{"ая" if has_media else "ый"}: {n} символов из {limit} (с учётом подвала). '
            f'Сократите текст минимум на {n - limit} и отправьте снова. '
            'Иначе Telegram обрежет конец вместе со ссылками.'
        )
    return text, None


def _visible_len(html_text: str) -> int:
    """Длина поста глазами Telegram: без тегов, с раскрытыми HTML-сущностями"""
    import html as _html
    import re as _re
    return len(_html.unescape(_re.sub(r"<[^>]+>", "", html_text or "")))

# Автоматически добавляется в конец КАЖДОГО поста, созданного через это меню
# — чтобы не приходилось вручную набирать ссылки в каждом посте.
#
# Юзернейм бота и ссылка на магазин НЕ хардкодятся — у каждой инсталляции
# бота свои значения (bot.get_me() и настраиваемый webapp_url), иначе
# посты в канале white-label клиента вели бы на чужого бота и чужой сайт.
async def _build_post_footer(bot) -> str:
    from database.requests import get_effective_webapp_url

    bot_username = (await bot.get_me()).username
    footer = (
        "\n\n━━━━━━━━━━━━━━\n"
        f"🤖 Подключиться в боте: <a href=\"https://t.me/{bot_username}\">@{bot_username}</a>"
    )
    webapp_url = get_effective_webapp_url()
    if webapp_url:
        shop_url = webapp_url.rstrip('/') + '/shop'
        shop_display = webapp_url.replace('https://', '').replace('http://', '').rstrip('/')
        footer += f"\n🛒 Купить на сайте: <a href=\"{shop_url}\">{shop_display}</a>"
    return footer


async def _step(message: Message, state: FSMContext, text: str, reply_markup=None) -> None:
    """Один диалог — одно сообщение бота.

    Сообщение пользователя удаляется, а подсказка бота (id в FSM: cp_msg)
    редактируется на месте. Так в чате не остаются «Дата публикации»,
    «Время публикации» и кнопки «Отмена» после завершения.
    """
    from aiogram.exceptions import TelegramBadRequest
    from aiogram.types import LinkPreviewOptions
    try:
        await message.delete()
    except Exception:
        pass
    data = await state.get_data()
    mid = data.get('cp_msg')
    if mid:
        try:
            await message.bot.edit_message_text(
                text=text, chat_id=message.chat.id, message_id=mid,
                reply_markup=reply_markup, parse_mode='HTML',
                link_preview_options=LinkPreviewOptions(is_disabled=True),
            )
            return
        except TelegramBadRequest as e:
            if 'message is not modified' in str(e).lower():
                return
        except Exception:
            pass
    sent = await message.answer(
        text, reply_markup=reply_markup, parse_mode='HTML',
        link_preview_options=LinkPreviewOptions(is_disabled=True),
    )
    await state.update_data(cp_msg=sent.message_id)


@router.callback_query(F.data == 'admin_channel_posts')
async def show_channel_posts_menu(callback: CallbackQuery, state: FSMContext):
    """Главное меню раздела публикации постов в канал."""
    if not is_admin(callback.from_user.id):
        await callback.answer('⛔ Доступ запрещён', show_alert=True)
        return
    from bot.services.license import is_feature_available
    if not is_feature_available("channel_posts"):
        await callback.answer("Эта функция доступна на вашем тарифе", show_alert=True)
        return
    await state.clear()
    from database.requests import get_marketing_channel_id
    channel_id = get_marketing_channel_id()
    channel_text = channel_id if channel_id else '⚠️ не настроен'
    from database.requests import is_post_footer_enabled
    footer_enabled = is_post_footer_enabled()

    footer_explanation = (
        '📌 <b>Что такое «Реклама бота в подвале»?</b>\n\n'
        'Если включено, в конец каждого поста из этого раздела автоматически добавляются две строки:\n'
        '• ссылка на бота — читатель сразу переходит в него из поста\n'
        '• ссылка на сайт-магазин — если он настроен\n\n'
        'Так не нужно вставлять ссылки вручную в каждый пост.\n\n'
    )
    if footer_enabled:
        footer_preview = await _build_post_footer(callback.message.bot)
        footer_explanation += f'Сейчас включено, вот как выглядит подвал прямо сейчас:{footer_preview}\n\n'
    else:
        footer_explanation += 'Сейчас выключено — посты публикуются без этих строк.\n\n'

    await safe_edit_or_send(
        callback.message,
        '📰 <b>Публикация в канал</b>\n\n'
        f'Текущий канал: {channel_text}\n\n'
        f'{footer_explanation}'
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

    try:
        await message.delete()
    except Exception:
        pass
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
    await state.update_data(cp_msg=callback.message.message_id)
    await safe_edit_or_send(
        callback.message,
        '✏️ <b>Текст поста</b>\n\n'
        'Отправьте текст поста в любом формате: форматирование Telegram, HTML-теги или Markdown (**жирный**, > цитата, [текст](ссылка)). '
        'Бот сам приведёт его к нужному виду.\n\n'
        '🖼 Чтобы добавить фото, видео или GIF, отправьте его сразу с текстом в подписи (до 1000 символов).',
        reply_markup=channel_post_cancel_kb(),
    )
    await callback.answer()


async def _accept_post_text(message: Message, state: FSMContext, media_type, media_id) -> None:
    text, error = await _compose_post(message, bool(media_id))
    if error:
        await _step(message, state, error, channel_post_cancel_kb())
        return
    await state.update_data(post_text=text, post_media_type=media_type, post_media_id=media_id)
    await state.set_state(AdminStates.channel_post_date)
    await _step(
        message, state,
        '📅 <b>Дата публикации</b>\n\nВведите дату в формате ДД.ММ.ГГГГ (например, 15.08.2026).',
        channel_post_cancel_kb(),
    )


@router.message(AdminStates.channel_post_text, F.text, ~F.text.startswith('/'))
async def process_channel_post_text(message: Message, state: FSMContext):
    """Сохраняет текст поста, запрашивает дату публикации."""
    if not is_admin(message.from_user.id):
        return
    await _accept_post_text(message, state, None, None)


@router.message(AdminStates.channel_post_text, F.photo | F.video | F.animation)
async def process_channel_post_media(message: Message, state: FSMContext):
    """Фото/видео/GIF с подписью: подпись станет текстом поста."""
    if not is_admin(message.from_user.id):
        return
    media_type, media_id = _extract_media(message)
    await _accept_post_text(message, state, media_type, media_id)


@router.message(AdminStates.channel_post_date, F.text, ~F.text.startswith('/'))
async def process_channel_post_date(message: Message, state: FSMContext):
    """Парсит дату публикации, запрашивает время."""
    if not is_admin(message.from_user.id):
        return
    text = get_message_text_for_storage(message, 'plain').strip()
    try:
        parsed_date = datetime.strptime(text, '%d.%m.%Y').date()
    except ValueError:
        await _step(
            message, state,
            '❌ Неверный формат. Введите дату как ДД.ММ.ГГГГ, например 15.08.2026',
            channel_post_cancel_kb(),
        )
        return
    await state.update_data(post_date=parsed_date.isoformat())
    await state.set_state(AdminStates.channel_post_time)
    await _step(
        message, state,
        '🕐 <b>Время публикации (по Москве)</b>\n\nВведите время в формате ЧЧ:ММ (например, 14:00).',
        channel_post_cancel_kb(),
    )


async def _show_media_preview(message: Message, state: FSMContext, caption: str, data: dict) -> None:
    """Превью поста с медиа: само медиа с подписью и кнопками подтверждения."""
    for mid in (message.message_id, data.get('cp_msg')):
        if mid:
            try:
                await message.bot.delete_message(message.chat.id, mid)
            except Exception:
                pass
    sent = await safe_edit_or_send(
        message, caption, reply_markup=channel_post_preview_kb(),
        media=data['post_media_id'], media_type=data.get('post_media_type'), force_new=True,
    )
    await state.update_data(cp_msg=sent.message_id)


@router.message(AdminStates.channel_post_time, F.text, ~F.text.startswith('/'))
async def process_channel_post_time(message: Message, state: FSMContext):
    """Парсит время публикации, собирает итоговую дату/время и показывает превью."""
    if not is_admin(message.from_user.id):
        return
    text = get_message_text_for_storage(message, 'plain').strip()
    try:
        parsed_time = datetime.strptime(text, '%H:%M').time()
    except ValueError:
        await _step(
            message, state,
            '❌ Неверный формат. Введите время как ЧЧ:ММ, например 14:00',
            channel_post_cancel_kb(),
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
    if data.get('post_media_id'):
        await _show_media_preview(message, state, f"📅 {msk_display} МСК\n\n{post_text}", data)
        return
    preview_msg = (
        f"👀 <b>Превью поста</b>\n\n"
        f"📅 Публикация: {msk_display} МСК\n"
        f"📢 Канал: {channel_id}\n\n"
        f"—————————\n\n"
        f"{post_text}"
    )
    await _step(message, state, preview_msg, channel_post_preview_kb())


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
    post_id = create_scheduled_post(
        channel_id, data['post_text'], data['scheduled_at'],
        data.get('post_media_type'), data.get('post_media_id'),
    )
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
            preview = MEDIA_ICONS.get(p.get('media_type'), '') + ' ' + escape_html(p['content_preview'] or '') if p.get('media_type') else escape_html(p['content_preview'] or '')
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
    if post.get('media_file_id'):
        await safe_edit_or_send(
            callback.message, f"📅 {msk_time} МСК\n\n{post['content']}",
            reply_markup=channel_post_detail_kb(post_id),
            media=post['media_file_id'], media_type=post.get('media_type'),
        )
        await callback.answer()
        return
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
    sent = await safe_edit_or_send(
        callback.message,
        '✏️ <b>Новый текст поста</b>\n\n'
        'Отправьте новый текст — он полностью заменит текущий (дата и время публикации не изменятся). '
        'Ссылки на бота и магазин будут добавлены автоматически.\n\n'
        '🖼 Отправьте фото, видео или GIF с подписью, чтобы заменить и медиа.',
        reply_markup=channel_post_edit_cancel_kb(post_id),
    )
    await state.update_data(cp_msg=getattr(sent, 'message_id', callback.message.message_id))
    await callback.answer()


async def _apply_post_edit(message: Message, state: FSMContext, media_type, media_id) -> None:
    data = await state.get_data()
    post_id = data.get('editing_post_id')
    if not post_id:
        await state.clear()
        return
    from database.requests import get_scheduled_post_full, update_scheduled_post_content
    post = get_scheduled_post_full(post_id) or {}
    has_media = bool(media_id or post.get('media_file_id'))
    text, error = await _compose_post(message, has_media)
    if error:
        await _step(message, state, error, channel_post_edit_cancel_kb(post_id))
        return
    updated = update_scheduled_post_content(post_id, text, media_type, media_id)
    logger.info(f"Админ {message.from_user.id} заменил пост #{post_id}: {updated}")
    await _step(
        message, state,
        f'✅ Пост #{post_id} заменён.' if updated
        else '⚠️ Не удалось заменить — возможно, пост уже опубликован.',
        channel_posts_menu_kb(),
    )
    await state.clear()


@router.message(AdminStates.channel_post_edit_text, F.text, ~F.text.startswith('/'))
async def process_edit_post_text(message: Message, state: FSMContext):
    """Сохраняет новый текст, заменяя содержимое поста (медиа остаётся прежним)."""
    if not is_admin(message.from_user.id):
        return
    await _apply_post_edit(message, state, None, None)


@router.message(AdminStates.channel_post_edit_text, F.photo | F.video | F.animation)
async def process_edit_post_media(message: Message, state: FSMContext):
    """Новое медиа с подписью заменяет и медиа, и текст поста."""
    if not is_admin(message.from_user.id):
        return
    media_type, media_id = _extract_media(message)
    await _apply_post_edit(message, state, media_type, media_id)


@router.message(AdminStates.channel_post_text, ~F.text)
@router.message(AdminStates.channel_post_edit_text, ~F.text)
async def channel_post_non_text(message: Message, state: FSMContext):
    """Фото, видео, пересланные медиа и т.п. сюда не подходят — раньше бот молча игнорировал."""
    if not is_admin(message.from_user.id):
        return
    data = await state.get_data()
    pid = data.get('editing_post_id')
    kb = channel_post_edit_cancel_kb(pid) if pid and await state.get_state() == AdminStates.channel_post_edit_text.state else channel_post_cancel_kb()
    await _step(
        message, state,
        '⚠️ Такой тип сообщения не подходит. Отправьте текст или фото, видео, GIF '
        'с текстом в подписи (форматирование Telegram и HTML-теги сохранятся).',
        kb,
    )
