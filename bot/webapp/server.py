"""
WebApp сервер для VPN-бота.

aiohttp веб-сервер, который:
- Раздаёт index.html (frontend WebApp)
- Предоставляет REST API (/api/keys, /api/status, /api/ping, /api/rename, /api/delete, /api/referral)
- Аутентифицирует запросы через проверку Telegram initData HMAC-SHA256

Запускается параллельно с aiogram polling через asyncio.
"""
import json
import html as _html_module
import logging
import os
import base64
import hmac
import hashlib
import secrets as _secrets_mod
from datetime import datetime
import asyncio
from typing import Optional, Dict, Any

from aiohttp import web

from bot.services.vpn_api import get_subscription_url_for_key

logger = logging.getLogger(__name__)

# Путь к директории с шаблонами (index.html)
_TEMPLATES_DIR = os.path.join(os.path.dirname(__file__), "templates")
_STATIC_DIR = os.path.join(os.path.dirname(__file__), "static")

# Кэш последней успешно полученной от панели подписки (Happ/INCY),
# ключ — sub_id. In-memory, живёт до перезапуска процесса — если панель
# временно недоступна (таймауты, сетевые проблемы на её стороне), клиент
# получает последнее рабочее содержимое вместо голой ошибки 502.
# См. handle_happ_subscription().
_HAPP_SUB_CACHE: dict = {}

# v1.188: заголовки, которые понимает только Happ (панель присылает их всем клиентам).
# В режиме «INCY: только свои заголовки» (/sub_mode incy strict) они убираются из ответа INCY.
_INCY_STRIP_HEADERS = (
    "ping-type", "tun-type", "http-auth-mode", "socks-auth-mode", "color-profile",
    "exclude-apns-enable", "routing-enable", "hide-settings", "providerid",
    "subscription-autoconnect", "subscription-autoconnect-type",
    "subscription-ping-onopen-enabled", "subscriptions-sort-type",
    "subscription-auto-update-enable", "notification-subs-expire",
    "sub-expire", "sub-expire-button-link",
    "sub-info-text", "sub-info-color", "sub-info-button-text", "sub-info-button-link",
)


# ============================================================
# Аутентификация: проверка Telegram initData через aiogram
# ============================================================

from bot.utils.payment_text import public_description as _pub  # v1.148 duration

def _validate_init_data(init_data: str, bot_token: str) -> Optional[int]:
    """
    Проверяет подлинность Telegram initData через встроенную проверку aiogram.

    Использует aiogram.utils.web_app.check_webapp_signature для HMAC-SHA256
    валидации и parse_webapp_init_data для извлечения user.id.

    Returns:
        telegram_id пользователя (int) при успехе, None при провале.
    """
    from aiogram.utils.web_app import (
        check_webapp_signature,
        parse_webapp_init_data,
    )

    try:
        if not check_webapp_signature(bot_token, init_data):
            logger.warning("WebApp: initData signature invalid — access denied")
            logger.debug(f"WebApp DEBUG: init_data_len={len(init_data)}")
            return None

        data = parse_webapp_init_data(init_data)
        # v1.157: initData старше 48 часов не принимаем (защита от повторного использования)
        try:
            import time as _t
            _auth_ts = data.auth_date.timestamp()
            if _t.time() - _auth_ts > 48 * 3600:
                logger.warning("WebApp: initData устарел (auth_date старше 48 часов) — доступ отклонён")
                return None
        except Exception:
            pass
        # parse_webapp_init_data возвращает объект WebAppInitData, не словарь
        user = data.user
        if user:
            return int(user.id)
        return None

    except Exception as e:
        logger.error(f"WebApp: initData validation error: {e}")
        return None


# v1.157: простой лимитер запросов в памяти процесса
_RL_BUCKETS: Dict[str, list] = {}


def _rl_prune(key: str, window: float) -> list:
    import time as _t
    now = _t.time()
    items = [t for t in _RL_BUCKETS.get(key, []) if now - t < window]
    if items:
        _RL_BUCKETS[key] = items
    else:
        _RL_BUCKETS.pop(key, None)
    if len(_RL_BUCKETS) > 5000:
        for k in [k for k, v in _RL_BUCKETS.items() if not v or now - v[-1] > 3600]:
            _RL_BUCKETS.pop(k, None)
    return items


def _rl_allowed(key: str, limit: int, window: float) -> bool:
    return len(_rl_prune(key, window)) < limit


def _rl_record(key: str) -> None:
    import time as _t
    _RL_BUCKETS.setdefault(key, []).append(_t.time())


def _get_telegram_id(request: web.Request) -> Optional[int]:
    """
    Извлекает и проверяет initData из запроса (query param или header).
    Возвращает telegram_id или None.
    """
    from config import BOT_TOKEN

    init_data = request.query.get("initData") or request.headers.get(
        "X-Init-Data", ""
    )
    if init_data:
        return _validate_init_data(init_data, BOT_TOKEN)
    token = request.query.get("token")
    if token:
        from bot.utils.webtoken import verify_token
        return verify_token(token, BOT_TOKEN)
    return None


# ============================================================
# API handlers
# ============================================================
async def handle_language(request: web.Request) -> web.Response:
    """GET /api/language — язык интерфейса пользователя WebApp (ru/en),
    определяется по telegram_id из уже проверенного initData."""
    telegram_id = _get_telegram_id(request)
    if not telegram_id:
        return web.json_response({"error": "unauthorized"}, status=401)

    from database.requests import get_user_language

    return web.json_response({"language": get_user_language(telegram_id)})

def _format_traffic(used: int, limit: int) -> Dict[str, Any]:
    """Форматирует трафик для фронтенда."""
    def human(b):
        if b < 1024:
            return f"{b} B"
        elif b < 1024 ** 2:
            return f"{b / 1024:.1f} KB"
        elif b < 1024 ** 3:
            return f"{b / 1024 ** 2:.1f} MB"
        elif b < 1024 ** 4:
            return f"{b / 1024 ** 3:.2f} GB"
        return f"{b / 1024 ** 4:.2f} TB"

    return {
        "used_human": human(used),
        "limit_human": "∞" if limit == 0 else human(limit),
        "is_unlimited": limit == 0,
        "used_bytes": used,
        "limit_bytes": limit,
        "percent": 0 if limit == 0 else min(100, round(used / limit * 100, 1)),
    }


def _format_expiry(expires_at: str) -> Dict[str, Any]:
    """Форматирует дату окончания для фронтенда."""
    try:
        dt = datetime.fromisoformat(expires_at.replace(" ", "T"))
        now = datetime.utcnow()
        is_active = dt > now
        delta = dt - now
        days_left = max(0, delta.days)

        if days_left == 0:
            if delta.total_seconds() > 0:
                remaining = "Истекает сегодня"
            else:
                remaining = "Истёк"
        elif days_left <= 30:
            remaining = f"{days_left} дн."
        else:
            remaining = dt.strftime("%d.%m.%Y")

        return {
            "date": dt.strftime("%d.%m.%Y"),
            "time": dt.strftime("%H:%M"),
            "is_active": is_active,
            "days_left": days_left,
            "remaining_human": remaining,
        }
    except Exception:
        return {
            "date": expires_at or "—",
            "time": "",
            "is_active": False,
            "days_left": 0,
            "remaining_human": expires_at or "—",
        }



async def _measure_ping(host: str, port: int, timeout: float = 5.0) -> Optional[int]:
    """Измеряет задержку TCP-соединения с VPN-сервером в миллисекундах.
    DNS резолвится отдельно, чтобы измерять только сетевую задержку."""
    loop = asyncio.get_event_loop()
    ip = None
    # 1) Resolve DNS outside timing window
    try:
        infos = await asyncio.wait_for(loop.getaddrinfo(host, port), timeout=3.0)
        if infos:
            ip = infos[0][4][0]
    except Exception:
        pass
    if not ip:
        ip = host  # fallback: maybe already an IP
    # 2) Measure pure TCP connect (no DNS overhead)
    try:
        start = loop.time()
        _, writer = await asyncio.wait_for(
            asyncio.open_connection(ip, port), timeout=timeout
        )
        writer.close()
        await writer.wait_closed()
        elapsed = loop.time() - start
        return int(elapsed * 1000)
    except (asyncio.TimeoutError, OSError, Exception):
        return None


