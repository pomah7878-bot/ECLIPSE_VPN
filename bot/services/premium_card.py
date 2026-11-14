"""Премиальная карточка главного экрана — PNG с золотыми иконками.

Одинаково выглядит в Telegram на Android, iOS и Desktop: фон и цвета
не зависят от темы пользователя, в отличие от HTML-карточки.
"""
from __future__ import annotations

import html
from io import BytesIO
from pathlib import Path
from typing import Any, Dict, Optional

from PIL import Image, ImageDraw, ImageFilter, ImageFont

ASSETS = Path(__file__).resolve().parents[1] / "assets" / "premium_card"
FONT_REG = "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"
FONT_BOLD = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"

GOLD = (214, 172, 74, 255)
GOLD_LIGHT = (247, 210, 118, 255)
GOLD_DIM = (166, 130, 60, 255)
WHITE = (247, 246, 250, 255)
MUTED = (170, 168, 184, 255)
PANEL = (29, 27, 38, 225)
PANEL_EDGE = (92, 77, 45, 170)
BG_TOP = (10, 10, 17)
BG_BOTTOM = (24, 20, 30)


def _font(path: str, size: int) -> ImageFont.FreeTypeFont:
    try:
        return ImageFont.truetype(path, size)
    except Exception:  # noqa: BLE001
        return ImageFont.load_default()


def _sprite(name: str, size: int) -> Optional[Image.Image]:
    path = ASSETS / f"{name}.png"
    if not path.exists():
        return None
    return Image.open(path).convert("RGBA").resize((size, size), Image.LANCZOS)


def _gradient(w: int, h: int) -> Image.Image:
    img = Image.new("RGB", (w, h), BG_TOP)
    d = ImageDraw.Draw(img)
    for y in range(h):
        t = y / max(1, h - 1)
        r = int(BG_TOP[0] + (BG_BOTTOM[0] - BG_TOP[0]) * t)
        g = int(BG_TOP[1] + (BG_BOTTOM[1] - BG_TOP[1]) * t)
        b = int(BG_TOP[2] + (BG_BOTTOM[2] - BG_TOP[2]) * t)
        d.line((0, y, w, y), fill=(r, g, b))
    return img


def _spaced(draw: ImageDraw.ImageDraw, xy, text: str, font, fill, spacing: int) -> None:
    x, y = xy
    for ch in text:
        draw.text((x, y), ch, font=font, fill=fill)
        x += draw.textlength(ch, font=font) + spacing


