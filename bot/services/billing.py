"""
Billing service - payment processing.

Verification of signatures, creation/renewal of keys after payment.
Creating QR payments via YuKassa REST API.
Referral accruals.
"""
import hmac
import hashlib
import logging
import uuid
import base64
import aiohttp
import qrcode
import io
import math
import asyncio
from collections import defaultdict
from typing import Optional, Dict, Any, Tuple

from database.requests import (
    find_order_by_order_id, complete_order, is_order_already_paid,
    get_setting,
    get_yookassa_credentials, get_wata_token, get_platega_credentials,
    get_cardlink_credentials,
    is_referral_enabled, get_referral_reward_type, get_active_referral_levels,
    get_user_referrer, get_user_referral_coefficient, get_user_balance,
    update_referral_stat
)
from bot.services.exchange_rate import get_usd_rub_rate
from bot.services.payment_api import (
    PaymentApiRateLimitError,
    PaymentApiResponseError,
    PaymentApiTransientError,
    payment_client_timeout,
    run_payment_api_operation,
)
from bot.utils.telegram_links import build_telegram_link

logger = logging.getLogger(__name__)

STAR_TO_USD = 0.013
USDT_TO_USD = 1.0

YOOKASSA_API_URL = "https://api.yookassa.ru/v3/payments"
WATA_API_URL = "https://api.wata.pro/api/h2h"
PLATEGA_API_URL = "https://app.platega.io"
CARDLINK_API_URL = "https://cardlink.link"
_payment_order_locks: dict[str, asyncio.Lock] = defaultdict(asyncio.Lock)

# Alphabet for Base62 encoding
ALPHABET = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz"


def _payment_configuration_error(provider: str, details: str) -> ValueError:
    logger.error(
        "Payment API configuration error: provider=%s error=%s",
        provider,
        details,
    )
    return ValueError(details)


def _payment_contract_error(
    provider: str,
    operation: str,
    order_id: str,
    details: str,
) -> PaymentApiResponseError:
    logger.error(
        "Payment API contract error: provider=%s operation=%s order=%s error=%s",
        provider,
        operation,
        order_id,
        details,
    )
    return PaymentApiResponseError(details)


async def _payment_api_json_request(
    *,
    provider: str,
    operation: str,
    order_id: str,
    method: str,
    url: str,
    expected_statuses: tuple[int, ...],
    retry: bool,
    headers: Optional[Dict[str, str]] = None,
    json_payload: Optional[Dict[str, Any]] = None,
    data: Any = None,
    params: Optional[Dict[str, Any]] = None,
) -> Any:
    """Executes one JSON payment API request under the shared reliability policy."""

    async def _request_once() -> Any:
        async with aiohttp.ClientSession(timeout=payment_client_timeout()) as session:
            async with session.request(
                method,
                url,
                headers=headers,
                json=json_payload,
                data=data,
                params=params,
            ) as response:
                if response.status >= 500:
                    raise PaymentApiTransientError(f'HTTP {response.status}')
                if response.status == 429:
                    raise PaymentApiRateLimitError('HTTP 429')
                try:
                    response_data = await response.json(content_type=None)
                except Exception as error:
                    raise PaymentApiResponseError(
                        f'Некорректный JSON-ответ HTTP {response.status}'
                    ) from error
                if response.status not in expected_statuses:
                    error_text = 'Неизвестная ошибка'
                    if isinstance(response_data, dict):
                        raw_error = response_data.get('error')
                        if isinstance(raw_error, dict):
                            raw_error = raw_error.get('description') or raw_error.get('code')
                        error_text = str(
                            response_data.get('message')
                            or response_data.get('description')
                            or raw_error
                            or error_text
                        )
                    raise PaymentApiResponseError(
                        f'HTTP {response.status}: {error_text}'
                    )
                return response_data

    return await run_payment_api_operation(
        provider=provider,
        operation=operation,
        order_id=order_id,
        call=_request_once,
        retry=retry,
    )


def build_payment_return_url(bot_name: str, provider: str, order_id: str) -> str:
    """
    Generates a deep-link return from an external payment form to the bot.

    The start parameter format is the same for QR providers:
    pay_{provider}_{order_id}
    """
    if not bot_name:
        return build_telegram_link()

    provider_code = str(provider or '').strip().lower()
    order_code = str(order_id or '').strip()
    if not provider_code or not order_code:
        return build_telegram_link(bot_name)

    return build_telegram_link(bot_name, f"pay_{provider_code}_{order_code}")




# ============================================================================
# YUKASSA QR-PAYMENT (direct REST API without Telegram Payments)
# ============================================================================

