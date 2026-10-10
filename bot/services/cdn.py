"""CDN-пакеты: платный доступ к CDN-инбаунду (обход белых списков).

Как это устроено:
  * CDN-инбаунд скрыт от обычной синхронизации (bot/utils/inbounds.py), основной
    клиент подписки в него не попадает.
  * Пакет = отдельный клиент панели ``cdn_<email ключа>`` с тем же subId. Он попадает
    в подписку ключа, а лимит ГБ и срок действия панель считает и применяет сама.
  * Пакет хранится в таблице cdn_packs (database/db_cdn.py), по одному на ключ.
  * Покупка — с баланса бота; админ может выдать пакет бесплатно и менять объём.
"""
from __future__ import annotations

import asyncio
import logging
import math
from collections import defaultdict
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

from bot.utils.inbounds import get_cdn_inbound_ids, is_cdn_inbound, reset_cdn_inbound_ids_cache
from database import db_cdn

logger = logging.getLogger(__name__)

GB = 1024 ** 3
DEFAULT_PACK_GB = 10
DEFAULT_PACK_DAYS = 30
NOTIFY_PCT = 80

_key_locks: Dict[int, asyncio.Lock] = defaultdict(asyncio.Lock)


# ---------------------------------------------------------------- settings --

def _int_setting(key: str, default: int) -> int:
    try:
        from database.db_settings import get_setting
        value = int(str(get_setting(key, str(default)) or default).strip())
        return value if value >= 0 else default
    except Exception:
        return default


def get_cdn_price_cents() -> int:
    """Цена пакета в копейках (0 = покупка выключена)."""
    return _int_setting("cdn_price_cents", 0)


def get_cdn_pack_gb() -> int:
    value = _int_setting("cdn_pack_gb", DEFAULT_PACK_GB)
    return value if value > 0 else DEFAULT_PACK_GB


def get_cdn_pack_days() -> int:
    value = _int_setting("cdn_pack_days", DEFAULT_PACK_DAYS)
    return value if value > 0 else DEFAULT_PACK_DAYS


def set_cdn_setting(key: str, value: str) -> None:
    from database.db_settings import set_setting
    set_setting(key, value)
    if key == "cdn_inbound_ids":
        reset_cdn_inbound_ids_cache()
    if key in ("cdn_price_cents", "cdn_pack_gb", "cdn_pack_days"):
        try:
            ensure_cdn_tariff()
        except Exception as e:  # noqa: BLE001
            logger.warning("CDN: не удалось обновить служебный тариф: %s", e)


# ------------------------------------------------- служебный тариф для оплаты --
# Пакет CDN оплачивается теми же способами, что и подписка (ЮKassa, карты, Stars,
# WATA, Platega, Cardlink), поэтому у него есть скрытый тариф (is_active=0):
# заказ на этот тариф с ключом выдаёт пакет, а не продлевает подписку.

CDN_TARIFF_NAME = "CDN-пакет"


def get_cdn_tariff_id() -> int:
    return _int_setting("cdn_tariff_id", 0)


def is_cdn_tariff(tariff_id: Any) -> bool:
    try:
        tid = int(tariff_id or 0)
    except (TypeError, ValueError):
        return False
    return tid > 0 and tid == get_cdn_tariff_id()


def ensure_cdn_tariff() -> int:
    """Создаёт/обновляет скрытый тариф под текущую цену пакета. 0 — покупка выключена."""
    from database import db_tariffs
    from database.db_settings import set_setting
    price = get_cdn_price_cents()
    if price <= 0:
        return 0
    rub = max(1, int(round(price / 100)))
    gb, days = get_cdn_pack_gb(), get_cdn_pack_days()
    try:
        from bot.services.stars_pricing import rate_rub
        rate = rate_rub()
    except Exception:  # noqa: BLE001
        rate = None
    usd_cents = int(round(rub / rate * 100)) if rate else 0
    fields = {
        "name": CDN_TARIFF_NAME, "duration_days": days, "price_rub": rub,
        "traffic_limit_gb": gb, "is_active": 0,
    }
    tid = get_cdn_tariff_id()
    current = db_tariffs.get_tariff_by_id(tid) if tid else None
    if current:
        changed = {k: v for k, v in fields.items() if current.get(k) != v}
        if usd_cents and current.get("price_cents") != usd_cents:
            changed["price_cents"] = usd_cents
        if changed:
            db_tariffs.update_tariff(tid, **changed)
        return tid
    tid = db_tariffs.add_tariff(
        CDN_TARIFF_NAME, days, usd_cents, 0, price_rub=rub, display_order=998,
        traffic_limit_gb=gb,
    )
    db_tariffs.update_tariff(tid, is_active=0)
    set_setting("cdn_tariff_id", str(tid))
    return tid