async def handle_ping(request: web.Request) -> web.Response:
    """GET /api/ping — задержка до VPN-сервера для каждого ключа."""
    telegram_id = _get_telegram_id(request)
    if not telegram_id:
        return web.json_response({"error": "unauthorized"}, status=401)

    try:
        from database.db_keys import get_user_keys_for_display
        from database.connection import get_connection

        keys = get_user_keys_for_display(telegram_id)
        server_ids = set(k.get("server_id") for k in keys if k.get("server_id"))

        pings = {}
        conn = get_connection()
        try:
            for sid in server_ids:
                row = conn.execute(
                    "SELECT host, port FROM servers WHERE id=?", (sid,)
                ).fetchone()
                if row:
                    # 3 замера, берём медиану для стабильности
                    samples = []
                    for _ in range(3):
                        s = await _measure_ping(row["host"], row["port"])
                        if s is not None:
                            samples.append(s)
                    if samples:
                        samples.sort()
                        pings[str(sid)] = samples[len(samples) // 2]  # median
                    else:
                        pings[str(sid)] = None
        finally:
            conn.close()

        return web.json_response({"pings": pings})

    except Exception as e:
        logger.error(f"WebApp /api/ping error: {e}", exc_info=True)
        return web.json_response({"error": "internal_error"}, status=500)


async def handle_key_inbounds(request: web.Request) -> web.Response:
    """GET /api/key/{key_id}/inbounds — детальный список отдельных
    подключений (inbound) ключа, сгруппированных по хосту, с реальным
    пингом каждого. Используется для разворачиваемого блока "Все
    подключения" в WebApp."""
    telegram_id = _get_telegram_id(request)
    if not telegram_id:
        return web.json_response({"error": "unauthorized"}, status=401)

    try:
        key_id = int(request.match_info["key_id"])
    except (KeyError, ValueError):
        return web.json_response({"error": "invalid_key_id"}, status=400)

    from database.requests import get_key_details_for_user
    key = get_key_details_for_user(key_id, telegram_id)
    if not key:
        return web.json_response({"error": "not_found"}, status=404)

    try:
        from bot.services.vpn_api import get_client
        from bot.utils.inbound_links import parse_and_group_inbound_links, add_ping_to_groups
        client = await get_client(key["server_id"])
        raw = await client.get_subscription_link(key["sub_id"])
        groups = parse_and_group_inbound_links(raw)
        groups = await add_ping_to_groups(groups)
        return web.json_response({"groups": groups})
    except Exception as e:
        logger.warning(f"handle_key_inbounds: ошибка для ключа {key_id}: {e}")
        return web.json_response({"error": "internal_error"}, status=500)


# v1.152 trial notice
def _trial_tariff_ids() -> set:
    ids = set()
    try:
        from database.requests import get_trial_tariff_id, get_groups_with_trial
        t = get_trial_tariff_id()
        if t:
            ids.add(int(t))
        for g in get_groups_with_trial():
            if g.get("trial_tariff_id"):
                ids.add(int(g["trial_tariff_id"]))
    except Exception:
        pass
    return ids


def _is_trial_key(key) -> bool:
    try:
        tid = key.get("tariff_id")
        return bool(tid) and int(tid) in _trial_tariff_ids()
    except Exception:
        return False


async def handle_keys(request: web.Request) -> web.Response:
    """GET /api/keys — возвращает список ключей пользователя."""
    telegram_id = _get_telegram_id(request)
    if not telegram_id:
        return web.json_response(
            {"error": "unauthorized"}, status=401
        )

    try:
        from database.db_keys import get_user_keys_for_display

        keys = get_user_keys_for_display(telegram_id)
        result = []

        for key in keys:
            traffic = _format_traffic(
                key.get("traffic_used", 0), key.get("traffic_limit", 0)
            )
            expiry = _format_expiry(key.get("expires_at", ""))

            # Build real subscription URL from panel settings
            sub_url = None
            try:
                from bot.services.vpn_api import get_public_subscription_url_for_key
                sub_url = await get_public_subscription_url_for_key(key)
            except Exception:
                sub_url = None

            result.append({
                "id": key["id"],
                "name": key.get("display_name", f"Ключ #{key['id']}"),
                "server": key.get("server_name", "—"),
                "protocol": "VLESS",
                "traffic": traffic,
                "sub_id": key.get("sub_id"),
                "sub_url": sub_url,
                "server_id": key.get("server_id"),
                "expiry": expiry,
                "is_active": key.get("is_active", 0) == 1,
                "is_trial": _is_trial_key(key),
            })

        return web.json_response({"keys": result})

    except Exception as e:
        logger.error(f"WebApp /api/keys error: {e}", exc_info=True)
        return web.json_response(
            {"error": "internal_error"}, status=500
        )


async def handle_status(request: web.Request) -> web.Response:
    """GET /api/status — сводка по всем ключам пользователя."""
    telegram_id = _get_telegram_id(request)
    if not telegram_id:
        return web.json_response(
            {"error": "unauthorized"}, status=401
        )

    try:
        from database.db_keys import get_user_keys_for_display

        keys = get_user_keys_for_display(telegram_id)

        active_keys = [k for k in keys if k.get("is_active", 1) == 1]
        total_used = sum(k.get("traffic_used", 0) for k in keys)
        total_limit = sum(
            k.get("traffic_limit", 0)
            for k in keys
            if k.get("traffic_limit", 0) > 0
        )

        nearest_expiry = None
        for k in active_keys:
            exp = k.get("expires_at", "")
            if exp and (nearest_expiry is None or exp < nearest_expiry):
                nearest_expiry = exp

        nearest = _format_expiry(nearest_expiry) if nearest_expiry else None

        # Get bot username from the running bot instance (set at main.py startup)
        from bot.utils.runtime_state import get_bot_username
        bot_username = get_bot_username() or None

        # Проверка доступности пробной подписки
        trial_available = False
        try:
            from database.db_settings import is_trial_enabled, get_trial_tariff_id
            from database.db_users import has_used_trial
            trial_available = (
                is_trial_enabled()
                and get_trial_tariff_id() is not None
                and not has_used_trial(telegram_id)
            )
        except Exception:
            pass

        return web.json_response({
            "total_keys": len(keys),
            "active_keys": len(active_keys),
            "traffic_total_used": _format_traffic(total_used, 0)["used_human"],
            "nearest_expiry": nearest,
            "has_keys": len(keys) > 0,
            "bot_username": bot_username,
            "trial_available": trial_available,
        })

    except Exception as e:
        logger.error(f"WebApp /api/status error: {e}", exc_info=True)
        return web.json_response(
            {"error": "internal_error"}, status=500
        )


async def handle_ai_consult(request: web.Request) -> web.Response:
    """POST /api/ai-consult — доверенный прокси к внутреннему AI-сервису.

    Браузер/WebApp никогда не видит SUPPORT_API_TOKEN и не может подставить
    чужой telegram_id — он всегда берётся из уже проверенного initData/token.
    Body JSON: {"message": "текст вопроса"}
    """
    telegram_id = _get_telegram_id(request)
    if not telegram_id:
        return web.json_response({"error": "unauthorized"}, status=401)
    try:
        data = await request.json()
    except json.JSONDecodeError:
        return web.json_response({"error": "invalid_json"}, status=400)

    from bot.services.license import is_feature_available as _lic_ok
    if not _lic_ok("ai_assistant"):
        return web.json_response(
            {"error": "feature_unavailable", "message": "AI-помощник недоступен."}, status=403,
        )

    message = (data.get("message") or "").strip()
    image_base64 = data.get("image_base64")
    if not message and not image_base64:
        return web.json_response({"error": "empty_message"}, status=400)
    if len(message) > 2000:
        return web.json_response({"error": "message_too_long"}, status=400)
    if image_base64 and len(image_base64) > 11 * 1024 * 1024:  # ~8 МБ бинарных данных с запасом на base64-накладные расходы
        return web.json_response({"error": "image_too_large"}, status=400)

    import aiohttp
    from config import SUPPORT_API_TOKEN

    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(
                "http://127.0.0.1:8086/consult",
                json={"user_id": telegram_id, "message": message, "image_base64": image_base64},
                headers={"X-Support-Token": SUPPORT_API_TOKEN},
                timeout=aiohttp.ClientTimeout(total=40),
            ) as resp:
                if resp.status != 200:
                    logger.warning(f"AI consult proxy: upstream вернул {resp.status}")
                    return web.json_response({"error": "ai_unavailable"}, status=502)
                payload = await resp.json()

                if payload.get("escalate"):
                    try:
                        from aiogram import Bot
                        from config import BOT_TOKEN
                        from database.requests import get_or_create_user, create_support_thread, record_support_message
                        from bot.services.support import send_ai_escalation_to_admins
                        from bot.utils.text import escape_html

                        escalation_text = message or "[прислал скриншот]"
                        esc_user, _ = get_or_create_user(telegram_id)
                        esc_thread = create_support_thread(telegram_id, initiator_type="user")
                        if esc_thread:
                            record_support_message(
                                esc_thread["id"],
                                sender_type="user",
                                sender_telegram_id=telegram_id,
                                recipient_telegram_id=esc_thread.get("assigned_admin_id"),
                                text_html=escape_html(escalation_text),
                                media_type="text",
                                media_file_id=None,
                                source_chat_id=telegram_id,
                                source_message_id=0,
                            )
                            escalation_bot = Bot(token=BOT_TOKEN)
                            try:
                                await send_ai_escalation_to_admins(
                                    escalation_bot,
                                    thread=esc_thread,
                                    user=esc_user,
                                    question=escalation_text,
                                    ai_reply=payload.get("reply", ""),
                                )
                            finally:
                                await escalation_bot.session.close()
                        else:
                            logger.warning(f"WebApp AI escalation: не удалось создать тред для {telegram_id}")
                    except Exception as e:
                        logger.error(f"WebApp AI escalation error: {e}")

                return web.json_response(payload)
    except asyncio.TimeoutError:
        return web.json_response({"error": "ai_timeout"}, status=504)
    except Exception as e:
        logger.error(f"AI consult proxy error: {e}")
        return web.json_response({"error": "ai_unavailable"}, status=502)


async def handle_ai_feedback(request: web.Request) -> web.Response:
    """POST /api/ai-feedback — доверенный прокси к внутреннему AI-сервису
    для оценки 👍/👎 конкретного ответа AI.
    Body JSON: {"response_id": "...", "rating": "up" | "down"}
    """
    telegram_id = _get_telegram_id(request)
    if not telegram_id:
        return web.json_response({"error": "unauthorized"}, status=401)
    try:
        data = await request.json()
    except json.JSONDecodeError:
        return web.json_response({"error": "invalid_json"}, status=400)

    response_id = (data.get("response_id") or "").strip()
    rating = (data.get("rating") or "").strip()
    if not response_id or rating not in ("up", "down"):
        return web.json_response({"error": "invalid_request"}, status=400)

    import aiohttp
    from config import SUPPORT_API_TOKEN

    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(
                "http://127.0.0.1:8086/feedback",
                json={"response_id": response_id, "rating": rating},
                headers={"X-Support-Token": SUPPORT_API_TOKEN},
                timeout=aiohttp.ClientTimeout(total=10),
            ) as resp:
                if resp.status != 200:
                    return web.json_response({"error": "ai_unavailable"}, status=502)
                return web.json_response({"status": "ok"})
    except asyncio.TimeoutError:
        return web.json_response({"error": "ai_timeout"}, status=504)
    except Exception as e:
        logger.error(f"AI feedback proxy error: {e}")
        return web.json_response({"error": "ai_unavailable"}, status=502)


async def handle_tariffs_list(request: web.Request) -> web.Response:
    """GET /api/tariffs — список активных тарифов для экрана оплаты в WebApp.

    Опциональный query-параметр vpn_key_id: если передан (продление
    конкретного ключа), в ответе помечается его ТЕКУЩИЙ тариф (is_current),
    чтобы клиент не перепутал его с другим и случайно не понизил подписку.
    """
    telegram_id = _get_telegram_id(request)
    if not telegram_id:
        return web.json_response({"error": "unauthorized"}, status=401)

    from database.db_tariffs import get_all_tariffs

    tariffs = get_all_tariffs(include_hidden=False)

    current_tariff_id = None
    vpn_key_id_raw = request.query.get("vpn_key_id")
    if vpn_key_id_raw:
        try:
            from database.requests import get_vpn_key_by_id
            key = get_vpn_key_by_id(int(vpn_key_id_raw))
            if key:
                current_tariff_id = key.get("tariff_id")
        except (ValueError, TypeError):
            pass

    return web.json_response({
        "tariffs": [
            {
                "id": t["id"],
                "name": t["name"],
                "duration_days": t["duration_days"],
                "price_rub": float(t.get("price_rub") or 0),
                "traffic_limit_gb": t.get("traffic_limit_gb"),
                "is_current": current_tariff_id is not None and t["id"] == current_tariff_id,
            }
            for t in tariffs
        ]
    })


async def handle_pay_create(request: web.Request) -> web.Response:
    """POST /api/pay/create — создаёт заказ на покупку нового ключа или
    продление существующего. По умолчанию — QR-платёж YooKassa; если
    передан payment_method="balance", списывает стоимость с личного
    баланса и завершает заказ сразу, без QR.
    Body JSON: {"tariff_id": int, "vpn_key_id": int | null, "payment_method": "yookassa_qr" | "balance"}
    """
    telegram_id = _get_telegram_id(request)
    if not telegram_id:
        return web.json_response({"error": "unauthorized"}, status=401)
    try:
        data = await request.json()
    except json.JSONDecodeError:
        return web.json_response({"error": "invalid_json"}, status=400)

    tariff_id = data.get("tariff_id")
    vpn_key_id = data.get("vpn_key_id")
    payment_method = data.get("payment_method") or "yookassa_qr"
    if not tariff_id:
        return web.json_response({"error": "tariff_id_required"}, status=400)
    if payment_method not in ("yookassa_qr", "balance"):
        return web.json_response({"error": "invalid_payment_method"}, status=400)

    from database.requests import get_tariff_by_id, get_user_internal_id, create_pending_order, save_yookassa_payment_id
    from bot.services.promotions import prepare_order_pricing
    from bot.services.billing import create_yookassa_qr_payment

    tariff = get_tariff_by_id(int(tariff_id))
    if not tariff:
        return web.json_response({"error": "tariff_not_found"}, status=404)

    user_id = get_user_internal_id(telegram_id)
    if not user_id:
        return web.json_response({"error": "user_not_found"}, status=404)

    if vpn_key_id:
        # v1.155: ключ должен принадлежать этому пользователю
        from database.requests import get_key_details_for_user
        try:
            _owned = get_key_details_for_user(int(vpn_key_id), telegram_id)
        except (TypeError, ValueError):
            return web.json_response({"error": "invalid_key"}, status=400)
        if not _owned:
            return web.json_response({"error": "key_not_found"}, status=404)

    action = "renewal" if vpn_key_id else "new_key"

    if payment_method == "balance":
        debited_cents = 0
        try:
            (_, order_id) = create_pending_order(
                user_id=user_id, tariff_id=tariff["id"], payment_type="balance",
                vpn_key_id=int(vpn_key_id) if vpn_key_id else None,
            )

            quote = prepare_order_pricing(
                order_id=order_id, user_id=user_id, tariff=tariff,
                payment_type="balance", action=action,
            )
            if not quote.get("ok"):
                return web.json_response(
                    {"error": "pricing_unavailable", "message": quote.get("unavailable_reason", "Оплата сейчас недоступна.")},
                    status=400,
                )

            if quote.get("is_free"):
                result = await _complete_webapp_order(order_id, quote_final_amount_cents=0, telegram_id=telegram_id)
                return web.json_response(result)

            from database.requests import get_user_balance
            current_balance = get_user_balance(user_id)
            if current_balance < quote["final_amount"]:
                return web.json_response({
                    "error": "insufficient_balance",
                    "message": "Недостаточно средств на балансе для этого тарифа.",
                    "balance_cents": current_balance,
                    "required_cents": quote["final_amount"],
                }, status=400)

            from bot.services.balance import debit_user_balance, credit_user_balance
            from database.requests import save_payment_balance_deduction
            debit = await debit_user_balance(
                user_id, quote["final_amount"],
                source="payment_balance", reason="Оплата тарифа с баланса (WebApp)",
                reference_type="payment_order", reference_id=order_id,
                metadata={"payment_type": "balance"},
            )
            if not debit.get("ok"):
                if debit.get("status") == "insufficient_funds":
                    return web.json_response({
                        "error": "insufficient_balance",
                        "message": "Недостаточно средств на балансе для этого тарифа.",
                        "balance_cents": debit.get("balance_before", 0),
                        "required_cents": quote["final_amount"],
                    }, status=400)
                return web.json_response({"error": "debit_failed", "message": "Не удалось списать баланс."}, status=502)
            debited_cents = int(quote["final_amount"])
            save_payment_balance_deduction(order_id, debited_cents)

            result = await _complete_webapp_order(
                order_id, quote_final_amount_cents=quote["final_amount"], telegram_id=telegram_id,
            )
            if result.get("status") != "paid":
                await credit_user_balance(
                    user_id, debited_cents, source="payment_balance_refund",
                    reason="Возврат: оплата тарифа не завершена",
                    reference_type="payment_order_refund", reference_id=order_id,
                )
                debited_cents = 0
            return web.json_response(result)
        except Exception as e:
            logger.error(f"WebApp pay/create (balance) error: {e}")
            if debited_cents:
                try:
                    from database.db_payments import is_order_already_paid
                    if not is_order_already_paid(order_id):
                        from bot.services.balance import credit_user_balance as _refund
                        await _refund(
                            user_id, debited_cents, source="payment_balance_refund",
                            reason="Возврат: ошибка при оплате тарифа",
                            reference_type="payment_order_refund", reference_id=order_id,
                        )
                except Exception as refund_err:
                    logger.error(f"WebApp balance refund failed for {order_id}: {refund_err}")
            return web.json_response({"error": "payment_creation_failed"}, status=502)

    try:
        (_, order_id) = create_pending_order(
            user_id=user_id, tariff_id=tariff["id"], payment_type="yookassa_qr",
            vpn_key_id=int(vpn_key_id) if vpn_key_id else None,
        )

        quote = prepare_order_pricing(
            order_id=order_id, user_id=user_id, tariff=tariff,
            payment_type="yookassa_qr", action=action,
        )
        if not quote.get("ok"):
            return web.json_response(
                {"error": "pricing_unavailable", "message": quote.get("unavailable_reason", "Оплата сейчас недоступна.")},
                status=400,
            )

        final_amount_rub = quote["final_amount"] / 100

        if quote.get("is_free"):
            result = await _complete_webapp_order(order_id, quote_final_amount_cents=0, telegram_id=telegram_id)
            return web.json_response(result)

        from aiogram import Bot
        from config import BOT_TOKEN

        pay_bot = Bot(token=BOT_TOKEN)
        try:
            bot_info = await pay_bot.get_me()
            description = _pub(tariff)
            yk_result = await create_yookassa_qr_payment(
                amount_rub=final_amount_rub, order_id=order_id, description=description,
                bot_name=bot_info.username,
            )
        finally:
            await pay_bot.session.close()

        save_yookassa_payment_id(order_id, yk_result["yookassa_payment_id"])

        qr_image_b64 = base64.b64encode(yk_result["qr_image_data"]).decode("ascii")
        qr_image_data_url = f"data:image/png;base64,{qr_image_b64}"

        return web.json_response({
            "order_id": order_id,
            "qr_image_url": qr_image_data_url,
            "qr_url": yk_result["qr_url"],
            "amount_rub": final_amount_rub,
        })
    except Exception as e:
        logger.error(f"WebApp pay/create error: {e}")
        return web.json_response({"error": "payment_creation_failed"}, status=502)


async def _complete_webapp_order(order_id: str, quote_final_amount_cents: int, telegram_id: int) -> dict:
    """Общая логика завершения оплаченного заказа — та же, что использует
    бот (process_payment_order + начисление рефералки), без Telegram-специфичного
    UI-финала (finalize_payment_ui), так как WebApp сам отрисовывает результат."""
    from aiogram import Bot
    from config import BOT_TOKEN
    from bot.services.billing import process_payment_order, _run_payment_post_actions

    complete_bot = Bot(token=BOT_TOKEN)
    try:
        success, text, order = await process_payment_order(order_id, bot=complete_bot, process_referrals=False)
        if success and order:
            await _run_payment_post_actions(
                order, bot=complete_bot, payment_type="yookassa_qr",
                referral_amount=quote_final_amount_cents, balance_override_cents=0,
            )

            # Для НОВОГО ключа (не продления) after-payment создаётся только
            # черновик — сервер ещё не назначен. В обычном боте это доводится
            # через отдельный FSM-флоу выбора сервера (start_new_key_config),
            # который не переносится напрямую в статeless WebApp — поэтому
            # явно сообщаем клиенту, что нужно доделать настройку в чате бота.
            key_id = order.get("vpn_key_id")
            is_draft = False
            if key_id:
                from database.requests import get_key_details_for_user
                key = get_key_details_for_user(key_id, telegram_id)
                if key and not key.get("server_id"):
                    is_draft = True

            return {"status": "paid", "message": text, "is_draft": is_draft}
        return {"status": "failed", "message": text}
    finally:
        await complete_bot.session.close()


async def handle_pay_check(request: web.Request) -> web.Response:
    """POST /api/pay/check — проверяет статус YooKassa QR-платежа и завершает
    заказ (выдача/продление ключа), если оплата прошла.
    Body JSON: {"order_id": "..."}
    """
    telegram_id = _get_telegram_id(request)
    if not telegram_id:
        return web.json_response({"error": "unauthorized"}, status=401)
    try:
        data = await request.json()
    except json.JSONDecodeError:
        return web.json_response({"error": "invalid_json"}, status=400)

    order_id = (data.get("order_id") or "").strip()
    if not order_id:
        return web.json_response({"error": "order_id_required"}, status=400)

    from database.requests import find_order_by_order_id, get_user_internal_id, is_order_already_paid
    from bot.services.billing import check_yookassa_payment_status

    order = find_order_by_order_id(order_id)
    if not order:
        return web.json_response({"error": "order_not_found"}, status=404)

    owner_user_id = get_user_internal_id(telegram_id)
    if not owner_user_id or int(order.get("user_id") or 0) != int(owner_user_id):
        # Не подтверждаем факт существования чужого заказа
        return web.json_response({"error": "order_not_found"}, status=404)

    if order.get("status") == "paid" or is_order_already_paid(order_id):
        return web.json_response({"status": "paid", "already_processed": True})

    payment_id = order.get("yookassa_payment_id")
    if not payment_id:
        return web.json_response({"status": "pending", "message": "Платёж ещё создаётся, попробуйте через пару секунд."})

    try:
        yk_status = await check_yookassa_payment_status(payment_id)
    except Exception as e:
        logger.error(f"WebApp YooKassa status check error: {e}")
        return web.json_response({"status": "pending", "message": "Не удалось проверить статус, попробуйте ещё раз."})

    if yk_status != "succeeded":
        status_map = {"pending": "pending", "waiting_for_capture": "pending", "canceled": "failed"}
        return web.json_response({"status": status_map.get(yk_status, "pending")})

    result = await _complete_webapp_order(order_id, int(order.get("final_amount_cents") or 0), telegram_id)
    return web.json_response(result)


# ============================================================================
# PUBLIC SHOP — покупка через обычный браузер, БЕЗ Telegram-авторизации.
# Для людей без доступа к Telegram/VPN, которым иначе не открыть Mini App.
# Оплата по полной цене (без промокодов/баланса — это только для аккаунтов
# в боте). После оплаты сразу выдаётся рабочий VPN-ключ + код привязки,
# которым потом можно забрать ключ под свой Telegram-аккаунт.
# ============================================================================

_PUBLIC_ORDER_ID_ALPHABET = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz"


def _generate_public_order_id() -> str:
    import secrets as _secrets
    return "pub" + "".join(_secrets.choice(_PUBLIC_ORDER_ID_ALPHABET) for _ in range(8))


_CACHED_APP_COMMIT: Optional[str] = None


def _get_cached_app_commit() -> str:
    """Короткий хеш git-коммита, с которым РЕАЛЬНО запущен этот процесс —
    вычисляется один раз при первом обращении и кэшируется на весь срок
    жизни процесса (не на каждый запрос — git rev-parse не бесплатный).

    Отдаётся заголовком X-App-Commit на страницах /shop и /welcome —
    позволяет за секунду проверить (curl -I или вкладка Network в
    браузере), совпадает ли версия, которая РЕАЛЬНО обслуживает
    запросы, с последним коммитом в git log. Если не совпадает — где-то
    работает другой, не перезапущенный процесс (см. `ps aux | grep
    python` — искать лишние копии main.py/webapp_main.py и т.п.)."""
    global _CACHED_APP_COMMIT
    if _CACHED_APP_COMMIT is None:
        from bot.utils.git_utils import get_current_commit
        _CACHED_APP_COMMIT = get_current_commit() or "unknown"
    return _CACHED_APP_COMMIT


def _serve_html_with_brand(filepath: str, title_format: str, header_format: str) -> web.Response:
    """Отдаёт HTML-файл с названием бренда, подставленным ПРЯМО НА СЕРВЕРЕ —
    в отличие от JS-подстановки через applyBrand() (она остаётся как
    подстраховка), здесь бренд уже правильный в самом первом байте ответа,
    поэтому браузер не успевает на долю секунды показать плейсхолдер
    "ECLIPSE Unlimited" до его замены — раньше это было заметно как
    мимолётное мигание чужого бренда.

    title_format / header_format — Python-шаблоны вида "💎{brand}💎" под
    оформление конкретного файла (title и h1 у index.html/shop.html
    отличаются по формату). Название бренда обязательно экранируется
    (html.escape) — это ввод админа, а не доверенная константа,
    подставлять его в HTML без экранирования небезопасно (риск XSS)."""
    import re

    if not os.path.exists(filepath):
        return web.Response(text="<h1>Template not found</h1>", status=404)

    with open(filepath, "r", encoding="utf-8") as f:
        content = f.read()

    from database.requests import get_effective_brand_name
    brand_name = get_effective_brand_name()
    if brand_name:
        safe_brand = _html_module.escape(brand_name, quote=False)
        content = re.sub(
            r'(<title id="pageTitle">).*?(</title>)',
            lambda m: m.group(1) + title_format.format(brand=safe_brand) + m.group(2),
            content, count=1,
        )
        content = re.sub(
            r'(<h1 id="brandHeader"[^>]*>).*?(</h1>)',
            lambda m: m.group(1) + header_format.format(brand=safe_brand) + m.group(2),
            content, count=1,
        )

    resp = web.Response(text=content, content_type="text/html")
    resp.headers['Cache-Control'] = 'no-store'
    resp.headers['X-App-Commit'] = _get_cached_app_commit()
    return resp


async def handle_shop_page(request: web.Request) -> web.Response:
    """GET /shop — публичная страница покупки, без Telegram."""
    shop_path = os.path.join(_TEMPLATES_DIR, "shop.html")
    resp = _serve_html_with_brand(
        shop_path,
        title_format="💎 {brand} — премиальный VPN",
        header_format="{brand}",
    )

    from database.requests import get_effective_turnstile_site_key
    turnstile_site_key = get_effective_turnstile_site_key()
    if not turnstile_site_key:
        import logging
        logging.getLogger(__name__).warning(
            "TURNSTILE_SITE_KEY не задан в secrets.env — виджет Turnstile "
            "на /shop не будет работать. Получите site key в кабинете "
            "Cloudflare для домена этой инсталляции и добавьте его в secrets.env."
        )
    patched_body = resp.text.replace("{{TURNSTILE_SITE_KEY}}", turnstile_site_key)
    try:
        from bot.services.branding_mark import powered_by_html
        patched_body = patched_body.replace("{{POWERED_BY}}", powered_by_html())
    except Exception:
        patched_body = patched_body.replace("{{POWERED_BY}}", "")
    new_resp = web.Response(text=patched_body, content_type="text/html")
    for hk, hv in resp.headers.items():
        if hk.lower() not in ("content-type", "content-length"):
            new_resp.headers[hk] = hv
    for ck, morsel in resp.cookies.items():
        new_resp.cookies[ck] = morsel
    resp = new_resp

    ref_code = request.query.get("ref")
    if ref_code and not request.cookies.get("site_ref_code"):
        # Сохраняем НАВСЕГДА (пока не истечёт) на первый заход по такой
        # ссылке — не перезаписываем, если человек уже пришёл по ЧУЖОЙ
        # реферальной ссылке ранее (первый код должен быть финальным).
        resp.set_cookie(
            "site_ref_code", ref_code.strip(),
            max_age=60 * 60 * 24 * 30, httponly=True, secure=True, samesite="Lax",
        )
    return resp


async def handle_welcome_page(request: web.Request) -> web.Response:
    """GET /welcome — публичная страница-витрина для новых (ещё не
    подключившихся) посетителей: описание сервиса + актуальные тарифы,
    без входа в бота и без Telegram initData. Полностью анонимная,
    в отличие от /shop.

    Управляется тогглом is_welcome_page_enabled() — выключена по
    умолчанию, пока админ явно не включит её."""
    from database.requests import is_welcome_page_enabled, get_welcome_template_id, WELCOME_TEMPLATES
    if not is_welcome_page_enabled():
        return web.Response(text="404: Not Found", status=404)

    template_id = get_welcome_template_id()
    filename = WELCOME_TEMPLATES[template_id]['file']
    welcome_path = os.path.join(_TEMPLATES_DIR, filename)
    if os.path.exists(welcome_path):
        resp = web.FileResponse(welcome_path)
        # Без этого браузер кэширует /welcome по URL и продолжает
        # показывать старый шаблон даже после того, как админ выбрал
        # другой в настройках — адрес-то не меняется, а разные шаблоны
        # это разные файлы на сервере.
        resp.headers['Cache-Control'] = 'no-store, no-cache, must-revalidate'
        resp.headers['Pragma'] = 'no-cache'
        return resp
    return web.Response(text="<h1>Welcome template not found</h1>", status=404)


async def handle_public_site_info(request: web.Request) -> web.Response:
    """GET /api/public/site-info — базовая информация о сервисе, полностью
    без авторизации: название бренда, юзернейм бота, ссылка на новостной
    канал (если настроен). Используется публичными страницами (например
    /welcome, /, /shop), чтобы не хардкодить эти данные в HTML — они
    берутся из настроек текущей инсталляции, как и везде в остальном боте."""
    from database.requests import (
        get_effective_brand_name, get_cabinet_theme_id, get_shop_theme_id, get_marketing_channel_id,
        is_site_auth_method_enabled, get_zvonok_public_key, get_zvonok_campaign_id,
        is_cabinet_import_app_enabled,
    )
    from bot.services.license import is_feature_available

    bot_username = await _resolve_bot_username_for_webapp()

    channel_id = get_marketing_channel_id()
    channel_url = f"https://t.me/{channel_id.lstrip('@')}" if channel_id else None

    phone_login_enabled = (
        is_site_auth_method_enabled('phone')
        and bool(get_zvonok_public_key())
        and bool(get_zvonok_campaign_id())
    )

    resp = web.json_response({
        "brand_name": get_effective_brand_name(),
        "bot_username": bot_username,
        "cabinet_theme_id": get_cabinet_theme_id(),
        "shop_theme_id": get_shop_theme_id(),
        "news_channel_url": channel_url,
        "code_login_enabled": is_site_auth_method_enabled('code'),
        "phone_login_enabled": phone_login_enabled,
        # Кнопки импорта в Happ/INCY/Karing — платная функция (см.
        # bot/services/license.py, GATED_FEATURES["app_import"]). Раньше
        # веб-кабинет (/ и /shop) показывал эти кнопки ВСЕГДА, когда у ключа
        # есть sub_url, независимо от лицензии — по клику пользователь
        # просто утыкался в страницу "функция недоступна" на /import.
        # Явно передаём статус фичи, чтобы фронтенд не показывал нерабочие
        # кнопки вообще, если она не куплена/выключена админом.
        "app_import_enabled": is_feature_available("app_import"),
        # Помимо общей лицензионной фичи выше, админ КОНКРЕТНОЙ установки
        # может независимо скрыть кнопку под каждое отдельное приложение
        # (например, показывать Happ и INCY, но не Karing) — см.
        # database/db_settings.py, is_cabinet_import_app_enabled().
        "happ_import_enabled": is_cabinet_import_app_enabled("happ"),
        "incy_import_enabled": is_cabinet_import_app_enabled("incy"),
        "karing_import_enabled": is_cabinet_import_app_enabled("karing"),
    })
    resp.headers['Cache-Control'] = 'no-store'
    return resp


async def _resolve_bot_username_for_webapp() -> str:
    """Юзернейм бота — тот же паттерн, что и в handle_public_site_info,
    вынесен отдельно, чтобы использовать и в handle_happ_subscription."""
    from bot.utils.runtime_state import get_bot_username
    return get_bot_username()


def _build_renew_link(key: Dict[str, Any], webapp_url: str, bot_username: str) -> Optional[str]:
    """Ссылка на кнопке продления в Happ (sub-expire-button-link /
    sub-info-button-link).

    Предпочитает сайт: если есть домен и telegram_id владельца ключа —
    генерирует одноразовый код входа (тот же безопасный механизм, что и
    кнопка «Управлять на сайте» в боте) и ведёт сразу на
    {домен}/shop?code=...&key_id=... — клиент попадает в свой личный
    кабинет УЖЕ авторизованным, сразу на нужном ключе, без Telegram.

    Если сайт не настроен или у ключа нет telegram_id (гостевая покупка
    без привязки к боту) — используется прежний способ: диплинк в
    Telegram-бота (?start=renew_{id})."""
    key_id = key.get("id")
    telegram_id = key.get("telegram_id")

    if webapp_url and telegram_id and key_id:
        try:
            from database.requests import create_site_login_code
            code = create_site_login_code(int(telegram_id), ttl_minutes=30)
            return f"{webapp_url.rstrip('/')}/shop?code={code}&key_id={key_id}"
        except Exception as e:
            logger.warning(f"_build_renew_link: не удалось создать код входа на сайт: {e}")

    if bot_username and key_id:
        return f"https://t.me/{bot_username}?start=renew_{key_id}"

    return None


async def _trial_device_guard(request: web.Request, key: Dict[str, Any]):
    """v1.190: один пробный ключ на устройство. Возвращает готовый ответ для заблокированного
    ключа или None. Любая ошибка = None (подписка не должна ломаться из-за этой проверки)."""
    from bot.services import trial_device as td

    if not td.guard_enabled() or not _is_trial_key(key):
        return None
    key_id, user_id = key.get("id"), key.get("user_id")
    if not key_id or not user_id:
        return None

    if td.is_key_blocked(key_id):
        td.touch_block(key_id)
    else:
        # берём только настоящий идентификатор клиента; синтетический (его подставляет бот) не годится
        hwid_hash = td.normalize_hwid(request.headers.get("X-HWID"))
        if not hwid_hash:
            return None
        res = td.check_and_register(hwid_hash, key_id, user_id)
        if res["status"] != "duplicate":
            return None
        created = td.block_key(key_id, hwid_hash, user_id, res.get("owner_user_id"))
        logger.warning(
            f"trial_device_guard: пробный ключ {key_id} (user {user_id}) заблокирован — устройство уже "
            f"использовало пробник (user {res.get('owner_user_id')})"
        )
        if created:
            try:
                from bot.utils.runtime_state import get_bot_instance
                from config import ADMIN_IDS
                bot_instance = get_bot_instance()
                if bot_instance:
                    for admin_id in ADMIN_IDS:
                        try:
                            await bot_instance.send_message(
                                admin_id,
                                f"⚠️ Повторный пробник на том же устройстве: ключ #{key_id} заблокирован. "
                                f"Список и исключения: /trial_devices",
                            )
                        except Exception:
                            pass
            except Exception as notify_err:
                logger.debug(f"trial_device_guard: уведомление не отправлено: {notify_err}")

    from database.requests import get_effective_brand_name, get_effective_webapp_url
    headers = {"Content-Type": "text/plain; charset=utf-8", "Cache-Control": "no-store"}
    brand = get_effective_brand_name()
    if brand:
        headers["profile-title"] = brand[:25]
    headers["subscription-userinfo"] = "upload=0;download=0;total=0;expire=1"
    headers["sub-info-text"] = "Пробный период на этом устройстве уже использован. Оформите подписку, чтобы продолжить."
    link = _build_renew_link(key, (get_effective_webapp_url() or "").rstrip("/"), await _resolve_bot_username_for_webapp())
    headers["sub-expire"] = "1"
    if link:
        headers["sub-expire-button-link"] = link
        headers["sub-info-button-text"] = "Купить / продлить"
        headers["sub-info-button-link"] = link
    return web.Response(status=200, body=b"", headers=headers)


async def handle_happ_subscription(request: web.Request) -> web.Response:
    """GET /happ-sub/{sub_id} — прокси-обёртка над реальной подпиской,
    отдаваемой панелью 3x-ui, добавляющая заголовки, которые понимает
    приложение Happ (и частично другие VLESS-клиенты, читающие тот же
    стандарт subscription-userinfo):

      - profile-title       — название бренда
      - profile-web-page-url — ссылка на сайт (если настроен)
      - support-url          — ссылка на поддержку в боте
      - subscription-userinfo — трафик/лимит/дата истечения (из нашей
        БД, если панель сама не прислала этот заголовок)
      - sub-expire + sub-expire-button-link — если подписка УЖЕ истекла,
        Happ покажет "Subscription has expired!" с кнопкой "Renew",
        ведущей прямо на карточку ключа в боте для продления
      - sub-info-text + sub-info-button-text/link — если подписка ЕЩЁ
        активна, но истекает в ближайшие 3 дня, показывает мягкое
        предупреждение с той же кнопкой продления (не блокирует
        использование, просто заранее напоминает)

    Само содержимое подписки (список VLESS/VMess-ссылок) передаётся от
    3x-ui БЕЗ ИЗМЕНЕНИЙ — мы только добавляем заголовки поверх.

    Официальный формат заголовков Happ: https://www.happ.su/main/dev-docs/app-management
    """
    sub_id = request.match_info.get("sub_id", "")
    if not sub_id:
        return web.Response(status=404, text="Not Found")

    from database.requests import get_vpn_key_by_sub_id
    key = get_vpn_key_by_sub_id(sub_id)
    if not key:
        return web.Response(status=404, text="Not Found")

    # v1.190: учёт устройств для пробных ключей
    try:
        _blocked_resp = await _trial_device_guard(request, key)
    except Exception as _guard_err:
        logger.warning(f"trial_device_guard: {_guard_err}")
        _blocked_resp = None
    if _blocked_resp is not None:
        return _blocked_resp

    raw_url = await get_subscription_url_for_key(key)
    if not raw_url:
        return web.Response(status=502, text="Subscription temporarily unavailable")

    import aiohttp as _aiohttp
    from multidict import CIMultiDict
    # Пробрасываем HWID-идентифицирующие заголовки от РЕАЛЬНОГО клиента
    # (Happ, INCY, v2rayTUN и т.п.) панели — без этого панель в режиме
    # HWID-ограничения не может опознать устройство и отдаёт пустое тело
    # подписки (сама панель добавляет заголовки x-hwid-active/
    # x-hwid-not-supported, сигнализируя об этом). См. стандарт:
    # github.com/XTLS/Xray-core/discussions/4877
    _forward_header_names = ("X-HWID", "User-Agent", "X-Device-OS", "X-Ver-OS", "X-Device-Model")
    forward_headers = {
        name: request.headers[name]
        for name in _forward_header_names
        if name in request.headers
    }
    # Некоторые клиенты (например, Karing на Windows) НЕ реализуют заголовок
    # X-HWID вообще — панель в режиме HWID-лимита в этом случае считает
    # запрос неавторизованным и отдаёт 404 (пустое тело) независимо от
    # проброса выше, поскольку пробрасывать просто нечего. Чтобы не
    # блокировать таких пользователей, подставляем панели СИНТЕТИЧЕСКИЙ
    # HWID, устойчиво рассчитанный из связки sub_id + IP клиента +
    # User-Agent — благодаря этому одно и то же реальное устройство при
    # повторных запросах попадает в один и тот же "слот" на панели (не
    # плодит новые записи в client_hwids на каждый запрос), а разные
    # устройства/IP всё равно получают разные значения.
    if "X-HWID" not in forward_headers:
        _synthetic_seed = f"{sub_id}:{_get_client_ip(request)}:{forward_headers.get('User-Agent', '')}"
        forward_headers["X-HWID"] = hashlib.sha256(_synthetic_seed.encode("utf-8")).hexdigest()
        logger.info(
            f"handle_happ_subscription: клиент не прислал X-HWID (sub_id={sub_id[:8]}..., "
            f"UA={forward_headers.get('User-Agent', '')!r}) — подставляю синтетический HWID, "
            f"чтобы панель в режиме HWID-лимита не блокировала подписку"
        )
    served_from_cache = False
    try:
        async with _aiohttp.ClientSession() as session:
            async with session.get(
                raw_url, timeout=_aiohttp.ClientTimeout(total=10), headers=forward_headers
            ) as upstream:
                body = await upstream.read()
                upstream_status = upstream.status
                # Копируем ВСЕ заголовки от панели как есть, кроме тех, что
                # должен считать сам сервер при формировании ответа
                # (Content-Length и т.п.). Раньше здесь копировались
                # только Content-Type и subscription-userinfo — из-за этого
                # пропадали любые другие Happ-заголовки от панели (например,
                # маршрутизация, настроенная во вкладке "Happ" в 3x-ui —
                # это тоже отдельный заголовок, про который мы просто не
                # знали и не копировали).
                _skip = {'content-length', 'transfer-encoding', 'connection', 'date', 'server'}
                headers = CIMultiDict(
                    (k, v) for k, v in upstream.headers.items() if k.lower() not in _skip
                )
        if upstream_status == 200 and body:
            # Панель ответила успешно — запоминаем на случай, если она
            # временно ляжет до следующего запроса (см. ниже).
            _HAPP_SUB_CACHE[sub_id] = (body, dict(headers))
    except Exception as e:
        cached = _HAPP_SUB_CACHE.get(sub_id)
        if cached:
            logger.warning(
                f"handle_happ_subscription: панель недоступна ({sub_id[:8]}...): {e} — "
                f"отдаю клиенту последнюю успешно полученную подписку из кэша"
            )
            body, cached_headers = cached
            headers = CIMultiDict(cached_headers)
            upstream_status = 200
            served_from_cache = True
        else:
            logger.warning(f"handle_happ_subscription: не удалось получить подписку у панели ({sub_id[:8]}...): {e}")
            return web.Response(status=502, text="Upstream subscription unavailable")

    if upstream_status != 200:
        cached = _HAPP_SUB_CACHE.get(sub_id)
        if cached and key.get('panel_removed_at'):
            # клиента штатно убрала panel_only_cleanup — кэш устарел, не отдаём его
            _HAPP_SUB_CACHE.pop(sub_id, None)
            cached = None
        if cached and 400 <= upstream_status < 500:
            from database.requests import get_setting as _get_setting_cache
            if (_get_setting_cache("sub_cache_on_4xx", "0") or "0") == "0":
                # строгий режим: ответ панели 4xx (в т.ч. лимит устройств) — это ответ, а не сбой
                logger.info(
                    f"handle_happ_subscription: панель вернула {upstream_status} для sub_id={sub_id[:8]}... — "
                    f"кэш не отдаём (строгий режим, /sub_mode hwid)"
                )
                return web.Response(status=502, text="Subscription temporarily unavailable — please try again shortly")
        if cached:
            logger.warning(
                f"handle_happ_subscription: панель вернула {upstream_status} для sub_id={sub_id[:8]}... — "
                f"отдаю клиенту последнюю успешно полученную подписку из кэша"
            )
            body, cached_headers = cached
            headers = CIMultiDict(cached_headers)
            served_from_cache = True
        else:
            # Панель НЕ подтвердила успех (например, 404). Два разных случая:
            #
            # 1. key['panel_removed_at'] уже проставлен — клиента с панели
            #    штатно убрала panel_only_cleanup.py (ключ истёк, клиент
            #    ещё не продлил). Это ожидаемо и никакого внимания не
            #    требует — не засоряем лог уровнем WARNING.
            # 2. panel_removed_at пуст, а панель всё равно не находит
            #    клиента — вот это уже настоящая рассинхронизация (ручное
            #    удаление на панели в обход бота, гонка при удалении и
            #    т.п.), и её стоит смотреть.
            #
            # Раньше здесь ОТСУТСТВОВАЛА эта проверка — клиент получал 200
            # OK с чем бы панель ни ответила, включая пустое тело при 404.
            # Найдено на практике (сервер Артёма) и починено вчера — но при
            # более поздних правках сегодня (разведение настроек Happ/INCY)
            # эта проверка была случайно утеряна при переписывании соседнего
            # блока. Восстановлено.
            if key.get('panel_removed_at'):
                logger.info(
                    f"handle_happ_subscription: панель вернула {upstream_status} для "
                    f"sub_id={sub_id[:8]}... — ключ истёк и убран с панели плановой "
                    f"очисткой (panel_removed_at={key.get('panel_removed_at')}), клиенту нужно продлить"
                )
            else:
                logger.warning(
                    f"handle_happ_subscription: панель вернула {upstream_status} для "
                    f"sub_id={sub_id[:8]}... (client_uuid={key.get('client_uuid')}) — "
                    f"ключ есть в БД и не убирался плановой очисткой, но панель его не "
                    f"находит. Похоже на рассинхронизацию."
                )
            return web.Response(status=502, text="Subscription temporarily unavailable — please try again shortly")

    if served_from_cache:
        headers["X-Eclipse-Cache-Fallback"] = "1"

    from database.requests import get_effective_brand_name, get_effective_webapp_url
    if "Content-Type" not in headers:
        headers["Content-Type"] = "text/plain; charset=utf-8"

    brand_name = get_effective_brand_name()
    if brand_name:
        headers["profile-title"] = brand_name[:25]

    webapp_url = get_effective_webapp_url()
    if webapp_url:
        headers["profile-web-page-url"] = f"{webapp_url.rstrip('/')}/shop"

    bot_username = await _resolve_bot_username_for_webapp()
    if bot_username:
        headers["support-url"] = f"https://t.me/{bot_username}?start=support"

    # Happ и INCY используют один движок, но по-разному трактуют одни и
    # те же "Advanced parameter" заголовки — обнаружено на практике:
    # настройка, нужная для нормальной работы Happ, ломала импорт в
    # INCY, и наоборот. Поэтому определяем приложение по User-Agent
    # (Happ шлёт "Happ/4.3.0/...", INCY — "INCY/2.6.1/...") и
    # применяем настройки, специфичные именно для НЕГО, а не общие.
    client_ua = request.headers.get("User-Agent", "")
    if client_ua.startswith("Happ"):
        detected_app = "happ"
    elif client_ua.startswith("INCY"):
        detected_app = "incy"
    else:
        detected_app = None

    from database.requests import get_happ_provider_id
    provider_id = get_happ_provider_id()
    # providerid — механизм ИМЕННО Happ (happ.su/main/dev-docs/app-management).
    # Раньше заголовок отправлялся ВСЕМ клиентам без разбора, включая INCY,
    # Karing и ECLIPSE VPN, у которых нет такого понятия и которые этот
    # заголовок просто не поймут — не критично, но не по документации.
    # Не шлём его только опознанному НЕ-Happ клиенту (INCY); для Karing/
    # ECLIPSE VPN и неопознанных User-Agent (в т.ч. старых версий Happ,
    # не попадающих под текущую проверку) поведение сохранено как раньше.
    if provider_id and detected_app != "incy":
        headers["providerid"] = provider_id

    if provider_id and detected_app == "happ":
        from database.requests import is_client_toggle_enabled
        if is_client_toggle_enabled("happ", "autoconnect"):
            # "Advanced parameter" — официально работает только при заданном
            # Provider ID (см. happ.su/main/dev-docs/app-management). Клиент
            # сам измеряет отклик каждого сервера в подписке и подключается
            # к самому быстрому при запуске приложения.
            headers["subscription-autoconnect"] = "1"
            headers["subscription-autoconnect-type"] = "lowestdelay"
            # v1.160: для выбора сервера с наименьшей задержкой Happ должен
            # замерить отклик серверов при открытии приложения.
            if "subscription-ping-onopen-enabled" not in headers:
                headers["subscription-ping-onopen-enabled"] = "1"
        if is_client_toggle_enabled("happ", "hide_settings"):
            headers["hide-settings"] = "1"
        if is_client_toggle_enabled("happ", "notify_expire"):
            headers["notification-subs-expire"] = "1"
        if is_client_toggle_enabled("happ", "sort_ping"):
            headers["subscriptions-sort-type"] = "ping"
            if "subscription-ping-onopen-enabled" not in headers:
                headers["subscription-ping-onopen-enabled"] = "1"
        if is_client_toggle_enabled("happ", "auto_update"):
            headers["subscription-auto-update-enable"] = "1"

    elif detected_app == "incy":
        # У INCY СВОИ имена заголовков и своя семантика — не переиспользуем
        # Happ'овские (проверено по официальной документации INCY:
        # docs.incy.cc/app-management). В частности: sort-order (не
        # subscriptions-sort-type), hide-url (прячет только САМУ ссылку
        # подписки — конфиги серверов внутри остаются видимыми, в
        # отличие от Happ'овского hide-settings), и
        # profile-update-interval — ЧИСЛО часов, а не переключатель
        # вкл/выкл. Автовыбора быстрого сервера и родных уведомлений об
        # истечении через обычные заголовки у INCY нет вообще — это
        # либо часть их отдельного Premium API (нужен свой аккаунт на
        # web.incy-panel.com), либо не поддерживается совсем.
        from database.requests import is_client_toggle_enabled, get_incy_update_interval_hours
        if is_client_toggle_enabled("incy", "sort_ping"):
            headers["sort-order"] = "ping"
        if is_client_toggle_enabled("incy", "hide_url"):
            headers["hide-url"] = "1"
        update_interval = get_incy_update_interval_hours()
        if update_interval:
            headers["profile-update-interval"] = str(update_interval)

    if detected_app in ("happ", "incy"):
        # Геонастройки / Routing — необязательный заголовок 'routing',
        # отправляется, только если явно настроен (по умолчанию режим
        # 'disabled' — заголовок не шлётся вообще, ничего не меняется для
        # уже работающих клиентов). Если режим add/onadd включён, но админ
        # не задал свой JSON-профиль вручную — используется дефолтный
        # ECLIPSE-профиль (компактные geo-базы, название = бренд бота),
        # см. get_effective_happ_routing_profile_json. У Happ и INCY общий
        # движок и одинаковая структура JSON-профиля (проверено по офиц.
        # докам обоих — incy.gitbook.io/docs/.../marshrutizaciya-routing),
        # но РАЗНЫЙ префикс схемы в значении заголовка ("happ://" / "incy://"),
        # и у INCY официально НЕТ режима "off" (только add/onadd) — если
        # выбран "off", INCY просто ничего не получает, чтобы не отправить
        # клиенту неподдерживаемое значение.
        from database.requests import get_happ_routing_mode, get_effective_happ_routing_profile_json
        routing_mode = get_happ_routing_mode()
        scheme = detected_app  # "happ" или "incy"
        if routing_mode == "off":
            if detected_app == "happ":
                headers["routing"] = "happ://routing/off"
        elif routing_mode in ("add", "onadd"):
            profile_json = get_effective_happ_routing_profile_json()
            if profile_json:
                import base64 as _base64
                profile_b64 = _base64.b64encode(profile_json.encode("utf-8")).decode("ascii")
                headers["routing"] = f"{scheme}://routing/{routing_mode}/{profile_b64}"

    if "subscription-userinfo" not in headers:
        # Далёкая дата (условно "безлимитный срок") в expires_at — тот же
        # служебный маркер, что уже обрабатывается при синхронизации с
        # панелью (см. _key_expiry_time_ms в bot/services/vpn_api.py,
        # порог тоже 90000 дней): такую дату нужно отдавать Happ/INCY как
        # expire=0 ("никогда не истекает"), а не буквальным epoch —
        # иначе вместо "∞" клиент показывает саму дату (обнаружено на
        # практике на одной из white-label инсталляций).
        from datetime import timedelta as _timedelta
        expire_epoch = 0
        try:
            expires_at = key.get("expires_at")
            if expires_at:
                expires_dt = datetime.fromisoformat(expires_at)
                if expires_dt <= datetime.now() + _timedelta(days=90000):
                    expire_epoch = int(expires_dt.timestamp())
        except Exception:
            expire_epoch = 0
        traffic_used = key.get("traffic_used") or 0
        traffic_limit = key.get("traffic_limit") or 0
        headers["subscription-userinfo"] = (
            f"upload=0;download={traffic_used};total={traffic_limit};expire={expire_epoch}"
        )

    is_expired = False
    days_left = None
    try:
        expires_at = key.get("expires_at")
        if expires_at:
            expires_dt = datetime.fromisoformat(expires_at)
            now = datetime.now()
            is_expired = expires_dt < now
            if not is_expired:
                delta = expires_dt - now
                days_left = delta.days
                if delta.seconds > 0:
                    days_left += 1
    except Exception:
        is_expired = False
        days_left = None

    if is_expired:
        # Уже истекла — жёсткий блок Happ: "Subscription has expired!" + Renew.
        # Ведём на сайт (с авто-входом по одноразовому коду), чтобы клиент мог
        # продлить прямо там, без Telegram — VPN всё равно уже не работает,
        # так что открывать Telegram специально ради этого не обязательно.
        renew_link = _build_renew_link(key, webapp_url, bot_username)
        if renew_link:
            headers["sub-expire"] = "1"
            headers["sub-expire-button-link"] = renew_link
    elif _is_trial_key(key) and _build_renew_link(key, webapp_url, bot_username):
        # Пробный период: подсказка, что продление не теряет остаток (бот, сайт, webapp — любой пробник)
        _left = f" Осталось {days_left} дн." if days_left is not None and 0 <= days_left <= 3 else ""
        headers["sub-info-text"] = "🎁 Пробный период." + _left + " Нажмите «Купить / продлить» — остаток дней сохранится"
        headers["sub-info-button-text"] = "Купить / продлить"
        headers["sub-info-button-link"] = _build_renew_link(key, webapp_url, bot_username)
        headers["sub-expire"] = "0"
    elif days_left is not None and 0 <= days_left <= 3 and bot_username and key.get("id"):
        # Ещё активна, но истекает в ближайшие 3 дня — мягкое предупреждение
        # (sub-info-*), а не жёсткий блок. VPN пока работает, поэтому ведём в
        # Telegram-бота (не на сайт) — клиент и так, скорее всего, обычно
        # управляет ключом через бота, пока подписка активна.
        word = "день" if days_left == 1 else ("дня" if 1 < days_left < 5 else "дней")
        headers["sub-info-text"] = f"⚠️ Подписка истекает через {days_left} {word}!"
        headers["sub-info-button-text"] = "Купить / продлить"
        headers["sub-info-button-link"] = f"https://t.me/{bot_username}?start=renew_{key['id']}"
        headers["sub-expire"] = "0"
    elif not is_expired:
        # Happ кэширует баннеры между обновлениями — снимаем их явно, иначе после
        # продления остаётся старое «истекает через N дней». По документации Happ:
        # info-блок отключается ПУСТОЙ строкой (значение «0» на части версий
        # выводится как текст «0»), а уведомление об истечении — значением 0.
        # v1.159: если инфо-блок задала сама панель (например кнопка «AI помощник»),
        # оставляем его — снимаем только наш устаревший баннер.
        if not headers.get("sub-info-text"):
            headers["sub-info-text"] = ""
        # v1.188: бот знает срок точно — для не истёкшего ключа всегда «0»
        # (раньше оставалось «1», которое присылала панель)
        headers["sub-expire"] = "0"

    if detected_app == "incy":
        from database.requests import get_setting as _get_setting_incy
        if (_get_setting_incy("incy_headers_mode", "strict") or "strict") == "strict":
            for _h in _INCY_STRIP_HEADERS:
                headers.popall(_h, None)

    resp = web.Response(body=body, headers=headers)
    resp.headers['Cache-Control'] = 'no-store'
    return resp


async def handle_landing_tariffs(request: web.Request) -> web.Response:
    """GET /api/public/landing-tariffs — упрощённый список активных тарифов
    для публичной страницы-витрины, БЕЗ авторизации (в отличие от
    /api/public/tariffs, который несмотря на название требует вход).
    Отдаёт только то, что уместно показывать анонимному посетителю:
    длительность, объём трафика, цену — без тарифов, скрытых из продажи
    (is_active=0), без служебных полей."""
    from database.db_tariffs import get_all_tariffs

    tariffs = get_all_tariffs(include_hidden=False)
    result = [
        {
            "duration_days": t.get("duration_days"),
            "traffic_limit_gb": t.get("traffic_limit_gb", 0),
            "price_rub": t.get("price_rub"),
            "price_stars": t.get("price_stars"),
        }
        for t in tariffs
    ]
    resp = web.json_response({"tariffs": result})
    resp.headers['Cache-Control'] = 'no-store'
    return resp


async def handle_public_tariffs(request: web.Request) -> web.Response:
    """GET /api/public/tariffs — список тарифов. Требует вход (по коду или
    OAuth) — цены и тарифы не должны быть видны анонимно всем подряд."""
    account_id = _verify_session(request.cookies.get("site_session"))
    if not account_id:
        return web.json_response({"error": "unauthorized"}, status=401)

    from database.db_tariffs import get_all_tariffs
    from database.requests import is_trial_enabled, get_trial_tariff_id, has_used_trial, get_site_account_by_id

    tariffs = get_all_tariffs(include_hidden=False)

    trial_available = False
    if is_trial_enabled():
        trial_tariff_id = get_trial_tariff_id()
        if trial_tariff_id:
            account = get_site_account_by_id(account_id)
            already_used = _site_account_used_trial(account_id, trial_tariff_id, account.get("telegram_id") if account else None)
            trial_available = not already_used

    current_tariff_id = None
    vpn_key_id_raw = request.query.get("vpn_key_id")
    if vpn_key_id_raw:
        try:
            from database.requests import get_vpn_key_by_id
            key = get_vpn_key_by_id(int(vpn_key_id_raw))
            if key:
                current_tariff_id = key.get("tariff_id")
        except (ValueError, TypeError):
            pass

    return web.json_response({
        "tariffs": [
            {
                "id": t["id"],
                "name": t["name"],
                "duration_days": t["duration_days"],
                "price_rub": float(t.get("price_rub") or 0),
                "traffic_limit_gb": t.get("traffic_limit_gb"),
                "is_current": current_tariff_id is not None and t["id"] == current_tariff_id,
            }
            for t in tariffs
        ],
        "trial_available": trial_available,
    })


def _site_account_used_trial(account_id: int, trial_tariff_id: int, telegram_id) -> bool:
    """Проверяет, использовал ли этот аккаунт (или связанный с ним реальный
    telegram-пользователь) пробный период — не даёт получить его повторно
    ни через сайт, ни через бота под одним и тем же человеком."""
    from database.connection import get_db

    with get_db() as conn:
        existing = conn.execute(
            """SELECT id FROM anonymous_purchases
               WHERE site_account_id = ? AND tariff_id = ? AND status IN ('paid', 'claimed')""",
            (account_id, trial_tariff_id),
        ).fetchone()
    if existing:
        return True

    # v1.186: пробник распознаём по маркеру 'trial' в заказе, а не только по текущему
    # пробному тарифу — иначе после смены пробного тарифа админом все могли бы взять его снова
    with get_db() as conn:
        any_trial = conn.execute(
            """SELECT id FROM anonymous_purchases
               WHERE site_account_id = ? AND yookassa_payment_id = 'trial' AND status IN ('paid', 'claimed')""",
            (account_id,),
        ).fetchone()
    if any_trial:
        return True

    if telegram_id:
        from database.requests import has_used_trial
        if has_used_trial(telegram_id):
            return True

    return False


async def handle_public_pay_create(request: web.Request) -> web.Response:
    """POST /api/public/pay/create — создаёт анонимный заказ и QR-платёж
    ЮKassa, без Telegram. Полная цена, без промокодов/баланса.
    Body JSON: {"tariff_id": int}
    """
    try:
        data = await request.json()
    except json.JSONDecodeError:
        return web.json_response({"error": "invalid_json"}, status=400)

    tariff_id = data.get("tariff_id")
    if not tariff_id:
        return web.json_response({"error": "tariff_id_required"}, status=400)

    from database.db_tariffs import get_tariff_by_id
    from database.db_payments import create_anonymous_purchase, save_anonymous_purchase_payment_id

    tariff = get_tariff_by_id(int(tariff_id))
    if not tariff:
        return web.json_response({"error": "tariff_not_found"}, status=404)

    price_rub = float(tariff.get("price_rub") or 0)
    if price_rub <= 0:
        return web.json_response({"error": "invalid_price"}, status=400)

    order_id = _generate_public_order_id()

    try:
        claim_code = create_anonymous_purchase(order_id, tariff["id"])

        # Если покупатель авторизован (Google/Яндекс/код) — сразу связываем
        # покупку с его аккаунтом, чтобы она отобразилась в личном кабинете
        # без необходимости повторно вводить код привязки.
        account_id = _verify_session(request.cookies.get("site_session"))
        if account_id:
            from database.requests import link_purchase_to_account
            link_purchase_to_account(order_id, account_id)

        from aiogram import Bot
        from config import BOT_TOKEN
        from bot.services.billing import create_yookassa_qr_payment

        pay_bot = Bot(token=BOT_TOKEN)
        try:
            bot_info = await pay_bot.get_me()
            description = _pub(tariff)
            yk_result = await create_yookassa_qr_payment(
                amount_rub=price_rub, order_id=order_id, description=description,
                bot_name=bot_info.username,
            )
        finally:
            await pay_bot.session.close()

        save_anonymous_purchase_payment_id(order_id, yk_result["yookassa_payment_id"])
        # Фоновая подстраховка для сайтовых заказов теперь идёт через ОТДЕЛЬНУЮ,
        # правильную систему (get_abandoned_anonymous_purchases +
        # run_anonymous_payment_auto_check_scheduler) — она сканирует
        # anonymous_purchases напрямую по времени, без отдельной таблицы
        # очереди. Раньше здесь ОШИБОЧНО вызывался schedule_payment_auto_check,
        # рассчитанный на заказы БОТА (payment_auto_checks.order_id имеет
        # FOREIGN KEY на payments.order_id) — у сайтовых заказов нет строки в
        # payments вообще, поэтому эта вставка ломалась с "FOREIGN KEY
        # constraint failed" везде, где SQLite строго проверяет внешние ключи.

        qr_image_b64 = base64.b64encode(yk_result["qr_image_data"]).decode("ascii")
        qr_image_data_url = f"data:image/png;base64,{qr_image_b64}"

        return web.json_response({
            "order_id": order_id,
            "qr_image_url": qr_image_data_url,
            "qr_url": yk_result["qr_url"],
            "amount_rub": price_rub,
        })
    except Exception as e:
        logger.error(f"Public pay/create error: {e}")
        return web.json_response({"error": "payment_creation_failed"}, status=502)


async def handle_public_pay_check(request: web.Request) -> web.Response:
    """POST /api/public/pay/check — проверяет статус анонимного платежа и,
    если оплата прошла, провижинит рабочий VPN-ключ прямо сейчас.
    Body JSON: {"order_id": "..."}
    """
    try:
        data = await request.json()
    except json.JSONDecodeError:
        return web.json_response({"error": "invalid_json"}, status=400)

    order_id = (data.get("order_id") or "").strip()
    if not order_id:
        return web.json_response({"error": "order_id_required"}, status=400)

    from bot.services.anonymous_purchase import check_and_complete_anonymous_payment
    result = await check_and_complete_anonymous_payment(order_id)
    if result["status"] == "not_found":
        return web.json_response({"error": "order_not_found"}, status=404)
    return web.json_response(result)


# ============================================================================
# SITE SESSIONS & OAuth (Google / Яндекс / VK) — личный кабинет на сайте.
# Сессия — подписанная HMAC cookie (без сторонних библиотек), содержит
# site_account_id и время истечения (30 дней).
# ============================================================================

_SESSION_TTL_SECONDS = 30 * 24 * 3600


def _get_session_secret() -> bytes:
    from database.requests import get_site_session_secret
    return get_site_session_secret().encode()


def _sign_session(account_id: int) -> str:
    import time
    secret = _get_session_secret()
    expires = int(time.time()) + _SESSION_TTL_SECONDS
    payload = f"{account_id}:{expires}"
    sig = hmac.new(secret, payload.encode(), hashlib.sha256).hexdigest()
    return f"{payload}:{sig}"


def _verify_session(cookie_value: Optional[str]) -> Optional[int]:
    import time
    if not cookie_value:
        return None
    try:
        account_id_str, expires_str, sig = cookie_value.split(":")
        payload = f"{account_id_str}:{expires_str}"
        expected_sig = hmac.new(_get_session_secret(), payload.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(sig, expected_sig):
            return None
        if int(expires_str) < int(time.time()):
            return None
        return int(account_id_str)
    except (ValueError, AttributeError):
        return None


# Защита пробного периода от ботов: простой rate-limit по IP в памяти
# процесса (без внешних зависимостей) + проверка Cloudflare Turnstile.
_trial_attempts_by_ip: dict[str, list[float]] = {}
_TRIAL_RATE_LIMIT_WINDOW_SEC = 3600  # 1 час
_TRIAL_RATE_LIMIT_MAX_ATTEMPTS = 3   # максимум попыток с одного IP за окно


def _get_client_ip(request: web.Request) -> str:
    """Реальный IP клиента — учитывает заголовок от прокси (nginx), если есть.

    ВАЖНО: берём ПОСЛЕДНЕЕ значение в X-Forwarded-For, а не первое. nginx
    обычно ДОБАВЛЯЕТ реальный IP подключившегося клиента в конец цепочки
    (директива proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for),
    а не заменяет заголовок целиком — значит любой внешний запрос может
    прислать СВОЙ X-Forwarded-For с произвольным (поддельным) IP первым
    значением. Взятие первого значения без проверки превращает публичные
    эндпоинты (например /api/public/connection-status — виджет "Вы
    защищены") в оракул: можно перебором значений выяснить настоящие IP
    VPN-серверов, просто проверяя, для какого IP приходит "защищено"."""
    forwarded = request.headers.get("X-Forwarded-For", "")
    if forwarded:
        parts = [p.strip() for p in forwarded.split(",") if p.strip()]
        if parts:
            return parts[-1]
    return request.remote or "unknown"


_connection_status_ip_cache: dict = {"ips": set(), "checked_at": 0.0}
_CONNECTION_STATUS_CACHE_TTL = 300  # 5 минут — резолвить хосты серверов на каждый визит смысла нет


async def _get_known_vpn_server_ips() -> set:
    """Множество IP-адресов всех активных VPN-серверов (панелей). Если
    посетитель сайта заходит с одного из этих адресов — значит его трафик
    реально идёт через наш VPN (когда клиент подключён, ВЕСЬ его интернет,
    включая открытие сайта, выходит через IP сервера). Резолвится не чаще
    раза в 5 минут, чтобы не бить DNS на каждый визит виджета."""
    import time as _time
    now = _time.time()
    if now - _connection_status_ip_cache["checked_at"] < _CONNECTION_STATUS_CACHE_TTL:
        return _connection_status_ip_cache["ips"]

    import socket
    import re
    from database.db_servers import get_all_servers
    ips = set()
    for srv in get_all_servers():
        if not srv.get("is_active"):
            continue
        public_ip_raw = (srv.get("public_ip") or "").strip()
        if public_ip_raw:
            # Админ явно указал реальный(е) адрес(а) (v1.89, v1.91 — список
            # для каскадных/многоадресных серверов) — доверяем полностью,
            # DNS вообще не трогаем. Актуально, если панель за Cloudflare
            # (её домен резолвится в IP Cloudflare, а не в реальный IP
            # сервера — Cloudflare не проксирует VLESS-порты), сервер с
            # несколькими внешними IP ("Global Auto" и т.п.), или каскад
            # из нескольких серверов с разными точками выхода.
            for part in re.split(r"[,\s]+", public_ip_raw):
                part = part.strip()
                if part:
                    ips.add(part)
            continue
        host = (srv.get("host") or "").strip()
        if not host:
            continue
        try:
            resolved = socket.gethostbyname(host)
            ips.add(resolved)
        except (socket.gaierror, OSError):
            ips.add(host)  # уже голый IP, либо временно не резолвится — пробуем как есть

    _connection_status_ip_cache["ips"] = ips
    _connection_status_ip_cache["checked_at"] = now
    return ips


async def handle_public_connection_status(request: web.Request) -> web.Response:
    """GET /api/public/connection-status — для виджета "Вы защищены /
    не защищены" на витрине. Сверяет IP посетителя с адресами наших
    собственных VPN-серверов — никаких cookie или клиентских проверок,
    только реальный маршрут трафика. Если IP не наш — отдаёт страну/город
    через бесплатный гео-сервис, чтобы показать "вот что видно о вас"."""
    ip = _get_client_ip(request)
    known_ips = await _get_known_vpn_server_ips()
    protected = ip in known_ips

    result = {"ip": ip, "protected": protected, "country": None, "city": None}

    if not protected and ip and ip != "unknown":
        try:
            import aiohttp
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=3)) as session:
                async with session.get(f"http://ip-api.com/json/{ip}?fields=status,country,city") as resp:
                    data = await resp.json()
                    if data.get("status") == "success":
                        result["country"] = data.get("country")
                        result["city"] = data.get("city")
        except Exception as e:
            logger.debug(f"connection-status: гео-запрос не удался для {ip}: {e}")

    return web.json_response(result)


