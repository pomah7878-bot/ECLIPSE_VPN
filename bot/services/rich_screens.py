"""Таблицы экранов «нового вида меню» (чистые функции: данные → HTML таблицы). Подключаются через render_page(rich_html=...)."""
from typing import Any, Dict, List, Mapping

from bot.utils.rich_view import table


def _date(value: Any) -> str:
    from bot.utils.datetime_format import format_date_for_display
    return format_date_for_display(value)


def my_keys_html(keys: List[Mapping[str, Any]]) -> str:
    """«Мои ключи»: ключ · статус · до · трафик."""
    from database.requests import is_traffic_exhausted
    from bot.services.vpn_api import format_traffic
    rows = []
    for k in keys:
        ok = k.get("is_active") and not is_traffic_exhausted(k)
        limit = k.get("traffic_limit") or 0
        traffic = f"{format_traffic(k.get('traffic_used') or 0)} / {format_traffic(limit) if limit > 0 else '∞'}"
        name = str(k.get("display_name") or f"Ключ {k.get('id')}")[:22]
        rows.append((f"{'🟢' if ok else '🔴'} {name}", f"до {_date(k.get('expires_at'))}\n{traffic}"))
    return table("🔑 Мои ключи", rows, header=("Ключ", "Срок и трафик"))


def key_details_html(key: Mapping[str, Any], status: str, traffic_info: str, inbound_name: str, protocol: str,
                     payments: List[Mapping[str, Any]]) -> str:
    """Карточка ключа: параметры и (если есть) последние операции второй таблицей."""
    from bot.services.client_cabinet import bar
    traffic = traffic_info
    limit, used = key.get("traffic_limit") or 0, key.get("traffic_used") or 0
    if limit > 0 and key.get("server_id"):
        traffic = f"{traffic_info}  {bar(used, limit)}"
    tariff = key.get("tariff_name") or "не определён"
    if key.get("max_ips"):
        tariff += f", устройств: {key.get('max_ips')}"
    rows = [("Статус", status), ("Сервер", key.get("server_name") or "Не выбран"),
            ("Протокол", f"{inbound_name} ({protocol})"), ("Тариф", tariff), ("Трафик", traffic),
            ("Действует до", _date(key.get("expires_at"))),
            ("Автопродление", "✅ включено" if key.get("auto_renew") else "⛔ выключено")]
    out = table(f"🔑 {key.get('display_name') or 'VPN-ключ'}", rows)
    history = _history_rows(payments)
    if history:
        out += table("📜 История операций", history, header=("Дата", "Операция", "Сумма"))
    return out


def _history_rows(payments: List[Mapping[str, Any]], limit: int = 5) -> List[tuple]:
    from bot.services.notifications import _format_payment_amount
    rows = []
    for p in list(payments or [])[:limit]:
        if p.get("history_type") == "key_operation":
            days = int(p.get("delta_days") or 0)
            rows.append((_date(p.get("paid_at")), p.get("reason") or "Начисление дней", f"+{days} дн." if days > 0 else "—"))
        else:
            rows.append((_date(p.get("paid_at")), p.get("tariff_name") or "Тариф", _format_payment_amount(dict(p))))
    return rows