async def create_yookassa_qr_payment(
    amount_rub: float,
    order_id: str,
    description: str,
    bot_name: str,
    metadata: Optional[Dict[str, Any]] = None
) -> Dict[str, Any]:
    """
    Creates a payment in YuKassa REST API with confirmation via a QR code.

    Returns a QR code (PNG) image from a link that can be
    send to the user directly in Telegram as a photo.

    Args:
        amount_rub: Amount in rubles (for example, 299.00)
        order_id: Our internal order (for metadata)
        description: Description of the payment (shown in the payment form)
        metadata: Additional metadata (optional)

    Returns:
        Dictionary with keys:
            - yookassa_payment_id: Payment ID in the Yookassa system
            - qr_image_url: QR code image URL (PNG)
            - qr_url: Raw SBP QR payload (same data encoded in the QR image;
              recognized directly by banking apps when scanned, not just a
              generic webpage link)

    Raises:
        ValueError: If credentials are not configured
        aiohttp.ClientError: If the API is not available
        RuntimeError: If the API returned an error
    """
    shop_id, secret_key = get_yookassa_credentials()
    if not shop_id or not secret_key:
        raise _payment_configuration_error(
            'yookassa',
            'ЮКасса: не настроены shop_id или secret_key',
        )

    # Basic Auth header: base64(shop_id:secret_key)
    credentials = base64.b64encode(f"{shop_id}:{secret_key}".encode()).decode()

    # Idempotency key - unique for this order
    idempotence_key = f"qr-{order_id}-{uuid.uuid4().hex[:8]}"
    return_url = build_payment_return_url(bot_name, 'yookassa', order_id)

    payload = {
        "amount": {
            "value": f"{amount_rub:.2f}",
            "currency": "RUB"
        },
        "capture": True,
        "payment_method_data": {
            "type": "sbp"
        },
        "confirmation": {
            "type": "qr"
        },
        "description": description,
        "receipt": {
            "customer": {
                "email": f"user_{order_id}@t.me"
            },
            "items": [
                {
                    "description": description[:128],
                    "quantity": "1.00",
                    "amount": {
                        "value": f"{amount_rub:.2f}",
                        "currency": "RUB"
                    },
                    "vat_code": 1,
                    "payment_mode": "full_prepayment",
                    "payment_subject": "service"
                }
            ]
        },
        "metadata": {
            "order_id": order_id,
            **(metadata or {})
        }
    }

    headers = {
        "Authorization": f"Basic {credentials}",
        "Idempotence-Key": idempotence_key,
        "Content-Type": "application/json"
    }

    data = await _payment_api_json_request(
        provider='yookassa',
        operation='create',
        order_id=order_id,
        method='POST',
        url=YOOKASSA_API_URL,
        expected_statuses=(200, 201),
        retry=True,
        headers=headers,
        json_payload=payload,
    )
    confirmation = data.get('confirmation', {}) if isinstance(data, dict) else {}
    qr_data = confirmation.get('confirmation_data', '')
    payment_id = data.get('id') if isinstance(data, dict) else None
    if not payment_id or not qr_data:
        raise _payment_contract_error(
            'yookassa',
            'create',
            order_id,
            'ЮКасса API не вернул id или confirmation_data',
        )

    qr = qrcode.QRCode(
        version=1,
        error_correction=qrcode.constants.ERROR_CORRECT_L,
        box_size=10,
        border=4,
    )
    qr.add_data(qr_data)
    qr.make(fit=True)
    img = qr.make_image(fill_color="black", back_color="white")
    bio = io.BytesIO()
    img.save(bio, format="PNG")
    qr_image_data = bio.getvalue()

    logger.info(
        "ЮКасса QR создан: payment_id=%s, order_id=%s, amount=%s RUB",
        payment_id,
        order_id,
        amount_rub,
    )
    return {
        'yookassa_payment_id': payment_id,
        'qr_image_data': qr_image_data,
        'qr_url': qr_data,
        'status': data.get('status', 'pending'),
    }


async def check_yookassa_payment_status(
    yookassa_payment_id: str,
    *,
    order_id: Optional[str] = None,
) -> str:
    """
    Checks the payment status in YuKassa REST API.

    Args:
        yookassa_payment_id: Payment ID in the Yookassa system

    Returns:
        Status line: 'pending', 'waiting_for_capture', 'succeeded', 'canceled'

    Raises:
        ValueError: If credentials are not configured
        aiohttp.ClientError: If the API is not available
        RuntimeError: If the API returned an error
    """
    shop_id, secret_key = get_yookassa_credentials()
    if not shop_id or not secret_key:
        raise _payment_configuration_error(
            'yookassa',
            'ЮКасса: не настроены shop_id или secret_key',
        )

    credentials = base64.b64encode(f"{shop_id}:{secret_key}".encode()).decode()
    headers = {
        "Authorization": f"Basic {credentials}",
        "Content-Type": "application/json"
    }

    url = f"{YOOKASSA_API_URL}/{yookassa_payment_id}"

    data = await _payment_api_json_request(
        provider='yookassa',
        operation='check',
        order_id=order_id or yookassa_payment_id,
        method='GET',
        url=url,
        expected_statuses=(200,),
        retry=True,
        headers=headers,
    )
    if not isinstance(data, dict) or not data.get('status'):
        raise _payment_contract_error(
            'yookassa',
            'check',
            order_id or yookassa_payment_id,
            'ЮКасса API не вернул статус платежа',
        )
    status = data['status']
    logger.debug("ЮКасса payment %s: status=%s", yookassa_payment_id, status)
    return status


# ============================================================================
# WATA - payment by card/SBP via REST API (https://wata.pro/api)
# ============================================================================

