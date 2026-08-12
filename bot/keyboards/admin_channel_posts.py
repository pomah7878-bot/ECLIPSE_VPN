"""Клавиатуры для раздела публикации постов в маркетинговый канал."""
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from aiogram.utils.keyboard import InlineKeyboardBuilder
from bot.keyboards.admin_misc import back_button, home_button


def channel_posts_menu_kb() -> InlineKeyboardMarkup:
    """Главное меню раздела публикации постов в канал."""
    builder = InlineKeyboardBuilder()
    builder.row(InlineKeyboardButton(text='➕ Создать пост', callback_data='admin_channel_post_new'))
    builder.row(InlineKeyboardButton(text='📋 Очередь публикаций', callback_data='admin_channel_posts_queue'))
    builder.row(InlineKeyboardButton(text='⚙️ Настроить канал', callback_data='admin_channel_settings'))
    builder.row(back_button('admin_marketing'), home_button())
    return builder.as_markup()


def channel_post_cancel_kb() -> InlineKeyboardMarkup:
    """Клавиатура отмены на промежуточных шагах ввода."""
    builder = InlineKeyboardBuilder()
    builder.row(InlineKeyboardButton(text='❌ Отмена', callback_data='admin_channel_posts'))
    return builder.as_markup()


def channel_post_preview_kb() -> InlineKeyboardMarkup:
    """Клавиатура подтверждения на экране превью поста."""
    builder = InlineKeyboardBuilder()
    builder.row(InlineKeyboardButton(text='✅ Опубликовать', callback_data='admin_channel_post_confirm'))
    builder.row(InlineKeyboardButton(text='❌ Отмена', callback_data='admin_channel_posts'))
    return builder.as_markup()


def channel_posts_queue_kb(pending_posts: list) -> InlineKeyboardMarkup:
    """Список запланированных постов — по кнопке на каждый, для управления."""
    builder = InlineKeyboardBuilder()
    for post in pending_posts:
        builder.row(InlineKeyboardButton(
            text=f"🕐 {post['msk_time']} — {post['preview']}",
            callback_data=f"admin_channel_post_view:{post['id']}",
        ))
    builder.row(back_button('admin_channel_posts'), home_button())
    return builder.as_markup()


def channel_post_detail_kb(post_id: int) -> InlineKeyboardMarkup:
    """Клавиатура детального просмотра одного поста — удалить/заменить/назад."""
    builder = InlineKeyboardBuilder()
    builder.row(InlineKeyboardButton(text='✏️ Заменить текст', callback_data=f'admin_channel_post_edit:{post_id}'))
    builder.row(InlineKeyboardButton(text='🗑️ Удалить пост', callback_data=f'admin_channel_post_delete_ask:{post_id}'))
    builder.row(back_button('admin_channel_posts_queue'), home_button())
    return builder.as_markup()


def channel_post_delete_confirm_kb(post_id: int) -> InlineKeyboardMarkup:
    """Подтверждение удаления поста."""
    builder = InlineKeyboardBuilder()
    builder.row(InlineKeyboardButton(text='✅ Да, удалить', callback_data=f'admin_channel_post_delete_confirm:{post_id}'))
    builder.row(InlineKeyboardButton(text='❌ Отмена', callback_data=f'admin_channel_post_view:{post_id}'))
    return builder.as_markup()


def channel_post_edit_cancel_kb(post_id: int) -> InlineKeyboardMarkup:
    """Отмена на шаге ввода нового текста при замене."""
    builder = InlineKeyboardBuilder()
    builder.row(InlineKeyboardButton(text='❌ Отмена', callback_data=f'admin_channel_post_view:{post_id}'))
    return builder.as_markup()
