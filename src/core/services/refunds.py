"""Список «к возврату»: оплаты, деньги по которым нужно вернуть клиенту.

Возвратов через API в системе нет (решение от 09.10): владелица возвращает деньги
в кабинете Робокассы и аннулирует чек в «Мой налог». Этот модуль только помнит, кому
вернуть, и даёт отметить «вернула», чтобы ни один возврат не потерялся в переписке.

Запись появляется сама:
- оплата пришла по заказу, который отменили клиент или владелица;
- заказ уже был оплачен, а пришла ещё одна оплата;
- владелица отменила уже оплаченный заказ.
"""

from __future__ import annotations

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from core.clock import now_utc
from core.enums import OrderEventType, PaymentStatus, RefundReason, RefundStatus
from core.errors import ConflictError, NotFoundError
from core.models import Order, Payment, Refund
from core.services import orders


async def require(
    session: AsyncSession,
    payment: Payment,
    order: Order,
    reason: RefundReason,
    *,
    actor: str,
) -> Refund:
    """Поставить оплату в список «к возврату». Повторный вызов вернёт ту же запись."""
    existing = await session.scalar(select(Refund).where(Refund.payment_id == payment.id))
    if existing is not None:
        return existing
    refund = Refund(
        order_id=order.id,
        payment_id=payment.id,
        amount_kopecks=payment.amount_kopecks,
        reason=reason,
        status=RefundStatus.PENDING,
    )
    session.add(refund)
    await session.flush()
    await orders.log_event(
        session,
        order,
        OrderEventType.REFUND_REQUIRED,
        payload={
            "refund_id": refund.id,
            "payment_id": payment.id,
            "amount_kopecks": refund.amount_kopecks,
            "reason": reason.value,
        },
        actor=actor,
    )
    return refund


async def require_for_paid_order(session: AsyncSession, order: Order, *, actor: str) -> None:
    """Оплаченный заказ отменили — деньги за его оплату нужно вернуть.

    Берём попытку со статусом PAID — ту, что оплатила заказ. Повторная оплата тоже
    с paid_at, но она «отменённая» и уже в списке: по самой свежей paid_at основная
    оплата в список не попала бы.
    """
    payment = await session.scalar(
        select(Payment).where(Payment.order_id == order.id, Payment.status == PaymentStatus.PAID),
    )
    if payment is not None:
        await require(session, payment, order, RefundReason.CANCELLED_AFTER_PAYMENT, actor=actor)


async def complete(
    session: AsyncSession,
    refund_id: int,
    *,
    actor: str,
    note: str | None = None,
) -> Refund:
    """Владелица вернула деньги в кабинете Робокассы."""
    refund = await session.scalar(
        select(Refund).where(Refund.id == refund_id).with_for_update(),
    )
    if refund is None:
        raise NotFoundError("Возврат не найден")
    if refund.status == RefundStatus.DONE:
        raise ConflictError("Этот возврат уже отмечен")
    refund.status = RefundStatus.DONE
    refund.completed_at = now_utc()
    refund.completed_by = actor
    refund.note = (note or "").strip() or None
    await session.flush()

    order = await orders.get_order(session, refund.order_id)
    await orders.log_event(
        session,
        order,
        OrderEventType.REFUND_DONE,
        payload={"refund_id": refund.id, "amount_kopecks": refund.amount_kopecks},
        actor=actor,
    )
    return refund


async def count_pending(session: AsyncSession) -> int:
    count = await session.scalar(
        select(func.count(Refund.id)).where(Refund.status == RefundStatus.PENDING),
    )
    return int(count or 0)


async def list_refunds(
    session: AsyncSession,
    status: RefundStatus,
    *,
    limit: int = 100,
) -> list[Refund]:
    """Возвраты в этом состоянии: ждущие — старые сверху, сделанные — свежие сверху."""
    order_by = (
        Refund.created_at.asc() if status == RefundStatus.PENDING else Refund.completed_at.desc()
    )
    result = await session.scalars(
        select(Refund)
        .options(
            selectinload(Refund.order).selectinload(Order.customer),
            selectinload(Refund.payment),
        )
        .where(Refund.status == status)
        .order_by(order_by)
        .limit(limit),
    )
    return list(result)


async def list_for_order(session: AsyncSession, order_id: int) -> list[Refund]:
    result = await session.scalars(
        select(Refund).where(Refund.order_id == order_id).order_by(Refund.created_at.desc()),
    )
    return list(result)