async def create_wata_payment(
    amount_rub: float,
    order_id: str,
    description: str,
    bot_name: str
) -> Dict[str, Any]:
    """
    Creates a payment link in WATA via the H2H API.

    POST https://api.wata.pro/api/h2h/links/

    Args:
        amount_rub: Amount in rubles
        order_id: Our internal order_id
        description: Description of the payment
        bot_name: Username of the bot (for building successRedirectUrl)

    Returns:
        Dictionary with keys:
            - wata_link_id: Link ID in the WATA system
            - qr_image_data: PNG bytes of the QR code
            - qr_url: Link for payment (cards/SBP)
            - status: Payment status

    Raises:
        ValueError: If the JWT token is not configured
        RuntimeError: If the API returned an error
    """
    token = get_wata_token()
    if not token:
        raise _payment_configuration_error('wata', 'WATA: JWT-токен не настроен')

    return_url = build_payment_return_url(bot_name, 'wata', order_id)

    payload = {
        "amount": round(float(amount_rub), 2),
        "currency": "RUB",
        "description": description[:255],
        "orderId": order_id,
        "successRedirectUrl": return_url,
        "failRedirectUrl": return_url,
    }

    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "Accept": "application/json",
    }

    url = f"{WATA_API_URL}/links/"

    data = await _payment_api_json_request(
        provider='wata',
        operation='create',
        order_id=order_id,
        method='POST',
        url=url,
        expected_statuses=(200, 201),
        retry=False,
        headers=headers,
        json_payload=payload,
    )
    if not isinstance(data, dict):
        raise _payment_contract_error(
            'wata', 'create', order_id,
            'WATA API вернул ответ неверного типа',
        )
    wata_link_id = data.get('id') or data.get('linkId') or data.get('uuid')
    qr_url = data.get('url') or data.get('paymentUrl')
    if not wata_link_id or not qr_url:
        raise _payment_contract_error(
            'wata', 'create', order_id,
            'WATA API не вернул id или URL платёжной ссылки',
        )

    qr = qrcode.QRCode(
        version=1,
        error_correction=qrcode.constants.ERROR_CORRECT_L,
        box_size=10,
        border=4,
    )
    qr.add_data(qr_url)
    qr.make(fit=True)
    img = qr.make_image(fill_color="black", back_color="white")
    bio = io.BytesIO()
    img.save(bio, format="PNG")
    qr_image_data = bio.getvalue()
    logger.info(
        "WATA ссылка создана: link_id=%s, order_id=%s, amount=%s RUB",
        wata_link_id,
        order_id,
        amount_rub,
    )
    return {
        'wata_link_id': str(wata_link_id),
        'qr_image_data': qr_image_data,
        'qr_url': qr_url,
        'status': str(data.get('status', 'Created')).lower(),
    }


async def check_wata_payment_status(
    wata_link_id: str,
    *,
    order_id: Optional[str] = None,
) -> str:
    """
    Checks the status of the WATA payment link by its ID.

    GET https://api.wata.pro/api/h2h/links/{wata_link_id}

    Endpoint /transactions/?orderId= does not work (404).
    Instead, we check the status of the link itself via /links/{id}.

    WATA has a limit - no more than one request per 30 seconds.
    Request rate control is performed on the handler side.

    Args:
        wata_link_id: WATA link ID (UUID)

    Returns:
        Normalized status: 'pending' | 'succeeded' | 'cancelled'

    Raises:
        ValueError: If the JWT token is not configured
        RuntimeError: If the API returned an error
    """
    token = get_wata_token()
    if not token:
        raise _payment_configuration_error('wata', 'WATA: JWT-токен не настроен')

    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/json",
    }

    url = f"{WATA_API_URL}/links/{wata_link_id}"

    data = await _payment_api_json_request(
        provider='wata',
        operation='check',
        order_id=order_id or wata_link_id,
        method='GET',
        url=url,
        expected_statuses=(200,),
        retry=True,
        headers=headers,
    )
    if not isinstance(data, dict) or not data.get('status'):
        raise _payment_contract_error(
            'wata', 'check', order_id or wata_link_id,
            'WATA API не вернул статус платежа',
        )
    status = str(data['status']).lower()
    logger.debug("WATA link %s: status=%s", wata_link_id, status)
    if status in ('closed', 'paid'):
        return 'succeeded'
    if status in ('declined', 'expired', 'canceled', 'cancelled'):
        return 'canceled'
    return 'pending'


# ============================================================================
# PLATEGA - payment form without a specified method (https://app.platega.io)
# ============================================================================

async def create_platega_payment(
    amount_rub: float,
    order_id: str,
    description: str,
    bot_name: str,
    user_telegram_id: Optional[int] = None,
) -> Dict[str, Any]:
    """
    Creates a transaction in the Platega API.

    POST https://app.platega.io/v2/transaction/process

    Args:
        amount_rub: Amount in rubles
        order_id: Our internal order_id
        description: Description of the payment
        bot_name: Username of the bot (for building return)
        user_telegram_id: Telegram payer ID for metadata.userId

    Returns:
        Dictionary with keys:
            - platega_transaction_id: Transaction ID in the Platega system
            - qr_image_data: PNG bytes of the QR code
            - qr_url: Payment link
            - status: Payment status

    Raises:
        ValueError: If credentials are not configured
        RuntimeError: If the API returned an error
    """
    merchant_id, secret = get_platega_credentials()
    if not merchant_id or not secret:
        raise _payment_configuration_error(
            'platega',
            'Platega: не настроены merchant_id или secret',
        )

    return_url = build_payment_return_url(bot_name, 'platega', order_id)
    fail_url = return_url

    payload = {
        "paymentDetails": {
            "amount": round(float(amount_rub), 2),
            "currency": "RUB",
        },
        "description": description[:255],
        "return": return_url,
        "failedUrl": fail_url,
        "payload": order_id,
    }
    if user_telegram_id is not None:
        payload["metadata"] = {
            "userId": str(user_telegram_id),
        }

    headers = {
        "X-MerchantId": merchant_id,
        "X-Secret": secret,
        "Content-Type": "application/json",
        "Accept": "application/json",
    }

    url = f"{PLATEGA_API_URL}/v2/transaction/process"

    data = await _payment_api_json_request(
        provider='platega',
        operation='create',
        order_id=order_id,
        method='POST',
        url=url,
        expected_statuses=(200, 201),
        retry=False,
        headers=headers,
        json_payload=payload,
    )
    if not isinstance(data, dict):
        raise _payment_contract_error(
            'platega', 'create', order_id,
            'Platega API вернул ответ неверного типа',
        )
    transaction_id = data.get('id') or data.get('transactionId') or data.get('uuid')
    qr_url = (
        data.get('redirect') or data.get('redirectUrl') or
        data.get('url') or data.get('paymentUrl')
    )
    if not transaction_id or not qr_url:
        raise _payment_contract_error(
            'platega', 'create', order_id,
            'Platega API не вернул id или URL платёжной ссылки',
        )

    qr = qrcode.QRCode(
        version=1,
        error_correction=qrcode.constants.ERROR_CORRECT_L,
        box_size=10,
        border=4,
    )
    qr.add_data(qr_url)
    qr.make(fit=True)
    img = qr.make_image(fill_color="black", back_color="white")
    bio = io.BytesIO()
    img.save(bio, format="PNG")
    qr_image_data = bio.getvalue()
    logger.info(
        "Platega транзакция создана: id=%s, order_id=%s, amount=%s RUB",
        transaction_id,
        order_id,
        amount_rub,
    )
    return {
        'platega_transaction_id': str(transaction_id),
        'qr_image_data': qr_image_data,
        'qr_url': qr_url,
        'status': str(data.get('status', 'PENDING')).upper(),
    }


