"""Нейтральный текст платежей: плательщик видит сервис и срок подписки."""


def duration_label(days) -> str:
    try:
        d = int(days)
    except (TypeError, ValueError):
        return ""
    if d <= 0:
        return ""
    if d in (360, 365, 366):
        return "1 год"
    if d >= 730 and d % 365 in (0, 1):
        return f"{d // 365} г."
    if d % 30 == 0 and d < 360:
        return f"{d // 30} мес."
    return f"{d} дн."


def public_description(tariff=None) -> str:
    from database.db_settings import get_effective_brand_name
    brand = (get_effective_brand_name() or "").strip() or "ECLIPSE Unlimited"
    text = f"Оплата услуг {brand}"
    if tariff:
        try:
            label = duration_label(tariff.get("duration_days"))
        except Exception:
            label = ""
        if label:
            text += f" — подписка на {label}"
    return text[:100]


def clean_description(description) -> str:
    """Для платёжных функций: наш нейтральный текст проходит как есть,
    всё остальное (названия тарифов, ключей и т.п.) заменяется на общий."""
    text = str(description or "")
    if text.startswith("Оплата услуг "):
        return text[:255]
    return public_description()

# v1.148 duration
