"""CDN у провайдера (Yandex Cloud): трафик и расход из Yandex Monitoring.

Нужны сервисный аккаунт с ролью monitoring.viewer, его API-ключ и ID каталога. Читается только
статистика (метрики сервиса yccdn), управлять CDN через этот ключ нельзя. Настройки хранятся
в настройках бота: yc_api_key, yc_folder_id, yc_price_per_gb_cents (копейки за 1 ГБ).
"""
from __future__ import annotations

import logging
import math
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

GB = 1024 ** 3
API_URL = "https://monitoring.api.cloud.yandex.net/monitoring/v2/data/read"
_CACHE: Dict[str, Tuple[float, Dict[str, Any]]] = {}
CACHE_SECONDS = 60


def _setting(key: str, default: str = "") -> str:
    try:
        from database.db_settings import get_setting
        return str(get_setting(key, default) or default)
    except Exception:  # noqa: BLE001
        return default


def set_setting_value(key: str, value: str) -> None:
    from database.db_settings import set_setting
    set_setting(key, value)
    _CACHE.clear()


def get_api_key() -> str:
    return _setting("yc_api_key").strip()


def get_folder_id() -> str:
    return _setting("yc_folder_id").strip()


def get_price_per_gb_cents() -> int:
    try:
        return max(0, int(_setting("yc_price_per_gb_cents", "0") or 0))
    except ValueError:
        return 0


def is_configured() -> bool:
    return bool(get_api_key() and get_folder_id())


def cost_rub(total_bytes: float) -> Optional[float]:
    """Оценка расхода, ₽ (None, если цена за ГБ не задана)."""
    price = get_price_per_gb_cents()
    if price <= 0:
        return None
    return total_bytes / GB * price / 100


async def _post(url: str, headers: Dict[str, str], body: Dict[str, Any]) -> Tuple[int, Any]:
    import aiohttp
    timeout = aiohttp.ClientTimeout(total=30)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        async with session.post(url, headers=headers, json=body) as resp:
            try:
                data = await resp.json(content_type=None)
            except Exception:  # noqa: BLE001
                data = {"message": (await resp.text())[:200]}
            return resp.status, data


class ProviderError(Exception):
    pass


def _values(response: Any) -> List[float]:
    """Все конечные значения из ответа (пропуски NaN отбрасываются)."""
    out: List[float] = []
    for metric in (response or {}).get("metrics", []) or []:
        ts = metric.get("timeseries") or {}
        for raw in (ts.get("doubleValues") or ts.get("int64Values") or []):
            try:
                v = float(raw)
            except (TypeError, ValueError):
                continue
            if math.isfinite(v):
                out.append(v)
    return out


async def _sum_metric(metric: str, start: datetime, end: datetime, grid_ms: int, extra: str = "") -> float:
    iso = lambda d: d.strftime("%Y-%m-%dT%H:%M:%SZ")  # noqa: E731
    query = f'series_sum("{metric}"{{service="yccdn"{extra}}})'
    headers = {"Authorization": f"Api-Key {get_api_key()}", "Content-Type": "application/json"}
    url = f"{API_URL}?folderId={get_folder_id()}"
    body: Dict[str, Any] = {
        "query": query, "fromTime": iso(start), "toTime": iso(end),
        "downsampling": {"gridInterval": grid_ms, "gridAggregation": "SUM"},
    }
    status, data = await _post(url, headers, body)
    if status == 400:  # на случай, если API не принял способ агрегации
        body["downsampling"] = {"gridInterval": grid_ms}
        status, data = await _post(url, headers, body)
    if status == 401:
        raise ProviderError("ключ API не принят (проверьте ключ)")
    if status == 403:
        raise ProviderError("у ключа нет прав на чтение мониторинга (нужна роль monitoring.viewer)")
    if status != 200:
        msg = str((data or {}).get("message", "")) if isinstance(data, dict) else ""
        raise ProviderError(f"ответ {status}: {msg[:120]}")
    return sum(_values(data))


async def fetch_summary(force: bool = False) -> Dict[str, Any]:
    """Трафик CDN у провайдера: за 24 часа и с начала месяца (UTC)."""
    if not is_configured():
        return {"ok": False, "error": "не заданы ключ API и ID каталога"}
    cache_key = get_folder_id()
    cached = _CACHE.get(cache_key)
    if cached and not force and time.time() - cached[0] < CACHE_SECONDS:
        return cached[1]
    now = datetime.now(timezone.utc)
    month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    try:
        day_bytes = await _sum_metric("edge.bytes_sent", now - timedelta(hours=24), now, 3600_000)
        month_bytes = await _sum_metric("edge.bytes_sent", month_start, now, 86400_000)
        month_requests = await _sum_metric("edge.requests", month_start, now, 86400_000)
        origin_bytes = await _sum_metric("origin.bytes_fetched", month_start, now, 86400_000)
    except ProviderError as e:
        return {"ok": False, "error": str(e)}
    except Exception as e:  # noqa: BLE001
        logger.warning("Yandex Monitoring: %s", e)
        return {"ok": False, "error": "нет связи с Yandex Cloud"}
    result = {
        "ok": True, "day_bytes": day_bytes, "month_bytes": month_bytes,
        "month_requests": month_requests, "origin_bytes": origin_bytes,
        "month_start": month_start.strftime("%d.%m.%Y"),
    }
    _CACHE[cache_key] = (time.time(), result)
    return result


def _gb(value: float) -> str:
    return f"{value / GB:.2f}".replace(".", ",") + " ГБ"


def _money(value: Optional[float]) -> str:
    return "цена за ГБ не задана" if value is None else f"≈ {value:,.2f} ₽".replace(",", " ").replace(".", ",")


def format_summary(summary: Dict[str, Any], packs: List[Dict[str, Any]]) -> str:
    """Текст экрана «CDN у провайдера» (HTML) вместе с учётом пакетов клиентов."""
    from html import escape
    if not summary.get("ok"):
        return f"❌ Не удалось получить данные: {escape(str(summary.get('error')))}"
    active = [p for p in packs if p.get("status") == "active"]
    limit = sum(int(p.get("limit_bytes") or 0) for p in active)
    used_active = sum(int(p.get("used_bytes") or 0) for p in active)
    free_count = sum(1 for p in active if p.get("is_free"))
    lines = [
        "<b>У провайдера (Yandex Cloud)</b>",
        f"За 24 часа: <b>{_gb(summary['day_bytes'])}</b> {_money(cost_rub(summary['day_bytes']))}",
        f"С {summary['month_start']} (месяц): <b>{_gb(summary['month_bytes'])}</b> "
        f"{_money(cost_rub(summary['month_bytes']))}",
        f"Запросов за месяц: {int(summary['month_requests']):,}".replace(",", " "),
        f"Забрано с вашего сервера за месяц: {_gb(summary['origin_bytes'])}",
        "",
        "<b>Пакеты клиентов (по учёту бота)</b>",
        f"Активных пакетов: {len(active)} (бесплатных: {free_count})",
        f"Выдано объёма: {_gb(limit)}, израсходовано: {_gb(used_active)}",
    ]
    return "\n".join(lines)