async def check_platega_payment_status(
    transaction_id: str,
    *,
    order_id: Optional[str] = None,
) -> str:
    """
    Checks the status of a Platega transaction.

    GET https://app.platega.io/transaction/{transaction_id}

    Platega statuses:
        - PENDING: in the process of payment
        - CONFIRMED: successfully paid
        - CANCELED: canceled
        - CHARGEBACKED: returnable

    Args:
        transaction_id: Transaction ID in the Platega system

    Returns:
        Normalized status: 'pending' | 'succeeded' | 'cancelled'

    Raises:
        ValueError: If credentials are not configured
        RuntimeError: If the API returned an error
    """
    merchant_id, secret = get_platega_credentials()
    if not merchant_id or not secret:
        raise _payment_configuration_error(
            'platega',
            'Platega: не настроены merchant_id или secret',
        )

    headers = {
        "X-MerchantId": merchant_id,
        "X-Secret": secret,
        "Accept": "application/json",
    }

    url = f"{PLATEGA_API_URL}/transaction/{transaction_id}"

    data = await _payment_api_json_request(
        provider='platega',
        operation='check',
        order_id=order_id or transaction_id,
        method='GET',
        url=url,
        expected_statuses=(200,),
        retry=True,
        headers=headers,
    )
    if not isinstance(data, dict) or not data.get('status'):
        raise _payment_contract_error(
            'platega', 'check', order_id or transaction_id,
            'Platega API не вернул статус платежа',
        )
    status = str(data['status']).upper()
    logger.debug("Platega transaction %s: status=%s", transaction_id, status)
    if status == 'CONFIRMED':
        return 'succeeded'
    if status in ('CANCELED', 'CANCELLED', 'CHARGEBACKED'):
        return 'canceled'
    return 'pending'


# ============================================================================
# CARDLINK - payment by Card/SBP via REST API (https://cardlink.link)
# ============================================================================

async def create_cardlink_payment(
    amount_rub: float,
    order_id: str,
    description: str,
    bot_name: str
) -> Dict[str, Any]:
    """
    Creates a bill in the Cardlink API.

    POST https://cardlink.link/api/v1/bill/create

    The body is sent as application/x-www-form-urlencoded.
    Authorization via Bearer token.

    Distinctive feature: instead of a webhook, the user after payment
    returns to the bot via deep-link `https://<telegram_link_domain>/{bot}?start=cl_Success`
    (or cl_Fail / cl_Result), which triggers the same check as
    “✅ I paid” button.

    Args:
        amount_rub: Amount in rubles
        order_id: Our internal order_id
        description: Payment description (not used by API, but logged)
        bot_name: Username of the bot (to build success_url/fail_url)

    Returns:
        Dictionary with keys:
            - cardlink_bill_id: Account ID in the Cardlink system
            - qr_image_data: PNG bytes of the QR code
            - qr_url: Link to the payment page
            - status: Payment status

    Raises:
        ValueError: If credentials are not configured
        RuntimeError: If the API returned an error
    """
    shop_id, api_token = get_cardlink_credentials()
    if not shop_id or not api_token:
        raise _payment_configuration_error(
            'cardlink',
            'Cardlink: не настроены shop_id или api_token',
        )

    form = aiohttp.FormData()
    form.add_field("shop_id", shop_id)
    form.add_field("amount", f"{float(amount_rub):.2f}")
    form.add_field("order_id", order_id)
    form.add_field("currency_in", "RUB")
    form.add_field("type", "normal")
    form.add_field("description", description[:255])
    form.add_field("name", description[:100])
    return_url = build_payment_return_url(bot_name, 'cardlink', order_id)
    form.add_field("return_url", return_url)
    form.add_field("success_url", return_url)
    form.add_field("fail_url", return_url)
    form.add_field("partner_uuid", "6e7e8f22-3410-4224-8b9c-e61430705963")

    headers = {
        "Authorization": f"Bearer {api_token}",
        "Accept": "application/json",
    }

    url = f"{CARDLINK_API_URL}/api/v1/bill/create"

    data = await _payment_api_json_request(
        provider='cardlink',
        operation='create',
        order_id=order_id,
        method='POST',
        url=url,
        expected_statuses=(200, 201),
        retry=False,
        headers=headers,
        data=form,
    )
    nested = data.get('success') if isinstance(data, dict) else None
    payload = nested if isinstance(nested, dict) else data
    bill_id = (
        payload.get('bill_id') or payload.get('id') or payload.get('uuid')
        if isinstance(payload, dict) else None
    )
    qr_url = (
        payload.get('link_page_url') or payload.get('url') or payload.get('payment_url')
        if isinstance(payload, dict) else None
    )
    if not bill_id or not qr_url:
        raise _payment_contract_error(
            'cardlink', 'create', order_id,
            'Cardlink API не вернул bill_id или URL',
        )

    qr = qrcode.QRCode(
        version=1,
        error_correction=qrcode.constants.ERROR_CORRECT_L,
        box_size=10,
        border=4,
    )
    qr.add_data(qr_url)
    qr.make(fit=True)
    img = qr.make_image(fill_color="black", back_color="white")
    bio = io.BytesIO()
    img.save(bio, format="PNG")
    qr_image_data = bio.getvalue()
    logger.info(
        "Cardlink счёт создан: bill_id=%s, order_id=%s, amount=%s RUB",
        bill_id,
        order_id,
        amount_rub,
    )
    return {
        'cardlink_bill_id': str(bill_id),
        'qr_image_data': qr_image_data,
        'qr_url': qr_url,
        'status': str(payload.get('status', 'NEW')).upper() if isinstance(payload, dict) else 'NEW',
    }


