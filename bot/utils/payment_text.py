"""Текст платежей: плательщик видит срок, трафик, устройства и название сервиса."""

PREFIXES = ("Подписка на ", "Пополнение баланса", "Оплата услуг ")


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


def _brand() -> str:
    from database.db_settings import get_effective_brand_name
    return (get_effective_brand_name() or "").strip() or "ECLIPSE Unlimited"


def _devices_word(n: int) -> str:
    if n % 10 == 1 and n % 100 != 11:
        return "устройства"
    return "устройств"


def public_description(tariff=None, short=False) -> str:
    """short=True — краткая подпись («Подписка на 6 мес.»), для строки счёта Telegram."""
    brand = _brand()
    label = ""
    if tariff:
        try:
            label = duration_label(tariff.get("duration_days"))
        except Exception:
            label = ""
    if not label:
        return (f"Услуги {brand}" if short else f"Оплата услуг {brand}")[:100]
    head = f"Подписка на {label}"
    if short:
        return head[:100]
    parts = [head]
    try:
        gb = int(tariff.get("traffic_limit_gb") or 0)
    except (TypeError, ValueError):
        gb = 0
    parts.append("Безлимитный трафик" if gb <= 0 else f"{gb} ГБ трафика")
    try:
        ips = int(tariff.get("max_ips") or 0)
    except (TypeError, ValueError):
        ips = 0
    if ips > 0:
        parts.append(f"до {ips} {_devices_word(ips)}")
    parts.append(brand)
    return " · ".join(parts)[:120]


def topup_description() -> str:
    return f"Пополнение баланса · {_brand()}"[:100]


def clean_description(description) -> str:
    """Для платёжных функций: наш текст проходит как есть; всё остальное
    (названия тарифов, ключей, пометки) заменяется нейтральным."""
    text = str(description or "")
    if text.startswith("Пополнение"):
        return topup_description()
    if text.startswith(PREFIXES):
        return text[:255]
    return public_description()

# v1.150 final text
