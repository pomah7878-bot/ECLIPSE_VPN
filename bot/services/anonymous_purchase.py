"""
Провижининг VPN-ключей для анонимных покупок через публичную страницу
(без Telegram) — см. также database/db_payments.py (anonymous_purchases)
и bot/webapp/server.py (публичные эндпоинты /api/public/*).
"""
import logging
import time
import uuid as uuid_module

logger = logging.getLogger(__name__)


async def provision_anonymous_vpn_key(tariff_id: int, order_id: str) -> dict:
    """
    Создаёт полностью рабочий VPN-ключ для анонимной покупки — сразу,
    без ожидания захода в Telegram-бота.

    Использует служебного "владельца"-заполнителя с заведомо невозможным
    для реального Telegram отрицательным ID (чтобы не путать с настоящими
    аккаунтами). При claim'е (см. claim_anonymous_purchase) ключ
    перепривязывается к реальному аккаунту клиента в нашей базе — сама
    панель 3x-ui не обновляется (это не влияет на работу бота, только
    на отображение владельца в самой панели для админа).

    Returns:
        dict с ключами: key_id, sub_url (может быть None), placeholder_user_id

    Raises:
        RuntimeError: если тариф/сервер не найден или провижининг не удался
    """
    from database.requests import (
        get_tariff_by_id, get_active_servers, create_initial_vpn_key,
        update_vpn_key_config, get_or_create_user,
    )
    from bot.services.vpn_api import get_client, get_client_subscription_inbounds, sync_key_to_panel_state, get_subscription_url_for_key
    from bot.handlers.admin.users_keys import generate_unique_email

    tariff = get_tariff_by_id(tariff_id)
    if not tariff:
        raise RuntimeError("Тариф не найден")

    servers = get_active_servers()
    if not servers:
        raise RuntimeError("Нет доступных серверов")
    server = servers[0]
    server_id = server["id"]

    # Заведомо невозможный для реального Telegram ID — отрицательный,
    # на основе текущего времени в миллисекундах (гарантированно уникален)
    placeholder_tg_id = -int(time.time() * 1000)
    placeholder_username = f"anon_{order_id}"
    owner, _ = get_or_create_user(placeholder_tg_id, username=placeholder_username)
    owner_user_id = owner["id"]

    days = tariff.get("duration_days") or 30
    traffic_limit_bytes = (tariff.get("traffic_limit_gb", 0) or 0) * 1024 ** 3

    key_id = create_initial_vpn_key(owner_user_id, tariff_id, days, traffic_limit=traffic_limit_bytes)

    user_fake_dict = {"telegram_id": placeholder_tg_id, "username": placeholder_username}
    panel_email = generate_unique_email(user_fake_dict)
    sub_id = uuid_module.uuid4().hex

    client = await get_client(server_id)
    inbounds = await get_client_subscription_inbounds(client)
    if not inbounds:
        raise RuntimeError("На сервере нет доступных inbound")

    limit_gb = tariff.get("traffic_limit_gb", 0) or 0
    max_ips = tariff.get("max_ips", 1) or 1

    first_uuid = None
    first_inbound_id = None
    ready_count = 0
    for inb in inbounds:
        try:
            flow = await client.get_inbound_flow(inb["id"])
            res = await client.add_client(
                inbound_id=inb["id"], email=panel_email, total_gb=limit_gb,
                expire_days=days, limit_ip=max_ips, enable=True,
                tg_id=str(placeholder_tg_id), flow=flow, sub_id=sub_id,
            )
            if first_uuid is None or inb["id"] < first_inbound_id:
                first_uuid = res["uuid"]
                first_inbound_id = inb["id"]
            ready_count += 1
        except Exception as e:
            logger.warning(f"anonymous provisioning: не удалось создать клиента в inbound {inb['id']}: {e}")

    if ready_count == 0 or first_uuid is None or first_inbound_id is None:
        raise RuntimeError("Не удалось создать ни одного клиента на сервере")

    update_vpn_key_config(
        key_id=key_id, server_id=server_id, panel_inbound_id=first_inbound_id,
        panel_email=panel_email, client_uuid=first_uuid, sub_id=sub_id,
    )

    try:
        sync_stats = await sync_key_to_panel_state(key_id)
        if not sync_stats.get("ok", True):
            logger.warning(f"anonymous provisioning: ключ {key_id} синхронизирован не полностью: {sync_stats}")
    except Exception as e:
        logger.warning(f"anonymous provisioning: sync_key_to_panel_state упал для ключа {key_id}: {e}")

    sub_url = None
    try:
        sub_url = await get_subscription_url_for_key({"sub_id": sub_id, "server_id": server_id})
    except Exception as e:
        logger.warning(f"anonymous provisioning: не удалось получить sub_url для ключа {key_id}: {e}")

    return {"key_id": key_id, "sub_url": sub_url, "placeholder_user_id": owner_user_id}