def _trial_rate_limit_check(ip: str) -> bool:
    """True, если можно пробовать — не превышен лимит попыток с этого IP."""
    import time
    now = time.time()
    attempts = _trial_attempts_by_ip.get(ip, [])
    attempts = [t for t in attempts if now - t < _TRIAL_RATE_LIMIT_WINDOW_SEC]
    _trial_attempts_by_ip[ip] = attempts
    return len(attempts) < _TRIAL_RATE_LIMIT_MAX_ATTEMPTS


def _trial_rate_limit_record(ip: str) -> None:
    import time
    _trial_attempts_by_ip.setdefault(ip, []).append(time.time())


async def _verify_turnstile_token(token: str, remote_ip: str) -> bool:
    """Проверяет токен Cloudflare Turnstile через siteverify API."""
    secret = os.environ.get("TURNSTILE_SECRET_KEY", "")
    if not secret or not token:
        return False
    try:
        import aiohttp
        async with aiohttp.ClientSession() as session:
            async with session.post(
                "https://challenges.cloudflare.com/turnstile/v0/siteverify",
                data={"secret": secret, "response": token, "remoteip": remote_ip},
                timeout=aiohttp.ClientTimeout(total=10),
            ) as resp:
                result = await resp.json()
                return bool(result.get("success"))
    except Exception as e:
        logger.warning(f"Turnstile verification error: {e}")
        return False