# ----------------------------------------------- CDN, входящий в обычный тариф --
# Админ может указать для тарифа объём CDN (ГБ): при покупке и продлении подписки
# по такому тарифу пакет подключается сразу, на срок тарифа. Хранится в настройке
# cdn_tariff_gb как JSON {"<id тарифа>": ГБ}.

def _tariff_gb_map() -> Dict[str, int]:
    import json
    try:
        from database.db_settings import get_setting
        raw = json.loads(get_setting("cdn_tariff_gb", "{}") or "{}")
        return {str(k): int(v) for k, v in raw.items() if int(v) > 0}
    except Exception:  # noqa: BLE001
        return {}


def get_tariff_cdn_gb(tariff_id: Any) -> int:
    try:
        return int(_tariff_gb_map().get(str(int(tariff_id or 0)), 0))
    except (TypeError, ValueError):
        return 0


def set_tariff_cdn_gb(tariff_id: int, gb: int) -> None:
    import json
    from database.db_settings import set_setting
    data = _tariff_gb_map()
    if gb > 0:
        data[str(int(tariff_id))] = int(gb)
    else:
        data.pop(str(int(tariff_id)), None)
    set_setting("cdn_tariff_gb", json.dumps(data))


def effective_tariff_cdn_gb(tariff_id: Any) -> int:
    """CDN в тарифе с учётом главного выключателя: 0, если CDN выключен или не настроен."""
    return get_tariff_cdn_gb(tariff_id) if is_cdn_active() else 0


def cdn_badge(tariff: Optional[Dict[str, Any]]) -> str:
    """Пометка для кнопок и карточек тарифа: «+CDN 10 ГБ» (пусто, если CDN в тариф не входит
    или CDN выключен)."""
    gb = effective_tariff_cdn_gb((tariff or {}).get("id"))
    return f"+CDN {gb} ГБ" if gb > 0 else ""


async def grant_tariff_pack(key_id: int) -> None:
    """Подключает пакет CDN, входящий в тариф ключа. Никогда не бросает исключений:
    сбой CDN не должен ломать покупку или продление подписки."""
    try:
        from database.db_tariffs import get_tariff_by_id
        from database.requests import get_vpn_key_by_id
        key = get_vpn_key_by_id(int(key_id))
        if not key or not key.get("tariff_id") or is_cdn_tariff(key.get("tariff_id")):
            return
        gb = effective_tariff_cdn_gb(key["tariff_id"])
        if gb <= 0:
            return
        tariff = get_tariff_by_id(int(key["tariff_id"])) or {}
        days = int(tariff.get("duration_days") or 0) or get_cdn_pack_days()
        pack = db_cdn.get_pack(int(key_id))
        if pack and pack["status"] == db_cdn.STATUS_ACTIVE and int(pack["limit_bytes"] or 0) >= gb * GB:
            try:
                until = datetime.fromisoformat(str(pack["expires_at"]))
            except ValueError:
                until = None
            if until and until >= datetime.utcnow() + timedelta(days=days):
                return  # уже есть пакет не хуже
        result = await activate_pack(int(key_id), gb=gb, days=days, is_free=False)
        if not result.get("ok"):
            logger.warning("CDN: пакет из тарифа не выдан, ключ %s: %s", key_id, result.get("error"))
    except Exception as e:  # noqa: BLE001
        logger.warning("CDN: пакет из тарифа не выдан, ключ %s: %s", key_id, e)