async def get_account_info_by_claim_code(claim_code: str) -> dict:
    """
    Возвращает информацию о ключе для личного кабинета по коду доступа
    (тот же claim_code, что и для привязки к Telegram) — трафик, срок
    действия, ссылка подписки.

    Returns:
        dict: {"ok": bool, "message"?: str, ...данные ключа при ok=True}
    """
    from database.requests import get_anonymous_purchase_by_claim_code, get_key_details_by_id

    purchase = get_anonymous_purchase_by_claim_code(claim_code)
    if not purchase or not purchase.get("vpn_key_id"):
        return {"ok": False, "message": "Код не найден или ключ ещё не готов. Проверьте правильность ввода."}

    key = get_key_details_by_id(purchase["vpn_key_id"])
    if not key:
        return {"ok": False, "message": "Ключ не найден. Обратитесь в поддержку."}

    return {
        "ok": True,
        "key_id": key["id"],
        "sub_url": purchase.get("sub_url"),
        "tariff_name": key.get("tariff_name"),
        "expires_at": key.get("expires_at"),
        "traffic_used": key.get("traffic_used") or 0,
        "traffic_limit": key.get("traffic_limit") or 0,
        "is_active": bool(key.get("is_active")),
        "already_claimed_to_telegram": purchase.get("status") == "claimed",
    }


async def renew_anonymous_vpn_key(key_id: int, tariff_id: int) -> dict:
    """
    Продлевает уже существующий ключ (личный кабинет на сайте) — без
    выпуска нового, без затрагивания панели напрямую (используется тот же
    надёжный путь, что и в самом боте).

    Returns:
        dict: {"ok": bool, "message": str}
    """
    from database.requests import get_tariff_by_id
    from bot.services.key_lifecycle import renew_key_access

    tariff = get_tariff_by_id(tariff_id)
    if not tariff:
        return {"ok": False, "message": "Тариф не найден."}

    days = tariff.get("duration_days") or 30
    result = await renew_key_access(key_id, days, reset_traffic=True, tariff_id=tariff_id)

    if not result.get("db_updated"):
        return {"ok": False, "message": "Не удалось продлить ключ. Обратитесь в поддержку."}

    return {"ok": True, "message": f"Ключ продлён на {days} дней."}


async def claim_anonymous_purchase(claim_code: str, telegram_id: int, username: str | None = None) -> dict:
    """
    Привязывает ранее купленный анонимно ключ к реальному Telegram-аккаунту.

    Returns:
        dict: {"ok": bool, "message": str, "key_id": int | None}
    """
    from database.requests import (
        get_anonymous_purchase_by_claim_code, mark_anonymous_purchase_claimed,
        get_or_create_user, reassign_vpn_key_owner, link_oauth_to_site_account,
    )

    purchase = get_anonymous_purchase_by_claim_code(claim_code)
    if not purchase:
        return {"ok": False, "message": "Код не найден. Проверьте правильность ввода.", "key_id": None}

    if purchase["status"] == "claimed":
        if purchase.get("claimed_by_telegram_id") == telegram_id:
            # Ретроактивно связываем OAuth-аккаунт, если это не было сделано
            # раньше (например, покупка/клейм были до появления этой логики).
            site_account_id = purchase.get("site_account_id")
            if site_account_id:
                try:
                    link_oauth_to_site_account(site_account_id, telegram_id)
                except Exception as e:
                    logger.warning(f"Не удалось связать site_account {site_account_id} с telegram_id {telegram_id}: {e}")
            return {"ok": True, "message": "Этот ключ уже привязан к вашему аккаунту — он в разделе «Мои ключи».", "key_id": purchase.get("claimed_vpn_key_id")}
        return {"ok": False, "message": "Этот код уже был использован ранее.", "key_id": None}

    if purchase["status"] != "paid":
        return {"ok": False, "message": "Оплата по этому коду ещё не подтверждена. Подождите немного и попробуйте снова.", "key_id": None}

    key_id = purchase.get("vpn_key_id")
    if not key_id:
        return {"ok": False, "message": "Ключ ещё не готов. Подождите немного и попробуйте снова.", "key_id": None}

    real_user, _ = get_or_create_user(telegram_id, username=username)
    real_user_id = real_user["id"]

    if not reassign_vpn_key_owner(key_id, real_user_id):
        return {"ok": False, "message": "Не удалось привязать ключ. Обратитесь в поддержку.", "key_id": None}

    mark_anonymous_purchase_claimed(claim_code, telegram_id, key_id)

    # Если покупка была сделана через сайт под OAuth-аккаунтом (Google/Яндекс/VK) —
    # связываем ЭТОТ аккаунт с реальным telegram_id, чтобы при следующем входе
    # через тот же OAuth сразу показывались все реальные ключи, а не только
    # тот, что был куплен изначально.
    site_account_id = purchase.get("site_account_id")
    if site_account_id:
        try:
            link_oauth_to_site_account(site_account_id, telegram_id)
        except Exception as e:
            logger.warning(f"Не удалось связать site_account {site_account_id} с telegram_id {telegram_id}: {e}")

    return {"ok": True, "message": "Готово! Ключ теперь в разделе «Мои ключи».", "key_id": key_id}