async def check_cardlink_payment_status(
    bill_id: str,
    *,
    order_id: Optional[str] = None,
) -> str:
    """
    Checks Cardlink account status.

    GET https://cardlink.link/api/v1/bill/status?id={bill_id}

    Cardlink statuses:
        - NEW / PROCESS / UNDERPAID: in progress
        - SUCCESS / OVERPAID: successfully paid
        - FAIL: canceled / unsuccessful

    Args:
        bill_id: Account ID in the Cardlink system

    Returns:
        Normalized status: 'pending' | 'succeeded' | 'cancelled'

    Raises:
        ValueError: If credentials are not configured
        RuntimeError: If the API returned an error
    """
    shop_id, api_token = get_cardlink_credentials()
    if not shop_id or not api_token:
        raise _payment_configuration_error(
            'cardlink',
            'Cardlink: не настроены shop_id или api_token',
        )

    headers = {
        "Authorization": f"Bearer {api_token}",
        "Accept": "application/json",
    }

    url = f"{CARDLINK_API_URL}/api/v1/bill/status"
    params = {"id": bill_id}

    data = await _payment_api_json_request(
        provider='cardlink',
        operation='check',
        order_id=order_id or bill_id,
        method='GET',
        url=url,
        expected_statuses=(200,),
        retry=True,
        headers=headers,
        params=params,
    )
    nested = data.get('success') if isinstance(data, dict) else None
    payload = nested if isinstance(nested, dict) else data
    if not isinstance(payload, dict) or not payload.get('status'):
        raise _payment_contract_error(
            'cardlink', 'check', order_id or bill_id,
            'Cardlink API не вернул статус платежа',
        )
    status = str(payload['status']).upper()
    logger.debug("Cardlink bill %s: status=%s", bill_id, status)
    if status in ('SUCCESS', 'OVERPAID'):
        return 'succeeded'
    if status == 'FAIL':
        return 'canceled'
    return 'pending'


def convert_to_rub_cents(amount_raw: int, payment_type: str, usd_rub_rate: int) -> int:
    """
    Convert the raw amount into kopecks of rubles.

    Args:
        amount_raw: raw amount (stars/USDT cents/ruble pennies)
        payment_type: payment type ('stars', 'crypto', 'cards', 'yookassa_qr', 'wata', 'platega')
        usd_rub_rate: USD/RUB rate in kopecks

    Returns:
        Amount in kopecks of rubles
    """
    if payment_type == 'stars':
        usd_cents = int(amount_raw * STAR_TO_USD * 100)
        return usd_cents * usd_rub_rate // 100
    elif payment_type == 'crypto':
        usd_cents = amount_raw
        return usd_cents * usd_rub_rate // 100
    else:
        return amount_raw


