"""
Главный экран «как приложение» (v1.340).

Главная показывает разное трём людям:
- новый (ключей нет) — короткое предложение и кнопка «Попробовать»;
- клиент (есть действующий ключ) — карточка подписки и кнопка «Подключиться»;
- истёкший (ключи есть, но все закончились) — карточка «подписка закончилась» и «Продлить».

Включается само, когда на главной есть кнопка btn_connect («⚡ Подключиться»):
у установок без неё (партнёры со своими настройками) ничего не меняется.

В тексте главной можно разделять части для разных людей:
  [новым]…[/новым]   [клиентам]…[/клиентам]   [истекшим]…[/истекшим]
и использовать плейсхолдеры %подписка%, %имя%, %от_цены%, %предложение%.
"""
from __future__ import annotations

import logging
import re
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from bot.utils.text import escape_html

logger = logging.getLogger(__name__)

CONNECT_BUTTON_ID = 'btn_connect'

AUDIENCE_NEW = 'new'
AUDIENCE_CLIENT = 'client'
AUDIENCE_EXPIRED = 'expired'

_SECTION_TAGS = {
    'новым': AUDIENCE_NEW,
    'клиентам': AUDIENCE_CLIENT,
    'истекшим': AUDIENCE_EXPIRED,
    'истёкшим': AUDIENCE_EXPIRED,
}
_SECTION_RE = re.compile(
    r'\[(новым|клиентам|истекшим|истёкшим)\](.*?)\[/\1\]\n?',
    re.DOTALL | re.IGNORECASE,
)

_MONTHS = (
    'января', 'февраля', 'марта', 'апреля', 'мая', 'июня',
    'июля', 'августа', 'сентября', 'октября', 'ноября', 'декабря',
)


# ---------------------------------------------------------------- секции текста

def has_audience_sections(text: str) -> bool:
    return bool(text) and bool(_SECTION_RE.search(text))


def apply_audience_sections(text: str, audience: Optional[str]) -> str:
    """Оставляет части текста, адресованные этому человеку; остальные убирает.

    Без известной аудитории теги просто снимаются, содержимое остаётся.
    """
    if not text or '[' not in text:
        return text

    def repl(match: re.Match) -> str:
        target = _SECTION_TAGS.get(match.group(1).lower())
        body = match.group(2)
        if audience is None or target == audience:
            return body + ('\n' if match.group(0).endswith('\n') else '')
        return ''

    return _SECTION_RE.sub(repl, text)


# ---------------------------------------------------------------- форматирование

def _plural(n: int, one: str, few: str, many: str) -> str:
    n = abs(int(n))
    if n % 10 == 1 and n % 100 != 11:
        return one
    if 2 <= n % 10 <= 4 and not 12 <= n % 100 <= 14:
        return few
    return many


def _parse_dt(value: Any) -> Optional[datetime]:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(str(value).replace('Z', '+00:00'))
    except (TypeError, ValueError):
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def format_day_month(value: Any) -> str:
    """«12 ноября», с годом — если не текущий."""
    dt = _parse_dt(value)
    if dt is None:
        return ''
    try:
        from bot.utils.datetime_format import get_display_tzinfo
        dt = dt.astimezone(get_display_tzinfo())
    except Exception:  # noqa: BLE001
        pass
    text = f"{dt.day} {_MONTHS[dt.month - 1]}"
    if dt.year != datetime.now(dt.tzinfo).year:
        text += f" {dt.year}"
    return text


