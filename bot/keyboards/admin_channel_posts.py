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


def channel_posts_queue_kb() -> InlineKeyboardMarkup:
    """Клавиатура экрана просмотра очереди публикаций."""
    builder = InlineKeyboardBuilder()
    builder.row(back_button('admin_channel_posts'), home_button())
    return builder.as_markup()