async def fulfil_paid_order(key_id: int, order_id: str) -> Dict[str, Any]:
    """Выдача пакета по оплаченному заказу (вызывается один раз на заказ)."""
    result = await activate_pack(key_id, gb=get_cdn_pack_gb(), days=get_cdn_pack_days(), is_free=False)
    if not result.get("ok"):
        raise RuntimeError(f"Пакет CDN не выдан для заказа {order_id}: {result.get('error')}")
    return result


def paid_message(gb: int, days: int) -> str:
    return (f"✅ Пакет CDN подключён: {gb} ГБ на {days} дн. "
            "Обновите подписку в приложении, чтобы появились новые подключения.")


def is_cdn_enabled() -> bool:
    """Главный выключатель CDN (⚙️ /cdn). Выключен — тарифы без CDN, продаж нет;
    настройки тарифов сохраняются, действующие пакеты работают до конца срока."""
    try:
        from database.db_settings import get_setting
        return str(get_setting("cdn_enabled", "1") or "1").strip() != "0"
    except Exception:  # noqa: BLE001
        return True


def is_cdn_active() -> bool:
    """CDN включён и настроен (есть CDN-инбаунд): только тогда он продаётся и выдаётся."""
    return is_cdn_enabled() and bool(get_cdn_inbound_ids())


def get_sale_price_cents() -> int:
    """Цена для клиента: 0, если CDN выключен или не настроен (покупка недоступна)."""
    return get_cdn_price_cents() if is_cdn_active() else 0


def format_price(cents: int) -> str:
    rub = cents / 100
    text = f"{rub:.2f}".rstrip("0").rstrip(".")
    return f"{text.replace('.', ',')} ₽"


def cdn_email(panel_email: str) -> str:
    return f"cdn_{panel_email}"


def cdn_sub_id(sub_id: str) -> str:
    """Отдельный subId CDN-клиента: панель 3x-ui не даёт двум клиентам один subId."""
    return f"{sub_id}cdn"


def _split_links(raw: bytes):
    """(список ссылок, был ли base64) или (None, False), если формат не распознан."""
    import base64
    text = (raw or b"").decode("utf-8", "ignore").strip()
    if not text or text[0] in "{[":
        return None, False
    if "://" in text:
        return [line.strip() for line in text.splitlines() if line.strip()], False
    try:
        decoded = base64.b64decode(text + "=" * (-len(text) % 4), validate=True).decode("utf-8", "ignore")
    except Exception:  # noqa: BLE001
        return None, False
    if "://" not in decoded:
        return None, False
    return [line.strip() for line in decoded.splitlines() if line.strip()], True


def merge_subscription_bodies(main: bytes, extra: bytes) -> bytes:
    """Добавляет ссылки из extra к подписке main в том же формате (base64 или текст)."""
    import base64
    main_links, main_b64 = _split_links(main)
    extra_links, _ = _split_links(extra)
    if not main_links or not extra_links:
        return main
    merged = main_links + [link for link in extra_links if link not in main_links]
    text = "\n".join(merged)
    if main_b64:
        return base64.b64encode(text.encode("utf-8"))
    return text.encode("utf-8")


async def get_connection_links(client, key_id: int, sub_id: str) -> str:
    """Ссылки подключений ключа для списка «Все подключения»: основные + CDN, если пакет активен."""
    raw = await client.get_subscription_link(sub_id) or ""
    try:
        pack = db_cdn.get_pack(int(key_id))
        if pack and pack["status"] == db_cdn.STATUS_ACTIVE:
            extra = await client.get_subscription_link(cdn_sub_id(sub_id))
            if extra:
                raw = f"{raw}\n{extra}".strip()
    except Exception as e:  # noqa: BLE001
        logger.debug("CDN: ссылки CDN-клиента не добавлены в список подключений: %s", e)
    return raw


