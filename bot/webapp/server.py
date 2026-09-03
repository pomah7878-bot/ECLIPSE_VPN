"""
WebApp сервер для VPN-бота.

aiohttp веб-сервер, который:
- Раздаёт index.html (frontend WebApp)
- Предоставляет REST API (/api/keys, /api/status, /api/ping, /api/rename, /api/delete, /api/referral)
- Аутентифицирует запросы через проверку Telegram initData HMAC-SHA256

Запускается параллельно с aiogram polling через asyncio.
"""
import json
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


# ============================================================
# Аутентификация: проверка Telegram initData через aiogram
# ============================================================

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
        # parse_webapp_init_data возвращает объект WebAppInitData, не словарь
        user = data.user
        if user:
            return int(user.id)
        return None

    except Exception as e:
        logger.error(f"WebApp: initData validation error: {e}")
        return None


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
        bot_username = None
        try:
            from main import bot as _bot
            if hasattr(_bot, 'my_username') and _bot.my_username:
                bot_username = _bot.my_username
        except Exception:
            pass

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
    """POST /api/pay/create — создаёт заказ и QR-платёж YooKassa для покупки
    нового ключа или продления существующего.
    Body JSON: {"tariff_id": int, "vpn_key_id": int | null}
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
    if not tariff_id:
        return web.json_response({"error": "tariff_id_required"}, status=400)

    from database.requests import get_tariff_by_id, get_user_internal_id, create_pending_order, save_yookassa_payment_id
    from bot.services.promotions import prepare_order_pricing
    from bot.services.billing import create_yookassa_qr_payment

    tariff = get_tariff_by_id(int(tariff_id))
    if not tariff:
        return web.json_response({"error": "tariff_not_found"}, status=404)

    user_id = get_user_internal_id(telegram_id)
    if not user_id:
        return web.json_response({"error": "user_not_found"}, status=404)

    action = "renewal" if vpn_key_id else "new_key"
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
            description = (
                f"Продление тарифа «{tariff['name']}» ({tariff['duration_days']} дн.)"
                if vpn_key_id else
                f"Покупка «{tariff['name']}» — {tariff['duration_days']} дней"
            )
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


async def handle_shop_page(request: web.Request) -> web.Response:
    """GET /shop — публичная страница покупки, без Telegram."""
    shop_path = os.path.join(_TEMPLATES_DIR, "shop.html")
    if os.path.exists(shop_path):
        return web.FileResponse(shop_path)
    return web.Response(text="<h1>Shop template not found</h1>", status=404)


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
    без авторизации: название бренда и юзернейм бота. Используется
    публичными страницами (например /welcome, /), чтобы не хардкодить
    название сервиса в HTML — оно берётся из настроек текущей
    инсталляции, как и везде в остальном боте."""
    from database.requests import get_effective_brand_name, get_cabinet_theme_id

    bot_username = await _resolve_bot_username_for_webapp()

    resp = web.json_response({
        "brand_name": get_effective_brand_name(),
        "bot_username": bot_username,
        "cabinet_theme_id": get_cabinet_theme_id(),
    })
    resp.headers['Cache-Control'] = 'no-store'
    return resp