async def process_referral_reward(
    payer_id: int,
    period_days: int,
    amount_raw: int,
    payment_type: str,
    bot: Optional[Any] = None,
    order: Optional[Dict[str, Any]] = None,
) -> list[Dict[str, Any]]:
    """
    Processing referral rewards upon payment.
    Called AFTER successful payment processing.
    
    Args:
        payer_id: Internal ID of the user who paid
        period_days: How many days did the referral buy?
        amount_raw: RAW amount:
            - 'stars': number of stars (int)
            - 'crypto': USDT cents (int)
            - 'cards': kopecks of rubles (int)
            - 'yookassa_qr': kopecks of rubles (int)
        payment_type: Payment type ('stars', 'crypto', 'cards', 'yookassa_qr')
    
    Note:
        When paying with balance, referral rewards are NOT accrued,
        therefore this function is not called for balance payments.
    """
    if payment_type in ('balance', 'trial', 'promo_free') or amount_raw <= 0:
        return []

    if not is_referral_enabled():
        return []
    
    reward_type = get_referral_reward_type()
    active_levels = dict(get_active_referral_levels())
    
    if not active_levels:
        return []
    
    usd_rub_rate = await get_usd_rub_rate()
    amount_rub_cents = convert_to_rub_cents(amount_raw, payment_type, usd_rub_rate)
    
    current_user_id = payer_id
    events = []
    
    for level_num in (1, 2, 3):
        referrer_id = get_user_referrer(current_user_id)
        if not referrer_id:
            break

        percent = active_levels.get(level_num)
        if percent is None:
            current_user_id = referrer_id
            continue
        
        coefficient = get_user_referral_coefficient(referrer_id)
        
        if reward_type == 'balance':
            base_reward = amount_rub_cents * (percent / 100)
            final_reward = int(base_reward * coefficient)
            final_reward = round(final_reward / 100) * 100
            reward_days = 0
        else:
            base_days = period_days * (percent / 100)
            final_days = base_days * coefficient
            reward_days = math.ceil(final_days)
            final_reward = 0

        try:
            from bot.utils.policy_registry import apply_referral_reward_policies

            reward_decision = apply_referral_reward_policies(
                {
                    'reward_cents': final_reward,
                    'reward_days': reward_days,
                },
                {
                    'payer_id': payer_id,
                    'referrer_id': referrer_id,
                    'level': level_num,
                    'reward_type': reward_type,
                    'period_days': period_days,
                    'amount_raw': amount_raw,
                    'amount_rub_cents': amount_rub_cents,
                    'payment_type': payment_type,
                    'percent': percent,
                    'coefficient': coefficient,
                    'order': dict(order or {}),
                },
            )
            final_reward = int(reward_decision.get('reward_cents') or 0)
            reward_days = int(reward_decision.get('reward_days') or 0)
            reward_policies = reward_decision.get('reward_policies') or []
            reward_policy = reward_decision.get('reward_policy')
        except Exception as policy_err:
            logger.warning(f'Ошибка referral reward policy для user {payer_id}, level {level_num}: {policy_err}')
            reward_policies = []
            reward_policy = None

        if final_reward > 0:
            from bot.services.balance import credit_user_balance

            balance_result = await credit_user_balance(
                referrer_id,
                final_reward,
                source='referral_reward',
                reason=f'Реферальное вознаграждение, уровень {level_num}',
                reference_type='payment_order',
                reference_id=str((order or {}).get('order_id') or ''),
                metadata={
                    'payer_id': payer_id,
                    'level': level_num,
                    'payment_type': payment_type,
                    'reward_policy': reward_policy,
                },
            )
            if not balance_result.get('ok'):
                final_reward = 0

        if reward_days > 0:
            from bot.services.rewards import grant_days_to_first_active_key

            days_result = await grant_days_to_first_active_key(
                referrer_id,
                reward_days,
                source='referral_reward',
                reason=f'Реферальное вознаграждение, уровень {level_num}',
                reference_type='payment_order',
                reference_id=str((order or {}).get('order_id') or ''),
                metadata={
                    'payer_id': payer_id,
                    'level': level_num,
                    'payment_type': payment_type,
                    'reward_policy': reward_policy,
                },
            )
            if not days_result.get('ok'):
                reward_days = 0

        applied_reward_type = 'balance' if final_reward > 0 else ('days' if reward_days > 0 else reward_type)
        
        update_referral_stat(
            referrer_id, payer_id, level_num,
            final_reward, reward_days
        )

        events.append({
            'referrer_id': referrer_id,
            'payer_id': payer_id,
            'level': level_num,
            'reward_type': applied_reward_type,
            'reward_cents': final_reward,
            'reward_days': reward_days,
            'reward_policy': reward_policy,
            'reward_policies': reward_policies,
            'period_days': period_days,
            'amount_raw': amount_raw,
            'amount_rub_cents': amount_rub_cents,
            'payment_type': payment_type,
        })
        
        current_user_id = referrer_id

    if bot is not None and order is not None and events:
        try:
            from bot.services.notifications import notify_referrers_purchase
            await notify_referrers_purchase(bot, order, events)
        except Exception as notify_err:
            logger.warning(f'Ошибка уведомления рефоводов о покупке: {notify_err}')

    return events


def calculate_balance_discount(user_id: int, tariff_price_cents: int) -> tuple[int, int]:
    """
    Calculate discount from balance. NO write-off!
    
    Args:
        user_id: Internal user ID
        tariff_price_cents: Tariff price in kopecks
    
    Returns:
        Tuple (remaining_to_pay_cents, to_deduct_cents):
        - remaining_to_pay_cents: how much you need to pay externally
        - to_deduct_cents: how much will be debited from the balance IF SUCCESSFUL payment
    """
    balance = get_user_balance(user_id)
    
    if balance >= tariff_price_cents:
        return 0, tariff_price_cents
    else:
        return tariff_price_cents - balance, balance


async def _show_complete_payment_status(
    message,
    *,
    title_html: str,
    body_text: str,
    reply_markup=None,
) -> None:
    """Shows the page-backed status of the overall post-payment flow."""
    from bot.handlers.user.payments.status_page import show_payment_status_message

    await show_payment_status_message(
        message,
        title_html=title_html,
        body_text=body_text,
        payment_provider_title='Payment',
        reply_markup=reply_markup,
    )


def _payment_order_referral_amount(order: Dict[str, Any]) -> int:
    """Returns the persisted amount used by post-payment referral processing."""
    try:
        if order.get('final_amount_cents') is not None:
            return int(order.get('final_amount_cents') or 0)
        return int(order.get('amount_cents') or 0)
    except (TypeError, ValueError):
        return 0