async def merge_cdn_into_subscription(body: bytes, key: Dict[str, Any], forward_headers: Dict[str, str], session_factory) -> bytes:
    """Если у ключа активен CDN-пакет, добавляет CDN-ссылки в подписку клиента.

    CDN-клиент панели имеет собственный subId, поэтому его ссылки берём отдельным запросом."""
    if not key or not key.get("id") or not key.get("sub_id") or not key.get("server_id"):
        return body
    pack = db_cdn.get_pack(int(key["id"]))
    if not pack or pack["status"] != db_cdn.STATUS_ACTIVE:
        return body
    import aiohttp
    from bot.services.vpn_api import get_client
    client = await get_client(int(key["server_id"]))
    url = await client.build_subscription_url(cdn_sub_id(key["sub_id"]))
    if not url:
        return body
    async with session_factory() as session:
        async with session.get(url, timeout=aiohttp.ClientTimeout(total=8), headers=forward_headers) as resp:
            if resp.status != 200:
                logger.info("CDN: подписка CDN-клиента вернула %s для ключа %s", resp.status, key.get("id"))
                return body
            extra = await resp.read()
    return merge_subscription_bodies(body, extra)


def is_cdn_offered_for_key(key_id: int) -> bool:
    """Показывать ли клиенту кнопку CDN: CDN настроен и цена задана, либо пакет уже есть."""
    pack = db_cdn.get_pack(key_id)
    if pack and pack["status"] in (
        db_cdn.STATUS_ACTIVE, db_cdn.STATUS_EXHAUSTED, db_cdn.STATUS_SUSPENDED, db_cdn.STATUS_EXPIRED,
    ):
        return True
    return get_sale_price_cents() > 0


# ------------------------------------------------------------- panel level --

async def _server_context(server_id: int):
    from bot.services.vpn_api import get_client, get_client_subscription_inbounds
    client = await get_client(server_id)
    inbounds = await get_client_subscription_inbounds(client, include_ignored=True)
    return client, inbounds, [i for i in inbounds if is_cdn_inbound(i)]


async def _remove_panel_clients(client, inbounds: List[Dict[str, Any]], email: str) -> int:
    from bot.services.vpn_api import _delete_client_from_snapshot, _parse_clients_by_email
    removed = 0
    for inbound_id, cl in sorted(_parse_clients_by_email(inbounds, email).items()):
        uid = cl.get("id") or cl.get("password")
        if not uid:
            continue
        try:
            await _delete_client_from_snapshot(
                client, None, inbound_id=inbound_id, client_uuid=uid, email=email,
            )
            removed += 1
        except Exception as e:  # noqa: BLE001
            logger.warning("CDN: не удалось удалить клиента %s из inbound %s: %s", email, inbound_id, e)
    return removed


def _limit_ip_for_key(key: Dict[str, Any]) -> int:
    try:
        if key.get("tariff_id"):
            from database.db_tariffs import get_tariff_by_id
            tariff = get_tariff_by_id(key["tariff_id"])
            if tariff and tariff.get("max_ips") is not None:
                return int(tariff["max_ips"])
    except Exception as e:  # noqa: BLE001
        logger.debug("CDN: limitIp тарифа недоступен: %s", e)
    return 1


def _validate_key(key: Optional[Dict[str, Any]]) -> Optional[str]:
    if not key:
        return "Ключ не найден"
    if not key.get("sub_id"):
        return "У ключа нет подписки: CDN доступен только для подписок"
    if not key.get("server_id") or not key.get("server_active") or not key.get("panel_email"):
        return "Ключ не настроен на сервере"
    return None