async def _resolve_bot_username_for_webapp() -> str:
    """Юзернейм бота — тот же паттерн, что и в handle_public_site_info,
    вынесен отдельно, чтобы использовать и в handle_happ_subscription."""
    try:
        from main import bot as _bot
        if hasattr(_bot, 'my_username') and _bot.my_username:
            return _bot.my_username
    except Exception:
        pass
    return ""


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

    raw_url = await get_subscription_url_for_key(key)
    if not raw_url:
        return web.Response(status=502, text="Subscription temporarily unavailable")

    import aiohttp as _aiohttp
    try:
        async with _aiohttp.ClientSession() as session:
            async with session.get(raw_url, timeout=_aiohttp.ClientTimeout(total=10)) as upstream:
                body = await upstream.read()
                upstream_content_type = upstream.headers.get("Content-Type")
                upstream_userinfo = upstream.headers.get("subscription-userinfo")
    except Exception as e:
        logger.warning(f"handle_happ_subscription: не удалось получить подписку у панели ({sub_id[:8]}...): {e}")
        return web.Response(status=502, text="Upstream subscription unavailable")

    from database.requests import get_effective_brand_name, get_effective_webapp_url
    headers = {"Content-Type": upstream_content_type or "text/plain; charset=utf-8"}

    brand_name = get_effective_brand_name()
    if brand_name:
        headers["profile-title"] = brand_name[:25]

    webapp_url = get_effective_webapp_url()
    if webapp_url:
        headers["profile-web-page-url"] = webapp_url

    bot_username = await _resolve_bot_username_for_webapp()
    if bot_username:
        headers["support-url"] = f"https://t.me/{bot_username}?start=support"

    if upstream_userinfo:
        headers["subscription-userinfo"] = upstream_userinfo
    else:
        expire_epoch = 0
        try:
            expires_at = key.get("expires_at")
            if expires_at:
                expire_epoch = int(datetime.fromisoformat(expires_at).timestamp())
        except Exception:
            expire_epoch = 0
        traffic_used = key.get("traffic_used") or 0
        traffic_limit = key.get("traffic_limit") or 0
        headers["subscription-userinfo"] = (
            f"upload=0; download={traffic_used}; total={traffic_limit}; expire={expire_epoch}"
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

    if is_expired and bot_username:
        # Уже истекла — жёсткий блок Happ: "Subscription has expired!" + Renew
        headers["sub-expire"] = "1"
        headers["sub-expire-button-link"] = f"https://t.me/{bot_username}?start=renew_{key['id']}"
    elif days_left is not None and 0 <= days_left <= 3 and bot_username:
        # Ещё активна, но истекает в ближайшие 3 дня — мягкое предупреждение
        # (sub-info-*), а не жёсткий блок: подписка ещё работает, это просто
        # заранее показанное напоминание продлить.
        word = "день" if days_left == 1 else ("дня" if 1 < days_left < 5 else "дней")
        headers["sub-info-text"] = f"⚠️ Подписка истекает через {days_left} {word}!"
        headers["sub-info-button-text"] = "Продлить"
        headers["sub-info-button-link"] = f"https://t.me/{bot_username}?start=renew_{key['id']}"

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
            description = f"Покупка «{tariff['name']}» — {tariff['duration_days']} дней (сайт)"
            yk_result = await create_yookassa_qr_payment(
                amount_rub=price_rub, order_id=order_id, description=description,
                bot_name=bot_info.username,
            )
        finally:
            await pay_bot.session.close()

        save_anonymous_purchase_payment_id(order_id, yk_result["yookassa_payment_id"])

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

    from database.db_payments import (
        get_anonymous_purchase_by_order_id, mark_anonymous_purchase_paid,
        save_anonymous_purchase_provisioning,
    )
    from bot.services.billing import check_yookassa_payment_status

    purchase = get_anonymous_purchase_by_order_id(order_id)
    if not purchase:
        return web.json_response({"error": "order_not_found"}, status=404)

    if purchase["status"] in ("paid", "claimed"):
        return web.json_response({
            "status": "paid",
            "claim_code": purchase["claim_code"],
            "sub_url": purchase.get("sub_url"),
        })

    payment_id = purchase.get("yookassa_payment_id")
    if not payment_id:
        return web.json_response({"status": "pending", "message": "Платёж ещё создаётся, попробуйте через пару секунд."})

    try:
        yk_status = await check_yookassa_payment_status(payment_id)
    except Exception as e:
        logger.error(f"Public YooKassa status check error: {e}")
        return web.json_response({"status": "pending", "message": "Не удалось проверить статус, попробуйте ещё раз."})

    if yk_status != "succeeded":
        status_map = {"pending": "pending", "waiting_for_capture": "pending", "canceled": "failed"}
        return web.json_response({"status": status_map.get(yk_status, "pending")})

    if not mark_anonymous_purchase_paid(order_id, payment_id):
        # Уже мог обработаться параллельным запросом (двойной опрос) — перечитываем
        purchase = get_anonymous_purchase_by_order_id(order_id)
        if purchase and purchase["status"] in ("paid", "claimed"):
            return web.json_response({
                "status": "paid", "claim_code": purchase["claim_code"], "sub_url": purchase.get("sub_url"),
            })
        return web.json_response({"status": "pending", "message": "Обрабатываем платёж, попробуйте через несколько секунд."})

    sub_url = None
    try:
        from bot.services.anonymous_purchase import provision_anonymous_vpn_key
        result = await provision_anonymous_vpn_key(purchase["tariff_id"], order_id)
        save_anonymous_purchase_provisioning(order_id, result["key_id"], result["sub_url"], result["placeholder_user_id"])
        sub_url = result["sub_url"]
    except Exception as e:
        logger.error(f"Public provisioning error for order {order_id}: {e}")
        # Оплата прошла успешно, но с выдачей ключа проблема — код привязки
        # у клиента всё равно есть, ключ можно довыдать вручную по order_id.

    purchase = get_anonymous_purchase_by_order_id(order_id)
    return web.json_response({
        "status": "paid",
        "claim_code": purchase["claim_code"],
        "sub_url": sub_url,
    })


# ============================================================================
# SITE SESSIONS & OAuth (Google / Яндекс / VK) — личный кабинет на сайте.
# Сессия — подписанная HMAC cookie (без сторонних библиотек), содержит
# site_account_id и время истечения (30 дней).
# ============================================================================

_SESSION_TTL_SECONDS = 30 * 24 * 3600


def _get_session_secret() -> bytes:
    from config import SUPPORT_API_TOKEN
    return SUPPORT_API_TOKEN.encode()


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
    """Реальный IP клиента — учитывает заголовок от прокси (nginx), если есть."""
    forwarded = request.headers.get("X-Forwarded-For", "")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.remote or "unknown"


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


async def handle_public_trial_create(request: web.Request) -> web.Response:
    """POST /api/public/trial/create — активирует бесплатный пробный период
    для текущего залогиненного аккаунта (по коду или OAuth). Без оплаты —
    сразу провижинит рабочий ключ, как и обычная покупка.

    Защищено от ботов: rate-limit по IP + обязательная проверка Cloudflare
    Turnstile (токен передаётся в теле запроса как turnstile_token)."""
    account_id = _verify_session(request.cookies.get("site_session"))
    if not account_id:
        return web.json_response({"error": "unauthorized"}, status=401)

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
    is_app_request = request.headers.get("X-Eclipse-App") == "1"
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
        result = await provision_anonymous_vpn_key(trial_tariff_id, order_id)
        save_anonymous_purchase_provisioning(order_id, result["key_id"], result["sub_url"], result["placeholder_user_id"])
        mark_anonymous_purchase_paid(order_id, "trial")  # без реального платежа, просто маркер завершения

        if account.get("telegram_id"):
            real_user_id = get_user_internal_id(account["telegram_id"])
            if real_user_id:
                mark_trial_used(real_user_id)

        purchase = get_anonymous_purchase_by_order_id(order_id)
        return web.json_response({
            "status": "paid",
            "claim_code": purchase["claim_code"],
            "sub_url": result["sub_url"],
        })
    except Exception as e:
        logger.error(f"Public trial creation error: {e}")
        return web.json_response({"error": "trial_creation_failed", "message": "Не удалось активировать пробный период. Попробуйте позже."}, status=502)


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
        )
        account_id = account["id"]

    is_app_client = request.cookies.get("oauth_client") == "app"

    if is_app_client:
        # Запрос из приложения — браузерная cookie бесполезна для OkHttp
        # клиента приложения. Возвращаем одноразовый короткоживущий код
        # обмена через deep-link в приложение вместо cookie.
        from database.requests import create_oauth_exchange_code
        exchange_code = create_oauth_exchange_code(account_id)
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

    from database.requests import consume_site_login_code
    from database.db_accounts import _get_or_create_telegram_site_account

    telegram_id = consume_site_login_code(code)
    if telegram_id:
        account = _get_or_create_telegram_site_account(telegram_id)
        session_value = _sign_session(account["id"])
        resp = web.json_response({"ok": True, "account_type": "telegram"})
        resp.set_cookie("site_session", session_value, max_age=_SESSION_TTL_SECONDS, httponly=True, secure=True, samesite="Lax")
        return resp

    # Не код из бота — пробуем как claim_code анонимной покупки
    from database.requests import get_anonymous_purchase_by_claim_code, link_purchase_to_account

    purchase = get_anonymous_purchase_by_claim_code(code)
    if not purchase or not purchase.get("vpn_key_id"):
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

    account_id = consume_oauth_exchange_code(code)
    if not account_id:
        return web.json_response({"ok": False, "message": "Код недействителен или истёк."}, status=400)

    session_value = _sign_session(account_id)
    resp = web.json_response({"ok": True})
    resp.set_cookie("site_session", session_value, max_age=_SESSION_TTL_SECONDS, httponly=True, secure=True, samesite="Lax")
    return resp


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

    return web.json_response({"ok": True})


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
        "keys": [{
            "key_id": key["id"],
            "display_name": key.get("tariff_name") or f"Ключ #{key['id']}",
            "expires_at": key.get("expires_at"),
            "traffic_used": key.get("traffic_used") or 0,
            "traffic_limit": key.get("traffic_limit") or 0,
            "is_active": True,
            "sub_url": purchase.get("sub_url"),
        }],
    })


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
            description = f"Продление тарифа «{tariff['name']}» ({tariff['duration_days']} дн., сайт)"
            yk_result = await create_yookassa_qr_payment(
                amount_rub=price_rub, order_id=order_id, description=description,
                bot_name=bot_info.username,
            )
        finally:
            await pay_bot.session.close()

        save_anonymous_purchase_payment_id(order_id, yk_result["yookassa_payment_id"])

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
            description = f"Продление тарифа «{tariff['name']}» ({tariff['duration_days']} дн., сайт)"
            yk_result = await create_yookassa_qr_payment(
                amount_rub=price_rub, order_id=order_id, description=description,
                bot_name=bot_info.username,
            )
        finally:
            await pay_bot.session.close()

        save_anonymous_purchase_payment_id(order_id, yk_result["yookassa_payment_id"])

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
        bot_username = None
        try:
            from main import bot as _bot
            if hasattr(_bot, 'my_username') and _bot.my_username:
                bot_username = _bot.my_username
        except Exception:
            pass

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
    if os.path.exists(index_path):
        return web.FileResponse(index_path)
    return web.Response(
        text="<h1>WebApp template not found</h1>", status=404
    )
async def handle_import(request: web.Request) -> web.Response:
    """GET /import — раздаёт страницу-редирект для импорта подписки в Happ/INCY."""
    import_path = os.path.join(_TEMPLATES_DIR, "import.html")
    if os.path.exists(import_path):
        return web.FileResponse(import_path)
    return web.Response(
        text="<h1>Import template not found</h1>", status=404
    )


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

def create_web_app() -> web.Application:
    """Создаёт aiohttp приложение с маршрутами WebApp."""
    app = web.Application(middlewares=[cors_middleware])
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
    app.router.add_post("/api/public/account/session-login", handle_public_account_session_login)
    app.router.add_post("/api/public/account/oauth-exchange", handle_public_account_oauth_exchange)
    app.router.add_post("/api/public/account/link-code", handle_public_account_link_code)
    app.router.add_get("/api/public/account/session", handle_public_account_session)
    app.router.add_post("/api/public/account/logout", handle_public_account_logout)
    app.router.add_post("/api/public/account/key/renew/create", handle_public_account_key_renew_create)
    app.router.add_post("/api/public/account/key/renew/check", handle_public_account_key_renew_check)
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
