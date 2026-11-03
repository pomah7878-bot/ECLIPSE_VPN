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


def build_favicons(static_dir: str) -> bool:
    """Иконка вкладки из logo.png: круглая, с прозрачными углами (без тёмного квадрата вокруг логотипа).
    Пересобирается, если логотип новее прошлой сборки. Возвращает True, если иконки готовы."""
    src = os.path.join(static_dir, "logo.png")
    marker = os.path.join(static_dir, ".favicon-round2")
    try:
        if not os.path.exists(src):
            return False
        if os.path.exists(marker) and os.path.getmtime(marker) >= os.path.getmtime(src):
            return True
        from PIL import Image, ImageDraw
        with Image.open(src) as img:
            img = img.convert("RGBA")
            # свечение и прозрачные поля вокруг логотипа обрезаем: иконка — по плотной части картинки
            box = img.getchannel("A").point(lambda a: 255 if a > 200 else 0).getbbox() or (0, 0, img.width, img.height)
            cx, cy = (box[0] + box[2]) / 2, (box[1] + box[3]) / 2
            half = min(max(box[2] - box[0], box[3] - box[1]) * 0.56, min(img.size) / 2)
            img = img.crop((int(cx - half), int(cy - half), int(cx + half), int(cy + half))).resize((256, 256), Image.LANCZOS)
        # маска круга рисуется крупно и уменьшается — ровный сглаженный край
        mask = Image.new("L", (1024, 1024), 0)
        ImageDraw.Draw(mask).ellipse((0, 0, 1023, 1023), fill=255)
        img.putalpha(mask.resize((256, 256), Image.LANCZOS))
        img.resize((32, 32), Image.LANCZOS).save(os.path.join(static_dir, "favicon-32x32.png"), format="PNG")
        img.resize((16, 16), Image.LANCZOS).save(os.path.join(static_dir, "favicon-16x16.png"), format="PNG")
        img.save(os.path.join(static_dir, "favicon.ico"), format="ICO", sizes=[(16, 16), (32, 32), (48, 48)])
        with open(marker, "w") as f:
            f.write("1")
        return True
    except Exception as e:
        logger.warning(f"Не удалось собрать иконку вкладки из логотипа: {e}")
        return False
