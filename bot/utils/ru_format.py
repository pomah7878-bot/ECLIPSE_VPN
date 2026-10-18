"""
Общие форматтеры для текстов бота (v1.350).

Раньше каждое сообщение форматировало суммы и дни по-своему: «150.00 ₽»,
«150,00 ₽», «150 ₽», «1 дней». Здесь — единые правила:

- суммы: «150 ₽», «150,50 ₽», «12 500 ₽» (разряды — неразрывным пробелом, от 10 000);
- дни и прочие числа со словом: «1 день», «2 дня», «5 дней».
"""
from __future__ import annotations

import re
from decimal import Decimal, InvalidOperation
from typing import Any

NBSP = ' '


def plural_ru(n: Any, one: str, few: str, many: str) -> str:
    """Форма слова после числа: 1 день, 2 дня, 5 дней."""
    try:
        n = abs(int(n))
    except (TypeError, ValueError):
        return many
    if n % 10 == 1 and n % 100 != 11:
        return one
    if 2 <= n % 10 <= 4 and not 12 <= n % 100 <= 14:
        return few
    return many


def days_ru(n: Any) -> str:
    """«1 день», «3 дня», «30 дней»."""
    try:
        value = int(n)
    except (TypeError, ValueError):
        return f'{n} дней'
    return f"{value} {plural_ru(value, 'день', 'дня', 'дней')}"


def format_number_ru(value: Any, decimals: int = 2) -> str:
    """Число без лишних нулей: 150 → «150», 150.5 → «150,5», 150.25 → «150,25»."""
    try:
        number = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return str(value)
    number = number.quantize(Decimal(1).scaleb(-decimals))
    text = format(number, 'f')
    if '.' in text:
        text = text.rstrip('0').rstrip('.')
    return text.replace('.', ',')


def format_rub(value: Any, *, cents: bool = False) -> str:
    """Сумма в рублях: «150 ₽», «150,50 ₽», «12 500 ₽».

    cents=True — на входе копейки (так суммы лежат в базе), иначе рубли.
    """
    try:
        amount = Decimal(str(value if value is not None else 0))
    except (InvalidOperation, ValueError):
        return f'{value} ₽'
    if cents:
        amount = amount / Decimal(100)
    amount = amount.quantize(Decimal('0.01'))
    whole, _, frac = format(abs(amount), 'f').partition('.')
    if len(whole) >= 5:
        whole = f'{int(whole):,}'.replace(',', NBSP)
    frac = frac.rstrip('0')
    sign = '−' if amount < 0 else ''
    return f"{sign}{whole}{',' + frac if frac else ''} ₽"


_DAYS_WORD_RE = re.compile(r'(%ключ_дней_до_окончания%|%key_days_left%)(\s+)(дней|дня|день)(?![а-яё])', re.IGNORECASE)


def fix_days_word(text: str, days: Any) -> str:
    """«%ключ_дней_до_окончания% дней» → «3 дня»: подбирает слово под число.

    Нужно, потому что в сохранённых текстах уведомлений слово «дней» стоит
    прямо после плейсхолдера и для 1, 2, 3, 4, 21… получалось «1 дней».
    """
    try:
        value = int(days)
    except (TypeError, ValueError):
        return text
    return _DAYS_WORD_RE.sub(lambda m: days_ru(value), text)
