"""Генератор простого столбчатого графика дневного расхода трафика
по ключу — через Pillow, без внешних зависимостей типа matplotlib."""
import io
from typing import Any

from PIL import Image, ImageDraw, ImageFont

_FONT_REGULAR = "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"
_FONT_BOLD = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"


def generate_traffic_chart(history: list[dict[str, Any]], key_name: str) -> bytes:
    """Строит график дневного расхода трафика по ключу.

    Args:
        history: список {snapshot_date, traffic_used_bytes} (кумулятивные
            значения), отсортированный по дате по возрастанию.
        key_name: отображаемое имя ключа для заголовка.

    Returns:
        PNG-изображение в виде bytes.
    """
    W, H = 700, 400
    PADDING_L, PADDING_R, PADDING_T, PADDING_B = 60, 30, 50, 60

    img = Image.new('RGB', (W, H), color=(24, 24, 28))
    draw = ImageDraw.Draw(img)

    try:
        font = ImageFont.truetype(_FONT_REGULAR, 14)
        font_small = ImageFont.truetype(_FONT_REGULAR, 11)
        font_title = ImageFont.truetype(_FONT_BOLD, 16)
    except Exception:
        font = ImageFont.load_default()
        font_small = font
        font_title = font

    draw.text((PADDING_L, 15), f"Трафик за день — {key_name}", font=font_title, fill=(230, 230, 235))

    # Считаем ДНЕВНОЙ расход (разница между соседними кумулятивными снапшотами)
    daily = []
    prev = None
    for point in history:
        used = point['traffic_used_bytes']
        if prev is None:
            delta = 0
        else:
            delta = max(0, used - prev)
        daily.append((point['snapshot_date'], delta))
        prev = used

    if len(daily) < 2:
        draw.text(
            (PADDING_L, H // 2),
            "Пока недостаточно данных для графика (нужно минимум 2 дня)",
            font=font, fill=(150, 150, 155),
        )
        buf = io.BytesIO()
        img.save(buf, format='PNG')
        return buf.getvalue()

    chart_w = W - PADDING_L - PADDING_R
    chart_h = H - PADDING_T - PADDING_B
    max_val = max(v for _, v in daily) or 1

    bar_gap = 8
    bar_w = max(10, (chart_w - bar_gap * (len(daily) - 1)) // len(daily))

    draw.line([(PADDING_L, PADDING_T), (PADDING_L, PADDING_T + chart_h)], fill=(80, 80, 88), width=1)
    draw.line([(PADDING_L, PADDING_T + chart_h), (PADDING_L + chart_w, PADDING_T + chart_h)], fill=(80, 80, 88), width=1)

    x = PADDING_L
    for date_str, val in daily:
        bar_h = int((val / max_val) * chart_h) if max_val else 0
        y_top = PADDING_T + chart_h - bar_h
        draw.rectangle([x, y_top, x + bar_w, PADDING_T + chart_h], fill=(45, 200, 180))

        gb = val / (1024 ** 3)
        label = f"{gb:.1f}" if gb >= 0.1 else ""
        if label:
            draw.text((x, y_top - 16), label, font=font_small, fill=(200, 200, 205))

        short_date = date_str[5:]  # MM-DD
        draw.text((x, PADDING_T + chart_h + 8), short_date, font=font_small, fill=(150, 150, 155))
        x += bar_w + bar_gap

    draw.text((PADDING_L, PADDING_T + chart_h + 30), "ГБ в день", font=font_small, fill=(120, 120, 125))

    buf = io.BytesIO()
    img.save(buf, format='PNG')
    return buf.getvalue()