async def _provision(key: Dict[str, Any], *, total_gb: int, expire_days: int) -> Dict[str, Any]:
    """Пересоздаёт CDN-клиента на панели с нуля (счётчик трафика обнуляется)."""
    from bot.services.vpn_api import _add_client_from_snapshot
    client, inbounds, cdn_inbounds = await _server_context(int(key["server_id"]))
    if not cdn_inbounds:
        return {"ok": False, "error": "На сервере нет CDN-инбаунда: проверьте настройки CDN"}
    email = cdn_email(key["panel_email"])
    await _remove_panel_clients(client, inbounds, email)
    limit_ip = _limit_ip_for_key(key)
    added, errors = 0, []
    for inbound in cdn_inbounds:
        try:
            flow = ""
            try:
                flow = await client.get_inbound_flow(inbound["id"]) or ""
            except Exception:  # noqa: BLE001
                flow = ""
            await _add_client_from_snapshot(
                client, None,
                inbound_id=inbound["id"], email=email, total_gb=int(total_gb),
                expire_days=int(expire_days), limit_ip=limit_ip, enable=True,
                tg_id=str(key.get("telegram_id") or ""), flow=flow, sub_id=cdn_sub_id(key["sub_id"]),
            )
            added += 1
        except Exception as e:  # noqa: BLE001
            errors.append(str(e))
            logger.warning("CDN: не удалось создать клиента %s в inbound %s: %s", email, inbound.get("id"), e)
    if not added:
        logger.warning("CDN: панель не приняла клиента %s: %s", email, "; ".join(errors)[:300])
        return {"ok": False, "error": "Сервер не принял подключение CDN. Попробуйте позже"}
    return {"ok": True, "added": added}


async def _unprovision(key: Dict[str, Any]) -> int:
    client, inbounds, _ = await _server_context(int(key["server_id"]))
    return await _remove_panel_clients(client, inbounds, cdn_email(key["panel_email"]))


async def get_pack_usage(key: Dict[str, Any]) -> Optional[int]:
    """Фактически израсходовано (байт) по CDN-клиенту панели, None — данных нет."""
    from bot.services.vpn_api import get_client
    client = await get_client(int(key["server_id"]))
    email = cdn_email(key["panel_email"])
    try:
        stats = await client.get_client_stats(email, resolve_inbound=False)
    except TypeError:
        stats = await client.get_client_stats(email)
    if not stats:
        return None
    return int(stats.get("up", 0) or 0) + int(stats.get("down", 0) or 0)


# ------------------------------------------------------------- operations --

async def activate_pack(
    key_id: int,
    *,
    gb: Optional[int] = None,
    days: Optional[int] = None,
    is_free: bool = False,
) -> Dict[str, Any]:
    """Выдаёт/продлевает пакет: клиент панели создаётся заново, счётчик с нуля."""
    from database.db_keys import get_vpn_key_by_id
    gb = int(gb if gb is not None else get_cdn_pack_gb())
    days = int(days if days is not None else get_cdn_pack_days())
    if gb <= 0 or days <= 0:
        return {"ok": False, "error": "Объём и срок должны быть больше нуля"}
    async with _key_locks[key_id]:
        key = get_vpn_key_by_id(key_id)
        problem = _validate_key(key)
        if problem:
            return {"ok": False, "error": problem}
        result = await _provision(key, total_gb=gb, expire_days=days)
        if not result["ok"]:
            return result
        expires_at = datetime.utcnow() + timedelta(days=days)
        db_cdn.save_pack(
            key_id, user_id=key.get("user_id"), server_id=key.get("server_id"),
            panel_email=key["panel_email"], limit_bytes=gb * GB, expires_at=expires_at,
            is_free=is_free,
        )
        return {"ok": True, "gb": gb, "days": days, "expires_at": expires_at}


async def set_pack_volume(key_id: int, gb: int) -> Dict[str, Any]:
    """Меняет объём действующего пакета (срок прежний, счётчик с нуля на новый объём)."""
    from database.db_keys import get_vpn_key_by_id
    gb = int(gb)
    if gb <= 0:
        return {"ok": False, "error": "Объём должен быть больше нуля"}
    async with _key_locks[key_id]:
        pack = db_cdn.get_pack(key_id)
        key = get_vpn_key_by_id(key_id)
        problem = _validate_key(key)
        if problem:
            return {"ok": False, "error": problem}
        if not pack:
            return {"ok": False, "error": "У ключа нет пакета CDN"}
        expires = db_cdn.parse_time(pack["expires_at"]) or datetime.utcnow()
        remaining = expires - datetime.utcnow()
        if remaining.total_seconds() <= 0:
            return {"ok": False, "error": "Срок пакета уже вышел: выдайте новый"}
        days_left = max(1, math.ceil(remaining.total_seconds() / 86400))
        result = await _provision(key, total_gb=gb, expire_days=days_left)
        if not result["ok"]:
            return result
        db_cdn.save_pack(
            key_id, user_id=key.get("user_id"), server_id=key.get("server_id"),
            panel_email=key["panel_email"], limit_bytes=gb * GB, expires_at=expires,
            is_free=bool(pack.get("is_free")),
        )
        return {"ok": True, "gb": gb}


