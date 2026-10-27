"""Лёгкая копия логотипа для сайта: logo-web.png (256×256, ~10 КБ вместо сотен КБ).

Мобильные операторы обрывают крупные загрузки, поэтому страницы грузят её, а не logo.png
(тот остаётся полноразмерным — его использует бот).
"""
import logging
import os

logger = logging.getLogger(__name__)

WEB_LOGO_SIZE = 256


def build_web_logo(static_dir: str) -> bool:
    """Создаёт logo-web.png из logo.png, если её нет или logo.png новее. Возвращает True, если файл готов."""
    src = os.path.join(static_dir, "logo.png")
    dst = os.path.join(static_dir, "logo-web.png")
    try:
        if not os.path.exists(src):
            return False
        if os.path.exists(dst) and os.path.getmtime(dst) >= os.path.getmtime(src):
            return True
        from PIL import Image
        with Image.open(src) as img:
            img = img.convert("RGBA")
            img.thumbnail((WEB_LOGO_SIZE, WEB_LOGO_SIZE), Image.LANCZOS)
            img.quantize(256, method=Image.FASTOCTREE).save(dst + ".tmp", format="PNG", optimize=True)
        os.replace(dst + ".tmp", dst)
        return True
    except Exception as e:
        logger.warning(f"Не удалось подготовить лёгкий логотип для сайта: {e}")
        return False