def _is_trusted_app_request(request: web.Request) -> bool:
    """v1.161: запрос из нативного приложения. Если админ задал секрет
    приложения (команда /app_secret), заголовок X-Eclipse-App-Key должен
    совпадать с ним; без заданного секрета работает прежняя проверка по
    X-Eclipse-App: 1 (совместимость со старыми версиями приложения)."""
    if request.headers.get("X-Eclipse-App") != "1":
        return False
    from database.requests import get_setting
    secret = (get_setting("app_client_secret", "") or "").strip()
    if not secret:
        return True
    # v1.163: пока админ не включил жёсткую проверку (/app_secret enforce),
    # старые версии приложения без ключа продолжают работать
    if (get_setting("app_client_secret_enforced", "") or "").strip() != "1":
        return True
    given = request.headers.get("X-Eclipse-App-Key", "")
    return hmac.compare_digest(given.encode("utf-8"), secret.encode("utf-8"))


async def _handle_public_trial_create_impl(request: web.Request) -> web.Response:
    """POST /api/public/trial/create — активирует бесплатный пробный период
    для текущего залогиненного аккаунта (по коду или OAuth). Без оплаты —
    сразу провижинит рабочий ключ, как и обычная покупка.

    Защищено от ботов: rate-limit по IP + обязательная проверка Cloudflare
    Turnstile (токен передаётся в теле запроса как turnstile_token)."""
    account_id = _verify_session(request.cookies.get("site_session"))
    if not account_id:
        return web.json_response({"error": "unauthorized"}, status=401)

    from bot.services.license import is_feature_available as _lic_ok
    if not _lic_ok("trial_period"):
        return web.json_response(
            {"error": "feature_unavailable", "message": "Пробный период недоступен."}, status=403,
        )

    client_ip = _get_client_ip(request)
    if not _trial_rate_limit_check(client_ip):
        return web.json_response(
            {"error": "rate_limited", "message": "Слишком много попыток. Попробуйте позже."},
            status=429,
        )

    try:
        body = await request.json()
    except json.JSONDecodeError:
        body = {}

    # Запрос из нативного приложения (не браузер) — Cloudflare Turnstile
    # там технически невозможен без встроенного WebView-виджета. Пропускаем
    # проверку капчи для этого канала: реальная защита от накрутки триалов
    # здесь — rate-limit по IP (уже проверен выше) и обязательное требование
    # настоящей авторизованной сессии (код из бота или OAuth), а не просто
    # этот заголовок — он не секрет и не заменяет капчу как таковую.
    is_app_request = _is_trusted_app_request(request)
    if not is_app_request:
        turnstile_token = body.get("turnstile_token", "")
        if not await _verify_turnstile_token(turnstile_token, client_ip):
            _trial_rate_limit_record(client_ip)
            return web.json_response(
                {"error": "captcha_failed", "message": "Не удалось подтвердить, что вы не робот. Попробуйте ещё раз."},
                status=400,
            )

    from database.requests import (
        is_trial_enabled, get_trial_tariff_id, get_site_account_by_id,
        create_anonymous_purchase, link_purchase_to_account,
        save_anonymous_purchase_provisioning, mark_anonymous_purchase_paid,
        get_anonymous_purchase_by_order_id, get_user_internal_id, mark_trial_used,
    )

    if not is_trial_enabled():
        return web.json_response({"error": "trial_disabled", "message": "Пробный период сейчас недоступен."}, status=400)

    trial_tariff_id = get_trial_tariff_id()
    if not trial_tariff_id:
        return web.json_response({"error": "trial_not_configured", "message": "Пробный период не настроен."}, status=400)

    account = get_site_account_by_id(account_id)
    if not account:
        return web.json_response({"error": "account_not_found"}, status=404)

    if _site_account_used_trial(account_id, trial_tariff_id, account.get("telegram_id")):
        return web.json_response({"error": "trial_already_used", "message": "Вы уже использовали пробный период."}, status=400)

    order_id = _generate_public_order_id()
    try:
        create_anonymous_purchase(order_id, trial_tariff_id)
        link_purchase_to_account(order_id, account_id)

        from bot.services.anonymous_purchase import provision_anonymous_vpn_key
        result = await provision_anonymous_vpn_key(trial_tariff_id, order_id, site_account_id=account_id)
        save_anonymous_purchase_provisioning(order_id, result["key_id"], result["sub_url"], result["placeholder_user_id"])
        mark_anonymous_purchase_paid(order_id, "trial")  # без реального платежа, просто маркер завершения
        _trial_rate_limit_record(client_ip)  # v1.157: считаем и успешные выдачи (раньше — только провалы капчи)

        if account.get("telegram_id"):
            real_user_id = get_user_internal_id(account["telegram_id"])
            if real_user_id:
                mark_trial_used(real_user_id)

        # Уведомление админам о выдаче пробного периода с сайта — раньше
        # отсутствовало (единственный путь оформления, не заходящий в
        # provision_anonymous_vpn_key через уже уведомляющие обёртки).
        try:
            from bot.services.notifications import notify_admins_payment
            from bot.utils.runtime_state import get_bot_instance
            from database.requests import get_tariff_by_id

            trial_tariff = get_tariff_by_id(trial_tariff_id)
            buyer_label = f"🌐 сайт ({account_id})"
            if account.get("email"):
                buyer_label = f"🌐 {account['email']}"
            elif account.get("provider"):
                buyer_label = f"🌐 сайт ({account['provider']})"

            notify_order = {
                "order_id": order_id,
                "user_id": None,
                "_site_buyer_label": buyer_label,
                "_payment_action": "trial",
                "tariff_id": trial_tariff_id,
                "tariff_name": trial_tariff.get("name") if trial_tariff else "—",
                "vpn_key_id": result["key_id"],
                "payment_type": "trial",
                "final_amount_cents": 0,
                "price_rub": 0,
            }
            bot_instance = get_bot_instance()
            if bot_instance:
                await notify_admins_payment(bot_instance, notify_order)
        except Exception as notify_err:
            logger.warning(f"Ошибка отправки уведомления админам о пробном периоде с сайта order={order_id}: {notify_err}")

        purchase = get_anonymous_purchase_by_order_id(order_id)
        return web.json_response({
            "status": "paid",
            "claim_code": purchase["claim_code"],
            "sub_url": result["sub_url"],
        })
    except Exception as e:
        logger.error(f"Public trial creation error: {e}")
        return web.json_response({"error": "trial_creation_failed", "message": "Не удалось активировать пробный период. Попробуйте позже."}, status=502)