async def deactivate_pack(key_id: int, status: str = db_cdn.STATUS_REVOKED) -> Dict[str, Any]:
    """Снимает CDN-клиента с панели и ставит пакету статус."""
    from database.db_keys import get_vpn_key_by_id
    async with _key_locks[key_id]:
        pack = db_cdn.get_pack(key_id)
        key = get_vpn_key_by_id(key_id)
        if key and key.get("server_id") and key.get("panel_email") and key.get("server_active"):
            try:
                if status in (db_cdn.STATUS_ACTIVE, db_cdn.STATUS_SUSPENDED) and pack:
                    used = await get_pack_usage(key)
                    if used is not None:
                        db_cdn.update_pack(key_id, used_bytes=used)
            except Exception as e:  # noqa: BLE001
                logger.debug("CDN: расход перед снятием недоступен: %s", e)
            try:
                await _unprovision(key)
            except Exception as e:  # noqa: BLE001
                logger.warning("CDN: не удалось снять клиента ключа %s: %s", key_id, e)
                return {"ok": False, "error": str(e)}
        if pack:
            db_cdn.update_pack(key_id, status=status)
        return {"ok": True}


async def restore_suspended_pack(key_id: int) -> Dict[str, Any]:
    """Возвращает приостановленный пакет с остатком ГБ и прежним сроком."""
    from database.db_keys import get_vpn_key_by_id
    async with _key_locks[key_id]:
        pack = db_cdn.get_pack(key_id)
        key = get_vpn_key_by_id(key_id)
        if not pack or pack["status"] != db_cdn.STATUS_SUSPENDED:
            return {"ok": False, "error": "Пакет не приостановлен"}
        problem = _validate_key(key)
        if problem:
            return {"ok": False, "error": problem}
        expires = db_cdn.parse_time(pack["expires_at"]) or datetime.utcnow()
        remaining_seconds = (expires - datetime.utcnow()).total_seconds()
        remaining_bytes = int(pack["limit_bytes"]) - int(pack["used_bytes"] or 0)
        if remaining_seconds <= 0 or remaining_bytes <= 0:
            db_cdn.update_pack(key_id, status=db_cdn.STATUS_EXPIRED if remaining_seconds <= 0 else db_cdn.STATUS_EXHAUSTED)
            return {"ok": False, "error": "Пакет закончился"}
        result = await _provision(
            key, total_gb=max(1, math.ceil(remaining_bytes / GB)),
            expire_days=max(1, math.ceil(remaining_seconds / 86400)),
        )
        if result["ok"]:
            db_cdn.update_pack(key_id, status=db_cdn.STATUS_ACTIVE)
        return result


# ------------------------------------------------------------- user level --

_buy_locks: Dict[int, asyncio.Lock] = defaultdict(asyncio.Lock)


def validate_key_for_purchase(key: Optional[Dict[str, Any]]) -> Optional[str]:
    """Покупать пакет можно только для рабочего, не просроченного ключа не забаненного владельца."""
    problem = _validate_key(key)
    if problem:
        return problem
    if key.get("is_banned"):
        return "Аккаунт заблокирован"
    expires = db_cdn.parse_time(key.get("expires_at")) if key.get("expires_at") else None
    if expires and expires < datetime.utcnow():
        return "Подписка ключа истекла: сначала продлите её"
    return None