async def _run_payment_post_actions(
    order: Dict[str, Any],
    *,
    bot: Any,
    payment_type: str,
    referral_amount: int,
    balance_override_cents: int = 0,
    force: bool = False,
) -> None:
    """Runs first-processing-only financial and notification side effects."""
    if not order.get('_payment_processed_now', True) and not force:
        logger.info("Повторная обработка платежа %s: побочные действия пропущены", order.get('order_id'))
        return

    user_internal_id = int(order['user_id'])
    persisted_balance = int(order.get('balance_deduct_cents') or 0)
    balance_to_deduct = persisted_balance or max(0, int(balance_override_cents or 0))
    if balance_to_deduct > 0:
        from database.requests import has_balance_operation_reference

        order_reference = str(order.get('order_id') or '')
        already_debited = has_balance_operation_reference(
            user_id=user_internal_id,
            operation_type='debit',
            source='payment_balance',
            reference_type='payment_order',
            reference_id=order_reference,
        )
        current_balance = get_user_balance(user_internal_id)
        actual_deduct = 0 if already_debited else min(balance_to_deduct, current_balance)
        if actual_deduct > 0:
            from bot.services.balance import debit_user_balance

            deduct_result = await debit_user_balance(
                user_internal_id,
                actual_deduct,
                source='payment_balance',
                reason='Списание баланса при оплате тарифа',
                reference_type='payment_order',
                reference_id=order_reference,
                metadata={'payment_type': payment_type},
            )
            if not deduct_result.get('ok'):
                raise RuntimeError(
                    f"Не удалось списать сохранённую часть баланса: {deduct_result.get('status')}"
                )
            logger.info(
                "Списано %s коп с баланса user=%s при частичной оплате (%s)",
                actual_deduct,
                user_internal_id,
                payment_type,
            )

    days = order.get('period_days') or order.get('duration_days') or 30
    await process_referral_reward(
        user_internal_id,
        days,
        referral_amount,
        payment_type,
        bot=bot,
        order=order,
    )

    try:
        from bot.services.notifications import notify_admins_payment

        await notify_admins_payment(bot, order)
    except Exception as notify_err:
        logger.warning("Ошибка уведомления об оплате order=%s: %s", order.get('order_id'), notify_err)


async def _notify_automatic_payment_user(bot: Any, order: Dict[str, Any]) -> bool:
    """Notifies a user that background polling completed the payment."""
    from database.requests import get_user_by_id, mark_user_bot_blocked
    from bot.keyboards.user import payment_auto_complete_kb
    from bot.utils.delivery import is_bot_blocked_error

    user = get_user_by_id(int(order.get('user_id') or 0))
    telegram_id = int((user or {}).get('telegram_id') or 0)
    if not telegram_id:
        return False
    try:
        await bot.send_message(
            telegram_id,
            (
                '✅ <b>Оплата получена</b>\n\n'
                'Платёж обработан автоматически. Откройте «Мои ключи», '
                'чтобы настроить или посмотреть доступ.'
            ),
            parse_mode='HTML',
            reply_markup=payment_auto_complete_kb(),
        )
        return True
    except Exception as error:
        if is_bot_blocked_error(error):
            mark_user_bot_blocked(telegram_id)
        logger.warning(
            "Не удалось уведомить пользователя об автозавершении order=%s: %s",
            order.get('order_id'),
            error,
        )
        return False


async def process_payment_order(
    order_id: str,
    *,
    bot: Any = None,
    process_referrals: bool = False,
) -> Tuple[bool, str, Optional[Dict[str, Any]]]:
    """
    Завершает оплаченный заказ: отмечает его оплаченным (идемпотентно —
    повторный вызов для уже оплаченного заказа не создаёт новый ключ и не
    продлевает повторно), затем либо создаёт черновой VPN-ключ (для новой
    покупки — сервер пользователь выберет отдельно), либо продлевает
    существующий ключ (для продления подписки).

    Args:
        order_id: ID заказа
        bot: экземпляр бота (нужен только если process_referrals=True)
        process_referrals: обрабатывать ли реферальное начисление здесь.
            По умолчанию False — обычно начисление делает отдельно
            _run_payment_post_actions после вызова этой функции.

    Returns:
        (success, text, order) — order содержит флаг '_payment_processed_now':
        True, если это первая обработка данного заказа (чтобы вызывающий
        код не повторял финансовые побочные эффекты при повторных
        вызовах — например, от автопроверки платежей).
    """
    from database.requests import (
        update_payment_key_id, create_initial_vpn_key, extend_vpn_key,
    )
    from database.db_tariffs import get_tariff_by_id
    from bot.services.vpn_api import extend_key_on_server

    order = find_order_by_order_id(order_id)
    if not order:
        return False, "Заказ не найден.", None

    already_paid = is_order_already_paid(order_id)
    order['_payment_processed_now'] = not already_paid

    if not already_paid:
        complete_order(order_id)
        order = find_order_by_order_id(order_id) or order
        order['_payment_processed_now'] = True

    days = order.get('duration_days') or order.get('period_days') or 30
    vpn_key_id = order.get('vpn_key_id')
    tariff_id = order.get('tariff_id')

    if not tariff_id and not vpn_key_id:
        # Пополнение личного баланса — не покупка тарифа и не продление ключа
        if order['_payment_processed_now']:
            from bot.services.balance import credit_user_balance
            amount_cents = int(order.get('final_amount_cents') or order.get('amount_cents') or 0)
            await credit_user_balance(
                int(order['user_id']),
                amount_cents,
                source='topup',
                reason='Пополнение личного баланса',
                reference_type='payment',
                reference_id=order_id,
            )
        amount_rub = int(order.get('final_amount_cents') or order.get('amount_cents') or 0) / 100
        text = f"✅ Баланс пополнен на {amount_rub:.2f} ₽!"
        return True, text, order

    if vpn_key_id:
        # Продление уже существующего ключа
        if order['_payment_processed_now']:
            extend_vpn_key(vpn_key_id, days)
            try:
                await extend_key_on_server(vpn_key_id, days)
            except Exception as e:
                logger.error(f"Не удалось продлить ключ {vpn_key_id} на панели для заказа {order_id}: {e}")
        text = f"✅ Подписка продлена на {days} дней!"
    else:
        # Новая покупка — черновой ключ, сервер выбирается пользователем позже
        if order['_payment_processed_now']:
            tariff_id = order.get('tariff_id')
            tariff = get_tariff_by_id(tariff_id) if tariff_id else None
            traffic_limit = int((tariff.get('traffic_limit_gb', 0) or 0) * 1024 ** 3) if tariff else 0
            new_key_id = create_initial_vpn_key(order['user_id'], tariff_id, days, traffic_limit)
            update_payment_key_id(order_id, new_key_id)
            order['vpn_key_id'] = new_key_id
        text = "✅ Оплата прошла успешно! Выберите сервер для подключения."

    if process_referrals and order['_payment_processed_now']:
        try:
            await process_referral_reward(
                int(order['user_id']),
                days,
                _payment_order_referral_amount(order),
                str(order.get('payment_type') or ''),
                bot=bot,
                order=order,
            )
        except Exception as e:
            logger.error(f"Ошибка обработки реферального начисления для заказа {order_id}: {e}")

    return True, text, order


