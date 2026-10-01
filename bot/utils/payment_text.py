"""Нейтральный текст платежей: плательщик видит только название сервиса."""


def public_description() -> str:
    from database.db_settings import get_effective_brand_name
    brand = (get_effective_brand_name() or "").strip() or "ECLIPSE Unlimited"
    return f"Оплата услуг {brand}"[:100]