async def purchase_pack(key_id: int, user_id: int) -> Dict[str, Any]:
    """Покупка пакета с баланса: списание → выдача; при ЛЮБОМ сбое выдачи деньги возвращаются.
    Параллельные покупки на один ключ выстраиваются в очередь (двойной клик не списывает дважды)."""
    from bot.services.balance import credit_user_balance, debit_user_balance
    from database.db_keys import get_vpn_key_by_id
    price = get_cdn_price_cents()
    if price <= 0:
        return {"ok": False, "error": "Покупка CDN сейчас недоступна"}
    if get_sale_price_cents() <= 0:
        return {"ok": False, "error": "Покупка CDN сейчас недоступна"}
    gb, days = get_cdn_pack_gb(), get_cdn_pack_days()
    async with _buy_locks[key_id]:
        key = get_vpn_key_by_id(key_id)
        if key and int(key.get("user_id") or 0) != int(user_id):
            return {"ok": False, "error": "Ключ не найден"}
        problem = validate_key_for_purchase(key)
        if problem:
            return {"ok": False, "error": problem}
        debit = await debit_user_balance(
            user_id, price, source="cdn_pack", reason=f"Пакет CDN {gb} ГБ на {days} дн.",
            reference_type="cdn_pack", reference_id=str(key_id),
        )
        if not debit.get("ok"):
            if debit.get("status") == "insufficient_funds":
                return {"ok": False, "error": "insufficient_funds", "need": price}
            return {"ok": False, "error": "Не удалось списать оплату"}
        try:
            result = await activate_pack(key_id, gb=gb, days=days, is_free=False)
        except Exception as e:  # noqa: BLE001
            logger.error("CDN: выдача пакета упала, key=%s: %s", key_id, e)
            result = {"ok": False, "error": "Сервер сейчас недоступен. Попробуйте позже"}
        if not result["ok"]:
            try:
                await credit_user_balance(
                    user_id, price, source="cdn_pack_refund", reason="Возврат: пакет CDN не выдан",
                    reference_type="cdn_pack", reference_id=str(key_id),
                )
            except Exception as e:  # noqa: BLE001
                logger.error("CDN: не удалось вернуть оплату user=%s key=%s: %s", user_id, key_id, e)
            return {"ok": False, "error": result["error"], "refunded": True}
        result["price"] = price
        return result


def describe_pack(pack: Optional[Dict[str, Any]]) -> str:
    """Текст статуса пакета для клиента и админа (HTML)."""
    from bot.services.vpn_api import format_traffic
    if not pack:
        return "Пакет не подключён"
    expires = db_cdn.parse_time(pack["expires_at"])
    until = expires.strftime("%d.%m.%Y %H:%M") + " UTC" if expires else "?"
    limit, used = int(pack["limit_bytes"]), int(pack["used_bytes"] or 0)
    status = {
        db_cdn.STATUS_ACTIVE: "🟢 Активен",
        db_cdn.STATUS_EXHAUSTED: "🔴 Объём исчерпан",
        db_cdn.STATUS_EXPIRED: "🔴 Срок вышел",
        db_cdn.STATUS_SUSPENDED: "⏸ Приостановлен (ключ неактивен)",
        db_cdn.STATUS_REVOKED: "⛔ Отключён",
    }.get(pack["status"], pack["status"])
    lines = [
        f"Статус: {status}",
        f"Объём: {format_traffic(used)} из {format_traffic(limit)}",
        f"Действует до: {until}",
    ]
    if pack.get("is_free"):
        lines.append("🎁 Выдан администратором")
    return "\n".join(lines)


# -------------------------------------------------------------- scheduler --

async def _notify(bot, key: Dict[str, Any], text: str) -> None:
    from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
    telegram_id = key.get("telegram_id")
    if not telegram_id:
        return
    key_id = key.get("id")
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🌐 Управление CDN", callback_data=f"key_cdn:{key_id}")],
        [InlineKeyboardButton(text="🈴 На главную", callback_data="start")],
    ])
    try:
        await bot.send_message(int(telegram_id), text, reply_markup=kb, parse_mode="HTML")
    except Exception as e:  # noqa: BLE001
        logger.debug("CDN: уведомление пользователю %s не доставлено: %s", telegram_id, e)