_TRIAL_IN_PROGRESS_ACCOUNTS: set = set()
_TRIAL_IN_PROGRESS_IPS: set = set()


async def handle_public_trial_create(request: web.Request) -> web.Response:
    """v1.186: обёртка над выдачей пробника. Пока запрос одного аккаунта или одного IP
    ещё выполняется, параллельные запросы отклоняются — иначе несколько одновременных
    запросов проходили проверку «ещё не брал» до того, как первый ключ был записан,
    и один аккаунт получал несколько пробных ключей."""
    account_id = _verify_session(request.cookies.get("site_session"))
    client_ip = _get_client_ip(request)
    if account_id and (account_id in _TRIAL_IN_PROGRESS_ACCOUNTS or client_ip in _TRIAL_IN_PROGRESS_IPS):
        return web.json_response(
            {"error": "in_progress", "message": "Пробный период уже оформляется. Подождите несколько секунд."},
            status=429,
        )
    if account_id:
        _TRIAL_IN_PROGRESS_ACCOUNTS.add(account_id)
        _TRIAL_IN_PROGRESS_IPS.add(client_ip)
    try:
        return await _handle_public_trial_create_impl(request)
    finally:
        _TRIAL_IN_PROGRESS_ACCOUNTS.discard(account_id)
        _TRIAL_IN_PROGRESS_IPS.discard(client_ip)


def _get_site_base_url(request: web.Request) -> str:
    from database.requests import get_effective_webapp_url
    default_host = get_effective_webapp_url().replace("https://", "").replace("http://", "").rstrip("/")
    scheme = request.headers.get("X-Forwarded-Proto", "https")
    host = request.headers.get("Host", default_host)
    return f"{scheme}://{host}"


async def handle_oauth_providers(request: web.Request) -> web.Response:
    """GET /api/public/oauth/providers — какие провайдеры реально
    настроены на сервере (чтобы фронтенд не показывал нерабочие кнопки)."""
    from bot.services.oauth import get_configured_providers
    return web.json_response({"providers": get_configured_providers()})


async def handle_oauth_start(request: web.Request) -> web.Response:
    """GET /auth/{provider}/start — редирект на страницу авторизации провайдера."""
    provider = request.match_info.get("provider", "")
    from bot.services.oauth import OAUTH_PROVIDERS, is_provider_configured, build_authorize_url

    if provider not in OAUTH_PROVIDERS:
        return web.Response(text="Неизвестный провайдер входа.", status=404)
    if not is_provider_configured(provider):
        return web.Response(text=f"Вход через {provider} сейчас не настроен на сервере.", status=503)

    state = _secrets_mod.token_urlsafe(24)
    redirect_uri = f"{_get_site_base_url(request)}/auth/{provider}/callback"
    url = build_authorize_url(provider, redirect_uri, state)

    resp = web.HTTPFound(url)
    resp.set_cookie("oauth_state", state, max_age=600, httponly=True, secure=True, samesite="Lax")

    # Если пользователь уже залогинен (например, вошёл по коду из бота) и
    # нажал "привязать OAuth" — запоминаем, к какому аккаунту привязывать.
    existing_account_id = _verify_session(request.cookies.get("site_session"))
    if existing_account_id and request.query.get("link") == "1":
        resp.set_cookie("oauth_link_account_id", str(existing_account_id), max_age=600, httponly=True, secure=True, samesite="Lax")

    # Запрос из нативного Android-приложения (?client=app) — запоминаем,
    # чтобы в конце callback'а вернуть код обмена сессии вместо cookie
    # (cookie браузера всё равно не попадёт в OkHttp-клиент приложения).
    if request.query.get("client") == "app":
        resp.set_cookie("oauth_client", "app", max_age=600, httponly=True, secure=True, samesite="Lax")
        # v1.162: PKCE — приложение передаёт code_challenge (base64url от SHA-256 verifier)
        _challenge = (request.query.get("code_challenge") or "").strip()
        if 32 <= len(_challenge) <= 128 and all(ch.isalnum() or ch in "-_" for ch in _challenge):
            resp.set_cookie("oauth_challenge", _challenge, max_age=600, httponly=True, secure=True, samesite="Lax")

    return resp


async def handle_oauth_callback(request: web.Request) -> web.Response:
    """GET /auth/{provider}/callback — обмен кода на данные пользователя,
    создание/поиск аккаунта, установка сессии."""
    provider = request.match_info.get("provider", "")
    from bot.services.oauth import OAUTH_PROVIDERS, exchange_code_for_user_info

    if provider not in OAUTH_PROVIDERS:
        return web.Response(text="Неизвестный провайдер входа.", status=404)

    code = request.query.get("code")
    state = request.query.get("state")
    cookie_state = request.cookies.get("oauth_state")
    if not code or not state or not cookie_state or state != cookie_state:
        return web.Response(text="Не удалось подтвердить запрос авторизации. Попробуйте войти заново.", status=400)

    redirect_uri = f"{_get_site_base_url(request)}/auth/{provider}/callback"
    try:
        user_info = await exchange_code_for_user_info(provider, code, redirect_uri)
    except Exception as e:
        logger.error(f"OAuth callback error ({provider}): {e}")
        return web.Response(text="Не удалось авторизоваться. Попробуйте ещё раз.", status=502)

    if not user_info.get("provider_user_id"):
        return web.Response(text="Провайдер не вернул идентификатор пользователя.", status=502)

    from database.requests import get_or_create_site_account, attach_oauth_to_existing_account

    link_account_id = request.cookies.get("oauth_link_account_id")
    _session_account = _verify_session(request.cookies.get("site_session"))
    if link_account_id and not (_session_account and str(_session_account) == str(link_account_id)):
        # v1.155: cookie без подтверждения подписанной сессией — игнорируем (защита от захвата аккаунта)
        link_account_id = None
    if link_account_id:
        ok = attach_oauth_to_existing_account(
            int(link_account_id), provider, user_info["provider_user_id"],
            email=user_info.get("email"), display_name=user_info.get("display_name"),
        )
        if not ok:
            resp = web.Response(text="Этот аккаунт уже привязан к другому пользователю сайта.", status=409)
            resp.del_cookie("oauth_state")
            resp.del_cookie("oauth_link_account_id")
            return resp
        account_id = int(link_account_id)
    else:
        account = get_or_create_site_account(
            provider, user_info["provider_user_id"],
            email=user_info.get("email"), display_name=user_info.get("display_name"),
            referred_by_code=request.cookies.get("site_ref_code"),
        )
        account_id = account["id"]

    is_app_client = request.cookies.get("oauth_client") == "app"

    if is_app_client:
        # Запрос из приложения — браузерная cookie бесполезна для OkHttp
        # клиента приложения. Возвращаем одноразовый короткоживущий код
        # обмена через deep-link в приложение вместо cookie.
        from database.requests import create_oauth_exchange_code
        exchange_code = create_oauth_exchange_code(account_id, code_challenge=request.cookies.get("oauth_challenge") or None)
        resp = web.HTTPFound(f"eclipsevpn://oauth-callback?code={exchange_code}")
        resp.del_cookie("oauth_state")
        resp.del_cookie("oauth_link_account_id")
        resp.del_cookie("oauth_client")
        return resp

    session_value = _sign_session(account_id)
    resp = web.HTTPFound("/shop#account")
    resp.set_cookie("site_session", session_value, max_age=_SESSION_TTL_SECONDS, httponly=True, secure=True, samesite="Lax")
    resp.del_cookie("oauth_state")
    resp.del_cookie("oauth_link_account_id")
    resp.del_cookie("oauth_client")
    return resp


async def handle_zvonok_postback(request: web.Request) -> web.Response:
    """GET /api/public/zvonok/postback — постбек (вебхук) от zvonok.com,
    приходит МГНОВЕННО при завершении звонка (успех или неответ), в
    отличие от периодического опроса их API. Настраивается на стороне
    zvonok.com отдельно для каждого из двух событий (см. дев-панель →
    Подтверждение номера → Постбеки):

    Успешный дозвон:
      https://ТВОЙ-ДОМЕН/api/public/zvonok/postback?call_id={{ct_call_id}}&result=ok
    Нет ответа на звонок:
      https://ТВОЙ-ДОМЕН/api/public/zvonok/postback?call_id={{ct_call_id}}&result=no_answer

    Не критичен для работы верификации — опрос API остаётся резервным
    вариантом, если постбек не настроен или не дошёл (см.
    check_phone_confirmation)."""
    from database.requests import get_zvonok_postback_token

    token = request.query.get("token", "").strip()
    expected_token = get_zvonok_postback_token()
    if not token or not hmac.compare_digest(token, expected_token):
        logger.warning("Zvonok postback: отклонён запрос с неверным/отсутствующим token")
        return web.Response(text="forbidden", status=403)

    call_id = request.query.get("call_id", "").strip()
    result = request.query.get("result", "").strip()
    if not call_id or result not in ("ok", "no_answer"):
        return web.Response(text="bad_request", status=400)

    from bot.services.zvonok_verification import save_postback_status
    save_postback_status(call_id, confirmed=(result == "ok"))
    return web.Response(text="ok")


# v1.155: call_id привязан к номеру, для которого был запрошен звонок, и одноразовый
_ZV_CALL_BINDINGS: Dict[str, tuple] = {}
_ZV_CALL_TTL_SECONDS = 1800


def _zv_norm_phone(phone: str) -> str:
    from bot.services.trial_phone_registry import normalize_phone
    return normalize_phone(phone or "")


def _zv_bind_call(call_id, phone: str) -> None:
    import time as _t
    now = _t.time()
    for k in [k for k, v in _ZV_CALL_BINDINGS.items() if now - v[1] > _ZV_CALL_TTL_SECONDS]:
        _ZV_CALL_BINDINGS.pop(k, None)
    _ZV_CALL_BINDINGS[str(call_id)] = [_zv_norm_phone(phone), now, 0]


def _zv_call_matches(call_id, phone: str) -> bool:
    import time as _t
    entry = _ZV_CALL_BINDINGS.get(str(call_id))
    if not entry or _t.time() - entry[1] > _ZV_CALL_TTL_SECONDS:
        return False
    return entry[0] == _zv_norm_phone(phone)


def _zv_code_attempt_ok(call_id) -> bool:
    """v1.157: не более 5 попыток ввода кода на один звонок (защита от подбора)."""
    entry = _ZV_CALL_BINDINGS.get(str(call_id))
    if not entry:
        return False
    entry[2] += 1
    if entry[2] > 5:
        _ZV_CALL_BINDINGS.pop(str(call_id), None)
        return False
    return True


def _zv_consume_call(call_id) -> None:
    _ZV_CALL_BINDINGS.pop(str(call_id), None)


async def handle_public_auth_phone_request(request: web.Request) -> web.Response:
    # v1.157: лимиты — каждый звонок стоит денег и беспокоит владельца номера
    _ip = _get_client_ip(request)
    try:
        _rb = await request.json()
    except Exception:
        _rb = {}
    _ph = _zv_norm_phone((_rb.get("phone") or "").strip()) if isinstance(_rb, dict) else ""
    if not _rl_allowed(f"phreq-ip:{_ip}", 10, 3600) or (_ph and not _rl_allowed(f"phreq-ph:{_ph}", 3, 600)):
        return web.json_response(
            {"error": "rate_limited", "message": "Слишком много запросов. Попробуйте позже."}, status=429
        )
    _rl_record(f"phreq-ip:{_ip}")
    if _ph:
        _rl_record(f"phreq-ph:{_ph}")
    resp = await _handle_public_auth_phone_request_impl(request)
    try:
        payload = json.loads(resp.text)
        if payload.get("status") == "ok" and payload.get("call_id"):
            body = await request.json()
            _zv_bind_call(payload["call_id"], (body.get("phone") or "").strip())
    except Exception as e:
        logger.warning(f"Не удалось привязать call_id к номеру: {e}")
    return resp


async def _handle_public_auth_phone_request_impl(request: web.Request) -> web.Response:
    """POST /api/public/auth/phone/request — инициирует вход по номеру
    телефона (отдельный, полноценный способ входа — НЕ путать с
    /trial/phone/request, который лишь защита от повторного пробника
    для уже залогиненного аккаунта). Сессия ещё не нужна — это САМ вход."""
    from database.requests import is_site_auth_method_enabled

    if not is_site_auth_method_enabled('phone'):
        return web.json_response({"error": "method_disabled", "message": "Вход по телефону сейчас недоступен."}, status=400)

    try:
        body = await request.json()
    except json.JSONDecodeError:
        body = {}
    phone = (body.get("phone") or "").strip()
    if not phone:
        return web.json_response({"error": "phone_required", "message": "Укажите номер телефона."}, status=400)

    from database.requests import get_zvonok_verification_method
    import bot.services.zvonok_verification as zv

    method = get_zvonok_verification_method()

    if method == "pincode":
        # Мы сами звоним клиенту — код нужно показать ему НА САЙТЕ
        # заранее, он вводит его с клавиатуры телефона во время звонка.
        result = await zv.request_phone_confirmation_pincode(phone)
        if not result or not result.get("pincode"):
            return web.json_response(
                {"error": "verification_unavailable", "message": "Вход по телефону временно недоступен, попробуйте позже."},
                status=503,
            )
        return web.json_response({"status": "ok", "method": "pincode", "pincode": result["pincode"], "call_id": result.get("call_id")})

    if method == "flashcall_real":
        # Настоящий Flash Call — клиент читает последние 4 цифры номера,
        # с которого ему позвонили, вводит их у нас на сайте.
        result = await zv.request_phone_confirmation_flashcall_real(phone)
        if not result or not result.get("call_id"):
            return web.json_response(
                {"error": "verification_unavailable", "message": "Вход по телефону временно недоступен, попробуйте позже."},
                status=503,
            )
        return web.json_response({"status": "ok", "method": "flashcall_real", "call_id": result["call_id"]})

    if method == "voice_code":
        # Робот диктует код — клиент вводит услышанное у нас на сайте.
        result = await zv.request_phone_confirmation_voice_code(phone)
        if not result or not result.get("call_id"):
            return web.json_response(
                {"error": "verification_unavailable", "message": "Вход по телефону временно недоступен, попробуйте позже."},
                status=503,
            )
        return web.json_response({"status": "ok", "method": "voice_code", "call_id": result["call_id"]})

    if method == "press_digit":
        result = await zv.request_phone_confirmation_press_digit(phone)
        if not result or not result.get("call_id"):
            return web.json_response(
                {"error": "verification_unavailable", "message": "Вход по телефону временно недоступен, попробуйте позже."},
                status=503,
            )
        return web.json_response({"status": "ok", "method": "press_digit", "call_id": result["call_id"]})

    result = await zv.request_phone_confirmation(phone)
    if not result or not result.get("allowed_phones_for_call"):
        return web.json_response(
            {"error": "verification_unavailable", "message": "Вход по телефону временно недоступен, попробуйте позже."},
            status=503,
        )
    return web.json_response({
        "status": "ok",
        "method": "flash_call",
        "allowed_phones_for_call": result["allowed_phones_for_call"],
        "call_id": result.get("call_id"),
    })


async def handle_public_auth_phone_check(request: web.Request) -> web.Response:
    """POST /api/public/auth/phone/check — проверяет подтверждение
    номера (опрашивает zvonok.com напрямую по номеру — своего локального
    состояния тут не нужно, в отличие от /trial/phone/check, где важно
    привязать подтверждение к конкретной уже начатой сессии). При
    успехе — находит/создаёт сайт-аккаунт по этому номеру и выдаёт сессию."""
    from database.requests import is_site_auth_method_enabled

    if not is_site_auth_method_enabled('phone'):
        return web.json_response({"error": "method_disabled"}, status=400)

    try:
        body = await request.json()
    except json.JSONDecodeError:
        body = {}
    phone = (body.get("phone") or "").strip()
    call_id = body.get("call_id")
    if not phone or not call_id:
        return web.json_response({"error": "phone_required"}, status=400)
    if not _zv_call_matches(call_id, phone):
        return web.json_response({"status": "ok", "verified": False, "message": "Запросите звонок заново."})

    from database.requests import get_zvonok_verification_method
    import bot.services.zvonok_verification as zv

    method = get_zvonok_verification_method()
    if method in ("voice_code", "flashcall_real"):
        # Здесь клиент присылает то, что ему продиктовал робот, либо
        # последние 4 цифры номера, с которого поступил звонок —
        # сверяем ЛОКАЛЬНО с тем, что мы сами получили при инициации
        # звонка (не требует похода к API Zvonok).
        entered_code = (body.get("entered_code") or "").strip()
        if not entered_code:
            msg = "Введите код, который продиктовал робот." if method == "voice_code" else "Введите 4 цифры номера, с которого поступил звонок."
            return web.json_response({"status": "ok", "verified": False, "message": msg})
        if not _zv_code_attempt_ok(call_id):
            return web.json_response({"status": "ok", "verified": False, "message": "Слишком много попыток. Запросите звонок заново."})
        confirmed = zv.check_voice_code(call_id, entered_code)
    else:
        # Кэшу постбека доверяем ТОЛЬКО для flash_call ("Звонок на
        # проверочный номер") — там "Успешный дозвон" на стороне Zvonok
        # действительно означает "подтверждено". Для методов с кодом/
        # цифрой (pincode, press_digit) "Успешный дозвон" может
        # означать лишь "абонент ответил", а НЕ "ввёл верный код" —
        # доверять кэшу здесь опасно, даже если постбек для этой
        # кампании случайно окажется настроен.
        confirmed = await zv.check_phone_confirmation(call_id, trust_postback_cache=(method == "flash_call"))

    if not confirmed:
        return web.json_response({"status": "ok", "verified": False})
    _zv_consume_call(call_id)

    from bot.services.trial_phone_registry import normalize_phone, get_telegram_id_for_verified_phone

    normalized = normalize_phone(phone)
    existing_telegram_id = get_telegram_id_for_verified_phone(normalized)
    if existing_telegram_id:
        # Этот номер уже подтверждался через бота (например, при получении
        # пробного периода) — узнаём в нём того же человека и входим в его
        # СУЩЕСТВУЮЩИЙ Telegram-аккаунт со всей историей, а не заводим
        # отдельный пустой аккаунт с provider='phone'.
        from database.db_accounts import _get_or_create_telegram_site_account
        account = _get_or_create_telegram_site_account(existing_telegram_id, referred_by_code=request.cookies.get("site_ref_code"))
        account_type = "telegram"
    else:
        from database.db_accounts import get_or_create_site_account_by_phone
        account = get_or_create_site_account_by_phone(normalized, referred_by_code=request.cookies.get("site_ref_code"))
        account_type = "phone"

    session_value = _sign_session(account["id"])
    resp = web.json_response({"status": "ok", "verified": True, "account_type": account_type})
    resp.set_cookie("site_session", session_value, max_age=_SESSION_TTL_SECONDS, httponly=True, secure=True, samesite="Lax")
    return resp


async def handle_public_account_session_login(request: web.Request) -> web.Response:
    """POST /api/public/account/session-login — вход по коду (из бота ИЛИ
    коду покупки на сайте), устанавливает сессионную cookie для дальнейших
    визитов без повторного ввода кода."""
    try:
        data = await request.json()
    except json.JSONDecodeError:
        return web.json_response({"error": "invalid_json"}, status=400)

    code = (data.get("code") or "").strip()
    if not code:
        return web.json_response({"ok": False, "message": "Введите код."}, status=400)

    _login_ip = _get_client_ip(request)
    if not _rl_allowed(f"login-fail:{_login_ip}", 10, 600):
        return web.json_response({"ok": False, "message": "Слишком много неудачных попыток. Попробуйте через 10 минут."}, status=429)

    from database.requests import consume_site_login_code, is_site_auth_method_enabled
    from database.db_accounts import _get_or_create_telegram_site_account

    telegram_id = consume_site_login_code(code) if is_site_auth_method_enabled('code') else None
    if telegram_id:
        account = _get_or_create_telegram_site_account(telegram_id, referred_by_code=request.cookies.get("site_ref_code"))
        session_value = _sign_session(account["id"])
        resp = web.json_response({"ok": True, "account_type": "telegram"})
        resp.set_cookie("site_session", session_value, max_age=_SESSION_TTL_SECONDS, httponly=True, secure=True, samesite="Lax")
        return resp

    # Не код из бота — пробуем как claim_code анонимной покупки
    from database.requests import get_anonymous_purchase_by_claim_code, link_purchase_to_account

    purchase = get_anonymous_purchase_by_claim_code(code)
    if not purchase or not purchase.get("vpn_key_id"):
        _rl_record(f"login-fail:{_login_ip}")
        return web.json_response({"ok": False, "message": "Код не найден или ключ ещё не готов. Проверьте правильность ввода."})

    site_account_id = purchase.get("site_account_id")
    if not site_account_id:
        from database.requests import get_or_create_site_account
        # Гостевая покупка без аккаунта — создаём лёгкий "виртуальный" аккаунт
        # на основе claim_code, чтобы дать такую же сессию
        account = get_or_create_site_account("guest_code", code.strip().upper())
        link_purchase_to_account(purchase["order_id"], account["id"])
        site_account_id = account["id"]

    session_value = _sign_session(site_account_id)
    resp = web.json_response({"ok": True, "account_type": "guest"})
    resp.set_cookie("site_session", session_value, max_age=_SESSION_TTL_SECONDS, httponly=True, secure=True, samesite="Lax")
    return resp


