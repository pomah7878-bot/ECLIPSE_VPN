"""Настройки пробной лицензии (на главном сервере).

Включено по умолчанию, 10 дней, все функции кроме права отключать метку Powered by.
Меняется командой /license_trial (только админ).
"""
from database.requests import get_setting, set_setting

DEFAULT_DAYS = 10


def trial_enabled() -> bool:
    return (get_setting("license_trial_enabled", "1") or "1") == "1"


def set_trial_enabled(value: bool) -> None:
    set_setting("license_trial_enabled", "1" if value else "0")


def trial_days() -> int:
    try:
        return max(1, min(60, int(get_setting("license_trial_days", str(DEFAULT_DAYS)) or DEFAULT_DAYS)))
    except (TypeError, ValueError):
        return DEFAULT_DAYS


def set_trial_days(days: int) -> None:
    set_setting("license_trial_days", str(max(1, min(60, int(days)))))


def trial_features() -> set:
    from bot.services.license import GATED_FEATURES, HIDE_POWERED_BY_FEATURE
    return set(GATED_FEATURES) - {HIDE_POWERED_BY_FEATURE}