async def process_cdn_packs(bot) -> Dict[str, int]:
    """Раз в час: срок, расход, приостановка при неактивном ключе, уведомления."""
    from database.db_keys import get_vpn_key_by_id, is_key_active
    stats = {"expired": 0, "exhausted": 0, "suspended": 0, "restored": 0, "notified": 0, "errors": 0}
    for pack in db_cdn.list_packs([db_cdn.STATUS_ACTIVE, db_cdn.STATUS_SUSPENDED]):
        key_id = int(pack["vpn_key_id"])
        try:
            key = get_vpn_key_by_id(key_id)
            if not key:
                # ключ удалён — снимаем CDN-клиента по данным пакета и убираем пакет
                if pack.get("server_id") and pack.get("panel_email"):
                    await _unprovision({"server_id": pack["server_id"], "panel_email": pack["panel_email"]})
                db_cdn.delete_pack(key_id)
                continue
            if not key.get("server_id"):
                continue
            expires = db_cdn.parse_time(pack["expires_at"])
            if expires and expires <= datetime.utcnow():
                await deactivate_pack(key_id, db_cdn.STATUS_EXPIRED)
                stats["expired"] += 1
                await _notify(bot, key, "⏰ <b>Пакет CDN закончился по сроку.</b>\nОбычные ключи подписки работают как раньше.")
                stats["notified"] += 1
                continue
            key_ok = is_key_active(key) and not key.get("is_banned")
            if pack["status"] == db_cdn.STATUS_SUSPENDED:
                if key_ok:
                    res = await restore_suspended_pack(key_id)
                    if res.get("ok"):
                        stats["restored"] += 1
                continue
            if not key_ok:
                await deactivate_pack(key_id, db_cdn.STATUS_SUSPENDED)
                stats["suspended"] += 1
                continue
            used = await get_pack_usage(key)
            if used is None:
                continue
            limit = int(pack["limit_bytes"])
            db_cdn.update_pack(key_id, used_bytes=used)
            if limit > 0 and used >= limit:
                await deactivate_pack(key_id, db_cdn.STATUS_EXHAUSTED)
                db_cdn.update_pack(key_id, used_bytes=limit)
                stats["exhausted"] += 1
                await _notify(bot, key, "📦 <b>Пакет CDN исчерпан.</b>\nОбычные ключи подписки работают как раньше. Докупить пакет можно на экране CDN.")
                stats["notified"] += 1
            elif limit > 0 and used * 100 // limit >= NOTIFY_PCT and int(pack["notified_pct"] or 0) < NOTIFY_PCT:
                db_cdn.update_pack(key_id, notified_pct=NOTIFY_PCT)
                await _notify(bot, key, f"⚠️ <b>Пакет CDN почти закончился.</b>\nИспользовано {used * 100 // limit}%.")
                stats["notified"] += 1
        except Exception as e:  # noqa: BLE001
            stats["errors"] += 1
            logger.error("CDN: ошибка обработки пакета ключа %s: %s", key_id, e)
    return stats


async def run_cdn_scheduler(bot) -> None:
    """Фоновая задача: раз в час обрабатывает CDN-пакеты."""
    from bot.services.panel_sync_coordinator import panel_sync_coordinator
    logger.info("🌐 Планировщик CDN-пакетов запущен (раз в час)")
    await asyncio.sleep(90)
    while True:
        try:
            if db_cdn.list_packs([db_cdn.STATUS_ACTIVE, db_cdn.STATUS_SUSPENDED]):
                async with panel_sync_coordinator.regular():
                    stats = await process_cdn_packs(bot)
                if any(stats.values()):
                    logger.info("🌐 CDN-пакеты: %s", stats)
            await asyncio.sleep(3600)
        except asyncio.CancelledError:
            logger.info("Планировщик CDN-пакетов остановлен")
            break
        except Exception as e:  # noqa: BLE001
            logger.error("Ошибка планировщика CDN-пакетов: %s", e)
            await asyncio.sleep(300)