async def handle_public_account_oauth_exchange(request: web.Request) -> web.Response:
    """POST /api/public/account/oauth-exchange — обменивает одноразовый код
    (полученный приложением через deep-link после OAuth-входа в системном
    браузере) на cookie-сессию для дальнейших запросов ИЗ приложения.
    Body JSON: {"code": "..."}"""
    try:
        data = await request.json()
    except json.JSONDecodeError:
        return web.json_response({"ok": False, "message": "invalid_json"}, status=400)

    code = (data.get("code") or "").strip()
    if not code:
        return web.json_response({"ok": False, "message": "Код не передан."}, status=400)

    from database.requests import consume_oauth_exchange_code

    _xip = _get_client_ip(request)
    if not _rl_allowed(f"oauth-xchg-fail:{_xip}", 10, 600):
        return web.json_response({"ok": False, "message": "Слишком много попыток. Попробуйте позже."}, status=429)
    account_id = consume_oauth_exchange_code(code, (data.get("code_verifier") or "").strip() or None)
    if not account_id:
        _rl_record(f"oauth-xchg-fail:{_xip}")
        return web.json_response({"ok": False, "message": "Код недействителен или истёк."}, status=400)

    session_value = _sign_session(account_id)
    resp = web.json_response({"ok": True})
    resp.set_cookie("site_session", session_value, max_age=_SESSION_TTL_SECONDS, httponly=True, secure=True, samesite="Lax")
    return resp


async def handle_license_check(request: web.Request) -> web.Response:
    """POST /api/license/check — эндпоинт лицензионного сервера (работает
    на ГЛАВНОЙ инсталляции). Клиентские боты whitelabel-партнёров стучатся
    сюда своим license_key, чтобы узнать функции и срок действия.
    Body JSON: {"license_key": "ECLW-XXXX-XXXX-XXXX"}.

    v1.165: ограничение частоты по IP (общее и отдельно по неудачным
    проверкам — защита от перебора ключей). Ответ 429 не содержит поля
    valid, поэтому клиент не принимает его за отзыв лицензии."""
    _lip = _get_client_ip(request)
    if not _rl_allowed(f"lic-req:{_lip}", 60, 600) or not _rl_allowed(f"lic-fail:{_lip}", 10, 600):
        return web.json_response({"error": "rate_limited"}, status=429)
    _rl_record(f"lic-req:{_lip}")

    try:
        data = await request.json()
    except json.JSONDecodeError:
        return web.json_response({"valid": False, "message": "Некорректный запрос."}, status=400)
    if not isinstance(data, dict):
        return web.json_response({"valid": False, "message": "Некорректный запрос."}, status=400)

    license_key = str(data.get("license_key") or "").strip()
    if not license_key:
        return web.json_response({"valid": False, "message": "Не передан license_key."}, status=400)

    from database.db_licenses import check_license_validity, register_activation, log_license_event
    result = check_license_validity(license_key)
    if not result.get("valid"):
        _rl_record(f"lic-fail:{_lip}")
        return web.json_response(result)

    import re
    # v1.166: учёт установок. Бот версии 1.166+ присылает instance_id и nonce.
    instance_id = str(data.get("instance_id") or "").strip()
    nonce = str(data.get("nonce") or "").strip()
    if instance_id and not re.fullmatch(r"[A-Za-z0-9_-]{8,64}", instance_id):
        return web.json_response({"valid": False, "message": "Некорректный идентификатор установки."}, status=400)
    if nonce and not re.fullmatch(r"[A-Za-z0-9_-]{8,64}", nonce):
        return web.json_response({"valid": False, "message": "Некорректный запрос."}, status=400)

    legacy = not instance_id
    allowed, used = register_activation(license_key, instance_id or f"legacy-{_lip}", _lip)
    if not allowed:
        _rl_record(f"lic-fail:{_lip}")
        log_license_event(license_key, "instance_refused", f"{instance_id[:8]} ip={_lip}")
        return web.json_response({
            "valid": False,
            "message": "Лицензия уже используется на другом сервере. Если вы переехали — напишите владельцу лицензии, он освободит место.",
        })

    if instance_id and nonce:
        try:
            from bot.services.license_signing import sign_license_response
            result = dict(result)
            result["signed"] = sign_license_response(result, license_key, instance_id, nonce)
        except Exception as e:
            logger.error(f"Лицензия: не удалось подписать ответ ({e}) — отправляю без подписи")
    return web.json_response(result)


async def handle_license_trial(request: web.Request) -> web.Response:
    """POST /api/license/trial — пробная лицензия по запросу из бота партнёра (только на
    главном сервере). Body: {"instance_id", "telegram_id"}. Одна на установку и на аккаунт Telegram,
    не больше 2 с одного IP за 30 дней."""
    import re
    _lip = _get_client_ip(request)
    if not _rl_allowed(f"lic-trial:{_lip}", 5, 3600):
        return web.json_response({"error": "rate_limited"}, status=429)
    _rl_record(f"lic-trial:{_lip}")

    from bot.services.license import is_license_server
    from bot.services.license_trial import trial_enabled, trial_days, trial_features
    if not is_license_server() or not trial_enabled():
        return web.json_response({"ok": False, "reason": "disabled", "message": "Пробный период сейчас недоступен."})

    try:
        data = await request.json()
    except json.JSONDecodeError:
        return web.json_response({"ok": False, "reason": "bad", "message": "Некорректный запрос."}, status=400)
    if not isinstance(data, dict):
        return web.json_response({"ok": False, "reason": "bad", "message": "Некорректный запрос."}, status=400)
    instance_id = str(data.get("instance_id") or "").strip()
    try:
        telegram_id = int(data.get("telegram_id"))
    except (TypeError, ValueError):
        telegram_id = 0
    if not re.fullmatch(r"[A-Za-z0-9_-]{8,64}", instance_id) or telegram_id <= 0:
        return web.json_response({"ok": False, "reason": "bad", "message": "Некорректный запрос."}, status=400)

    from database.db_licenses import issue_trial_license, count_recent_trials_by_ip, log_license_event
    if count_recent_trials_by_ip(_lip, 30) >= 2:
        return web.json_response({
            "ok": False, "reason": "limit",
            "message": "С вашего адреса уже выдавались пробные лицензии. Выберите тариф: команда /buy_license в главном боте.",
        })
    days = trial_days()
    res = issue_trial_license(telegram_id, f"tg{telegram_id}", days, trial_features(), instance_id=instance_id, ip=_lip)
    if res["status"] == "already":
        return web.json_response({
            "ok": False, "reason": "used",
            "message": "Пробный период для этой установки или аккаунта уже использован. Выберите тариф: /buy_license в главном боте.",
        })
    log_license_event(res["license_key"], "trial_issued", f"remote tg={telegram_id} inst={instance_id[:8]} ip={_lip}")
    return web.json_response({"ok": True, "license_key": res["license_key"], "expires_at": res["expires_at"], "days": days})


async def handle_public_account_claim_purchase(request: web.Request) -> web.Response:
    """POST /api/public/account/claim-purchase — для УЖЕ залогиненного (через
    OAuth/телефон) аккаунта: привязывает к нему покупку с сайта по её
    claim_code — например, автоматически при входе, если этот же браузер
    ранее оформлял анонимную покупку до входа (см. tryAutoClaimPendingCode
    на фронтенде). В отличие от /account/session-login (который создаёт
    для claim_code НОВУЮ сессию), этот эндпоинт добавляет покупку к
    ТЕКУЩЕЙ, уже открытой сессии, не подменяя её."""
    account_id = _verify_session(request.cookies.get("site_session"))
    if not account_id:
        return web.json_response({"ok": False, "message": "Сессия истекла, войдите заново."}, status=401)

    try:
        data = await request.json()
    except json.JSONDecodeError:
        return web.json_response({"ok": False, "message": "Некорректный запрос."}, status=400)

    code = (data.get("code") or "").strip()
    if not code:
        return web.json_response({"ok": False, "message": "Введите код."}, status=400)

    from database.requests import get_anonymous_purchase_by_claim_code, link_purchase_to_account

    purchase = get_anonymous_purchase_by_claim_code(code)
    if not purchase:
        return web.json_response({"ok": False, "message": "Код не найден."})

    existing_account_id = purchase.get("site_account_id")
    if existing_account_id and existing_account_id != account_id:
        # Уже привязана к ДРУГОМУ аккаунту — не перехватываем чужую покупку.
        return web.json_response({"ok": False, "message": "Этот код уже привязан к другому аккаунту."})

    if not existing_account_id:
        link_purchase_to_account(purchase["order_id"], account_id)

    return web.json_response({"ok": True})


async def handle_public_account_link_code(request: web.Request) -> web.Response:
    """POST /api/public/account/link-code — для УЖЕ залогиненного через OAuth
    аккаунта: привязывает его к существующему клиенту бота по коду из бота
    («Мои ключи» → «Управлять на сайте»). Нужно для старых клиентов бота,
    которые впервые заходят на сайт через Google/Яндекс/VK и иначе не
    увидели бы свои реальные ключи."""
    account_id = _verify_session(request.cookies.get("site_session"))
    if not account_id:
        return web.json_response({"ok": False, "message": "Сессия истекла, войдите заново."}, status=401)

    try:
        data = await request.json()
    except json.JSONDecodeError:
        return web.json_response({"ok": False, "message": "Некорректный запрос."}, status=400)

    code = (data.get("code") or "").strip()
    if not code:
        return web.json_response({"ok": False, "message": "Введите код."}, status=400)

    from database.requests import consume_site_login_code, link_oauth_to_site_account

    telegram_id = consume_site_login_code(code)
    if not telegram_id:
        return web.json_response({"ok": False, "message": "Код не найден, уже использован или истёк."})

    if not link_oauth_to_site_account(account_id, telegram_id):
        return web.json_response({"ok": False, "message": "Не удалось привязать аккаунт. Обратитесь в поддержку."})

    # v1.186: если этот сайт-аккаунт уже брал пробник — помечаем и Telegram-пользователя,
    # иначе тот же человек мог взять второй пробник уже в боте
    try:
        from database.requests import get_trial_tariff_id, get_user_internal_id, mark_trial_used
        _tid = get_trial_tariff_id()
        if _tid and _site_account_used_trial(account_id, _tid, None):
            _uid = get_user_internal_id(telegram_id)
            if _uid:
                mark_trial_used(_uid)
    except Exception as _e:
        logger.warning(f"link-code: не удалось перенести отметку о пробнике: {_e}")

    return web.json_response({"ok": True})


async def handle_public_account_referral(request: web.Request) -> web.Response:
    """GET /api/public/account/referral — собственная реферальная
    ссылка сайт-аккаунта. Работает и для чисто сайтовых пользователей
    (без Telegram) — код привязан к их стабильной служебной личности
    (site_accounts.placeholder_user_id), той же, что используется для
    провижининга их ключей, поэтому баланс/дни от рефералов и покупки
    накапливаются на одной и той же внутренней личности."""
    account_id = _verify_session(request.cookies.get("site_session"))
    if not account_id:
        return web.json_response({"ok": True, "logged_in": False})

    from database.requests import get_site_account_by_id, get_effective_webapp_url, is_referral_enabled
    account = get_site_account_by_id(account_id)
    if not account:
        return web.json_response({"error": "account_not_found"}, status=404)

    if not is_referral_enabled():
        return web.json_response({"ok": True, "enabled": False})

    if account.get("telegram_id"):
        from database.requests import get_user_internal_id
        internal_user_id = get_user_internal_id(account["telegram_id"])
    else:
        from database.db_accounts import get_or_create_placeholder_user_for_site_account
        internal_user_id = get_or_create_placeholder_user_for_site_account(account_id)

    from database.requests import ensure_user_referral_code, get_user_balance
    referral_code = ensure_user_referral_code(internal_user_id)
    webapp_url = get_effective_webapp_url()
    referral_link = f"{webapp_url}/shop?ref={referral_code}" if webapp_url else ""

    return web.json_response({
        "ok": True,
        "enabled": True,
        "referral_code": referral_code,
        "referral_link": referral_link,
        "balance_cents": get_user_balance(internal_user_id),
    })


async def handle_public_account_session(request: web.Request) -> web.Response:
    """GET /api/public/account/session — данные кабинета для текущей
    сессии (OAuth или вход по коду). Показывает ВСЕ ключи для клиентов,
    вошедших через Telegram-мост."""
    account_id = _verify_session(request.cookies.get("site_session"))
    if not account_id:
        return web.json_response({"ok": True, "logged_in": False})

    from database.requests import get_site_account_by_id
    account = get_site_account_by_id(account_id)
    if not account:
        return web.json_response({"ok": True, "logged_in": False})

    if account.get("telegram_id"):
        from database.requests import get_user_keys_for_display
        from bot.services.vpn_api import get_public_subscription_url_for_key

        keys = get_user_keys_for_display(account["telegram_id"])

        async def _with_sub_url(k):
            sub_url = None
            try:
                sub_url = await get_public_subscription_url_for_key({"sub_id": k.get("sub_id"), "server_id": k.get("server_id")})
            except Exception as e:
                logger.warning(f"Не удалось получить sub_url для ключа {k['id']}: {e}")
            return {
                "key_id": k["id"], "display_name": k["display_name"],
                "expires_at": k["expires_at"], "traffic_used": k["traffic_used"] or 0,
                "traffic_limit": k["traffic_limit"] or 0, "is_active": bool(k["is_active"]),
                "server_name": k.get("server_name"), "sub_url": sub_url,
                "is_trial": _is_trial_key(k),
            }

        keys_with_urls = await asyncio.gather(*[_with_sub_url(k) for k in keys])

        from database.requests import get_user_by_telegram_id, get_user_balance
        balance_cents = 0
        tg_user = get_user_by_telegram_id(account["telegram_id"])
        if tg_user:
            balance_cents = get_user_balance(tg_user["id"]) or 0
        rub = balance_cents // 100
        kop = balance_cents % 100
        balance_human = f"{rub} ₽" if kop == 0 else f"{rub}.{kop:02d} ₽"

        return web.json_response({
            "ok": True, "logged_in": True, "account_type": "telegram",
            "can_link_oauth": account.get("provider") in (None, "telegram"),
            "phone": account.get("phone"),
            "email": account.get("email"),
            "keys": keys_with_urls,
            "balance_cents": balance_cents,
            "balance_human": balance_human,
        })

    from database.requests import get_latest_purchase_for_account, get_key_details_by_id
    purchase = get_latest_purchase_for_account(account_id)
    if not purchase:
        return web.json_response({"ok": True, "logged_in": True, "account_type": "oauth_new", "keys": []})

    key = get_key_details_by_id(purchase["vpn_key_id"])
    if not key:
        return web.json_response({"ok": True, "logged_in": True, "account_type": "oauth_new", "keys": []})

    return web.json_response({
        "ok": True, "logged_in": True, "account_type": "oauth",
        "can_link_oauth": False,
        "phone": account.get("phone"),
        "email": account.get("email"),
        "keys": [{
            "key_id": key["id"],
            "display_name": key.get("tariff_name") or f"Ключ #{key['id']}",
            "expires_at": key.get("expires_at"),
            "traffic_used": key.get("traffic_used") or 0,
            "traffic_limit": key.get("traffic_limit") or 0,
            "is_active": True,
            "sub_url": purchase.get("sub_url"),
            "claim_code": purchase.get("claim_code"),
            "is_trial": _is_trial_key(key),
        }],
    })


async def handle_public_account_link_phone_check(request: web.Request) -> web.Response:
    """POST /api/public/account/link-phone-check — привязывает
    ПОДТВЕРЖДЁННЫЙ телефон к УЖЕ залогиненному аккаунту (в отличие от
    /api/public/auth/phone/check, который логинит/создаёт аккаунт по
    телефону как единственному способу входа). Используется для полной
    связки: пользователь вошёл через email/telegram и хочет добавить
    телефон, не теряя существующий email (в отличие от старого
    attach_oauth_to_existing_account, phone хранится в отдельной
    колонке — оба способа сосуществуют)."""
    account_id = _verify_session(request.cookies.get("site_session"))
    if not account_id:
        return web.json_response({"ok": False, "message": "Сессия истекла, войдите заново."}, status=401)

    try:
        body = await request.json()
    except json.JSONDecodeError:
        body = {}
    phone = (body.get("phone") or "").strip()
    call_id = body.get("call_id")
    if not phone or not call_id:
        return web.json_response({"ok": False, "message": "Не указан телефон или call_id."}, status=400)
    if not _zv_call_matches(call_id, phone):
        return web.json_response({"ok": True, "verified": False, "message": "Запросите звонок заново."})

    from database.requests import get_zvonok_verification_method
    import bot.services.zvonok_verification as zv

    method2 = get_zvonok_verification_method()
    if method2 in ("voice_code", "flashcall_real"):
        entered_code = (body.get("entered_code") or "").strip()
        if not entered_code:
            msg = "Введите код, который продиктовал робот." if method2 == "voice_code" else "Введите 4 цифры номера, с которого поступил звонок."
            return web.json_response({"ok": True, "verified": False, "message": msg})
        if not _zv_code_attempt_ok(call_id):
            return web.json_response({"ok": True, "verified": False, "message": "Слишком много попыток. Запросите звонок заново."})
        confirmed = zv.check_voice_code(call_id, entered_code)
    else:
        # См. подробный комментарий в handle_public_auth_phone_check —
        # кэшу постбека доверяем только для flash_call.
        confirmed = await zv.check_phone_confirmation(call_id, trust_postback_cache=(method2 == "flash_call"))
    if not confirmed:
        return web.json_response({"ok": True, "verified": False})
    _zv_consume_call(call_id)

    from bot.services.trial_phone_registry import normalize_phone
    from database.db_accounts import set_account_phone

    normalized = normalize_phone(phone)
    ok = set_account_phone(account_id, normalized)
    if not ok:
        return web.json_response({"ok": False, "message": "Этот номер уже привязан к другому аккаунту."}, status=409)

    return web.json_response({"ok": True, "verified": True, "phone": normalized})


async def handle_public_account_logout(request: web.Request) -> web.Response:
    """POST /api/public/account/logout — выход из личного кабинета."""
    resp = web.json_response({"ok": True})
    resp.del_cookie("site_session")
    return resp


def _verify_key_belongs_to_account(key_id: int, account: dict) -> bool:
    """Проверяет, что ключ реально принадлежит этому аккаунту личного
    кабинета — либо через telegram_id (существующие клиенты бота), либо
    через anonymous_purchases (OAuth/гостевые покупки с сайта)."""
    if account.get("telegram_id"):
        from database.requests import get_key_details_by_id
        key = get_key_details_by_id(key_id)
        return bool(key and key.get("telegram_id") == account["telegram_id"])

    from database.connection import get_db
    with get_db() as conn:
        row = conn.execute(
            "SELECT 1 FROM anonymous_purchases WHERE site_account_id = ? AND vpn_key_id = ? LIMIT 1",
            (account["id"], key_id),
        ).fetchone()
        return row is not None


async def handle_public_key_inbounds(request: web.Request) -> web.Response:
    """GET /api/public/key/{key_id}/inbounds — детальный список отдельных
    подключений (inbound) ключа для личного кабинета на сайте, та же
    логика, что и в WebApp-версии."""
    account_id = _verify_session(request.cookies.get("site_session"))
    if not account_id:
        return web.json_response({"error": "unauthorized"}, status=401)

    try:
        key_id = int(request.match_info["key_id"])
    except (KeyError, ValueError):
        return web.json_response({"error": "invalid_key_id"}, status=400)

    from database.requests import get_site_account_by_id, get_key_details_by_id
    account = get_site_account_by_id(account_id)
    if not account or not _verify_key_belongs_to_account(key_id, account):
        return web.json_response({"error": "key_not_found"}, status=404)

    key = get_key_details_by_id(key_id)
    if not key:
        return web.json_response({"error": "key_not_found"}, status=404)

    try:
        from bot.services.vpn_api import get_client
        from bot.utils.inbound_links import parse_and_group_inbound_links, add_ping_to_groups
        client = await get_client(key["server_id"])
        raw = await client.get_subscription_link(key["sub_id"])
        groups = parse_and_group_inbound_links(raw)
        groups = await add_ping_to_groups(groups)
        return web.json_response({"groups": groups})
    except Exception as e:
        logger.warning(f"handle_public_key_inbounds: ошибка для ключа {key_id}: {e}")
        return web.json_response({"error": "internal_error"}, status=500)


def _resolve_site_account_internal_user_id(account: dict) -> Optional[int]:
    """Внутренний user_id (бот) для этого сайт-аккаунта — та же служебная
    личность, на которой лежит баланс (см. handle_public_account_referral):
    для аккаунтов, привязанных к Telegram — обычный user по telegram_id,
    для чисто сайтовых (OAuth/телефон) — их постоянный placeholder_user_id."""
    if account.get("telegram_id"):
        from database.requests import get_user_internal_id
        return get_user_internal_id(account["telegram_id"])
    from database.db_accounts import get_or_create_placeholder_user_for_site_account
    return get_or_create_placeholder_user_for_site_account(account["id"])


