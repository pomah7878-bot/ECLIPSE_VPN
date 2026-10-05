"""Резерв баланса под частичную оплату «баланс + карта/СБП» (v1.195).

Часть цены, оплачиваемая с баланса, списывается сразу при создании счёта
(атомарно) и запоминается в заказе. Если заказ отменён или устарел, списанное
возвращается. Списание делается с теми же reference, что использует финальная
обработка платежа, поэтому второй раз при завершении оно не повторяется.
"""
import logging
import threading
from typing import Optional

from database.connection import get_db
from database.db_business_operations import apply_balance_operation, has_balance_operation_reference

logger = logging.getLogger(__name__)

# проверка «уже списано» и само списание — один шаг (двойной клик / параллельные запросы)
_LOCK = threading.RLock()

DEBIT_SOURCE = 'payment_balance'
DEBIT_REF = 'payment_order'
REFUND_SOURCE = 'refund'
REFUND_REF = 'payment_order_refund'


def _order_row(order_id: str):
    with get_db() as conn:
        row = conn.execute(
            "SELECT order_id, user_id, status, payment_type, balance_deduct_cents "
            "FROM payments WHERE order_id = ?",
            (str(order_id),),
        ).fetchone()
        return dict(row) if row else None


def reserve_balance_for_order(order_id: str, user_id: int, cents: int) -> bool:
    with _LOCK:
        return _reserve_locked(order_id, user_id, cents)


def _reserve_locked(order_id: str, user_id: int, cents: int) -> bool:
    """Списывает cents с баланса под заказ. False — заказ не ожидает оплаты или не хватает денег."""
    cents = int(cents or 0)
    if cents <= 0:
        return True
    order = _order_row(order_id)
    if not order or order['status'] != 'pending' or int(order['user_id']) != int(user_id):
        return False
    if has_balance_operation_reference(
        user_id=int(user_id), operation_type='debit', source=DEBIT_SOURCE,
        reference_type=DEBIT_REF, reference_id=str(order_id),
    ):
        _save_deduction(order_id, cents)
        return True
    result = apply_balance_operation(
        user_id=int(user_id),
        operation_type='debit',
        cents=cents,
        source=DEBIT_SOURCE,
        reason='Резерв баланса под оплату заказа',
        reference_type=DEBIT_REF,
        reference_id=str(order_id),
        metadata={'reserve': True},
    )
    if not result.get('ok'):
        return False
    _save_deduction(order_id, cents)
    return True


def _save_deduction(order_id: str, cents: int) -> None:
    with get_db() as conn:
        conn.execute(
            "UPDATE payments SET balance_deduct_cents = ? WHERE order_id = ?",
            (int(cents), str(order_id)),
        )


def release_balance_reservation(order_id: str) -> bool:
    with _LOCK:
        return _release_locked(order_id)


def _release_locked(order_id: str) -> bool:
    """Возвращает зарезервированное, если заказ не оплачен. Идемпотентно."""
    order = _order_row(order_id)
    if not order or order['status'] == 'paid':
        return False
    amount = int(order.get('balance_deduct_cents') or 0)
    if amount <= 0:
        return False
    user_id = int(order['user_id'])
    if not has_balance_operation_reference(
        user_id=user_id, operation_type='debit', source=DEBIT_SOURCE,
        reference_type=DEBIT_REF, reference_id=str(order_id),
    ):
        return False
    if has_balance_operation_reference(
        user_id=user_id, operation_type='credit', source=REFUND_SOURCE,
        reference_type=REFUND_REF, reference_id=str(order_id),
    ):
        return False
    result = apply_balance_operation(
        user_id=user_id,
        operation_type='credit',
        cents=amount,
        source=REFUND_SOURCE,
        reason='Возврат резерва: заказ не оплачен',
        reference_type=REFUND_REF,
        reference_id=str(order_id),
    )
    ok = bool(result.get('ok'))
    if ok:
        logger.info("Возвращён резерв баланса %s коп по заказу %s", amount, order_id)
    return ok


def release_stale_balance_reservations(card_minutes: int = 180, provider_hours: int = 24) -> int:
    """Отменяет давно ожидающие заказы с резервом и возвращает деньги.

    Счета Telegram (cards) — после card_minutes (запоздавшая оплата отклонится на
    pre_checkout). Остальные способы (СБП и т.п.) — после provider_hours: за это
    время провайдер уже закрыл платёж.
    """
    released = 0
    with get_db() as conn:
        rows = conn.execute(
            """
            SELECT p.order_id AS order_id
            FROM payments p
            JOIN balance_operations bo
              ON bo.reference_id = p.order_id
             AND bo.reference_type = 'payment_order'
             AND bo.source = 'payment_balance'
             AND bo.operation_type = 'debit'
            WHERE p.status = 'pending' AND COALESCE(p.balance_deduct_cents, 0) > 0
              AND (
                (p.payment_type = 'cards' AND bo.created_at < datetime('now', ?))
                OR bo.created_at < datetime('now', ?)
              )
            """,
            (f"-{int(card_minutes)} minutes", f"-{int(provider_hours)} hours"),
        ).fetchall()
    from database.db_payments import cancel_pending_order
    for row in rows:
        try:
            if cancel_pending_order(row['order_id']):
                released += 1
        except Exception as e:
            logger.error("Не удалось снять резерв по заказу %s: %s", row['order_id'], e)
    return released