async def complete_payment_order_background(
    order_id: str,
    *,
    bot: Any,
    notify_user: bool = True,
    retry_post_actions: bool = False,
) -> Dict[str, Any]:
    """Completes a provider-confirmed order without Telegram callback or FSM state."""
    success, text, order = await process_payment_order(
        order_id,
        bot=bot,
        process_referrals=False,
    )
    result: Dict[str, Any] = {
        'ok': bool(success and order),
        'text': text,
        'order': order,
        'processed_now': bool(order and order.get('_payment_processed_now', True)),
        'user_notified': False,
    }
    if not success or not order:
        return result

    if result['processed_now'] or retry_post_actions:
        payment_type = str(order.get('payment_type') or '')
        await _run_payment_post_actions(
            order,
            bot=bot,
            payment_type=payment_type,
            referral_amount=_payment_order_referral_amount(order),
            force=retry_post_actions,
        )
        if notify_user:
            result['user_notified'] = await _notify_automatic_payment_user(bot, order)
    return result


async def complete_payment_flow(
    order_id: str,
    message,
    state,
    telegram_id: int,
    payment_type: str,
    referral_amount: int
) -> None:
    """
    Single post-payment flow after payment confirmation.
    
    Performs:
    1. Order processing (process_payment_order)
    2. Write off the balance (if partial payment)
    3. Accrual of referral reward
    4. Finalization of the UI (issuing a key / showing the result)
    
    Called from:
    - successful_payment_handler (Stars/TG payments) — base.py
    - check_yookassa_payment (Yukassa) - yookassa.py
    
    Args:
        order_id: Order ID
        message: Message to respond to the user
        state: FSM context (for balance and cleanup)
        telegram_id: Telegram user ID
        payment_type: Payment type ('stars', 'cards', 'yookassa_qr')
        referral_amount: Raw amount for referral reward:
            - 'stars': number of stars
            - 'cards': pennies of rubles
            - 'yookassa_qr': pennies of rubles
    """
    from bot.handlers.user.payments.base import finalize_payment_ui
    state_data = await state.get_data()
    balance_to_deduct = state_data.get('balance_to_deduct', 0)
    
    try:
        (success, text, order) = await process_payment_order(
            order_id,
            bot=message.bot,
            process_referrals=False,
        )
        
        if success and order:
            await _run_payment_post_actions(
                order,
                bot=message.bot,
                payment_type=payment_type,
                referral_amount=referral_amount,
                balance_override_cents=balance_to_deduct,
            )

            # Clearing FSM balance data
            await state.update_data(balance_to_deduct=0, remaining_cents=0)
            
            # UI finalization
            await finalize_payment_ui(message, state, text, order, user_id=telegram_id)
        else:
            from bot.keyboards.admin import home_only_kb

            await _show_complete_payment_status(
                message,
                title_html='❌ <b>Платёж не обработан</b>',
                body_text=text,
                reply_markup=home_only_kb(),
            )
    
    except Exception as e:
        from bot.errors import TariffNotFoundError
        if isinstance(e, TariffNotFoundError):
            from bot.keyboards.support import support_contact_kb
            support_link = get_setting('support_channel_link', build_telegram_link('ECLIPSESupport'))
            await _show_complete_payment_status(
                message,
                title_html='⚠️ <b>Тариф не найден</b>',
                body_text=str(e),
                reply_markup=support_contact_kb(support_link),
            )
        else:
            logger.exception(f'Ошибка обработки {payment_type} платежа: {e}')
            await _show_complete_payment_status(
                message,
                title_html='❌ <b>Ошибка обработки платежа</b>',
                body_text='Произошла ошибка при обработке платежа.',
            )