async def handle_public_account_key_renew_create(request: web.Request) -> web.Response:
    """POST /api/public/account/key/renew/create — продление КОНКРЕТНОГО
    ключа для текущей сессии (работает и при нескольких ключах у клиента).
    Body JSON: {"key_id": int, "tariff_id": int}"""
    account_id = _verify_session(request.cookies.get("site_session"))
    if not account_id:
        return web.json_response({"error": "unauthorized"}, status=401)

    try:
        data = await request.json()
    except json.JSONDecodeError:
        return web.json_response({"error": "invalid_json"}, status=400)

    key_id = data.get("key_id")
    tariff_id = data.get("tariff_id")
    if not key_id:
        return web.json_response({"error": "key_id_required"}, status=400)

    from database.requests import get_site_account_by_id
    account = get_site_account_by_id(account_id)
    if not account or not _verify_key_belongs_to_account(int(key_id), account):
        return web.json_response({"error": "key_not_found"}, status=404)

    if not tariff_id:
        # ECLIPSE: автопродление на тот же тариф, что уже был у ключа —
        # клиент не выбирает тариф заново при обычном продлении.
        from database.requests import get_vpn_key_by_id
        current_key = get_vpn_key_by_id(int(key_id))
        if not current_key or not current_key.get("tariff_id"):
            return web.json_response({"error": "current_tariff_not_found"}, status=404)
        tariff_id = current_key["tariff_id"]

    from database.db_tariffs import get_tariff_by_id
    from database.db_payments import create_anonymous_purchase, save_anonymous_purchase_payment_id

    tariff = get_tariff_by_id(int(tariff_id))
    if not tariff:
        return web.json_response({"error": "tariff_not_found"}, status=404)

    price_rub = float(tariff.get("price_rub") or 0)
    if price_rub <= 0:
        return web.json_response({"error": "invalid_price"}, status=400)

    order_id = _generate_public_order_id()

    try:
        create_anonymous_purchase(order_id, tariff["id"], renewal_of_key_id=int(key_id))

        from aiogram import Bot
        from config import BOT_TOKEN
        from bot.services.billing import create_yookassa_qr_payment

        pay_bot = Bot(token=BOT_TOKEN)
        try:
            bot_info = await pay_bot.get_me()
            description = _pub(tariff)
            yk_result = await create_yookassa_qr_payment(
                amount_rub=price_rub, order_id=order_id, description=description,
                bot_name=bot_info.username,
            )
        finally:
            await pay_bot.session.close()

        save_anonymous_purchase_payment_id(order_id, yk_result["yookassa_payment_id"])
        # Фоновая подстраховка для сайтовых заказов теперь идёт через ОТДЕЛЬНУЮ,
        # правильную систему (get_abandoned_anonymous_purchases +
        # run_anonymous_payment_auto_check_scheduler) — она сканирует
        # anonymous_purchases напрямую по времени, без отдельной таблицы
        # очереди. Раньше здесь ОШИБОЧНО вызывался schedule_payment_auto_check,
        # рассчитанный на заказы БОТА (payment_auto_checks.order_id имеет
        # FOREIGN KEY на payments.order_id) — у сайтовых заказов нет строки в
        # payments вообще, поэтому эта вставка ломалась с "FOREIGN KEY
        # constraint failed" везде, где SQLite строго проверяет внешние ключи.

        qr_image_b64 = base64.b64encode(yk_result["qr_image_data"]).decode("ascii")
        qr_image_data_url = f"data:image/png;base64,{qr_image_b64}"

        return web.json_response({
            "order_id": order_id,
            "qr_image_url": qr_image_data_url,
            "qr_url": yk_result["qr_url"],
            "amount_rub": price_rub,
        })
    except Exception as e:
        logger.error(f"Public account key/renew/create error: {e}")
        return web.json_response({"error": "payment_creation_failed"}, status=502)


async def handle_public_account_key_renew_check(request: web.Request) -> web.Response:
    """POST /api/public/account/key/renew/check — проверка статуса
    продления конкретного ключа. Body JSON: {"order_id": "..."}"""
    account_id = _verify_session(request.cookies.get("site_session"))
    if not account_id:
        return web.json_response({"error": "unauthorized"}, status=401)

    try:
        data = await request.json()
    except json.JSONDecodeError:
        return web.json_response({"error": "invalid_json"}, status=400)

    order_id = (data.get("order_id") or "").strip()
    if not order_id:
        return web.json_response({"error": "order_id_required"}, status=400)

    from database.requests import get_anonymous_purchase_by_order_id, mark_anonymous_purchase_paid
    from bot.services.billing import check_yookassa_payment_status

    purchase = get_anonymous_purchase_by_order_id(order_id)
    if not purchase or not purchase.get("renewal_of_key_id"):
        return web.json_response({"error": "order_not_found"}, status=404)

    if purchase["status"] == "paid":
        return web.json_response({"status": "paid", "message": "Ключ уже продлён."})

    payment_id = purchase.get("yookassa_payment_id")
    if not payment_id:
        return web.json_response({"status": "pending", "message": "Платёж ещё создаётся, попробуйте через пару секунд."})

    try:
        yk_status = await check_yookassa_payment_status(payment_id)
    except Exception as e:
        logger.error(f"Public account key renew status check error: {e}")
        return web.json_response({"status": "pending", "message": "Не удалось проверить статус, попробуйте ещё раз."})

    if yk_status != "succeeded":
        status_map = {"pending": "pending", "waiting_for_capture": "pending", "canceled": "failed"}
        return web.json_response({"status": status_map.get(yk_status, "pending")})

    if not mark_anonymous_purchase_paid(order_id, payment_id):
        purchase = get_anonymous_purchase_by_order_id(order_id)
        if purchase and purchase["status"] == "paid":
            return web.json_response({"status": "paid", "message": "Ключ уже продлён."})
        return web.json_response({"status": "pending", "message": "Обрабатываем платёж, попробуйте через несколько секунд."})

    from bot.services.anonymous_purchase import renew_anonymous_vpn_key
    result = await renew_anonymous_vpn_key(purchase["renewal_of_key_id"], purchase["tariff_id"])
    return web.json_response({"status": "paid" if result.get("ok") else "failed", "message": result.get("message")})


async def handle_public_account_key_renew_balance(request: web.Request) -> web.Response:
    """POST /api/public/account/key/renew/balance — продление КОНКРЕТНОГО
    ключа личного кабинета оплатой с личного баланса, без QR/ЮKassa —
    завершается сразу, синхронно, в один запрос (в отличие от
    /create + /check для ЮKassa).
    Body JSON: {"key_id": int, "tariff_id": int | null}"""
    account_id = _verify_session(request.cookies.get("site_session"))
    if not account_id:
        return web.json_response({"error": "unauthorized"}, status=401)

    try:
        data = await request.json()
    except json.JSONDecodeError:
        return web.json_response({"error": "invalid_json"}, status=400)

    key_id = data.get("key_id")
    tariff_id = data.get("tariff_id")
    if not key_id:
        return web.json_response({"error": "key_id_required"}, status=400)

    from database.requests import get_site_account_by_id
    account = get_site_account_by_id(account_id)
    if not account or not _verify_key_belongs_to_account(int(key_id), account):
        return web.json_response({"error": "key_not_found"}, status=404)

    if not tariff_id:
        # Тот же автовыбор текущего тарифа, что и в .../key/renew/create.
        from database.requests import get_vpn_key_by_id
        current_key = get_vpn_key_by_id(int(key_id))
        if not current_key or not current_key.get("tariff_id"):
            return web.json_response({"error": "current_tariff_not_found"}, status=404)
        tariff_id = current_key["tariff_id"]

    from database.db_tariffs import get_tariff_by_id
    tariff = get_tariff_by_id(int(tariff_id))
    if not tariff:
        return web.json_response({"error": "tariff_not_found"}, status=404)

    price_cents = int(round(float(tariff.get("price_rub") or 0) * 100))
    if price_cents <= 0:
        return web.json_response({"error": "invalid_price"}, status=400)

    internal_user_id = _resolve_site_account_internal_user_id(account)
    if not internal_user_id:
        return web.json_response({"error": "account_not_linked"}, status=400)

    import time as _time
    from bot.services.balance import debit_user_balance, credit_user_balance

    order_reference = f"site-key-renew-{key_id}-{int(_time.time() * 1000)}"
    debit_result = await debit_user_balance(
        internal_user_id, price_cents,
        source="payment_balance", reason="Продление ключа с сайта (оплата балансом)",
        reference_type="site_key_renewal", reference_id=order_reference,
        metadata={"key_id": int(key_id), "tariff_id": int(tariff_id)},
    )
    if not debit_result.get("ok"):
        if debit_result.get("status") == "insufficient_funds":
            return web.json_response({
                "error": "insufficient_balance",
                "message": "Недостаточно средств на балансе для этого тарифа.",
                "balance_cents": debit_result.get("balance_before", 0),
                "required_cents": price_cents,
            }, status=400)
        return web.json_response({"error": "debit_failed", "message": "Не удалось списать баланс."}, status=502)

    from bot.services.anonymous_purchase import renew_anonymous_vpn_key
    try:
        result = await renew_anonymous_vpn_key(int(key_id), int(tariff_id), payment_type="balance")
    except Exception as e:
        result = {"ok": False, "message": str(e)}

    if not result.get("ok"):
        # Продление не удалось уже ПОСЛЕ списания — возвращаем деньги,
        # чтобы клиент не остался без ключа и без баланса одновременно.
        await credit_user_balance(
            internal_user_id, price_cents,
            source="refund", reason="Возврат за неудавшееся продление ключа с сайта (баланс)",
            reference_type="site_key_renewal", reference_id=order_reference,
            metadata={"key_id": int(key_id), "tariff_id": int(tariff_id)},
        )
        return web.json_response({"error": "renew_failed", "message": result.get("message")}, status=502)

    return web.json_response({"status": "paid", "message": result.get("message")})


def _serialize_device(d: dict) -> dict:
    """Готовит одну запись устройства для отдачи фронтенду сайта."""
    return {
        "device_id": d.get("device_id"),
        "ip": d.get("ip"),
        "os": d.get("os"),
        "os_version": d.get("os_version"),
        "model": d.get("model"),
        "first_seen": d.get("first_seen"),
        "last_seen": d.get("last_seen"),
    }


async def handle_public_account_key_devices(request: web.Request) -> web.Response:
    """POST /api/public/account/key/devices — список подключённых устройств
    ключа личного кабинета: с типом/моделью (режим HWID) или списком IP
    (режим IP, без типа устройства — панель их не хранит в этом режиме).
    Body JSON: {"key_id": int}"""
    account_id = _verify_session(request.cookies.get("site_session"))
    if not account_id:
        return web.json_response({"error": "unauthorized"}, status=401)

    try:
        data = await request.json()
    except json.JSONDecodeError:
        return web.json_response({"error": "invalid_json"}, status=400)

    key_id = data.get("key_id")
    if not key_id:
        return web.json_response({"error": "key_id_required"}, status=400)

    from database.requests import get_site_account_by_id, get_key_details_by_id
    account = get_site_account_by_id(account_id)
    if not account or not _verify_key_belongs_to_account(int(key_id), account):
        return web.json_response({"error": "key_not_found"}, status=404)

    key = get_key_details_by_id(int(key_id))
    if not key or not key.get("panel_email"):
        return web.json_response({"error": "key_not_configured"}, status=400)

    from bot.services.device_management import get_devices_for_key, DeviceManagementError
    try:
        result = await get_devices_for_key(key)
    except DeviceManagementError as e:
        return web.json_response({"error": "panel_error", "message": str(e)}, status=502)
    except Exception as e:
        logger.warning(f"Public account key/devices error для ключа {key_id}: {e}")
        return web.json_response({"error": "internal_error"}, status=502)

    return web.json_response({
        "limit_type": result["limit_type"],
        "max_devices": result.get("max_devices"),
        "current_count": result["current_count"],
        "devices": [_serialize_device(d) for d in result.get("devices") or []],
    })


async def handle_public_account_key_device_disconnect(request: web.Request) -> web.Response:
    """POST /api/public/account/key/device_disconnect — отключает ОДНО
    устройство ключа личного кабинета (доступно только в режиме HWID).
    Body JSON: {"key_id": int, "device_id": int}"""
    account_id = _verify_session(request.cookies.get("site_session"))
    if not account_id:
        return web.json_response({"error": "unauthorized"}, status=401)

    try:
        data = await request.json()
    except json.JSONDecodeError:
        return web.json_response({"error": "invalid_json"}, status=400)

    key_id = data.get("key_id")
    device_id = data.get("device_id")
    if not key_id or device_id is None:
        return web.json_response({"error": "key_id_and_device_id_required"}, status=400)

    from database.requests import get_site_account_by_id, get_key_details_by_id
    account = get_site_account_by_id(account_id)
    if not account or not _verify_key_belongs_to_account(int(key_id), account):
        return web.json_response({"error": "key_not_found"}, status=404)

    key = get_key_details_by_id(int(key_id))
    if not key or not key.get("panel_email"):
        return web.json_response({"error": "key_not_configured"}, status=400)

    from bot.services.device_management import disconnect_device as _disconnect_device
    try:
        result = await _disconnect_device(key, int(device_id))
    except Exception as e:
        logger.warning(f"Public account key/device_disconnect error для ключа {key_id}: {e}")
        return web.json_response({"error": "internal_error"}, status=502)

    if not result.get("ok"):
        return web.json_response({"error": "disconnect_failed", "message": result.get("message")}, status=502)

    return web.json_response({"status": "ok", "message": result.get("message")})


async def handle_public_account_buy_balance(request: web.Request) -> web.Response:
    """POST /api/public/account/buy/balance — покупка НОВОГО ключа для
    залогиненного личного кабинета сайта, оплата с личного баланса, без
    QR/ЮKassa — завершается сразу, синхронно, в один запрос.
    Body JSON: {"tariff_id": int}"""
    account_id = _verify_session(request.cookies.get("site_session"))
    if not account_id:
        return web.json_response({"error": "unauthorized"}, status=401)

    try:
        data = await request.json()
    except json.JSONDecodeError:
        return web.json_response({"error": "invalid_json"}, status=400)

    tariff_id = data.get("tariff_id")
    if not tariff_id:
        return web.json_response({"error": "tariff_id_required"}, status=400)

    from bot.services.anonymous_purchase import complete_anonymous_purchase_via_balance
    try:
        result = await complete_anonymous_purchase_via_balance(account_id, int(tariff_id))
    except Exception as e:
        logger.error(f"Public account buy/balance error: {e}")
        return web.json_response({"error": "payment_creation_failed"}, status=502)

    if not result.get("ok"):
        if result.get("insufficient"):
            return web.json_response({
                "error": "insufficient_balance",
                "message": result.get("message"),
                "balance_cents": result.get("balance_cents", 0),
                "required_cents": result.get("required_cents", 0),
            }, status=400)
        return web.json_response({"error": "purchase_failed", "message": result.get("message")}, status=502)

    return web.json_response({
        "status": "paid",
        "claim_code": result.get("claim_code"),
        "sub_url": result.get("sub_url"),
    })


async def handle_public_account_lookup(request: web.Request) -> web.Response:
    """POST /api/public/account/lookup — вход в личный кабинет по коду,
    без Telegram. Body JSON: {"code": "XXXX-XXXX"}"""
    try:
        data = await request.json()
    except json.JSONDecodeError:
        return web.json_response({"error": "invalid_json"}, status=400)

    code = (data.get("code") or "").strip()
    if not code:
        return web.json_response({"ok": False, "message": "Введите код."}, status=400)

    from bot.services.anonymous_purchase import get_account_info_by_claim_code
    result = await get_account_info_by_claim_code(code)
    return web.json_response(result)


async def handle_public_account_renew_create(request: web.Request) -> web.Response:
    """POST /api/public/account/renew/create — создаёт платёж на продление
    существующего ключа личного кабинета.
    Body JSON: {"code": "XXXX-XXXX", "tariff_id": int}"""
    try:
        data = await request.json()
    except json.JSONDecodeError:
        return web.json_response({"error": "invalid_json"}, status=400)

    code = (data.get("code") or "").strip()
    tariff_id = data.get("tariff_id")
    if not code or not tariff_id:
        return web.json_response({"error": "code_and_tariff_id_required"}, status=400)

    from bot.services.anonymous_purchase import get_account_info_by_claim_code
    account = await get_account_info_by_claim_code(code)
    if not account.get("ok"):
        return web.json_response({"error": "invalid_code", "message": account.get("message")}, status=404)

    from database.db_tariffs import get_tariff_by_id
    from database.db_payments import create_anonymous_purchase, save_anonymous_purchase_payment_id

    tariff = get_tariff_by_id(int(tariff_id))
    if not tariff:
        return web.json_response({"error": "tariff_not_found"}, status=404)

    price_rub = float(tariff.get("price_rub") or 0)
    if price_rub <= 0:
        return web.json_response({"error": "invalid_price"}, status=400)

    order_id = _generate_public_order_id()

    try:
        create_anonymous_purchase(order_id, tariff["id"], renewal_of_key_id=account["key_id"])

        from aiogram import Bot
        from config import BOT_TOKEN
        from bot.services.billing import create_yookassa_qr_payment

        pay_bot = Bot(token=BOT_TOKEN)
        try:
            bot_info = await pay_bot.get_me()
            description = _pub(tariff)
            yk_result = await create_yookassa_qr_payment(
                amount_rub=price_rub, order_id=order_id, description=description,
                bot_name=bot_info.username,
            )
        finally:
            await pay_bot.session.close()

        save_anonymous_purchase_payment_id(order_id, yk_result["yookassa_payment_id"])
        # Фоновая подстраховка для сайтовых заказов теперь идёт через ОТДЕЛЬНУЮ,
        # правильную систему (get_abandoned_anonymous_purchases +
        # run_anonymous_payment_auto_check_scheduler) — она сканирует
        # anonymous_purchases напрямую по времени, без отдельной таблицы
        # очереди. Раньше здесь ОШИБОЧНО вызывался schedule_payment_auto_check,
        # рассчитанный на заказы БОТА (payment_auto_checks.order_id имеет
        # FOREIGN KEY на payments.order_id) — у сайтовых заказов нет строки в
        # payments вообще, поэтому эта вставка ломалась с "FOREIGN KEY
        # constraint failed" везде, где SQLite строго проверяет внешние ключи.

        qr_image_b64 = base64.b64encode(yk_result["qr_image_data"]).decode("ascii")
        qr_image_data_url = f"data:image/png;base64,{qr_image_b64}"

        return web.json_response({
            "order_id": order_id,
            "qr_image_url": qr_image_data_url,
            "qr_url": yk_result["qr_url"],
            "amount_rub": price_rub,
        })
    except Exception as e:
        logger.error(f"Public account renew/create error: {e}")
        return web.json_response({"error": "payment_creation_failed"}, status=502)


async def handle_public_account_renew_check(request: web.Request) -> web.Response:
    """POST /api/public/account/renew/check — проверяет статус платежа за
    продление и, если оплачен, реально продлевает ключ.
    Body JSON: {"order_id": "..."}"""
    try:
        data = await request.json()
    except json.JSONDecodeError:
        return web.json_response({"error": "invalid_json"}, status=400)

    order_id = (data.get("order_id") or "").strip()
    if not order_id:
        return web.json_response({"error": "order_id_required"}, status=400)

    from database.db_payments import get_anonymous_purchase_by_order_id, mark_anonymous_purchase_paid
    from bot.services.billing import check_yookassa_payment_status

    purchase = get_anonymous_purchase_by_order_id(order_id)
    if not purchase or not purchase.get("renewal_of_key_id"):
        return web.json_response({"error": "order_not_found"}, status=404)

    if purchase["status"] in ("paid", "claimed"):
        return web.json_response({"status": "paid", "message": "Ключ уже продлён."})

    payment_id = purchase.get("yookassa_payment_id")
    if not payment_id:
        return web.json_response({"status": "pending", "message": "Платёж ещё создаётся, попробуйте через пару секунд."})

    try:
        yk_status = await check_yookassa_payment_status(payment_id)
    except Exception as e:
        logger.error(f"Public renew check error: {e}")
        return web.json_response({"status": "pending", "message": "Не удалось проверить статус, попробуйте ещё раз."})

    if yk_status != "succeeded":
        status_map = {"pending": "pending", "waiting_for_capture": "pending", "canceled": "failed"}
        return web.json_response({"status": status_map.get(yk_status, "pending")})

    if not mark_anonymous_purchase_paid(order_id, payment_id):
        purchase = get_anonymous_purchase_by_order_id(order_id)
        if purchase and purchase["status"] in ("paid", "claimed"):
            return web.json_response({"status": "paid", "message": "Ключ уже продлён."})
        return web.json_response({"status": "pending", "message": "Обрабатываем платёж, попробуйте через несколько секунд."})

    from bot.services.anonymous_purchase import renew_anonymous_vpn_key
    result = await renew_anonymous_vpn_key(purchase["renewal_of_key_id"], purchase["tariff_id"])
    return web.json_response({"status": "paid" if result["ok"] else "failed", "message": result["message"]})



async def handle_rename(request: web.Request) -> web.Response:
    """POST /api/rename — переименование ключа.

    Body JSON: {"key_id": 123, "name": "Новое имя"}
    """
    telegram_id = _get_telegram_id(request)
    if not telegram_id:
        return web.json_response({"error": "unauthorized"}, status=401)

    try:
        data = await request.json()
        key_id = int(data.get("key_id", 0))
        new_name = (data.get("new_name") or data.get("name") or "").strip()

        if not key_id:
            return web.json_response({"error": "invalid_key_id"}, status=400)

        if len(new_name) > 30:
            return web.json_response({"error": "name_too_long"}, status=400)

        from database.db_keys import update_key_custom_name

        success = update_key_custom_name(key_id, telegram_id, new_name)
        if success:
            return web.json_response({"success": True})
        else:
            return web.json_response({"error": "not_found_or_forbidden"}, status=404)

    except json.JSONDecodeError:
        return web.json_response({"error": "invalid_json"}, status=400)
    except Exception as e:
        logger.error(f"WebApp /api/rename error: {e}", exc_info=True)
        return web.json_response({"error": "internal_error"}, status=500)