def _row(draw, img, y: int, icon: str, label: str, value: str) -> None:
    x0, x1 = 80, 1120
    h = 72
    draw.rounded_rectangle((x0, y, x1, y + h), radius=20, fill=PANEL, outline=PANEL_EDGE, width=1)
    ic = _sprite(icon, 42)
    if ic:
        img.alpha_composite(ic, (x0 + 22, y + (h - 42) // 2))
    draw.text((x0 + 84, y + 18), label, font=_font(FONT_REG, 27), fill=MUTED)
    f = _font(FONT_BOLD, 34)
    tw = draw.textlength(value, font=f)
    draw.text((x1 - 26 - tw, y + 14), value, font=f, fill=WHITE)


def build_premium_card(state: Dict[str, Any], first_name: str) -> Optional[bytes]:
    """Возвращает PNG-карточку или None для новичка без подписки."""
    from bot.services.main_screen import (
        AUDIENCE_CLIENT,
        AUDIENCE_EXPIRED,
        _days_left,
        _max_devices,
        _plan_name,
        _rub,
        format_day_month,
        _plural,
    )

    audience = state.get("audience")
    balance = int(state.get("balance_cents") or 0)
    name = html.unescape((first_name or "").strip()) or "Друг"
    rows: list[tuple[str, str, str]] = []

    if audience == AUDIENCE_CLIENT:
        active = state.get("active") or []
        nearest = active[0]
        until = format_day_month(nearest.get("expires_at"))
        days = _days_left(nearest.get("expires_at"))
        if len(active) == 1:
            plan = _plan_name(nearest.get("tariff_id")) or "Подписка"
            rows.append(("crown" if "premium" in plan.lower() else "generic", "Статус", plan))
            rows.append(("calendar", "Действует до", until))
        else:
            rows.append(("key", "Активных ключей", str(len(active))))
            rows.append(("calendar", "Ближайший до", until))
        if days is not None and days >= 0:
            rows.append(("hourglass", "Осталось", f"{days} {_plural(days, 'день', 'дня', 'дней')}"))
        devices = _max_devices(nearest.get("tariff_id")) if len(active) == 1 else 0
        if devices > 0:
            rows.append(("phone", "Устройства", f"до {devices}"))
        if balance >= 100:
            rows.append(("wallet", "Баланс", _rub(balance)))
    elif audience == AUDIENCE_EXPIRED:
        last = max(state.get("keys") or [{}], key=lambda k: str(k.get("expires_at") or ""))
        rows.append(("close", "Статус", "Закончилась"))
        rows.append(("calendar", "Дата окончания", format_day_month(last.get("expires_at"))))
        rows.append(("gift", "Продлите", "ключи заработают сразу"))
        if balance >= 100:
            rows.append(("wallet", "Баланс", _rub(balance)))
    else:
        return None

    W, H = 1200, 920
    img = _gradient(W, H).convert("RGBA")
    draw = ImageDraw.Draw(img)

    # Мягкое золотое свечение сверху и декоративное кольцо.
    glow = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    gd = ImageDraw.Draw(glow)
    for r, alpha in ((520, 26), (380, 20), (250, 18)):
        gd.ellipse((W // 2 - r, -r + 40, W // 2 + r, r + 40), fill=(214, 172, 74, alpha))
    glow = glow.filter(ImageFilter.GaussianBlur(60))
    img.alpha_composite(glow)

    # Рамка карточки.
    draw.rounded_rectangle((26, 26, W - 26, H - 26), radius=38,
                           outline=(*GOLD[:3], 180), width=2)
    draw.rounded_rectangle((36, 36, W - 36, H - 36), radius=32,
                           outline=(255, 255, 255, 20), width=1)

    # Логотип-затмение.
    cx, cy = 112, 120
    draw.ellipse((cx - 38, cy - 38, cx + 38, cy + 38), outline=GOLD_LIGHT, width=5)
    draw.ellipse((cx - 29, cy - 29, cx + 29, cy + 29), fill=(8, 8, 12, 255))
    draw.arc((cx - 38, cy - 38, cx + 38, cy + 38), 210, 330, fill=(255, 232, 168, 255), width=5)

    _spaced(draw, (172, 70), "ECLIPSE", _font(FONT_BOLD, 56), GOLD_LIGHT, 9)
    _spaced(draw, (176, 136), "UNLIMITED", _font(FONT_BOLD, 20), GOLD_DIM, 8)

    # Плашка PREMIUM.
    draw.rounded_rectangle((938, 78, 1112, 132), radius=27, outline=GOLD, width=2)
    _spaced(draw, (960, 91), "PREMIUM", _font(FONT_BOLD, 22), GOLD_LIGHT, 4)

    # Приветствие.
    draw.text((82, 224), f"{name}, всё работает", font=_font(FONT_BOLD, 46), fill=WHITE)
    draw.ellipse((1108, 248, 1124, 264), fill=(88, 220, 130, 255))

    draw.line((82, 300, 1118, 300), fill=(*GOLD[:3], 90), width=2)
    draw.line((82, 300, 330, 300), fill=GOLD_LIGHT, width=3)

    y = 334
    for icon, label, value in rows:
        _row(draw, img, y, icon, label, value)
        y += 88

    draw.line((82, 842, 1118, 842), fill=(255, 255, 255, 18), width=1)
    draw.text((82, 860), "ECLIPSE UNLIMITED", font=_font(FONT_BOLD, 18), fill=GOLD_DIM)
    end = "Спасибо, что с нами"
    f = _font(FONT_REG, 18)
    draw.text((1118 - draw.textlength(end, font=f), 860), end, font=f, fill=MUTED)

    out = BytesIO()
    img.convert("RGB").save(out, format="PNG", optimize=True)
    return out.getvalue()