def _days_left(value: Any) -> Optional[int]:
    dt = _parse_dt(value)
    if dt is None:
        return None
    sec = (dt - datetime.now(timezone.utc)).total_seconds()
    return int(sec // 86400) if sec > 0 else -1


def _rub(cents: int) -> str:
    rub = int(cents) // 100
    return (f"{rub:,}".replace(',', ' ') if rub >= 10000 else str(rub)) + ' ₽'


# ---------------------------------------------------------------- данные

def get_user_state(telegram_id: int) -> Dict[str, Any]:
    """Ключи, аудитория и баланс человека для главной."""
    state: Dict[str, Any] = {
        'audience': AUDIENCE_NEW, 'keys': [], 'active': [], 'balance_cents': 0,
    }
    if not telegram_id:
        return state
    try:
        from database.requests import get_user_keys_for_display
        keys = get_user_keys_for_display(int(telegram_id)) or []
    except Exception as e:  # noqa: BLE001
        logger.debug('main_screen: ключи не получены: %s', e)
        keys = []
    active = [k for k in keys if k.get('is_active')]
    active.sort(key=lambda k: str(k.get('expires_at') or ''))
    state['keys'] = keys
    state['active'] = active
    if active:
        state['audience'] = AUDIENCE_CLIENT
    elif keys:
        state['audience'] = AUDIENCE_EXPIRED
    try:
        from database.requests import get_user_internal_id, get_user_balance
        internal_id = get_user_internal_id(int(telegram_id))
        state['balance_cents'] = int(get_user_balance(internal_id) or 0) if internal_id else 0
    except Exception as e:  # noqa: BLE001
        logger.debug('main_screen: баланс не получен: %s', e)
    return state


def _plan_name(tariff_id: Any) -> str:
    """Название подписки для карточки: группа тарифа (если групп несколько) или сам тариф."""
    if not tariff_id:
        return ''
    try:
        from database.requests import get_tariff_by_id, get_groups_count, get_group_by_id
        from bot.utils.page_dynamic_data import _clean_tariff_name
        tariff = get_tariff_by_id(int(tariff_id))
        if not tariff:
            return ''
        if tariff.get('group_id') and get_groups_count() > 1:
            group = get_group_by_id(int(tariff['group_id']))
            if group and group.get('name'):
                return _strip_emoji(str(group['name']))
        return _strip_emoji(_clean_tariff_name(tariff.get('name') or ''))
    except Exception as e:  # noqa: BLE001
        logger.debug('main_screen: тариф не получен: %s', e)
        return ''


def _strip_emoji(name: str) -> str:
    """«Premium 👑» / «⚡ 1 месяц» → «Premium» / «1 месяц»: в карточке уже есть 🟢, второй значок лишний."""
    from bot.utils.page_dynamic_data import _is_symbol
    cleaned = ''.join(ch for ch in str(name or '') if not _is_symbol(ch))
    cleaned = ' '.join(cleaned.split())
    return cleaned or str(name or '').strip()


def _emoji_first(name: str) -> str:
    """«Premium 👑» → «👑 Premium»."""
    from bot.utils.page_dynamic_data import _is_symbol
    raw = name.strip()
    tail = ''
    while raw and (_is_symbol(raw[-1]) or (raw[-1] == ' ' and tail)):
        tail = raw[-1] + tail
        raw = raw[:-1]
    raw, tail = raw.strip(), tail.strip()
    return f"{tail} {raw}" if raw and tail else name.strip()


def _max_devices(tariff_id: Any) -> int:
    if not tariff_id:
        return 0
    try:
        from database.requests import get_tariff_by_id
        tariff = get_tariff_by_id(int(tariff_id)) or {}
        return int(tariff.get('max_ips') or 0)
    except Exception:  # noqa: BLE001
        return 0


def build_subscription_card(state: Dict[str, Any]) -> str:
    """Карточка подписки (HTML-цитата). Для нового человека — пусто."""
    audience = state.get('audience')
    balance = int(state.get('balance_cents') or 0)
    lines: List[str] = []

    if audience == AUDIENCE_CLIENT:
        active = state.get('active') or []
        nearest = active[0]
        until = format_day_month(nearest.get('expires_at'))
        days = _days_left(nearest.get('expires_at'))
        days_text = f"{days} {_plural(days, 'день', 'дня', 'дней')}" if days is not None and days >= 0 else ''
        if len(active) == 1:
            plan = _plan_name(nearest.get('tariff_id'))
            head = f"🟢 {escape_html(plan)} · до {until}" if plan else f"🟢 Подписка активна · до {until}"
            lines.append(head)
            second = []
            if days_text:
                second.append(f"⏳ {days_text}")
            devices = _max_devices(nearest.get('tariff_id'))
            if devices > 0:
                second.append(f"📱 до {devices} {_plural(devices, 'устройства', 'устройств', 'устройств')}")
            if second:
                lines.append(' · '.join(second))
            limit = int(nearest.get('traffic_limit') or 0)
            if limit > 0:
                from bot.services.vpn_api import format_traffic
                used = int(nearest.get('traffic_used') or 0)
                lines.append(f"📊 {format_traffic(used)} из {format_traffic(limit)}")
        else:
            lines.append(f"🟢 Активных ключей: {len(active)}")
            near = f"⏳ Ближайший до {until}"
            if days_text:
                near += f" · {days_text}"
            lines.append(near)
    elif audience == AUDIENCE_EXPIRED:
        last = max(state.get('keys') or [{}], key=lambda k: str(k.get('expires_at') or ''))
        when = format_day_month(last.get('expires_at'))
        lines.append(f"🔴 Подписка закончилась {when}".rstrip())
        lines.append('Продлите — ключи заработают сразу')
    else:
        return ''

    if balance >= 100:
        lines.append(f"💰 Баланс {_rub(balance)}")
    return '<blockquote>' + '\n'.join(lines) + '</blockquote>'


def build_subscription_html(state: Dict[str, Any], first_name: str, extra: Optional[List[str]] = None) -> Optional[str]:
    """«Новый вид меню»: приветствие и карточка подписки таблицей (для клиентов и тех, у кого подписка закончилась)."""
    from bot.utils.rich_view import table
    audience = state.get('audience')
    balance = int(state.get('balance_cents') or 0)
    rows: List[tuple] = []
    if audience == AUDIENCE_CLIENT:
        active = state.get('active') or []
        nearest = active[0]
        until = format_day_month(nearest.get('expires_at'))
        days = _days_left(nearest.get('expires_at'))
        if len(active) == 1:
            plan = _plan_name(nearest.get('tariff_id'))
            rows.append(('🟢 Подписка', f"{plan} · до {until}" if plan else f"активна · до {until}"))
        else:
            rows.append(('🟢 Активных ключей', str(len(active))))
            rows.append(('⏳ Ближайший до', until))
        if days is not None and days >= 0:
            rows.append(('⏳ Осталось', f"{days} {_plural(days, 'день', 'дня', 'дней')}"))
        devices = _max_devices(nearest.get('tariff_id')) if len(active) == 1 else 0
        if devices > 0:
            rows.append(('📱 Устройства', f"до {devices}"))
        limit = int(nearest.get('traffic_limit') or 0)
        if len(active) == 1 and limit > 0:
            from bot.services.vpn_api import format_traffic
            rows.append(('📊 Трафик', f"{format_traffic(int(nearest.get('traffic_used') or 0))} из {format_traffic(limit)}"))
    elif audience == AUDIENCE_EXPIRED:
        last = max(state.get('keys') or [{}], key=lambda k: str(k.get('expires_at') or ''))
        rows.append(('🔴 Подписка', f"закончилась {format_day_month(last.get('expires_at'))}".rstrip()))
        rows.append(('➡️ Что делать', 'продлите — ключи заработают сразу'))
    else:
        return None
    if balance >= 100:
        rows.append(('💰 Баланс', _rub(balance)))
    rows.extend(('📝', t) for t in (extra or []))
    name = (first_name or '').strip()
    return table(f"{name + ', в' if name else 'В'}сё работает 👋", rows)


def min_month_price_text() -> str:
    """«от 84 ₽/мес» — самая низкая цена в пересчёте на месяц (только платные тарифы в рублях)."""
    try:
        from database.requests import get_all_tariffs
        from bot.services.cdn import is_cdn_tariff
        best = None
        for t in get_all_tariffs() or []:
            rub = int(t.get('price_rub') or 0)
            days = int(t.get('duration_days') or 0)
            if rub <= 1 or days < 28 or is_cdn_tariff(t.get('id')):
                continue
            per_month = rub / max(1.0, days / 30.0)
            best = per_month if best is None else min(best, per_month)
        if best is None:
            return ''
        return f"от {int(round(best))} ₽/мес"
    except Exception as e:  # noqa: BLE001
        logger.debug('main_screen: цена не посчитана: %s', e)
        return ''


def trial_days() -> int:
    """Длительность пробного периода в днях (0 — пробного нет)."""
    try:
        from database.requests import get_trial_tariff_id, get_tariff_by_id, get_groups_with_trial
        tid = get_trial_tariff_id()
        if not tid:
            groups = get_groups_with_trial() or []
            tid = next((g.get('trial_tariff_id') for g in groups if g.get('trial_tariff_id')), None)
        if not tid:
            return 0
        tariff = get_tariff_by_id(int(tid)) or {}
        return int(tariff.get('duration_days') or 0)
    except Exception:  # noqa: BLE001
        return 0


def build_offer_text(trial_available: bool) -> str:
    """Строка-предложение для нового человека."""
    price = min_month_price_text()
    days = trial_days() if trial_available else 0
    if days > 0:
        text = f"🎁 {days} {_plural(days, 'день', 'дня', 'дней')} бесплатно"
        return f"{text}, дальше {price}" if price else text
    return f"💳 Тарифы {price}" if price else ''


def page_uses_app_screen(page: Optional[Dict[str, Any]]) -> bool:
    """Новый режим главной включён, если на странице есть кнопка «Подключиться»."""
    if not page:
        return False
    import json
    for raw in (page.get('buttons_custom'), page.get('buttons_default')):
        try:
            for btn in json.loads(raw or '[]') or []:
                if isinstance(btn, dict) and btn.get('id') == CONNECT_BUTTON_ID:
                    return True
        except (TypeError, ValueError):
            continue
    return False


def resolve_connect_button(ctx: dict) -> Optional[dict]:
    """Системная кнопка «⚡ Подключиться»: один ключ — сразу его карточка, несколько — список."""
    try:
        telegram_id = int((ctx or {}).get('telegram_id') or 0)
    except (TypeError, ValueError):
        telegram_id = 0
    if telegram_id <= 0:
        return None
    active = get_user_state(telegram_id).get('active') or []
    if not active:
        return None
    if len(active) == 1:
        return {'callback_data': f"key:{int(active[0]['id'])}:m"}  # :m — «Назад» ведёт на главную
    return {'callback_data': 'my_keys'}


# ---------------------------------------------------------------- переключение вида главной (v1.347)

VIEW_CLASSIC = 'classic'
VIEW_APP = 'app'
_SNAPSHOT_KEY = 'main_view_snapshot:'

# id кнопки → (ряд, колонка, подпись, цвет) для вида «Приложение»
APP_LAYOUT = {
    'btn_trial':         (0, 0, '🎁 Попробовать бесплатно', 'success'),
    CONNECT_BUTTON_ID:   (0, 1, '⚡ Подключиться', 'primary'),
    'btn_my_keys':       (1, 0, None, None),
    'btn_buy_key':       (1, 1, '💳 Тарифы', None),
    'btn_balance_topup': (2, 0, '💰 Пополнить', None),
    'btn_referral':      (2, 1, '🤝 Пригласить', None),
    'btn_ai_support':    (3, 0, None, 'secondary'),
    'btn_help':          (3, 1, '📖 Инструкция', None),
}
_CONNECT_BUTTON = {
    'id': CONNECT_BUTTON_ID, 'label': '⚡ Подключиться', 'color': 'primary', 'row': 0, 'col': 1,
    'is_hidden': False, 'action_type': 'system', 'action_value': None,
}
_APP_TAIL = (
    "[клиентам]<b>%имя%, всё работает</b> 👋\n[/клиентам]"
    "%подписка%"
)


def current_view() -> str:
    from database.requests import get_page
    return VIEW_APP if page_uses_app_screen(get_page('main')) else VIEW_CLASSIC


WEBAPP_VIEW_KEY = 'webapp_app_view'


def webapp_app_view_enabled() -> bool:
    """Вид «Приложение» в мини-приложении — отдельный переключатель, по умолчанию выключен."""
    try:
        from database.requests import get_setting
        return str(get_setting(WEBAPP_VIEW_KEY) or '0') == '1'
    except Exception:  # noqa: BLE001
        return False


def set_webapp_app_view(enabled: bool) -> None:
    from database.requests import set_setting
    set_setting(WEBAPP_VIEW_KEY, '1' if enabled else '0')


WEBAPP_COSMIC_KEY = 'webapp_cosmic'


def webapp_cosmic_enabled() -> bool:
    """Космическая тема мини-приложения — отдельный переключатель, по умолчанию выключен."""
    try:
        from database.requests import get_setting
        return str(get_setting(WEBAPP_COSMIC_KEY) or '0') == '1'
    except Exception:  # noqa: BLE001
        return False


def set_webapp_cosmic(enabled: bool) -> None:
    from database.requests import set_setting
    set_setting(WEBAPP_COSMIC_KEY, '1' if enabled else '0')


def _merged_buttons(page: Dict[str, Any]) -> List[Dict[str, Any]]:
    from bot.utils.page_renderer import _merge_buttons_by_id
    return [dict(b) for b in _merge_buttons_by_id(page.get('buttons_default') or '[]', page.get('buttons_custom'))]


def _build_app_view(page: Dict[str, Any]) -> Dict[str, Any]:
    """Вид «Приложение» из текущей (классической) главной: ваш текст остаётся для новичков."""
    buttons = [b for b in _merged_buttons(page) if b.get('id') != CONNECT_BUTTON_ID]
    buttons.append(dict(_CONNECT_BUTTON))
    others = [b for b in buttons if b.get('id') not in APP_LAYOUT]
    for b in buttons:
        if b.get('id') in APP_LAYOUT:
            r, c, label, color = APP_LAYOUT[b['id']]
            b['row'], b['col'] = r, c
            if label:
                b['label'] = label
            if color:
                b['color'] = color
    next_row, prev = 4, None
    for b in sorted(others, key=lambda x: (x.get('row', 0), x.get('col', 0))):
        if prev is not None and b.get('row', 0) != prev:
            next_row += 1
        prev = b.get('row', 0)
        b['row'] = next_row
    buttons.sort(key=lambda x: (x.get('row', 0), x.get('col', 0)))
    classic_text = (page.get('text_custom') or page.get('text_default') or '').strip()
    text = f"[новым]{classic_text}\n%предложение%[/новым]{_APP_TAIL}"
    import json as _json
    return {'text_custom': text, 'buttons_custom': _json.dumps(buttons, ensure_ascii=False)}


def _build_classic_view(page: Dict[str, Any]) -> Dict[str, Any]:
    """Запасной вариант, если классический вид не сохранён: без «Подключиться», текст по умолчанию."""
    import json as _json
    buttons = [b for b in _merged_buttons(page) if b.get('id') != CONNECT_BUTTON_ID]
    return {'text_custom': None, 'buttons_custom': _json.dumps(buttons, ensure_ascii=False)}


def save_view_snapshot(view: str, text_custom: Optional[str], buttons_custom: Optional[str]) -> None:
    import json as _json
    from database.requests import set_setting
    set_setting(_SNAPSHOT_KEY + view, _json.dumps(
        {'text_custom': text_custom, 'buttons_custom': buttons_custom}, ensure_ascii=False))


def _load_view_snapshot(view: str) -> Optional[Dict[str, Any]]:
    import json as _json
    from database.requests import get_setting
    raw = get_setting(_SNAPSHOT_KEY + view)
    if not raw:
        return None
    try:
        data = _json.loads(raw)
        return data if isinstance(data, dict) else None
    except (TypeError, ValueError):
        return None


def switch_view(target: str) -> bool:
    """Переключает вид главной. Текст и кнопки текущего вида сохраняются, чтобы к нему вернуться.

    Возвращает True, если вид изменился. Картинка (баннер) общая и не меняется.
    """
    if target not in (VIEW_CLASSIC, VIEW_APP):
        raise ValueError('unknown view')
    from database.requests import get_page
    from database.connection import get_db
    page = get_page('main')
    if not page:
        return False
    current = VIEW_APP if page_uses_app_screen(page) else VIEW_CLASSIC
    if current == target:
        return False
    save_view_snapshot(current, page.get('text_custom'), page.get('buttons_custom'))
    snap = _load_view_snapshot(target)
    if snap is None or (target == VIEW_APP and CONNECT_BUTTON_ID not in str(snap.get('buttons_custom') or '')):
        snap = _build_app_view(page) if target == VIEW_APP else _build_classic_view(page)
    with get_db() as conn:
        conn.execute(
            "UPDATE pages SET text_custom = ?, buttons_custom = ?, updated_at = CURRENT_TIMESTAMP WHERE page_key = 'main'",
            (snap.get('text_custom'), snap.get('buttons_custom')),
        )
    logger.info('Вид главной переключён: %s → %s', current, target)
    return True
