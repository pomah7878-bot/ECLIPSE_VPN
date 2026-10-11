"""CDN у провайдера (Yandex Cloud): трафик и расход из Yandex Monitoring.

Нужны сервисный аккаунт с ролью monitoring.viewer, его API-ключ и ID каталога. Читается только
статистика (метрики сервиса yccdn), управлять CDN через этот ключ нельзя. Настройки хранятся
в настройках бота: yc_api_key, yc_folder_id, yc_price_per_gb (₽ за 1 ГБ сверх лимита), yc_prepay_cents, yc_included_gb.
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


# Тариф Yandex Cloud CDN на момент написания (с НДС, в рублях): предоплата за ресурс в месяц,
# в неё входит 150 ГБ исходящего трафика и 100 млн запросов; сверх лимита — за каждый ГБ
# и за каждые 100 тыс. запросов. Значения можно изменить в настройках бота.
DEFAULT_PREPAY_RUB = 150.0
DEFAULT_PRICE_PER_GB_RUB = 1.054
DEFAULT_INCLUDED_GB = 150
INCLUDED_REQUESTS = 100_000_000
PRICE_PER_100K_REQUESTS_RUB = 1.0


def _float_setting(key: str, default: float) -> float:
    raw = _setting(key, "").strip().replace(",", ".")
    if raw == "":
        return default
    try:
        return max(0.0, float(raw))
    except ValueError:
        return default


def get_price_per_gb() -> float:
    """Цена 1 ГБ трафика сверх предоплаты, ₽."""
    return _float_setting("yc_price_per_gb", DEFAULT_PRICE_PER_GB_RUB)


def get_prepay_rub() -> float:
    """Ежемесячная предоплата за ресурс, ₽."""
    raw = _setting("yc_prepay_cents", "").strip()
    if raw == "":
        return DEFAULT_PREPAY_RUB
    try:
        return max(0, int(raw)) / 100
    except ValueError:
        return DEFAULT_PREPAY_RUB


def get_included_gb() -> int:
    """Трафик, входящий в ежемесячную предоплату ресурса (по тарифу Yandex Cloud — 150 ГБ)."""
    try:
        value = int(_setting("yc_included_gb", str(DEFAULT_INCLUDED_GB)) or DEFAULT_INCLUDED_GB)
    except ValueError:
        return DEFAULT_INCLUDED_GB
    return max(0, value)


def is_configured() -> bool:
    return bool(get_api_key() and get_folder_id())


def cost_rub(total_bytes: float) -> float:
    """Стоимость трафика по цене за ГБ, ₽."""
    return total_bytes / GB * get_price_per_gb()


def month_breakdown(month_bytes: float, month_requests: float = 0) -> Dict[str, Any]:
    """Расход за месяц по тарифу: предоплата (в неё входят 150 ГБ и 100 млн запросов)
    + трафик сверх лимита + запросы сверх лимита. Гранты и скидки провайдера не учитываются."""
    included = get_included_gb() * GB
    over_bytes = max(0.0, month_bytes - included)
    over_cost = cost_rub(over_bytes)
    over_requests = max(0.0, month_requests - INCLUDED_REQUESTS)
    requests_cost = math.ceil(over_requests / 100_000) * PRICE_PER_100K_REQUESTS_RUB if over_requests else 0.0
    prepay = get_prepay_rub()
    total = prepay + over_cost + requests_cost
    return {"included_bytes": included, "left_bytes": max(0.0, included - month_bytes),
            "over_bytes": over_bytes, "over_cost": over_cost, "over_requests": over_requests,
            "requests_cost": requests_cost, "prepay": prepay, "total": total,
            "per_gb": (total / (month_bytes / GB)) if month_bytes >= GB / 100 else None}


async def _post(url: str, headers: Dict[str, str], body: Dict[str, Any]) -> Tuple[int, Any]:
    import aiohttp
    timeout = aiohttp.ClientTimeout(total=10, connect=5)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        async with session.post(url, headers=headers, json=body) as resp:
            try:
                data = await resp.json(content_type=None)
            except Exception:  # noqa: BLE001
                data = {"message": (await resp.text())[:200]}
            return resp.status, data


class ProviderError(Exception):
    pass


class TransientProviderError(ProviderError):
    """Временный сбой на стороне провайдера или сети (5xx, 429, таймаут)."""


RETRY_DELAYS = (1.5,)          # паузы между повторами, секунд
_TRANSIENT_STATUSES = (429, 500, 502, 503, 504)
_LAST_GOOD_KEY = "yc_last_summary"


async def _post_retry(url: str, headers: Dict[str, str], body: Dict[str, Any]) -> Tuple[int, Any]:
    """_post с повторами при временных сбоях: у Yandex шлюз изредка отвечает
    503 «upstream connect error», через несколько секунд всё работает."""
    import asyncio
    attempts = len(RETRY_DELAYS) + 1
    last: Optional[Exception] = None
    for i in range(attempts):
        try:
            status, data = await _post(url, headers, body)
            if status not in _TRANSIENT_STATUSES:
                return status, data
            last = TransientProviderError(f"ответ {status}")
            logger.warning("Yandex Monitoring: ответ %s (попытка %d/%d): %s", status, i + 1, attempts, str(data)[:160])
        except Exception as e:  # noqa: BLE001
            last = e
            logger.warning("Yandex Monitoring: %s (попытка %d/%d)", e, i + 1, attempts)
        if i < attempts - 1:
            await asyncio.sleep(RETRY_DELAYS[i])
    if isinstance(last, TransientProviderError):
        raise last
    raise TransientProviderError("нет связи с Yandex Cloud")


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


class NoDataError(ProviderError):
    """Yandex ответил 200, но точек метрики нет."""


async def _sum_metric(metric: str, start: datetime, end: datetime, grid_ms: int, extra: str = "",
                      allow_empty: bool = False) -> float:
    """Метрики CDN у Yandex — скорости (байт/с, запросов/с), а не накопленные суммы:
    объём = среднее значение за интервал × длительность интервала в секундах."""
    iso = lambda d: d.strftime("%Y-%m-%dT%H:%M:%SZ")  # noqa: E731
    query = f'series_sum("{metric}"{{service="yccdn"{extra}}})'
    headers = {"Authorization": f"Api-Key {get_api_key()}", "Content-Type": "application/json"}
    url = f"{API_URL}?folderId={get_folder_id()}"
    body: Dict[str, Any] = {
        "query": query, "fromTime": iso(start), "toTime": iso(end),
        "downsampling": {"gridInterval": grid_ms, "gridAggregation": "AVG"},
    }
    status, data = await _post_retry(url, headers, body)
    if status == 400:  # на случай, если API не принял способ агрегации
        body["downsampling"] = {"gridInterval": grid_ms}
        status, data = await _post_retry(url, headers, body)
    if status == 401:
        raise ProviderError("ключ API не принят (проверьте ключ)")
    if status == 403:
        raise ProviderError("у ключа нет прав на чтение мониторинга (нужна роль monitoring.viewer)")
    if status != 200:
        msg = str((data or {}).get("message", "")) if isinstance(data, dict) else ""
        raise ProviderError(f"ответ {status}: {msg[:120]}")
    values = _values(data)
    if not values and not allow_empty:
        raise NoDataError("Yandex вернул пустой ответ: проверьте ID каталога (20 символов, начинается с b1g) "
                          "и что CDN-ресурс находится в этом каталоге")
    return sum(values) * (grid_ms / 1000.0)


_BG: Dict[str, Any] = {"task": None}


async def fetch_summary_fast() -> Dict[str, Any]:
    """Для открытия экрана: если есть свежий кэш или прошлые данные, отдаём сразу,
    а обновление идёт в фоне. Ждём Yandex только при самом первом запросе."""
    import asyncio
    if not is_configured():
        return await fetch_summary()
    cached = _CACHE.get(get_folder_id())
    if cached and time.time() - cached[0] < CACHE_SECONDS:
        return cached[1]
    last = _load_last_good()
    if not last:
        return await fetch_summary()
    task = _BG.get("task")
    if not task or task.done():
        _BG["task"] = asyncio.ensure_future(fetch_summary(force=True))
    return last


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
        import asyncio
        day_bytes, month_bytes, month_requests, origin_bytes = await asyncio.gather(
            _sum_metric("edge.bytes_sent", now - timedelta(hours=24), now, 3600_000, allow_empty=True),
            _sum_metric("edge.bytes_sent", month_start, now, 86400_000),
            _sum_metric("edge.requests", month_start, now, 86400_000, allow_empty=True),
            _sum_metric("origin.bytes_fetched", month_start, now, 86400_000, allow_empty=True),
        )
    except TransientProviderError:
        stale = _load_last_good()
        if stale:
            return stale
        return {"ok": False, "error": "Yandex Cloud временно не отвечает, повторите через минуту"}
    except ProviderError as e:
        return {"ok": False, "error": str(e)}
    except Exception as e:  # noqa: BLE001
        logger.warning("Yandex Monitoring: %s", e)
        stale = _load_last_good()
        if stale:
            return stale
        return {"ok": False, "error": "нет связи с Yandex Cloud"}
    result = {
        "ok": True, "day_bytes": day_bytes, "month_bytes": month_bytes,
        "month_requests": month_requests, "origin_bytes": origin_bytes,
        "month_start": month_start.strftime("%d.%m.%Y"),
    }
    _CACHE[cache_key] = (time.time(), result)
    _save_last_good(result)
    return result


def _save_last_good(result: Dict[str, Any]) -> None:
    try:
        import json
        from database.db_settings import set_setting   # напрямую: set_setting_value сбрасывает кэш
        set_setting(_LAST_GOOD_KEY, json.dumps({**result, "saved_at": time.time()}))
    except Exception:  # noqa: BLE001
        pass


def _load_last_good() -> Optional[Dict[str, Any]]:
    """Последние удачно полученные данные (переживают перезапуск) — показываем,
    когда провайдер временно не отвечает, с пометкой о времени."""
    try:
        import json
        data = json.loads(_setting(_LAST_GOOD_KEY, "") or "{}")
        if data.get("ok") and data.get("saved_at"):
            data["stale_at"] = data["saved_at"]
            return data
    except Exception:  # noqa: BLE001
        pass
    return None


def _gb(value: float) -> str:
    return f"{value / GB:.2f}".replace(".", ",") + " ГБ"


def _money(value: Optional[float]) -> str:
    return "—" if value is None else f"{value:,.2f} ₽".replace(",", " ").replace(".", ",")


def format_summary(summary: Dict[str, Any], packs: List[Dict[str, Any]], client_price_per_gb: Optional[float] = None) -> str:
    """Текст экрана «CDN у провайдера» (HTML) вместе с учётом пакетов клиентов."""
    from html import escape
    if not summary.get("ok"):
        return f"❌ Не удалось получить данные: {escape(str(summary.get('error')))}"
    br = month_breakdown(summary["month_bytes"], summary["month_requests"])
    requests = int(summary["month_requests"])
    lines = []
    if summary.get("stale_at"):
        msk = datetime.fromtimestamp(float(summary["stale_at"]), tz=timezone(timedelta(hours=3)))
        lines += [f"⚠️ Yandex Cloud сейчас не отвечает. Показаны данные от {msk:%d.%m %H:%M} МСК, "
                  "нажмите «Обновить» через минуту.", ""]
    lines += [
        "<b>У провайдера (Yandex Cloud)</b>",
        f"За 24 часа: <b>{_gb(summary['day_bytes'])}</b>",
        f"С {summary['month_start']} (месяц): <b>{_gb(summary['month_bytes'])}</b>",
    ]
    if br["included_bytes"]:
        if br["over_bytes"] <= 0:
            lines.append(f"В предоплату входит {_gb(br['included_bytes'])}, осталось {_gb(br['left_bytes'])}")
        else:
            lines.append(f"⚠️ Сверх предоплаты ({_gb(br['included_bytes'])}): <b>{_gb(br['over_bytes'])}</b>, "
                         f"≈ {_money(br['over_cost'])}")
    parts = [f"предоплата {_money(br['prepay'])}"]
    if br["over_cost"]:
        parts.append(f"трафик сверх лимита {_money(br['over_cost'])}")
    if br["requests_cost"]:
        parts.append(f"запросы сверх лимита {_money(br['requests_cost'])}")
    lines.append(f"Расход за месяц по тарифу: <b>≈ {_money(br['total'])}</b> ({' + '.join(parts)})")
    if br["per_gb"] is not None:
        eff = f"Эффективная цена: ≈ {_money(br['per_gb'])} за ГБ (расход ÷ трафик)"
        if client_price_per_gb:
            eff += f"; клиентам вы продаёте по {_money(client_price_per_gb)} за ГБ"
        lines.append(eff)
    lines.append(f"Запросов за месяц: {requests:,} из {INCLUDED_REQUESTS:,} включённых".replace(",", " "))
    lines.append(f"Забрано с вашего сервера: {_gb(summary['origin_bytes'])}")
    active = [p for p in packs if p.get("status") == "active"]
    limit = sum(int(p.get("limit_bytes") or 0) for p in active)
    used_active = sum(int(p.get("used_bytes") or 0) for p in active)
    free_count = sum(1 for p in active if p.get("is_free"))
    lines += [
        "",
        "<b>Пакеты клиентов (по учёту бота)</b>",
        f"Активных пакетов: {len(active)} (бесплатных: {free_count})",
        f"Выдано объёма: {_gb(limit)}, израсходовано: {_gb(used_active)}",
        "",
        "<i>Расчёт по тарифу, без грантов и скидок провайдера: в счёте он может быть меньше. "
        "Период предоплаты может начинаться не с 1 числа, поэтому границы месяца приблизительные.</i>",
    ]
    return "\n".join(lines)


# --------------------------------------------------- предупреждения о лимите --
ALERT_STEPS = (70, 90, 100)
_ALERT_KEY = "yc_alert_state"


async def check_limits(bot) -> Optional[int]:
    """Раз в час: предупреждает админов, когда месячный трафик подходит к включённым в прайс ГБ.
    Каждый порог (70, 90, 100%) отправляется один раз за месяц. Возвращает порог или None."""
    if not is_configured():
        return None
    summary = await fetch_summary(force=True)
    if not summary.get("ok") or summary.get("stale_at"):
        return None   # по устаревшим данным не предупреждаем
    import json
    included = float(get_included_gb()) * GB
    if included <= 0:
        return None
    used = float(summary.get("month_bytes") or 0)
    pct = used * 100.0 / included
    month = str(summary.get("month_start") or "")
    try:
        state = json.loads(_setting(_ALERT_KEY, "{}") or "{}")
    except ValueError:
        state = {}
    if state.get("month") != month:
        state = {"month": month, "pct": 0}
    step = max((s for s in ALERT_STEPS if pct >= s), default=0)
    if step <= int(state.get("pct") or 0):
        return None
    br = month_breakdown(used, float(summary.get("month_requests") or 0))
    extra = max(0.0, used - included)
    text = (
        f"📊 <b>CDN у провайдера: использовано {pct:.0f}% включённого трафика</b>\n\n"
        f"За месяц с {month}: {_gb(used)} из {_gb(included)}.\n"
        + (f"Сверх лимита уже {_gb(extra)}: каждый следующий ГБ стоит {get_price_per_gb():g} ₽.\n"
           if extra > 0 else
           f"Дальше каждый ГБ сверх лимита стоит {get_price_per_gb():g} ₽.\n")
        + f"Ориентировочный расход за месяц: {_money(br.get('total'))}.\n\n"
        "Что можно сделать: поднять цену или уменьшить объём пакета в /cdn, "
        "посмотреть расход по клиентам в «CDN у провайдера»."
    )
    try:
        from config import ADMIN_IDS
        from bot.utils.notify_kb import with_home
        for admin_id in list(ADMIN_IDS)[:5]:
            try:
                await bot.send_message(admin_id, text, parse_mode="HTML", reply_markup=with_home())
            except Exception as e:  # noqa: BLE001
                logger.debug("Yandex CDN: алерт админу %s не доставлен: %s", admin_id, e)
    finally:
        state["pct"] = step
        set_setting_value(_ALERT_KEY, json.dumps(state))
    return step
