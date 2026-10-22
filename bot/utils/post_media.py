"""Публикация поста «фото + длинный текст» одним сообщением.

Подпись к фото ограничена 1024 символами, поэтому длинный текст отправляется
обычным сообщением (до 4096 символов), а фото показывается превью над текстом.
Фото берётся из Telegram и кладётся в bot/webapp/static/posts/ — эту папку сайт
бота уже раздаёт по {адрес сайта}/static/posts/.
"""
import logging
import os
from typing import Optional

import aiohttp
from aiogram.types import LinkPreviewOptions

logger = logging.getLogger(__name__)

POSTS_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "webapp", "static", "posts")


async def _url_is_image(url: str) -> bool:
    """Проверяет, что по ссылке реально отдаётся картинка (иначе превью не появится)."""
    try:
        timeout = aiohttp.ClientTimeout(total=10)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(url) as resp:
                ctype = resp.headers.get("Content-Type", "")
                return resp.status == 200 and ctype.startswith("image/")
    except Exception as e:
        logger.warning("Проверка картинки поста по ссылке %s не прошла: %s", url, e)
        return False


async def publish_photo_with_long_text(bot, channel_id, post_id: int, file_id: str, text: str):
    """Отправляет текст с фото-превью сверху. Возвращает сообщение или None.

    None означает «не вышло»: вызывающий код публикует фото и текст отдельно.
    """
    try:
        from database.requests import get_effective_webapp_url

        base = (get_effective_webapp_url() or "").rstrip("/")
        if not base.startswith("https://"):
            return None

        os.makedirs(POSTS_DIR, exist_ok=True)
        name = f"post_{int(post_id)}.jpg"
        path = os.path.join(POSTS_DIR, name)
        if not os.path.exists(path):
            tg_file = await bot.get_file(file_id)
            await bot.download_file(tg_file.file_path, destination=path)

        url = f"{base}/static/posts/{name}"
        if not await _url_is_image(url):
            return None

        return await bot.send_message(
            channel_id,
            text,
            parse_mode="HTML",
            link_preview_options=LinkPreviewOptions(
                url=url, show_above_text=True, prefer_large_media=True,
            ),
        )
    except Exception as e:
        logger.warning(
            "Пост #%s: одним сообщением не вышло, отправляю фото и текст отдельно: %s", post_id, e
        )
        return None