async def handle_delete(request: web.Request) -> web.Response:
    """POST /api/delete — удаление истекшего ключа.

    Body JSON: {"key_id": 123}
    Удаляет только если ключ принадлежит пользователю и не активен.
    """
    telegram_id = _get_telegram_id(request)
    if not telegram_id:
        return web.json_response({"error": "unauthorized"}, status=401)

    try:
        data = await request.json()
        key_id = int(data.get("key_id", 0))

        if not key_id:
            return web.json_response({"error": "invalid_key_id"}, status=400)

        from database.db_keys import get_key_details_for_user, delete_vpn_key
        from bot.services.vpn_api import get_client

        key = get_key_details_for_user(key_id, telegram_id)
        if not key:
            return web.json_response({"error": "not_found_or_forbidden"}, status=404)

        # Запрещаем удалять активные ключи
        if key.get("is_active"):
            return web.json_response({"error": "key_still_active"}, status=400)

        # Удаляем клиента с панели 3X-UI
        if key.get("server_id") and key.get("panel_email"):
            try:
                client = await get_client(key["server_id"])
                if key.get("sub_id"):
                    deleted = await client.delete_clients_by_email_on_server(key["panel_email"])
                    logger.info(f"Subscription-ключ {key_id}: удалено {deleted} клиентов с панели")
                elif key.get("panel_inbound_id") and key.get("client_uuid"):
                    await client.delete_client(key["panel_inbound_id"], key["client_uuid"])
                    logger.info(f"Клиент {key.get('panel_email')} удалён с панели")
            except Exception as e:
                logger.warning(f"Не удалось удалить клиента с панели: {e}")

        success = delete_vpn_key(key_id)
        if success:
            return web.json_response({"success": True})
        else:
            return web.json_response({"error": "db_error"}, status=500)

    except json.JSONDecodeError:
        return web.json_response({"error": "invalid_json"}, status=400)
    except Exception as e:
        logger.error(f"WebApp /api/delete error: {e}", exc_info=True)
        return web.json_response({"error": "internal_error"}, status=500)


async def handle_referral(request: web.Request) -> web.Response:
    """GET /api/referral — реферальная программа и личный баланс.

    Returns: {referral_link, balance_cents, balance_human, referrals_count}
    """
    telegram_id = _get_telegram_id(request)
    if not telegram_id:
        return web.json_response({"error": "unauthorized"}, status=401)

    try:
        from database.db_users import (
            get_user_by_telegram_id,
            get_user_balance,
            ensure_user_referral_code,
        )
        from database.connection import get_connection

        user = get_user_by_telegram_id(telegram_id)
        if not user:
            return web.json_response({"error": "user_not_found"}, status=404)

        user_internal_id = user["id"]
        balance_cents = get_user_balance(user_internal_id)

        referral_code = ensure_user_referral_code(user_internal_id)

        # Get bot username from the running bot instance (set at main.py startup)
        from bot.utils.runtime_state import get_bot_username
        bot_username = get_bot_username() or None

        # НЕ подставляем чужой bot_username как fallback — если lookup не
        # удался, лучше пустая ссылка (клиент попробует обновить страницу),
        # чем реферальная ссылка, ведущая на другого, чужого бота.
        referral_link = f"https://t.me/{bot_username}?start=ref_{referral_code}" if bot_username else ""

        # Count referrals
        referrals_count = 0
        conn = get_connection()
        try:
            row = conn.execute(
                "SELECT COUNT(*) as cnt FROM users WHERE referred_by = ?",
                (user_internal_id,)
            ).fetchone()
            if row:
                referrals_count = row["cnt"]
        except Exception:
            # Fallback: try referred_by_code field
            try:
                conn2 = get_connection.__wrapped__ if hasattr(get_connection, '__wrapped__') else None
                row = conn.execute(
                    "SELECT COUNT(*) as cnt FROM users WHERE referred_by_code = ?",
                    (referral_code,)
                ).fetchone()
                if row:
                    referrals_count = row["cnt"]
            except Exception:
                pass
        finally:
            conn.close()

        # Format balance
        rub = balance_cents // 100
        kop = balance_cents % 100
        if kop == 0:
            balance_human = f"{rub} ₽"
        else:
            balance_human = f"{rub}.{kop:02d} ₽"

        return web.json_response({
            "referral_link": referral_link,
            "referral_code": referral_code,
            "balance_cents": balance_cents,
            "balance_human": balance_human,
            "referrals_count": referrals_count,
        })

    except Exception as e:
        logger.error(f"WebApp /api/referral error: {e}", exc_info=True)
        return web.json_response({"error": "internal_error"}, status=500)


async def handle_weblink(request: web.Request) -> web.Response:
    """GET /api/weblink — генерирует подписанную ссылку для браузера (TTL 1 час)."""
    telegram_id = _get_telegram_id(request)
    if not telegram_id:
        return web.json_response({"error": "unauthorized"}, status=401)
    from config import BOT_TOKEN
    from bot.utils.webtoken import make_token
    token = make_token(telegram_id, BOT_TOKEN, ttl_seconds=3600)
    url = f"https://support.pchelp-24.com/support?token={token}"
    return web.json_response({"url": url})

async def handle_favicon(request: web.Request) -> web.Response:
    """GET /favicon.ico — браузеры запрашивают этот путь напрямую,
    независимо от тегов <link rel=\"icon\"> в HTML."""
    favicon_path = os.path.join(_STATIC_DIR, "favicon.ico")
    if os.path.exists(favicon_path):
        return web.FileResponse(favicon_path)
    return web.Response(status=404)


async def handle_index(request: web.Request) -> web.Response:
    """GET / — раздаёт index.html."""
    index_path = os.path.join(_TEMPLATES_DIR, "index.html")
    return _serve_html_with_brand(
        index_path,
        title_format="💎{brand}💎",
        header_format="{brand}",
    )
def _extract_sub_id_from_sub_url(sub_url: str) -> Optional[str]:
    """Достаёт sub_id из ссылки вида '{webapp_url}/happ-sub/{sub_id}'."""
    if not sub_url or "/happ-sub/" not in sub_url:
        return None
    tail = sub_url.rsplit("/happ-sub/", 1)[1]
    # На случай query-параметров после sub_id (сейчас их не бывает, но на
    # будущее — не должны попасть в install_code lookup).
    return tail.split("?", 1)[0].split("#", 1)[0].strip() or None


def _import_error_page(title: str, message: str) -> web.Response:
    """Небольшая страница-заглушка в том же стиле, что и import.html —
    показывается вместо голого 404/503, когда ссылка на импорт оказалась
    нештатной (устарела, была подделана или тариф не позволяет импорт)."""
    page_html = f"""<!DOCTYPE html>
<html lang="ru"><head><meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>{_html_module.escape(title)}</title>
<style>
body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
       background: #0f172a; color: #e2e8f0; display: flex; align-items: center;
       justify-content: center; min-height: 100vh; margin: 0; padding: 20px; }}
.card {{ background: #1e293b; border-radius: 16px; padding: 32px 24px; max-width: 380px;
        width: 100%; text-align: center; box-shadow: 0 4px 24px rgba(0,0,0,0.3); }}
.icon {{ font-size: 48px; margin-bottom: 12px; }}
h1 {{ font-size: 20px; margin: 0 0 12px; font-weight: 700; }}
p {{ font-size: 14px; color: #94a3b8; line-height: 1.6; margin: 6px 0; }}
</style></head><body>
<div class="card"><div class="icon">⚠️</div>
<h1>{_html_module.escape(title)}</h1>
<p>{_html_module.escape(message)}</p>
</div></body></html>"""
    return web.Response(text=page_html, content_type="text/html", status=400)


async def handle_import(request: web.Request) -> web.Response:
    """GET /import — раздаёт страницу-редирект для импорта подписки в
    Happ/INCY/Karing/ECLIPSE VPN.

    ВАЖНО: `url` из query-параметров — это ссылка, которую приложение
    клиента добавит себе как VPN-подписку, поэтому она принимается
    ТОЛЬКО если это настоящая ссылка-подписка этого же сайта (вида
    "{webapp_url}/happ-sub/{sub_id}", см. get_public_subscription_url_for_key
    в vpn_api.py — все ссылки на импорт, которые бот/сайт показывают
    пользователю, всегда именно такие). Так исключается подстановка
    произвольного чужого адреса в диплинк (иначе ссылку с ДОВЕРЕННОГО
    домена можно было бы использовать, чтобы подсунуть пользователю чужой
    VPN-сервер). sub_id дополнительно сверяется с БД — так гарантированно
    импортируется именно та подписка, на которую указывает ссылка, а не
    произвольный текст.

    Для scheme=happ, если в админке настроены provider_code/auth_key
    happ-proxy.com (API лимитированных ссылок), дополнительно:
      1) при необходимости регистрирует домен сайта в happ-proxy.com
         (ensure_domain_registered — не чаще одного раза на смену домена);
      2) получает/создаёт install_code для sub_id этого ключа (кэшируется
         в БД — один install_code на подписку, не на каждый клик);
      3) передаёт install_code странице через встроенный <script>, чтобы
         клиентский JS добавил параметр InstallID к ссылке happ://add/...
         (см. happ.su/main/dev-docs/limited-links).
    Если API не настроено или запрос не удался — страница отдаётся как
    раньше, без InstallID (импорт всё равно работает, просто без лимита
    установок)."""
    from bot.services.license import is_feature_available
    if not is_feature_available("app_import"):
        return _import_error_page(
            "Функция недоступна",
            "Импорт подписки в приложение сейчас недоступен на этом тарифе.",
        )

    import_path = os.path.join(_TEMPLATES_DIR, "import.html")
    if not os.path.exists(import_path):
        return web.Response(text="<h1>Import template not found</h1>", status=404)

    scheme = request.query.get("scheme", "").strip().lower()

    # Помимо общей лицензионной фичи выше, админ конкретной установки может
    # независимо скрыть кнопку под каждое отдельное приложение (см.
    # database/db_settings.py, is_cabinet_import_app_enabled) — если так,
    # прямой запрос к /import с этим scheme тоже должен быть отклонён, а
    # не только скрыта сама кнопка на фронтенде.
    from database.requests import is_cabinet_import_app_enabled, CABINET_IMPORT_APPS
    if scheme in CABINET_IMPORT_APPS and not is_cabinet_import_app_enabled(scheme):
        return _import_error_page(
            "Функция недоступна",
            "Импорт подписки в это приложение сейчас отключён администратором.",
        )

    raw_url = request.query.get("url", "").strip()
    if not raw_url:
        return _import_error_page(
            "Ссылка не передана",
            "Откройте карточку ключа в боте или в личном кабинете и запустите импорт ещё раз.",
        )

    from database.requests import get_effective_webapp_url, get_vpn_key_by_sub_id
    webapp_url = (get_effective_webapp_url() or "").rstrip("/")
    expected_prefix = f"{webapp_url}/happ-sub/" if webapp_url else None

    sub_id = None
    if expected_prefix and raw_url.startswith(expected_prefix):
        sub_id = _extract_sub_id_from_sub_url(raw_url)

    if not sub_id:
        logger.warning(
            "handle_import: url не является собственной ссылкой-подпиской "
            "этого сайта (scheme=%s) — запрос отклонён", scheme,
        )
        return _import_error_page(
            "Ссылка недействительна",
            "Эта ссылка на импорт не похожа на настоящую ссылку-подписку этого "
            "сервиса — возможно, она устарела или повреждена. Откройте карточку "
            "ключа в боте или в личном кабинете и запустите импорт оттуда ещё раз.",
        )

    key = get_vpn_key_by_sub_id(sub_id)
    if not key:
        return _import_error_page(
            "Ключ не найден",
            "Похоже, эта подписка была удалена или ссылка устарела. Откройте "
            "«🔑 Мои ключи» в боте, чтобы получить актуальную ссылку на импорт.",
        )

    sub_url = raw_url

    install_code = ""
    if scheme == "happ":
        try:
            from bot.services.happ_proxy import is_configured, ensure_domain_registered, get_or_create_install_code_for_sub
            if is_configured():
                if webapp_url and await ensure_domain_registered(webapp_url):
                    install_code = await get_or_create_install_code_for_sub(sub_id) or ""
        except Exception as e:
            # Никогда не должны ломать сам импорт из-за проблем с
            # happ-proxy.com — в худшем случае просто без InstallID.
            logger.warning(f"handle_import: не удалось подготовить InstallID для happ-proxy.com: {e}")

    html = await asyncio.to_thread(lambda: open(import_path, encoding="utf-8").read())
    if install_code:
        injection = f'<script>window.__ECLIPSE_HAPP_INSTALLID__={json.dumps(install_code)};</script>'
        html = html.replace("<script>", injection + "\n<script>", 1)
    return web.Response(text=html, content_type="text/html")


async def handle_app_page(request: web.Request) -> web.Response:
    """GET /app — страница скачивания Android-приложения.

    Сама подтягивает последний релиз из GitHub и предлагает подходящий APK,
    чтобы клиенту не нужно было разбираться в архитектурах.

    Эта механика (автопроверка релизов конкретно из GitHub-репозитория
    Android-приложения) целиком специфична для инсталляций, у которых
    есть СВОЁ Android-приложение (own_app_url настроен) — у white-label
    клиентов без своего приложения (own_app_url пуст, дефолт для новых
    установок) страница отдаёт 404, а не показывает чужой репозиторий
    чужого приложения."""
    from database.requests import get_effective_own_app_url
    if not get_effective_own_app_url():
        return web.Response(text="404: Not Found", status=404)

    app_path = os.path.join(_TEMPLATES_DIR, "app.html")
    if os.path.exists(app_path):
        return web.FileResponse(app_path)
    return web.Response(
        text="<h1>App page not found</h1>", status=404
    )


# ============================================================
# CORS: разрешаем запросы с собственного домена WebApp
# ============================================================

def _build_cors_allowed() -> tuple:
    from database.requests import get_effective_webapp_url
    allowed = [get_effective_webapp_url().rstrip("/")]
    # Дополнительные разрешённые источники можно перечислить через
    # переменную окружения CORS_EXTRA_ORIGINS (через запятую), например
    # для отдельного статического сайта поддержки на другом домене.
    extra = os.environ.get("CORS_EXTRA_ORIGINS", "")
    if extra:
        allowed.extend(o.strip().rstrip("/") for o in extra.split(",") if o.strip())
    return tuple(allowed)


@web.middleware
async def cors_middleware(request: web.Request, handler):
    origin = request.headers.get("Origin", "")
    allowed = origin if origin in _build_cors_allowed() else ""

    if request.method == "OPTIONS":
        resp = web.Response(status=204)
    else:
        resp = await handler(request)

    if allowed:
        resp.headers["Access-Control-Allow-Origin"] = allowed
        resp.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
        resp.headers["Access-Control-Allow-Headers"] = "Content-Type, X-Init-Data"
        resp.headers["Vary"] = "Origin"
    return resp


# ============================================================
# App factory
# ============================================================

_LICENSE_GATE_ALWAYS_ALLOWED_PREFIXES = (
    "/happ-sub/",
    "/import",
    "/api/status",
    "/api/ping",
    "/api/license/",
    "/static/",
    "/favicon.ico",
    # v1.165: Telegram Mini App и оплата из него — базовые функции, они работают
    # всегда (сайт и личный кабинет закрываются лицензией «site_webapp»)
    "/api/keys",
    "/api/key/",
    "/api/pay/",
    "/api/tariffs",
    "/api/language",
    "/api/rename",
    "/api/delete",
    "/api/referral",
    "/api/weblink",
    "/api/ai-consult",
    "/api/ai-feedback",
)


@web.middleware
async def license_gate_middleware(request: web.Request, handler):
    """Блокирует доступ к сайту/личному кабинету (шоп, оплата, аккаунт),
    если у whitelabel-партнёра нет активной лицензии полного тарифа —
    см. bot/services/license.py. Доставка подписки для уже оплативших
    клиентов (см. _LICENSE_GATE_ALWAYS_ALLOWED_PREFIXES) НИКОГДА не
    блокируется вне зависимости от статуса лицензии."""
    path = request.path
    if path == "/" or any(path.startswith(prefix) for prefix in _LICENSE_GATE_ALWAYS_ALLOWED_PREFIXES):
        return await handler(request)

    from bot.services.license import is_feature_available
    if not is_feature_available("site_webapp"):
        if path.startswith("/api/"):
            return web.json_response(
                {"error": "feature_unavailable", "message": "Сайт временно недоступен."},
                status=503,
            )
        return web.Response(
            text="<html><body style='font-family:sans-serif;text-align:center;padding:60px;'>"
                 "<h2>Сайт временно недоступен</h2></body></html>",
            content_type="text/html",
            status=503,
        )

    return await handler(request)


def create_web_app() -> web.Application:
    """Создаёт aiohttp приложение с маршрутами WebApp."""
    app = web.Application(middlewares=[cors_middleware, license_gate_middleware])
    app.router.add_get("/api/weblink", handle_weblink)
    app.router.add_static("/static/", path=_STATIC_DIR, name="static")
    app.router.add_get("/favicon.ico", handle_favicon)
    app.router.add_get("/", handle_index)
    app.router.add_get("/import", handle_import)
    app.router.add_get("/app", handle_app_page)
    app.router.add_get("/api/keys", handle_keys)
    app.router.add_get("/api/key/{key_id}/inbounds", handle_key_inbounds)
    app.router.add_get("/api/public/key/{key_id}/inbounds", handle_public_key_inbounds)
    app.router.add_get("/api/status", handle_status)
    app.router.add_get("/api/ping", handle_ping)
    app.router.add_get("/api/language", handle_language)
    app.router.add_post("/api/ai-consult", handle_ai_consult)
    app.router.add_post("/api/ai-feedback", handle_ai_feedback)
    app.router.add_get("/api/tariffs", handle_tariffs_list)
    app.router.add_post("/api/pay/create", handle_pay_create)
    app.router.add_post("/api/pay/check", handle_pay_check)
    app.router.add_get("/shop", handle_shop_page)
    app.router.add_get("/welcome", handle_welcome_page)
    app.router.add_get("/api/public/site-info", handle_public_site_info)
    app.router.add_get("/api/public/connection-status", handle_public_connection_status)
    app.router.add_get("/happ-sub/{sub_id}", handle_happ_subscription)
    app.router.add_get("/api/public/landing-tariffs", handle_landing_tariffs)
    app.router.add_get("/api/public/tariffs", handle_public_tariffs)
    app.router.add_post("/api/public/pay/create", handle_public_pay_create)
    app.router.add_post("/api/public/trial/create", handle_public_trial_create)
    app.router.add_post("/api/public/pay/check", handle_public_pay_check)
    app.router.add_post("/api/public/account/lookup", handle_public_account_lookup)
    app.router.add_post("/api/public/account/renew/create", handle_public_account_renew_create)
    app.router.add_post("/api/public/account/renew/check", handle_public_account_renew_check)
    app.router.add_get("/api/public/oauth/providers", handle_oauth_providers)
    app.router.add_get("/auth/{provider}/start", handle_oauth_start)
    app.router.add_get("/auth/{provider}/callback", handle_oauth_callback)
    app.router.add_get("/api/public/zvonok/postback", handle_zvonok_postback)
    app.router.add_post("/api/public/auth/phone/request", handle_public_auth_phone_request)
    app.router.add_post("/api/public/auth/phone/check", handle_public_auth_phone_check)
    app.router.add_post("/api/public/account/link-phone-check", handle_public_account_link_phone_check)
    app.router.add_post("/api/public/account/session-login", handle_public_account_session_login)
    app.router.add_post("/api/public/account/oauth-exchange", handle_public_account_oauth_exchange)
    app.router.add_post("/api/license/check", handle_license_check)
    app.router.add_post("/api/license/trial", handle_license_trial)
    app.router.add_post("/api/public/account/claim-purchase", handle_public_account_claim_purchase)
    app.router.add_post("/api/public/account/link-code", handle_public_account_link_code)
    app.router.add_get("/api/public/account/referral", handle_public_account_referral)
    app.router.add_get("/api/public/account/session", handle_public_account_session)
    app.router.add_post("/api/public/account/logout", handle_public_account_logout)
    app.router.add_post("/api/public/account/key/renew/create", handle_public_account_key_renew_create)
    app.router.add_post("/api/public/account/key/renew/check", handle_public_account_key_renew_check)
    app.router.add_post("/api/public/account/key/renew/balance", handle_public_account_key_renew_balance)
    app.router.add_post("/api/public/account/key/devices", handle_public_account_key_devices)
    app.router.add_post("/api/public/account/key/device_disconnect", handle_public_account_key_device_disconnect)
    app.router.add_post("/api/public/account/buy/balance", handle_public_account_buy_balance)
    app.router.add_post("/api/rename", handle_rename)
    app.router.add_post("/api/delete", handle_delete)
    app.router.add_get("/api/referral", handle_referral)
    return app


async def run_webapp(host: str = "127.0.0.1", port: int = 3000) -> None:
    """Запускает aiohttp WebApp."""
    app = create_web_app()
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, host=host, port=port)
    await site.start()
    logger.info(f"🌐 WebApp started on http://{host}:{port}")
    try:
        await asyncio.Event().wait()
    finally:
        await runner.cleanup()
